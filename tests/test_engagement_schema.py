"""Specs for the ENGAGEMENT MODE schema — see `docs/enhancements.md` items 1–4.

Eight new tables and three new columns, and the whole point of these tests is that an
existing database survives them untouched. That is not a given here: this schema has been
bitten three times by SQLite treating NULLs as DISTINCT in a unique index (a half-backfilled
column sails straight past the constraint), and once by a migration that read a column an
old database did not have — killing the boot on exactly the databases the migration existed
to rescue.

Nothing here is retrofitted onto an existing row, which is why the new tables can declare
``NOT NULL`` and ``UNIQUE`` honestly. ``todos.number`` could not: it reached existing
databases via ``ALTER TABLE``, which cannot add a NOT NULL column without a default.

The load-bearing claims:

* the eight tables and three columns appear;
* a pre-existing task, its todos, its contracts and its agents are **byte-identical**
  afterwards — a new feature must cost an untouched database nothing;
* ``init_db`` is re-entrant, because it runs on every single broker start;
* the deliverable numbering constraint is real, so ``#2`` can never be handed out twice
  on one task (the owner's own receipt file is named after that number and has to keep
  lining up with this row years later).
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from sys_buddy.db import connect, init_db


NEW_TABLES = {
    "deliverables",
    "deliverable_lists",
    "deliverable_decisions",
    "todo_deliverables",
    "guidelines",
    "specs",
    "verification_runs",
    "verification_results",
}


def _tables(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _cols(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _fingerprint(conn, table: str):
    """Rows as an ordered tuple list — a byte-for-byte comparison, not a count."""
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


# --- the tables and columns arrive ------------------------------------------
def test_the_eight_engagement_tables_are_created(tmp_path):
    path = init_db(tmp_path / "e.db")
    conn = connect(path)
    assert NEW_TABLES <= _tables(conn)


def test_the_three_new_columns_arrive_with_safe_defaults(tmp_path):
    """Defaults matter more than the columns: every existing row acquires them silently,
    so the wrong default would change behaviour on databases nobody migrated on purpose."""
    path = init_db(tmp_path / "e.db")
    conn = connect(path)
    assert {"guidelines_ready", "guidelines_report"} <= _cols(conn, "agents")
    assert "internal" in _cols(conn, "todos")

    conn.execute(
        "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
        ("t", "T", "open", '["backend"]', time.time()),
    )
    conn.execute(
        "INSERT INTO agents (task_id, name, role, token_hash, created_at) VALUES (?,?,?,?,?)",
        ("t", "a", "backend", "h", time.time()),
    )
    conn.execute(
        "INSERT INTO todos (task_id, number, title, scope, parties_json, proposed_role, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        ("t", 1, "x", "y", '["backend"]', "backend", time.time()),
    )
    conn.commit()
    # Not ready, not internal — a new gate must never open itself, and a todo must never
    # classify itself as housekeeping the owner is not shown.
    assert conn.execute("SELECT guidelines_ready FROM agents").fetchone()[0] == 0
    assert conn.execute("SELECT internal FROM todos").fetchone()[0] == 0


# --- an existing database is untouched --------------------------------------
def _seed_pre_engagement(path):
    """A database as it exists before any of this: a task, an agent, a todo, a contract."""
    conn = connect(path)
    now = time.time()
    # `seat_roles_json` is set deliberately: a task WITHOUT it is a pre-v2 row, and the
    # v2 backfill would fire on the next boot — a real migration doing its job, but it
    # would mask what this file is actually asserting. This is a current-shape task.
    conn.execute(
        "INSERT INTO tasks (id, title, state, roles_json, seat_roles_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "signin", "Sign-in", "open", '["backend", "frontend"]',
            '{"backend": "backend", "frontend": "frontend"}', now,
        ),
    )
    conn.execute(
        "INSERT INTO agents (task_id, name, role, handle, token_hash, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("signin", "dev", "backend", "backend", "hash", now),
    )
    conn.execute(
        "INSERT INTO todos (task_id, number, title, scope, parties_json, proposed_role, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        ("signin", 1, "api", "scope", '["backend", "frontend"]', "backend", now),
    )
    tid = conn.execute("SELECT id FROM todos").fetchone()["id"]
    conn.execute(
        "INSERT INTO contracts (task_id, todo_id, version, spec_json, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("signin", tid, 1, '{"endpoints": []}', "locked", now),
    )
    conn.commit()
    return conn


def test_an_existing_task_is_byte_identical_after_the_migration(tmp_path):
    """The strongest claim in this file. A team that never opens an engagement should not
    be able to tell this feature shipped."""
    path = init_db(tmp_path / "e.db")
    conn = _seed_pre_engagement(path)
    before = {t: _fingerprint(conn, t) for t in ("tasks", "todos", "contracts", "agents")}
    conn.close()

    init_db(path)  # the migration runs again on the next broker start

    conn = connect(path)
    for table, rows in before.items():
        assert _fingerprint(conn, table) == rows, f"{table} changed"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_init_db_is_re_entrant(tmp_path):
    """It runs on EVERY broker start, so the second run must be a no-op — including the
    eight CREATE TABLE IF NOT EXISTS and the three ALTERs."""
    path = init_db(tmp_path / "e.db")
    conn = _seed_pre_engagement(path)
    conn.close()
    for _ in range(3):
        init_db(path)
    conn = connect(path)
    assert NEW_TABLES <= _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_new_tables_start_empty_on_an_existing_database(tmp_path):
    """Migration ADDS the shape and invents no data. An engagement is something a person
    opens, never something a boot decides a task now has."""
    path = init_db(tmp_path / "e.db")
    conn = _seed_pre_engagement(path)
    conn.close()
    init_db(path)
    conn = connect(path)
    for table in NEW_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# --- the constraints are real -----------------------------------------------
def test_a_deliverable_number_cannot_be_handed_out_twice(tmp_path):
    """`#2` is quoted in messages, in the event log, and in the FILENAME of the owner's
    own receipt (`D2-contact-form.md`) on his machine. That has to keep pointing at this
    row, so the uniqueness is enforced by the database rather than by whoever inserts."""
    path = init_db(tmp_path / "e.db")
    conn = _seed_pre_engagement(path)
    now = time.time()
    conn.execute(
        "INSERT INTO deliverables (task_id, number, text, created_at) VALUES (?,?,?,?)",
        ("signin", 1, "Landing page", now),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deliverables (task_id, number, text, created_at) VALUES (?,?,?,?)",
            ("signin", 1, "Something else", now),
        )


def test_the_same_number_is_free_on_a_different_task(tmp_path):
    """Numbering is per task, like `todos.number` — three engagements each have a #1."""
    path = init_db(tmp_path / "e.db")
    conn = _seed_pre_engagement(path)
    now = time.time()
    conn.execute(
        "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
        ("other", "Other", "open", '["backend"]', now),
    )
    for task in ("signin", "other"):
        conn.execute(
            "INSERT INTO deliverables (task_id, number, text, created_at) VALUES (?,?,?,?)",
            (task, 1, "Landing page", now),
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM deliverables").fetchone()[0] == 2


def test_one_builder_decides_once_per_list_version(tmp_path):
    """A revision mints a new list version and everyone re-signs, so consent is keyed on
    the VERSION — two frontends each record their own row, and neither can vote twice."""
    path = init_db(tmp_path / "e.db")
    conn = _seed_pre_engagement(path)
    now = time.time()
    conn.execute(
        "INSERT INTO deliverable_lists (task_id, version, created_at) VALUES (?,?,?)",
        ("signin", 1, now),
    )
    lid = conn.execute("SELECT id FROM deliverable_lists").fetchone()["id"]
    conn.execute(
        "INSERT INTO deliverable_decisions (list_id, role, decision, created_at) "
        "VALUES (?,?,?,?)",
        (lid, "frontend-1", "accepted", now),
    )
    conn.execute(
        "INSERT INTO deliverable_decisions (list_id, role, decision, created_at) "
        "VALUES (?,?,?,?)",
        (lid, "frontend-2", "accepted", now),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deliverable_decisions (list_id, role, decision, created_at) "
            "VALUES (?,?,?,?)",
            (lid, "frontend-1", "pushed_back", now),
        )


def test_one_spec_per_dev_per_deliverable(tmp_path):
    """Two devs on one deliverable leave two specs and both are evaluated — attribution is
    the point. One dev cannot leave two competing claims about the same deliverable."""
    path = init_db(tmp_path / "e.db")
    conn = _seed_pre_engagement(path)
    now = time.time()
    conn.execute(
        "INSERT INTO deliverables (task_id, number, text, created_at) VALUES (?,?,?,?)",
        ("signin", 1, "Landing page", now),
    )
    did = conn.execute("SELECT id FROM deliverables").fetchone()["id"]
    aid = conn.execute("SELECT id FROM agents").fetchone()["id"]
    for role in ("frontend-1", "frontend-2"):
        conn.execute(
            "INSERT INTO specs (deliverable_id, agent_id, role, claim, how, stamped_json, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (did, aid, role, "built it", "on the home page", "{}", now),
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM specs").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO specs (deliverable_id, agent_id, role, claim, how, stamped_json, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (did, aid, "frontend-1", "also built it", "elsewhere", "{}", now),
        )
