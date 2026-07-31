"""The enforced state machine, contract lifecycle, and broker-counted strikes.

SPEC §5 (states), §6 (contract lock), §8 (strikes). This module is where the
guiding principle lives: **the broker enforces, agents request.** Every workflow
rule here is code or a DB fact, never a prompt. An agent asks to deploy; the
broker decides whether a locked contract exists and whether the caller is the
backend. An agent reports a failing test; the broker — not the agent — increments
a database column and pulls the stuck cord at three.

Enforcement runs in BOTH modes. SPEC §3 calls the local state machine "advisory",
but we enforce there too: a second, laxer code path is a second place for bugs and
security gaps to hide, and enforcement never *hurts* a well-behaved local agent. So
these functions are mode-agnostic — there is exactly one path, and it enforces.

States (SPEC §5):
    open → contract_proposed → contract_locked → backend_live → testing → verified
                                                       ↑             │
                                                       └── retry ─────┘ (or → stuck)
``verified`` and ``stuck`` are terminal: reopening requires a human.

TODOS (v2) fork this in exactly one place: a task running on todos does not own its
state — the state is a ROLLUP of its todos (``todos.apply_rollup``), each of which
marches through the SAME six states with its own contract chain. The ``_..._todo``
functions drive one deliverable and then re-derive the parent.

CONTRACTS are todo-scoped, full stop. There is exactly ONE kind: an agreement about one
todo, whose signatory set is that todo's party list. There used to be a second,
task-level kind, so every contract operation existed twice and the second copy stayed
reachable in ways nobody intended — a bare ``decline_contract`` could kill a proposal on
a deliverable the caller never named. ``contracts.todo_id`` is now NOT NULL, and the
selector is required on every operation that acts on a contract. The task-level
``report_status`` paths below survive only for ``stuck``/``waiting``/``resolved``, which
are deliberately whole-collaboration words.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import config, contracts, notify, seats, service, todos
from .identity import Identity

# --- states -----------------------------------------------------------------
OPEN = "open"
CONTRACT_PROPOSED = "contract_proposed"
CONTRACT_LOCKED = "contract_locked"
BACKEND_LIVE = "backend_live"
TESTING = "testing"
VERIFIED = "verified"
STUCK = "stuck"
RESOLVED = "resolved"  # debug tasks: terminal, reached from any non-terminal state

TERMINAL_STATES = frozenset({VERIFIED, STUCK, RESOLVED})

# --- report_status vocabulary -----------------------------------------------
# The status strings an agent may pass to report_status. Named for what the agent
# is asserting happened, so the broker can map each to a transition + typed message.
STATUS_DEPLOYED = "deployed"       # backend: the API is live on staging
STATUS_TEST_PASSED = "test_passed"  # client role: e2e suite went green
STATUS_TEST_FAILED = "test_failed"  # client role: e2e suite went red (a strike)
STATUS_VERIFIED = "verified"        # feature confirmed end-to-end (terminal)
STATUS_STUCK = "stuck"              # give up; humans needed (terminal)
STATUS_RESOLVED = "resolved"        # debug task: the issue is fixed (terminal)
STATUS_WAITING = "waiting"          # soft nudge: needs a human's attention, NOT terminal

TEST_STATUSES = frozenset({STATUS_TEST_PASSED, STATUS_TEST_FAILED})

# Task-agnostic vocabulary: the canonical words agents should reach for. Each is a
# pure ALIAS of an existing API/deploy-shaped status — same transition, same message,
# same strike behavior — so nothing downstream needs to know these words exist.
STATUS_READY = "ready"       # producer: my part is ready for the peer to build on
STATUS_CHECKED = "checked"    # consumer: it works against the producer's side
STATUS_BLOCKED = "blocked"    # consumer: it doesn't work (a strike)
_STATUS_ALIASES = {
    STATUS_READY: STATUS_DEPLOYED,
    STATUS_CHECKED: STATUS_TEST_PASSED,
    STATUS_BLOCKED: STATUS_TEST_FAILED,
}

# The statuses that are meaningless at the task level once there are N deliverables:
# "backend is ready" — ready on WHICH todo? So on a task WITH todos these REQUIRE a
# todo NUMBER (v2 design, settled). ``stuck`` is deliberately absent: it is valid at both
# levels — with a todo it flags that deliverable, without one it escalates the whole
# collaboration ("my token expired") — and ``resolved`` is debug-only, where todos
# cannot exist at all.
TODO_SCOPED_STATUSES = frozenset(
    {STATUS_DEPLOYED, STATUS_TEST_PASSED, STATUS_TEST_FAILED, STATUS_VERIFIED}
)

MAX_STRIKES = 3  # SPEC §8: at 3 the broker force-transitions to stuck.

# The producer is NOT a hardcoded role (model B): it is whichever role proposed the
# current locked contract — see ``_producer_role``. Only the producer may report
# `ready`; only the OTHER (consuming) roles may report checks.


# --------------------------------------------------------------------------- #
# low-level helpers — the event-log convention (see step-4 brief) lives here
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.time()


def _state(conn, task_id: str) -> str:
    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    return row["state"]


def _task_mode(conn, task_id: str) -> str:
    """The task's workflow mode: 'contract' (full state machine) or 'debug'
    (simple open → resolved). Defaults to 'contract' when NULL or missing."""
    row = conn.execute("SELECT mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["mode"] is None:
        return "contract"
    return row["mode"]


def _task_is_same_machine(conn, task_id: str) -> bool:
    """Is this task confined to ONE machine (host-declared at setup)?

    Two independent conditions must BOTH hold, so the lenient staging_url path is
    only ever taken on positive evidence:

    1. the task row carries ``same_machine = 1`` — set only by host setup when the
       broker origin was loopback and no public/tunnel URL was given; and
    2. this broker process has no ``public_url`` — if the process is reachable at a
       public origin, a peer may well be off-box regardless of what the row says.

    A missing column/row/NULL reads as False (strict). Nothing an AGENT can send
    over MCP feeds into either condition.
    """
    try:
        row = conn.execute("SELECT same_machine FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return False  # pre-migration db → strict
    if row is None or not row["same_machine"]:
        return False
    return not (config.get_config().public_url or "").strip()


def _task_staging_url(conn, task_id: str) -> str | None:
    """The host-chosen deployment target for this task, if they set one."""
    try:
        row = conn.execute("SELECT staging_url FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row["staging_url"]:
        return None
    return str(row["staging_url"]).strip() or None


def _todo_staging_url(conn, todo_id: int | None) -> str | None:
    """The HOST's per-deliverable override, if they set one on this todo."""
    if todo_id is None:
        return None
    try:
        row = conn.execute("SELECT staging_url FROM todos WHERE id = ?", (todo_id,)).fetchone()
    except sqlite3.OperationalError:
        return None  # pre-migration db → no overrides exist
    if row is None or not row["staging_url"]:
        return None
    return str(row["staging_url"]).strip() or None


def _legacy_spec_staging_url(spec: object) -> str | None:
    """The target a pre-existing contract carries INSIDE its signed spec.

    Back-compat only. Contracts written before the target became host-owned hold it in
    ``spec_json``, and those documents are never rewritten — so a task whose host has
    not set a target still resolves to what its own contract said, rather than to
    nothing. New proposals cannot produce this: ``contracts.validate_spec`` refuses a
    spec that carries a ``staging_url`` at all.
    """
    if not isinstance(spec, dict):
        return None
    url = spec.get("staging_url")
    return str(url).strip() or None if isinstance(url, str) and url.strip() else None


def _spec_of(conn, contract_id: int) -> dict | None:
    """One contract's stored spec, for the legacy leg of :func:`resolve_staging_url`."""
    row = conn.execute(
        "SELECT spec_json FROM contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        spec = json.loads(row["spec_json"])
    except (TypeError, ValueError):
        return None
    return spec if isinstance(spec, dict) else None


def resolve_staging_url(
    conn, task_id: str, todo_id: int | None = None, spec: object = None
) -> str | None:
    """The deployment target in force RIGHT NOW: todo override, else task, else legacy.

    Read LIVE, on every read, and that is the whole point. The target used to live
    inside the signed spec, so an ngrok free URL rotating on a tunnel restart staled a
    locked contract and the only fix in the rules was a full renegotiation — a new
    version identical to the last but for one string. Making people re-sign on
    non-events teaches them to sign without reading, which quietly costs every
    signature that WOULD have caught something. Resolving here means a tunnel bounce
    touches no contract, no version and no signature.

    It is also the stronger security posture, not a relaxation: the host owns every
    input to this function, no agent tool writes any of them, so an injected "test
    against evil.com" has nothing left to aim at. It used to be an agent-authored
    field defended only by a reviewer noticing.

    Order:

    1. ``todos.staging_url`` — the host's override for THIS deliverable. Most specific
       wins, because a real task deploys its pieces to different places.
    2. ``tasks.staging_url`` — the host's target for the whole task.
    3. the contract's own spec, for a contract signed before any of this existed. Last,
       so a host who sets a target overrides a stale one baked into an old document —
       which is exactly the situation that prompted the change (a locked contract
       carrying an ngrok URL with a port that could never connect).
    """
    return (
        _todo_staging_url(conn, todo_id)
        or _task_staging_url(conn, task_id)
        or _legacy_spec_staging_url(spec)
    )


def _event(conn, task_id: str, kind: str, detail: object) -> None:
    """Append an events row. ``detail`` is any JSON value; the API layer depends on
    the exact shapes documented in the step-4 brief (transition/lock/deploy/test)."""
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, kind, json.dumps(detail), _now()),
    )


def _transition(conn, task_id: str, to_state: str) -> str:
    """Move the task to ``to_state``, writing a ``transition`` event iff the state
    actually changes. Returns the resulting state. The transition event's
    ``created_at`` is what the API reads as ``times[to_state]`` — so we only emit
    one when there is a genuine change, never a no-op self-transition."""
    current = _state(conn, task_id)
    if current == to_state:
        return current
    conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (to_state, task_id))
    _event(conn, task_id, "transition", {"from": current, "to": to_state})
    return to_state


def _notify(conn, task_id: str, text: str) -> None:
    """Fire a best-effort human notification and record the event either way.

    Fans out to every configured channel via ``notify.send`` — the call sites here name
    no channel, which is the whole point of that seam: adding one must not mean editing
    eleven transitions.

    The event is written regardless of whether any channel is configured or the send
    succeeded, so the dashboard's log shows that a notification was TRIGGERED at this
    point. ``detail.channels`` records which ones actually got it, so "nobody was paged"
    is visible rather than inferred. ``notify.send`` never raises (SPEC §14).
    """
    results = notify.send(text)
    _event(conn, task_id, "notify", {"text": text, "channels": sorted(results)})


def _reject_if_terminal(state: str) -> None:
    if state in TERMINAL_STATES:
        raise ValueError(
            f"task is in terminal state '{state}'; reopening requires a human"
        )


def _assert_live(conn, task_id: str) -> str:
    """The terminal gate, todo-aware. Returns the current task state.

    ``verified`` is terminal for a task that owns its own state machine. A task
    running on todos does NOT own it: the state is a rollup that reads ``verified``
    when the last todo verifies and legitimately goes backwards when a seventh todo
    appears. Freezing the task there would turn the rollup into a one-way door that
    no new deliverable could reopen. A HUMAN-escalated ``stuck``/``resolved`` stays
    terminal in both worlds — only a human reopens one.
    """
    current = _state(conn, task_id)
    if current == VERIFIED and todos.has_todos(conn, task_id):
        return current
    _reject_if_terminal(current)
    return current


def _live_todo_list(conn, task_id: str) -> str:
    """``#1 (title), #2 (title)`` — for error messages that must be ACTIONABLE.

    Prints ``todos.number``, never ``todos.id``: an error that lists a number the agent
    cannot pass back to ``todo=`` is worse than listing nothing.
    """
    rows = todos.live_todos(conn, task_id)
    return ", ".join(f"#{r['number']} ({r['title']})" for r in rows) or "(none)"


def _todo_clause(todo_id: int | None) -> str:
    """``AND todo_id = ?`` when a deliverable is named.

    ``None`` means NO FILTER, deliberately not ``todo_id IS NULL`` — which would now match
    nothing, since a contract always belongs to a deliverable. It is what the task-wide
    readers ask for: "the newest contract on this task, whichever todo it is about", which
    is how ``get_contract`` answers a caller that did not name one and how the dashboard
    reads a task at a glance.

    Which is exactly why the queries that use this order by ``id`` and not by ``version``.
    Versions are per-chain, so with no filter "the highest version" compares numbers from
    unrelated chains; "the most recent row" is the only reading that survives it. And a
    lookup that names ONE version — ``lock_contract`` — cannot use this at all: it has to
    be chain-scoped, with ``todo_id IS ?``, because ``(task_id, version)`` names one row
    per deliverable.
    """
    return " AND todo_id = ?" if todo_id is not None else ""


def _current_locked(conn, task_id: str, todo_id: int | None = None) -> dict | None:
    """The newest locked contract for the task, or None.

    'Current' is the *newest* locked version: a v2 replanning supersedes v1
    the moment v2 locks, even though v1's row stays 'locked' for the audit trail.
    ``todo_id`` narrows it to one deliverable's chain, whose versions run 1, 2, 3…

    ``ORDER BY id DESC``, not ``version DESC``: versions are numbered PER CHAIN now, so
    across chains the highest version is meaningless — todo #1 on its third revision
    would outrank todo #6's live v1. Within a chain the two orders are identical (a
    proposal is always MAX+1 and ids only grow), so this is strictly more correct and
    never less.
    """
    row = conn.execute(
        "SELECT id, version, spec_json, todo_id, locked_at FROM contracts "
        f"WHERE task_id = ? AND status = 'locked'{_todo_clause(todo_id)} "
        "ORDER BY id DESC LIMIT 1",
        (task_id, todo_id) if todo_id is not None else (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "version": row["version"],
        "spec": json.loads(row["spec_json"]),
        "todo_id": row["todo_id"],
        "locked_at": row["locked_at"],
    }


def _newest_contract(conn, task_id: str, todo_id: int | None = None) -> dict | None:
    """The newest contract row for the task regardless of status (draft or
    locked) — the one an agent is currently meant to act on. A freshly proposed v1
    (still 'draft') or a v2 replan-in-progress is returned here even though it hasn't
    locked; ``get_contract`` uses this so a proposal is REVIEWABLE before signing.
    ``todo_id`` narrows it to one deliverable's chain.

    ``ORDER BY id DESC`` for the reason spelled out in ``_current_locked``: per-chain
    versions make "highest version" the wrong question once more than one chain is in
    scope, and inside a chain it is the same answer."""
    row = conn.execute(
        "SELECT id, version, spec_json, status, todo_id, locked_at, staging_url_at_lock "
        f"FROM contracts WHERE task_id = ?{_todo_clause(todo_id)} ORDER BY id DESC LIMIT 1",
        (task_id, todo_id) if todo_id is not None else (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "version": row["version"],
        "spec": json.loads(row["spec_json"]),
        "status": row["status"],
        "todo_id": row["todo_id"],
        "locked_at": row["locked_at"],
        # The target that was live when this locked — the historical record, kept
        # apart from the value `resolve_staging_url` reads today.
        "staging_url_at_lock": row["staging_url_at_lock"],
    }


def _signatures_for(conn, contract_id: int) -> list[str]:
    """SEATS that have signed a given contract row.

    Handles, not role types — a signature is a person's, and with two frontend seats
    both must sign for themselves. The quorum check downstream is unchanged
    (``[r for r in required if r not in signed_set]``); it simply compares handles to
    the handles in ``parties_json`` now.
    """
    return [
        r["role"]
        for r in conn.execute(
            "SELECT COALESCE(a.handle, a.role) AS role FROM contract_signatures s "
            "JOIN agents a ON a.id = s.agent_id WHERE s.contract_id = ?",
            (contract_id,),
        ).fetchall()
    ]


def _producer_role(conn, task_id: str, todo_id: int | None = None) -> str | None:
    """The PRODUCER role for a task — model B: whoever proposed the current locked
    contract. That role is the one others build against: it reports ``ready``, and it
    is the only role that may NOT report checks. ``None`` when no contract is locked
    yet (so there is no producer to speak of). Nothing is hardcoded to 'backend'.

    ``todo_id`` scopes it to one deliverable, which is what makes the producer a
    PER-TODO fact: backend produces todo #1 while mobile produces todo #2, and each
    is the consumer of the other's — a single task-wide producer could not express it.

    ``ORDER BY c.id DESC`` for the reason in ``_current_locked`` — per-chain versions.
    """
    row = conn.execute(
        "SELECT COALESCE(a.handle, a.role) AS role FROM contracts c "
        "JOIN agents a ON a.id = c.proposed_by "
        "WHERE c.task_id = ? AND c.status = 'locked'"
        + (" AND c.todo_id = ?" if todo_id is not None else "")
        + " ORDER BY c.id DESC LIMIT 1",
        (task_id, todo_id) if todo_id is not None else (task_id,),
    ).fetchone()
    return row["role"] if row else None


# --------------------------------------------------------------------------- #
# "what happens next" — the ONE move that unblocks a todo
# --------------------------------------------------------------------------- #
# The failure this exists to fix: a human stares at a locked todo that will not move
# and has no way to know the next move is his own agent typing `ready #1`. Nothing on
# the dashboard said what it was waiting for.
#
# It lives HERE, next to the gates, and is computed from the SAME predicates
# ``report_status`` enforces — ``_newest_contract``, ``_current_locked``,
# ``_producer_role``, ``todos.status_of``, the todo's own march — rather than being a
# second, hand-maintained copy of the rules in JS. A "next step" that drifts from what
# the broker actually allows is worse than none: it sends a human to type a command
# that will be refused.
#
# The shorthand strings are the ones the agent prompt teaches and the dashboard
# cheatsheet mirrors (`yes #N`, `pc #N`, `sign #N`, `ready #N`, `ok #N`, `done #N`).
# Naming the literal thing to TYPE is the whole point — "backend must report ready" is
# still a puzzle if you don't know what to say to your agent.

# The verb each shorthand maps to, for the tooltip/aria text. Kept beside the codes so
# the two cannot drift.
# ``{n}`` is the todo's per-task NUMBER, never its internal id — these strings are
# literal tool calls a human/agent is meant to make, so only the typeable handle fits.
_NEXT_TOOLS = {
    "yes": "accept_todo({n})",
    "no": "decline_todo({n}, reason)",
    "pc": "propose_contract(spec, todo={n})",
    "sign": "lock_contract(version, todo={n})",
    "ready": "report_status('ready', detail, todo={n})",
    "ok": "report_status('checked', detail, todo={n})",
    "block": "report_status('blocked', detail, todo={n})",
    "done": "report_status('verified', detail, todo={n})",
}


def _who_label(roles: list[str]) -> str:
    """``backend`` · ``backend or mobile`` · ``backend, mobile or frontend``."""
    if not roles:
        return "nobody"
    if len(roles) == 1:
        return roles[0]
    return ", ".join(roles[:-1]) + " or " + roles[-1]


def _unjoined_note(unjoined: list[str]) -> str:
    """The sentence that separates "waiting on Priya" from "waiting on a seat nobody
    ever accepted". Without it those two read identically and need different actions:
    nudge a colleague, or chase an invite.

    Deliberately no deadline. Any timeout would be an arbitrary number, and a todo that
    silently expires is worse than one that visibly waits — ``host_drop_todo`` is
    already the escape hatch, and it is named here so the reader has a move.
    """
    if not unjoined:
        return ""
    seats = ", ".join(f"@{h}" for h in unjoined)
    is_are = "has" if len(unjoined) == 1 else "have"
    return (
        f" {seats} {is_are} NEVER JOINED — nobody accepted that invite, so no agent is "
        f"there to act. Chase the invite, or the host drops the todo."
    )


def _next(stage, who, cmd, text, *, alt=None, number=None, human=False, done=False,
          unjoined=None):
    """``number`` is the todo's HUMAN ``#N`` — it lands inside a command string a person
    is meant to type, so it can never be the internal ``todos.id``.

    ``unjoined`` is the subset of ``who`` that no one has ever paired into. It rides on
    the payload as its own field (so the dashboard can badge it) AND is spelled out in
    ``text`` (so the agent reading the sentence gets it too).
    """
    who = list(who or [])
    unjoined = [h for h in (unjoined or []) if h in who]
    return {
        "stage": stage,
        "who": who,
        "who_label": _who_label(who) if who else None,
        "unjoined": unjoined,
        "cmd": cmd,
        "alt": alt,
        "tool": (
            _NEXT_TOOLS[cmd.split()[0]].format(n=number)
            if cmd and cmd.split()[0] in _NEXT_TOOLS
            else None
        ),
        "text": text + _unjoined_note(unjoined),
        "human": human,
        "done": done,
    }


def next_step(conn, task_id: str, todo_id: int) -> dict:
    """The one move that unblocks a todo: WHO owes it and WHAT they type.

    ``todo_id`` is the INTERNAL ``todos.id`` — this is called by the dashboard from a
    row it already has, never from something a human typed. The shorthand it emits
    (``ready #N``) uses the todo's ``number``, because that is what the broker will
    accept back.

    The branch order deliberately mirrors the guard order the writes enforce, so the
    answer can never promise a move the broker would refuse:

    * dropped / verified — terminal, nothing is owed (``_report_on_todo``);
    * three strikes — the broker pulled the cord, HUMANS own it (``_report_on_todo``);
    * not every party has accepted — ``_resolve_contract_todo`` refuses a contract on a
      todo that is still ``pending``, so the move is the missing ``yes``;
    * no contract at all — one of the parties proposes. There is NO producer yet (the
      producer is derived from whoever proposed the lock), so we say "one of you"
      rather than inventing a name;
    * a proposal in flight — the outstanding signatures. ``_report_todo_deployed``
      refuses ``ready`` while the newest version is unsigned, even when an older one is
      locked, which is exactly why this branch sits above the producer branches;
    * locked but not live — the producer, and only the producer, reports ``ready``;
    * live — the CONSUMERS (every party but the producer) check it;
    * testing — any party may call it verified, once a check has actually run.
    """
    row = todos.row_by_id(conn, task_id, todo_id)
    tid = row["id"]        # internal — for the contract/decision lookups below
    num = row["number"]    # human — for every string a person is told to type
    parties = todos.parties_of(row)
    status = todos.status_of(conn, row)
    # Which of this todo's seats nobody ever accepted an invite for. `_next` keeps only
    # the ones that are actually being waited on, so a never-joined seat that owes
    # nothing right now stays quiet.
    unjoined = todos.unjoined_parties(conn, task_id, parties)

    if status == todos.DROPPED:
        return _next(
            "dropped", [], None,
            "Dropped — it no longer counts toward this task. Nothing is owed.",
            number=num, done=True,
        )
    if row["state"] == VERIFIED:
        return _next(
            "verified", [], None,
            "Done — this deliverable is verified end to end. Nothing left to do; "
            "follow-up work is a NEW todo.",
            number=num, done=True,
        )

    # The three-strike cord (SPEC §8) is enforced off the DB count, and once it is
    # pulled no agent report is accepted at all — so no shorthand would work here.
    if row["strikes"] >= MAX_STRIKES:
        return _next(
            "cord_pulled", [], None,
            f"{MAX_STRIKES} fix cycles reached — the broker pulled the cord and the "
            f"humans own this one. Drop it from your terminal, or agree a fresh todo.",
            number=num, human=True,
        )

    # --- agree on WHAT ------------------------------------------------------
    if status == todos.PENDING:
        d = todos.decisions(conn, tid, row["version"])
        awaiting = [p for p in parties if d.get(p, {}).get("decision") != todos.ACCEPT]
        return _next(
            "pending", awaiting, f"yes #{num}",
            f"{_who_label(awaiting)} still has to agree to the SCOPE — their agent types "
            f"`yes #{num}` (or `no #{num} <why>` to send it back for a reshape). "
            f"Nothing here moves until then.",
            alt=f"no #{num} <why>", number=num, unjoined=unjoined,
        )

    # --- agree on HOW -------------------------------------------------------
    newest = _newest_contract(conn, task_id, todo_id=tid)
    if newest is None:
        return _next(
            "accepted", parties, f"pc #{num}",
            f"Everyone agreed on WHAT. Next is HOW: one of you proposes the contract — "
            f"`pc #{num}`. Whoever proposes it becomes the PRODUCER for this "
            f"deliverable, so there is no producer yet.",
            number=num, unjoined=unjoined,
        )

    if newest["status"] == "declined":
        # The whole point of decline: "waiting on X to sign" would be a lie here — X has
        # looked and objected. The move is a NEW version, not a signature.
        row = conn.execute(
            "SELECT COALESCE(a.handle, a.role) AS role, c.decline_reason AS reason FROM contracts c "
            "LEFT JOIN agents a ON a.id = c.declined_by WHERE c.id = ?",
            (newest["id"],),
        ).fetchone()
        who = row["role"] if row and row["role"] else None
        why = f" — \u201c{row['reason']}\u201d" if row and row["reason"] else ""
        return _next(
            "contract_declined", parties, f"pc #{num}",
            f"{(who or 'A party')} declined contract v{newest['version']}{why}. That "
            f"version is dead and cannot be signed. Someone proposes a new one that "
            f"addresses it — `pc #{num}`.",
            number=num, unjoined=unjoined,
        )

    if newest["status"] != "locked":
        signed = set(_signatures_for(conn, newest["id"]))
        awaiting = [p for p in parties if p not in signed]
        return _next(
            "contract_proposed", awaiting, f"sign #{num}",
            f"Contract v{newest['version']} is proposed and waiting on "
            f"{_who_label(awaiting)} to sign — `sign #{num}`. The staging URL stays "
            f"hidden until every party has signed, and nobody can report ready until "
            f"it locks.",
            number=num, unjoined=unjoined,
        )

    # A lock exists on this todo, so its proposer exists too — but never interpolate a
    # None into a sentence a human is meant to act on.
    producer = _producer_role(conn, task_id, todo_id=tid)
    owner = [producer] if producer else []
    named = producer or "whoever proposed the locked contract"
    consumers = [p for p in parties if p != producer]

    if row["state"] not in (BACKEND_LIVE, TESTING):
        return _next(
            "contract_locked", owner, f"ready #{num}",
            f"The contract is locked and nothing is building yet — {named} proposed "
            f"it, so {named} is the producer and owes the build. When their side is "
            f"live to build against, their agent types `ready #{num}`. Only they can: "
            f"the broker refuses `ready` from anyone else.",
            number=num,
        )

    if row["state"] == BACKEND_LIVE:
        return _next(
            "backend_live", consumers, f"ok #{num}",
            f"{_who_label(consumers)} builds against it and reports back — `ok #{num}` "
            f"if it works, `block #{num}` if it doesn't (that's a strike; {MAX_STRIKES} "
            f"and the humans take over). The producer ({named}) may not check its "
            f"own work.",
            alt=f"block #{num}", number=num, unjoined=unjoined,
        )

    # testing — checks have run, so `done` is finally allowed.
    return _next(
        "testing", parties, f"done #{num}",
        f"Checks have run. When it's confirmed end to end, any party calls it: "
        f"`done #{num}`. `block #{num}` instead if a check failed. The task itself "
        f"concludes when the LAST todo is done.",
        alt=f"block #{num}", number=num, unjoined=unjoined,
    )


def _todo_march(conn, task_id: str, todo_id: int, from_state: str, to_state: str) -> str:
    """Advance ONE todo's march, logging it as a ``todo`` event.

    Deliberately NOT a task-level ``transition`` event: those feed the dashboard's
    per-state clock (``api._times_for``), and six todos writing into it would make
    ``times[backend_live]`` mean "whichever deliverable happened to get there last".
    The task's own transitions come from the ROLLUP alone.
    """
    if from_state == to_state:
        return to_state
    conn.execute("UPDATE todos SET state = ? WHERE id = ?", (to_state, todo_id))
    todos._event(conn, task_id, "todo_transition", todo_id, {"from": from_state, "to": to_state})
    return to_state


def _clear_todo_stuck(conn, todo_id: int) -> None:
    """A todo that is moving again is not stuck any more.

    Safe because the BROKER's three-strike cord is enforced off ``todos.strikes``,
    not off this flag (see ``_report_on_todo``) — so clearing it cannot talk the
    broker out of a strike count, it only stops the dashboard showing a ⚠ against a
    deliverable that has since progressed.
    """
    conn.execute(
        "UPDATE todos SET stuck_at = NULL, stuck_reason = NULL WHERE id = ?", (todo_id,)
    )


def _todo_label(row) -> str:
    """``[todo #3 payments]`` — prefixed onto the typed message a report posts.

    The prefix is what tells the peer (and the human thread) WHICH deliverable a
    deploy/test/verified belongs to, so it prints the per-task ``number`` the peer would
    type back — not the global ``todos.id`` the row is keyed on.
    """
    return f"[todo #{row['number']} {row['title']}]"


# --------------------------------------------------------------------------- #
# contract lifecycle
# --------------------------------------------------------------------------- #
def _require_contract_todo(conn, task_id: str, number: int | None, call: str):
    """Resolve the chain a contract operation names. Never ``None``.

    The counterpart of ``_resolve_contract_todo`` for the ops that act on an EXISTING
    contract. Both exist because proposing additionally validates the todo's stage — you
    cannot shape work nobody has accepted — while this one only needs to know which chain
    the caller meant.

    ``lock_contract`` uses it because it names ONE version, and ``(task_id, version)``
    names one row per deliverable, so an unscoped lookup fetches a sibling todo's contract
    and then reports *its* status. ``decline_contract`` and ``reopen_negotiations`` raise
    their own equivalent once the task has todos (they act on "the newest proposal", not a
    named version); ``get_contract`` deliberately stays unscoped, because the dashboard
    reads it to show a task's current contract whichever deliverable it belongs to.
    """
    if number is None:
        if todos.has_todos(conn, task_id):
            raise ValueError(
                f"name the deliverable: {call}. Live todos: "
                f"{_live_todo_list(conn, task_id)} — call get_todos() for their scopes."
            )
        raise ValueError(
            f"task '{task_id}' has no todos, so it has no contracts — a contract is an "
            f"agreement about one deliverable. Start with propose_todo(title, scope, parties)."
        )
    return todos.get_row(conn, task_id, number)


def _resolve_contract_todo(conn, identity: Identity, number: int | None):
    """Resolve the deliverable a contract belongs to. Never ``None``.

    ``number`` is the agent-supplied ``#N`` (``todos.number``, per task), resolved
    through ``todos.get_row``; the row's internal ``id`` is what gets written to
    ``contracts.todo_id``.

    The selector is ALWAYS required, because there is exactly one kind of contract: an
    agreement about ONE todo. There used to be a second, task-level kind, which meant
    every contract operation had two code paths and the task-level one stayed reachable
    in ways nobody intended — a bare ``decline_contract`` could kill a proposal on a
    deliverable the caller never named. It also asked the WHOLE cast to sign work that
    binds two of them (the signatory set is the party list — see ``lock_contract``) and
    could never be reported against, since ``report_status`` is todo-scoped.

    Stage matters: agree on WHAT before HOW. A contract on a todo that not every party
    has accepted yet is a shape for work nobody signed up to.
    """
    if number is None:
        if todos.has_todos(conn, identity.task_id):
            raise ValueError(
                f"a contract must name the deliverable it shapes: "
                f"propose_contract(spec, todo=<N>). Live todos: "
                f"{_live_todo_list(conn, identity.task_id)} — call get_todos() for their "
                f"scopes and party lists."
            )
        raise ValueError(
            f"task '{identity.task_id}' has no todos yet, and a contract is an agreement "
            f"about one deliverable — so there is nothing to contract. Propose the todo "
            f"first: propose_todo(title, scope, parties), have every party accept it with "
            f"accept_todo(<N>), then propose_contract(spec, todo=<N>)."
        )

    row = todos.get_row(conn, identity.task_id, number)
    todos.assert_party(row, identity.role, "propose a contract on")
    status = todos.status_of(conn, row)
    if status == todos.DROPPED:
        raise ValueError(
            f"todo #{row['number']} was dropped; there is nothing left to contract"
        )
    if status == todos.VERIFIED:
        raise ValueError(
            f"todo #{row['number']} is already verified — propose a NEW todo for follow-up "
            f"work rather than re-contracting finished work"
        )
    if status == todos.PENDING:
        d = todos.decisions(conn, row["id"], row["version"])
        awaiting = [
            p for p in todos.parties_of(row) if d.get(p, {}).get("decision") != todos.ACCEPT
        ]
        raise ValueError(
            f"todo #{row['number']} '{row['title']}' is not accepted yet — agree on WHAT "
            f"before HOW. Waiting on {', '.join(awaiting)} to accept_todo({row['number']})."
        )
    return row


def propose_contract(conn, identity: Identity, spec: dict, number: int | None = None) -> dict:
    """Validate and record a contract proposal, (re)opening planning.

    A proposal is valid from ``open`` or any later non-terminal state — a v2+
    proposal from, say, ``backend_live`` reopens planning and drops the task
    back to ``contract_proposed`` (SPEC §5 rule 1). Terminal tasks cannot be
    reopened without a human.

    ``number`` (the agent's ``todo=N``) names the ONE deliverable this contract is about.
    It is always required — there is no other kind of contract — and the signatory set is
    that todo's party list, not the task's full cast (see ``_resolve_contract_todo``).

    Versions are numbered PER CHAIN: MAX(version)+1 among *this todo's* contracts, so
    every deliverable's first proposal is v1 and its chain reads 1, 2, 3… A declined v1
    keeps its number (nothing is reused); the replacement is v2.
    """
    todo = _resolve_contract_todo(conn, identity, number)
    # NOTHING is injected into the spec any more. The deployment target is host-owned
    # configuration on the task/todo (see `resolve_staging_url`) and never part of the
    # document the parties sign — so there is no gap to fill here, and a spec that
    # carries a `staging_url` is REFUSED by the validator below rather than quietly
    # accepted into spec_json.
    #
    # `is_remote`/`same_machine` are still threaded through: they are the CONNECTIVITY
    # facts the host-target validator keys on (a peer on another machine needs a real
    # https domain + the SSRF guard; a task the host declared same-machine is one
    # person on one box, where http://localhost:PORT is correct), and the GUI always
    # runs the broker in remote mode for token auth so is_remote alone cannot tell them
    # apart. See contracts.validate_staging_url.
    errors = contracts.validate_spec(
        spec,
        is_remote=config.get_config().is_remote,
        same_machine=_task_is_same_machine(conn, identity.task_id),
    )
    if errors:
        # Raise with joined errors so the agent gets every fix in one shot.
        raise ValueError("invalid contract:\n- " + "\n- ".join(errors))

    _assert_live(conn, identity.task_id)

    # Every PARTY TO THIS TODO must clear pre-flight before anyone can propose (owner
    # rule): a contract agreed with an agent that never proved it understands the
    # protocol is worthless.
    #
    # Scoped to the todo's party list, NOT the task's cast. Pre-flight gates the
    # INTERACTION, not the task — the same rule `parties_json` already encodes: a seat
    # this todo does not name may read it, is not bound by it, is not in its quorum
    # (see `lock_contract`), and therefore does not block it. Asking it for readiness
    # first was a task-wide `WHERE ready = 0` that froze two ready people over a third
    # seat that was party to neither of their deliverables.
    #
    # Remote-only — local self-declared identities don't run pre-flight (the middleware
    # readiness gate is remote-only too), so gating there would brick the local flow.
    if config.get_config().is_remote:
        not_ready = seats.unready(conn, identity.task_id, todos.parties_of(todo))
        if not_ready:
            waiting = ", ".join(r["seat"] for r in not_ready)
            raise ValueError(
                f"every party to todo #{todo['number']} must pass pre-flight before a "
                f"contract can be proposed on it; waiting on: {waiting}. Each of them "
                f"calls readiness_check() then submit_readiness(answers) — tell them "
                f"with send_message. Seats that are not parties to this todo do not "
                f"block it."
            )

    spec_json = json.dumps(spec)
    service.assert_content_size(spec_json, "contract spec")

    # Version is MAX+1 SCOPED TO THIS CHAIN — this todo's own contracts — read in the SAME
    # transaction as the INSERT that consumes it, exactly like `todos.next_number`. That is
    # what makes a deliverable's first proposal read as v1 instead of "v9 on a six-todo
    # task". `_resolve_contract_todo` never returns None, so this is a plain `= ?`.
    #
    # Two concurrent proposals can read the same MAX and collide on the chain's unique
    # index; retry re-reads it, so a racing proposer gets a clean higher version instead of
    # an uncaught 500.
    chain_id = todo["id"]
    for _attempt in range(6):
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM contracts "
            "WHERE task_id = ? AND todo_id = ?",
            (identity.task_id, chain_id),
        ).fetchone()
        version = row["v"] + 1
        try:
            conn.execute(
                "INSERT INTO contracts (task_id, version, spec_json, status, proposed_by, "
                "todo_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (identity.task_id, version, spec_json, "draft", identity.agent_id,
                 chain_id, _now()),
            )
            break
        except sqlite3.IntegrityError as exc:
            # ONLY a version collision is retryable. Catching every IntegrityError meant a
            # NOT NULL / FK violation burned all six attempts and then surfaced as "could
            # not allocate a contract version — please retry", advice that could never work.
            if "todo_id" in str(exc) or "NOT NULL" in str(exc).upper():
                conn.rollback()
                raise
            conn.rollback()
    else:
        raise ValueError("could not allocate a contract version — please retry")

    # Rule 1 one level down: a v2 proposal on a todo that is already built reopens
    # THAT deliverable's planning, and only that one — the other five keep marching.
    _todo_march(conn, identity.task_id, todo["id"], todo["state"], CONTRACT_PROPOSED)
    state = todos.apply_rollup(conn, identity.task_id)
    conn.commit()
    # Tell the peer directly — a transition event alone is dashboard-only and would
    # never reach the other agent's wait_for_message queue. This is what makes
    # planning actually flow: the peer hears "there's a proposal to assess."
    n_endpoints = len(spec.get("endpoints", []))
    scope_note = f" on todo #{todo['number']} ({todo['title']})"
    signers = f"the parties on this todo ({', '.join(todos.parties_of(todo))})"
    # Versions are per-deliverable, so the signature call must carry the deliverable too —
    # `lock_contract(1)` is genuinely ambiguous on a task where five todos each have a v1.
    sign_call = f"lock_contract({version}, todo={todo['number']})"
    service.post_message(
        conn,
        identity,
        "contract_proposal",
        f"Proposed contract v{version}{scope_note} ({n_endpoints} endpoint"
        f"{'' if n_endpoints == 1 else 's'}). Review the shape with get_contract (it now "
        f"shows the proposed contract, not only locked ones), then {sign_call} "
        f"to sign — or message me to request changes first. The staging_url appears in "
        f"get_contract once {signers} have signed.",
        todo_id=todo["id"],
    )
    return {
        "version": version,
        "state": state,
        # ONE selector, on purpose: `todo` is the per-task `#N` the agent passes back on
        # its next call. The internal `todos.id` is NOT returned to an agent — handing an
        # LLM two integers where only one resolves is how a report lands on the wrong
        # deliverable. The dashboard reads the id from `/api`, which joins it itself.
        "todo": todo["number"],
        "todo_state": CONTRACT_PROPOSED,
        "signatories": todos.parties_of(todo),
    }


def _chain_versions(conn, task_id: str, todo_id: int) -> list[int]:
    """Every version in ONE contract chain, ascending. A chain is exactly one todo."""
    return [
        r["version"]
        for r in conn.execute(
            "SELECT version FROM contracts WHERE task_id = ? AND todo_id = ? "
            "ORDER BY version",
            (task_id, todo_id),
        ).fetchall()
    ]


def _wrong_chain_hint(conn, task_id: str, version: int, number: int, todo_id: int) -> str:
    """"You asked for v3 on this deliverable; v3 on this task is a DIFFERENT one."

    The diagnostics half of the fix. Versions are per-chain now, so ``v1`` usually exists
    on several todos and naming a sibling is only TRUE when exactly one other chain holds
    that number. When several do, the honest answer is the rule itself.
    """
    others = [
        r["todo_id"]
        for r in conn.execute(
            "SELECT todo_id FROM contracts WHERE task_id = ? AND version = ? "
            "AND todo_id != ? ORDER BY id",
            (task_id, version, todo_id),
        ).fetchall()
    ]
    if not others:
        return ""
    if len(others) > 1:
        return (
            f" (Contract versions are numbered PER DELIVERABLE, from 1, so several todos "
            f"on this task hold a v{version} and they are different contracts.)"
        )
    sibling = todos.row_by_id(conn, task_id, others[0])
    return (
        f" Contract v{version} belongs to todo #{sibling['number']} "
        f"('{sibling['title']}'), not todo #{int(number)} — check get_todos() before signing."
    )


def _no_such_version(conn, identity: Identity, version: int, number: int, todo) -> str:
    """The refusal for a version that does not exist IN THE CHAIN THE CALLER NAMED.

    This is the message the old code got wrong. It used to look a contract up by
    ``(task_id, version)`` alone, so ``lock_contract(1, todo=2)`` fetched todo #1's row,
    found it locked, and answered "contract version 1 is already locked and immutable" —
    a true sentence about a DIFFERENT deliverable, with advice that was wrong for the one
    the caller asked about. The chain is established first now, so every branch below is
    about the deliverable the caller actually meant — ``todo`` is never None, because
    ``_require_contract_todo`` has already refused a call that named no deliverable.
    """
    task_id = identity.task_id
    todo_id = todo["id"]
    where = f"todo #{todo['number']} ('{todo['title']}')"
    gc = f"get_contract(todo={int(number)})"
    scope = f", todo={int(number)}"
    hint = _wrong_chain_hint(conn, task_id, version, number, todo_id)
    mine = _chain_versions(conn, task_id, todo_id)

    if mine:
        # Something HAS been proposed here — so "nothing to sign" would be a lie. Name
        # what the chain actually holds; that is the whole answer.
        return (
            f"no contract version {version} on {where} — its chain has "
            f"{', '.join('v' + str(v) for v in mine)}. Contract versions are numbered per "
            f"deliverable, from 1, so the same number on another todo is a different "
            f"contract. Read {gc} and sign a version this chain really has." + hint
        )
    # Nothing proposed on this chain at all. The commonest way to land here is a human
    # saying "sign it" on a scope where nobody has proposed anything — and an agent that
    # only learns "no such version" goes back to its human to ask, which is the stall this
    # text exists to end. A signature needs a proposal to attach to, so name the move that
    # is actually missing, and name who can make it (anyone bound by the scope, incl. you).
    return (
        f"no contract version {version} on {where}. Check {gc} — if nothing has been "
        f"proposed yet there is nothing to sign, and the missing step is the PROPOSAL, "
        f"not the signature. If you are a party you can supply it: "
        f"propose_contract(spec{scope}) with your reading of your human's instruction "
        f"stated as an explicit assumption, then sign THAT version. Your peer still "
        f"reviews and signs (or declines) before anything locks." + hint
    )


def lock_contract(conn, identity: Identity, version: int, number: int | None = None) -> dict:
    """Record this agent's signature on a contract version; lock only when every
    required signatory has signed (SPEC §5 rule 2, §6).

    The required signatories are exactly the todo's ``parties_json``. A task seat the todo
    does not bind neither blocks the lock nor may sign it — SEATS ≠ PARTICIPANTS. That is
    why todos need no separate quorum mechanism: it is the same "all must sign" rule over a
    smaller "all". (There used to be a second, task-level kind of contract whose signatory
    set was the whole of ``tasks.roles_json``; it is gone, and with it the branch.)

    A locked contract is immutable (rule 6): re-signing or re-locking it is rejected,
    and changes must go through a fresh version that every signatory re-signs.

    ``number`` (the agent's ``todo=N``) is what identifies the CHAIN, and the lookup is
    scoped by it. That is structural, not a courtesy: versions are numbered per
    deliverable, so ``(task_id, version)`` names as many rows as there are todos. It also
    fixes a diagnostics defect — the old unscoped lookup fetched a SIBLING todo's contract
    and then reported *its* status, so ``lock_contract(1, todo=2)`` answered "version 1 is
    already locked and immutable" about todo #1 while todo #2 had a perfectly signable
    draft. The broker must establish WHICH deliverable the caller meant before it says
    what is wrong with it.
    """
    _assert_live(conn, identity.task_id)

    # Resolve the CHAIN first — the deliverable, not the version. `get_row` is the
    # resolver for anything a human or an agent typed, and it raises a clean "no todo #N
    # on task X" before any contract query happens.
    todo = _require_contract_todo(
        conn, identity.task_id, number, "lock_contract(version, todo=<N>)"
    )
    contract = conn.execute(
        "SELECT id, status, todo_id FROM contracts "
        "WHERE task_id = ? AND todo_id = ? AND version = ?",
        (identity.task_id, todo["id"], version),
    ).fetchone()
    if contract is None:
        raise ValueError(_no_such_version(conn, identity, version, number, todo))

    scope_note = f" on todo #{todo['number']} ({todo['title']})"

    # Permission and liveness of the DELIVERABLE come before the state of its contract:
    # "you are not a party to this" and "this deliverable was dropped" are facts about the
    # thing the caller named, so they are never the wrong answer, whereas a status
    # complaint only makes sense once the caller is entitled to hear it.
    if todo["dropped_at"] is not None:
        raise ValueError(
            f"todo #{todo['number']} was dropped; its contract v{version} cannot be signed"
        )
    # A non-party has no say in a shape that does not bind it — the mirror image of
    # "a non-party does not block the lock".
    todos.assert_party(todo, identity.role, "sign a contract on")

    if contract["status"] == "locked":
        raise ValueError(
            f"contract version {version}{scope_note} is already locked and immutable; "
            f"propose a new version to change it — "
            f"reopen_negotiations(reason, todo={todo['number']}) then "
            f"propose_contract(spec, todo={todo['number']})"
        )
    if contract["status"] == "declined":
        raise ValueError(
            f"contract version {version}{scope_note} was declined and cannot be signed. "
            f"Propose a new version with propose_contract(spec, todo={todo['number']}) that "
            f"addresses the objection — get_contract shows who declined it and why."
        )

    # Record this signature (idempotent — signing twice is a no-op, not an error).
    conn.execute(
        "INSERT OR IGNORE INTO contract_signatures (contract_id, agent_id, signed_at) "
        "VALUES (?,?,?)",
        (contract["id"], identity.agent_id, _now()),
    )

    required = todos.parties_of(todo)
    signed = [
        r["role"]
        for r in conn.execute(
            "SELECT COALESCE(a.handle, a.role) AS role FROM contract_signatures s "
            "JOIN agents a ON a.id = s.agent_id WHERE s.contract_id = ?",
            (contract["id"],),
        ).fetchall()
    ]
    signed_set = set(signed)
    remaining = [r for r in required if r not in signed_set]

    if remaining:
        # Partial signature is a normal, expected outcome — not an error.
        conn.commit()
        # Let the peer know a signature landed and the ball is in their court.
        service.post_message(
            conn,
            identity,
            "contract_lock",
            f"Signed contract v{version}{scope_note}. Waiting on {', '.join(remaining)} to "
            f"sign before it locks.",
            todo_id=todo["id"],
        )
        return {
            "locked": False,
            "version": version,
            "signed": sorted(signed_set),
            "remaining": remaining,
            "todo": todo["number"],
        }

    # All roles have signed → the contract locks and the task advances. The UPDATE
    # is conditional on status='draft' so that if two roles sign the final signature
    # concurrently and both observe "all signed", exactly one wins the lock — the
    # loser's rowcount is 0 and it returns the locked result WITHOUT a duplicate lock
    # event or a second human Slack ping.
    # The lock also RECORDS the deployment target that was live at this moment, so
    # "what did we agree to?" keeps an answer after the host re-points a tunnel. It is
    # written to its own column, never into `spec_json`: the spec is the signed shape,
    # and mutating a document at the instant it is signed would be the one thing a
    # signature must never permit. The recorded value and the live one are allowed to
    # diverge afterwards — that divergence IS the feature.
    cur = conn.execute(
        "UPDATE contracts SET status = 'locked', locked_at = ?, staging_url_at_lock = ? "
        "WHERE id = ? AND status = 'draft'",
        (
            _now(),
            resolve_staging_url(
                conn, identity.task_id, todo["id"], _spec_of(conn, contract["id"])
            ),
            contract["id"],
        ),
    )
    if cur.rowcount != 1:
        conn.commit()
        return {"locked": True, "version": version, "signed": sorted(signed_set)}
    _todo_march(conn, identity.task_id, todo["id"], todo["state"], CONTRACT_LOCKED)
    # A newly locked version is a genuine new ATTEMPT, so the ping-pong counter starts
    # over (SPEC §8). The task-level `_report_deployed` has to derive that from event
    # timestamps (D3) because it cannot see the lock; here we ARE the lock, so we just
    # reset it — and no check can slip in between, since checks are refused until the
    # todo reaches backend_live.
    conn.execute("UPDATE todos SET strikes = 0 WHERE id = ?", (todo["id"],))
    _clear_todo_stuck(conn, todo["id"])
    state = todos.apply_rollup(conn, identity.task_id)
    _event(
        conn, identity.task_id, "lock",
        {"version": version, "signed": sorted(signed_set), "todo_id": todo["id"]},
    )
    _notify(
        conn,
        identity.task_id,
        f"[{identity.task_id}] Contract v{version}{scope_note} locked — signed by "
        f"{', '.join(sorted(signed_set))}",
    )
    conn.commit()
    # PUSH the lock to the roles that already signed. Only the FINAL signer learns the
    # lock from this call's return value; without this the others would have to poll
    # get_contract, and a parked wait_for_message would sleep straight through the lock
    # it is waiting for (an event is not a message; the wait loop only reads the message
    # queue). So the broker drops one broker-authored `contract_locked` notification:
    #   * `service.BROKER_TYPES` → delivered in the <broker trust="broker"> envelope,
    #     never framed as peer DATA, and unforgeable via send_message;
    #   * authored on the finalising signer's row, so `from_agent_id != me` delivers it
    #     to exactly the already-signed roles and not to the signer who just got
    #     {locked: true} synchronously — no double-notify;
    #   * exactly ONE message for exactly one `lock` event, so the dashboard thread
    #     shows a single lock bubble beside its divider (D10's 1:1 invariant holds).
    service.post_message(
        conn,
        identity,
        "contract_locked",
        f"Contract v{version}{scope_note} is LOCKED — signed by all parties "
        f"({', '.join(sorted(signed_set))}). This is the blueprint to build against; read "
        f"the frozen shape and the staging_url from get_contract(todo={todo['number']}). "
        f"Report progress on this deliverable with "
        f"report_status(..., todo={todo['number']}).",
        todo_id=todo["id"],
    )
    return {
        "locked": True,
        "version": version,
        "signed": sorted(signed_set),
        "state": state,
        "todo": todo["number"],
        "todo_state": CONTRACT_LOCKED,
    }


def decline_contract(
    conn, identity: Identity, reason: str, number: int | None = None
) -> dict:
    """Formally push back on a PROPOSED contract: mark that version declined, with a reason.

    Why this exists. A contract had ``lock`` and nothing else, so an unsigned proposal
    meant EITHER "I have read it and I object" OR "I have not looked yet". The broker
    could not tell those apart, so neither could the dashboard, so the humans had to hold
    it in their heads — the exact class of invisibility that strands a collaboration.
    Todos already had accept AND decline; contracts were the odd one out.

    A decline kills that VERSION: nobody may sign it afterwards, and the answer is a new
    proposal, never a mutation of this one (same immutability rule as a locked contract).
    That is not the same as destroying agreed work — a proposal is not yet agreed, and
    withholding a signature already blocked it. This only makes the block visible, and
    says why.

    Anyone bound by the contract may decline, INCLUDING its proposer (who may spot their
    own mistake before a peer wastes time reviewing it).
    """
    _assert_live(conn, identity.task_id)
    service.assert_content_size(reason, "decline reason")
    if not (reason or "").strip():
        raise ValueError(
            "a decline needs a reason — your peer has to know what to change in the "
            "next version"
        )

    # `number` is the agent's `#N` and must be resolved to the internal id BEFORE it
    # reaches a query against `contracts.todo_id`, which keys on `todos.id`.
    #
    # And it is REQUIRED, for the same reason `propose_contract` and `reopen_negotiations`
    # require it: without it the "newest contract" is whichever chain happened to be
    # written to last, so a bare decline could kill a proposal on a deliverable the caller
    # never named — and the refusal it earns instead would be about the wrong one.
    # "Decline the contract" is not a question with one answer on a six-deliverable task.
    if number is None and todos.has_todos(conn, identity.task_id):
        raise ValueError(
            f"task '{identity.task_id}' runs on todos, so a decline must name the "
            f"deliverable whose proposal it pushes back on: "
            f"decline_contract(reason, todo=<N>). Live todos: "
            f"{_live_todo_list(conn, identity.task_id)} — call get_contract(todo=N) to see "
            f"what is proposed on each."
        )
    scoped_to = None if number is None else todos.get_row(conn, identity.task_id, number)
    newest = _newest_contract(
        conn, identity.task_id, scoped_to["id"] if scoped_to is not None else None
    )
    if newest is None:
        # No `number` and no contract means the task has no todos either (the guard above
        # would have fired), and a task with no todos has nothing to contract.
        scope = (
            f"todo #{scoped_to['number']}" if scoped_to is not None
            else f"task '{identity.task_id}'"
        )
        raise ValueError(
            f"nothing to decline on {scope} — no contract has been proposed yet. "
            f"Declining pushes back on a PROPOSAL; there has to be one first."
        )
    # Establish the DELIVERABLE (and the caller's standing on it) before reporting what
    # is wrong with its contract — the same ordering rule as ``lock_contract``. A status
    # complaint that does not say which deliverable it is about is how the broker ends up
    # blaming the wrong thing. `todo_id` is NOT NULL, so there is always one to name.
    todo = todos.row_by_id(conn, identity.task_id, newest["todo_id"])
    # A non-party has no say in a shape that does not bind it — the mirror image of the
    # same rule on signing.
    todos.assert_party(todo, identity.role, "decline a contract on")
    scope_note = f" on todo #{todo['number']} ({todo['title']})"
    scope = f", todo={todo['number']}"

    if newest["status"] == "locked":
        raise ValueError(
            f"contract v{newest['version']}{scope_note} is already locked — a locked "
            f"contract is immutable. To change it, both of you reopen planning with "
            f"reopen_negotiations(reason{scope}), then a new version is proposed and signed."
        )
    if newest["status"] == "declined":
        raise ValueError(
            f"contract v{newest['version']}{scope_note} is already declined — propose a "
            f"new version with propose_contract(spec{scope}) rather than declining it twice."
        )

    now = time.time()
    conn.execute(
        "UPDATE contracts SET status = 'declined', declined_by = ?, decline_reason = ?, "
        "declined_at = ? WHERE id = ?",
        (identity.agent_id, reason, now, newest["id"]),
    )
    # Any signatures already on this version are void with it — keeping them would let a
    # later reader think the version was part-agreed when it is dead.
    conn.execute("DELETE FROM contract_signatures WHERE contract_id = ?", (newest["id"],))
    conn.commit()

    _event(
        conn,
        identity.task_id,
        "lock",
        {
            "text": f"Contract v{newest['version']}{scope_note} declined by "
                    f"{identity.role}: {reason}"
        },
    )
    service.post_message(
        conn,
        identity,
        "renegotiation",
        f"Declined contract v{newest['version']}{scope_note}: {reason}. That version is "
        f"dead — nobody can sign it now. Propose a new version with "
        f"propose_contract(spec{scope}) that addresses this, or message me if you want to "
        f"talk the shape through first.",
        todo_id=todo["id"],
    )
    return {
        "version": newest["version"],
        "status": "declined",
        "declined_by": identity.role,
        "reason": reason,
        "todo": todo["number"],
        "next": f"propose a new version with propose_contract(spec, todo={todo['number']})",
    }


def reopen_negotiations(
    conn, identity: Identity, reason: str, number: int | None = None
) -> dict:
    """Drop a locked-or-later task back to ``contract_proposed`` (planning) so a
    new contract version can be proposed and re-signed.

    Non-destructive: the currently-locked contract keeps serving via ``get_contract``
    until a NEW version locks — nothing is deleted. Either party may call it (the
    "agreement" happens in chat first; a one-sided reopen is harmless — the peer just
    won't propose/sign anything new). Rejected on terminal tasks, and on tasks that
    haven't locked a contract yet (there's nothing to re-plan — just keep talking
    or propose a first version).

    On a task with todos it reopens ONE deliverable (``number`` is then required, for
    the same reason a proposal needs it — "reopen planning" on six chains is not a
    thing), and only that todo goes back to planning; the other five keep marching.
    This is also the sanctioned route to change a todo whose contract already LOCKED:
    reopen → propose a new version → every party re-signs (``repropose_todo`` refuses
    once a lock exists, because a locked contract is immutable).
    """
    current = _assert_live(conn, identity.task_id)
    if number is None and todos.has_todos(conn, identity.task_id):
        raise ValueError(
            f"task '{identity.task_id}' runs on todos, so name the deliverable to "
            f"replan: reopen_negotiations(reason, todo=<N>). Live todos: "
            f"{_live_todo_list(conn, identity.task_id)}."
        )

    if number is not None:
        return _reopen_todo(conn, identity, reason, number)

    if _current_locked(conn, identity.task_id) is None:
        raise ValueError(
            "nothing to reopen — no contract has locked yet. Keep planning, or "
            "propose a first version with propose_contract."
        )
    if current in (OPEN, CONTRACT_PROPOSED):
        raise ValueError(f"already in planning (state '{current}') — nothing to reopen")

    detail = (reason or "").strip() or "(no reason given)"
    service.assert_content_size(detail, "reopen reason")
    state = _transition(conn, identity.task_id, CONTRACT_PROPOSED)
    _event(conn, identity.task_id, "reopen", {"from": current, "reason": detail})
    conn.commit()
    service.post_message(
        conn,
        identity,
        "renegotiation",
        f"Reopened planning (was {current}): {detail}. The last locked contract still "
        f"stands until a new version is proposed and re-signed.",
    )
    return {"state": state, "from": current, "reason": detail}


def _reopen_todo(conn, identity: Identity, reason: str, number: int) -> dict:
    """Reopen planning on ONE deliverable. Same rules, one level down.

    ``number`` is the agent's ``#N``; the internal id is read off the resolved row.
    """
    row = todos.get_row(conn, identity.task_id, number)
    if row["dropped_at"] is not None:
        raise ValueError(f"todo #{row['number']} was dropped; there is nothing to replan")
    todos.assert_party(row, identity.role, "reopen planning on")
    if row["state"] == VERIFIED:
        raise ValueError(
            f"todo #{row['number']} is verified — replanning finished work would make the "
            f"task's rollup lie. Propose a NEW todo for follow-up work."
        )
    if _current_locked(conn, identity.task_id, todo_id=row["id"]) is None:
        raise ValueError(
            f"nothing to reopen on todo #{row['number']} — no contract has locked on it "
            f"yet. Keep planning, or propose a first version with "
            f"propose_contract(spec, todo={row['number']})."
        )
    if row["state"] in (OPEN, CONTRACT_PROPOSED):
        raise ValueError(
            f"todo #{row['number']} is already in planning (state '{row['state']}') — "
            f"nothing to reopen"
        )

    detail = (reason or "").strip() or "(no reason given)"
    service.assert_content_size(detail, "reopen reason")
    _todo_march(conn, identity.task_id, row["id"], row["state"], CONTRACT_PROPOSED)
    # `text` is not decoration: `reopen` is not one of api._EVENT_KINDS, so
    # `_render_detail` falls through to its generic branch — which prints `text` when
    # there is one and otherwise DUMPS THE WHOLE DETAIL AS JSON. Without this the human
    # event log printed the internal `todo_id`. `todo_number` rides along beside the id
    # for the same reason todos._event carries both: joinable AND printable.
    _event(
        conn, identity.task_id, "reopen",
        {
            "text": f"Reopened planning on todo #{row['number']} ('{row['title']}'), "
                    f"was {row['state']}: {detail}",
            "from": row["state"],
            "reason": detail,
            "todo_id": row["id"],
            "todo_number": row["number"],
        },
    )
    state = todos.apply_rollup(conn, identity.task_id)
    conn.commit()
    service.post_message(
        conn,
        identity,
        "renegotiation",
        f"Reopened planning on todo #{row['number']} ({row['title']}), was {row['state']}: "
        f"{detail}. Its last locked contract still stands until a new version is proposed "
        f"with propose_contract(spec, todo={row['number']}) and re-signed by "
        f"{', '.join(todos.parties_of(row))}.",
        todo_id=row["id"],
    )
    return {
        "state": state,
        "from": row["state"],
        "reason": detail,
        "todo": row["number"],
        "todo_state": CONTRACT_PROPOSED,
    }


def get_contract(conn, task_id: str, number: int | None = None) -> dict:
    """Return the current contract for the task — PROPOSED or LOCKED.

    This is the single source of truth for the interface, at both stages:

    * LOCKED — the full frozen shape, plus the ``staging_url`` RESOLVED LIVE from the
      host's configuration (:func:`resolve_staging_url`), never from a chat message
      and no longer from the signed document (SPEC §5 rule 5, §9). This is the trusted
      source of the staging URL for a consumer, and it is now trusted for a stronger
      reason than before: the value has no agent-writable input at all, so an injected
      "test against evil.com" has nothing to land in. ``staging_url_at_lock`` rides
      alongside it — the target that was live when the contract locked — so "what did
      we agree to?" still has an answer when the host has since re-pointed a tunnel.
    * PROPOSED (not yet locked) — the proposed SHAPE, so an assessor can review it
      before signing, WITH the ``staging_url`` WITHHELD (``None``): an unsigned URL
      must not be fetchable (rule 2), so it only appears once every role has signed.
      That incentive to actually READ the shape survives the target becoming
      host-owned, which is why this withholding is keyed on the LOCK and not on
      whether a target happens to be configured.
      Also returns who has ``signatures`` / who is ``awaiting``.

    Returning proposals here (not just locked ones) removes the old chicken-and-egg
    where an assessor was told to review via get_contract but saw exists:false until
    it locked — which it can't do until they sign.

    ``number`` (the agent's ``todo=N``) scopes it to ONE deliverable's chain. This is the
    one contract op where it is OPTIONAL, and deliberately so: reading is how an agent
    recovers a number it has lost. Without it you get the task's newest contract whichever
    todo it belongs to — which is also what the dashboard reads — plus a ``todo`` field
    (the per-task ``#N``, the only selector an agent ever gets back) and a note saying
    which deliverable that is, because "the current contract" is genuinely ambiguous once
    there are six.
    """
    todo = None if number is None else todos.get_row(conn, task_id, number)
    newest = _newest_contract(conn, task_id, todo_id=(todo["id"] if todo is not None else None))
    if newest is None:
        if todo is None:
            return {"exists": False}
        return {
            "exists": False,
            "todo": todo["number"],
            "note": (
                f"No contract has been proposed on todo #{todo['number']} "
                f"('{todo['title']}') "
                f"yet. Agree on WHAT first (every party accepts the todo), then one party "
                f"proposes the HOW with propose_contract(spec, todo={todo['number']}). If your "
                f"human has already told you to sign or lock this one, that IS the "
                f"direction to propose it — send the shape their instruction implies with "
                f"your reading stated as an explicit assumption, then sign it; your peer "
                f"still reviews and signs (or declines) before it locks."
            ),
        }

    spec = newest["spec"]
    signed = sorted(_signatures_for(conn, newest["id"]))
    # The signatory set is the CONTRACT's, not the caller's request: a contract is signed
    # by its todo's party list even when the caller asked task-wide, so `awaiting` must
    # never name a seat the todo does not bind. `todo_id` is NOT NULL, so there is always
    # a deliverable to name here.
    scoped = todos.row_by_id(conn, task_id, newest["todo_id"])
    todo_block = {
        "todo": scoped["number"],
        "todo_title": scoped["title"],
        "signatories": todos.parties_of(scoped),
    }

    if newest["status"] == "locked":
        return {
            "exists": True,
            "version": newest["version"],
            "status": "locked",
            "locked": True,
            # LIVE, not frozen: the host's current target for this deliverable. A
            # tunnel restart changes this and touches no contract, no version and no
            # signature — which is the entire reason the target moved out of the spec.
            "staging_url": resolve_staging_url(conn, task_id, scoped["id"], spec),
            # …and the target that was live when this locked, so the two questions
            # ("where do I point tests now?" / "what did we agree to?") stay separate
            # answers instead of one string doing both jobs badly.
            "staging_url_at_lock": newest["staging_url_at_lock"],
            "spec": spec,
            "signatures": signed,
            "locked_at": newest["locked_at"],
            **todo_block,
        }

    # Proposed / draft: expose the shape, and no target until it locks. `spec` cannot
    # carry a staging_url any more (the validator refuses one), but a LEGACY row can —
    # so the strip stays, or reviewing an old proposal would hand out an unsigned URL.
    required = todos.parties_of(scoped)
    awaiting = [r for r in required if r not in set(signed)]
    shape = {k: v for k, v in spec.items() if k != "staging_url"}
    out = {
        "exists": True,
        "version": newest["version"],
        "status": "proposed",
        "locked": False,
        "staging_url": None,
        "spec": shape,
        "signatures": signed,
        "awaiting": awaiting,
        "note": (
            # The version alone never names one contract: versions are per deliverable,
            # so the signature call has to carry the todo as well.
            f"Proposed, not yet locked — this is the shape to review. Call "
            f"lock_contract({newest['version']}, todo={scoped['number']}) to sign it; "
            f"the staging_url appears here only after all of {', '.join(required)} "
            f"have signed."
        ),
        **todo_block,
    }
    # A v2 replan can be in flight while an older v1 is still the locked, serving
    # contract — point the assessor at it so they know what's currently in force.
    # Scoped to the same chain: the version in force on THIS deliverable, never a
    # sibling todo's lock, which would send the assessor to the wrong blueprint.
    locked = _current_locked(conn, task_id, todo_id=scoped["id"])
    if locked is not None and locked["version"] != newest["version"]:
        out["locked_version_in_force"] = locked["version"]
    return out


# --------------------------------------------------------------------------- #
# status reporting — the transitions, the strikes, the typed messages
# --------------------------------------------------------------------------- #
def report_status(
    conn, identity: Identity, status: str, detail: str, number: int | None = None
) -> dict:
    """Drive a state transition AND post the corresponding typed message so the
    dashboard thread reflects it. Rejects (raises ``ValueError``) with a clear,
    agent-readable reason on any workflow or permission violation.

    Role-scoped permissions and state gates are enforced here, in code:
      * ``ready``         — producer role only; needs a locked contract; → backend_live
      * ``checked/blocked`` — consumer roles only; only after the producer is ready
      * ``verified``      — → terminal verified
      * ``stuck``         — → terminal stuck
    The old API/deploy-shaped words (``deployed``/``test_passed``/``test_failed``) are
    still accepted as aliases of ``ready``/``checked``/``blocked``.

    SCOPE, once the task has todos (v2): "backend is ready" is meaningless when there
    are six deliverables, so ``ready``/``checked``/``blocked``/``verified`` are
    todo-scoped and ``number`` (``todo=N``) is REQUIRED — the ambiguity is removed by construction,
    not by convention. ``stuck`` is valid at BOTH levels and they stay distinguishable:
    with a todo it FLAGS that deliverable (the other five keep marching), without one it
    escalates the whole collaboration to a terminal ``stuck``. The TASK's own state is
    then never set by an agent at all — it is re-derived from the todos after every
    report (``todos.apply_rollup``), which is what makes "the task concludes when the
    LAST todo verifies" fall out instead of needing a special case.
    """
    service.assert_content_size(detail, "status detail")

    # Task-agnostic words funnel into the existing paths: normalize before any
    # dispatch or mode gate inspects the status, so all downstream logic is unchanged.
    # The word the AGENT typed is kept for error messages: telling an agent that
    # 'deployed' is todo-scoped when it typed 'ready' is a small lie it has to decode.
    requested = status
    status = _STATUS_ALIASES.get(status, status)

    # 'waiting' is a soft, mode-agnostic NUDGE — handled before the mode gate so it works
    # on both contract and debug tasks. It pings the humans without changing state, touching
    # strikes, or ending anything: the rung below 'stuck' for "a peer's gone quiet, a human's
    # attention would help" (see the give-up rule in the prompt). Task-level only — to flag
    # ONE deliverable, use 'stuck' with a todo NUMBER.
    if status == STATUS_WAITING:
        if number is not None:
            raise ValueError(
                "'waiting' is a task-level nudge for the humans — it takes no todo number. "
                "To flag one deliverable, use report_status('stuck', detail, todo=<N>)."
            )
        return _report_waiting(conn, identity, detail)

    # Mode gate: debug tasks have a single 'resolved' status; contract tasks have
    # the full deploy/test/verified vocabulary. Keep the two vocabularies disjoint.
    mode = _task_mode(conn, identity.task_id)
    if mode == "debug" and number is not None:
        raise ValueError(
            "debug tasks don't carry todos — they are a single problem you fix and "
            "report_status('resolved'). Todos are for contract tasks."
        )
    if mode == "debug" and status != STATUS_RESOLVED:
        raise ValueError(
            "this is a debug task — report_status('resolved') when the issue is "
            "fixed (deploy/test/verified don't apply)"
        )
    if mode != "debug" and status == STATUS_RESOLVED:
        raise ValueError(
            "'resolved' is only for debug tasks; contract tasks finish with 'verified'"
        )

    if number is not None:
        return _report_on_todo(conn, identity, status, detail, number, requested)
    if status in TODO_SCOPED_STATUSES and todos.has_todos(conn, identity.task_id):
        raise ValueError(
            f"'{requested}' is per-DELIVERABLE on task '{identity.task_id}', which runs on "
            f"todos — '{requested}' on which one? Pass the todo number: "
            f"report_status('{requested}', detail, todo=<N>). Live todos: "
            f"{_live_todo_list(conn, identity.task_id)} — call get_todos() to see their "
            f"scopes, parties and progress. (Only 'stuck' may be reported without a todo, "
            f"and that escalates the WHOLE collaboration, not one deliverable.)"
        )

    if status == STATUS_DEPLOYED:
        return _report_deployed(conn, identity, detail)
    if status in TEST_STATUSES:
        return _report_test(conn, identity, status, detail)
    if status == STATUS_VERIFIED:
        return _report_verified(conn, identity, detail)
    if status == STATUS_STUCK:
        return _report_stuck(conn, identity, detail)
    if status == STATUS_RESOLVED:
        return _report_resolved(conn, identity, detail)
    raise ValueError(
        f"unknown status {status!r}; expected one of: "
        f"{STATUS_READY}, {STATUS_CHECKED}, {STATUS_BLOCKED}, "
        f"{STATUS_VERIFIED}, {STATUS_STUCK}"
    )


# --------------------------------------------------------------------------- #
# status reporting, one level down — the per-todo march
# --------------------------------------------------------------------------- #
def _report_on_todo(
    conn, identity: Identity, status: str, detail: str, number: int, requested: str
) -> dict:
    """Dispatch a todo-scoped report: same gates, one deliverable, then re-derive
    the task.

    Two guards belong here rather than in each branch:

    * only a NAMED PARTY may report on a todo — the mirror of "a seat the todo does
      not bind does not block it";
    * the three-strike cord is keyed off ``todos.strikes``, a DB count an agent cannot
      argue with (SPEC §8). Once it is pulled, that deliverable takes no more
      ready/check/verified — humans own it (the host can drop it, or the parties can
      propose a fresh todo). ``stuck`` stays available so an agent can still add
      context for those humans.
    """
    _assert_live(conn, identity.task_id)
    row = todos.get_row(conn, identity.task_id, number)
    if row["dropped_at"] is not None:
        raise ValueError(
            f"todo #{row['number']} was dropped; there is nothing left to report on it"
        )
    todos.assert_party(row, identity.role, f"report '{requested}' on")
    # ``verified`` IS terminal for a todo, even though it no longer is for the task: any
    # further report would march a finished deliverable backwards, and the task's
    # "N of 6 verified" rollup — and its own conclusion — would become a lie. Follow-up
    # work on something already verified is a NEW todo.
    if row["state"] == VERIFIED:
        raise ValueError(
            f"todo #{row['number']} ('{row['title']}') is verified — nothing more is reported "
            f"on it. Propose a NEW todo for follow-up work, or escalate the whole task "
            f"with report_status('stuck', detail) if it needs humans."
        )

    if status == STATUS_STUCK:
        return _report_todo_stuck(conn, identity, row, detail)
    if row["strikes"] >= MAX_STRIKES:
        raise ValueError(
            f"todo #{row['number']} ('{row['title']}') reached {MAX_STRIKES} fix cycles — the "
            f"broker pulled the cord and humans own it now. The other todos on this task "
            f"are unaffected; either the host drops this one or you agree a fresh todo."
        )
    if status == STATUS_DEPLOYED:
        return _report_todo_deployed(conn, identity, row, detail)
    if status in TEST_STATUSES:
        return _report_todo_test(conn, identity, row, status, detail)
    if status == STATUS_VERIFIED:
        return _report_todo_verified(conn, identity, row, detail)
    raise ValueError(
        f"unknown status {requested!r}; expected one of: "
        f"{STATUS_READY}, {STATUS_CHECKED}, {STATUS_BLOCKED}, "
        f"{STATUS_VERIFIED}, {STATUS_STUCK}"
    )


def _todo_result(conn, identity: Identity, row, status: str, **extra) -> dict:
    """Commit, then answer with BOTH levels: the todo that moved and the task state it
    re-derived. An agent that only reads ``state`` still sees the truth about the task."""
    task_state = todos.apply_rollup(conn, identity.task_id)
    conn.commit()
    fresh = todos.row_by_id(conn, identity.task_id, row["id"])
    return {
        "status": status,
        # `todo` is the `#N` the agent hands back on its next report, and the ONLY
        # deliverable handle in an agent-facing reply — see `propose_contract`.
        "todo": fresh["number"],
        "todo_state": fresh["state"],
        "strikes": fresh["strikes"],
        "state": task_state,
        "rollup": todos.rollup(conn, identity.task_id),
        **extra,
    }


def _report_todo_deployed(conn, identity: Identity, row, detail: str) -> dict:
    """The producer of ONE deliverable signals it is ready to build against. Same
    gates as the task-level path — a locked contract must exist, no newer proposal may
    be in flight, and only the role that PROPOSED that lock may report it — read off
    the todo's own contract chain (model B, per todo: backend may produce todo #1
    while mobile produces todo #2)."""
    newest = _newest_contract(conn, identity.task_id, todo_id=row["id"])
    if newest is not None and newest["status"] != "locked":
        raise ValueError(
            f"cannot report 'ready' on todo #{row['number']} while contract v{newest['version']} "
            f"is awaiting signatures; lock that version first"
        )
    if _current_locked(conn, identity.task_id, todo_id=row["id"]) is None:
        raise ValueError(
            f"cannot report 'ready' on todo #{row['number']}: no locked contract exists on "
            f"it yet. Agree the shape with propose_contract(spec, todo={row['number']}) and have "
            f"every party sign it."
        )
    producer = _producer_role(conn, identity.task_id, todo_id=row["id"])
    if identity.role != producer:
        raise ValueError(
            f"only the role that proposed the contract on todo #{row['number']} may report "
            f"'ready' for it (the producer there is '{producer}', you are "
            f"'{identity.role}')"
        )

    _clear_todo_stuck(conn, row["id"])  # a deliverable that is moving again is not stuck
    _todo_march(conn, identity.task_id, row["id"], row["state"], BACKEND_LIVE)
    _event(conn, identity.task_id, "deploy", {"text": detail, "todo_id": row["id"]})
    service.post_message(
        conn, identity, "deploy_confirmed", f"{_todo_label(row)} {detail}",
        todo_id=row["id"],
    )
    return _todo_result(conn, identity, row, STATUS_DEPLOYED)


def _report_todo_test(conn, identity: Identity, row, status: str, detail: str) -> dict:
    """A consuming party reports a check on ONE deliverable. A failure is a strike on
    THAT todo, and the third pulls the cord on it alone: the todo is flagged stuck and
    the humans are pinged, but the task is NOT forced terminal — one bricked
    deliverable must not freeze the other five (this is why ``stuck`` is a per-todo
    FLAG and never a march state)."""
    if row["state"] not in (BACKEND_LIVE, TESTING):
        raise ValueError(
            f"cannot report a check on todo #{row['number']} before its producer is ready "
            f"(the todo is '{row['state']}', need '{BACKEND_LIVE}')"
        )
    producer = _producer_role(conn, identity.task_id, todo_id=row["id"])
    if identity.role == producer:
        raise ValueError(
            f"the producer ('{producer}') doesn't report checks on its own work; the "
            f"consuming party/parties on todo #{row['number']} do"
        )

    # First check after a deploy moves this deliverable into its testing phase.
    _todo_march(conn, identity.task_id, row["id"], row["state"], TESTING)

    if status == STATUS_TEST_PASSED:
        _event(conn, identity.task_id, "test",
               {"pass": True, "strike": None, "todo_id": row["id"]})
        service.post_message(conn, identity, "test_result", f"{_todo_label(row)} {detail}",
                             todo_id=row["id"])
        return _todo_result(conn, identity, row, status)

    # blocked → the broker (not the agent) counts the strike, on the todo.
    conn.execute("UPDATE todos SET strikes = strikes + 1 WHERE id = ?", (row["id"],))
    strikes = todos.row_by_id(conn, identity.task_id, row["id"])["strikes"]
    _event(conn, identity.task_id, "test",
           {"pass": False, "strike": strikes, "todo_id": row["id"]})
    service.post_message(conn, identity, "test_result", f"{_todo_label(row)} {detail}",
                         todo_id=row["id"])

    if strikes >= MAX_STRIKES:
        conn.execute(
            "UPDATE todos SET stuck_at = ?, stuck_reason = ? WHERE id = ?",
            (_now(), f"{MAX_STRIKES} fix cycles reached. Last failure: {detail}", row["id"]),
        )
        todos._event(conn, identity.task_id, "todo_stuck", row["id"],
                     {"reason": detail, "by": identity.role, "strikes": strikes})
        service.post_message(
            conn, identity, "stuck",
            f"{_todo_label(row)} {MAX_STRIKES} fix cycles reached on this deliverable — "
            f"humans needed. Last failure: {detail}. The rest of the task is unaffected; "
            f"don't re-report on this todo until a human unblocks it.",
            todo_id=row["id"],
        )
        _notify(
            conn, identity.task_id,
            f"[{identity.task_id}] STUCK on todo #{row['number']} ({row['title']}): "
            f"{MAX_STRIKES} fix cycles reached — humans needed. Last failure: {detail}",
        )
    return _todo_result(conn, identity, row, status)


def _report_todo_verified(conn, identity: Identity, row, detail: str) -> dict:
    """ONE deliverable is confirmed end-to-end. Requires that checks have actually run
    on it (the todo is in ``testing``), exactly as the task-level rule does.

    This is where ``verified`` stops being task-terminal: the rollup turns the TASK
    verified only when every live todo is verified, so the task concludes on the LAST
    one rather than the first."""
    if row["state"] != TESTING:
        raise ValueError(
            f"cannot report 'verified' on todo #{row['number']} before checks have run on it "
            f"(the todo is '{row['state']}', need '{TESTING}'): nobody has checked it yet. "
            f"A CONSUMING party — not the producer, who may not check its own work — "
            f"reports it first with report_status('checked', todo={row['number']}), which is "
            f"what moves this todo to 'testing'; 'verified' is available after that. That "
            f"gate is why a verified todo can never mean 'nobody actually tried it'."
        )
    _todo_march(conn, identity.task_id, row["id"], row["state"], VERIFIED)
    conn.execute(
        "UPDATE todos SET verified_at = ?, stuck_at = NULL, stuck_reason = NULL WHERE id = ?",
        (_now(), row["id"]),
    )
    service.post_message(conn, identity, "verified", f"{_todo_label(row)} {detail}",
                         todo_id=row["id"])
    out = _todo_result(conn, identity, row, STATUS_VERIFIED)
    roll = out["rollup"] or {}
    if roll.get("complete"):
        _notify(
            conn, identity.task_id,
            f"[{identity.name}] VERIFIED: every todo on '{identity.task_id}' is done "
            f"({roll.get('total')}/{roll.get('total')}). Last one: {row['title']} — {detail}",
        )
    else:
        _notify(
            conn, identity.task_id,
            f"[{identity.name}] todo #{row['number']} ({row['title']}) VERIFIED "
            f"({roll.get('verified')}/{roll.get('total')} on '{identity.task_id}'): {detail}",
        )
    conn.commit()
    return out


def _report_todo_stuck(conn, identity: Identity, row, detail: str) -> dict:
    """A stuck DELIVERABLE — a flag, never a state, and never terminal for the task.

    Task-level ``stuck`` bricks the collaboration on purpose; doing that from one of
    six todos would take the other five down with it. So this stamps
    ``todos.stuck_at`` (which the rollup never reads — see ``todos.rollup``), pings the
    humans, and leaves the march exactly where it was, so work can resume the moment
    the humans unblock it.
    """
    conn.execute(
        "UPDATE todos SET stuck_at = ?, stuck_reason = ? WHERE id = ?",
        (_now(), detail, row["id"]),
    )
    todos._event(conn, identity.task_id, "todo_stuck", row["id"],
                 {"reason": detail, "by": identity.role})
    service.post_message(conn, identity, "stuck", f"{_todo_label(row)} {detail}",
                         todo_id=row["id"])
    _notify(
        conn, identity.task_id,
        f"[{identity.task_id}] STUCK on todo #{row['number']} ({row['title']}): {detail}",
    )
    return _todo_result(conn, identity, row, STATUS_STUCK, stuck=True)


def _report_deployed(conn, identity: Identity, detail: str) -> dict:
    """The producer signals its part is ready for the other side to build on. Gated
    on: caller IS the producer (model B: the role that proposed the locked contract),
    task not terminal, and a locked contract exists (SPEC §5 rule 3 — no contract, no
    ready). Resets strikes when this carries a *newer* locked contract version than
    the last one (SPEC §8 — a genuine new attempt, not the same loop).
    """
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)

    # A proposal in flight means an unsigned newer version exists. Advancing now would
    # move the task on a contract that hasn't been re-signed by all roles (SPEC §5
    # rule 6). Even though an older version is still 'locked', refuse until the pending
    # proposal is locked.
    if state == CONTRACT_PROPOSED:
        raise ValueError(
            "cannot report 'ready' while a contract proposal is awaiting signatures; "
            "lock the current version first"
        )

    contract = _current_locked(conn, identity.task_id)
    if contract is None:
        raise ValueError("cannot report 'ready': no locked contract exists yet")

    # Only the producer — the role that PROPOSED this locked contract — may report it.
    producer = _producer_role(conn, identity.task_id)
    if identity.role != producer:
        raise ValueError(
            f"only the role that proposed the contract may report 'ready' "
            f"(the producer is '{producer}', you are '{identity.role}')"
        )

    # Strike reset: if the current locked contract was locked *after* the previous
    # deploy, the backend is deploying a re-planned version — a fresh attempt, so
    # the ping-pong counter starts over. Same contract redeployed = same loop, keep
    # the count. This needs no extra column: locked_at vs the last deploy event time.
    last_deploy = conn.execute(
        "SELECT created_at FROM events WHERE task_id = ? AND kind = 'deploy' "
        "ORDER BY id DESC LIMIT 1",
        (identity.task_id,),
    ).fetchone()
    if (
        last_deploy is not None
        and contract["locked_at"] is not None
        and contract["locked_at"] > last_deploy["created_at"]
    ):
        conn.execute("UPDATE tasks SET strikes = 0 WHERE id = ?", (identity.task_id,))

    state = _transition(conn, identity.task_id, BACKEND_LIVE)
    _event(conn, identity.task_id, "deploy", {"text": detail})
    service.post_message(conn, identity, "deploy_confirmed", detail)
    conn.commit()
    strikes = conn.execute(
        "SELECT strikes FROM tasks WHERE id = ?", (identity.task_id,)
    ).fetchone()["strikes"]
    return {"status": STATUS_DEPLOYED, "state": state, "strikes": strikes}


def _report_test(conn, identity: Identity, status: str, detail: str) -> dict:
    """A consuming role reports a check result. Gated on: caller is NOT the producer
    (model B: the role that proposed the locked contract runs no checks on its own
    work), and the producer is already ready (SPEC §5 rule 4 — no checks before
    backend_live). A failure is a broker-counted strike; the third pulls the stuck cord.
    """
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)
    if state not in (BACKEND_LIVE, TESTING):
        raise ValueError(
            f"cannot report a check before the producer is ready "
            f"(task is '{state}', need '{BACKEND_LIVE}')"
        )
    # The producer doesn't check its own work; the consuming role(s) do.
    producer = _producer_role(conn, identity.task_id)
    if identity.role == producer:
        raise ValueError(
            f"the producer ('{producer}') doesn't report checks on its own work; "
            f"the consuming role(s) do"
        )

    # First test after a deploy advances the task into the testing phase.
    if state == BACKEND_LIVE:
        state = _transition(conn, identity.task_id, TESTING)

    if status == STATUS_TEST_PASSED:
        _event(conn, identity.task_id, "test", {"pass": True, "strike": None})
        service.post_message(conn, identity, "test_result", detail)
        conn.commit()
        return {"status": status, "state": state, "strikes": _strikes(conn, identity.task_id)}

    # test_failed → the broker (not the agent) counts the strike.
    conn.execute("UPDATE tasks SET strikes = strikes + 1 WHERE id = ?", (identity.task_id,))
    strikes = _strikes(conn, identity.task_id)
    _event(conn, identity.task_id, "test", {"pass": False, "strike": strikes})
    service.post_message(conn, identity, "test_result", detail)

    if strikes >= MAX_STRIKES:
        # Three strikes: force stuck, refuse further cycles. The counter is a DB
        # column — an agent cannot talk it out of this (SPEC §8).
        state = _transition(conn, identity.task_id, STUCK)
        service.post_message(
            conn, identity, "stuck",
            f"{MAX_STRIKES} fix cycles reached — humans needed. Last failure: {detail}",
        )
        _notify(
            conn, identity.task_id,
            f"[{identity.task_id}] STUCK: {MAX_STRIKES} fix cycles reached — humans needed. "
            f"Last failure: {detail}",
        )
    conn.commit()
    return {"status": status, "state": state, "strikes": strikes}


def _report_verified(conn, identity: Identity, detail: str) -> dict:
    """The feature is confirmed end-to-end → terminal ``verified``. Per SPEC §5
    (state table): ``verified`` transitions from ``testing`` ONLY — a client must
    have reported at least one test result first (that is what moves the task
    backend_live → testing), so the terminal state can never be reached with zero
    tests run. Role is unrestricted (any party may confirm); a human owns any
    reopening after."""
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)
    if state != TESTING:
        raise ValueError(
            f"cannot report 'verified' before tests have run (task is '{state}', "
            f"need '{TESTING}'): nobody has checked yet. A consumer — not the producer — "
            f"reports the check first with report_status('checked'), which is what moves "
            f"this to 'testing'; then 'verified' is available. That gate is why a verified "
            f"task can never mean 'nobody actually tried it'."
        )
    state = _transition(conn, identity.task_id, VERIFIED)
    service.post_message(conn, identity, "verified", detail)
    _notify(conn, identity.task_id, f"[{identity.name}] VERIFIED: {detail}")
    conn.commit()
    return {"status": STATUS_VERIFIED, "state": state}


def _report_resolved(conn, identity: Identity, detail: str) -> dict:
    """Debug task: the issue is fixed → terminal ``resolved``. Either party may
    resolve, from any non-terminal state. Reopening after is a human's job."""
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)
    state = _transition(conn, identity.task_id, RESOLVED)
    service.post_message(conn, identity, "resolved", detail)
    _event(conn, identity.task_id, "resolved", {"text": detail})
    _notify(conn, identity.task_id, f"[{identity.name}] RESOLVED: {detail}")
    conn.commit()
    return {"status": STATUS_RESOLVED, "state": state}


def _report_stuck(conn, identity: Identity, detail: str) -> dict:
    """Give up and pull in the humans → terminal ``stuck``. Valid from any
    non-terminal state (SPEC §7).

    This is the WHOLE-collaboration escalation ("my token expired", "I don't understand
    the goal") and stays available on a task with todos, where it outranks the rollup:
    ``todos.apply_rollup`` refuses to overwrite a human-escalated stuck, so one call
    here freezes every deliverable until a human reopens it. To flag a single
    deliverable instead, pass its ``#N`` (``todos.number``) — see ``_report_todo_stuck``."""
    _assert_live(conn, identity.task_id)
    state = _transition(conn, identity.task_id, STUCK)
    service.post_message(conn, identity, "stuck", detail)
    _notify(conn, identity.task_id, f"[{identity.task_id}] STUCK: {detail}")
    conn.commit()
    return {"status": STATUS_STUCK, "state": state}


def _report_waiting(conn, identity: Identity, detail: str) -> dict:
    """A soft nudge for the humans — NOT a state change and NOT terminal.

    The give-up rung below ``stuck``: when a peer has gone quiet and a human's attention
    would help, this pings Slack (and the desktop operator via Claude remote when the agent
    yields) and drops a message + event on the thread — WITHOUT transitioning the task,
    touching strikes, or ending anything. The task keeps whatever state it had, and the
    agent (or the humans) can nudge again later. Only blocked on a genuinely terminal task,
    where there is nothing left to wait for."""
    _assert_live(conn, identity.task_id)
    state = _state(conn, identity.task_id)
    service.post_message(conn, identity, "waiting", detail)
    _event(conn, identity.task_id, "waiting", {"text": detail})
    _notify(conn, identity.task_id, f"[{identity.name}] WAITING — needs a human: {detail}")
    conn.commit()
    return {"status": STATUS_WAITING, "state": state}


def _strikes(conn, task_id: str) -> int:
    return conn.execute("SELECT strikes FROM tasks WHERE id = ?", (task_id,)).fetchone()["strikes"]
