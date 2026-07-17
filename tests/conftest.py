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


def seed_agent(conn, task_id, role, name, token):
    """Insert an agent whose token_hash matches `token`. Returns the agent id."""
    cur = conn.execute(
        "INSERT INTO agents (task_id, name, role, token_hash, created_at) VALUES (?,?,?,?,?)",
        (task_id, name, role, sha256_hex(token), time.time()),
    )
    conn.commit()
    return cur.lastrowid


def seed_viewer(conn, label, token, task_id=None):
    cur = conn.execute(
        "INSERT INTO viewers (task_id, label, token_hash, created_at) VALUES (?,?,?,?)",
        (task_id, label, sha256_hex(token), time.time()),
    )
    conn.commit()
    return cur.lastrowid
