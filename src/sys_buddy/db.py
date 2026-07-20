"""SQLite storage, WAL mode, schema (SPEC §4).

WAL matters here: the dashboard polls read queries every ~3s while agents write
messages and the long-poll loop opens a connection every ~2s. WAL lets readers
never block the writer (and vice versa) — a known bug in the ``agent_bus.py``
predecessor was the absence of it.

Deviation from SPEC §4: delivery state is tracked per-recipient in ``deliveries``
rather than as ``delivered_at``/``acked_at`` columns on ``messages``. A task can
have 3+ roles (the spec's own ``signin`` example has backend+frontend+mobile) and
one message is read by every other agent on the task, so a single pair of columns
on the message row cannot represent "delivered to frontend but not mobile". The
per-recipient table preserves the crash-safety intent — split delivered/acked,
``ack_messages(ids)`` — while supporting N agents. See DECISIONS.md.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import get_config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    state       TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'contract',  -- 'contract' | 'debug'
    roles_json  TEXT NOT NULL,
    strikes     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    closed_at   REAL
);

CREATE TABLE IF NOT EXISTS contracts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    version     INTEGER NOT NULL,
    spec_json   TEXT NOT NULL,
    status      TEXT NOT NULL,            -- 'draft' | 'locked'
    proposed_by INTEGER REFERENCES agents(id),
    locked_at   REAL,
    created_at  REAL NOT NULL,
    UNIQUE(task_id, version)
);

CREATE TABLE IF NOT EXISTS contract_signatures (
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    signed_at   REAL NOT NULL,
    UNIQUE(contract_id, agent_id)
);

CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    token_hash  TEXT,                     -- sha256; NULL for implicit local identities
    pubkey      TEXT,
    created_at  REAL NOT NULL,
    revoked_at  REAL,
    expires_at  REAL,                     -- optional TTL; NULL = never expires
    ready       INTEGER NOT NULL DEFAULT 0, -- 0 = not yet passed the pre-flight
    readiness_status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'passed' | 'failed'
    readiness_report TEXT              -- JSON of the last attempt's per-question results
);

-- Fixed cast: at most one *live* agent per role. A partial index (not a plain
-- UNIQUE(task_id, role)) so that revoking an agent leaves its historical row in
-- place — keeping message provenance intact — while freeing the seat to be
-- re-paired. A blanket UNIQUE counted revoked rows and permanently bricked a role
-- once its agent was revoked.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_live_role
    ON agents(task_id, role) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS viewers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT REFERENCES tasks(id),  -- NULL = host (all tasks)
    label       TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    revoked_at  REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    from_agent_id INTEGER NOT NULL REFERENCES agents(id),
    type          TEXT NOT NULL,
    body_json     TEXT NOT NULL,
    state_at_send TEXT NOT NULL,
    created_at    REAL NOT NULL,
    to_role       TEXT                      -- optional directed recipient; NULL = broadcast to all roles
);

-- Per-recipient delivery tracking (see module docstring).
CREATE TABLE IF NOT EXISTS deliveries (
    message_id   INTEGER NOT NULL REFERENCES messages(id),
    agent_id     INTEGER NOT NULL REFERENCES agents(id),
    delivered_at REAL,
    acked_at     REAL,
    UNIQUE(message_id, agent_id)
);

CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    role        TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    kind        TEXT NOT NULL,            -- transition|lock|deploy|test|slack|token|task
    detail_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, id);
CREATE INDEX IF NOT EXISTS idx_deliveries_agent ON deliveries(agent_id, acked_at);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a WAL-friendly connection with a busy timeout and FK enforcement."""
    path = Path(db_path) if db_path is not None else get_config().db_path
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL is a persistent, database-level setting written once by init_db — no need
    # to re-issue it on every connection. foreign_keys and busy_timeout, however,
    # are per-connection and must be set each time.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path | str | None = None) -> Path:
    """Create the schema if absent. Idempotent. Returns the resolved db path."""
    path = Path(db_path) if db_path is not None else get_config().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        # Migration: add agents.expires_at to a db created before token TTLs existed.
        # (CREATE TABLE IF NOT EXISTS won't alter an existing table.)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN expires_at REAL")
        # Migration: add agents.ready to a db created before pre-flight readiness existed.
        if "ready" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
        # Migration: add readiness_status/report so a failed pre-flight is distinguishable
        # from "not attempted yet" (ready alone can't tell them apart) and the human can
        # read WHY it failed to coach the agent.
        if "readiness_status" not in cols:
            conn.execute(
                "ALTER TABLE agents ADD COLUMN readiness_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "readiness_report" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN readiness_report TEXT")
        # Migration: add tasks.mode to a db created before debug tasks existed.
        task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "mode" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'contract'")
        # Migration: add messages.to_role to a db created before directed messages existed.
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "to_role" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN to_role TEXT")
        conn.commit()
    finally:
        conn.close()
    # The db holds token hashes, messages, and contracts — restrict it (and its WAL/
    # SHM sidecars) to owner-only. Best-effort: a no-op where chmod isn't supported.
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass
    return path
