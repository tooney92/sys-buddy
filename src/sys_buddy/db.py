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
    -- 1 only when the HOST declared at setup that everything lives on ONE box
    -- (loopback broker origin, no public/tunnel URL). Defaults to 0 so anything
    -- created by another path is validated with the strict remote rules.
    same_machine INTEGER NOT NULL DEFAULT 0,
    staging_url TEXT,                       -- the host-chosen deployment target
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
    -- NULL = a TASK-level contract (everything before todos existed, and every task
    -- that never grows a todo). Non-NULL keys the contract to one deliverable, whose
    -- party list — not the task's full cast — is the signatory set (see todos.py).
    todo_id     INTEGER REFERENCES todos(id),
    locked_at   REAL,
    created_at  REAL NOT NULL,
    -- Versions stay a single MAX+1 sequence PER TASK even when contracts belong to
    -- different todos, so this constraint is untouched by todos and `lock_contract(3)`
    -- still names exactly one row. A todo's chain is therefore v1, v4, v7 rather than
    -- v1, v2, v3 — non-contiguous but unambiguous, and it needs no table rebuild on
    -- databases that predate todos.
    UNIQUE(task_id, version)
);

-- TODOS (v2): several deliverables under one task, each with its own contract chain
-- and its own proposed→locked→built→verified march. The TASK's state becomes a
-- ROLLUP of these (see todos.rollup) rather than something an agent sets.
--
-- `parties_json` is the decision the feature rests on: SEATS ≠ PARTICIPANTS. A todo
-- reuses the task's seats (you pair ONCE, at the task) and names WHICH of them it
-- binds. A seat not named here may READ the todo, is not bound by it, is not in its
-- contract's quorum, and does not block it.
CREATE TABLE IF NOT EXISTS todos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    title         TEXT NOT NULL,
    scope         TEXT NOT NULL,
    parties_json  TEXT NOT NULL,            -- roles bound by THIS todo (⊆ task roles)
    version       INTEGER NOT NULL DEFAULT 1,  -- bumped by repropose_todo; resets acceptances
    -- The per-todo march. Deliberately NOT the agreement stage: `pending/accepted/
    -- contracted/verified/dropped` is DERIVED from acceptances + contracts + this
    -- column (todos.status_of), so a rollup can never disagree with its parts.
    state         TEXT NOT NULL DEFAULT 'open',
    strikes       INTEGER NOT NULL DEFAULT 0,  -- per-todo ping-pong counter
    proposed_by   INTEGER REFERENCES agents(id),
    proposed_role TEXT NOT NULL,
    created_at    REAL NOT NULL,
    verified_at   REAL,
    -- `stuck` is an orthogonal FLAG, not a state: a stuck deliverable must not brick
    -- the whole task the way task-level `stuck` does, and the rollup still needs to
    -- know how far this todo actually got.
    stuck_at      REAL,
    stuck_reason  TEXT,
    dropped_at    REAL,
    dropped_by    TEXT,                     -- role that finalised the drop, or 'host'
    drop_reason   TEXT
);

-- Acceptances/declines as LISTS (mirroring contract_signatures) rather than a
-- `declined` status a state machine would then have to unwind. Keyed by VERSION so a
-- repropose resets consent without deleting the audit trail of who accepted v1.
CREATE TABLE IF NOT EXISTS todo_decisions (
    todo_id    INTEGER NOT NULL REFERENCES todos(id),
    version    INTEGER NOT NULL,
    role       TEXT NOT NULL,
    agent_id   INTEGER REFERENCES agents(id),
    decision   TEXT NOT NULL,               -- 'accepted' | 'declined'
    reason     TEXT,
    created_at REAL NOT NULL,
    UNIQUE(todo_id, version, role)
);

-- Dropping is MUTUAL: every named party consents. Version-independent — you are
-- abandoning the deliverable, not a revision of it.
CREATE TABLE IF NOT EXISTS todo_drop_consents (
    todo_id    INTEGER NOT NULL REFERENCES todos(id),
    role       TEXT NOT NULL,
    agent_id   INTEGER REFERENCES agents(id),
    reason     TEXT,
    created_at REAL NOT NULL,
    UNIQUE(todo_id, role)
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
    readiness_report TEXT,             -- JSON of the last attempt's per-question results
    listening_until  REAL,             -- presence EXPIRY: parked in wait_for_message while > now
    listening_since  REAL              -- start of the current listening streak (for "listening — 42m")
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
    to_role       TEXT,                     -- optional directed recipient; NULL = broadcast to all roles
    -- Which deliverable this message belongs to, or NULL for a task-level message
    -- (everything before todos existed, and every message on a task with none). This
    -- is what the dashboard's ⟨todo⟩ chip keys on — an authoritative id rather than a
    -- string scraped from the body.
    todo_id       INTEGER REFERENCES todos(id)
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

CREATE INDEX IF NOT EXISTS idx_todos_task ON todos(task_id, id);
-- NOTE: the index on contracts(todo_id) is created in init_db AFTER the migrations,
-- not here. `contracts` predates todos, so on an existing db the CREATE TABLE above is
-- a no-op and the todo_id column is added by ALTER later — indexing it here would run
-- inside executescript, before that ALTER, and fail with "no such column: todo_id".
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
        # Migration: add the presence columns to a db created before the listening
        # signal existed. An EXPIRY (not a boolean): every wait is bounded by
        # tools.WAIT_CAP, so `listening_until > now` self-heals across a broker crash
        # that never ran the clearing `finally`. listening_since carries the streak.
        if "listening_until" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN listening_until REAL")
        if "listening_since" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN listening_since REAL")
        # Migration: add tasks.mode to a db created before debug tasks existed.
        task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "mode" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'contract'")
        # Migration: connectivity + the host-chosen staging target. An existing task
        # predates the flag, so it gets 0 — i.e. the strict staging_url rules. Failing
        # closed here is deliberate: same-machine leniency must be positively declared.
        if "same_machine" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN same_machine INTEGER NOT NULL DEFAULT 0")
        if "staging_url" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN staging_url TEXT")
        # Migration: key contracts to a todo. NULL on every pre-existing row, which is
        # exactly right — they are TASK-level contracts and keep behaving as such. The
        # todos/todo_decisions/todo_drop_consents tables come from CREATE TABLE IF NOT
        # EXISTS above, so an old db needs nothing else: "do nothing" is the whole
        # migration story for a task that never grows a todo.
        contract_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(contracts)").fetchall()
        }
        if "todo_id" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN todo_id INTEGER REFERENCES todos(id)")
        # Migration: add messages.to_role to a db created before directed messages existed.
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "to_role" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN to_role TEXT")
        # Migration: key a message to a todo. NULL on every pre-existing row — a
        # task-level message, which is exactly what they were. The dashboard falls
        # back to scraping "todo #N" from the body for these old rows, so the chip
        # still renders on them; new rows carry the id directly.
        if "todo_id" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN todo_id INTEGER REFERENCES todos(id)")
        # Index contracts.todo_id only now that the column is guaranteed to exist —
        # whether it came from the fresh CREATE TABLE or the ALTER above. Kept out of
        # SCHEMA on purpose (see the note there).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_todo ON contracts(todo_id)")
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
