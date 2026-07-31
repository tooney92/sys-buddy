"""Shared test fixtures.

Every test gets a brand-new SQLite database in a temp dir, so tests never see
each other's data and can run in any order.
"""

from __future__ import annotations

import time

import pytest

from sys_buddy import db
from sys_buddy.config import Config, set_config
from sys_buddy.identity import sha256_hex


@pytest.fixture
def conn(tmp_path):
    """A connection to a freshly-initialised, isolated database (local mode)."""
    dbfile = tmp_path / "test.db"
    set_config(Config(mode="local", db_path=dbfile))
    db.init_db(dbfile)
    c = db.connect(dbfile)
    yield c
    c.close()


def seed_task(conn, task_id="signin", roles=("backend", "frontend"), state="open"):
    import json

    conn.execute(
        "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
        (task_id, task_id, state, json.dumps(list(roles)), time.time()),
    )
    conn.commit()
    return task_id


def seed_agent(conn, task_id, role, name, token, handle=None):
    """Insert an agent whose token_hash matches `token`. Returns the agent id.

    `handle` is the SEAT and defaults to `role`, which is what every task with one seat
    per role type carries — and what the migration backfills. Never leave it NULL: a
    NULL handle is a state the schema's unique index cannot catch (SQLite treats NULLs
    as distinct) and the migration explicitly refuses, so a fixture must not create one.
    """
    cur = conn.execute(
        "INSERT INTO agents (task_id, name, role, handle, token_hash, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, name, role, handle or role, sha256_hex(token), time.time()),
    )
    conn.commit()
    return cur.lastrowid


def seed_accepted_todo(
    conn,
    agents,
    task_id="signin",
    title="Sign-in endpoint",
    scope="POST /api/auth/login",
    parties=None,
):
    """Propose and fully accept a todo, and return its per-task ``#N``.

    There is exactly ONE kind of contract — an agreement about one deliverable — so a
    task with no todos has nothing to contract, and any test that reaches a contract
    needs this first. Built through the REAL ops (``propose_todo`` → ``accept_todo``)
    rather than an INSERT, so the fixture cannot drift from the flow it stands in for.

    ``agents`` is the ``{role: Identity}`` map the caller already has; ``parties``
    defaults to every role in it, which is what a test wants unless it is specifically
    exercising SEATS ≠ PARTICIPANTS.
    """
    from sys_buddy import todos

    roles = list(parties) if parties is not None else list(agents)
    proposer = roles[0]
    t = todos.propose_todo(conn, agents[proposer], title, scope, roles)
    # Proposing implies the proposer's own acceptance; everyone else must agree.
    for role in roles[1:]:
        todos.accept_todo(conn, agents[role], t["number"])
    return t["number"]


def seed_viewer(conn, label, token, task_id=None):
    cur = conn.execute(
        "INSERT INTO viewers (task_id, label, token_hash, created_at) VALUES (?,?,?,?)",
        (task_id, label, sha256_hex(token), time.time()),
    )
    conn.commit()
    return cur.lastrowid
