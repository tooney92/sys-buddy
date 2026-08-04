"""Messaging core — the agent_bus.py port, with its three known bugs fixed.

Pure-ish functions that take an open connection and a resolved ``Identity``. The
FastMCP tool wrappers in ``tools.py`` are thin shells over these; keeping the logic
here means we can spec it without a running server.

Bugs fixed vs the predecessor (SPEC §14 / reference/NOTE.md):
1. WAL — handled in ``db.connect`` (poll loop no longer contends).
2. ``notify_human`` error handling — see ``slack.py`` (built in step 8).
3. delivered vs acked — the predecessor marked messages read *on fetch*, so a
   crashed session lost them. Here a fetch stamps ``delivered_at`` only; the agent
   must ``ack_messages(ids)`` after processing. Until acked, a message keeps coming
   back — a dropped tunnel mid-fetch can't silently eat it.
"""

from __future__ import annotations

import html
import json
import time
from typing import NamedTuple

from .identity import Identity


# --------------------------------------------------------------------------- #
# role tags
# --------------------------------------------------------------------------- #
# Short tags a human types to their own agent ("sm @BE ...") so they don't have to
# spell a role out. The agent is briefed to expand them (onboarding.py / rules.py),
# but the BROKER resolves them too — a tag that reaches the wire is delivered rather
# than rejected, which is the same "broker enforces, agents request" split used
# everywhere else. Tags are a display convenience only: `roles_json` on the task keeps
# storing canonical role names, so nothing downstream (delivery, dashboard, contracts)
# ever sees a tag.
ROLE_TAGS = {
    "be": "backend",
    "fe": "frontend",
    "mb": "mobile",
    "de": "designer",
}


class Resolution(NamedTuple):
    """What ``@something`` turned out to name.

    ``handles`` is a SET of seats (as a list, in declaration order) because a role type
    may now name several of them. ``kind`` says HOW it matched, which is the fact
    callers branch on: a role type naming two seats is a legitimate fan-out for a
    MESSAGE and a refusal in a PARTY LIST. ``canonical`` is the string to store in
    ``messages.to_role`` — the handle for a single seat, the role type for a fan-out.

    ``kind == 'collision'`` is the one case nothing may use: the token is BOTH a role
    type and the handle of a seat that does not hold that role type, so it has two
    honest readings and the broker refuses both rather than picking one.
    """

    handles: list[str]
    kind: str        # 'role' | 'handle' | 'tag' | 'name' | 'collision'
    canonical: str


def _seat_roles(seats: list[str], seat_roles: dict[str, str] | None) -> dict[str, str]:
    """The identity map for anything the caller did not declare — which is exactly
    right for every task written before seats and role types were split apart."""
    return {s: (seat_roles or {}).get(s, s) for s in seats}


def resolve_role(
    value: str,
    seats: list[str],
    seat_roles: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> Resolution | None:
    """Resolve ``value`` against a task's cast, or None when it names nothing.

    Priority order, and it matters: **role type → handle → tag → agent name**.

    * ``@frontend`` / ``@FE`` → every frontend-TYPE seat (a fan-out), always.
    * ``@frontend-2`` → one seat.
    * ``@sarah`` → the seat held by the agent named Sarah, case-insensitively.

    **A role type always means the TYPE.** The first seat of a type is handled
    ``frontend`` — the same string as the type it holds — so with two frontends the
    old handle-first order made ``@frontend`` mean "both frontends" in a message and
    "Sarah's seat" in a party list. One token, two meanings: a human will not hold that
    distinction, and the party-list reading can bind the wrong person to a contract.
    Now it is the type in both, and the unambiguous way to name the first frontend is
    the PERSON'S NAME (which is why names are unique per task). There is deliberately
    no ``@frontend-1`` alias: a second spelling for a seat is the same trap again.

    Nothing is silently shadowed. On a one-seat-per-type task — every task written
    before seats and role types were split — the handle and the role type ARE the same
    string, so this order returns exactly what handle-first returned. A seat whose
    handle equals a role type it does NOT hold (only reachable by a host overriding the
    derived handle) is a genuine two-way collision and comes back as ``kind
    == 'collision'``, refused by every caller.

    This function decides NO policy — it never raises and never picks a winner between
    candidates. Ambiguity is resolved by the caller, because the right answer depends
    on what is being asked: see :func:`resolve_addressee` (fan-out is fine) and
    :func:`resolve_seat` (it is not).
    """
    roles = _seat_roles(seats, seat_roles)
    folded = (value or "").strip().lower()
    if not folded:
        return None

    # The exact seat this token would have named under the old handle-first order.
    # Case-sensitive first, so a cast holding both `QA` and `qa` resolves each to
    # itself rather than to whichever came first.
    exact = value if value in seats else next(
        (s for s in seats if s.lower() == folded), None
    )

    # 1. role type — may name several seats, and it OUTRANKS a like-named handle.
    #    Store the ROLE TYPE as the canonical `to_role`; that single string is what
    #    makes the fan-out work at read time.
    by_role = [s for s in seats if roles[s].lower() == folded]
    if by_role:
        if exact is not None and exact not in by_role:
            # `qa` is a role type here AND the handle of a seat doing something else.
            # Both readings are honest, so neither is chosen — the caller refuses and
            # names every candidate.
            return Resolution(by_role + [exact], "collision", roles[by_role[0]])
        return Resolution(by_role, "role", roles[by_role[0]])

    # 2. exact handle — for everything the role types do not already claim
    #    (`frontend-2`, or a seat the host named by hand).
    if exact is not None:
        return Resolution([exact], "handle", exact)

    # 2b. the derived alias of a SHADOWED seat: `frontend-1` for a seat whose stored
    #     handle is the bare `frontend` on a task that later grew a second frontend.
    #     Its handle cannot be renumbered (it is quoted in message history and
    #     signatures), so it gains an ADDRESS instead — and canonicalises straight back
    #     to the stored handle, so nothing downstream ever sees the alias. Casts
    #     declared today number every seat of a shared type and never land here; see
    #     `seats.seat_aliases`.
    from .seats import seat_aliases  # local: seats imports this module lazily too

    aliased = seat_aliases(seats, roles).get(folded)
    if aliased is not None:
        return Resolution([aliased], "handle", aliased)

    # 3. tag — a typing shortcut for a role type, never for a person or a seat.
    expanded = ROLE_TAGS.get(folded)
    if expanded:
        tagged = [s for s in seats if roles[s].lower() == expanded]
        if tagged:
            return Resolution(tagged, "tag", roles[tagged[0]])

    # 4. display name — unique per task, so this names exactly one seat unless a
    #    legacy database carries a duplicate, which is refused rather than guessed.
    from .seats import fold_name  # local: seats imports this module lazily too

    wanted = fold_name(value)
    named = [s for s, n in (names or {}).items() if s in seats and fold_name(n) == wanted]
    if named:
        ordered = [s for s in seats if s in named]
        return Resolution(ordered, "name", ordered[0])
    return None


def seat_addresses(seats: list[str], seat_roles: dict[str, str] | None = None) -> dict[str, str]:
    """``{handle: the token a human should type for it}``.

    Identical to the handle for every seat but one: a seat shadowed by a role type
    several seats share is typed as its alias (``@frontend-1``), because typing its
    stored handle (``@frontend``) would name the TYPE. Refusals list candidates through
    this map so that every token they print can actually be typed back — a refusal that
    answers "`@frontend` is ambiguous" with "did you mean `@frontend`?" is no answer.
    """
    from .seats import seat_aliases  # local: seats imports this module lazily too

    return {h: a for a, h in seat_aliases(seats, _seat_roles(seats, seat_roles)).items()}


def describe_seats(
    handles: list[str],
    names: dict[str, str] | None = None,
    addresses: dict[str, str] | None = None,
) -> str:
    """``@frontend-1 (Sarah) and @frontend-2 (Priya)`` — for a refusal that has to NAME
    the candidates rather than leave the human guessing which two it meant.

    ``addresses`` (from :func:`seat_addresses`) overrides how a seat is SPELLED, never
    which seat it is.
    """
    names = names or {}
    addresses = addresses or {}
    parts = [
        f"@{addresses.get(h, h)} ({names[h]})" if names.get(h) else f"@{addresses.get(h, h)}"
        for h in handles
    ]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def resolve_addressee(
    value: str,
    seats: list[str],
    seat_roles: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
    task_id: str = "",
) -> str:
    """The ``to_role`` string for a MESSAGE, or ValueError.

    Fan-out is fine here and always was: telling every frontend something is
    unambiguous. A duplicated NAME is not — "Sarah" with two Sarahs on the task means
    the sender has one particular person in mind and the broker must not pick.
    """
    res = resolve_role(value, seats, seat_roles, names)
    at = seat_addresses(seats, seat_roles)
    if res is None:
        raise ValueError(
            f"cannot address '{value}' — not a seat, role or tag on task '{task_id}'. "
            f"This task seats {describe_seats(seats, names, at)}."
        )
    if res.kind == "collision":
        raise ValueError(
            f"'{value}' is both a role type and the name of a seat that does not hold "
            f"it on this task — {describe_seats(res.handles, names, at)}. One token cannot "
            f"mean two things; address the seat you mean by name."
        )
    if res.kind == "name" and len(res.handles) > 1:
        raise ValueError(
            f"'{value}' is the name of {len(res.handles)} people on this task — "
            f"{describe_seats(res.handles, names, at)}. Names are display only and may be "
            f"shared; address the SEAT instead."
        )
    return res.canonical


def resolve_seat(
    value: str,
    seats: list[str],
    seat_roles: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
    task_id: str = "",
    binding: str = "This binds specific people",
    hint: str = "",
) -> str:
    """Exactly ONE seat handle, or ValueError naming the candidates.

    The strict counterpart to :func:`resolve_addressee`, for anything that BINDS —
    a todo's party list, an invite, a seat lookup. ``@FE`` is refused here even though
    it fans out perfectly well in a message, because binding "both frontends" and
    binding "Sarah only" are different agreements and the broker must not choose one.

    ``binding`` is a whole SENTENCE ("A todo binds specific people"), not a fragment —
    it is joined verbatim, so the caller owns the grammar and the refusal reads as
    English rather than as a template with a visible seam in it.

    Note what this means once a role type has two seats: ``@frontend`` is the TYPE
    everywhere, so it is refused here even though a legacy first frontend's handle may
    be spelled the same way. EVERY seat still has a token of its own to say instead —
    a cast declared today numbers all the seats of a shared type (``@frontend-1``,
    ``@frontend-2``), and a task that grew its second frontend later reaches the first
    through the derived alias ``@frontend-1``. The refusal lists those tokens, so it can
    be answered without knowing anyone's name — which matters because an UNJOINED seat
    has no name to know.
    """
    res = resolve_role(value, seats, seat_roles, names)
    at = seat_addresses(seats, seat_roles)
    if res is None:
        raise ValueError(
            f"'{value}' is not a seat on task '{task_id}'. This task seats "
            f"{describe_seats(seats, names, at)}.{hint}"
        )
    if len(res.handles) > 1:
        alt = (
            " Name the seat or the person, not the role type."
            if res.kind in ("role", "tag", "collision") else ""
        )
        raise ValueError(
            f"'{value}' names {len(res.handles)} seats on this task — "
            f"{describe_seats(res.handles, names, at)}. {binding}, so say which.{alt}"
        )
    return res.handles[0]


# --------------------------------------------------------------------------- #
# local-mode identity (self-declared, auto-provisioned on loopback)
# --------------------------------------------------------------------------- #
def ensure_local_identity(conn, task_id: str, agent_name: str) -> Identity:
    """Local mode only: make sure the task and this agent exist, then return it.

    Zero-friction: on loopback an agent just names itself and starts talking. The
    name doubles as the role (fine on a single developer's machine). Remote mode
    never calls this — identity is stamped from the token by the middleware.
    """
    from . import seats as _seats

    row = conn.execute("SELECT roles_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    now = time.time()
    if row is None:
        # Local mode's name-doubles-as-role convention means the seat handle and the
        # role type are the same string, so the seat map is the identity map — exactly
        # what every pre-split task carries.
        conn.execute(
            "INSERT INTO tasks (id, title, state, roles_json, seat_roles_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, task_id, "open", json.dumps([agent_name]),
             json.dumps({agent_name: agent_name}), now),
        )
        row_roles: list[str] = [agent_name]
    else:
        row_roles = json.loads(row["roles_json"])

    # Look the agent up by NAME, not by role. The name is the identity here ("make sure
    # THIS agent exists"); the role merely defaults to it on first sight. Keying on role
    # meant that once an agent's role differed from its name — reassigned on the task, or
    # seeded then corrected — this lookup missed and inserted a SECOND agent with the same
    # name, and appended that name to roles_json as a phantom role. The task then showed
    # duplicate agents and bogus pre-flight chips that no flow could ever clear.
    agent = conn.execute(
        "SELECT id, task_id, name, role, handle FROM agents WHERE task_id = ? AND name = ?",
        (task_id, agent_name),
    ).fetchone()
    if agent is not None:
        conn.commit()
        return Identity(
            agent_id=agent["id"],
            task_id=agent["task_id"],
            name=agent["name"],
            role=agent["handle"] or agent["role"],
            role_type=agent["role"],
        )

    # Genuinely new agent: register its name as a seat (local mode's name-doubles-as-role
    # convention) only now, so re-seeing an existing agent can never grow roles_json.
    if agent_name not in row_roles:
        row_roles.append(agent_name)
        seat_roles = _seats.seat_roles_of(conn, task_id)
        seat_roles[agent_name] = agent_name
        _seats.write_cast(conn, task_id, row_roles, seat_roles)
    cur = conn.execute(
        "INSERT INTO agents (task_id, name, role, handle, token_hash, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, agent_name, agent_name, agent_name, None, now),
    )
    conn.commit()
    return Identity(
        agent_id=cur.lastrowid, task_id=task_id, name=agent_name,
        role=agent_name, role_type=agent_name,
    )


def ensure_broker_identity(conn, task_id: str) -> Identity:
    """A synthetic, permanently-revoked seat the BROKER posts as on ``task_id``.

    Some broker notifications have no triggering agent at all — the host dropping a
    todo from the CLI is the case that forces this. ``messages.from_agent_id`` is NOT
    NULL, and ``_fetch`` excludes ``from_agent_id = me``, so attributing a host action
    to any real agent would both misattribute it AND silently withhold it from that
    very agent. So the broker gets its own row:

    * ``role = BROKER_ROLE`` — a name ``admin.create_task`` already refuses for a real
      seat, so it can never collide with a participant;
    * ``token_hash = NULL`` and ``revoked_at`` set at birth — it can never
      authenticate, never shows up in ``/api`` agent lists, never counts toward the
      pre-flight gate, and never occupies the live-role unique index. It exists only
      to be a message author.
    """
    row = conn.execute(
        "SELECT id, name, role FROM agents WHERE task_id = ? AND role = ? ORDER BY id LIMIT 1",
        (task_id, BROKER_ROLE),
    ).fetchone()
    if row is not None:
        return Identity(
            agent_id=row["id"], task_id=task_id, name=row["name"],
            role=row["role"], role_type=row["role"],
        )
    now = time.time()
    cur = conn.execute(
        "INSERT INTO agents (task_id, name, role, handle, token_hash, created_at, revoked_at) "
        "VALUES (?,?,?,?,NULL,?,?)",
        (task_id, BROKER_NAME, BROKER_ROLE, BROKER_ROLE, now, now),
    )
    conn.commit()
    return Identity(
        agent_id=cur.lastrowid, task_id=task_id, name=BROKER_NAME,
        role=BROKER_ROLE, role_type=BROKER_ROLE,
    )


# --------------------------------------------------------------------------- #
# messaging
# --------------------------------------------------------------------------- #
# Message types the broker produces as a side effect of report_status — they carry
# lifecycle meaning (a deploy, a counted test result, a terminal state). Agents must
# NOT post them via send_message: a free-form test_result would desync the dashboard's
# broker-counted strike total, and a free-form verified/deploy_confirmed would forge a
# lifecycle event that never happened. They go through report_status only.
#
# `fixed` is the debug half of the same idea: one party's "my side of issue #N is fixed",
# written by `state._report_issue_fixed`. It is reserved for exactly the reason
# `test_result` is — a free-form one would claim a party had confirmed a fix they never
# looked at, which is the single thing the all-must-agree rule exists to prevent.
RESERVED_TYPES = frozenset({"deploy_confirmed", "test_result", "verified", "stuck", "fixed"})

# The conversational types an agent may send via send_message. A positive
# allow-list (not just blocking RESERVED_TYPES) also stops an agent forging
# broker-authoritative chips like 'contract_lock' in the human dashboard thread.
ALLOWED_SEND_TYPES = frozenset({"question", "answer", "status_update", "contract_proposal"})

# Types the BROKER itself authors — not peer content at all. They are pushed onto the
# message queue (the only channel a parked wait_for_message reads) so an agent is woken
# by a broker fact instead of having to poll for it. Two consequences, both deliberate:
#   * they are wrapped in the BROKER envelope, not the peer <msg trust="external"> one
#     (see _wrap_broker) — the framing must never claim a peer said this;
#   * no agent can ever send one (assert_sendable rejects them, and they're outside
#     ALLOWED_SEND_TYPES), so a lock notification cannot be forged.
#
# `todo_dropped` joins it for the HOST's unilateral drop (todos.host_drop_todo): the
# escape hatch for a party who went offline and will never consent. Nobody's agent
# authored it, and the absent party's agent MUST find an explanation rather than
# vanished work — so it is broker-authored and pushed to every seat on the task.
BROKER_TYPES = frozenset({"contract_locked", "todo_dropped"})
BROKER_NAME = "sys-buddy"   # the author shown for a broker-authored notification
BROKER_ROLE = "broker"      # …and its role, in both the agent view and the dashboard

# An unbounded body is a DoS AND a prompt-injection amplifier: it is persisted and
# redelivered to the peer on every poll until acked, stuffing the peer LLM's context
# and token budget (SPEC §7). Cap all agent-supplied content at the broker.
MAX_CONTENT_BYTES = 64 * 1024


def assert_content_size(text: str, label: str = "message") -> None:
    """Reject agent-supplied content larger than MAX_CONTENT_BYTES."""
    if text is not None and len(str(text).encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError(
            f"{label} exceeds the {MAX_CONTENT_BYTES // 1024} KB limit — shorten it "
            f"or split it across messages"
        )


def assert_sendable(mtype: str) -> None:
    """Gate the send_message path: only conversational types, never lifecycle ones
    or forged broker chips (SPEC §7/§8)."""
    if mtype in RESERVED_TYPES:
        raise ValueError(
            f"'{mtype}' is a lifecycle event — report it via report_status(...), "
            f"not send_message"
        )
    if mtype in BROKER_TYPES:
        raise ValueError(
            f"'{mtype}' is authored by the broker itself — you cannot send one; "
            f"the broker emits it when the underlying fact actually happens"
        )
    if mtype not in ALLOWED_SEND_TYPES:
        raise ValueError(
            f"'{mtype}' is not a valid message type; use one of "
            f"{sorted(ALLOWED_SEND_TYPES)}"
        )


def _count_other_agents(conn, task_id: str, exclude_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM agents WHERE task_id = ? AND id != ? AND revoked_at IS NULL",
        (task_id, exclude_id),
    ).fetchone()["n"]


def post_message(
    conn,
    identity: Identity,
    mtype: str,
    body: str,
    to_role: str | None = None,
    todo_id: int | None = None,
) -> dict:
    """Store a message from ``identity`` on its task. Returns a small receipt.

    Delivery rows are created lazily on fetch, so an agent that pairs later still
    picks up anything it hasn't acked.

    ``to_role`` directs the message; None/empty broadcasts to all other agents on the
    task (the unchanged default). A non-empty ``to_role`` must name something on the
    task's cast — a SEAT (``frontend-2``), a ROLE TYPE (``frontend``), a tag (``FE``)
    or an agent's display name — and is stored CANONICALISED: the handle for one seat,
    the role type for a fan-out, which is what makes ``@FE`` reach both frontends at
    read time with no storage change.

    A handle that is ALSO a role type shared with other seats (``frontend`` alongside
    ``frontend-2``) delivers to every seat of that type. ``to_role`` is one string
    matched against both fields at read time, so the two cases are indistinguishable
    on the wire — and fanning out is the safer reading of "tell the frontend". Address
    ``@frontend-2`` when you mean one particular seat.

    ``todo_id`` records which deliverable the message belongs to, so the dashboard's
    ⟨todo⟩ chip keys on a real id rather than a string scraped from the body. None
    (the default) is a task-level message — every message before todos existed.
    """
    from . import seats as _seats

    task = conn.execute(
        "SELECT state, closed_at FROM tasks WHERE id = ?", (identity.task_id,)
    ).fetchone()
    if task is None:
        raise ValueError(f"unknown task '{identity.task_id}'")
    if task["closed_at"] is not None:
        raise ValueError(f"task '{identity.task_id}' is closed")
    # Empty string means broadcast, same as None.
    if not to_role:
        to_role = None
    else:
        # Resolve tags/casing/names to the canonical string BEFORE storing, so
        # `to_role` on the message row is always a real seat or role type — the
        # dashboard and the delivery fan-out match on it exactly and must never see "BE".
        cast, seat_roles = _seats.cast_of(conn, identity.task_id)
        to_role = resolve_addressee(
            to_role, cast, seat_roles, _seats.names_of(conn, identity.task_id),
            task_id=identity.task_id,
        )
    assert_content_size(body, "message body")
    state_at_send = task["state"]
    now = time.time()
    cur = conn.execute(
        "INSERT INTO messages (task_id, from_agent_id, type, body_json, to_role, todo_id, state_at_send, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (identity.task_id, identity.agent_id, mtype, json.dumps(body), to_role, todo_id, state_at_send, now),
    )
    conn.commit()
    recipients = _count_other_agents(conn, identity.task_id, identity.agent_id)
    return {"id": cur.lastrowid, "recipients": recipients, "type": mtype, "to_role": to_role}


def _wrap(from_name: str, role: str, task_id: str, body: str, to_role: str | None = None) -> str:
    """The untrusted-content envelope (SPEC §7). Content is DATA, not instructions.

    Every interpolated value is HTML-escaped so attacker-controlled content can't
    break out of the envelope. Without this, a body containing ``</msg>`` could
    close the wrapper early and inject a forged ``trust="internal"`` block that the
    receiving agent would read as trusted in-band instructions — the exact hijack
    the envelope exists to prevent. Agent name/role are chosen by the buddy at
    pairing time, so the attributes are escaped too (``quote=True``).
    """
    attr = lambda v: html.escape(str(v), quote=True)
    to_attr = f' to="{attr(to_role)}"' if to_role else ""
    return (
        f'<msg from="{attr(from_name)}" role="{attr(role)}"{to_attr} trust="external" task="{attr(task_id)}">\n'
        f"{html.escape(str(body), quote=False)}\n"
        f"</msg>"
    )


def _wrap_broker(task_id: str, body: str) -> str:
    """The BROKER envelope — for the notifications the broker authors itself.

    ``_wrap`` frames *peer* content as external DATA. A ``contract_locked`` push is
    not peer content: it is the enforcing broker stating a fact about the task's own
    state (a fact it just wrote to the contracts table). Reusing the peer envelope
    would be dishonest in BOTH directions — it would attribute broker words to an
    agent, and it would tell the reader to treat the broker's own statement as
    untrusted external chatter.

    It still cannot be forged. Message bodies are HTML-escaped by both wrappers, so
    peer content can never emit a real ``<broker>`` tag, and ``assert_sendable``
    refuses ``BROKER_TYPES`` on the public send path.
    """
    attr = lambda v: html.escape(str(v), quote=True)
    return (
        f'<broker from="{attr(BROKER_NAME)}" trust="broker" task="{attr(task_id)}">\n'
        f"{html.escape(str(body), quote=False)}\n"
        f"</broker>"
    )


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fetch(conn, identity: Identity, only_new: bool, mark_delivered: bool = True) -> list[dict]:
    """Return messages on my task, from other agents.

    Two modes, deliberately different (this split is what keeps long-poll working
    while staying crash-safe):

    - ``only_new=False`` (check_messages): everything I haven't *acked*. This is the
      recovery path — a message keeps coming back until I ack it, so a crash mid-
      fetch can't lose it.
    - ``only_new=True`` (wait_for_message): only messages not yet *delivered* to me.
      A parked agent wakes on genuinely new mail and then goes back to sleep,
      instead of busy-spinning on backlog it has already seen but not acked.

    Both stamp ``delivered_at`` (first-seen) on return; neither sets ``acked_at``.
    """
    unseen = "d.delivered_at IS NULL" if only_new else "d.acked_at IS NULL"
    # A directed message matches EITHER of the reader's two names: its seat handle
    # (`@frontend-2` → exactly one) or its role type (`@FE` → every frontend seat).
    # That single OR is the whole of the fan-out — `to_role` stays a plain string
    # filtered at read time, so nothing about the storage changed.
    rows = conn.execute(
        f"""
        SELECT m.id, m.from_agent_id, m.type, m.body_json, m.created_at, m.to_role,
               a.name AS from_name, COALESCE(a.handle, a.role) AS from_role
        FROM messages m
        JOIN agents a ON a.id = m.from_agent_id
        LEFT JOIN deliveries d ON d.message_id = m.id AND d.agent_id = ?
        WHERE m.task_id = ?
          AND m.from_agent_id != ?
          AND (m.to_role IS NULL OR m.to_role = ? OR m.to_role = ?)
          AND ({unseen})
        ORDER BY m.id
        """,
        (identity.agent_id, identity.task_id, identity.agent_id,
         identity.role, identity.kind),
    ).fetchall()

    now = time.time()
    out = []
    for r in rows:
        if mark_delivered:
            # Upsert a delivery row; set delivered_at on first sight, keep acked_at NULL.
            conn.execute(
                "INSERT INTO deliveries (message_id, agent_id, delivered_at) VALUES (?,?,?) "
                "ON CONFLICT(message_id, agent_id) DO UPDATE SET "
                "delivered_at = COALESCE(deliveries.delivered_at, excluded.delivered_at)",
                (r["id"], identity.agent_id, now),
            )
        body = json.loads(r["body_json"])
        if r["type"] in BROKER_TYPES:
            # Broker-authored: the row carries the agent whose call TRIGGERED it (the
            # finalising signer — useful provenance, and it keeps the recipient set
            # right: `from_agent_id != me` means the trigger doesn't get told about
            # the thing it just did, everyone else does). But the words are the
            # broker's, so that is how they are attributed and framed.
            from_name, from_role = BROKER_NAME, BROKER_ROLE
            content = _wrap_broker(identity.task_id, body)
        else:
            from_name, from_role = r["from_name"], r["from_role"]
            content = _wrap(from_name, from_role, identity.task_id, body, r["to_role"])
        out.append(
            {
                "id": r["id"],
                "from": from_name,
                "role": from_role,
                "type": r["type"],
                "sent_at": _fmt_time(r["created_at"]),
                "content": content,
            }
        )
    if mark_delivered:
        conn.commit()
    return out


def fetch_unacked(conn, identity: Identity, mark_delivered: bool = True) -> list[dict]:
    """check_messages: everything I haven't acked yet (crash-safe recovery)."""
    return _fetch(conn, identity, only_new=False, mark_delivered=mark_delivered)


def fetch_new(conn, identity: Identity, mark_delivered: bool = True) -> list[dict]:
    """wait_for_message: only mail not yet delivered to me (wake on new traffic)."""
    return _fetch(conn, identity, only_new=True, mark_delivered=mark_delivered)


def ack(conn, identity: Identity, ids: list[int]) -> int:
    """Mark messages processed. Returns how many were acked.

    Only ids that are real messages on the agent's *own* task, sent by *another*
    agent, are acked. Unknown, foreign-task, or self-sent ids are ignored — so a
    stale or wrong id can't crash the call (no FK error) or write across tasks.
    """
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    valid = [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM messages "
            f"WHERE task_id = ? AND from_agent_id != ? AND id IN ({placeholders})",
            (identity.task_id, identity.agent_id, *ids),
        ).fetchall()
    ]
    now = time.time()
    for mid in valid:
        conn.execute(
            "INSERT INTO deliveries (message_id, agent_id, delivered_at, acked_at) VALUES (?,?,?,?) "
            "ON CONFLICT(message_id, agent_id) DO UPDATE SET "
            "acked_at = excluded.acked_at, "
            "delivered_at = COALESCE(deliveries.delivered_at, excluded.delivered_at)",
            (mid, identity.agent_id, now, now),
        )
    conn.commit()
    return len(valid)


# --------------------------------------------------------------------------- #
# presence — "this seat is parked in wait_for_message"
# --------------------------------------------------------------------------- #
# An agent that keeps a listener parked respawns it every ~WAIT_CAP seconds, so
# there is always a small gap between one wait returning and the next starting.
# Treat gaps under this as the SAME streak, otherwise the dashboard's "listening —
# 42m" would reset to 0 on every message cycle.
LISTEN_STREAK_GAP = 120.0


def mark_listening(conn, identity: Identity, timeout_seconds: float, cap: float) -> float:
    """Stamp this seat as listening until ``now + min(timeout, cap)``.

    Deliberately an EXPIRY, not a boolean: a boolean would persist a LIE if the
    broker dies with agents parked (the clearing ``finally`` never runs), and the
    rows would claim "listening" forever. Every wait is bounded by the cap, so
    ``listening_until > now`` is self-healing with no cleanup job.

    ``listening_since`` is the streak start: kept when the previous stamp expired
    less than ``LISTEN_STREAK_GAP`` ago (the respawn gap between consecutive
    listener waits), reset to now otherwise. Returns the new ``listening_until``.
    """
    now = time.time()
    until = now + max(0.0, min(float(timeout_seconds), float(cap)))
    row = conn.execute(
        "SELECT listening_until, listening_since FROM agents WHERE id = ?",
        (identity.agent_id,),
    ).fetchone()
    since = now
    if row is not None:
        prev_until, prev_since = row["listening_until"], row["listening_since"]
        if prev_since and prev_until and (now - prev_until) < LISTEN_STREAK_GAP:
            since = prev_since
    conn.execute(
        "UPDATE agents SET listening_until = ?, listening_since = ? WHERE id = ?",
        (until, since, identity.agent_id),
    )
    conn.commit()
    return until


def clear_listening(conn, identity: Identity) -> None:
    """Mark this seat's listening window as ended — NOT NULL, "expired as of now".

    Writing ``now`` (rather than NULL) keeps the dot going out immediately while
    leaving ``listening_until`` readable as the moment the seat stopped listening,
    so a respawned listener inside LISTEN_STREAK_GAP still counts as one streak.
    """
    conn.execute(
        "UPDATE agents SET listening_until = ? WHERE id = ?", (time.time(), identity.agent_id)
    )
    conn.commit()


def is_listening(until: float | None, now: float | None = None) -> bool:
    """Read the stored expiry as a live boolean."""
    if not until:
        return False
    return float(until) > (time.time() if now is None else now)


MAX_HISTORY = 200  # cap the agent-supplied `limit` (OWASP API4: records per page)


def channel_history(conn, task_id: str, limit: int = 20) -> list[dict]:
    """Recent traffic on a task (read or unread), oldest-first, for context."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, MAX_HISTORY))
    rows = conn.execute(
        """
        SELECT m.id, m.type, m.body_json, m.created_at, m.to_role, a.name AS from_name,
               COALESCE(a.handle, a.role) AS from_role
        FROM messages m
        JOIN agents a ON a.id = m.from_agent_id
        WHERE m.task_id = ?
        ORDER BY m.id DESC
        LIMIT ?
        """,
        (task_id, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            # Broker-authored notifications are attributed to the broker here too, so
            # the recap an agent reads matches the envelope it was delivered in.
            "from": BROKER_NAME if r["type"] in BROKER_TYPES else r["from_name"],
            "role": BROKER_ROLE if r["type"] in BROKER_TYPES else r["from_role"],
            "type": r["type"],
            "to_role": r["to_role"],
            "sent_at": _fmt_time(r["created_at"]),
            "body": json.loads(r["body_json"]),
        }
        for r in reversed(rows)
    ]
