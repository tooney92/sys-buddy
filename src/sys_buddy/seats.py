"""Seats — the cast of a task, and the ONE place that knows who is on it.

``role`` used to do two jobs at once, and the schema enforced the first:
``UNIQUE(task_id, role) WHERE revoked_at IS NULL`` meant at most one live agent per
role, so a session with two frontend developers was undescribable. One of them had to
pair as ``mobile`` — a lie that then propagated into every message, signature and
event for the life of the task.

The split:

===============  =========================================================
**handle**       WHO. The seat. Unique per task, stable forever, quoted in
                 message history and signatures — so it is never renamed
                 or re-pointed once used, the same rule as todo ``#N`` and
                 for the same reason. ``parties_json``, quorum and
                 provenance all key on this.
**role type**    WHAT KIND of work. Many seats may share one. Drives the
                 agent briefing, the pre-flight question set, the ``@FE``
                 tags and the dashboard colour.
**name**         The human's chosen display name, supplied at join. Display
                 only and never a key — but UNIQUE PER TASK
                 (case-insensitive, whitespace-trimmed), because the point of
                 a name is that ``@sarah`` is safe to type. See
                 :func:`fold_name`.
===============  =========================================================

Why the change is small: every quorum path already iterates the strings in
``tasks.roles_json`` (``remaining = [r for r in required if r not in signed_set]``).
Keep that field's SHAPE and change only what the strings MEAN — role type → handle —
and "both frontends must sign" falls out with no change to quorum logic at all.

**One roster, two renderers.** :func:`roster` is the single source the dashboard panel
and the agent-facing ``get_roster`` tool both read. The shorthand list was hand-typed
in two places in ``ui.html`` once and had already drifted; this does not repeat it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

# A convenience list for the host's "+ add someone" picker, NOT a whitelist. The role
# vocabulary was never closed — `create_task` takes arbitrary strings — and it stays
# open: a task is free to declare `platform` or `data science`.
SUGGESTED_ROLE_TYPES = (
    "backend",
    "frontend",
    "mobile",
    "designer",
    "qa",
    "project manager",
    "devops",
)

# A handle is typed by a human as `@handle` and stored in `parties_json`, so keep it to
# the characters that survive a terminal, a URL and a JSON blob without quoting.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MAX_SEATS = 24  # a sanity bound, not a design limit; a cast this big is a typo


def slug(role_type: str) -> str:
    """The handle a role type suggests: lowercase, non-alphanumerics collapsed to ``-``.

    ``"project manager"`` → ``project-manager``. Falls back to ``seat`` for a role type
    with no word characters at all, so a handle is never the empty string.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (role_type or "").lower()).strip("-")
    return s or "seat"


def _free_number(base: str, taken_lower: set[str], start: int) -> str:
    n = start
    while f"{base}-{n}" in taken_lower:
        n += 1
    return f"{base}-{n}"


def suggest_handle(role_type: str, taken: list[str] | set[str]) -> str:
    """The handle for ONE MORE seat of ``role_type``, given the handles already taken.

    ``frontend`` while it is the only frontend seat; once the family exists, the next
    free number in it — ``frontend-2`` after ``frontend``, ``frontend-3`` after
    ``frontend-1``/``frontend-2``.

    NEVER reused and NEVER renumbered — the same rule as todo ``#N``, and for the same
    reason: a handle is quoted in message history, so re-pointing one silently rewrites
    the past. ``taken`` must therefore be every handle the task has EVER declared (i.e.
    ``roles_json``, which keeps a seat listed even after its agent is revoked), not just
    the live ones.

    Numbering starts at 1 unless the bare handle is taken, in which case it starts at 2:
    ``<base>-1`` is then reserved as that seat's ALIAS (see :func:`seat_aliases`) and
    handing it to a different seat would point one address at two people.
    """
    taken_lower = {str(t).lower() for t in taken}
    base = slug(role_type)
    family = {t for t in taken_lower if t == base or re.fullmatch(rf"{re.escape(base)}-\d+", t)}
    if not family:
        return base
    return _free_number(base, taken_lower, 2 if base in taken_lower else 1)


def numbered_handle(role_type: str, taken: list[str] | set[str]) -> str:
    """Always a NUMBERED handle — ``frontend-1``, ``frontend-2`` — never the bare type.

    Used when the declared cast holds two or more seats of one role type, so that no
    seat's handle is ever spelled the same way as the type it holds. That collision is
    the whole ambiguity: ``@frontend`` would be the type (naming both seats) and the
    first seat's handle at the same time, and an UNJOINED seat has no display name to
    disambiguate it with — so it could not be named in a party list at all. Numbering
    both removes the collision at the source rather than resolving it afterwards.
    """
    taken_lower = {str(t).lower() for t in taken}
    base = slug(role_type)
    return _free_number(base, taken_lower, 2 if base in taken_lower else 1)


# --------------------------------------------------------------------------- #
# names — display only, but UNIQUE PER TASK
# --------------------------------------------------------------------------- #
def fold_name(name: object) -> str:
    """The comparison key for a display NAME: trimmed, inner runs of whitespace
    collapsed, lowercased.

    Uniqueness is per TASK, not per ``(task, role type)``. The whole point of a name is
    that ``@sarah`` is safe to type: scoped to the role you could have Sarah/frontend
    and Sarah/backend, and every ``@sarah`` from then on would be ambiguous — a
    permanent hazard on the most common action, bought for a one-time annoyance at
    join. One Sarah per task.

    Whitespace is collapsed as well as trimmed for the same reason: ``Sarah  K`` and
    ``Sarah K`` are one token to the human typing it, so they must be one name here.
    """
    return " ".join(str(name or "").split()).lower()


def taken_names(conn: sqlite3.Connection, task_id: str) -> dict[str, str]:
    """``{folded name: handle}`` for the LIVE agents on a task.

    Legacy databases predate the per-task rule and may hold duplicates, so a fold that
    two rows share collapses to whichever seat is newest. That is a display artefact
    only: nothing keys on a name, and the rows themselves are untouched.
    """
    out: dict[str, str] = {}
    for handle, name in names_of(conn, task_id).items():
        folded = fold_name(name)
        if folded:
            out[folded] = handle
    return out


def name_holder(conn: sqlite3.Connection, task_id: str, name: object) -> str | None:
    """The seat already displaying ``name`` on this task, or ``None``."""
    folded = fold_name(name)
    return taken_names(conn, task_id).get(folded) if folded else None


def assert_name_free(conn: sqlite3.Connection, task_id: str, name: object) -> str:
    """The display name to STORE, or a refusal that says what to do instead.

    **The check on insert is the guarantee**, and it is only a guarantee because every
    caller runs it inside the same ``BEGIN IMMEDIATE`` transaction as the INSERT that
    follows. SQLite has no ``SELECT FOR UPDATE`` and needs none: an immediate
    transaction takes the write lock up front, so no second joiner can slip between
    this read and that write. Anything checking availability outside such a
    transaction — the join page's live tick, the CLI's pre-prompt — is ADVISORY and may
    race; it exists to save a round trip, never to decide.

    Names stay display-only and never become a key: handles remain the identifier, so a
    rename can never move a signature.
    """
    display = " ".join(str(name or "").split())
    if not display:
        raise ValueError(
            "pick a name — it is how your buddy's agent addresses you (`@sarah`)"
        )
    if name_holder(conn, task_id, display) is not None:
        raise ValueError(
            f'"{display}" is already on this task — pick another name '
            f'(e.g. "{display} K")'
        )
    return display


def joined_handles(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """The seats someone has actually PAIRED into.

    Its complement against the declared cast is the fact that makes a stalled todo
    legible: "waiting on Priya" and "waiting on a seat nobody ever accepted" look
    identical without it, and they need different actions — nudge a colleague, or
    chase an invite.
    """
    return set(names_of(conn, task_id))


def assert_handle(handle: str) -> str:
    handle = (handle or "").strip()
    if not _HANDLE_RE.match(handle):
        raise ValueError(
            f"'{handle}' is not a usable seat name — use 1-64 characters from "
            f"letters, digits, dot, underscore or hyphen (it is typed as '@{handle}' "
            f"and stored in every party list and signature)"
        )
    return handle


def normalise_cast(entries: object) -> tuple[list[str], dict[str, str]]:
    """Turn a cast declaration into ``(handles, {handle: role_type})``.

    Each entry is either

    * a plain string — a ROLE TYPE, whose handle is derived (``frontend`` then
      ``frontend-2``); this is the shape every existing caller passes, and for a cast
      with no repeats it produces exactly the identity map the old code implied;
    * or a mapping with ``role`` (the role type) and an optional ``handle``/``seat``
      to override the derived name.

    Deriving rather than demanding is the point: choosing "frontend" twice yields
    ``@frontend-1`` and ``@frontend-2`` with no thought from the host.

    **A role type declared 2+ times numbers EVERY one of its seats**, so no handle is
    ever spelled the same way as a role type several seats share. Declared ONCE, the
    seat keeps the bare handle (``frontend``) — which is every task written before seats
    and role types were split apart, and they are untouched. See :func:`numbered_handle`
    for why the first seat is numbered too.
    """
    if isinstance(entries, str):
        entries = [e.strip() for e in entries.split(",")]
    if not isinstance(entries, (list, tuple)):
        raise ValueError("a task's cast must be a list of roles")
    parsed: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict):
            role_type = str(entry.get("role") or entry.get("role_type") or "").strip()
            handle = str(entry.get("handle") or entry.get("seat") or "").strip()
        else:
            role_type, handle = str(entry).strip(), ""
        if not role_type:
            raise ValueError("every seat needs a role type (e.g. backend, frontend, qa)")
        parsed.append((role_type, handle))

    # How many seats each role type gets, counted BEFORE any handle is derived — a
    # host-named seat is still a seat of its type, so it counts towards "shared".
    shared = {
        base for base in (slug(r) for r, _ in parsed)
        if sum(1 for r, _ in parsed if slug(r) == base) > 1
    }

    handles: list[str] = []
    seat_roles: dict[str, str] = {}
    for role_type, handle in parsed:
        if handle:
            handle = assert_handle(handle)
        elif slug(role_type) in shared:
            handle = numbered_handle(role_type, handles)
        else:
            handle = suggest_handle(role_type, handles)
        if any(h.lower() == handle.lower() for h in handles):
            raise ValueError(
                f"seat '{handle}' is declared twice — a handle names ONE seat, and quorum "
                f"keys on it. Two people doing the same job get two seats "
                f"('{handle}' and '{suggest_handle(role_type, handles)}'), not one name."
            )
        handles.append(handle)
        seat_roles[handle] = role_type
    if len(handles) > MAX_SEATS:
        raise ValueError(f"a task supports at most {MAX_SEATS} seats; you declared {len(handles)}")

    # The invariant the numbering exists to hold, asserted rather than assumed, because
    # a host may still name a seat by hand: no handle may be spelled like a role type
    # that names some OTHER seat. Deriving never produces one — this only fires on an
    # override — and it is what lets "the role type always wins" hold everywhere with no
    # exception. See :func:`assert_no_type_collision`.
    for handle in handles:
        assert_no_type_collision(handle, handles, seat_roles)
    return handles, seat_roles


def assert_no_type_collision(
    handle: str, handles: list[str], seat_roles: dict[str, str]
) -> None:
    """Refuse a handle that a role type would also claim — the collision, at the source.

    Two shapes, one problem: ``@frontend`` naming a seat AND every frontend seat.

    * the handle is a role type SEVERAL seats hold (``frontend`` on a two-frontend
      cast), so the token means one seat and all of them at once;
    * the handle is a role type ANOTHER seat holds (a backend seat handled ``qa`` on a
      cast that also has a QA seat), so the token has two honest readings.

    Both used to be resolved after the fact — refused as ambiguous, or matched by an
    exception for callers that address declared seats. Refusing them here means no task
    created from now on carries the ambiguity at all. A seat whose handle is its OWN
    role type and the only seat holding it (``frontend`` on a one-frontend cast — i.e.
    every task written before seats and role types were split) is untouched: the handle
    and the type are the same string and name the same single seat.
    """
    base = handle.lower()
    mine = slug(seat_roles.get(handle, handle))
    # The seats this token would name if it were read as a ROLE TYPE.
    holders = [h for h in handles if slug(seat_roles.get(h, h)) == base]
    if not holders or holders == [handle]:
        return
    if base == mine:
        raise ValueError(
            f"seat '{handle}' is named after a role type this cast declares "
            f"{len(holders)} times, so '@{handle}' would mean both that one seat and "
            f"all of them. Name it "
            f"'{numbered_handle(base, [h for h in handles if h != handle])}' or "
            f"something of your own."
        )
    raise ValueError(
        f"seat '{handle}' is named after the role type of "
        f"{_describe([h for h in holders if h != handle])}, so '@{handle}' would mean two "
        f"different things. Give it a name no role type on this task uses."
    )


def _describe(handles) -> str:
    """``@backend and @qa`` — for a refusal that names the seats it is talking about."""
    parts = [f"@{h}" for h in handles]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def seat_aliases(handles: list[str], seat_roles: dict[str, str] | None = None) -> dict[str, str]:
    """``{folded alias: handle}`` — the extra ADDRESS a shadowed seat can be reached by.

    One seat, one situation: its handle IS a bare role type (``frontend``) and the task
    now holds two or more seats of that type, so ``@frontend`` means the TYPE and the
    seat has no token of its own. Only reachable two ways — :func:`append_seat` growing
    a second seat of a type onto a task whose first seat already holds the bare handle
    (that handle is immutable, it is quoted in message history and signatures, so it
    cannot be renumbered), and a task declared before this rule existed. A cast declared
    now numbers every seat of a shared type and never lands here.

    The alias is DERIVED, never stored: it exists exactly while 2+ seats of that type do,
    and the stored handle never moves. It is an address, not a rename — ``parties_json``,
    quorum and message history keep resolving through the handle.

    Nothing is minted when ``<type>-1`` is already somebody else's handle: an address
    that points at two seats is the very problem being fixed.
    """
    roles = {h: (seat_roles or {}).get(h, h) for h in handles}
    lower = {h.lower() for h in handles}
    types = {r.lower() for r in roles.values()}
    out: dict[str, str] = {}
    for handle in handles:
        base = slug(roles[handle])
        if handle.lower() != base:
            continue
        if sum(1 for h in handles if slug(roles[h]) == base) < 2:
            continue
        alias = f"{base}-1"
        if alias in lower or alias in types:
            continue
        out[alias] = handle
    return out


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def _task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT roles_json, seat_roles_json FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    return row


def _loads(value, default):
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def cast_of(conn: sqlite3.Connection, task_id: str) -> tuple[list[str], dict[str, str]]:
    """``(handles, {handle: role_type})`` for a task.

    The map falls back to the IDENTITY for any handle it does not mention, which is
    what makes every pre-split task correct with no work: it declared one seat per
    role, so its handles and its role types are the same strings.
    """
    row = _task_row(conn, task_id)
    handles = [str(h) for h in _loads(row["roles_json"], [])]
    declared = _loads(row["seat_roles_json"], {})
    seat_roles = {h: str(declared.get(h, h)) for h in handles}
    return handles, seat_roles


def handles_of(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return cast_of(conn, task_id)[0]


def seat_roles_of(conn: sqlite3.Connection, task_id: str) -> dict[str, str]:
    return cast_of(conn, task_id)[1]


def addresses_of(conn: sqlite3.Connection, task_id: str) -> dict[str, str]:
    """``{handle: the token a human should TYPE for it}`` for one task.

    The DB-backed wrapper over :func:`service.seat_addresses`, so a surface that renders
    a seat does not have to load the cast and re-derive the alias itself. Identical to
    the handle for every seat except a shadowed one (see :func:`seat_aliases`).
    """
    from . import service  # local: service imports this module lazily too

    handles, seat_roles = cast_of(conn, task_id)
    return {h: service.seat_addresses(handles, seat_roles).get(h, h) for h in handles}


def address_of(conn: sqlite3.Connection, task_id: str, handle: str) -> str:
    """The token a human should TYPE for ONE seat — its handle, or its derived address.

    DISPLAY only. The handle is still what binds, signs and resolves; this is the
    spelling that gets a human back to it.
    """
    return addresses_of(conn, task_id).get(handle, handle)


def names_of(conn: sqlite3.Connection, task_id: str) -> dict[str, str]:
    """``{handle: display name}`` for the LIVE agents on a task. Unjoined seats are
    simply absent — a name only exists once a human has picked one at join."""
    return {
        r["handle"]: r["name"]
        for r in conn.execute(
            "SELECT handle, name FROM agents WHERE task_id = ? AND revoked_at IS NULL "
            "ORDER BY id",
            (task_id,),
        ).fetchall()
        if r["handle"]
    }


def unready(conn: sqlite3.Connection, task_id: str, handles) -> list[dict]:
    """Which of ``handles`` have NOT passed pre-flight — ``[{seat, status}, …]``.

    **Pre-flight gates the INTERACTION, not the task.** Every caller passes the seats
    it is actually about (a todo's party list), never "everyone on the task": a seat a
    todo does not name may read it, is not bound by it, is not in its quorum, and does
    not block it — the rule ``parties_json`` already encodes for contracts. A task-wide
    readiness query here once froze two ready people over a third seat that was party
    to neither deliverable.

    Only seats someone has actually PAIRED into are judged. A declared-but-unjoined
    seat has no readiness to fail — "nobody accepted that invite" is a different fact,
    with a different fix, and it must not borrow this one's error message. The roster
    (:func:`roster`) is where that distinction is rendered.

    Order follows ``handles``, so an error message lists seats the way the caller (and
    the human reading it) already has them.
    """
    live = {
        (r["handle"] or r["role"]): r
        for r in conn.execute(
            "SELECT handle, role, ready, readiness_status FROM agents "
            "WHERE task_id = ? AND revoked_at IS NULL ORDER BY id",
            (task_id,),
        ).fetchall()
    }
    out: list[dict] = []
    for handle in handles:
        row = live.get(handle)
        if row is None or row["ready"]:
            continue
        out.append({"seat": handle, "status": row["readiness_status"] or "pending"})
    return out


def write_cast(
    conn: sqlite3.Connection, task_id: str, handles: list[str], seat_roles: dict[str, str]
) -> None:
    """Persist a cast. Both columns together — they are one fact in two fields."""
    conn.execute(
        "UPDATE tasks SET roles_json = ?, seat_roles_json = ? WHERE id = ?",
        (json.dumps(list(handles)), json.dumps(dict(seat_roles)), task_id),
    )


def append_seat(
    conn: sqlite3.Connection, task_id: str, role_type: str, handle: str | None = None
) -> str:
    """Add ONE seat to an existing task and return its handle.

    Declaring the cast up front must not mean the cast is FIXED for the life of the
    task: a QA seat added on day three is ordinary, and forcing a new task instead
    would split the history of one piece of work in two. Creation is simply "append
    these N seats at once", so this and ``create_task`` share the derivation rules.

    A new seat does NOT retroactively join existing todos — ``parties_json`` names who
    AGREED, and a person who was not there did not agree.

    This is the ONE path that can still put a bare role type and a handle on the same
    string: a task declared with one frontend seat holds the handle ``frontend``, and
    that handle is immutable — it is quoted in message history and signatures, so it is
    never renumbered. Adding a second frontend therefore makes ``@frontend`` the TYPE
    and leaves the first seat with no token of its own, which :func:`seat_aliases`
    answers by DERIVING ``@frontend-1`` as an extra address for it. Nothing is written
    and nothing moves; the alias simply exists while 2+ seats of that type do.
    """
    role_type = (role_type or "").strip()
    if not role_type:
        raise ValueError("a seat needs a role type (e.g. backend, frontend, qa)")
    handles, seat_roles = cast_of(conn, task_id)
    if len(handles) >= MAX_SEATS:
        raise ValueError(f"task '{task_id}' already has the maximum of {MAX_SEATS} seats")
    if handle:
        handle = assert_handle(handle)
        if any(h.lower() == handle.lower() for h in handles):
            raise ValueError(
                f"seat '{handle}' already exists on task '{task_id}'. Handles are never "
                f"reused — they are quoted in message history, so re-pointing one would "
                f"silently rewrite the past."
            )
    elif any(slug(seat_roles.get(h, h)) == slug(role_type) for h in handles):
        # The type is about to be shared, so the NEW seat is numbered — never handed the
        # bare type, whatever the seat already holding it is called. (The seat already
        # there keeps its handle: it is immutable, and `seat_aliases` gives it the
        # address `<type>-1` instead.)
        handle = numbered_handle(role_type, handles)
    else:
        handle = suggest_handle(role_type, handles)
    handles.append(handle)
    seat_roles[handle] = role_type
    assert_no_type_collision(handle, handles, seat_roles)
    write_cast(conn, task_id, handles, seat_roles)
    return handle


# --------------------------------------------------------------------------- #
# the roster — ONE source, two renderers
# --------------------------------------------------------------------------- #
def roster(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """Every declared seat: seat · role type · who · pre-flight · presence.

    **Unjoined seats are listed.** A roster built only from joined agents cannot answer
    "who never accepted their invite?" — which is precisely the state that silently
    stalls a task, and the reason the cast has to be DISCOVERABLE rather than inferred
    from who happens to have spoken.

    Rows come out in DECLARATION order (the order the host named the seats), with any
    agent that somehow holds a handle the task never declared appended at the end
    rather than dropped — a roster that hides a participant is worse than an untidy one.

    Every row carries BOTH ``seat`` (the stored handle — what ``parties_json``, quorum
    and message history key on) and ``address`` (the token a human should actually
    TYPE). They differ for exactly one seat: one whose handle IS a bare role type on a
    task that later grew a second seat of that type, where ``@frontend`` now resolves to
    the TYPE. Its handle is immutable — it is quoted in signatures — so it gains the
    derived address ``@frontend-1`` instead (see :func:`seat_aliases`). Rendering the
    handle there would print the one token on the task that is ambiguous, which is
    precisely what a roster exists to prevent. Nothing is stored and nothing moves:
    ``address`` is derived on every read, and equals ``seat`` for every other row.
    """
    from . import service  # local: service imports nothing from here, but keep it lazy

    handles, seat_roles = cast_of(conn, task_id)
    addresses = service.seat_addresses(handles, seat_roles)
    agents = {
        r["handle"]: r
        for r in conn.execute(
            "SELECT id, name, role, handle, ready, readiness_status, listening_until, "
            "listening_since FROM agents "
            "WHERE task_id = ? AND revoked_at IS NULL ORDER BY id",
            (task_id,),
        ).fetchall()
        if r["handle"]
    }
    # An invite that exists and has NOT been redeemed is the difference between "we
    # never asked them" and "we asked and they haven't shown up".
    invited = {
        r["role"]
        for r in conn.execute(
            "SELECT role FROM invites WHERE task_id = ? AND used_at IS NULL "
            "AND expires_at > ?",
            (task_id, time.time()),
        ).fetchall()
    }
    now = time.time()
    order = list(handles) + [h for h in agents if h not in handles]
    out: list[dict] = []
    for handle in order:
        a = agents.get(handle)
        if handle == service.BROKER_ROLE and a is None:
            continue
        out.append(
            {
                "seat": handle,
                # The TYPEABLE token for this seat — the handle for all but a shadowed
                # one. Always present, so a renderer never has to know which case it is
                # in (and never has to re-derive the alias itself).
                "address": addresses.get(handle, handle),
                "role": seat_roles.get(handle, (a["role"] if a else handle)),
                "name": a["name"] if a else None,
                "joined": a is not None,
                "declared": handle in handles,
                "ready": bool(a["ready"]) if a else False,
                "readiness_status": (a["readiness_status"] or "pending") if a else None,
                "listening": service.is_listening(a["listening_until"], now) if a else False,
                "listening_since": a["listening_since"] if a else None,
                "invite_pending": a is None and handle in invited,
            }
        )
    return out


def roster_summary(conn: sqlite3.Connection, task_id: str) -> dict:
    """``{seats, joined, rows}`` — the headline "4 of 5 joined" plus the rows.

    The headline number is the point: an unjoined seat is what silently stalls a task,
    and no roster built only from joined agents can show it.
    """
    rows = roster(conn, task_id)
    return {
        "seats": len(rows),
        "joined": sum(1 for r in rows if r["joined"]),
        "rows": rows,
    }


# --- the owner seat (engagement mode) ---------------------------------------
# One role type is special, and only in `engagement` tasks: the person who
# commissioned the work. He is an ORDINARY seat — same invite, same pre-flight,
# same roster row — and the asymmetry is only in what he does: he authors the
# deliverables, he does not build, and he verifies at the end.
#
# It lives here, beside `cast_of`, because three separate modules need the same
# answer to "is this seat a builder?" and three copies of that question is how
# they drift apart. See docs/enhancements.md item 1.
OWNER_ROLE = "owner"


def is_owner(conn: sqlite3.Connection, task_id: str, handle: str) -> bool:
    """Does this SEAT hold the owner role type?

    Keyed on the role TYPE, not the handle, so an engagement whose owner seat is called
    ``@client`` is answered correctly, and so a task that ever declares two owner seats
    (``owner-1``, ``owner-2``) needs no special case. Compared through :func:`slug` for
    the same reason handles are derived through it — ``"Owner"`` and ``"owner"`` are the
    same role type, and a host typing the capital should not silently get a builder.
    """
    return slug(seat_roles_of(conn, task_id).get(handle, handle)) == OWNER_ROLE


def owner_handles(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Every declared seat whose role type is ``owner`` — normally exactly one."""
    handles, seat_roles = cast_of(conn, task_id)
    return [h for h in handles if slug(seat_roles.get(h, h)) == OWNER_ROLE]


def builder_handles(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Every DECLARED seat that is not the owner — the people who build, and therefore
    the people whose acceptance the deliverable list waits on.

    The owner is excluded because he WROTE the list; asking an author to accept his own
    words is the same theatre as quizzing the host on guidelines he authored himself.

    **Declared, not merely joined** — the same reading ``roles_json`` gets everywhere
    else. A seat nobody ever paired into therefore blocks the lock rather than being
    silently written out of the agreement, and the fix is a human removing it from the
    cast. That is the stricter of the two readings and it is deliberate here: this is a
    SCOPE agreement at the very start of an engagement, when everyone is onboarding
    anyway, not a mid-flight gate. (Pre-flight took the looser reading in 2.0.1 for the
    opposite reason — an unready seat there froze a session already in progress.)

    On a non-engagement task there is no owner seat, so this is simply the whole cast —
    which is why callers can use it without first asking what mode they are in.
    """
    handles, seat_roles = cast_of(conn, task_id)
    return [h for h in handles if slug(seat_roles.get(h, h)) != OWNER_ROLE]
