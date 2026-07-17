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
import sqlite3
import time

from .db import connect
from .identity import new_invite_code, new_viewer_token, sha256_hex

# Single-use invites live 15 minutes (SPEC §9). Short enough that a code lingering
# in a Slack scrollback is dead by the time anyone scans for it.
INVITE_TTL_SECONDS = 15 * 60


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _write_event(conn: sqlite3.Connection, task_id: str, kind: str, detail: dict) -> None:
    """Append an audit row. Every state-changing admin action leaves a trace so the
    dashboard's event log (SPEC §11) and both humans can reconstruct what happened."""
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, kind, json.dumps(detail), time.time()),
    )


def create_task(id: str, *, title: str, roles: list[str]) -> dict:
    """Create a task in the ``open`` state with the given fixed cast of roles.

    Duplicate ids are rejected explicitly (rather than surfacing a raw sqlite
    IntegrityError) so the CLI can print an actionable message.
    """
    if "backend" not in roles:
        # The state machine designates 'backend' as the role that deploys (SPEC §7);
        # a task without it can lock a contract but never reach backend_live, so the
        # workflow would deadlock. Reject at creation rather than strand it later.
        raise ValueError("roles must include 'backend' (the role that deploys)")
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (id,)).fetchone() is not None:
            raise ValueError(f"task '{id}' already exists")
        now = time.time()
        conn.execute(
            "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
            (id, title, "open", json.dumps(list(roles)), now),
        )
        _write_event(conn, id, "task", {"text": f"Task created: {id}"})
        conn.commit()
        return {"id": id, "state": "open", "title": title, "roles": list(roles)}
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


def revoke_agent(name: str) -> int:
    """Revoke every live agent with ``name``. Returns how many were revoked.

    Only live agents (``revoked_at IS NULL``) are touched, so re-running is a no-op
    and the returned count reflects agents actually killed by this call.
    """
    conn = connect()
    try:
        now = time.time()
        cur = conn.execute(
            "UPDATE agents SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
            (now, name),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def revoke_viewer(label: str) -> int:
    """Revoke every live viewer with ``label``. Returns how many were revoked."""
    conn = connect()
    try:
        now = time.time()
        cur = conn.execute(
            "UPDATE viewers SET revoked_at = ? WHERE label = ? AND revoked_at IS NULL",
            (now, label),
        )
        conn.commit()
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
