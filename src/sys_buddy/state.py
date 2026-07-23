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

TODOS (v2) fork this in exactly one place, and only for tasks that have them: a task
running on todos does not own its state — the state is a ROLLUP of its todos
(``todos.apply_rollup``), each of which marches through the SAME six states with its
own contract chain. So the functions below come in pairs: the task-level path, byte
for byte what it always was, and a ``_..._todo`` path that drives one deliverable and
then re-derives the parent. A task with no todos never touches the second half.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import config, contracts, service, slack, todos
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
# todo id (v2 design, settled). ``stuck`` is deliberately absent: it is valid at both
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
    """The host-chosen deployment target for this task, if they set one at setup."""
    try:
        row = conn.execute("SELECT staging_url FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row["staging_url"]:
        return None
    return str(row["staging_url"]).strip() or None


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


def _slack(conn, task_id: str, text: str) -> None:
    """Fire a best-effort Slack ping and record a ``slack`` event either way.

    The event is written regardless of whether a webhook is configured or the send
    succeeds, so the dashboard's event log shows that a human notification was
    triggered at this point. ``slack.notify`` never raises (SPEC §14)."""
    slack.notify(text)
    _event(conn, task_id, "slack", {"text": text})


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
    """``#1 (title), #2 (title)`` — for error messages that must be ACTIONABLE."""
    rows = todos.live_todos(conn, task_id)
    return ", ".join(f"#{r['id']} ({r['title']})" for r in rows) or "(none)"


def _roles(conn, task_id: str) -> list[str]:
    row = conn.execute("SELECT roles_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return json.loads(row["roles_json"])


def _todo_clause(todo_id: int | None) -> str:
    """``AND todo_id = ?`` when a deliverable is named.

    ``None`` means NO FILTER, deliberately not ``todo_id IS NULL``: the task-level
    callers below predate todos and must keep seeing the whole task's chain, whichever
    deliverable each version belongs to.
    """
    return " AND todo_id = ?" if todo_id is not None else ""


def _current_locked(conn, task_id: str, todo_id: int | None = None) -> dict | None:
    """The highest-version locked contract for the task, or None.

    'Current' is the *newest* locked version: a v2 replanning supersedes v1
    the moment v2 locks, even though v1's row stays 'locked' for the audit trail.
    ``todo_id`` narrows it to one deliverable's chain — whose versions are a
    non-contiguous slice of the task's single MAX+1 sequence (v1, v4, v7).
    """
    row = conn.execute(
        "SELECT id, version, spec_json, todo_id, locked_at FROM contracts "
        f"WHERE task_id = ? AND status = 'locked'{_todo_clause(todo_id)} "
        "ORDER BY version DESC LIMIT 1",
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
    """The highest-version contract row for the task regardless of status (draft or
    locked) — the one an agent is currently meant to act on. A freshly proposed v1
    (still 'draft') or a v2 replan-in-progress is returned here even though it hasn't
    locked; ``get_contract`` uses this so a proposal is REVIEWABLE before signing.
    ``todo_id`` narrows it to one deliverable's chain."""
    row = conn.execute(
        "SELECT id, version, spec_json, status, todo_id, locked_at FROM contracts "
        f"WHERE task_id = ?{_todo_clause(todo_id)} ORDER BY version DESC LIMIT 1",
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
    }


def _signatures_for(conn, contract_id: int) -> list[str]:
    """Roles that have signed a given contract row."""
    return [
        r["role"]
        for r in conn.execute(
            "SELECT a.role FROM contract_signatures s "
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
    """
    row = conn.execute(
        "SELECT a.role AS role FROM contracts c JOIN agents a ON a.id = c.proposed_by "
        "WHERE c.task_id = ? AND c.status = 'locked'"
        + (" AND c.todo_id = ?" if todo_id is not None else "")
        + " ORDER BY c.version DESC LIMIT 1",
        (task_id, todo_id) if todo_id is not None else (task_id,),
    ).fetchone()
    return row["role"] if row else None


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

    ``messages`` has no ``todo_id`` column, so this text prefix is what tells the peer
    (and the human thread) WHICH deliverable a deploy/test/verified belongs to.
    """
    return f"[todo #{row['id']} {row['title']}]"


# --------------------------------------------------------------------------- #
# contract lifecycle
# --------------------------------------------------------------------------- #
def _resolve_contract_todo(conn, identity: Identity, todo_id: int | None):
    """Resolve the deliverable a contract belongs to, or ``None`` for a task-level one.

    Once a task runs on todos the selector is REQUIRED. A task-level contract there
    would silently ask the WHOLE cast to sign work that binds two of them (the
    signatory set is the party list — see ``lock_contract``), and nothing could ever
    be reported against it, because ``report_status`` is todo-scoped on such a task
    too. So the ambiguity is removed by construction rather than by convention.

    Stage matters: agree on WHAT before HOW. A contract on a todo that not every party
    has accepted yet is a shape for work nobody signed up to.
    """
    if todo_id is None:
        if todos.has_todos(conn, identity.task_id):
            raise ValueError(
                f"task '{identity.task_id}' runs on todos, so a contract must name the "
                f"deliverable it shapes: propose_contract(spec, todo=<id>). Live todos: "
                f"{_live_todo_list(conn, identity.task_id)} — call get_todos() for their "
                f"scopes and party lists."
            )
        return None

    row = todos.get_row(conn, identity.task_id, todo_id)
    todos.assert_party(row, identity.role, "propose a contract on")
    status = todos.status_of(conn, row)
    if status == todos.DROPPED:
        raise ValueError(f"todo {row['id']} was dropped; there is nothing left to contract")
    if status == todos.VERIFIED:
        raise ValueError(
            f"todo {row['id']} is already verified — propose a NEW todo for follow-up "
            f"work rather than re-contracting finished work"
        )
    if status == todos.PENDING:
        d = todos.decisions(conn, row["id"], row["version"])
        awaiting = [
            p for p in todos.parties_of(row) if d.get(p, {}).get("decision") != todos.ACCEPT
        ]
        raise ValueError(
            f"todo {row['id']} '{row['title']}' is not accepted yet — agree on WHAT before "
            f"HOW. Waiting on {', '.join(awaiting)} to accept_todo({row['id']})."
        )
    return row


def propose_contract(conn, identity: Identity, spec: dict, todo_id: int | None = None) -> dict:
    """Validate and record a contract proposal, (re)opening planning.

    A proposal is valid from ``open`` or any later non-terminal state — a v2+
    proposal from, say, ``backend_live`` reopens planning and drops the task
    back to ``contract_proposed`` (SPEC §5 rule 1). Terminal tasks cannot be
    reopened without a human.

    ``todo_id`` scopes the contract to ONE deliverable (v2): the row records it, and
    the contract's signatory set becomes that todo's party list instead of the task's
    full cast. It is optional only for a task with no todos, and required for one that
    has them (see ``_resolve_contract_todo``). Version numbering is untouched — a
    single MAX+1 sequence per TASK — so one todo's chain reads v1, v4, v7.
    """
    todo = _resolve_contract_todo(conn, identity, todo_id)
    # The host may have nominated the deployment target at setup — the human owns it,
    # and the producer agent INHERITS it rather than inventing a URL that was never
    # deployed. Only fills a gap: an explicit staging_url in the proposal still wins.
    task_url = _task_staging_url(conn, identity.task_id)
    if task_url and not (isinstance(spec, dict) and str(spec.get("staging_url") or "").strip()):
        spec = {**spec, "staging_url": task_url} if isinstance(spec, dict) else spec

    # staging_url strictness is CONNECTIVITY-aware, not auth-mode-aware: a peer on
    # another machine needs a real https domain + the SSRF guard, while a task the
    # host declared same-machine (loopback origin, no public URL) is one person on one
    # box, where http://localhost:PORT is the correct target. The GUI always runs the
    # broker in remote mode for token auth, so is_remote alone cannot tell them apart.
    # See contracts._validate_staging_url.
    errors = contracts.validate_spec(
        spec,
        is_remote=config.get_config().is_remote,
        same_machine=_task_is_same_machine(conn, identity.task_id),
    )
    if errors:
        # Raise with joined errors so the agent gets every fix in one shot.
        raise ValueError("invalid contract:\n- " + "\n- ".join(errors))

    _assert_live(conn, identity.task_id)

    # Both parties must clear pre-flight before ANYONE can propose (owner rule): a
    # contract agreed with an agent that never proved it understands the protocol
    # is worthless. Remote-only — local self-declared identities don't run pre-flight
    # (the middleware readiness gate is remote-only too), so gating there would brick
    # the whole local contract flow.
    if config.get_config().is_remote:
        not_ready = conn.execute(
            "SELECT role FROM agents WHERE task_id = ? AND revoked_at IS NULL AND ready = 0 "
            "ORDER BY role",
            (identity.task_id,),
        ).fetchall()
        if not_ready:
            waiting = ", ".join(r["role"] for r in not_ready)
            raise ValueError(
                "all parties must pass pre-flight before a contract can be proposed; "
                f"waiting on: {waiting}"
            )

    spec_json = json.dumps(spec)
    service.assert_content_size(spec_json, "contract spec")

    # Version is MAX+1; two concurrent proposals can compute the same value and
    # collide on UNIQUE(task_id, version). Retry on that collision (re-reading MAX)
    # so a racing proposer gets a clean higher version instead of an uncaught 500.
    for _attempt in range(6):
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM contracts WHERE task_id = ?",
            (identity.task_id,),
        ).fetchone()
        version = row["v"] + 1
        try:
            conn.execute(
                "INSERT INTO contracts (task_id, version, spec_json, status, proposed_by, "
                "todo_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (identity.task_id, version, spec_json, "draft", identity.agent_id,
                 (todo["id"] if todo is not None else None), _now()),
            )
            break
        except sqlite3.IntegrityError:
            conn.rollback()
    else:
        raise ValueError("could not allocate a contract version — please retry")

    if todo is None:
        state = _transition(conn, identity.task_id, CONTRACT_PROPOSED)
    else:
        # Rule 1 one level down: a v2 proposal on a todo that is already built reopens
        # THAT deliverable's planning, and only that one — the other five keep marching.
        _todo_march(conn, identity.task_id, todo["id"], todo["state"], CONTRACT_PROPOSED)
        state = todos.apply_rollup(conn, identity.task_id)
    conn.commit()
    # Tell the peer directly — a transition event alone is dashboard-only and would
    # never reach the other agent's wait_for_message queue. This is what makes
    # planning actually flow: the peer hears "there's a proposal to assess."
    n_endpoints = len(spec.get("endpoints", []))
    scope_note = "" if todo is None else f" on todo #{todo['id']} ({todo['title']})"
    signers = (
        "every role" if todo is None
        else f"the parties on this todo ({', '.join(todos.parties_of(todo))})"
    )
    service.post_message(
        conn,
        identity,
        "contract_proposal",
        f"Proposed contract v{version}{scope_note} ({n_endpoints} endpoint"
        f"{'' if n_endpoints == 1 else 's'}). Review the shape with get_contract (it now "
        f"shows the proposed contract, not only locked ones), then lock_contract({version}) "
        f"to sign — or message me to request changes first. The staging_url appears in "
        f"get_contract once {signers} {'has' if todo is None else 'have'} signed.",
    )
    if todo is None:
        return {"version": version, "state": state}
    return {
        "version": version,
        "state": state,
        "todo_id": todo["id"],
        "todo_state": CONTRACT_PROPOSED,
        "signatories": todos.parties_of(todo),
    }


def lock_contract(conn, identity: Identity, version: int, todo_id: int | None = None) -> dict:
    """Record this agent's signature on a contract version; lock only when every
    required signatory has signed (SPEC §5 rule 2, §6).

    Who is required depends on what the contract is ABOUT, and this is the hinge of
    the todos feature:

    * a TASK-level contract (``contracts.todo_id IS NULL`` — every contract that
      existed before todos, and every one on a task that never grows a todo) needs
      *all of them*, per ``tasks.roles_json``;
    * a TODO-scoped contract needs exactly that todo's ``parties_json``. A task seat
      the todo does not bind neither blocks the lock nor may sign it — SEATS ≠
      PARTICIPANTS. That is why todos need no separate quorum mechanism: it is the
      same "all must sign" rule over a smaller "all".

    A locked contract is immutable (rule 6): re-signing or re-locking it is rejected,
    and changes must go through a fresh version that every signatory re-signs.
    ``todo_id`` is optional — the version already names exactly one row — but when
    passed it is CHECKED against the contract, so an agent that means to sign the
    payments todo cannot mis-sign the refunds one.
    """
    _assert_live(conn, identity.task_id)

    contract = conn.execute(
        "SELECT id, status, todo_id FROM contracts WHERE task_id = ? AND version = ?",
        (identity.task_id, version),
    ).fetchone()
    if contract is None:
        raise ValueError(
            f"no contract version {version} on task '{identity.task_id}'"
        )
    if contract["status"] == "locked":
        raise ValueError(
            f"contract version {version} is already locked and immutable; "
            f"propose a new version to change it"
        )

    todo = None
    if contract["todo_id"] is not None:
        todo = todos.get_row(conn, identity.task_id, contract["todo_id"])
        if todo_id is not None and int(todo_id) != todo["id"]:
            raise ValueError(
                f"contract v{version} belongs to todo {todo['id']} ('{todo['title']}'), "
                f"not todo {int(todo_id)} — check get_todos() before signing"
            )
        if todo["dropped_at"] is not None:
            raise ValueError(
                f"todo {todo['id']} was dropped; its contract v{version} cannot be signed"
            )
        # A non-party has no say in a shape that does not bind it — the mirror image of
        # "a non-party does not block the lock".
        todos.assert_party(todo, identity.role, "sign a contract on")
    elif todo_id is not None:
        raise ValueError(
            f"contract v{version} is a TASK-level contract (it binds the whole cast), "
            f"not one scoped to todo {int(todo_id)} — sign it with lock_contract({version})"
        )

    # Record this signature (idempotent — signing twice is a no-op, not an error).
    conn.execute(
        "INSERT OR IGNORE INTO contract_signatures (contract_id, agent_id, signed_at) "
        "VALUES (?,?,?)",
        (contract["id"], identity.agent_id, _now()),
    )

    required = _roles(conn, identity.task_id) if todo is None else todos.parties_of(todo)
    signed = [
        r["role"]
        for r in conn.execute(
            "SELECT a.role FROM contract_signatures s "
            "JOIN agents a ON a.id = s.agent_id WHERE s.contract_id = ?",
            (contract["id"],),
        ).fetchall()
    ]
    signed_set = set(signed)
    remaining = [r for r in required if r not in signed_set]

    scope_note = "" if todo is None else f" on todo #{todo['id']} ({todo['title']})"

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
        )
        out = {
            "locked": False,
            "version": version,
            "signed": sorted(signed_set),
            "remaining": remaining,
        }
        if todo is not None:
            out["todo_id"] = todo["id"]
        return out

    # All roles have signed → the contract locks and the task advances. The UPDATE
    # is conditional on status='draft' so that if two roles sign the final signature
    # concurrently and both observe "all signed", exactly one wins the lock — the
    # loser's rowcount is 0 and it returns the locked result WITHOUT a duplicate lock
    # event or a second human Slack ping.
    cur = conn.execute(
        "UPDATE contracts SET status = 'locked', locked_at = ? WHERE id = ? AND status = 'draft'",
        (_now(), contract["id"]),
    )
    if cur.rowcount != 1:
        conn.commit()
        return {"locked": True, "version": version, "signed": sorted(signed_set)}
    if todo is None:
        state = _transition(conn, identity.task_id, CONTRACT_LOCKED)
        lock_detail = {"version": version, "signed": sorted(signed_set)}
    else:
        _todo_march(conn, identity.task_id, todo["id"], todo["state"], CONTRACT_LOCKED)
        # A newly locked version is a genuine new ATTEMPT, so the ping-pong counter
        # starts over (SPEC §8). The task-level path has to derive that from event
        # timestamps (D3) because it cannot see the lock from _report_deployed; here we
        # ARE the lock, so we just reset it — and no check can slip in between, since
        # checks are refused until the todo reaches backend_live.
        conn.execute("UPDATE todos SET strikes = 0 WHERE id = ?", (todo["id"],))
        _clear_todo_stuck(conn, todo["id"])
        state = todos.apply_rollup(conn, identity.task_id)
        lock_detail = {"version": version, "signed": sorted(signed_set), "todo_id": todo["id"]}
    _event(conn, identity.task_id, "lock", lock_detail)
    _slack(
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
        f"the frozen shape and the staging_url from get_contract"
        + ("." if todo is None else f"(todo={todo['id']}).")
        + (
            "" if todo is None
            else f" Report progress on this deliverable with report_status(..., todo={todo['id']})."
        ),
    )
    out = {"locked": True, "version": version, "signed": sorted(signed_set), "state": state}
    if todo is not None:
        out["todo_id"] = todo["id"]
        out["todo_state"] = CONTRACT_LOCKED
    return out


def reopen_negotiations(
    conn, identity: Identity, reason: str, todo_id: int | None = None
) -> dict:
    """Drop a locked-or-later task back to ``contract_proposed`` (planning) so a
    new contract version can be proposed and re-signed.

    Non-destructive: the currently-locked contract keeps serving via ``get_contract``
    until a NEW version locks — nothing is deleted. Either party may call it (the
    "agreement" happens in chat first; a one-sided reopen is harmless — the peer just
    won't propose/sign anything new). Rejected on terminal tasks, and on tasks that
    haven't locked a contract yet (there's nothing to re-plan — just keep talking
    or propose a first version).

    On a task with todos it reopens ONE deliverable (``todo_id`` is then required, for
    the same reason a proposal needs it — "reopen planning" on six chains is not a
    thing), and only that todo goes back to planning; the other five keep marching.
    This is also the sanctioned route to change a todo whose contract already LOCKED:
    reopen → propose a new version → every party re-signs (``repropose_todo`` refuses
    once a lock exists, because a locked contract is immutable).
    """
    current = _assert_live(conn, identity.task_id)
    if todo_id is None and todos.has_todos(conn, identity.task_id):
        raise ValueError(
            f"task '{identity.task_id}' runs on todos, so name the deliverable to "
            f"replan: reopen_negotiations(reason, todo=<id>). Live todos: "
            f"{_live_todo_list(conn, identity.task_id)}."
        )

    if todo_id is not None:
        return _reopen_todo(conn, identity, reason, todo_id)

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


def _reopen_todo(conn, identity: Identity, reason: str, todo_id: int) -> dict:
    """Reopen planning on ONE deliverable. Same rules, one level down."""
    row = todos.get_row(conn, identity.task_id, todo_id)
    if row["dropped_at"] is not None:
        raise ValueError(f"todo {row['id']} was dropped; there is nothing to replan")
    todos.assert_party(row, identity.role, "reopen planning on")
    if row["state"] == VERIFIED:
        raise ValueError(
            f"todo {row['id']} is verified — replanning finished work would make the "
            f"task's rollup lie. Propose a NEW todo for follow-up work."
        )
    if _current_locked(conn, identity.task_id, todo_id=row["id"]) is None:
        raise ValueError(
            f"nothing to reopen on todo {row['id']} — no contract has locked on it yet. "
            f"Keep planning, or propose a first version with "
            f"propose_contract(spec, todo={row['id']})."
        )
    if row["state"] in (OPEN, CONTRACT_PROPOSED):
        raise ValueError(
            f"todo {row['id']} is already in planning (state '{row['state']}') — "
            f"nothing to reopen"
        )

    detail = (reason or "").strip() or "(no reason given)"
    service.assert_content_size(detail, "reopen reason")
    _todo_march(conn, identity.task_id, row["id"], row["state"], CONTRACT_PROPOSED)
    _event(
        conn, identity.task_id, "reopen",
        {"from": row["state"], "reason": detail, "todo_id": row["id"]},
    )
    state = todos.apply_rollup(conn, identity.task_id)
    conn.commit()
    service.post_message(
        conn,
        identity,
        "renegotiation",
        f"Reopened planning on todo #{row['id']} ({row['title']}), was {row['state']}: "
        f"{detail}. Its last locked contract still stands until a new version is proposed "
        f"with propose_contract(spec, todo={row['id']}) and re-signed by "
        f"{', '.join(todos.parties_of(row))}.",
    )
    return {
        "state": state,
        "from": row["state"],
        "reason": detail,
        "todo_id": row["id"],
        "todo_state": CONTRACT_PROPOSED,
    }


def get_contract(conn, task_id: str, todo_id: int | None = None) -> dict:
    """Return the current contract for the task — PROPOSED or LOCKED.

    This is the single source of truth for the interface, at both stages:

    * LOCKED — the full frozen contract, including the ``staging_url`` read from the
      stored ``spec_json`` (NEVER from a chat message; SPEC §5 rule 5, §9). This is
      the trusted source of the staging URL for a consumer: an injected "test against
      evil.com" message has no path into this value.
    * PROPOSED (not yet locked) — the proposed SHAPE, so an assessor can review it
      before signing, WITH the ``staging_url`` WITHHELD (``None``): an unsigned URL
      must not be fetchable (rule 2), so it only appears once every role has signed.
      Also returns who has ``signatures`` / who is ``awaiting``.

    Returning proposals here (not just locked ones) removes the old chicken-and-egg
    where an assessor was told to review via get_contract but saw exists:false until
    it locked — which it can't do until they sign.

    ``todo_id`` scopes it to ONE deliverable's chain. Without it on a task that has
    todos you get the task's newest contract whichever todo it belongs to (unchanged
    behaviour, and what the dashboard reads) — plus a ``todo_id`` field and a note
    saying which deliverable that is, because "the current contract" is genuinely
    ambiguous once there are six.
    """
    todo = None if todo_id is None else todos.get_row(conn, task_id, todo_id)
    newest = _newest_contract(conn, task_id, todo_id=(todo["id"] if todo is not None else None))
    if newest is None:
        if todo is None:
            return {"exists": False}
        return {
            "exists": False,
            "todo_id": todo["id"],
            "note": (
                f"No contract has been proposed on todo {todo['id']} ('{todo['title']}') "
                f"yet. Agree on WHAT first (every party accepts the todo), then one party "
                f"proposes the HOW with propose_contract(spec, todo={todo['id']})."
            ),
        }

    spec = newest["spec"]
    signed = sorted(_signatures_for(conn, newest["id"]))
    # The signatory set is the CONTRACT's, not the caller's request: a todo-scoped
    # contract is signed by its party list even when the caller asked task-wide, so
    # `awaiting` must never name a seat the todo does not bind.
    scoped = None if newest["todo_id"] is None else todos.get_row(conn, task_id, newest["todo_id"])
    todo_block = (
        {}
        if scoped is None
        else {
            "todo_id": scoped["id"],
            "todo_title": scoped["title"],
            "signatories": todos.parties_of(scoped),
        }
    )

    if newest["status"] == "locked":
        return {
            "exists": True,
            "version": newest["version"],
            "status": "locked",
            "locked": True,
            "staging_url": spec.get("staging_url"),
            "spec": spec,
            "signatures": signed,
            "locked_at": newest["locked_at"],
            **todo_block,
        }

    # Proposed / draft: expose the shape but strip staging_url until it locks.
    required = _roles(conn, task_id) if scoped is None else todos.parties_of(scoped)
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
            "Proposed, not yet locked — this is the shape to review. Call "
            f"lock_contract({newest['version']}) to sign it; the staging_url appears "
            "here only after "
            + ("ALL roles" if scoped is None else f"all of {', '.join(required)}")
            + " have signed."
        ),
        **todo_block,
    }
    # A v2 replan can be in flight while an older v1 is still the locked, serving
    # contract — point the assessor at it so they know what's currently in force.
    # Scoped to the same chain: the version in force on THIS deliverable, never a
    # sibling todo's lock, which would send the assessor to the wrong blueprint.
    locked = _current_locked(
        conn, task_id, todo_id=(scoped["id"] if scoped is not None else None)
    )
    if locked is not None and locked["version"] != newest["version"]:
        out["locked_version_in_force"] = locked["version"]
    return out


# --------------------------------------------------------------------------- #
# status reporting — the transitions, the strikes, the typed messages
# --------------------------------------------------------------------------- #
def report_status(
    conn, identity: Identity, status: str, detail: str, todo_id: int | None = None
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
    todo-scoped and ``todo_id`` is REQUIRED — the ambiguity is removed by construction,
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

    # Mode gate: debug tasks have a single 'resolved' status; contract tasks have
    # the full deploy/test/verified vocabulary. Keep the two vocabularies disjoint.
    mode = _task_mode(conn, identity.task_id)
    if mode == "debug" and todo_id is not None:
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

    if todo_id is not None:
        return _report_on_todo(conn, identity, status, detail, todo_id, requested)
    if status in TODO_SCOPED_STATUSES and todos.has_todos(conn, identity.task_id):
        raise ValueError(
            f"'{requested}' is per-DELIVERABLE on task '{identity.task_id}', which runs on "
            f"todos — '{requested}' on which one? Pass the todo id: "
            f"report_status('{requested}', detail, todo=<id>). Live todos: "
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
    conn, identity: Identity, status: str, detail: str, todo_id: int, requested: str
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
    row = todos.get_row(conn, identity.task_id, todo_id)
    if row["dropped_at"] is not None:
        raise ValueError(
            f"todo {row['id']} was dropped; there is nothing left to report on it"
        )
    todos.assert_party(row, identity.role, f"report '{requested}' on")
    # ``verified`` IS terminal for a todo, even though it no longer is for the task: any
    # further report would march a finished deliverable backwards, and the task's
    # "N of 6 verified" rollup — and its own conclusion — would become a lie. Follow-up
    # work on something already verified is a NEW todo.
    if row["state"] == VERIFIED:
        raise ValueError(
            f"todo {row['id']} ('{row['title']}') is verified — nothing more is reported "
            f"on it. Propose a NEW todo for follow-up work, or escalate the whole task "
            f"with report_status('stuck', detail) if it needs humans."
        )

    if status == STATUS_STUCK:
        return _report_todo_stuck(conn, identity, row, detail)
    if row["strikes"] >= MAX_STRIKES:
        raise ValueError(
            f"todo {row['id']} ('{row['title']}') reached {MAX_STRIKES} fix cycles — the "
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
    fresh = todos.get_row(conn, identity.task_id, row["id"])
    return {
        "status": status,
        "todo_id": fresh["id"],
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
            f"cannot report 'ready' on todo {row['id']} while contract v{newest['version']} "
            f"is awaiting signatures; lock that version first"
        )
    if _current_locked(conn, identity.task_id, todo_id=row["id"]) is None:
        raise ValueError(
            f"cannot report 'ready' on todo {row['id']}: no locked contract exists on it "
            f"yet. Agree the shape with propose_contract(spec, todo={row['id']}) and have "
            f"every party sign it."
        )
    producer = _producer_role(conn, identity.task_id, todo_id=row["id"])
    if identity.role != producer:
        raise ValueError(
            f"only the role that proposed the contract on todo {row['id']} may report "
            f"'ready' for it (the producer there is '{producer}', you are "
            f"'{identity.role}')"
        )

    _clear_todo_stuck(conn, row["id"])  # a deliverable that is moving again is not stuck
    _todo_march(conn, identity.task_id, row["id"], row["state"], BACKEND_LIVE)
    _event(conn, identity.task_id, "deploy", {"text": detail, "todo_id": row["id"]})
    service.post_message(
        conn, identity, "deploy_confirmed", f"{_todo_label(row)} {detail}"
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
            f"cannot report a check on todo {row['id']} before its producer is ready "
            f"(the todo is '{row['state']}', need '{BACKEND_LIVE}')"
        )
    producer = _producer_role(conn, identity.task_id, todo_id=row["id"])
    if identity.role == producer:
        raise ValueError(
            f"the producer ('{producer}') doesn't report checks on its own work; the "
            f"consuming party/parties on todo {row['id']} do"
        )

    # First check after a deploy moves this deliverable into its testing phase.
    _todo_march(conn, identity.task_id, row["id"], row["state"], TESTING)

    if status == STATUS_TEST_PASSED:
        _event(conn, identity.task_id, "test",
               {"pass": True, "strike": None, "todo_id": row["id"]})
        service.post_message(conn, identity, "test_result", f"{_todo_label(row)} {detail}")
        return _todo_result(conn, identity, row, status)

    # blocked → the broker (not the agent) counts the strike, on the todo.
    conn.execute("UPDATE todos SET strikes = strikes + 1 WHERE id = ?", (row["id"],))
    strikes = todos.get_row(conn, identity.task_id, row["id"])["strikes"]
    _event(conn, identity.task_id, "test",
           {"pass": False, "strike": strikes, "todo_id": row["id"]})
    service.post_message(conn, identity, "test_result", f"{_todo_label(row)} {detail}")

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
        )
        _slack(
            conn, identity.task_id,
            f"[{identity.task_id}] STUCK on todo #{row['id']} ({row['title']}): "
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
            f"cannot report 'verified' on todo {row['id']} before checks have run on it "
            f"(the todo is '{row['state']}', need '{TESTING}'); a consuming party must "
            f"report a check first"
        )
    _todo_march(conn, identity.task_id, row["id"], row["state"], VERIFIED)
    conn.execute(
        "UPDATE todos SET verified_at = ?, stuck_at = NULL, stuck_reason = NULL WHERE id = ?",
        (_now(), row["id"]),
    )
    service.post_message(conn, identity, "verified", f"{_todo_label(row)} {detail}")
    out = _todo_result(conn, identity, row, STATUS_VERIFIED)
    roll = out["rollup"] or {}
    if roll.get("complete"):
        _slack(
            conn, identity.task_id,
            f"[{identity.name}] VERIFIED: every todo on '{identity.task_id}' is done "
            f"({roll.get('total')}/{roll.get('total')}). Last one: {row['title']} — {detail}",
        )
    else:
        _slack(
            conn, identity.task_id,
            f"[{identity.name}] todo #{row['id']} ({row['title']}) VERIFIED "
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
    service.post_message(conn, identity, "stuck", f"{_todo_label(row)} {detail}")
    _slack(
        conn, identity.task_id,
        f"[{identity.task_id}] STUCK on todo #{row['id']} ({row['title']}): {detail}",
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
        _slack(
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
            f"need '{TESTING}'); a client must report a test result first"
        )
    state = _transition(conn, identity.task_id, VERIFIED)
    service.post_message(conn, identity, "verified", detail)
    _slack(conn, identity.task_id, f"[{identity.name}] VERIFIED: {detail}")
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
    _slack(conn, identity.task_id, f"[{identity.name}] RESOLVED: {detail}")
    conn.commit()
    return {"status": STATUS_RESOLVED, "state": state}


def _report_stuck(conn, identity: Identity, detail: str) -> dict:
    """Give up and pull in the humans → terminal ``stuck``. Valid from any
    non-terminal state (SPEC §7).

    This is the WHOLE-collaboration escalation ("my token expired", "I don't understand
    the goal") and stays available on a task with todos, where it outranks the rollup:
    ``todos.apply_rollup`` refuses to overwrite a human-escalated stuck, so one call
    here freezes every deliverable until a human reopens it. To flag a single
    deliverable instead, pass a todo id — see ``_report_todo_stuck``."""
    _assert_live(conn, identity.task_id)
    state = _transition(conn, identity.task_id, STUCK)
    service.post_message(conn, identity, "stuck", detail)
    _slack(conn, identity.task_id, f"[{identity.task_id}] STUCK: {detail}")
    conn.commit()
    return {"status": STATUS_STUCK, "state": state}


def _strikes(conn, task_id: str) -> int:
    return conn.execute("SELECT strikes FROM tasks WHERE id = ?", (task_id,)).fetchone()["strikes"]
