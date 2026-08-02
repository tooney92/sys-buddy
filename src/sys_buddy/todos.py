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

**No peer may remove a peer.** You joined by accepting; you leave by your own call.
If backend could remove mobile, then the moment mobile objects to a shape backend
removes it and locks without the dissent — "both sides sign" quietly becomes
"whoever proposes wins". The real problem is ABSENCE, not dissent, and a mutual
drop deadlocks on the very party who is missing. So the escape hatch is HUMAN:
:func:`host_drop_todo`, reachable from the CLI/GUI, never from a peer's tool.

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

ACCEPT = "accepted"
DECLINE = "declined"

HOST = "host"  # the `dropped_by` value for a unilateral host drop

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
def task_roles(conn, task_id: str) -> list[str]:
    row = conn.execute("SELECT roles_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    return json.loads(row["roles_json"])


def parties_of(row) -> list[str]:
    return json.loads(row["parties_json"])


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
        return VERIFIED
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
    return {
        # BOTH, on purpose: `number` is the handle humans and agents type (`#N`,
        # `todo=N`), `id` is the internal key every join in the schema uses. The
        # dashboard needs `id` to key its selection and `number` to print.
        "id": row["id"],
        "number": row["number"],
        "title": row["title"],
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
    """
    rows = live_todos(conn, task_id)
    if not rows:
        return None
    counts = {s: 0 for s in STATUSES}
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
    if all(s == "verified" for s in states):
        derived = "verified"
    else:
        derived = max((s for s in states if s != "verified"), key=lambda s: _MARCH_RANK.get(s, 0))

    total = len(rows)
    return {
        "total": total,
        "counts": counts,
        "verified": counts[VERIFIED],
        "pending": counts[PENDING],
        "stuck": stuck,
        # ⊆ pending: how many of those are blocked on a seat nobody ever joined.
        "unjoined": unjoined,
        "dropped": conn.execute(
            "SELECT COUNT(*) AS n FROM todos WHERE task_id = ? AND dropped_at IS NOT NULL",
            (task_id,),
        ).fetchone()["n"],
        "state": derived,
        "complete": counts[VERIFIED] == total,
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
    if current is not None and current["state"] in (
        _state.STUCK, _state.RESOLVED, _state.CONFIRMED
    ):
        return current["state"]
    return _state._transition(conn, task_id, roll["state"])


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
    if len(cleaned) < 2 and not deliverables.is_engagement(conn, task_id):
        raise ValueError(
            "a todo binds at least TWO of the task's seats — one to produce the "
            "deliverable and one to build against it (same rule as a contract task's cast)"
        )
    if not cleaned:
        raise ValueError(
            "a todo binds at least one of the task's seats — name who is doing the work"
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


def _assert_text(title: str, scope: str) -> tuple[str, str]:
    title = (title or "").strip()
    scope = (scope or "").strip()
    if not title:
        raise ValueError("a todo needs a title")
    if len(title) > MAX_TITLE:
        raise ValueError(f"title must be at most {MAX_TITLE} characters")
    if not scope:
        raise ValueError(
            "a todo needs a scope — what is in and out of this deliverable. The other "
            "parties accept the SCOPE, not the title."
        )
    service.assert_content_size(scope, "todo scope")
    return title, scope


def _assert_task_usable(conn, task_id: str) -> None:
    from . import state as _state

    row = conn.execute("SELECT state, mode, closed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    if row["closed_at"] is not None:
        raise ValueError(f"task '{task_id}' is closed")
    if (row["mode"] or "contract") == "debug":
        raise ValueError(
            "debug tasks don't carry todos — they are a single problem you fix and "
            "report_status('resolved'). Todos are for contract tasks."
        )
    # `verified` is NOT terminal once a task runs on todos (it is a rollup that can
    # go backwards when a new todo appears), but a human-escalated stuck/resolved is.
    if row["state"] in (_state.STUCK, _state.RESOLVED):
        raise ValueError(
            f"task is in terminal state '{row['state']}'; reopening requires a human"
        )


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def propose_todo(conn, identity: Identity, title: str, scope: str, parties: list[str]) -> dict:
    """Propose a deliverable. **Proposing IS the creator's own consent**; the other
    named parties must accept before the todo is `accepted`.

    Todos surface FROM the conversation — by the time you know them the host setup
    screen is long gone — so they are agent-proposed, with no human approval gate.
    That is not new authority: ``propose_contract``/``lock_contract`` have no human
    sign-off either; control comes from the prompt's "propose only when your human
    directs it", and todos follow the identical rule.
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
    deliverables.assert_can_build(conn, identity.task_id, "propose a todo")
    title, scope = _assert_text(title, scope)
    parties = _validate_parties(conn, identity.task_id, parties)
    if identity.role not in parties:
        raise ValueError(
            f"you ('{identity.role}') must be one of the parties on a todo you propose — "
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
                "INSERT INTO todos (task_id, number, title, scope, parties_json, version, "
                "state, proposed_by, proposed_role, created_at) "
                "VALUES (?,?,?,?,?,1,'open',?,?,?)",
                (identity.task_id, number, title, scope, json.dumps(parties),
                 identity.agent_id, identity.role, now),
            )
            break
        except sqlite3.IntegrityError:
            conn.rollback()
    else:
        raise ValueError("could not allocate a todo number — please retry")

    todo_id = cur.lastrowid
    _record(conn, todo_id, 1, identity, ACCEPT, None)
    _event(conn, identity.task_id, "todo_proposed", todo_id,
           {"title": title, "parties": parties, "by": identity.role})
    conn.commit()

    others = [p for p in parties if p != identity.role]
    service.post_message(
        conn, identity, "todo_proposal",
        f"Proposed todo #{number}: {title}. Scope: {scope}. This binds "
        f"{', '.join(parties)} — proposing is my acceptance, so I'm waiting on "
        f"{', '.join(others)}. Read it with get_todos(), then accept_todo({number}) "
        f"if the scope is right, or decline_todo({number}, reason) / message me to "
        f"reshape it. Accepting agrees on WHAT; the contract on this todo is a "
        f"separate, later agreement about HOW.",
        todo_id=todo_id,
    )
    return _result(conn, identity.task_id, todo_id)


def accept_todo(conn, identity: Identity, number: int) -> dict:
    """Agree to WHAT this deliverable is. Not a lock — only "yes, let's work on it"."""
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
    awaiting = [p for p in parties if d.get(p, {}).get("decision") != ACCEPT]
    if awaiting:
        body = (
            f"Accepted todo #{row['number']} v{row['version']} ({row['title']}). "
            f"Still waiting on {', '.join(awaiting)}."
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
        "UPDATE todos SET title = ?, scope = ?, parties_json = ?, version = ? WHERE id = ?",
        (new_title, new_scope, json.dumps(new_parties), version, row["id"]),
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
