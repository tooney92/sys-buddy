"""Read-only HTTP API for the dashboard (SPEC §11), with server-side scoping.

Guiding principle (SPEC §0): **the broker enforces, agents/clients request.** Viewer
scoping lives here, not in the browser. A buddy's ``viewer_token`` is bound to one
task, so ``/api/tasks`` returns exactly that one task and ``/api/task/{id}`` refuses
any other id with 403. The client never filters for security — the server only ever
hands back what the token permits (SPEC §9, §12 "two corrections to the prototype").

Every route is read-only, and stays that way (DECISIONS D11): the dashboard surfaces
state and tells the human what to type — it never acts. The viewer token is
read-scoped (D7), so a leaked ``?v=`` link must only ever be able to LOOK; a single
write route here would be the first crack in that. Host ACTIONS live in the CLI
(``sys-buddy todo drop``) and the desktop app, never behind this origin.

The query logic is factored into ``_``-prefixed helpers that take an open connection
so they can be unit-tested without a running HTTP server (see ``tests/test_api.py``);
the ``async def`` handlers are thin shells that resolve the viewer token, enforce
scope, open/close a connection, and serialise.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from . import identity, readiness, service, todos
from .config import Config
from .db import connect
from .identity import ViewerIdentity

# HTTP verb set is small; the event ``kind`` set is fixed by the state machine.
# ``todo`` is ONE kind carrying the specific action inside its detail (todos._event),
# so the dashboard's filter vocabulary stays fixed as the todo actions grow.
_EVENT_KINDS = {"task", "transition", "lock", "deploy", "test", "slack", "token", "todo"}

# Which todo a thread message belongs to, for the UI's ``⟨api123⟩`` chip. There is no
# ``messages.todo_id`` column — that is a schema change owned elsewhere — but every
# message ``todos.py`` writes names its deliverable as "todo #N", so the chip is
# DERIVED from that reference and then validated against the task's real todo ids
# (see ``_messages_for``). Unmatched bodies simply get no chip.
_TODO_REF_RE = re.compile(r"\btodos?\s*#(\d+)", re.IGNORECASE)

# Sort buckets for the todo panel. A PENDING todo is the only thing on that screen
# blocking a human — it is a request, not progress — so it sorts to the top; a dropped
# one no longer counts toward the task, so it sinks. Everything in between keeps
# creation order, which is the order the humans discussed it in.
_TODO_ORDER = {todos.PENDING: 0, todos.DROPPED: 2}


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _hhmm(ts: float | None) -> str:
    """A wall-clock ``HH:MM`` for the timeline/thread (mono in the design)."""
    if ts is None:
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


def _time_ago(ts: float | None, *, now: float | None = None) -> str:
    """A coarse "time ago" for the task-list ``last`` column.

    Coarse on purpose: the list refreshes every ~3s and only needs a glanceable
    recency, not a precise duration. ``now`` is injectable so the derivation is
    deterministic under test.
    """
    if ts is None:
        return ""
    delta = (now if now is not None else time.time()) - ts
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _render_detail(kind: str, detail: dict) -> str:
    """Render a short human string for the event log from ``detail_json``.

    The state machine writes a fixed detail shape per kind (see the task's
    EVENT-LOG CONVENTION); we mirror those shapes here. Unknown/partial details
    fall back to a compact JSON dump so a new event kind is still legible rather
    than blank.
    """
    if kind == "transition":
        return f"{detail.get('from', '?')} → {detail.get('to', '?')}"
    if kind == "lock":
        signed = detail.get("signed") or []
        who = f" ({', '.join(signed)})" if signed else ""
        return f"Contract v{detail.get('version', '?')} locked{who}"
    if kind == "deploy":
        return detail.get("text", "deployed")
    if kind == "test":
        passed = detail.get("pass")
        strike = detail.get("strike")
        if passed:
            return "Tests passed"
        return f"Tests failed (strike {strike})" if strike is not None else "Tests failed"
    if kind == "todo":
        # One event kind, the action inside it (todos._event). Read the action back out
        # so the log reads as a sentence instead of a JSON dump.
        action = (detail.get("action") or "todo").removeprefix("todo_")
        title = detail.get("title")
        text = f"Todo #{detail.get('todo_id', '?')}"
        if title:
            text += f" '{title}'"
        text += f" {action}"
        if detail.get("by"):
            text += f" by {detail['by']}"
        if detail.get("reason"):
            text += f": {detail['reason']}"
        return text
    # Everything else (slack, token, task, resolved, …) carries free-form detail;
    # surface a text/message field if present, else a compact JSON dump.
    return detail.get("text") or detail.get("message") or json.dumps(detail)


# --------------------------------------------------------------------------- #
# viewer resolution + scoping (server-side — the whole point of §11)
# --------------------------------------------------------------------------- #
def viewer_block(viewer: ViewerIdentity) -> dict:
    """The ``viewer`` object echoed to the UI so it can render a static scope badge.

    A buddy also gets ``task_id`` (the one task they may see); a host does not,
    because host means "all tasks". This is a *reflection* of the token's scope,
    never a control the client can change (SPEC §12 correction #1).
    """
    block: dict = {"mode": "host" if viewer.is_host else "buddy", "label": viewer.label}
    if not viewer.is_host:
        block["task_id"] = viewer.task_id
    return block


def viewer_can_see(viewer: ViewerIdentity, task_id: str) -> bool:
    """True iff this token is allowed to read ``task_id``. Host sees all; buddy one."""
    return viewer.is_host or viewer.task_id == task_id


def _last_activity(conn, task_id: str, created_at: float) -> float:
    """Latest message/event timestamp for a task, falling back to task creation.

    Falls back so a freshly created task with no traffic still shows *something*
    sensible in the list's ``last`` column instead of an empty cell.
    """
    row = conn.execute(
        """
        SELECT MAX(t) AS last FROM (
            SELECT created_at AS t FROM messages WHERE task_id = ?
            UNION ALL
            SELECT created_at AS t FROM events   WHERE task_id = ?
        )
        """,
        (task_id, task_id),
    ).fetchone()
    return row["last"] if row and row["last"] is not None else created_at


def _list_tasks_for(conn, viewer: ViewerIdentity, *, now: float | None = None) -> list[dict]:
    """The task list the token is allowed to see (SPEC §11 ``/api/tasks``).

    Scoping is a SQL ``WHERE``, not a post-filter: a buddy's query selects only
    their one task, so no other task's existence is even observable to them.
    """
    if viewer.is_host:
        rows = conn.execute(
            "SELECT id, title, state, mode, roles_json, strikes, created_at FROM tasks ORDER BY created_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, state, mode, roles_json, strikes, created_at FROM tasks WHERE id = ?",
            (viewer.task_id,),
        ).fetchall()

    out = []
    for r in rows:
        row = {
            "id": r["id"],
            "title": r["title"],
            "state": r["state"],
            "mode": r["mode"] or "contract",
            "roles": json.loads(r["roles_json"]),
            "last": _time_ago(_last_activity(conn, r["id"], r["created_at"]), now=now),
            "strikes": r["strikes"],
        }
        # The list row carries the same rollup as the task view ("2/6 verified ⚠1") so a
        # host can triage without opening anything. ABSENT — not empty — for a task with
        # no todos, so a pre-todo row serialises byte-identically to before.
        roll = todos.rollup(conn, r["id"])
        if roll is not None:
            row["todo_rollup"] = roll
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# task detail building blocks
# --------------------------------------------------------------------------- #
def _times_for(conn, task_id: str, created_at: float) -> dict:
    """``times[state]`` = HH:MM the task entered that state.

    ``open`` is the task's own creation time; every other state comes from the
    ``to`` field of a ``transition`` event. Only states actually reached appear
    (so ``verified``/``stuck`` are naturally optional).
    """
    times = {"open": _hhmm(created_at)}
    rows = conn.execute(
        "SELECT detail_json, created_at FROM events WHERE task_id = ? AND kind = 'transition' ORDER BY id",
        (task_id,),
    ).fetchall()
    for r in rows:
        detail = json.loads(r["detail_json"])
        to = detail.get("to")
        if to:
            times[to] = _hhmm(r["created_at"])
    return times


def _contract_for(conn, task_id: str, *, todo_id: int | None = None) -> dict:
    """The contract block: versions, the default to show, and per-version data.

    ``default`` is the latest *locked* version (that's the one agents integrate
    against), or the latest version if none is locked yet, or ``None`` when no
    contract has been proposed. Empty state: ``exists=False`` with empty
    ``versions``/``data`` so the UI can render its "awaiting contract" panel.

    ``todo_id=None`` means the TASK-level chain (``contracts.todo_id IS NULL``) — every
    contract that existed before todos, and every task that never grows one. Passing a
    todo id gives that deliverable's own chain, which is what the right-hand panel
    renders when a todo is selected: same shape, same renderer, one level down. The two
    are kept apart on purpose — folding six todos' contracts into the task card would
    show a jumble of unrelated shapes under a single "API contract" heading.
    """
    if todo_id is None:
        contracts = conn.execute(
            "SELECT id, version, spec_json, status FROM contracts "
            "WHERE task_id = ? AND todo_id IS NULL ORDER BY version",
            (task_id,),
        ).fetchall()
    else:
        contracts = conn.execute(
            "SELECT id, version, spec_json, status FROM contracts "
            "WHERE task_id = ? AND todo_id = ? ORDER BY version",
            (task_id, todo_id),
        ).fetchall()
    if not contracts:
        return {"exists": False, "versions": [], "default": None, "data": {}}

    versions = []
    data: dict = {}
    latest_vid = None
    latest_locked_vid = None
    for c in contracts:
        vid = f"v{c['version']}"
        locked = c["status"] == "locked"
        versions.append({"id": vid, "locked": locked})
        latest_vid = vid
        if locked:
            latest_locked_vid = vid

        signed = conn.execute(
            """
            SELECT a.role AS role, s.signed_at AS signed_at
            FROM contract_signatures s
            JOIN agents a ON a.id = s.agent_id
            WHERE s.contract_id = ?
            ORDER BY s.signed_at
            """,
            (c["id"],),
        ).fetchall()
        spec = json.loads(c["spec_json"])
        data[vid] = {
            "locked": locked,
            "signed": [{"role": s["role"], "time": _hhmm(s["signed_at"])} for s in signed],
            "endpoints": spec.get("endpoints", []),
            "staging_url": spec.get("staging_url"),
        }

    return {
        "exists": True,
        "versions": versions,
        "default": latest_locked_vid or latest_vid,
        "data": data,
    }


def _todos_for(conn, task_id: str) -> list[dict]:
    """The todo panel: every todo on the task, each with its own contract block.

    The wire shape is ``todos.to_dict`` verbatim (so the dashboard and the agents'
    ``get_todos`` never drift) plus two view-only additions: ``time`` for the mono
    HH:MM the rest of the dashboard uses, and ``contract`` — this deliverable's own
    chain in exactly the shape the task card already renders.

    Nothing is withheld by stage: a todo is a title, a scope and a party list, with no
    ``staging_url`` equivalent to protect until agreement (its contract block does the
    withholding that needs doing, at the task level, as before).
    """
    out = []
    for t in todos.get_todos(conn, task_id):
        d = dict(t)
        d["time"] = _hhmm(t["created_at"])
        d["contract"] = _contract_for(conn, task_id, todo_id=t["id"])
        out.append(d)
    out.sort(key=lambda t: (_TODO_ORDER.get(t["status"], 1), t["id"]))
    return out


def _messages_for(conn, task_id: str) -> list[dict]:
    """Agent messages for the thread, oldest-first, with role joined in.

    ``body`` is the decoded ``body_json``. Bodies are stored by the messaging
    core as a JSON string (``service.post_message`` does ``json.dumps(body)``), so
    the common case decodes to a plain string. We also tolerate a dict body
    (forward-compat: a structured envelope may carry ``code``/``strike``) and lift
    those optional fields when present.

    ``strike`` is otherwise derived for ``test_result`` messages by zipping them,
    in order, to the ``test`` events the state machine writes 1:1 for each — so a
    failing test shows "strike N" without trusting anything in the free-form body.

    ``todo`` is the id of the deliverable a message belongs to, when it names one — the
    UI's ``⟨api123⟩`` chip. ONE thread per task is deliberate (six threads would
    fragment a conversation that is genuinely one conversation, and you would lose "we
    discussed refunds while building payments"), so the chip is how a message is
    attributed to a deliverable rather than filed under it. Absent when the message
    belongs to no todo.

    Broker-authored notifications (``service.BROKER_TYPES``, e.g. ``contract_locked``)
    are attributed to the ``broker`` role rather than to the agent row that triggered
    them: the human thread must not show broker words in a peer's voice. They render
    once, as a single bubble — the ``lock`` EVENT is what draws the thread divider, and
    the two are 1:1, so nothing is duplicated.
    """
    # Strikes recorded by the broker for each test cycle, in order.
    test_strikes = [
        json.loads(e["detail_json"]).get("strike")
        for e in conn.execute(
            "SELECT detail_json FROM events WHERE task_id = ? AND kind = 'test' ORDER BY id",
            (task_id,),
        ).fetchall()
    ]

    rows = conn.execute(
        """
        SELECT m.id, m.type, m.body_json, m.to_role, m.created_at, a.role AS role
        FROM messages m
        JOIN agents a ON a.id = m.from_agent_id
        WHERE m.task_id = ?
        ORDER BY m.id
        """,
        (task_id,),
    ).fetchall()

    out = []
    test_idx = 0
    todo_ids: set[int] | None = None  # loaded lazily: a pre-todo task never queries it
    for r in rows:
        body = json.loads(r["body_json"])
        role = service.BROKER_ROLE if r["type"] in service.BROKER_TYPES else r["role"]
        msg: dict = {"id": r["id"], "role": role, "type": r["type"], "to_role": r["to_role"], "time": _hhmm(r["created_at"]), "ts": r["created_at"]}
        if isinstance(body, dict):
            msg["body"] = body.get("text") or body.get("body") or ""
            if "code" in body:
                msg["code"] = body["code"]
            if "strike" in body:
                msg["strike"] = body["strike"]
        else:
            msg["body"] = body

        if r["type"] == "test_result" and "strike" not in msg:
            if test_idx < len(test_strikes):
                strike = test_strikes[test_idx]
                if strike is not None:
                    msg["strike"] = strike
                test_idx += 1

        # Only the todo message types carry a reference worth trusting, and the id is
        # checked against this TASK's todos — a body that says "todo #999" (or one from
        # another task) gets no chip rather than a chip pointing at nothing.
        if r["type"].startswith("todo"):
            if todo_ids is None:
                todo_ids = {
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM todos WHERE task_id = ?", (task_id,)
                    ).fetchall()
                }
            ref = _TODO_REF_RE.search(str(msg.get("body") or ""))
            if ref is not None and int(ref.group(1)) in todo_ids:
                msg["todo"] = int(ref.group(1))
        out.append(msg)
    return out


def _events_for(conn, task_id: str, filter: str = "all") -> list[dict]:
    """The event log as ``[[time, kind, detail], ...]``, oldest-first.

    ``filter`` narrows to one ``kind``; anything outside the known set (or
    ``all``/empty) means no filter. Returns ``[]`` for a task with no events.
    """
    if filter and filter != "all" and filter in _EVENT_KINDS:
        rows = conn.execute(
            "SELECT kind, detail_json, created_at FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, filter),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT kind, detail_json, created_at FROM events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    # 4th element is the raw created_at (float) so the client can sort the thread by
    # true creation time, not just minute precision. Existing consumers use [0:3].
    return [[_hhmm(r["created_at"]), r["kind"], _render_detail(r["kind"], json.loads(r["detail_json"])), r["created_at"]] for r in rows]


def _agents_for(conn, task_id: str) -> list[dict]:
    """Live agents on the task (revoked_at IS NULL), with their pre-flight readiness.

    ``ready`` is a bool so the UI can padlock any agent that hasn't yet passed
    ``submit_readiness``. ``readiness_status`` (pending/passed/failed) distinguishes a
    FAILED attempt from a not-yet-attempted one — ``ready`` alone can't — and
    ``readiness_report`` carries the per-question results (parsed) so the human can see
    WHY it failed and coach the agent to retry.

    ``listening`` is presence: the agent is parked in ``wait_for_message`` right now.
    It's computed here (``listening_until > now``) rather than stored as a boolean, so
    a broker crash that never cleared the stamp lapses on its own. ``listening_since``
    is the streak start, for the dashboard's "listening — 42m".
    """
    rows = conn.execute(
        "SELECT name, role, ready, readiness_status, readiness_report, "
        "listening_until, listening_since "
        "FROM agents WHERE task_id = ? AND revoked_at IS NULL ORDER BY id",
        (task_id,),
    ).fetchall()
    now = time.time()
    out = []
    for r in rows:
        try:
            report = json.loads(r["readiness_report"]) if r["readiness_report"] else None
        except (ValueError, TypeError):
            report = None
        out.append({
            "name": r["name"],
            "role": r["role"],
            "ready": bool(r["ready"]),
            "readiness_status": r["readiness_status"] or "pending",
            "readiness_report": report,
            "listening": service.is_listening(r["listening_until"], now),
            "listening_since": r["listening_since"],
        })
    return out


def _task_detail(conn, task_id: str) -> dict | None:
    """Full per-task payload (SPEC §11 ``/api/task/{id}``), or ``None`` if absent.

    Composes the building blocks above. Callers must already have checked viewer
    scope; this function is scope-agnostic (it's reused by both host and buddy).

    The three ``todo*`` keys are ADDITIVE and OMITTED ENTIRELY for a task that has no
    todos, so every pre-todo task serialises byte-identically to before this feature —
    that is what keeps the deployed ``ui.html`` working, and it is the same "the single
    switch" property ``todos.has_todos`` gives the tools. In the other direction,
    ``ui.html`` is served from disk and can be NEWER than the running ``api.py`` across a
    restart, so the page must treat all three as optional (``d.todos || []``) exactly as
    it already falls back on the thread's raw ``ts``.
    """
    t = conn.execute(
        "SELECT id, title, state, mode, roles_json, strikes, created_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if t is None:
        return None
    detail = {
        "id": t["id"],
        "title": t["title"],
        "state": t["state"],
        "mode": t["mode"] or "contract",
        "roles": json.loads(t["roles_json"]),
        "strikes": t["strikes"],
        "times": _times_for(conn, task_id, t["created_at"]),
        "contract": _contract_for(conn, task_id),
        "messages": _messages_for(conn, task_id),
        "events": _events_for(conn, task_id),
        "agents": _agents_for(conn, task_id),
        "readiness_preview": readiness.preview_questions(),
    }
    todo_list = _todos_for(conn, task_id)
    if todo_list:
        # `has_todos` is the stepper switch and is NOT `len(todos) > 0`: a task whose
        # todos were all DROPPED runs on its own state machine again (todos.has_todos),
        # but the dropped rows stay visible so the human reads a decision rather than
        # finding a hole. `todo_rollup` is None in exactly that case.
        detail["has_todos"] = todos.has_todos(conn, task_id)
        detail["todos"] = todo_list
        detail["todo_rollup"] = todos.rollup(conn, task_id)
    return detail


# --------------------------------------------------------------------------- #
# live-stream change detection (SSE — see the /api/stream route)
# --------------------------------------------------------------------------- #
def _change_tokens(conn, viewer: ViewerIdentity) -> tuple[str, dict[str, str]]:
    """Opaque change-detection tokens for the SSE stream, scoped to the viewer.

    Returns ``(list_token, {task_id: task_token})``:

    * ``list_token`` fingerprints the viewer's VISIBLE task list — its membership
      plus each task's state/strikes and latest activity — so it moves whenever a
      task appears, changes state, or gains new traffic (drives the ``tasks`` event).
    * each ``task_token`` fingerprints one task's DETAIL — state/strikes, latest
      activity, message/event/agent counts, agent readiness, and contract
      version/lock/signature state — so it moves whenever that task's detail
      changes (drives the ``task`` event).

    Pure and connection-taking like the sibling ``_``-helpers, and built ONLY from
    the existing query helpers so the viewer scoping/visibility matches the JSON
    routes exactly (no duplicated query logic). Tokens are opaque strings; only
    this module ever computes or compares them.
    """
    list_parts: list[str] = []
    task_tokens: dict[str, str] = {}
    for t in _list_tasks_for(conn, viewer):
        tid = t["id"]
        row = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (tid,)).fetchone()
        created_at = row["created_at"] if row else 0.0
        last = _last_activity(conn, tid, created_at)

        # List-level fingerprint: membership + coarse per-task state (NOT the
        # wall-clock "last ago" string, which would churn every second). The rollup
        # counts join it so the row's "2/6 verified ⚠1" refreshes live; the suffix is
        # absent for a task with no todos, leaving pre-todo tokens unchanged.
        roll = t.get("todo_rollup")
        roll_part = (
            f"|{roll['verified']}/{roll['total']}|{roll['pending']}|{roll['stuck']}|{roll['state']}"
            if roll else ""
        )
        list_parts.append(f"{tid}|{t['state']}|{t['strikes']}|{last!r}{roll_part}")

        # Detail-level fingerprint: reuse the same helpers the /api/task route
        # composes from, then reduce to counts + the fields the UI actually renders.
        contract = _contract_for(conn, tid)
        fingerprint = {
            "state": t["state"],
            "strikes": t["strikes"],
            "last": last,
            "messages": len(_messages_for(conn, tid)),
            "events": len(_events_for(conn, tid)),
            # `listening` is in the fingerprint (the bool, never the raw expiry) so the
            # live presence dot flips over SSE. It moves only on a start/stop — unlike
            # the timestamp, which would churn the token every tick.
            "agents": [
                [a["role"], a["ready"], a["readiness_status"], a["listening"]]
                for a in _agents_for(conn, tid)
            ],
            "contract": [
                [v["id"], v["locked"], len(contract["data"][v["id"]]["signed"])]
                for v in contract["versions"]
            ],
        }
        # Todos move the detail token on every part the panel renders — a new acceptance,
        # a decline, a repropose, a march step, a stuck flag, a drop. Added as a key only
        # when the task HAS todos, so a pre-todo task's token is byte-identical to before.
        task_todos = _todos_for(conn, tid)
        if task_todos:
            fingerprint["todos"] = [
                [
                    d["id"], d["version"], d["status"], d["state"], d["stuck"],
                    d["accepted_by"], d["declined_by"], d["parties"],
                    d["contract_versions"], d["locked_versions"], d["drop_consents"],
                ]
                for d in task_todos
            ]
        task_tokens[tid] = json.dumps(fingerprint, sort_keys=True)

    return json.dumps(sorted(list_parts)), task_tokens


async def _sse_events(
    request,
    viewer: ViewerIdentity,
    *,
    poll: float = 1.0,
    ping_every: float = 15.0,
    idle_timeout: float = 30 * 60.0,
):
    """Async generator of SSE frames for one viewer's ``/api/stream`` connection.

    Each iteration reads the viewer's current change tokens from the LOCAL db
    (cheap; never crosses the tunnel), diffs against the last-seen set, and yields
    one frame per changed channel::

        event: tasks\\n
        data: {"token": "..."}\\n\\n              # visible list changed

        event: task\\n
        data: {"id": "...", "token": "..."}\\n\\n  # one task's detail changed

    plus a bare ``: ping`` comment roughly every ``ping_every`` seconds so idle
    proxies/tunnels don't drop the socket. The current tokens are captured as the
    baseline on entry and are NOT emitted, so a freshly-opened stream stays silent
    until something actually changes (the client does its own catch-up fetch on
    open — SSE contract §"send nothing until the first real change"). The loop
    exits when the client disconnects, or after ``idle_timeout`` seconds with no
    emitted change (a still-present browser just auto-reconnects). ``poll`` /
    ``ping_every`` / ``idle_timeout`` are keyword knobs so the loop can be driven
    deterministically under test.
    """
    conn = connect()
    try:
        last_list, last_tasks = _change_tokens(conn, viewer)
    finally:
        conn.close()

    last_ping = time.monotonic()
    last_change = time.monotonic()
    while True:
        if await request.is_disconnected():
            break
        now = time.monotonic()
        if now - last_change >= idle_timeout:
            break

        conn = connect()
        try:
            list_token, task_tokens = _change_tokens(conn, viewer)
        finally:
            conn.close()

        changed = False
        if list_token != last_list:
            last_list = list_token
            changed = True
            yield f"event: tasks\ndata: {json.dumps({'token': list_token})}\n\n"
        for tid, tok in task_tokens.items():
            if last_tasks.get(tid) != tok:
                changed = True
                yield f"event: task\ndata: {json.dumps({'id': tid, 'token': tok})}\n\n"
        # Reassigning (rather than updating) drops tokens for tasks that vanished.
        last_tasks = task_tokens

        if changed:
            last_change = now
        if now - last_ping >= ping_every:
            last_ping = now
            yield ": ping\n\n"

        await asyncio.sleep(poll)


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
def _request_token(request) -> str:
    """Resolve the viewer token, most-secure source first:

    1. the ``sb_view`` HttpOnly cookie (set on the first ``/ui`` load — JS can't read
       it and it never rides in a URL, so it can't leak via history/Referer/logs),
    2. an ``Authorization: Bearer`` header (API clients),
    3. the ``?v=`` query param (the bootstrap link, before the cookie is set).
    """
    cookie = request.cookies.get("sb_view")
    if cookie:
        return cookie
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.query_params.get("v", "") or ""


def _resolve(request, conn) -> ViewerIdentity | None:
    return identity.resolve_viewer_token(conn, _request_token(request))


# --------------------------------------------------------------------------- #
# route registration
# --------------------------------------------------------------------------- #
def register_api_routes(mcp, cfg: Config) -> None:
    """Register the read-only dashboard routes on the FastMCP app.

    Every route below is ``methods=["GET"]`` and that is a HARD invariant, not a
    coincidence (D11): there is no POST/PUT/PATCH/DELETE on this surface, including for
    the host's todo drop. A host acts through ``sys-buddy todo drop`` or the desktop app,
    whose window loads from disk on a trusted origin; the dashboard's ``[Drop todo]``
    only ever deep-links there, the way ``gui._DashApi.new_task`` already does.

    ``cfg`` is accepted for symmetry with ``register_tools`` and future use (e.g.
    mode-dependent behaviour); the routes themselves are mode-independent.
    """

    @mcp.custom_route("/api/tasks", methods=["GET"])
    async def api_tasks(request):
        conn = connect()
        try:
            viewer = _resolve(request, conn)
            if viewer is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return JSONResponse(
                {"viewer": viewer_block(viewer), "tasks": _list_tasks_for(conn, viewer)}
            )
        finally:
            conn.close()

    @mcp.custom_route("/api/task/{id}", methods=["GET"])
    async def api_task(request):
        task_id = request.path_params["id"]
        conn = connect()
        try:
            viewer = _resolve(request, conn)
            if viewer is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if not viewer_can_see(viewer, task_id):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            detail = _task_detail(conn, task_id)
            if detail is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse(detail)
        finally:
            conn.close()

    @mcp.custom_route("/api/task/{id}/events", methods=["GET"])
    async def api_task_events(request):
        task_id = request.path_params["id"]
        filter = request.query_params.get("filter", "all")
        conn = connect()
        try:
            viewer = _resolve(request, conn)
            if viewer is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if not viewer_can_see(viewer, task_id):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            # 404 the unknown task so a buddy can't distinguish "no such task" via events.
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse(_events_for(conn, task_id, filter))
        finally:
            conn.close()

    @mcp.custom_route("/api/stream", methods=["GET"])
    async def api_stream(request):
        """Server-Sent-Events feed of change notifications for the dashboard.

        Same viewer-cookie auth as the other ``/api/*`` routes — an unauthenticated
        request gets the SAME 401, never an open stream. Once authorised, hand back
        a long-lived ``text/event-stream`` whose frames tell the client *what* to
        refetch (``tasks`` / ``task``), never the data itself; the actual reads still
        go through the token-scoped JSON routes. See ``_sse_events`` for the frame
        format and the connection lifecycle.
        """
        conn = connect()
        try:
            viewer = _resolve(request, conn)
        finally:
            conn.close()
        if viewer is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return StreamingResponse(
            _sse_events(request, viewer),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @mcp.custom_route("/ui", methods=["GET"])
    async def ui(request):
        """Serve the packaged single-file dashboard.

        The page is inert and reads data only from ``/api/*`` (token-scoped). A
        dashboard link arrives as ``/ui?v=<token>``; we move that token into an
        HttpOnly cookie and redirect to a clean ``/ui`` so the secret leaves the URL
        after the first hop (out of browser history, Referer, and proxy logs). The
        page's own ``/api/*`` fetches then authenticate via the cookie automatically.
        """
        secure = (cfg.public_url or "").lower().startswith("https://")
        v = request.query_params.get("v")
        if v:
            resp = RedirectResponse(url="/ui", status_code=302)
            resp.set_cookie(
                "sb_view", v, max_age=7 * 24 * 3600, path="/",
                httponly=True, samesite="strict", secure=secure,
            )
            resp.headers["Referrer-Policy"] = "no-referrer"
            return resp
        html = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")
        resp = HTMLResponse(html)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @mcp.custom_route("/join", methods=["GET"])
    async def join(request):
        """Serve the packaged single-file buddy onboarding page.

        A pre-token entry point, so — like ``/pair`` — it is UNAUTHENTICATED: it is
        where a buddy lands with an invite code (carried in the URL fragment, never
        reaching the server) to redeem it. Simpler than ``/ui``: no ``?v=`` cookie
        dance, just the static page. ``Referrer-Policy: no-referrer`` keeps the
        landing URL out of any onward Referer header.
        """
        html = (Path(__file__).parent / "join.html").read_text(encoding="utf-8")
        resp = HTMLResponse(html)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp
