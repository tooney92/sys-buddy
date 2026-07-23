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

from . import audit, service
from .db import connect
from .identity import new_invite_code, new_viewer_token, sha256_hex

# Single-use invites live 15 minutes (SPEC §9). Short enough that a code lingering
# in a Slack scrollback is dead by the time anyone scans for it.
INVITE_TTL_SECONDS = 15 * 60

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


def create_task(id: str | None, *, title: str, roles: list[str], mode: str = "contract") -> dict:
    """Create a task in the ``open`` state with the given fixed cast of roles.

    ``mode`` selects the workflow: ``'contract'`` (the default) runs the full
    propose/lock/deploy state machine; ``'debug'`` is a lightweight mode where two
    buddies just fix a problem and mark it resolved, with no contract required.

    ``id`` may be falsy (``None``/``""``): the id is then derived from ``title`` via
    :func:`new_task_id`, so a human only has to supply a Title. An explicit id is
    used verbatim, and a duplicate explicit id is rejected explicitly (rather than
    surfacing a raw sqlite IntegrityError) so the CLI can print an actionable message.
    """
    if mode not in ("contract", "debug"):
        raise ValueError(f"unknown mode {mode!r}; expected 'contract' or 'debug'")
    # Normalise + validate the cast: trim, no blanks, no duplicates. The fixed-cast
    # rule allows one live agent per role, so a duplicate role is nonsensical — reject
    # it at creation rather than silently storing a role that can never be filled twice.
    roles = [r.strip() for r in roles]
    if not roles or any(not r for r in roles):
        raise ValueError("a task needs at least one non-empty role")
    if len(roles) != len(set(roles)):
        raise ValueError("task roles must be unique (no duplicates)")
    # `broker` is the broker's OWN voice: it authors pushes like contract_locked, and
    # both the agent envelope and the dashboard thread attribute them to that role. A
    # seat literally named 'broker' would be indistinguishable from the broker itself,
    # so the name is reserved.
    if any(r.lower() == service.BROKER_ROLE for r in roles):
        raise ValueError(
            f"'{service.BROKER_ROLE}' is reserved for the broker's own notifications — "
            f"pick another role name"
        )
    if mode == "contract" and len(roles) < 2:
        # Model B: the producer is whoever proposes the contract (no hardcoded role).
        # A contract still needs at least two roles — one to produce and one to build
        # against it — else the workflow can never reach a check/verify. Debug tasks
        # skip the state machine, so a single role is fine there.
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
        conn.execute(
            "INSERT INTO tasks (id, title, state, mode, roles_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (id, title, "open", mode, json.dumps(list(roles)), now),
        )
        _write_event(conn, id, "task", {"text": f"Task created: {id}"})
        conn.commit()
        return {"id": id, "state": "open", "title": title, "roles": list(roles), "mode": mode}
    finally:
        conn.close()


def mint_invite(task: str, role: str) -> tuple[str, str]:
    """Generate a single-use invite code for ``role`` on ``task``.

    Only the code's sha256 is stored; the raw code is returned to the caller once
    and never persisted. Validates that the task exists and the role is one the task
    actually declared — you cannot invite a role into a cast it has no seat for.

    Returns ``(raw_code, human_readable_expiry)``.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT roles_json FROM tasks WHERE id = ?", (task,)).fetchone()
        if row is None:
            raise ValueError(f"unknown task '{task}'")
        roles = json.loads(row["roles_json"])
        if role not in roles:
            raise ValueError(f"role '{role}' is not one of task '{task}' roles: {', '.join(roles)}")

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
                "state": r["state"],
                "roles": json.loads(r["roles_json"]),
                "strikes": r["strikes"],
                "closed": r["closed_at"] is not None,
            }
            for r in rows
        ]
    finally:
        conn.close()
