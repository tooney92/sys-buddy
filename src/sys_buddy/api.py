"""Read-only HTTP API for the dashboard (SPEC §11), with server-side scoping.

Guiding principle (SPEC §0): **the broker enforces, agents/clients request.** Viewer
scoping lives here, not in the browser. A buddy's ``viewer_token`` is bound to one
task, so ``/api/tasks`` returns exactly that one task and ``/api/task/{id}`` refuses
any other id with 403. The client never filters for security — the server only ever
hands back what the token permits (SPEC §9, §12 "two corrections to the prototype").

Every route is read-only. The query logic is factored into ``_``-prefixed helpers
that take an open connection so they can be unit-tested without a running HTTP
server (see ``tests/test_api.py``); the ``async def`` handlers are thin shells that
resolve the viewer token, enforce scope, open/close a connection, and serialise.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import identity, readiness
from .config import Config
from .db import connect
from .identity import ViewerIdentity

# HTTP verb set is small; the event ``kind`` set is fixed by the state machine.
_EVENT_KINDS = {"task", "transition", "lock", "deploy", "test", "slack", "token"}


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
        out.append(
            {
                "id": r["id"],
                "title": r["title"],
                "state": r["state"],
                "mode": r["mode"] or "contract",
                "roles": json.loads(r["roles_json"]),
                "last": _time_ago(_last_activity(conn, r["id"], r["created_at"]), now=now),
                "strikes": r["strikes"],
            }
        )
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


def _contract_for(conn, task_id: str) -> dict:
    """The contract block: versions, the default to show, and per-version data.

    ``default`` is the latest *locked* version (that's the one agents integrate
    against), or the latest version if none is locked yet, or ``None`` when no
    contract has been proposed. Empty state: ``exists=False`` with empty
    ``versions``/``data`` so the UI can render its "awaiting contract" panel.
    """
    contracts = conn.execute(
        "SELECT id, version, spec_json, status FROM contracts WHERE task_id = ? ORDER BY version",
        (task_id,),
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
        }

    return {
        "exists": True,
        "versions": versions,
        "default": latest_locked_vid or latest_vid,
        "data": data,
    }


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
    for r in rows:
        body = json.loads(r["body_json"])
        msg: dict = {"id": r["id"], "role": r["role"], "type": r["type"], "to_role": r["to_role"], "time": _hhmm(r["created_at"])}
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
    return [[_hhmm(r["created_at"]), r["kind"], _render_detail(r["kind"], json.loads(r["detail_json"]))] for r in rows]


def _agents_for(conn, task_id: str) -> list[dict]:
    """Live agents on the task (revoked_at IS NULL), with their pre-flight readiness.

    ``ready`` is surfaced as a bool so the UI can padlock any agent that hasn't yet
    passed ``submit_readiness`` (its action tools are still locked by the broker).
    """
    rows = conn.execute(
        "SELECT name, role, ready FROM agents WHERE task_id = ? AND revoked_at IS NULL ORDER BY id",
        (task_id,),
    ).fetchall()
    return [{"name": r["name"], "role": r["role"], "ready": bool(r["ready"])} for r in rows]


def _task_detail(conn, task_id: str) -> dict | None:
    """Full per-task payload (SPEC §11 ``/api/task/{id}``), or ``None`` if absent.

    Composes the building blocks above. Callers must already have checked viewer
    scope; this function is scope-agnostic (it's reused by both host and buddy).
    """
    t = conn.execute(
        "SELECT id, title, state, mode, roles_json, strikes, created_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if t is None:
        return None
    return {
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
