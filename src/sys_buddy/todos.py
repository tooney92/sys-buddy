"""Todos — several deliverables under one task, each with its own contract.

A real piece of work is usually N things ("six todos, a contract on each"). Before
this module a task carried ONE contract chain and ``verified`` was terminal, so
verifying item one ended the whole task and six deliverables meant six tasks — six
pairings, six seats, six threads, and no parent to conclude.

Three decisions carry the whole feature:

**SEATS ≠ PARTICIPANTS.** A todo reuses the TASK's seats — you pair once, at the
task — and names WHICH of them it binds (``parties``). Backend names
``[backend, mobile]``; frontend is seated on the task, may READ the todo, is not
bound by it, is not in its contract's quorum, and does not block it. Let todos
inherit the full cast instead and nothing is fixed.

**Two-stage agreement.** Agree on WHAT (the todo is accepted by every named party),
then on HOW (its contract is locked by those same parties). Today's single contract
stage carries both and is overloaded for it. The contract's signatory set comes
STRAIGHT from the party list, which is why quorum needs no separate mechanism — it
is the same "all must sign" rule over a smaller "all".

**No AGENT may remove a peer.** You joined by accepting; you leave by your own call
(:func:`leave_todo`, which has no seat argument at all, so naming somebody else is
unspellable rather than merely refused). If backend could remove mobile, then the
moment mobile objects to a shape backend removes it and locks without the dissent —
"both sides sign" quietly becomes "whoever proposes wins".

The real problem is ABSENCE, not dissent, and neither a mutual drop nor a self-removal
can answer it: both need a call from the very party who is missing. So absence has a
HUMAN escape hatch, in two sizes, reachable from the CLI/GUI and never from a peer's
tool — :func:`host_drop_todo` abandons the whole deliverable, and
:func:`host_remove_party` removes only the silent party and lets the rest carry on.
Both are recorded (``todo_departures``, plus an event and a broker-authored message):
a party that vanishes with no trace is worse than one that never left.

**TWO IDENTIFIERS, and they are not interchangeable.** ``todos.id`` is a global
AUTOINCREMENT and the target of every foreign key in the schema; ``todos.number`` is
the ``#N`` a person types, allocated per TASK from 1. This is the GitHub pattern, and
it is not cosmetic: with a global id, three tasks holding one todo each read ``#1``,
``#2``, ``#3``, so on the third task the ONLY deliverable is "#3" — a number nobody
can guess and nobody can type. So :func:`get_row` resolves what a human or an agent
supplied (a NUMBER, scoped to the task) and :func:`row_by_id` resolves what a join
handed us (an id). Numbers are never reused and never renumbered: ``#2`` is quoted in
the thread, in the event log, and in whatever the two humans said yesterday.

Backwards compatibility is load-bearing: a task with NO todos never enters this
module, keeps today's single contract chain, its agent-driven state machine, and a
terminal ``verified``. Todos are additive; the migration story is "do nothing".
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import deliverables, service
from .identity import Identity

# --- the DERIVED agreement stage (todos.status) ------------------------------
# ONE tool with status as a FIELD, never separate per-stage tools: splitting
# visibility by stage is exactly the bug the get_contract fix removed.
PENDING = "pending"        # proposed; at least one named party has not accepted
ACCEPTED = "accepted"      # every named party said yes to WHAT — NOT a lock
CONTRACTED = "contracted"  # a contract exists on it (proposed or locked) — the HOW
VERIFIED = "verified"      # this deliverable is done
DROPPED = "dropped"        # abandoned by mutual consent, or by the host

STATUSES = (PENDING, ACCEPTED, CONTRACTED, VERIFIED, DROPPED)

# ISSUES — the same five stages MINUS the contract, spoken in debug's vocabulary. An
# issue on a debug task is a todo with the contract half removed, so `contracted` cannot
# occur and the finish line is called `resolved` rather than `verified`: "the bug is
# resolved" is the word the humans and the task state already use in that mode.
RESOLVED = "resolved"      # every named party said `fixed` — this issue is done
ISSUE_STATUSES = (PENDING, ACCEPTED, RESOLVED, DROPPED)
# Every status either vocabulary can produce — what `rollup` keys its counts by, so a
# debug task's `resolved` issue does not blow up a dict built from contract words alone.
ALL_STATUSES = (PENDING, ACCEPTED, CONTRACTED, VERIFIED, RESOLVED, DROPPED)

ACCEPT = "accepted"
DECLINE = "declined"

HOST = "host"  # the `dropped_by` / `acted_by` value for a unilateral host action

# --- how a party stopped being a party (todo_departures.mode) ----------------
# Two words for the same missing name, and they are not interchangeable: LEFT is a
# decision ("we don't need mobile after all", from mobile), REMOVED is an outage
# ("mobile's agent cannot call anything", from a human). Every surface prints which,
# because the reader's next move differs — you message someone who left, and you go
# and find someone who was removed.
LEFT = "left"        # the seat removed ITSELF, via leave_todo
REMOVED = "removed"  # the HOST removed it, via `sys-buddy todo drop-party`
DEPARTURE_MODES = (LEFT, REMOVED)

# --- the per-todo march ------------------------------------------------------
# Deliberately the SAME vocabulary as the task state machine: the UI's mini-stepper
# is then the familiar widget one level down, and nobody learns a second idiom.
# `stuck` is NOT here — it is an orthogonal flag (todos.stuck_at), because a stuck
# deliverable must not brick the task the way task-level `stuck` does, and the
# rollup still needs to know how far the todo actually got.
_MARCH_RANK = {
    "open": 0,
    "contract_proposed": 1,
    "contract_locked": 2,
    "backend_live": 3,
    "testing": 4,
    "verified": 5,
}

MAX_TITLE = 200


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def task_mode(conn, task_id: str) -> str:
    """The task's workflow mode — ``'contract'`` (default), ``'debug'`` or
    ``'engagement'``.

    THE mode read for this module, and it is always a read: ``tasks.mode`` is the only
    source of truth for whether a row here is a TODO or an ISSUE. No function in the
    issue flow takes a ``debug=`` argument, because a parameter would be a second
    statement of the same fact and the two can disagree — the exact class of bug that
    made engagement mode shippable-but-uncreatable when three surfaces each spelled the
    mode list themselves.
    """
    row = conn.execute("SELECT mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["mode"] is None:
        return "contract"
    return row["mode"]


def is_debug(conn, task_id: str) -> bool:
    """Does this task carry ISSUES rather than todos? Derived from ``tasks.mode``."""
    return task_mode(conn, task_id) == "debug"


def noun(conn, task_id: str) -> str:
    """``'issue'`` on a debug task, ``'todo'`` everywhere else.

    One word, read from the mode, used by every message and refusal this module writes —
    so an agent that was taught "issue" is never told off in a vocabulary its human does
    not use.
    """
    return "issue" if is_debug(conn, task_id) else "todo"


def task_roles(conn, task_id: str) -> list[str]:
    row = conn.execute("SELECT roles_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    return json.loads(row["roles_json"])


def parties_of(row) -> list[str]:
    return json.loads(row["parties_json"])


def _row_get(row, key: str, default=None):
    """``row[key]`` that tolerates the column being absent.

    ``sqlite3.Row`` raises ``IndexError`` for a key it does not have, and a row can
    legitimately lack a young column: a caller holding a row read before this process
    ran the migration, or a test constructing one by hand. Every read of a column added
    by ALTER goes through here so a stale row degrades to ``None`` instead of a crash.
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _rows(conn, task_id: str, *, live_only: bool = False) -> list:
    sql = "SELECT * FROM todos WHERE task_id = ?"
    if live_only:
        sql += " AND dropped_at IS NULL"
    return conn.execute(sql + " ORDER BY id", (task_id,)).fetchall()


def live_todos(conn, task_id: str) -> list:
    """Todos that still count — everything not dropped."""
    return _rows(conn, task_id, live_only=True)


def has_todos(conn, task_id: str) -> bool:
    """Does this task run on todos?

    The single switch that keeps every pre-todo task byte-identical: false here and
    `propose_contract`/`report_status`/the task state machine take exactly the path
    they took before this module existed. A task whose todos were ALL dropped reads
    as false again — there is genuinely nothing left to roll up.
    """
    return (
        conn.execute(
            "SELECT 1 FROM todos WHERE task_id = ? AND dropped_at IS NULL LIMIT 1",
            (task_id,),
        ).fetchone()
        is not None
    )


def get_row(conn, task_id: str, number: int) -> dict:
    """One todo by the HUMAN's ``#N``, SCOPED to ``task_id``.

    THE resolver for anything a person or an agent typed. ``#N`` is
    ``todos.number`` — per-task, starting at 1 — and never ``todos.id``, which is a
    global AUTOINCREMENT: on a broker with three tasks the third task's only
    deliverable would otherwise be ``#3``, which nobody can guess and nobody can type.

    Scoping is what makes the number safe: task A's ``#1`` and task B's ``#1`` are
    different rows and a caller can never reach across tasks.

    Internal callers that already hold a ``todos.id`` (from ``contracts.todo_id``,
    ``messages.todo_id``, or a row they just wrote) must use :func:`row_by_id`.
    """
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise ValueError(f"todo number must be a number, got {number!r}") from None
    row = conn.execute(
        "SELECT * FROM todos WHERE number = ? AND task_id = ?", (number, task_id)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"no todo #{number} on task '{task_id}'. Todos are numbered PER TASK from "
            f"#1 — call get_todos() for the live list."
        )
    return row


def row_by_id(conn, task_id: str, todo_id: int) -> dict:
    """One todo by its INTERNAL ``todos.id``, still scoped to ``task_id``.

    The other half of the split: every foreign key in the schema
    (``contracts.todo_id``, ``messages.todo_id``, ``todo_decisions.todo_id``,
    ``todo_drop_consents.todo_id``) points at ``todos.id``, so code walking those joins
    must resolve on the id. Nothing a human types ever arrives here — that is
    :func:`get_row`.
    """
    row = conn.execute(
        "SELECT * FROM todos WHERE id = ? AND task_id = ?", (int(todo_id), task_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"no todo with id {todo_id} on task '{task_id}'")
    return row


def next_number(conn, task_id: str) -> int:
    """The number the next todo on this task gets: ``MAX(number) + 1``.

    MAX and not COUNT, deliberately: numbers are NEVER REUSED. Drop #2 and the next
    todo is #3, because ``#2`` is already quoted in the thread, in the event log and in
    whatever the two humans said to each other about it. Must be read inside the same
    transaction as the INSERT that consumes it; the ``todos_task_number`` unique index
    is the backstop if two proposals race.
    """
    return conn.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM todos WHERE task_id = ?", (task_id,)
    ).fetchone()["n"]


def decisions(conn, todo_id: int, version: int) -> dict[str, dict]:
    """``{role: {decision, reason, at}}`` for one VERSION of a todo."""
    return {
        r["role"]: {"decision": r["decision"], "reason": r["reason"], "at": r["created_at"]}
        for r in conn.execute(
            "SELECT role, decision, reason, created_at FROM todo_decisions "
            "WHERE todo_id = ? AND version = ?",
            (todo_id, version),
        ).fetchall()
    }


def fixes(conn, todo_id: int, version: int) -> dict[str, dict]:
    """``{role: {detail, at}}`` — who has said this ISSUE is fixed, for one VERSION.

    The debug counterpart of ``contract_signatures``, and deliberately the same shape of
    rule: an issue resolves only when EVERY named party has said so, over the same "all"
    (the party list). Partial is a normal, expected state — one party fixing something the
    other has not yet confirmed is most of the life of a bug — never an error.
    """
    return {
        r["role"]: {"detail": r["detail"], "at": r["created_at"]}
        for r in conn.execute(
            "SELECT role, detail, created_at FROM todo_fixes WHERE todo_id = ? AND version = ?",
            (todo_id, version),
        ).fetchall()
    }


def all_fixed(conn, row) -> bool:
    """Has every named party said this issue is fixed? The all-must-agree rule itself."""
    f = fixes(conn, row["id"], row["version"])
    parties = parties_of(row)
    return bool(parties) and all(p in f for p in parties)


def awaiting_fix(conn, row) -> list[str]:
    """The parties who have NOT said `fixed` yet, in party order."""
    f = fixes(conn, row["id"], row["version"])
    return [p for p in parties_of(row) if p not in f]


def record_fix(conn, todo_id: int, version: int, identity: Identity, detail: str | None) -> None:
    """Stamp ONE party's "this is fixed" on one version of an issue.

    An upsert, like :func:`_record`: saying it twice is the same statement, not a second
    one, and re-saying it must not be a way to resolve an issue on one seat's word.
    """
    conn.execute(
        "INSERT INTO todo_fixes (todo_id, version, role, agent_id, detail, created_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(todo_id, version, role) DO UPDATE SET "
        "detail = excluded.detail, agent_id = excluded.agent_id, "
        "created_at = excluded.created_at",
        (todo_id, version, identity.role, identity.agent_id, detail, _now()),
    )


def departures(conn, todo_id: int) -> list[dict]:
    """Every party that has STOPPED being a party on this todo, oldest first.

    The counterpart to ``parties_json``: that says who is bound now, this says who used
    to be and why they aren't. Version-INDEPENDENT, unlike acceptances and fixes — you
    left the deliverable, not a revision of it — and append-only, because a party can be
    named again by a ``repropose_todo`` and leave a second time for a second reason.

    Read by every surface that shows a party list, and by ``get_contract``: a locked
    contract keeps a signature from a signatory who has since left, and displaying that
    signature with nothing beside it would be the silent case this table exists to
    prevent.
    """
    return [
        {
            "seat": r["role"],
            "mode": r["mode"],
            "by": r["acted_by"],
            "reason": r["reason"],
            "version": r["version"],
            "at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT role, mode, acted_by, reason, version, created_at "
            "FROM todo_departures WHERE todo_id = ? ORDER BY id",
            (todo_id,),
        ).fetchall()
    ]


def departed_seats(conn, row, among: list[str] | None = None) -> list[str]:
    """The seats that left and are NOT parties again, in departure order.

    A seat that left and was later named by a ``repropose_todo`` is bound again and must
    not read as gone, so the CURRENT party list wins over the history — the log keeps
    both facts, this answers only "who is missing right now".

    ``among`` narrows it to a set the caller cares about: ``get_contract`` passes the
    signatures on a contract, so it asks "which of the people who signed this are no
    longer bound by it?" rather than "who has ever left this todo?".
    """
    current = set(parties_of(row))
    out: list[str] = []
    for d in departures(conn, row["id"]):
        seat = d["seat"]
        if seat in current or seat in out:
            continue
        if among is not None and seat not in among:
            continue
        out.append(seat)
    return out


def drop_consents(conn, todo_id: int) -> dict[str, str | None]:
    return {
        r["role"]: r["reason"]
        for r in conn.execute(
            "SELECT role, reason FROM todo_drop_consents WHERE todo_id = ?", (todo_id,)
        ).fetchall()
    }


def _contract_rows(conn, todo_id: int) -> list:
    return conn.execute(
        "SELECT id, version, status, spec_json, proposed_by, locked_at FROM contracts "
        "WHERE todo_id = ? ORDER BY version",
        (todo_id,),
    ).fetchall()


def status_of(conn, row) -> str:
    """The agreement stage — DERIVED, never stored.

    Storing it would create a second source of truth that a state machine then has
    to keep in step with the acceptances, the contracts table and the march. Derived,
    it cannot disagree with them.
    """
    if row["dropped_at"] is not None:
        return DROPPED
    if row["state"] == "verified":
        # The SAME march value, read in the vocabulary the task actually speaks: an issue
        # whose every party has said `fixed` is `resolved`, a deliverable that has been
        # checked end to end is `verified`. One stored fact, two words for it — and the
        # word comes from `tasks.mode`, never from a caller.
        return RESOLVED if is_debug(conn, row["task_id"]) else VERIFIED
    if _contract_rows(conn, row["id"]):
        return CONTRACTED
    d = decisions(conn, row["id"], row["version"])
    parties = parties_of(row)
    if all(d.get(p, {}).get("decision") == ACCEPT for p in parties):
        return ACCEPTED
    return PENDING


def unjoined_parties(conn, task_id: str, parties: list[str]) -> list[str]:
    """Which of ``parties`` are seats NOBODY has ever accepted an invite for.

    A party list may legitimately name an unjoined seat — you can propose ahead of
    someone's arrival — but then "waiting on Priya" and "waiting on a seat nobody ever
    accepted" look identical, and they need different actions: nudge a colleague, or
    chase an invite. This is the difference, in party order.

    There is deliberately NO timeout attached to it. Any number would be arbitrary, and
    a todo that silently expires is worse than one visibly waiting; ``host_drop_todo``
    is already the escape hatch.
    """
    from . import seats as _seats

    joined = _seats.joined_handles(conn, task_id)
    return [p for p in parties if p not in joined]


def to_dict(conn, row) -> dict:
    """The wire shape for ``get_todos`` and ``/api``.

    NOTHING is withheld by stage. A todo is a title, a scope and a party list — there
    is nothing here to protect until agreement, so unlike ``get_contract`` there is
    nothing to strip from a proposal.

    A todo row DOES now carry a host-set ``staging_url`` override, and it is
    deliberately NOT in this shape. This is what the agents' ``get_todos`` returns, and
    publishing the target here would hand an agent the one fetchable URL without it
    having signed anything — dissolving the exact incentive ``get_contract``'s
    withholding exists to create. The dashboard adds it in ``api._todos_for``, where the
    reader is a human holding a viewer token.
    """
    d = decisions(conn, row["id"], row["version"])
    parties = parties_of(row)
    contracts = _contract_rows(conn, row["id"])
    locked = [c["version"] for c in contracts if c["status"] == "locked"]
    out = {
        # BOTH, on purpose: `number` is the handle humans and agents type (`#N`,
        # `todo=N`), `id` is the internal key every join in the schema uses. The
        # dashboard needs `id` to key its selection and `number` to print.
        "id": row["id"],
        "number": row["number"],
        "title": row["title"],
        # The human's one-liner, and `None` when nobody wrote one — never "" and never
        # a slice of scope. A surface renders the absence out loud ("no summary yet")
        # rather than an empty line, so a missing one is visible instead of silent.
        "summary": _row_get(row, "summary"),
        "scope": row["scope"],
        "parties": parties,
        "status": status_of(conn, row),
        "version": row["version"],
        "proposed_by": row["proposed_role"],
        "accepted_by": sorted(r for r, v in d.items() if v["decision"] == ACCEPT),
        "declined_by": sorted(r for r, v in d.items() if v["decision"] == DECLINE),
        "awaiting": [p for p in parties if p not in d],
        # The seats on this todo that nobody ever paired into. "awaiting @designer —
        # never joined" and "awaiting Priya" are different problems with different
        # fixes, and without this field every surface renders them the same.
        "unjoined": unjoined_parties(conn, row["task_id"], parties),
        "decline_reasons": {r: v["reason"] for r, v in d.items() if v["decision"] == DECLINE},
        # WHO IS NO LONGER BOUND, and whether they left or were removed. Shipped on every
        # todo (an empty list when nobody has left) rather than only when non-empty: an
        # agent reading its own todo has to be able to tell "mobile was never on this"
        # from "mobile left", and a key that appears only sometimes teaches neither.
        "departed": departures(conn, row["id"]),
        # The per-todo march + its own contract chain, for the mini-stepper.
        "state": row["state"],
        "strikes": row["strikes"],
        "stuck": row["stuck_at"] is not None,
        "stuck_reason": row["stuck_reason"],
        "contract_versions": [c["version"] for c in contracts],
        "locked_versions": locked,
        "contract_version": (locked[-1] if locked else (contracts[-1]["version"] if contracts else None)),
        "drop_consents": sorted(drop_consents(conn, row["id"])),
        "dropped_by": row["dropped_by"],
        "drop_reason": row["drop_reason"],
        "created_at": row["created_at"],
        "verified_at": row["verified_at"],
    }
    # ISSUES carry two more fields, and ONLY on a debug task — the same
    # additive/omit-when-it-does-not-apply rule the engagement keys follow. A contract
    # todo has no `fixed` step at all, so shipping an always-empty `fixed_by` on one
    # would invite an agent to look for a move that does not exist there.
    if is_debug(conn, row["task_id"]):
        f = fixes(conn, row["id"], row["version"])
        out["fixed_by"] = [p for p in parties if p in f]
        out["awaiting_fix"] = [p for p in parties if p not in f]
    return out


def get_todos(conn, task_id: str) -> list[dict]:
    """ALL todos on the task — every stage, nothing withheld.

    One tool, not two: status is a FIELD. A seat that is not a party on a todo still
    sees it here (it may need to know the work exists); it simply is not bound by it.
    """
    return [to_dict(conn, r) for r in _rows(conn, task_id)]


# --------------------------------------------------------------------------- #
# the task ROLLUP — the task's state stops being agent-driven
# --------------------------------------------------------------------------- #
def rollup(conn, task_id: str) -> dict | None:
    """Counts + the task state DERIVED from the todos, or ``None`` if there are none.

    "The task concludes when the last todo verifies" then falls out instead of
    needing a special case, and — the reason for paying the cost of making the task
    state machine non-agent-driven — a rollup cannot drift from its parts the way an
    agent-set task state can.

    The derived state is the FURTHEST march any live todo has reached, except that
    ``verified`` requires ALL of them. ``stuck`` is never derived: a stuck todo is
    surfaced as a count so the human sees it, but it must not force the task into a
    terminal state that would freeze the other five deliverables.

    On a DEBUG task the same arithmetic is spoken in the debug vocabulary, because that is
    the only vocabulary its task state machine has: ``open`` until every live issue is
    fixed, then ``resolved``. The contract path is untouched — it never sees this branch,
    and the six march values it derives from are unchanged.
    """
    rows = live_todos(conn, task_id)
    if not rows:
        return None
    debug = is_debug(conn, task_id)
    counts = {s: 0 for s in ALL_STATUSES}
    stuck = 0
    # Todos still waiting on a seat NOBODY ever accepted an invite for. Counted
    # separately from `pending` because the fix is different: pending is a colleague to
    # nudge, this is an invite to chase. No timeout — see `unjoined_parties`.
    unjoined = 0
    for r in rows:
        status = status_of(conn, r)
        counts[status] += 1
        if r["stuck_at"] is not None:
            stuck += 1
        if status == PENDING and unjoined_parties(conn, task_id, parties_of(r)):
            unjoined += 1

    states = [r["state"] for r in rows]
    done = all(s == "verified" for s in states)
    if debug:
        # An issue has no contract chain, so it only ever sits at `open` or `verified` and
        # the furthest-march reading has nothing to say. The task is `resolved` when every
        # live issue is fixed and `open` while any is not — and that is a ROLLUP, so it
        # goes backwards the moment a new issue is raised (see `apply_rollup`).
        derived = "resolved" if done else "open"
    elif done:
        derived = "verified"
    else:
        derived = max((s for s in states if s != "verified"), key=lambda s: _MARCH_RANK.get(s, 0))

    total = len(rows)
    # ONE count, two names. `verified` is what every existing surface reads; `fixed` is the
    # same number under the word a debug task's panel prints ("2/3 fixed"), so no surface
    # has to translate a vocabulary it was not told about.
    finished = counts[VERIFIED] + counts[RESOLVED]
    return {
        "total": total,
        "counts": counts,
        "verified": finished,
        "fixed": finished,
        "pending": counts[PENDING],
        "stuck": stuck,
        # ⊆ pending: how many of those are blocked on a seat nobody ever joined.
        "unjoined": unjoined,
        "dropped": conn.execute(
            "SELECT COUNT(*) AS n FROM todos WHERE task_id = ? AND dropped_at IS NOT NULL",
            (task_id,),
        ).fetchone()["n"],
        "state": derived,
        "complete": finished == total,
    }


def apply_rollup(conn, task_id: str) -> str | None:
    """Write the derived state onto the task. Returns it, or ``None`` when there are
    no todos (in which case the task's own agent-driven machine still owns the state).

    A task-level ``stuck``/``resolved`` is a HUMAN-facing escalation about the whole
    collaboration, so the rollup never overwrites one — only a human reopens it.
    """
    from . import state as _state  # local: state imports this module lazily too

    roll = rollup(conn, task_id)
    if roll is None:
        return None
    current = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    # THE ROLLUP DOES NOT OVERWRITE A HUMAN-LEVEL VERDICT.
    #
    # `stuck`/`resolved` are escalations about the whole collaboration; only a human
    # reopens one. `confirmed` joins them for a different reason and it is the one that
    # makes engagement mode work at all: the rollup can only ever derive one of the six
    # march states, so a task the client has CONFIRMED would be silently stomped back to
    # `verified` by the very next rollup pass — which fires on every contract proposal,
    # every lock, every reopen and every todo report. The client's acceptance would
    # evaporate the moment anyone touched anything.
    #
    # `resolved` is the ONE of the three that is not always human-level, and the
    # distinction is the same one `_assert_task_usable` already draws: a task that reached
    # `resolved` because every live ISSUE is fixed got there by this very function, so it
    # is a rollup and must go backwards when a new issue appears — nobody should need a
    # human to reopen a bug they just found. A task that reached it because an agent said
    # `report_status('resolved')` is a human-level verdict and stays terminal.
    if current is not None and current["state"] in (_state.STUCK, _state.CONFIRMED):
        return current["state"]
    if (
        current is not None
        and current["state"] == _state.RESOLVED
        and not rollup_resolved(conn, task_id)
    ):
        return current["state"]
    return _state._transition(conn, task_id, roll["state"])


def rollup_resolved(conn, task_id: str) -> bool:
    """Did this task reach ``resolved`` by ROLLUP rather than by a person saying so?

    True only for a debug task whose ``resolved`` has no task-level ``resolved`` EVENT
    behind it — that event is written by ``state._report_resolved`` and by nothing else, so
    its absence is exactly "the state came from the issue counts". No new column: the
    provenance is already in the log, and a column would be a second copy of it.
    """
    from . import state as _state

    return is_debug(conn, task_id) and not _state.human_resolved(conn, task_id)


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #
def assert_party(row, role: str, action: str) -> None:
    """Only a NAMED party may act on a todo.

    The mirror image of "a non-party does not block it": a seat that the todo does
    not bind has no say in it either. Reading is unrestricted (``get_todos``).
    """
    parties = parties_of(row)
    if role not in parties:
        raise ValueError(
            f"you ('{role}') are not a party on todo #{row['number']} '{row['title']}' — "
            f"it binds {', '.join(parties)}. You can read it with get_todos, but only "
            f"a named party can {action} it."
        )


def _assert_open(row, action: str) -> None:
    if row["dropped_at"] is not None:
        raise ValueError(f"todo #{row['number']} was dropped; cannot {action} it")


def min_parties(conn, task_id: str) -> int:
    """How few seats a todo on this task may bind — 2, or 1 on an engagement.

    ONE statement of the rule, read by :func:`_validate_parties` when a party list is
    PROPOSED and by :func:`_assert_departure_allowed` when one shrinks. They have to be
    the same number or removal quietly manufactures a shape that could never have been
    proposed — see the reasoning at the propose-time check below, which is where the
    "why" lives.
    """
    return 1 if deliverables.is_engagement(conn, task_id) else 2


def _validate_parties(conn, task_id: str, parties: object) -> list[str]:
    """Resolve a party list to SEAT HANDLES, refusing anything that names more than one.

    A party list BINDS. ``@FE`` fans out perfectly well in a message — telling every
    frontend something is unambiguous — but binding "both frontends" and binding
    "Sarah only" are different agreements, so the broker must not choose between them.
    Hence :func:`service.resolve_seat` rather than the generous addressee resolver, and
    a refusal that NAMES the candidates instead of guessing.

    Handles, role types that name exactly one seat, tags and display names all resolve
    here — a human types what they have in their head and the broker canonicalises it.
    """
    from . import seats as _seats

    handles, seat_roles = _seats.cast_of(conn, task_id)
    names = _seats.names_of(conn, task_id)
    if isinstance(parties, str):
        parties = [p.strip() for p in parties.split(",")]
    if not isinstance(parties, (list, tuple)):
        raise ValueError("parties must be a list of seats already on the task")
    cleaned = [
        service.resolve_seat(
            str(p).strip(), handles, seat_roles, names, task_id=task_id,
            binding="A todo binds specific people",
            hint=" A todo reuses the task's existing seats — you pair once, at the "
                 "task; there is no per-todo pairing.",
        )
        for p in parties
        if str(p).strip()
    ]
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("parties must be unique (no duplicates)")
    # An EMPTY list first, and before the minimum below: on an engagement the minimum is
    # one, so "you named nobody" would otherwise be answered with "a todo binds at least
    # TWO seats" — a rule that does not apply there and a fix the caller cannot act on.
    if not cleaned:
        raise ValueError(
            f"a {noun(conn, task_id)} binds at least one of the task's seats — name who "
            f"is doing the work"
        )
    # A SOLO todo is legitimate on an engagement, and only there.
    #
    # On a peer task the second seat IS the accountability: a contract with one
    # signatory is not an agreement, it is a note to self, and nobody is positioned to
    # say the work was actually done.
    #
    # An engagement has an outer ring a peer task does not — the deliverable list agreed
    # with the client, and a verification run that checks it. One dev building a landing
    # page alone still had to agree the deliverable with the owner, still locks a
    # contract saying what he will build, and still gets checked against it by the
    # owner's agent. The invariant holds ("we said we will build like this, and this is
    # how we have built"); the counterparty is the client rather than a peer. Requiring a
    # second dev there would just be conscripting somebody to rubber-stamp work they had
    # nothing to do with, which is worse than no signature at all.
    if len(cleaned) < min_parties(conn, task_id):
        if is_debug(conn, task_id):
            raise ValueError(
                "an issue binds at least TWO of the task's seats — one to raise it and one "
                "to agree it is real and, later, that it is fixed. `fixed #N` requires "
                "EVERY party, and an issue with one party would resolve on its own word"
            )
        raise ValueError(
            "a todo binds at least TWO of the task's seats — one to produce the "
            "deliverable and one to build against it (same rule as a contract task's cast)"
        )
    _assert_parties_ready(conn, task_id, cleaned)
    return cleaned


def _assert_parties_ready(conn, task_id: str, parties: list[str]) -> None:
    """Refuse to BIND a seat that has not passed pre-flight.

    Naming someone a party is the moment you start depending on them, so it is the
    right moment to block — and the only one where blocking is a choice the caller can
    act on, since it is CHOOSING the seats. Everything downstream is scoped instead:
    ``state.propose_contract`` waits only on the parties of its own todo, and a seat
    the todo does not name never blocks it at all.

    Messaging is deliberately NOT gated anywhere. "Go finish your pre-flight" is the
    most natural thing to say to an unready seat, and refusing to carry it would leave
    the task with no way out of exactly this state — so the refusal below points at
    ``send_message`` rather than closing that door.

    Remote-only, like every readiness gate here: local self-declared identities never
    run pre-flight (nor does the middleware gate them), so enforcing it locally would
    brick the whole local todo flow.
    """
    from . import config as _config
    from . import seats as _seats

    if not _config.get_config().is_remote:
        return
    not_ready = _seats.unready(conn, task_id, parties)
    if not_ready:
        who = ", ".join(f"{r['seat']} ({r['status']} pre-flight)" for r in not_ready)
        one = not_ready[0]["seat"]
        raise ValueError(
            f"a todo cannot bind a seat that has not passed pre-flight; not ready: "
            f"{who}. Their agent calls readiness_check() then "
            f"submit_readiness(answers) — ask them to, with "
            f"send_message('question', '...', to_role='{one}'), and propose the todo "
            f"once they have. Or bind only the seats that are ready: a seat a todo "
            f"does not name is not blocked by it and does not block it."
        )


def _assert_text(title: str, scope: str, what: str = "todo") -> tuple[str, str]:
    """``what`` is the noun this task's rows go by (:func:`noun`) — ``todo`` or ``issue``.
    Same two fields, same limits; only the word the refusal uses changes, so an agent
    working issues is never corrected in a vocabulary its human does not use."""
    title = (title or "").strip()
    scope = (scope or "").strip()
    if not title:
        raise ValueError(f"an {what} needs a title" if what == "issue"
                         else f"a {what} needs a title")
    if len(title) > MAX_TITLE:
        raise ValueError(f"title must be at most {MAX_TITLE} characters")
    if not scope:
        if what == "issue":
            raise ValueError(
                "an issue needs a scope — what is actually wrong: the symptom, how to "
                "reproduce it, and what counts as fixed. The other parties accept THAT, "
                "not the title, and they each have to agree it is fixed later."
            )
        raise ValueError(
            "a todo needs a scope — what is in and out of this deliverable. The other "
            "parties accept the SCOPE, not the title."
        )
    service.assert_content_size(scope, f"{what} scope")
    return title, scope


MAX_SUMMARY = 240


def _assert_summary(summary: object, scope: str) -> str | None:
    """The human-facing one-liner. Returns the cleaned text, or ``None`` if omitted.

    `scope` is written for the agent that has to BUILD the thing, and it grows to a wall
    of text — in scope, out of scope, constraints, open questions — which is right for
    its job and unreadable at a glance. The summary is the other audience: a person
    scanning the board who wants to know what is being built without reading a spec.

    **A copy-paste of the scope is refused.** That is the failure mode: an agent asked
    for a summary hands back the first N characters of what it already wrote, which
    costs the reader a truncated spec instead of buying them a sentence. The broker
    cannot check that a summary is ACCURATE — that is guidance, and the briefing carries
    it — but it can check that somebody actually wrote one.
    """
    if summary is None:
        return None
    summary = str(summary).strip()
    if not summary:
        return None
    if len(summary) > MAX_SUMMARY:
        raise ValueError(
            f"summary must be at most {MAX_SUMMARY} characters (got {len(summary)}) — "
            f"it is the one line a human reads to know what you are building. The detail "
            f"belongs in scope, which has room for it."
        )
    normalise = lambda s: " ".join(s.split()).lower()
    if normalise(summary) and normalise(scope).startswith(normalise(summary)[:80]):
        raise ValueError(
            "summary must not be the opening of scope — a truncated spec is harder to "
            "read than the spec. Say what this deliverable IS, in a sentence somebody "
            "who has not read the scope would understand."
        )
    return summary


def _assert_task_usable(conn, task_id: str) -> None:
    from . import state as _state

    row = conn.execute("SELECT state, mode, closed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    if row["closed_at"] is not None:
        raise ValueError(f"task '{task_id}' is closed")
    # A debug task DOES carry rows in this table now — they are ISSUES: a todo with the
    # contract half removed. Nothing here refuses them; which of the two a caller is
    # allowed to propose is decided by mode, in `propose_todo`/`propose_issue`.
    #
    # `verified` is NOT terminal once a task runs on todos (it is a rollup that can
    # go backwards when a new todo appears), but a human-escalated stuck/resolved is. A
    # ROLLUP-derived `resolved` is the debug-mode mirror of that same distinction: every
    # issue is fixed, so the task reads resolved — and raising a new issue must drop it
    # back with no human involved, exactly as a seventh todo reopens a `verified` task.
    if row["state"] == _state.RESOLVED and rollup_resolved(conn, task_id):
        return
    if row["state"] in (_state.STUCK, _state.RESOLVED):
        raise ValueError(
            f"task is in terminal state '{row['state']}'; reopening requires a human"
        )


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def propose_todo(conn, identity: Identity, title: str, scope: str, parties: list[str],
                 summary: str | None = None) -> dict:
    """Propose a DELIVERABLE on a contract task. Refused on a debug task, which carries
    ISSUES — see :func:`propose_issue`, which is this same function under the other name.
    """
    if is_debug(conn, identity.task_id):
        raise ValueError(
            f"task '{identity.task_id}' is a DEBUG task: its work is ISSUES, not todos — "
            f"a problem you agree is real, fix, and every party says `fixed #N` on. Same "
            f"call, other name: propose_issue(title, scope, parties). (There is no "
            f"contract stage on a debug task, which is the only difference between them.)"
        )
    return _propose(conn, identity, title, scope, parties, summary)


def propose_issue(conn, identity: Identity, title: str, scope: str, parties: list[str],
                  summary: str | None = None) -> dict:
    """Raise an ISSUE on a debug task — TWO NAMES, ONE MECHANISM.

    An issue is a todo with the contract half removed, so there is nothing here to
    implement twice: the numbering, the party list, "proposing IS your own acceptance",
    accept/decline/repropose/drop, the stuck flag and the task rollup are the same rows and
    the same code as a contract task's todos. The second NAME exists because an agent
    briefed on a debug session is told to raise an "issue", and making it type
    ``propose_todo`` for that is a tool fighting its own vocabulary.
    """
    if not is_debug(conn, identity.task_id):
        mode = task_mode(conn, identity.task_id)
        raise ValueError(
            f"issues are for DEBUG tasks, and '{identity.task_id}' is a {mode} task — its "
            f"work is todos, each with a contract on it. Same call, other name: "
            f"propose_todo(title, scope, parties)."
        )
    return _propose(conn, identity, title, scope, parties, summary)


def _propose(conn, identity: Identity, title: str, scope: str, parties: list[str],
             summary: str | None = None) -> dict:
    """Propose a deliverable. **Proposing IS the creator's own consent**; the other
    named parties must accept before the todo is `accepted`.

    Todos surface FROM the conversation — by the time you know them the host setup
    screen is long gone — so they are agent-proposed, with no human approval gate.
    That is not new authority: ``propose_contract``/``lock_contract`` have no human
    sign-off either; control comes from the prompt's "propose only when your human
    directs it", and todos follow the identical rule.

    The ONE implementation behind ``propose_todo`` and ``propose_issue``. Every word it
    writes reads its noun off ``tasks.mode`` (:func:`noun`) — never off an argument — so
    the two names can never mean two behaviours.
    """
    _assert_task_usable(conn, identity.task_id)
    # ENGAGEMENT GATE. On commissioned work nothing may be built until the client's
    # deliverable list is agreed — an engagement with no agreed scope has nothing to
    # build, exactly as a task with no todos has nothing to contract. A no-op on
    # `contract`/`debug` tasks, which have no client and no list.
    #
    # It lives HERE, in the domain layer, and deliberately not in middleware's
    # ACTION_TOOLS gate: that whole gate sits inside `if cfg.is_remote`, so a rule
    # placed there would silently not apply to `sys-buddy local` — which is how this
    # gets demoed and how CLAUDE.md says to test. It would look enforced and not be.
    # The domain layer can also refuse USEFULLY, naming who has not accepted yet.
    what = noun(conn, identity.task_id)
    deliverables.assert_can_build(conn, identity.task_id, f"propose a {what}")
    title, scope = _assert_text(title, scope, what)
    summary = _assert_summary(summary, scope)
    parties = _validate_parties(conn, identity.task_id, parties)
    if identity.role not in parties:
        raise ValueError(
            f"you ('{identity.role}') must be one of the parties on a {what} you propose — "
            f"you named {', '.join(parties)}. Proposing IS your consent, so you cannot "
            f"propose work that binds only other people."
        )

    now = _now()
    # The human's `#N` is allocated here, MAX+1 within this task, in the SAME
    # transaction as the INSERT that consumes it. Two concurrent proposals can read the
    # same MAX and collide on the `todos_task_number` unique index; retry re-reads it.
    for _attempt in range(6):
        number = next_number(conn, identity.task_id)
        try:
            cur = conn.execute(
                "INSERT INTO todos (task_id, number, title, scope, summary, parties_json, "
                "version, state, proposed_by, proposed_role, created_at) "
                "VALUES (?,?,?,?,?,?,1,'open',?,?,?)",
                (identity.task_id, number, title, scope, summary, json.dumps(parties),
                 identity.agent_id, identity.role, now),
            )
            break
        except sqlite3.IntegrityError:
            conn.rollback()
    else:
        raise ValueError(f"could not allocate a {what} number — please retry")

    todo_id = cur.lastrowid
    _record(conn, todo_id, 1, identity, ACCEPT, None)
    _event(conn, identity.task_id, "todo_proposed", todo_id,
           {"title": title, "parties": parties, "by": identity.role})
    conn.commit()

    others = [p for p in parties if p != identity.role]
    if what == "issue":
        body = (
            f"Raised issue #{number}: {title}. Detail: {scope}. This binds "
            f"{', '.join(parties)} — raising it IS my acceptance that it is real, so I'm "
            f"waiting on {', '.join(others)}. Read it with get_todos(), then "
            f"accept_todo({number}) if it is a genuine problem, or decline_todo("
            f"{number}, reason) if it is not one. Once accepted, whoever fixes it reports "
            f"report_status('fixed', detail, todo={number}) — and it is only RESOLVED once "
            f"EVERY party has reported that independently. There is no contract on an issue."
        )
    else:
        body = (
            f"Proposed todo #{number}: {title}. Scope: {scope}. This binds "
            f"{', '.join(parties)} — proposing is my acceptance, so I'm waiting on "
            f"{', '.join(others)}. Read it with get_todos(), then accept_todo({number}) "
            f"if the scope is right, or decline_todo({number}, reason) / message me to "
            f"reshape it. Accepting agrees on WHAT; the contract on this todo is a "
            f"separate, later agreement about HOW."
        )
    service.post_message(conn, identity, "todo_proposal", body, todo_id=todo_id)
    # RE-DERIVE THE TASK. A new deliverable moves the rollup backwards — that is the whole
    # point of the task state being derived rather than latched: on a debug task whose
    # every issue was fixed, raising the next one drops it out of `resolved` with no human
    # needed (a human-escalated `resolved` is refused above and never reaches here).
    apply_rollup(conn, identity.task_id)
    conn.commit()
    return _result(conn, identity.task_id, todo_id)


def accept_todo(conn, identity: Identity, number: int) -> dict:
    """Agree to WHAT this deliverable is. Not a lock — only "yes, let's work on it".

    The same call accepts an ISSUE on a debug task ("yes, that is a real bug") — one
    mechanism, and the message it posts names the next move in that task's own vocabulary.
    """
    _assert_task_usable(conn, identity.task_id)
    row = get_row(conn, identity.task_id, number)
    _assert_open(row, "accept")
    assert_party(row, identity.role, "accept")

    _record(conn, row["id"], row["version"], identity, ACCEPT, None)
    _event(conn, identity.task_id, "todo_accepted", row["id"],
           {"title": row["title"], "by": identity.role, "version": row["version"]})
    conn.commit()

    d = decisions(conn, row["id"], row["version"])
    parties = parties_of(row)
    what = noun(conn, identity.task_id)
    awaiting = [p for p in parties if d.get(p, {}).get("decision") != ACCEPT]
    if awaiting:
        body = (
            f"Accepted {what} #{row['number']} v{row['version']} ({row['title']}). "
            f"Still waiting on {', '.join(awaiting)}."
        )
    elif what == "issue":
        body = (
            f"Accepted issue #{row['number']} v{row['version']} ({row['title']}) — every "
            f"party ({', '.join(parties)}) now agrees this is real, so this is where the "
            f"work happens. There is no contract to agree: fix it, then each of us reports "
            f"report_status('fixed', detail, todo={row['number']}) independently. It stays "
            f"accepted until EVERY party has, and resolves on the last one."
        )
    else:
        body = (
            f"Accepted todo #{row['number']} v{row['version']} ({row['title']}) — every party "
            f"({', '.join(parties)}) has now agreed on WHAT. Next is HOW: one of us "
            f"proposes a contract on this todo with "
            f"propose_contract(spec, todo={row['number']}), and the SAME parties sign it."
        )
    service.post_message(conn, identity, "todo_accept", body, todo_id=row["id"])
    return _result(conn, identity.task_id, row["id"])


def decline_todo(conn, identity: Identity, number: int, reason: str) -> dict:
    """Bounce a todo back to its creator, with a reason.

    Recorded as a LIST entry beside the acceptances, not as a `declined` STATUS: a
    status would have to be unwound by the state machine the moment the creator
    reproposes, and it would lose "who said no, and why".
    """
    _assert_task_usable(conn, identity.task_id)
    row = get_row(conn, identity.task_id, number)
    _assert_open(row, "decline")
    assert_party(row, identity.role, "decline")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("decline_todo needs a reason — the creator has to know what to change")
    service.assert_content_size(reason, "decline reason")

    _record(conn, row["id"], row["version"], identity, DECLINE, reason)
    _event(conn, identity.task_id, "todo_declined", row["id"],
           {"title": row["title"], "by": identity.role, "reason": reason,
            "version": row["version"]})
    conn.commit()
    service.post_message(
        conn, identity, "todo_decline",
        f"Declined todo #{row['number']} v{row['version']} ({row['title']}): {reason}. "
        f"Reshape it and repropose_todo({row['number']}, ...) — that issues a new version "
        f"and resets everyone's acceptance, so nobody is held to a scope they didn't read.",
        todo_id=row["id"],
    )
    return _result(conn, identity.task_id, row["id"])


def repropose_todo(
    conn,
    identity: Identity,
    number: int,
    title: str | None = None,
    scope: str | None = None,
    parties: list[str] | None = None,
    summary: str | None = None,
) -> dict:
    """Issue a NEW VERSION of a todo and reset every acceptance.

    Versioning is what makes "you accepted v1, this is v2" auditable instead of
    mysterious — the same shape as re-proposing a contract. Changing the party list
    is allowed here precisely because everyone re-accepts afterwards, so a newly
    added party is indistinguishable from an original one.

    Stage matters, and this is the part that is easy to hand-wave:

    * no contract yet → re-acceptance by all, and that is the whole story;
    * a contract PROPOSED but not locked → its signatures RESET too. The others
      signed a shape that bound two parties and it may now bind three;
    * a contract already LOCKED → refused. A locked contract is immutable; go
      through ``reopen_negotiations`` → new version → everyone signs.
    """
    _assert_task_usable(conn, identity.task_id)
    row = get_row(conn, identity.task_id, number)
    _assert_open(row, "repropose")
    assert_party(row, identity.role, "repropose")
    if row["state"] == "verified":
        raise ValueError(
            f"todo #{row['number']} is verified — reproposing finished work would make the "
            f"task's rollup lie. Propose a NEW todo for follow-up work."
        )

    contracts = _contract_rows(conn, row["id"])
    if any(c["status"] == "locked" for c in contracts):
        raise ValueError(
            f"todo #{row['number']} already has a LOCKED contract, which is immutable. Call "
            f"reopen_negotiations(reason) and propose a new contract version on this "
            f"todo; every party re-signs it."
        )

    new_title, new_scope = _assert_text(
        title if title is not None else row["title"],
        scope if scope is not None else row["scope"],
    )
    # An omitted summary KEEPS the existing one rather than clearing it — same rule as
    # title/scope above. Reproposing to change the party list should not silently strip
    # the sentence the humans have been reading.
    new_summary = (
        _assert_summary(summary, new_scope)
        if summary is not None
        else _row_get(row, "summary")
    )
    new_parties = (
        _validate_parties(conn, identity.task_id, parties)
        if parties is not None
        else parties_of(row)
    )
    if identity.role not in new_parties:
        raise ValueError(
            f"you ('{identity.role}') must remain a party on a todo you repropose. "
            f"To leave a todo, drop it by mutual consent — you cannot write yourself "
            f"out of an agreement you are still party to."
        )

    version = row["version"] + 1
    conn.execute(
        "UPDATE todos SET title = ?, scope = ?, summary = ?, parties_json = ?, "
        "version = ? WHERE id = ?",
        (new_title, new_scope, new_summary, json.dumps(new_parties), version, row["id"]),
    )
    # A draft contract's signatures no longer mean what they meant.
    reset = 0
    for c in contracts:
        if c["status"] != "locked":
            reset += conn.execute(
                "DELETE FROM contract_signatures WHERE contract_id = ?", (c["id"],)
            ).rowcount
    _record(conn, row["id"], version, identity, ACCEPT, None)
    _event(conn, identity.task_id, "todo_reproposed", row["id"],
           {"title": new_title, "parties": new_parties, "by": identity.role,
            "version": version, "signatures_reset": reset})
    conn.commit()

    added = [p for p in new_parties if p not in parties_of(row)]
    note = f" New party: {', '.join(added)}." if added else ""
    sig_note = (
        " Existing contract signatures were RESET — the shape now binds a different set "
        "of parties, so everyone signs again." if reset else ""
    )
    service.post_message(
        conn, identity, "todo_proposal",
        f"Reproposed todo #{row['number']} as v{version}: {new_title}. Scope: {new_scope}. "
        f"Binds {', '.join(new_parties)}.{note} Everyone's earlier acceptance is cleared — "
        f"accept_todo({row['number']}) again if v{version} is right.{sig_note}",
        todo_id=row["id"],
    )
    return _result(conn, identity.task_id, row["id"])


def drop_todo(conn, identity: Identity, number: int, reason: str) -> dict:
    """"We don't need this after all" — MUTUAL: every named party must consent.

    Blocked once the todo is ``verified``: abandoning finished work would make the
    task's "concludes when the last todo verifies" rollup lie.

    If a party has gone silent this deadlocks on exactly the person who is missing —
    that is the ABSENCE case, and its answer is the host's unilateral
    :func:`host_drop_todo`, never a peer-removal tool.
    """
    _assert_task_usable(conn, identity.task_id)
    row = get_row(conn, identity.task_id, number)
    if row["dropped_at"] is not None:
        return _result(conn, identity.task_id, row["id"])
    assert_party(row, identity.role, "drop")
    if row["state"] == "verified":
        raise ValueError(
            f"todo #{row['number']} is verified and cannot be dropped — the task's rollup "
            f"reports it as done, and abandoning it would make that count a lie."
        )
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("drop_todo needs a reason — the other parties have to see why")
    service.assert_content_size(reason, "drop reason")

    conn.execute(
        "INSERT INTO todo_drop_consents (todo_id, role, agent_id, reason, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(todo_id, role) DO UPDATE SET "
        "reason = excluded.reason, created_at = excluded.created_at",
        (row["id"], identity.role, identity.agent_id, reason, _now()),
    )
    conn.commit()

    parties = parties_of(row)
    consented = drop_consents(conn, row["id"])
    remaining = [p for p in parties if p not in consented]
    if remaining:
        service.post_message(
            conn, identity, "todo_drop",
            f"Proposed dropping todo #{row['number']} ({row['title']}): {reason}. Dropping is "
            f"mutual — waiting on {', '.join(remaining)} to also call "
            f"drop_todo({row['number']}, reason). Say so in chat if you'd rather keep it.",
            todo_id=row["id"],
        )
        return _result(conn, identity.task_id, row["id"])

    _finalise_drop(conn, identity.task_id, row["id"], identity.role, reason)
    conn.commit()
    service.post_message(
        conn, identity, "todo_drop",
        f"Todo #{row['number']} ({row['title']}) is DROPPED by mutual consent of "
        f"{', '.join(parties)}: {reason}. It no longer counts toward the task.",
        todo_id=row["id"],
    )
    apply_rollup(conn, identity.task_id)
    conn.commit()
    return _result(conn, identity.task_id, row["id"])


def host_drop_todo(conn, task_id: str, number: int, reason: str, by: str = HOST) -> dict:
    """The HOST drops a todo unilaterally. The escape hatch, and the ONLY one.

    A mutual drop needs every named party's consent — including the party who went
    offline and is the reason you want to drop it. That deadlock is why a human owns
    this, and why no peer-removal tool exists: if a peer could remove a peer, the
    moment one objects to a shape the other removes it and locks without the dissent.

    It MUST leave an explanation. The absent party's agent will come back and find
    the work gone; a broker-authored ``todo_dropped`` push (see
    ``service.BROKER_TYPES``) lands in EVERY seat's message queue saying who dropped
    it and why, so they read a decision rather than discovering a hole.
    """
    # The host types `#N` at a terminal, so this resolves the same way an agent's does.
    row = get_row(conn, task_id, number)
    if row["dropped_at"] is not None:
        raise ValueError(f"todo #{row['number']} is already dropped")
    if row["state"] == "verified":
        raise ValueError(
            f"todo #{row['number']} is verified and cannot be dropped — the task's rollup "
            f"reports it as done, and abandoning it would make that count a lie."
        )
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a host drop needs a reason — it is the only thing the agents will see")
    service.assert_content_size(reason, "drop reason")

    _finalise_drop(conn, task_id, row["id"], by, reason, host=True)
    conn.commit()

    broker = service.ensure_broker_identity(conn, task_id)
    service.post_message(
        conn, broker, "todo_dropped",
        f"Todo #{row['number']} ({row['title']}) was DROPPED by the host ({by}): {reason}. "
        f"It bound {', '.join(parties_of(row))} and no longer counts toward this task. "
        f"This was a human decision, not a peer's — if you were mid-work on it, stop, "
        f"and check get_todos() for what is still live.",
        todo_id=row["id"],
    )
    apply_rollup(conn, task_id)
    conn.commit()
    return to_dict(conn, row_by_id(conn, task_id, row["id"]))


# --------------------------------------------------------------------------- #
# removing ONE party — the two situations, and why they need two mechanisms
# --------------------------------------------------------------------------- #
# "We don't need mobile after all" and "mobile has an outage" look identical from the
# party list and are opposites underneath.
#
#   * PRESENT AND COOPERATIVE → the seat removes ITSELF. :func:`leave_todo` takes no
#     seat argument at all, so "remove my peer" is not a refused call, it is an
#     unspellable one. That is what keeps "no agent ejects a peer" true on a
#     cross-org broker: the property is structural, not a permission check somebody
#     can be argued past.
#   * GONE DARK → self-removal is useless exactly when it is needed, because an outage
#     IS "cannot call a tool". So somebody else must act, and that somebody is a
#     HUMAN: :func:`host_remove_party`, reachable from the CLI and the desktop app.
#     Ejecting a peer is the most abusable capability on a cross-org task, and a human
#     typing a command cannot be prompt-injected. Same posture as `staging_url`,
#     `add-seat`, `revoke-agent` and `close`, all host-owned for the same reason.
#
# Both write the SAME row (``todo_departures``) through the same core below, so the two
# can never drift into two behaviours; only who may call them, and the word the log
# prints, differ.
def leave_todo(conn, identity: Identity, number: int, reason: str) -> dict:
    """Take YOURSELF off one todo. The deliverable carries on without you.

    The gap this fills: ``drop_todo`` abandons the whole thing and needs every party to
    agree, so "we don't need mobile after all" had no move at all — the only options
    were to abandon work two other people still wanted, or to leave a name on a party
    list that would block its quorum forever.

    **SELF ONLY, structurally.** There is no ``seat`` parameter here and none on either
    tool surface, so no argument exists that could name a peer. See the section comment
    above for why that matters more than a permission check would.

    Refused when you are the last party, when the todo is already verified (the same
    terminal rule ``drop_todo`` obeys, for the same reason: the rollup counts it as
    done), and when leaving would take the todo below the number of parties it could
    have been PROPOSED with — a peer deliverable with one party has nobody to sign
    against and nobody who may check the work, so it is not a smaller agreement, it is
    an abandoned one, and abandoning is what ``drop_todo`` is for.

    Everything you already did stays recorded: your acceptance, your signature on a
    locked contract, your ``fixed``. You are no longer BOUND; you were still there.
    """
    _assert_task_usable(conn, identity.task_id)
    row = get_row(conn, identity.task_id, number)
    what = noun(conn, identity.task_id)
    _assert_open(row, "leave")
    assert_party(row, identity.role, "leave")
    reason = _clean_departure_reason(reason, "leave_todo needs a reason")
    _assert_departure_allowed(conn, row, identity.role, what, host=False)

    remaining = _record_departure(
        conn, identity.task_id, row, identity.role, reason,
        mode=LEFT, acted_by=identity.role, agent_id=identity.agent_id,
    )
    conn.commit()
    service.post_message(
        conn, identity, "todo_leave",
        f"I've LEFT {what} #{row['number']} ({row['title']}): {reason}. It is no longer "
        f"waiting on me and I am not bound by it — it now binds "
        f"{', '.join(remaining)}, and its quorum has been recalculated over them. "
        f"Nothing I already did is erased: my acceptance and any signature of mine stay "
        f"on the record. If you want me back on it, repropose_todo({row['number']}, "
        f"parties=[...]) naming me again — I cannot put myself back.",
        todo_id=row["id"],
    )
    settle_after_departure(conn, identity.task_id, row["id"], identity.role)
    apply_rollup(conn, identity.task_id)
    conn.commit()
    return _result(conn, identity.task_id, row["id"])


def host_remove_party(conn, task_id: str, number: int, seat: str, reason: str,
                      by: str = HOST) -> dict:
    """The HOST removes ONE unresponsive party. NOT an agent tool, deliberately.

    The case ``leave_todo`` cannot cover: an outage means the agent cannot call
    anything, so the one call that would fix it is the one call that is unavailable.
    Before this, absence had exactly one answer — ``host_drop_todo``, which throws away
    a deliverable the remaining parties were still working on and still wanted.

    It is the smaller of the two host hatches, and the log says which was used. Same
    rules as :func:`leave_todo` otherwise, because they are the same act: verified is
    terminal, the last party may not go, and the remaining list must still be a list a
    todo could have been proposed with.

    ``seat`` is resolved the way every binding seat reference is (``service.resolve_seat``
    via :func:`_validate_parties`'s resolver) so the host can type ``@mobile``,
    ``mobile``, or that person's display name and be corrected rather than guessed at.
    """
    from . import seats as _seats

    row = get_row(conn, task_id, number)
    what = noun(conn, task_id)
    if row["dropped_at"] is not None:
        raise ValueError(
            f"{what} #{row['number']} is already dropped; there is nobody left to remove "
            f"from it"
        )
    handles, seat_roles = _seats.cast_of(conn, task_id)
    names = _seats.names_of(conn, task_id)
    # A leading `@` is stripped here and only here. This is a HOST-TYPED surface and the
    # briefings teach humans to say `@mobile`, so refusing the form we taught them would
    # be a wall at the one moment they are already dealing with an outage. The resolver
    # itself stays literal — agent-supplied party lists go through it untouched.
    seat = service.resolve_seat(
        str(seat or "").strip().lstrip("@"), handles, seat_roles, names, task_id=task_id,
        binding="Removing a party removes ONE specific person",
        hint=" Run `sys-buddy todo list` to see who each todo binds.",
    )
    if seat not in parties_of(row):
        raise ValueError(
            f"'{seat}' is not a party on {what} #{row['number']} ('{row['title']}') — it "
            f"binds {', '.join(parties_of(row))}. Nothing to remove."
        )
    reason = _clean_departure_reason(
        reason, "removing a party needs a reason — it is the only thing the agents will see"
    )
    _assert_departure_allowed(conn, row, seat, what, host=True)

    remaining = _record_departure(
        conn, task_id, row, seat, reason, mode=REMOVED, acted_by=by, agent_id=None,
    )
    conn.commit()

    # Broker-authored, exactly like a host drop: there is no triggering agent, and
    # attributing a human's decision to a participant would both misattribute it and
    # (because `_fetch` excludes your own messages) withhold it from that participant.
    broker = service.ensure_broker_identity(conn, task_id)
    service.post_message(
        conn, broker, "todo_party_removed",
        f"{seat} was REMOVED from {what} #{row['number']} ({row['title']}) by the host "
        f"({by}): {reason}. This was a human decision, not a peer's — no agent can remove "
        f"anyone from a {what}. It now binds {', '.join(remaining)}, and its quorum has "
        f"been recalculated over them, so anything that was only waiting on {seat} is "
        f"unblocked. {seat} is still on the TASK and can still read and message; it is "
        f"this one deliverable it is no longer bound by. Nothing already recorded is "
        f"erased — {seat}'s acceptance and any signature stay on the record. To bring "
        f"them back, a remaining party calls repropose_todo({row['number']}, "
        f"parties=[...]) naming them again.",
        todo_id=row["id"],
    )
    settle_after_departure(conn, task_id, row["id"], by)
    apply_rollup(conn, task_id)
    conn.commit()
    return to_dict(conn, row_by_id(conn, task_id, row["id"]))


def _clean_departure_reason(reason: object, blank_message: str) -> str:
    """A departure ALWAYS carries a reason. It is the only thing the people left behind
    get, and — for a removal — the only thing the absent party's agent will find when it
    comes back to a deliverable that no longer names it."""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError(blank_message)
    service.assert_content_size(reason, "departure reason")
    return reason


def _assert_departure_allowed(conn, row, seat: str, what: str, *, host: bool) -> None:
    """The three refusals, identical for a self-leave and a host removal.

    They are identical on purpose: the two differ in WHO may act, never in what the
    resulting todo is allowed to look like. A rule that a human could bypass would just
    be a rule that gets bypassed.
    """
    # 1. VERIFIED IS TERMINAL — the same rule `drop_todo` obeys. The rollup already
    #    reports this deliverable as done; rewriting who was bound by finished work
    #    would make that count, and the signatures under it, retrospectively untrue.
    if row["state"] == "verified":
        raise ValueError(
            f"{what} #{row['number']} is verified — nobody can be removed from it. The "
            f"task's rollup reports it as done and the record of who delivered it is "
            f"part of that. Follow-up work is a NEW {what}."
        )
    parties = parties_of(row)
    floor = min_parties(conn, row["task_id"])
    # 2. THE LAST PARTY MAY NOT GO. A todo bound to nobody is orphaned — no quorum to
    #    reach, nobody who may act on it, and it still counts toward the task's rollup
    #    forever. That case already has a name and a tool.
    if len(parties) <= 1:
        who = "You are" if not host else f"'{seat}' is"
        raise ValueError(
            f"{who} the ONLY party on {what} #{row['number']} ('{row['title']}'), so "
            f"removing {'you' if not host else 'them'} would leave it bound to nobody — "
            f"orphaned, unactionable, and still counted by the task. That is not a "
            f"departure, it is an abandonment, and abandoning has its own move: "
            + (f"drop_todo({row['number']}, reason)." if not host
               else f"`sys-buddy todo drop <task> {row['number']} --reason \"...\"`.")
        )
    # 3. THE REMAINDER MUST STILL BE A SHAPE THAT COULD HAVE BEEN PROPOSED. A peer
    #    deliverable with one party is not a smaller agreement; it is a note to self.
    #    Concretely it DEADLOCKS: the sole party proposes the contract, so it is the
    #    producer, and `state._report_todo_test` refuses a producer checking its own
    #    work off a peer task — so the todo can never reach `testing`, and `verified`
    #    becomes unreachable. On a debug task the equivalent is worse: `fixed #N` over a
    #    party list of one resolves the issue on its author's word alone. An ENGAGEMENT
    #    is the documented exception and stays one here, because its outer ring (the
    #    client's own agent verifies against the deliverable list) supplies the
    #    counterparty a peer task has to name.
    if len(parties) - 1 < floor:
        left = [p for p in parties if p != seat]
        raise ValueError(
            f"removing '{seat}' would leave {what} #{row['number']} ('{row['title']}') "
            f"bound to {', '.join(left)} alone, and a {what} on this task binds at least "
            f"{floor} seats — the second one is the accountability: it signs the contract "
            f"against you and it is the only party allowed to check your work. A "
            f"one-party {what} here cannot reach verified at all, so this would not "
            f"shrink the deliverable, it would strand it. Either bring in another seat "
            f"first with repropose_todo({row['number']}, parties=[...]), or abandon it: "
            + (f"drop_todo({row['number']}, reason)." if not host
               else f"`sys-buddy todo drop <task> {row['number']} --reason \"...\"`.")
        )


def _record_departure(conn, task_id: str, row, seat: str, reason: str, *,
                      mode: str, acted_by: str, agent_id: int | None) -> list[str]:
    """Write the departure and shrink ``parties_json``. Returns the remaining parties.

    ONE core for both callers. The party list is the ONLY thing that changes: decisions,
    fixes, drop consents and contract signatures are all left exactly as they were,
    because they are statements somebody actually made and deleting them would rewrite
    history rather than end an obligation. Every quorum reads the party list live
    (``status_of``, ``all_fixed``, ``lock_contract``'s ``required``, ``drop_consents``),
    so a departed party's stale row is simply never counted again — which is what makes
    "quorum recomputes" a consequence of the shrink instead of a second mechanism that
    could disagree with it.
    """
    remaining = [p for p in parties_of(row) if p != seat]
    conn.execute(
        "INSERT INTO todo_departures (todo_id, role, mode, acted_by, agent_id, reason, "
        "version, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (row["id"], seat, mode, acted_by, agent_id, reason, row["version"], _now()),
    )
    conn.execute(
        "UPDATE todos SET parties_json = ? WHERE id = ?",
        (json.dumps(remaining), row["id"]),
    )
    _event(
        conn, task_id,
        "todo_party_left" if mode == LEFT else "todo_party_removed",
        row["id"],
        {"title": row["title"], "seat": seat, "by": acted_by, "mode": mode,
         "reason": reason, "version": row["version"], "parties": remaining,
         "host": mode == REMOVED},
    )
    return remaining


def settle_after_departure(conn, task_id: str, todo_id: int, acted_by: str = HOST) -> dict:
    """Re-run every all-must-agree gate on ONE todo now that its party list is smaller.

    **This is the point of the feature.** Removing a name from ``parties_json`` fixes
    every DERIVED reading for free — ``status_of`` recomputes, ``awaiting`` recomputes,
    ``awaiting_fix`` recomputes — but three gates are LATCHED: they fire once, inside the
    call that completes them, and there is no later call to notice that "everyone" now
    means fewer people. Left alone, the outage case ends with a todo that is unblocked on
    paper and still frozen in fact, which is the same as not shipping this at all.

    The three, in the order they can matter:

    * **a mutual drop** whose last outstanding consent was the departing party's. Settled
      first and returns immediately — there is nothing to settle on an abandoned
      deliverable;
    * **a draft contract** every remaining party has already signed. It LOCKS, over the
      same "all must sign" rule, now over a smaller "all";
    * **an issue** every remaining party has already reported ``fixed``. It RESOLVES.

    Only ever UNBLOCKS. No gate here can move a todo backwards, refuse anything, or
    change a shape: a smaller party list can only ever satisfy an all-must-agree test
    that a larger one did not.
    """
    from . import state as _state

    row = row_by_id(conn, task_id, todo_id)
    if row["dropped_at"] is not None or row["state"] == "verified":
        return {}
    out: dict = {}
    parties = parties_of(row)
    consented = drop_consents(conn, todo_id)
    if parties and all(p in consented for p in parties):
        reason = next((consented[p] for p in parties if consented[p]), "dropped by consent")
        # `dropped_by` is "who finalised it", and here that is honestly whoever caused the
        # party list to shrink — the seat that left, or `host` for a removal. Crediting a
        # remaining party would name somebody who consented days ago and did nothing today.
        _finalise_drop(conn, task_id, todo_id, acted_by, reason, host=(acted_by == HOST))
        conn.commit()
        broker = service.ensure_broker_identity(conn, task_id)
        service.post_message(
            conn, broker, "todo_dropped",
            f"{noun(conn, task_id).title()} #{row['number']} ({row['title']}) is now "
            f"DROPPED. Every remaining party ({', '.join(parties)}) had already consented; "
            f"the only outstanding consent belonged to a party that has since left, so the "
            f"drop completes over the parties who are actually on it: {reason}",
            todo_id=todo_id,
        )
        return {"dropped": True}
    # The other two live in the state machine, which owns locks, the march and the
    # messages that go with them — settling them here would be a second implementation
    # of `lock_contract`'s and `_report_issue_fixed`'s endings.
    out.update(_state.settle_todo_quorum(conn, task_id, row))
    return out


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _record(conn, todo_id: int, version: int, identity: Identity, decision: str,
            reason: str | None) -> None:
    """Upsert this role's decision on this VERSION (accept after decline overwrites)."""
    conn.execute(
        "INSERT INTO todo_decisions (todo_id, version, role, agent_id, decision, reason, "
        "created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(todo_id, version, role) DO UPDATE "
        "SET decision = excluded.decision, reason = excluded.reason, "
        "agent_id = excluded.agent_id, created_at = excluded.created_at",
        (todo_id, version, identity.role, identity.agent_id, decision, reason, _now()),
    )


def _finalise_drop(conn, task_id: str, todo_id: int, by: str, reason: str,
                   host: bool = False) -> None:
    conn.execute(
        "UPDATE todos SET dropped_at = ?, dropped_by = ?, drop_reason = ? WHERE id = ?",
        (_now(), by, reason, todo_id),
    )
    _event(conn, task_id, "todo_dropped", todo_id,
           {"by": by, "reason": reason, "host": host})


def _event(conn, task_id: str, kind: str, todo_id: int, detail: dict) -> None:
    """Append a ``todo`` event. One kind in the log (``todo``) with the specific
    action inside, so the dashboard's existing kind filter keeps a fixed vocabulary.

    Records BOTH ids: ``todo_id`` (internal, so the row is still joinable) and
    ``todo_number`` (what the log PRINTS, because "#3" in the log has to be the same
    "#3" the humans typed). Rows written before numbering carry only ``todo_id`` and
    the renderer falls back to it — see ``api._render_detail``.
    """
    ref = conn.execute("SELECT number FROM todos WHERE id = ?", (todo_id,)).fetchone()
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, "todo",
         json.dumps({
             "action": kind,
             "todo_id": todo_id,
             "todo_number": (ref["number"] if ref is not None else None),
             **detail,
         }),
         _now()),
    )


def _result(conn, task_id: str, todo_id: int) -> dict:
    """The wire result for a write. Takes the INTERNAL id — the caller just wrote the
    row and holds it; nothing human arrives here."""
    out = to_dict(conn, row_by_id(conn, task_id, todo_id))
    out["task_rollup"] = rollup(conn, task_id)
    return out
