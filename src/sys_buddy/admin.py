"""Host-side admin operations (SPEC §9): create tasks, mint invites, issue and
revoke credentials, close tasks.

These run on the *host's* machine, against the same SQLite file the broker serves,
so — unlike the messaging tools, which are handed an open connection and a resolved
identity — each function opens its own connection via ``db.connect()``. That mirrors
the ``tools.py`` ``_op_*`` helpers and matches how ``cli.py`` calls them (no ``conn``
argument threaded through the CLI).

Guiding principle: the broker enforces, agents request. Raw tokens and invite codes
never touch the database — only their sha256 (SPEC §9). A leaked db reveals nothing
that can be replayed.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time

from . import audit, seats, service, todos
from .db import connect
from .identity import new_invite_code, new_viewer_token, sha256_hex

# The workflows a task can run. ONE list, because this used to be spelled out
# independently in `create_task`, in the CLI's `--mode` choices and in the desktop app's
# radio group — and when `engagement` shipped in v2.1.0 all three kept the old pair. The
# schema, domain layer, tools, dashboard and briefings supported engagements; the only
# function that creates a task refused the mode, so the feature had no door for two
# releases. A single tuple the surfaces read from is what makes that unrepeatable.
MODES = ("contract", "debug", "engagement")

# Single-use invites live 30 minutes (SPEC §9). Short enough that a code lingering
# in a Slack scrollback is dead by the time anyone scans for it, but long enough that a
# host who mints one, then goes to explain the join to a teammate on another channel,
# comes back to a code that still works rather than one that lapsed mid-handoff.
INVITE_TTL_SECONDS = 30 * 60

# Cap on the slug part of an auto-derived id — keeps ids short enough to read and
# type while still recognisably echoing the title.
_SLUG_MAX = 40


def new_task_id(title: str) -> str:
    """Derive a task id from a human ``title``: a slug plus a short random suffix.

    Humans should only have to type a Title; the id is machine-friendly and unique.
    The slug is lowercase with non-alphanumerics collapsed to single hyphens (e.g.
    "new API" → "new-api"); an empty slug (a title with no word characters) falls
    back to a generic base. The 4-hex-char suffix (16 bits of entropy) keeps two
    same-titled tasks apart without the caller having to invent an id.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:_SLUG_MAX].strip("-")
    base = slug or "task"
    return f"{base}-{secrets.token_hex(2)}"


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _write_event(conn: sqlite3.Connection, task_id: str, kind: str, detail: dict) -> None:
    """Append an audit row. Every state-changing admin action leaves a trace so the
    dashboard's event log (SPEC §11) and both humans can reconstruct what happened."""
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, kind, json.dumps(detail), time.time()),
    )


def create_task(
    id: str | None,
    *,
    title: str,
    roles: list[str],
    mode: str = "contract",
    same_machine: bool = False,
    staging_url: str | None = None,
    dev_url: str | None = None,
) -> dict:
    """Create a task in the ``open`` state with the given cast.

    ``roles`` is a CAST DECLARATION: each entry either a plain role type (``"frontend"``,
    whose seat handle is derived — ``frontend``, then ``frontend-2`` on a repeat) or a
    mapping ``{"role": "frontend", "handle": "sarah"}`` naming the seat explicitly. The
    host is a seat like any other; listing ``frontend`` twice is now the ordinary way to
    say "two frontend developers" rather than an error.

    ``mode`` selects the workflow: ``'contract'`` (the default) runs the full
    propose/lock/deploy state machine; ``'debug'`` is a lightweight mode where two
    buddies just fix a problem and mark it resolved, with no contract required;
    ``'engagement'`` is the contract flow plus a client — an ``owner`` seat whose
    deliverables the team accepts before anything may be built, and whose agent
    verifies the result (see ``deliverables.py``).

    An engagement wants an ``owner`` seat in its cast, but that is NOT enforced here.
    The cast is deliberately not frozen at setup (``add_seat`` exists), so a host may
    create the engagement and invite the client afterwards; ``deliverables._assert_owner``
    is where the absence is caught, and it says exactly what to add.

    ``same_machine`` records the task's CONNECTIVITY (not the broker's auth mode):
    True only when the host proved everything lives on one box. It relaxes the
    ``staging_url`` rules for this task, so it defaults to False — a caller that
    doesn't say gets the strict remote validation. ``staging_url`` is the host-chosen
    deployment target the producer agent inherits when it proposes a contract.

    ``id`` may be falsy (``None``/``""``): the id is then derived from ``title`` via
    :func:`new_task_id`, so a human only has to supply a Title. An explicit id is
    used verbatim, and a duplicate explicit id is rejected explicitly (rather than
    surfacing a raw sqlite IntegrityError) so the CLI can print an actionable message.
    """
    if mode not in MODES:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {', '.join(repr(m) for m in MODES)}"
        )
    # Normalise the cast into (handles, {handle: role_type}). Repeating a role type is
    # now MEANINGFUL — it is how you say "two frontend developers" — and the derivation
    # gives the second one `frontend-2` with no thought from the host. What must still
    # be unique is the HANDLE, because that is what quorum and provenance key on.
    if not roles:
        raise ValueError("a task needs at least one non-empty role")
    handles, seat_roles = seats.normalise_cast(roles)
    if not handles:
        raise ValueError("a task needs at least one non-empty role")
    # `broker` is the broker's OWN voice: it authors pushes like contract_locked, and
    # both the agent envelope and the dashboard thread attribute them to that role. A
    # seat literally named 'broker' would be indistinguishable from the broker itself,
    # so the name is reserved — as a handle AND as a role type, since either would end
    # up rendered beside the broker's own notifications.
    if any(v.lower() == service.BROKER_ROLE for v in (*handles, *seat_roles.values())):
        raise ValueError(
            f"'{service.BROKER_ROLE}' is reserved for the broker's own notifications — "
            f"pick another role name"
        )
    if mode == "contract" and len(handles) < 2:
        # Model B: the producer is whoever proposes the contract (no hardcoded role).
        # A contract still needs at least two SEATS — one to produce and one to build
        # against it — else the workflow can never reach a check/verify. Two seats of
        # the SAME role type satisfy this: they are two people. Debug tasks skip the
        # state machine, so a single seat is fine there.
        raise ValueError("a contract task needs at least two roles (a producer and someone who builds against it)")
    conn = connect()
    try:
        if not id:
            # Derive from the title. Regenerate on the (vanishingly unlikely) suffix
            # collision so an auto-id never fails the way an explicit duplicate does.
            id = new_task_id(title)
            while conn.execute("SELECT 1 FROM tasks WHERE id = ?", (id,)).fetchone() is not None:
                id = new_task_id(title)
        elif conn.execute("SELECT 1 FROM tasks WHERE id = ?", (id,)).fetchone() is not None:
            raise ValueError(f"task '{id}' already exists")
        now = time.time()
        staging_url = (staging_url or "").strip() or None
        dev_url = (dev_url or "").strip() or None
        conn.execute(
            "INSERT INTO tasks (id, title, state, mode, roles_json, seat_roles_json, "
            "same_machine, staging_url, dev_url, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                id, title, "open", mode, json.dumps(handles), json.dumps(seat_roles),
                1 if same_machine else 0, staging_url, dev_url, now,
            ),
        )
        _write_event(conn, id, "task", {"text": f"Task created: {id}"})
        conn.commit()
        return {
            "id": id,
            "state": "open",
            "title": title,
            # `roles` is the list of SEAT HANDLES — same key, same shape, new meaning
            # (see db.SCHEMA's note on tasks.roles_json). `seat_roles` carries the other
            # half so a caller can render "backend ×2" without a second query.
            "roles": list(handles),
            "seat_roles": dict(seat_roles),
            "mode": mode,
            "same_machine": bool(same_machine),
            "staging_url": staging_url,
            "dev_url": dev_url,
        }
    finally:
        conn.close()


def _seat_for(conn: sqlite3.Connection, task: str, role: str) -> str:
    """The one SEAT a host's token names, or a refusal naming the candidates."""
    cast, seat_roles = seats.cast_of(conn, task)
    return service.resolve_seat(
        role, cast, seat_roles, seats.names_of(conn, task),
        task_id=task, binding="An invite fills ONE seat",
    )


def seat_for(task: str, role: str) -> str:
    """Read-only DISPLAY token: which seat ``role`` names on ``task``, spelled the way
    a human would type it, by exactly the rules :func:`mint_invite` mints against.

    The host types a token, not a handle — a role type, a seat, that seat's
    ``<type>-1`` address, or somebody's name — and the seat it lands on is often
    spelled differently. Echoing back what they typed would name the wrong thing on
    the one line that matters, so the CLI resolves through here.

    It returns the seat's ADDRESS rather than its stored handle, for the same reason
    the roster does: on a task that grew a second seat of a role type, the first seat's
    handle is that bare type, and printing ``@frontend`` would hand the host back the
    one token on the task that no longer names a single seat. The invite is still minted
    against the handle — this is display only.
    """
    conn = connect()
    try:
        return seats.address_of(conn, task, _seat_for(conn, task, role))
    finally:
        conn.close()


def mint_invite(task: str, role: str) -> tuple[str, str]:
    """Generate a single-use invite code for ONE SEAT on ``task``.

    Invites are per SEAT, not per role type: with two frontend seats, "an invite for
    frontend" would not say which one the redeemer takes. ``role`` is resolved to a
    single handle, and an input that names two seats is REFUSED with both named.

    It resolves through the ordinary :func:`service.resolve_seat`, with no exception of
    its own. There used to be one — an exact declared handle won here even when a role
    type was spelled the same way — because the first seat of a duplicated role type was
    handled ``frontend``, so refusing ``@frontend`` as ambiguous meant that seat could
    never be invited at all, and the display name that would disambiguate it does not
    exist until somebody redeems the invite you could not mint. That hole is closed
    where it was made: a cast declaring two frontends now numbers BOTH seats
    (``@frontend-1``, ``@frontend-2``), and a task that grew its second frontend later
    reaches the first through the derived alias ``@frontend-1``. Every seat has a token
    of its own, so "the role type always wins" needs no exception here.

    Only the code's sha256 is stored; the raw code is returned to the caller once and
    never persisted.

    Returns ``(raw_code, human_readable_expiry)``.
    """
    conn = connect()
    try:
        role = _seat_for(conn, task, role)

        code = new_invite_code(task)
        now = time.time()
        expires_at = now + INVITE_TTL_SECONDS
        conn.execute(
            "INSERT INTO invites (task_id, role, code_hash, created_at, expires_at, used_at) "
            "VALUES (?,?,?,?,?,NULL)",
            (task, role, sha256_hex(code), now, expires_at),
        )
        conn.commit()
        return code, _fmt_time(expires_at)
    finally:
        conn.close()


def add_seat(task: str, role_type: str, handle: str | None = None) -> dict:
    """Append a seat to a live task and mint its invite. Returns the seat + the code.

    Declaring the cast up front must not mean the cast is FIXED for the life of the
    task: a QA seat added on day three is ordinary, and forcing a new task instead
    would split the history of one piece of work in two. Creation is simply "add these
    N seats at once", so this shares its derivation rules with :func:`create_task` —
    choosing ``frontend`` on a task that already has one yields ``@frontend-2``.

    What is immutable is a seat that has already been USED: its handle is quoted in
    message history and signatures, so it is never renamed or re-pointed. (Revoking an
    agent frees its seat for RE-PAIRING, which already works — the live-seat index is
    partial on ``revoked_at IS NULL`` precisely so the historical row survives.)

    A new seat does NOT retroactively join existing todos: ``parties_json`` names who
    agreed, and a person who was not there did not agree.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT closed_at FROM tasks WHERE id = ?", (task,)).fetchone()
        if row is None:
            raise ValueError(f"unknown task '{task}'")
        if row["closed_at"] is not None:
            raise ValueError(f"task '{task}' is closed")
        if (role_type or "").strip().lower() == service.BROKER_ROLE:
            raise ValueError(
                f"'{service.BROKER_ROLE}' is reserved for the broker's own notifications — "
                f"pick another role name"
            )
        seat = seats.append_seat(conn, task, role_type, handle)
        _write_event(conn, task, "task", {"text": f"Seat added: {seat} ({role_type})"})
        conn.commit()
    finally:
        conn.close()
    # Mint AFTER the seat is committed — mint_invite validates against the declared
    # cast, so it has to be able to see the row we just wrote.
    code, expires = mint_invite(task, seat)
    audit.event("seat_added", task=task, role=seat)
    return {"task": task, "seat": seat, "role": role_type, "code": code, "expires": expires}


def add_guest(task: str, name: str, handle: str | None = None) -> dict:
    """Provision a GUEST seat — a non-technical human with no AI of their own.

    Every other seat is paired by an AI redeeming an invite; a guest has no AI to
    redeem one, so this creates her seat and her ``agents`` row directly, mints ONE
    credential — a viewer token *linked* to that seat via ``viewers.agent_id`` — and
    hands it back. The host shares ``/ui?v=<token>``; she opens it in a browser, the
    read-only dashboard grows a message box for her, and the linked seat is who her
    messages are stamped from. She installs nothing and never touches a terminal.

    The guest's ``agents`` row carries NO agent token (``token_hash`` NULL — she never
    calls ``/mcp``) and is ``ready = 1``: pre-flight grades the work an AI does for a
    role, and a guest does none. The narrow ``/guest/*`` routes are her only write
    surface, and the viewer link is the only thing that reaches them.

    Returns ``{task, seat, name, viewer_token}``; the caller builds the link.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a guest needs a display name (what to call her in the thread)")
    conn = connect()
    try:
        row = conn.execute("SELECT closed_at FROM tasks WHERE id = ?", (task,)).fetchone()
        if row is None:
            raise ValueError(f"unknown task '{task}'")
        if row["closed_at"] is not None:
            raise ValueError(f"task '{task}' is closed")
        # One display name per task (same rule as pairing): @ada must be unambiguous.
        name = seats.assert_name_free(conn, task, name)
        seat = seats.append_seat(conn, task, seats.GUEST_ROLE, handle)
        now = time.time()
        # role holds the role TYPE ("guest"); handle holds the seat. token_hash NULL —
        # she has no agent token. ready=1 — nothing about a guest is graded.
        #
        # The closed_at read above is a fast-path message; make the INSERT itself
        # conditional on the task still being open, exactly as pairing._redeem does. A
        # concurrent close_task either commits first (0 rows here → we abort) or commits
        # after (its sweep then revokes this row) — no live guest survives on a closed task.
        cur = conn.execute(
            "INSERT INTO agents (task_id, name, role, handle, token_hash, created_at, "
            "ready, readiness_status) "
            "SELECT ?,?,?,?,NULL,?,1,'passed' WHERE EXISTS "
            "(SELECT 1 FROM tasks WHERE id = ? AND closed_at IS NULL)",
            (task, name, seats.GUEST_ROLE, seat, now, task),
        )
        if cur.rowcount != 1:
            raise ValueError(f"task '{task}' is closed")
        agent_id = cur.lastrowid
        viewer_token = new_viewer_token()
        conn.execute(
            "INSERT INTO viewers (task_id, label, token_hash, created_at, agent_id) "
            "VALUES (?,?,?,?,?)",
            (task, name, sha256_hex(viewer_token), now, agent_id),
        )
        _write_event(conn, task, "token", {"text": f"Guest '{name}' added as {seat}"})
        conn.commit()
    finally:
        conn.close()
    audit.event("guest_added", task=task, role=seat)
    return {"task": task, "seat": seat, "name": name, "viewer_token": viewer_token}


def set_staging_url(
    task: str, url: str | None, todo: int | None = None
) -> dict:
    """Point a task (or ONE of its deliverables) at a deployment target. HOST ONLY.

    The target is CONFIGURATION, not an agreement. Changing it is therefore an ordinary
    host action with an event in the log — not a renegotiation, not a new consent flow,
    and deliberately NOT a tool an agent can request: the whole security value of moving
    the target out of the signed spec is that nothing an agent says can reach it. There
    is no "propose a new staging_url" path and there must not be one.

    It writes no contract and bumps no version. Every reader resolves the target live
    (``state.resolve_staging_url``), so an ngrok URL that rotated on a tunnel restart is
    fixed here, once, and every locked contract on the task points at the new one with
    no signature disturbed. That is the defect this replaces: a locked contract carrying
    a dead ``…ngrok-free.dev:3000`` was immutable inside a signed document, so the only
    sanctioned fix was to re-sign an identical shape.

    ``todo`` (the human's ``#N``) sets the PER-DELIVERABLE override; without it the
    task-level value moves. Passing an empty/None ``url`` CLEARS the value — clearing a
    todo's override makes it inherit the task's again, which is a real thing a host
    wants and is why "unset" and "set to blank" are the same word here.

    Returns ``{scope, task, todo, previous, staging_url, effective}`` — ``effective``
    being what a reader on that deliverable now resolves, which may come from the task
    when a todo override was just cleared.
    """
    from . import contracts, state

    url = (url or "").strip() or None
    conn = connect()
    try:
        _assert_task(conn, task)
        if url:
            # The SAME rules the broker has always applied to this value — https, and
            # never a private/reserved/metadata address (SSRF). Checked HERE, where the
            # human can fix it, because this is now the only door the value comes in by.
            #
            # Leniency comes from `state._task_is_same_machine`, not from the raw column:
            # it ALSO refuses to trust a same_machine row while this process is reachable
            # at a public origin, because a peer may then be off-box whatever the row
            # says. Reading the column directly would quietly drop that second condition.
            from .config import get_config

            errors = contracts.validate_staging_url(
                url,
                is_remote=get_config().is_remote,
                same_machine=state._task_is_same_machine(conn, task),
            )
            if errors:
                raise ValueError("; ".join(errors))
        todo_row = None
        if todo is not None:
            todo_row = todos.get_row(conn, task, todo)
            previous = todo_row["staging_url"]
            conn.execute("UPDATE todos SET staging_url = ? WHERE id = ?", (url, todo_row["id"]))
            where = f"todo #{todo_row['number']} ({todo_row['title']})"
        else:
            previous = conn.execute(
                "SELECT staging_url FROM tasks WHERE id = ?", (task,)
            ).fetchone()["staging_url"]
            conn.execute("UPDATE tasks SET staging_url = ? WHERE id = ?", (url, task))
            where = "the task"
        _write_event(
            conn, task, "task",
            {
                "text": (
                    f"Deployment target for {where}: "
                    f"{url or 'cleared'}"
                    + (f" (was {previous})" if previous else "")
                ),
                "staging_url": url,
                "previous": previous,
                "todo": todo_row["number"] if todo_row is not None else None,
            },
        )
        conn.commit()
        effective = state.resolve_staging_url(
            conn, task, todo_row["id"] if todo_row is not None else None
        )
    finally:
        conn.close()
    audit.event("staging_url_set", task=task, todo=todo if todo is not None else "*")
    return {
        "scope": "todo" if todo is not None else "task",
        "task": task,
        "todo": todo_row["number"] if todo_row is not None else None,
        "previous": previous,
        "staging_url": url,
        "effective": effective,
    }


def set_dev_url(task: str, url: str | None) -> dict:
    """Point a task at its LOCAL dev target — where the app runs during development
    (``http://localhost:3000``, a bare host, anything). HOST ONLY, task-level.

    Deliberately lenient, and that is the whole point: unlike :func:`set_staging_url` this
    is NOT the fetchable, SSRF-checked deployment target, so localhost and http are exactly
    what it exists for. It is CONFIGURATION, never an agreement, and no agent tool writes it
    — the same host-owned posture as staging_url. Passing empty/None clears it.

    Returns ``{task, previous, dev_url}``.
    """
    url = (url or "").strip() or None
    conn = connect()
    try:
        _assert_task(conn, task)
        previous = conn.execute(
            "SELECT dev_url FROM tasks WHERE id = ?", (task,)
        ).fetchone()["dev_url"]
        conn.execute("UPDATE tasks SET dev_url = ? WHERE id = ?", (url, task))
        _write_event(
            conn, task, "task",
            {
                "text": f"Local dev URL for the task: {url or 'cleared'}"
                + (f" (was {previous})" if previous else ""),
                "dev_url": url,
                "previous": previous,
            },
        )
        conn.commit()
    finally:
        conn.close()
    audit.event("dev_url_set", task=task)
    return {"task": task, "previous": previous, "dev_url": url}


def list_guests(task: str) -> list[dict]:
    """The live guest seats on a task — ``[{seat, name}]`` — for a host picking one to
    reissue a link for. Empty when the task has no guests."""
    conn = connect()
    try:
        return [
            {"seat": r["handle"], "name": r["name"]}
            for r in conn.execute(
                "SELECT handle, name FROM agents WHERE task_id=? AND role=? "
                "AND revoked_at IS NULL ORDER BY id",
                (task, seats.GUEST_ROLE),
            ).fetchall()
        ]
    finally:
        conn.close()


def reissue_guest_link(task: str, who: str) -> dict:
    """Mint a FRESH viewer link for an existing guest seat — same seat, a new token.

    The raw token is stored only HASHED, so a lost guest link cannot be recovered; this
    RE-ISSUES one instead. Crucially it reuses her existing seat (``viewers.agent_id`` →
    the same ``agents`` row), so her acceptances, signatures and message history stay hers
    — only the credential is new. ``who`` matches the guest by seat HANDLE or display NAME.

    HOST action — the ``/host/*`` surface and the CLI reach it; no agent tool does. Returns
    ``{task, seat, name, viewer_token}``; the caller builds the ``/ui?v=`` link, because only
    the caller knows the public origin (the tunnel) the guest must reach.
    """
    who = (who or "").strip()
    if not who:
        raise ValueError("name the guest to reissue for (seat handle or display name)")
    conn = connect()
    try:
        _assert_task(conn, task)
        row = conn.execute(
            "SELECT id, name, handle FROM agents WHERE task_id=? AND role=? "
            "AND revoked_at IS NULL AND (handle=? OR name=?)",
            (task, seats.GUEST_ROLE, who, who),
        ).fetchone()
        if row is None:
            have = ", ".join(
                f"{g['name']} (@{g['seat']})" for g in list_guests(task)
            ) or "none"
            raise ValueError(f"no live guest '{who}' on task '{task}'. Guests: {have}")
        token = new_viewer_token()
        conn.execute(
            "INSERT INTO viewers (task_id, label, token_hash, created_at, agent_id) "
            "VALUES (?,?,?,?,?)",
            (task, row["name"], sha256_hex(token), time.time(), row["id"]),
        )
        _write_event(
            conn, task, "token",
            {"text": f"Reissued a dashboard link for guest {row['name']} (@{row['handle']})"},
        )
        conn.commit()
    finally:
        conn.close()
    audit.event("guest_link_reissued", task=task, role=row["handle"])
    return {"task": task, "seat": row["handle"], "name": row["name"], "viewer_token": token}


def get_dev_url(task: str) -> str | None:
    """Read-only: the task's local dev URL, or None."""
    conn = connect()
    try:
        row = conn.execute("SELECT dev_url FROM tasks WHERE id = ?", (task,)).fetchone()
        return (row["dev_url"] or None) if row is not None else None
    finally:
        conn.close()


def get_staging_url(task: str, todo: int | None = None) -> dict:
    """Read-only: what a reader on this task/deliverable resolves right now, and why.

    Returns the ``task`` value, the todo ``override`` and the ``effective`` result, so
    a host can see WHICH of the two is in force rather than having to infer it.
    """
    from . import state

    conn = connect()
    try:
        _assert_task(conn, task)
        row = todos.get_row(conn, task, todo) if todo is not None else None
        return {
            "task": conn.execute(
                "SELECT staging_url FROM tasks WHERE id = ?", (task,)
            ).fetchone()["staging_url"],
            "override": row["staging_url"] if row is not None else None,
            "effective": state.resolve_staging_url(
                conn, task, row["id"] if row is not None else None
            ),
        }
    finally:
        conn.close()


def issue_host_viewer(label: str) -> str:
    """Create an all-tasks (``task_id = NULL``) viewer and return its RAW token.

    The host holds a distinct credential that sees every task, as opposed to a
    buddy's per-task viewer (SPEC §9). Only the sha256 is stored.
    """
    conn = connect()
    try:
        token = new_viewer_token()
        conn.execute(
            "INSERT INTO viewers (task_id, label, token_hash, created_at) VALUES (NULL,?,?,?)",
            (label, sha256_hex(token), time.time()),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def revoke_agent(name: str, task: str | None = None) -> int:
    """Revoke live agents named ``name``. Returns how many were revoked.

    Because ``name`` is buddy-chosen at pairing, the same name can exist on more than
    one task; pass ``task`` to scope the revocation so a host doesn't collaterally
    kill a same-named agent on an unrelated task. Only live agents
    (``revoked_at IS NULL``) are touched, so re-running is a no-op.
    """
    conn = connect()
    try:
        now = time.time()
        if task is None:
            cur = conn.execute(
                "UPDATE agents SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (now, name),
            )
        else:
            cur = conn.execute(
                "UPDATE agents SET revoked_at = ? WHERE name = ? AND task_id = ? "
                "AND revoked_at IS NULL",
                (now, name, task),
            )
        conn.commit()
        audit.event("revoke_agent", name=name, task=task or "*", count=cur.rowcount)
        return cur.rowcount
    finally:
        conn.close()


def extend_agent_tokens(
    task: str, *, hours: float = 24.0, never: bool = False
) -> list[dict]:
    """Push back (or lift) the expiry on every live agent token on ``task``.

    THE GAP THIS FILLS. A tunnelled broker defaults agent tokens to a 24h TTL so a leaked
    one self-expires, and the code that does it says "agents refresh with rotate_token".
    They cannot. `rotate_token` authenticates with the token it is replacing, so the moment
    one expires the agent is locked out of the only tool that could have saved it — and
    `report_status("stuck")` is equally gone, so it cannot even escalate. Nothing warns
    beforehand and the failure reads as "invalid or revoked", which sends everyone hunting a
    revocation that never happened.

    Recovery used to mean revoking the seat, minting a fresh invite, re-pairing, rewiring the
    MCP client and restarting the agent's session — losing its context — or editing the
    database by hand. This is the host-side move that was missing.

    ``never`` clears the expiry outright (the same state a same-machine broker mints), which
    is what you want for a long session or a demo. Only LIVE agents are touched: a revoked
    seat stays revoked, because "extend the tokens" must never quietly re-admit somebody a
    host deliberately cut off.

    Returns one row per agent it touched, so the caller can print who was extended rather
    than a bare count — a host needs to see that the seat they were worried about is in it.
    """
    conn = connect()
    try:
        _assert_task(conn, task)
        now = time.time()
        new_expiry = None if never else now + hours * 3600.0
        rows = conn.execute(
            "SELECT id, COALESCE(handle, role) AS seat, name, expires_at FROM agents "
            "WHERE task_id = ? AND revoked_at IS NULL ORDER BY id",
            (task,),
        ).fetchall()
        touched = [
            {
                "seat": r["seat"],
                "name": r["name"],
                "was": r["expires_at"],
                "now": new_expiry,
                # The one a host actually reacts to: this seat was already dead.
                "was_expired": r["expires_at"] is not None and r["expires_at"] < now,
            }
            for r in rows
        ]
        conn.execute(
            "UPDATE agents SET expires_at = ? WHERE task_id = ? AND revoked_at IS NULL",
            (new_expiry, task),
        )
        conn.commit()
        _write_event(
            conn, task, "token",
            {"text": (
                f"Agent tokens extended for {len(touched)} seat(s): "
                + ("no expiry" if never else f"+{hours:g}h")
            )},
        )
        conn.commit()
        audit.event(
            "extend_agent_tokens", task=task,
            count=len(touched), never=never, hours=None if never else hours,
        )
        return touched
    finally:
        conn.close()


def revoke_viewer(label: str, task: str | None = None) -> int:
    """Revoke live viewers with ``label`` (optionally scoped to ``task``). Returns
    how many were revoked."""
    conn = connect()
    try:
        now = time.time()
        if task is None:
            cur = conn.execute(
                "UPDATE viewers SET revoked_at = ? WHERE label = ? AND revoked_at IS NULL",
                (now, label),
            )
        else:
            cur = conn.execute(
                "UPDATE viewers SET revoked_at = ? WHERE label = ? AND task_id = ? "
                "AND revoked_at IS NULL",
                (now, label, task),
            )
        conn.commit()
        audit.event("revoke_viewer", label=label, task=task or "*", count=cur.rowcount)
        return cur.rowcount
    finally:
        conn.close()


def close_task(task: str) -> None:
    """Close a task and revoke ALL its agents and viewers (SPEC §9: "close kills
    everything for that task").

    One atomic sweep: stamp ``closed_at``, then revoke every still-live agent and
    per-task viewer. Instant and total — no credential scoped to this task survives.
    """
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task,)).fetchone() is None:
            raise ValueError(f"unknown task '{task}'")
        now = time.time()
        conn.execute("UPDATE tasks SET closed_at = ? WHERE id = ?", (now, task))
        conn.execute(
            "UPDATE agents SET revoked_at = ? WHERE task_id = ? AND revoked_at IS NULL",
            (now, task),
        )
        conn.execute(
            "UPDATE viewers SET revoked_at = ? WHERE task_id = ? AND revoked_at IS NULL",
            (now, task),
        )
        # Burn any invite that hasn't been redeemed yet — otherwise a buddy could
        # redeem a still-valid invite AFTER close and get live access to a closed
        # task (SPEC §9: close kills everything for that task).
        conn.execute(
            "UPDATE invites SET used_at = ? WHERE task_id = ? AND used_at IS NULL",
            (now, task),
        )
        _write_event(conn, task, "task", {"text": f"Task closed: {task}"})
        conn.commit()
    finally:
        conn.close()
    audit.event("task_closed", task=task)


def _assert_task(conn: sqlite3.Connection, task: str) -> None:
    """Fail with the task id, not with "no todo N" — the host mistyped one of two args
    and has to be told which."""
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task,)).fetchone() is None:
        raise ValueError(f"unknown task '{task}'")


def list_todos(task: str) -> tuple[list[dict], dict | None]:
    """Every todo on ``task`` plus its rollup, for the host's CLI view.

    Returns ``(todos, rollup)``; the rollup is ``None`` when the task has no live todos
    (``todos.rollup``), i.e. exactly when the task still runs its own state machine.
    Read-only — the host's one WRITE here is :func:`host_drop_todo`.
    """
    conn = connect()
    try:
        _assert_task(conn, task)
        return todos.get_todos(conn, task), todos.rollup(conn, task)
    finally:
        conn.close()


def host_drop_todo(task: str, todo: int, reason: str) -> tuple[dict, dict | None]:
    """Drop a todo unilaterally, as the HOST. The escape hatch, and the only one.

    A mutual ``drop_todo`` needs every named party's consent — including the party whose
    human went offline and is the whole reason you want it gone — so that path deadlocks
    on exactly the person who is missing. This is why the escape hatch is HUMAN and lives
    here and in the desktop app rather than as a tool: no peer may ever remove a peer,
    or the moment one objects to a shape the other removes it and locks without the
    dissent ("both sides sign" quietly becomes "whoever proposes wins").

    ``todos.host_drop_todo`` posts the who/why to the task thread as the BROKER's own
    seat, so the absent party's agent finds an explanation instead of vanished work.

    Returns ``(dropped_todo, task_rollup)`` — the rollup so the caller can print what the
    task now reports, and ``None`` there when the last live todo just went (the task is
    back on its own state machine).
    """
    conn = connect()
    try:
        _assert_task(conn, task)
        result = todos.host_drop_todo(conn, task, todo, reason)
        roll = todos.rollup(conn, task)
    finally:
        conn.close()
    audit.event("todo_dropped", task=task, todo=result["number"], by=todos.HOST)
    return result, roll


def host_remove_party(task: str, todo: int, seat: str, reason: str) -> tuple[dict, dict | None]:
    """Remove ONE unresponsive party from a todo, as the HOST. The smaller escape hatch.

    :func:`host_drop_todo` is the sledgehammer — it abandons the whole deliverable, which
    is wrong when the OTHER parties are present, cooperative, and still want it. The
    outage case ("mobile's agent is down") does not need the work thrown away; it needs
    the one name that will never answer taken off the party list so the quorum can be
    reached by the people who are actually here.

    HOST-ONLY, and for a stronger reason than the drop is. "Eject a peer" is the most
    abusable capability there is on a cross-org task: an agent that could do it would
    remove whoever objected and lock without the dissent. A human typing a command cannot
    be prompt-injected, which is why this sits beside `staging-url`, `add-seat`,
    `revoke-agent` and `close` rather than in the tool registry. The self-service half —
    a present, cooperative party taking itself off — IS an agent tool (`leave_todo`), and
    it has no seat argument at all.

    ``todos.host_remove_party`` posts the who/why to the thread as the BROKER's own seat,
    then re-runs the todo's quorum, so the removal actually unblocks rather than merely
    tidying a list.

    Returns ``(todo, task_rollup)``.
    """
    conn = connect()
    try:
        _assert_task(conn, task)
        result = todos.host_remove_party(conn, task, todo, seat, reason)
        roll = todos.rollup(conn, task)
    finally:
        conn.close()
    audit.event(
        "todo_party_removed", task=task, todo=result["number"], seat=seat, by=todos.HOST
    )
    return result, roll


def list_tasks() -> list[dict]:
    """All tasks, newest first, with the fields the CLI printer needs."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, state, roles_json, strikes, created_at, closed_at "
            "FROM tasks ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "roles": json.loads(r["roles_json"]),
                "seat_roles": seats.seat_roles_of(conn, r["id"]),
                "state": r["state"],
                "strikes": r["strikes"],
                "closed": r["closed_at"] is not None,
            }
            for r in rows
        ]
    finally:
        conn.close()


def roster(task: str) -> dict:
    """The task's CAST for the host's CLI — the SAME rows the dashboard panel and the
    agents' ``get_roster`` tool render, from ``seats.roster_summary``.

    One roster, N renderers. The shorthand list was hand-typed twice in ``ui.html``
    once and had already drifted (``pc [#N]`` vs ``pc #N``); this does not repeat it.
    """
    conn = connect()
    try:
        _assert_task(conn, task)
        return seats.roster_summary(conn, task)
    finally:
        conn.close()
