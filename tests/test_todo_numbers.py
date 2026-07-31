"""Specs for the human-facing todo NUMBER (`#N`) and the migration that backfills it.

The bug this exists to kill, observed live: `todos.id` was a global AUTOINCREMENT and
`#N` resolved straight against it, so three tasks holding one deliverable each read
`#1`, `#2`, `#3`. On the third task the ONLY todo was "#3" — a handle nobody can guess,
nobody can type, and no error message could usefully list. See
:func:`test_the_reported_bug_three_tasks_one_todo_each_all_read_hash_one`.

Two identifiers now, and they are not interchangeable:

* `todos.id` — global, the target of every foreign key (`contracts.todo_id`,
  `messages.todo_id`, `todo_decisions.todo_id`, `todo_drop_consents.todo_id`);
* `todos.number` — per TASK, from 1, what a person types. `todos.get_row` resolves it,
  `todos.row_by_id` resolves the other.

The heaviest tests here are the MIGRATION ones. An existing user's database is real data,
so the migration must be all-or-nothing (this is the first one in the project that creates
a constraint) and must delete nothing. The single most important test in the file is
:func:`test_migration_numbers_each_task_from_one_in_id_order`, which runs `init_db()` over
a v1.4.0-shaped database — several tasks, several todos each, interleaved global ids — and
proves the upgrade lands them 1..N per task.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from sys_buddy import api, db, service, state, todos, tools
from sys_buddy.config import Config, set_config
from tests.conftest import seed_agent, seed_task

# --------------------------------------------------------------------------- #
# a v1.4.0-shaped database: `todos` WITHOUT the `number` column
# --------------------------------------------------------------------------- #
# Copied from the v1.4.0 schema rather than derived from today's, because the whole
# point is to reproduce what is sitting on a user's disk right now. Only the tables the
# migration reads or references are needed; `init_db`'s CREATE TABLE IF NOT EXISTS makes
# the rest, and leaves this `todos` exactly as it finds it.
_V140_SCHEMA = """
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    state       TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'contract',
    roles_json  TEXT NOT NULL,
    strikes     INTEGER NOT NULL DEFAULT 0,
    same_machine INTEGER NOT NULL DEFAULT 0,
    staging_url TEXT,
    created_at  REAL NOT NULL,
    closed_at   REAL
);

CREATE TABLE todos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    title         TEXT NOT NULL,
    scope         TEXT NOT NULL,
    parties_json  TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'open',
    strikes       INTEGER NOT NULL DEFAULT 0,
    proposed_by   INTEGER,
    proposed_role TEXT NOT NULL,
    created_at    REAL NOT NULL,
    verified_at   REAL,
    stuck_at      REAL,
    stuck_reason  TEXT,
    dropped_at    REAL,
    dropped_by    TEXT,
    drop_reason   TEXT
);

CREATE TABLE todo_decisions (
    todo_id    INTEGER NOT NULL REFERENCES todos(id),
    version    INTEGER NOT NULL,
    role       TEXT NOT NULL,
    agent_id   INTEGER,
    decision   TEXT NOT NULL,
    reason     TEXT,
    created_at REAL NOT NULL,
    UNIQUE(todo_id, version, role)
);

CREATE TABLE todo_drop_consents (
    todo_id    INTEGER NOT NULL REFERENCES todos(id),
    role       TEXT NOT NULL,
    agent_id   INTEGER,
    reason     TEXT,
    created_at REAL NOT NULL,
    UNIQUE(todo_id, role)
);
"""

# (task_id, todo_id) in the order the rows were written — global ids INTERLEAVED across
# tasks, exactly as they come out of a shared AUTOINCREMENT while three collaborations
# run side by side. Nothing here is contiguous per task, which is the whole problem.
_LEGACY_ROWS = [
    ("signin", 1),
    ("payments", 2),
    ("signin", 3),
    ("reports", 4),
    ("payments", 5),
    ("signin", 6),
    ("payments", 7),
    ("reports", 8),
]

_EXPECTED_NUMBERS = {
    1: 1, 3: 2, 6: 3,      # signin
    2: 1, 5: 2, 7: 3,      # payments
    4: 1, 8: 2,            # reports
}


def _legacy_db(tmp_path, rows=_LEGACY_ROWS, name="legacy.db"):
    """Write a v1.4.0-shaped db with `rows` todos and a child row hanging off each."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(_V140_SCHEMA)
    for task_id in dict.fromkeys(t for t, _ in rows):
        conn.execute(
            "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
            (task_id, task_id, "open", json.dumps(["backend", "frontend"]), time.time()),
        )
    for task_id, todo_id in rows:
        conn.execute(
            "INSERT INTO todos (id, task_id, title, scope, parties_json, proposed_role, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (todo_id, task_id, f"todo {todo_id}", f"scope {todo_id}",
             json.dumps(["backend", "frontend"]), "backend", time.time()),
        )
        # A child row per todo, so "the migration keeps every foreign key pointing at the
        # same row" is something the assertions can actually check.
        conn.execute(
            "INSERT INTO todo_decisions (todo_id, version, role, decision, created_at) "
            "VALUES (?,1,'backend','accepted',?)",
            (todo_id, time.time()),
        )
    conn.commit()
    conn.close()
    return path


def _numbers(path):
    conn = db.connect(path)
    try:
        return {r["id"]: r["number"] for r in conn.execute("SELECT id, number FROM todos")}
    finally:
        conn.close()


def _has_column(path, table, column) -> bool:
    conn = db.connect(path)
    try:
        return column in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _dump(path) -> list[str]:
    """The db's full logical content + schema.

    Used instead of comparing raw bytes: `init_db` switches the file to WAL before any
    migration runs, so the header moves for reasons that have nothing to do with this
    change. The dump is the assertion that actually matters — no schema change, no row
    changed, nothing dropped.
    """
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# THE migration test
# --------------------------------------------------------------------------- #
def test_migration_numbers_each_task_from_one_in_id_order(tmp_path):
    """An existing user boots the new broker: every todo comes out numbered 1..N within
    its own task, in id (i.e. creation) order — and its `id` is untouched."""
    path = _legacy_db(tmp_path)
    assert not _has_column(path, "todos", "number")

    db.init_db(path)

    assert _numbers(path) == _EXPECTED_NUMBERS
    conn = db.connect(path)
    try:
        # Numbers are per task, contiguous from 1, and follow id order.
        for task_id in ("signin", "payments", "reports"):
            rows = conn.execute(
                "SELECT id, number FROM todos WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            assert [r["number"] for r in rows] == list(range(1, len(rows) + 1))
        # The ids the foreign keys point at did not move.
        assert [r["id"] for r in conn.execute("SELECT id FROM todos ORDER BY id")] == [
            tid for _, tid in sorted(_LEGACY_ROWS, key=lambda r: r[1])
        ]
    finally:
        conn.close()


def test_migration_deletes_nothing_and_keeps_every_foreign_key(tmp_path):
    """Users have live data. The migration ADDS a column and fills it; it must not drop
    or rewrite a row, and every child row must still resolve to the same deliverable."""
    path = _legacy_db(tmp_path)
    db.init_db(path)

    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM todos").fetchone()["n"] == len(
            _LEGACY_ROWS
        )
        # Each decision row still hangs off the todo it was written for…
        joined = conn.execute(
            "SELECT d.todo_id, t.task_id, t.number FROM todo_decisions d "
            "JOIN todos t ON t.id = d.todo_id ORDER BY d.todo_id"
        ).fetchall()
        assert len(joined) == len(_LEGACY_ROWS)
        assert {(r["todo_id"], r["number"]) for r in joined} == set(_EXPECTED_NUMBERS.items())
        # …and the titles/scopes were not touched on the way through.
        for r in conn.execute("SELECT id, title, scope FROM todos"):
            assert (r["title"], r["scope"]) == (f"todo {r['id']}", f"scope {r['id']}")
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path):
    """`init_db` runs on EVERY boot. The second and third runs must be no-ops — not a
    renumbering, and not an error from re-creating the index."""
    path = _legacy_db(tmp_path)
    db.init_db(path)
    first = _numbers(path)
    db.init_db(path)
    db.init_db(path)
    assert _numbers(path) == first == _EXPECTED_NUMBERS


def test_migration_creates_the_unique_index_and_it_bites(tmp_path):
    path = _legacy_db(tmp_path)
    db.init_db(path)

    conn = db.connect(path)
    try:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert db.TODO_NUMBER_INDEX in names
        # Two todos on one task cannot share a number…
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO todos (task_id, number, title, scope, parties_json, "
                "proposed_role, created_at) VALUES ('signin',1,'dupe','s','[]','backend',?)",
                (time.time(),),
            )
        conn.rollback()
        # …but two tasks obviously can, which is the entire point of scoping it.
        assert {
            r["number"]
            for r in conn.execute("SELECT number FROM todos WHERE number = 1")
        } == {1}
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM todos WHERE number = 1"
        ).fetchone()["n"] == 3
    finally:
        conn.close()


def test_migration_completes_a_half_upgraded_database(tmp_path):
    """The nastiest state: the column exists but some rows are still NULL (a boot that
    died between the ALTER and the backfill, or a hand-edited db).

    It matters because SQLite treats NULLs as DISTINCT in a unique index, so a
    half-numbered table sails straight past the constraint and those todos are simply
    unreachable by `#N`. Only the explicit NULL check catches it — so a re-run must
    COMPLETE the job, and must not hand out a number that already exists.
    """
    path = _legacy_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE todos ADD COLUMN number INTEGER")
    conn.execute("UPDATE todos SET number = 1 WHERE id = 1")   # signin's first, done
    conn.commit()
    conn.close()

    db.init_db(path)

    nums = _numbers(path)
    assert None not in nums.values()
    assert nums[1] == 1                       # the already-numbered row is left alone
    assert sorted(n for i, n in nums.items() if i in (1, 3, 6)) == [1, 2, 3]
    # The backfill offsets past MAX(number), so it can never collide with a number that
    # was already handed out.
    assert len({(t, n) for t, n in _task_number_pairs(path)}) == len(_LEGACY_ROWS)


def _task_number_pairs(path):
    conn = db.connect(path)
    try:
        return [(r["task_id"], r["number"]) for r in conn.execute(
            "SELECT task_id, number FROM todos"
        )]
    finally:
        conn.close()


def test_a_dropped_todo_is_numbered_too_and_never_deleted(tmp_path):
    """A dropped deliverable keeps its number: `#2` is quoted in the thread and in the
    event log, and the row is the humans' record of a decision."""
    path = _legacy_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE todos SET dropped_at = ?, dropped_by = 'host', drop_reason = 'descoped' "
        "WHERE id = 3",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = db.connect(path)
    try:
        row = conn.execute("SELECT * FROM todos WHERE id = 3").fetchone()
        assert row is not None                 # not deleted
        assert row["number"] == 2              # numbered in id order like any other
        assert row["dropped_at"] is not None
    finally:
        conn.close()


class _Sabotaged:
    """A connection proxy that lets one statement misbehave.

    ``sqlite3.Connection`` is an immutable type, so the failure has to be injected at the
    connection ``db.init_db`` opens rather than by patching the driver.
    """

    def __init__(self, real, trap):
        self._real = real
        self._trap = trap

    def execute(self, sql, *args):
        handled = self._trap(self._real, sql)
        return self._real.execute(sql, *args) if handled is None else handled

    def __getattr__(self, name):
        return getattr(self._real, name)


def _sabotage(monkeypatch, trap):
    real_connect = db.connect
    monkeypatch.setattr(
        db, "connect", lambda path=None: _Sabotaged(real_connect(path), trap)
    )


def _snapshot_without_numbering(monkeypatch, path) -> list[str]:
    """Run every OTHER migration, skip numbering, and dump.

    That is the baseline a failed numbering migration must leave behind: the rest of
    ``init_db`` has legitimately run and committed (it always did), so the assertion that
    means "nothing survived" is about ``todos`` and its rows, not about the empty tables
    ``CREATE TABLE IF NOT EXISTS`` adds on any boot.
    """
    monkeypatch.setattr(db, "_migrate_todo_numbers", lambda conn: None)
    db.init_db(path)
    monkeypatch.undo()
    return _dump(path)


def test_migration_rolls_back_whole_and_leaves_the_database_unchanged(monkeypatch, tmp_path):
    """A half-migrated db is far worse than a failed boot, so the three steps are ONE
    transaction. Break the LAST one (the index) and nothing may survive — no column, no
    numbers, not a single changed row."""
    path = _legacy_db(tmp_path)
    before = _snapshot_without_numbering(monkeypatch, path)

    def explode(real, sql):
        if db.TODO_NUMBER_INDEX in sql and "CREATE" in sql.upper():
            raise sqlite3.OperationalError("simulated disk failure")
        return None

    _sabotage(monkeypatch, explode)
    with pytest.raises(sqlite3.OperationalError):
        db.init_db(path)
    monkeypatch.undo()

    assert not _has_column(path, "todos", "number")
    assert _dump(path) == before


def test_the_null_check_refuses_to_constrain_a_half_numbered_table(monkeypatch, tmp_path):
    """The check the unique index CANNOT make for us: sabotage the backfill so rows are
    left NULL and the boot must FAIL loudly rather than constrain a table where some
    deliverables are unreachable by `#N`."""
    path = _legacy_db(tmp_path)
    before = _snapshot_without_numbering(monkeypatch, path)

    def skip_backfill(real, sql):
        if sql.strip().upper().startswith("UPDATE TODOS SET NUMBER"):
            return real.execute("SELECT 1")   # swallow it: nothing gets numbered
        return None

    _sabotage(monkeypatch, skip_backfill)
    with pytest.raises(RuntimeError, match="NULL number"):
        db.init_db(path)
    monkeypatch.undo()

    # …and it rolled back whole, so there is no half-filled column left behind.
    assert not _has_column(path, "todos", "number")
    assert _dump(path) == before


def test_a_fresh_database_gets_the_column_and_the_constraint(tmp_path):
    """No migration to run, but the constraint still has to exist — otherwise the first
    racing pair of proposals is the thing that discovers it is missing."""
    path = tmp_path / "fresh.db"
    db.init_db(path)
    assert _has_column(path, "todos", "number")
    conn = db.connect(path)
    try:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert db.TODO_NUMBER_INDEX in names
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# numbering, through the real flow
# --------------------------------------------------------------------------- #
def _agents(conn, task="signin", roles=("backend", "frontend")):
    seed_task(conn, task, roles=roles)
    conn.commit()
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{task}-{role}", f"sbk_{task}_{role}"),
            task_id=task,
            name=f"{task}-{role}",
            role=role,
        )
        for role in roles
    }


def _propose(conn, ag, title, parties=("backend", "frontend")) -> dict:
    return todos.propose_todo(conn, ag["backend"], title, f"scope of {title}", list(parties))


def test_numbers_start_at_one_on_every_task(conn):
    a = _agents(conn, "signin")
    b = _agents(conn, "payments")
    assert _propose(conn, a, "one")["number"] == 1
    assert _propose(conn, a, "two")["number"] == 2
    # A brand-new task starts over at 1 even though the broker is on its third todo.
    first_on_b = _propose(conn, b, "one")
    assert first_on_b["number"] == 1
    assert first_on_b["id"] == 3      # …while the internal id keeps counting globally


def test_the_reported_bug_three_tasks_one_todo_each_all_read_hash_one(conn):
    """The live proof: three tasks, one deliverable each, global ids 1/2/3. Before this
    change the third task's only todo was `#3`."""
    made = []
    for task in ("signin", "payments", "reports"):
        ag = _agents(conn, task)
        made.append(_propose(conn, ag, "the only deliverable"))
    assert [t["id"] for t in made] == [1, 2, 3]
    assert [t["number"] for t in made] == [1, 1, 1]


def test_hash_one_is_scoped_so_each_task_has_its_own(conn):
    """`#1` on signin and `#1` on payments are different rows, and acting on one never
    reaches the other — scoping is what makes a per-task number safe."""
    a = _agents(conn, "signin")
    b = _agents(conn, "payments")
    ta = _propose(conn, a, "signin work")
    tb = _propose(conn, b, "payments work")
    assert ta["number"] == tb["number"] == 1
    assert ta["id"] != tb["id"]

    assert todos.get_row(conn, "signin", 1)["id"] == ta["id"]
    assert todos.get_row(conn, "payments", 1)["id"] == tb["id"]

    todos.accept_todo(conn, a["frontend"], 1)
    assert todos.to_dict(conn, todos.get_row(conn, "signin", 1))["status"] == todos.ACCEPTED
    assert todos.to_dict(conn, todos.get_row(conn, "payments", 1))["status"] == todos.PENDING


def test_a_number_that_exists_on_another_task_is_refused(conn):
    a = _agents(conn, "signin")
    _agents(conn, "payments")
    _propose(conn, a, "one")
    _propose(conn, a, "two")
    with pytest.raises(ValueError, match="no todo #2 on task 'payments'"):
        todos.get_row(conn, "payments", 2)


def test_a_dropped_number_is_never_reused(conn):
    """Drop `#2` and the next todo is `#3`, not `#2`. MAX+1, never COUNT+1: `#2` is
    already quoted in the thread and in the event log."""
    ag = _agents(conn)
    _propose(conn, ag, "one")
    two = _propose(conn, ag, "two")
    todos.host_drop_todo(conn, "signin", two["number"], "descoped")
    assert _propose(conn, ag, "three")["number"] == 3
    # …and the dropped one keeps the number it was given.
    assert todos.get_row(conn, "signin", 2)["dropped_at"] is not None


def test_numbers_are_never_renumbered(conn):
    """Dropping `#1` does not slide `#2` down to `#1`. A human who typed `ready #2`
    yesterday must mean the same deliverable today."""
    ag = _agents(conn)
    one = _propose(conn, ag, "one")
    two = _propose(conn, ag, "two")
    todos.host_drop_todo(conn, "signin", one["number"], "descoped")
    assert todos.get_row(conn, "signin", 2)["id"] == two["id"]
    assert [t["number"] for t in todos.get_todos(conn, "signin")] == [1, 2]


def test_repropose_bumps_the_version_not_the_number(conn):
    """A new VERSION is a new reading of the same deliverable — `#N` is how the humans
    refer to it, so it must survive the reshape."""
    ag = _agents(conn)
    t = _propose(conn, ag, "one")
    again = todos.repropose_todo(conn, ag["backend"], t["number"], scope="narrower")
    assert (again["number"], again["version"]) == (t["number"], 2)


def test_the_api_exposes_both_the_number_and_the_id(conn):
    """`number` for the dashboard to PRINT, `id` for it to key selection and joins on."""
    ag = _agents(conn)
    _propose(conn, ag, "one")
    (row,) = api._task_detail(conn, "signin")["todos"]
    assert row["number"] == 1
    assert row["id"] == 1
    assert set(row) >= {"id", "number"}


# --------------------------------------------------------------------------- #
# the agent-facing surfaces: every `#N` a broker prints must be typeable back
# --------------------------------------------------------------------------- #
def _second_task_todos(conn):
    """Two tasks, so the second task's todos have ids that are NOT their numbers — the
    only setup in which a leaked `todos.id` is visible at all."""
    a = _agents(conn, "signin")
    b = _agents(conn, "payments")
    _propose(conn, a, "decoy one")
    _propose(conn, a, "decoy two")
    first = _propose(conn, b, "real one")
    second = _propose(conn, b, "real two")
    assert (first["id"], first["number"]) == (3, 1)
    assert (second["id"], second["number"]) == (4, 2)
    return b, first, second


def test_the_live_todo_list_in_errors_names_numbers(conn):
    """The error that lists the live todos is the agent's map out of the refusal — an id
    it cannot pass back to `todo=` would make it worse, not better."""
    ag, _first, _second = _second_task_todos(conn)
    with pytest.raises(ValueError) as e:
        state.report_status(conn, ag["backend"], "ready", "which one?")
    msg = str(e.value)
    assert "#1 (real one)" in msg and "#2 (real two)" in msg
    assert "#3" not in msg and "#4" not in msg


def test_next_step_shorthand_carries_the_number(conn):
    """`next_step` is what the dashboard prints for a human to type, computed off the
    internal row — so it is the most likely place for the internal id to leak."""
    ag, first, _second = _second_task_todos(conn)
    n = state.next_step(conn, "payments", first["id"])
    assert n["cmd"] == "yes #1"
    assert n["tool"] == "accept_todo(1)"
    assert "#3" not in n["text"]


def test_the_thread_prefix_and_the_broker_pushes_carry_the_number(conn):
    """The peer reads `[todo #N …]` and types `#N` back; a global id there is a handle
    that resolves to the wrong deliverable (or to nothing) on their side of the task."""
    ag, first, _second = _second_task_todos(conn)
    todos.accept_todo(conn, ag["frontend"], first["number"])
    r = state.propose_contract(
        conn, ag["backend"],
        {"version": 1, "endpoints": [{"method": "POST", "path": "/x"}],
         },
        first["number"],
    )
    for role in ("backend", "frontend"):
        state.lock_contract(conn, ag[role], r["version"], first["number"])
    state.report_status(conn, ag["backend"], "ready", "live", first["number"])

    bodies = [m["body"] for m in api._messages_for(conn, "payments")]
    assert any(b.startswith("[todo #1 real one]") for b in bodies)
    assert not any("todo #3" in b for b in bodies)


def test_the_event_log_renders_the_number(conn):
    ag, first, _second = _second_task_todos(conn)
    todos.host_drop_todo(conn, "payments", first["number"], "descoped")
    rendered = [e[2] for e in api._events_for(conn, "payments", "todo")]
    assert any(text.startswith("Todo #1 ") for text in rendered)
    assert not any("Todo #3" in text for text in rendered)


def _overlapping_keys_task(conn):
    """A task whose todos' ids and numbers OVERLAP but DISAGREE.

    The only fixture in which misreading a scraped ``#N`` produces a WRONG chip rather
    than merely no chip — which is why an aligned fixture (id == number) proves nothing
    here. signin takes id 1; payments then holds ids 2 and 3 as numbers 1 and 2. So on
    payments, "#2" is a valid NUMBER (id 3) *and* a valid ID (number 1), pointing at two
    different deliverables.
    """
    a = _agents(conn, "signin")
    b = _agents(conn, "payments")
    _propose(conn, a, "decoy")
    one = _propose(conn, b, "payments one")
    two = _propose(conn, b, "payments two")
    assert (one["id"], one["number"]) == (2, 1)
    assert (two["id"], two["number"]) == (3, 2)
    return b, one, two


def _prose_message(conn, ident, body, task="payments"):
    """A message with ``todo_id`` LEFT NULL that names a deliverable only in prose.

    Not a museum piece: ``service.post_message`` defaults ``todo_id`` to None, so this is
    the shape of any message that mentions a deliverable without being filed under one.
    """
    conn.execute(
        "INSERT INTO messages (task_id, from_agent_id, type, body_json, state_at_send, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (task, ident.agent_id, "status_update", json.dumps(body), "open", time.time()),
    )
    conn.commit()
    return [m for m in api._messages_for(conn, task) if m["type"] == "status_update"][-1]


def test_a_scraped_hash_n_is_read_as_a_number_not_an_id(conn):
    """`#2` in a body is what a HUMAN typed, so it means number 2 — the third todo in the
    db, id 3. Reading it as id 2 would chip the message onto `#1`: the wrong deliverable,
    silently, which is precisely the confusion per-task numbering exists to remove."""
    ag, one, two = _overlapping_keys_task(conn)
    m = _prose_message(conn, ag["frontend"], "blocked on todo #2 until the shape lands")
    assert m["todo"] == two["id"] == 3
    assert m["todo_number"] == 2
    # The id reading would have landed on the OTHER deliverable, not on nothing.
    assert m["todo"] != one["id"]


def test_a_scraped_hash_n_falls_back_to_the_id_for_pre_numbering_bodies(conn):
    """A row written before numbering quoted the GLOBAL id. No number 3 exists on
    payments, so the id reading is the only one left and it still earns a chip."""
    ag, _one, two = _overlapping_keys_task(conn)
    m = _prose_message(conn, ag["frontend"], "legacy note about todo #3")
    assert m["todo"] == two["id"] == 3
    assert m["todo_number"] == two["number"] == 2


def test_the_column_outranks_a_contradicting_body(conn):
    """``messages.todo_id`` is authoritative: prose is never allowed to overrule the id
    the broker stamped at post time."""
    ag, one, two = _overlapping_keys_task(conn)
    service.post_message(
        conn, ag["backend"], "status_update",
        "shipping this; ignore the stale 'todo #2' reference in here",
        todo_id=one["id"],
    )
    (m,) = [m for m in api._messages_for(conn, "payments") if m["type"] == "status_update"]
    assert m["todo"] == one["id"] and m["todo_number"] == one["number"]
    assert m["todo"] != two["id"]


def test_a_scraped_reference_to_nothing_gets_no_chip(conn):
    """Better a missing chip than one pointing at nothing — or at another task's work."""
    ag, _one, _two = _overlapping_keys_task(conn)
    m = _prose_message(conn, ag["frontend"], "vague memory of todo #99")
    assert "todo" not in m and "todo_number" not in m
    # …and a number that exists only on the OTHER task earns nothing either: payments
    # has no #3, even though signin's decoy sits at id 1 / number 1.
    m = _prose_message(conn, ag["frontend"], "and nothing about todo #4")
    assert "todo" not in m


def test_the_event_log_falls_back_to_the_id_on_pre_numbering_rows(conn):
    """Rows written before numbering carry only `todo_id`; showing that beats "#?"."""
    seed_task(conn, "signin")
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        ("signin", "todo",
         json.dumps({"action": "todo_proposed", "todo_id": 7, "by": "backend"}),
         time.time()),
    )
    conn.commit()
    (rendered,) = api._events_for(conn, "signin", "todo")
    assert rendered[2].startswith("Todo #7 ")


def test_signing_the_wrong_number_is_refused_by_number(conn):
    """The mis-sign guard compares what the agent TYPED against the todo's number."""
    ag, first, second = _second_task_todos(conn)
    for t in (first, second):
        todos.accept_todo(conn, ag["frontend"], t["number"])
    r = state.propose_contract(
        conn, ag["backend"],
        {"version": 1, "endpoints": [{"method": "POST", "path": "/x"}],
         },
        first["number"],
    )
    with pytest.raises(ValueError, match=r"belongs to todo #1 .*not todo #2"):
        state.lock_contract(conn, ag["backend"], r["version"], second["number"])


# --------------------------------------------------------------------------- #
# ONE selector: an agent-facing reply carries the number and NEVER the internal id
# --------------------------------------------------------------------------- #
# Two integers where only one is a valid selector is a footgun, not a convenience: an
# agent that passes the internal id back gets it read as a NUMBER by `todos.get_row`.
# Usually that is a clean "no todo #N", but on any task where a number of that value
# ALSO exists it silently resolves to a DIFFERENT deliverable. `_overlapping_keys_task`
# is the fixture where that is true — payments holds ids 2 and 3 as numbers 1 and 2 — so
# leaking `one`'s id (2) would point an agent at `two` (number 2). Every assertion below
# therefore needs that fixture; an aligned one (id == number) proves nothing.
_CONTRACT_SPEC = {
    "endpoints": [{"method": "POST", "path": "/x"}],
}


def _drive_the_flow(conn, ag, num) -> dict[str, dict]:
    """Every agent-facing reply from one deliverable's whole life, keyed by the call."""
    out: dict[str, dict] = {}
    out["accept_todo"] = tools._op_accept_todo(ag["frontend"], num)
    out["propose_contract"] = state.propose_contract(conn, ag["backend"], _CONTRACT_SPEC, num)
    version = out["propose_contract"]["version"]
    out["lock_contract_partial"] = state.lock_contract(conn, ag["backend"], version, num)
    out["lock_contract_locked"] = state.lock_contract(conn, ag["frontend"], version, num)
    out["get_contract"] = state.get_contract(conn, "payments", num)
    out["report_ready"] = state.report_status(conn, ag["backend"], "ready", "live", num)
    out["report_checked"] = state.report_status(conn, ag["frontend"], "checked", "works", num)
    out["report_verified"] = state.report_status(conn, ag["backend"], "verified", "e2e", num)
    return out


def test_no_agent_facing_reply_carries_the_internal_todo_id(conn):
    """The whole flow, every reply: `todo` is the number and `todo_id` is simply gone."""
    ag, one, two = _overlapping_keys_task(conn)
    replies = _drive_the_flow(conn, ag, one["number"])

    for call, r in replies.items():
        assert "todo_id" not in r, f"{call} still leaks the internal id"
        assert "id" not in r, f"{call} still leaks the internal id"
        # …and the one selector it does carry is the NUMBER, not the id it would have
        # been mistaken for (1, not 2 — and `two` is the row id 2 would have hit).
        # The todo WRITES answer with the full todo shape (`number`); the contract and
        # status calls answer with `todo`. Either way, exactly one integer, and it is #N.
        selector = r["todo"] if "todo" in r else r["number"]
        assert selector == one["number"] == 1, call
        assert selector != one["id"] == 2, call
    assert two["id"] == 3  # the fixture really does diverge


def test_the_write_replies_hand_back_a_number_that_round_trips(conn):
    """The proof that matters: feed a reply's own selector straight back and it must land
    on the SAME deliverable. The internal id would have landed on the other one."""
    ag, one, two = _overlapping_keys_task(conn)
    r = tools._op_accept_todo(ag["frontend"], one["number"])
    assert r["number"] == one["number"]
    # Round-trip the value the reply gave us.
    again = tools._op_repropose_todo(ag["backend"], r["number"], scope="narrower")
    assert again["title"] == one["title"]
    # The id reading would NOT have been a clean error — it resolves to `two`.
    assert todos.get_row(conn, "payments", one["id"])["id"] == two["id"]


def test_get_todos_shows_an_agent_the_number_only_while_the_api_keeps_the_id(conn):
    """The deliberate split: the dashboard keys its selection on `id` and joins
    `messages.todo_id` through it; an agent has no use for either and must not be
    offered the choice."""
    ag, one, two = _overlapping_keys_task(conn)

    for row in tools._op_get_todos("payments"):
        assert "id" not in row
        assert row["number"] in (one["number"], two["number"])

    api_rows = api._task_detail(conn, "payments")["todos"]
    assert {r["id"] for r in api_rows} == {one["id"], two["id"]}
    assert {r["number"] for r in api_rows} == {one["number"], two["number"]}


def test_the_todo_write_tools_all_strip_the_id(conn):
    """propose/accept/decline/repropose/drop — the five writes an agent calls, each of
    which returns the same `todos.to_dict` shape the dashboard also reads."""
    ag, _one, _two = _overlapping_keys_task(conn)
    made = tools._op_propose_todo(ag["backend"], "third", "scope", ["backend", "frontend"])
    n = made["number"]
    assert n == 3 and "id" not in made      # ids are at 4 by now; the number is 3
    for reply in (
        tools._op_decline_todo(ag["frontend"], n, "too broad"),
        tools._op_repropose_todo(ag["backend"], n, scope="narrower"),
        tools._op_accept_todo(ag["frontend"], n),
        tools._op_drop_todo(ag["backend"], n, "descoped"),
    ):
        assert "id" not in reply
        assert reply["number"] == n


def test_declining_and_reopening_a_contract_answer_with_the_number(conn):
    """The two contract paths that are not part of the happy march, and both used to
    return `todo_id` beside `todo`."""
    ag, one, _two = _overlapping_keys_task(conn)
    tools._op_accept_todo(ag["frontend"], one["number"])
    r = state.propose_contract(conn, ag["backend"], _CONTRACT_SPEC, one["number"])

    declined = state.decline_contract(conn, ag["frontend"], "wrong verb", one["number"])
    assert declined["todo"] == one["number"] and "todo_id" not in declined

    r = state.propose_contract(conn, ag["backend"], _CONTRACT_SPEC, one["number"])
    for role in ("backend", "frontend"):
        state.lock_contract(conn, ag[role], r["version"], one["number"])
    reopened = state.reopen_negotiations(conn, ag["backend"], "shape changed", one["number"])
    assert reopened["todo"] == one["number"] and "todo_id" not in reopened


def test_get_contract_on_a_todo_with_nothing_proposed_names_the_number(conn):
    """The `exists: False` branch is the one an agent hits when told "sign it" with
    nothing proposed — the reply is its map out, so it must be typeable."""
    ag, one, _two = _overlapping_keys_task(conn)
    r = state.get_contract(conn, "payments", one["number"])
    assert r["exists"] is False
    assert r["todo"] == one["number"] and "todo_id" not in r
    assert f"todo #{one['number']}" in r["note"]
    assert f"todo #{one['id']}" not in r["note"]


def test_the_reopen_event_log_line_prints_the_number(conn):
    """`reopen` is not one of api._EVENT_KINDS, so `_render_detail` falls through to its
    generic branch — which DUMPS THE WHOLE DETAIL AS JSON when there is no `text`. That
    printed the internal `todo_id` straight into the human event log."""
    ag, one, _two = _overlapping_keys_task(conn)
    tools._op_accept_todo(ag["frontend"], one["number"])
    r = state.propose_contract(conn, ag["backend"], _CONTRACT_SPEC, one["number"])
    for role in ("backend", "frontend"):
        state.lock_contract(conn, ag[role], r["version"], one["number"])
    state.reopen_negotiations(conn, ag["backend"], "shape changed", one["number"])

    (line,) = [e[2] for e in api._events_for(conn, "payments") if e[1] == "reopen"]
    assert line.startswith(f"Todo #{one['number']} ") or f"todo #{one['number']}" in line
    assert "todo_id" not in line
    assert f"todo #{one['id']}" not in line


def test_the_slack_ping_for_a_stuck_deliverable_names_the_number(conn, monkeypatch):
    """This one goes to a human's PHONE. A `#N` they cannot find on the dashboard (or
    that points at another deliverable) is worse than no ping."""
    from sys_buddy import slack
    from sys_buddy.config import Config, get_config, set_config

    sent: list[str] = []
    set_config(
        Config(mode="local", db_path=get_config().db_path,
               slack_webhook="https://hooks.example/x")
    )
    monkeypatch.setattr(slack, "notify", lambda text: sent.append(text) or "")

    ag, one, _two = _overlapping_keys_task(conn)
    state.report_status(conn, ag["backend"], "stuck", "the upstream is down", one["number"])

    assert sent
    assert all(f"todo #{one['number']}" in s for s in sent)
    assert not any(f"todo #{one['id']}" in s for s in sent)


def test_the_verified_slack_ping_names_the_number(conn, monkeypatch):
    """The other phone-bound ping: one deliverable done while others are still running."""
    from sys_buddy import slack
    from sys_buddy.config import Config, get_config, set_config

    sent: list[str] = []
    set_config(
        Config(mode="local", db_path=get_config().db_path,
               slack_webhook="https://hooks.example/x")
    )
    monkeypatch.setattr(slack, "notify", lambda text: sent.append(text) or "")

    ag, one, _two = _overlapping_keys_task(conn)
    _drive_the_flow(conn, ag, one["number"])

    verified = [s for s in sent if "VERIFIED" in s]
    assert verified
    assert all(f"todo #{one['number']}" in s for s in verified)
    assert not any(f"todo #{one['id']}" in s for s in verified)


def test_the_charter_and_the_tool_help_never_point_at_an_id(conn):
    """The briefing is where an agent learns which integer to pass back, so a sentence
    naming an `id` beside the number reintroduces the ambiguity the reply shapes just
    removed."""
    from sys_buddy import rules

    text = rules.RULES_OF_ENGAGEMENT
    assert "`number` from get_todos()" in text
    assert "the `id` beside it" not in text
    assert "todo_id" not in text


def test_a_racing_pair_of_proposals_cannot_share_a_number(tmp_path):
    """The unique index is the backstop for the MAX+1 read: two proposals that computed
    the same number cannot both land."""
    path = tmp_path / "race.db"
    set_config(Config(mode="local", db_path=path))
    db.init_db(path)
    conn = db.connect(path)
    try:
        ag = _agents(conn, "signin")
        _propose(conn, ag, "one")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO todos (task_id, number, title, scope, parties_json, "
                "proposed_role, created_at) VALUES ('signin',1,'racer','s','[]','backend',?)",
                (time.time(),),
            )
    finally:
        conn.rollback()
        conn.close()
