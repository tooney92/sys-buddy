"""Specs for PER-TODO contract versions, and for the broker naming the right deliverable.

Two defects, one change, because the first forces the second — plus the migration that
retires the TASK-level contract, which shares the same table rebuild.

**Versions were per TASK.** `contracts.version` was a single `MAX(version)+1` sequence
across the whole task, so on a seeded task todo #1's first proposal was v1 and todo #2's
FIRST proposal was v2. On a six-todo task todo #6's first proposal read as **v9** — which
looks like eight renegotiations that never happened. Each todo owns its own contract chain
(`docs/todo-flow.md` §2), so a deliverable's first proposal must be v1.

**The version lookup ignored the todo.** `lock_contract` fetched its contract with
`WHERE task_id = ? AND version = ?` — the `todo=N` argument was not in the lookup at all,
and the todo cross-check sat BELOW the status checks. Observed live:
`lock_contract(version=1, todo=2)` fetched **todo #1's** contract, saw it locked, and
raised *"contract version 1 is already locked and immutable; propose a new version to
change it"*. Every word of that is true about a different deliverable, and its advice is
wrong for the one the caller asked about — todo #2 had a signable draft sitting there. It
could never produce a wrong signature (the cross-check still ran when the status checks
passed), so it is a DIAGNOSTICS defect — but a bad one: the broker blamed the wrong thing.
See :func:`test_signing_todo_two_is_not_refused_because_todo_one_is_locked`.

Once versions are per-todo the lookup HAS to be chain-scoped, which fixes the diagnostics
structurally rather than by adding another check.

The heaviest tests here are the MIGRATION ones, for the same reason as in
`test_todo_numbers.py`: an existing user's database is real data. This migration is harder
than the numbering one because SQLite cannot drop the table's declared
`UNIQUE(task_id, version)` — per-todo versions make two todos both hold a v1, so that
constraint does not merely mis-describe the data, it makes the second todo's first
proposal impossible. The table therefore has to be REBUILT, once, and nothing may be lost
on the way through: :func:`test_the_rebuild_deletes_nothing_and_keeps_every_signature`.

The same rebuild makes `contracts.todo_id` NOT NULL, because there is now exactly ONE kind
of contract: an agreement about one todo. The rows that used to hang off the task are real,
signed agreements, so they are ADOPTED by a new deliverable rather than deleted —
:func:`test_a_task_level_contract_is_adopted_by_a_new_todo`.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from sys_buddy import db, service, state, todos, tools
from sys_buddy.config import Config, set_config
from tests.conftest import seed_agent, seed_task

# --------------------------------------------------------------------------- #
# a pre-migration database: `contracts` WITH the old UNIQUE(task_id, version)
# --------------------------------------------------------------------------- #
# Copied from the shipped schema rather than derived from today's, because the point is to
# reproduce what is sitting on a user's disk right now — including the constraint that has
# to be shed. `todos` already carries `number` here: the numbering migration shipped
# first, so this is genuinely the previous release's shape and not a composite.
_PRE_SCHEMA = """
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

CREATE TABLE agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    token_hash  TEXT,
    pubkey      TEXT,
    created_at  REAL NOT NULL,
    revoked_at  REAL
);

CREATE TABLE todos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    number        INTEGER,
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

CREATE TABLE contracts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    version     INTEGER NOT NULL,
    spec_json   TEXT NOT NULL,
    status      TEXT NOT NULL,
    proposed_by INTEGER REFERENCES agents(id),
    todo_id     INTEGER REFERENCES todos(id),
    declined_by INTEGER REFERENCES agents(id),
    decline_reason TEXT,
    declined_at REAL,
    locked_at   REAL,
    created_at  REAL NOT NULL,
    UNIQUE(task_id, version)
);

CREATE TABLE contract_signatures (
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    signed_at   REAL NOT NULL,
    UNIQUE(contract_id, agent_id)
);
"""

# THE fixture shape the owner asked for: several todos on one task, each with SEVERAL
# versions, INTERLEAVED so the old task-scoped sequence is visible in the data. Reading
# down the `version` column tells you nothing about either deliverable's history; reading
# down a chain is the only thing that ever did.
#
#   payments (todo #1) → v1, v3, v5      ← "v5" implies four earlier revisions of payments
#   refunds  (todo #2) → v2, v4          ← "v2" implies a renegotiation that never happened
#   exports  (todo #3) → v6
#   task-level         → v7              ← a legacy pre-todo contract on the same task
#
# (contract_id, task_id, todo_id, old_version, status)
_PRE_CONTRACTS = [
    (1, "signin", 1, 1, "declined"),
    (2, "signin", 2, 2, "locked"),
    (3, "signin", 1, 3, "declined"),
    (4, "signin", 2, 4, "draft"),
    (5, "signin", 1, 5, "locked"),
    (6, "signin", 3, 6, "draft"),
    (7, "signin", None, 7, "locked"),
]

# The task-level row is ADOPTED by a new todo rather than deleted or left hanging: there is
# exactly one kind of contract now, an agreement about one deliverable, so a chain with no
# todo has nowhere to live. The adopting todo is MAX(number)+1 on the task — #4 here — and
# takes the next free id, so it never merges its history into an existing deliverable.
_ADOPTED_TODO_ID = 4
_ADOPTED_TODO_NUMBER = 4

# What each row's version must READ as once the chain owns the numbering. The adopted row
# is renumbered like any other chain: it is the only contract on its new todo, so it is v1.
_EXPECTED_VERSIONS = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 1, 7: 1}

# Where each row ends up. Identical to `_PRE_CONTRACTS` except for the adopted one.
_EXPECTED_TODO_IDS = {1: 1, 2: 2, 3: 1, 4: 2, 5: 1, 6: 3, 7: _ADOPTED_TODO_ID}


def _pre_db(tmp_path, name="pre.db", contracts=_PRE_CONTRACTS):
    """Write a pre-migration db: three todos on one task, contracts interleaved."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
        ("signin", "signin", "open", json.dumps(["backend", "frontend"]), time.time()),
    )
    for role in ("backend", "frontend"):
        conn.execute(
            "INSERT INTO agents (task_id, name, role, created_at) VALUES (?,?,?,?)",
            ("signin", f"signin-{role}", role, time.time()),
        )
    for num, title in ((1, "payments"), (2, "refunds"), (3, "exports")):
        conn.execute(
            "INSERT INTO todos (id, task_id, number, title, scope, parties_json, "
            "proposed_role, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (num, "signin", num, title, f"scope of {title}",
             json.dumps(["backend", "frontend"]), "backend", time.time()),
        )
    for cid, task_id, todo_id, version, status in contracts:
        conn.execute(
            "INSERT INTO contracts (id, task_id, version, spec_json, status, proposed_by, "
            "todo_id, locked_at, created_at) VALUES (?,?,?,?,?,1,?,?,?)",
            (cid, task_id, version, json.dumps({"endpoints": [{"path": f"/c{cid}"}],
                                                }),
             status, todo_id, time.time() if status == "locked" else None, time.time()),
        )
        # A signature per contract, so "the migration keeps every foreign key pointing at
        # the same row" is something the assertions can actually check. Signatures
        # reference contract_id, NOT version — this fixture is what proves that holds.
        conn.execute(
            "INSERT INTO contract_signatures (contract_id, agent_id, signed_at) VALUES (?,1,?)",
            (cid, time.time()),
        )
    conn.commit()
    conn.close()
    return path


def _versions(path) -> dict[int, int]:
    conn = db.connect(path)
    try:
        return {r["id"]: r["version"] for r in conn.execute("SELECT id, version FROM contracts")}
    finally:
        conn.close()


def _chain(path, todo_id) -> list[int]:
    conn = db.connect(path)
    try:
        return [
            r["version"]
            for r in conn.execute(
                "SELECT version FROM contracts WHERE todo_id IS ? ORDER BY id", (todo_id,)
            )
        ]
    finally:
        conn.close()


def _indexes(path) -> set[str]:
    conn = db.connect(path)
    try:
        return {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        conn.close()


def _dump(path) -> list[str]:
    """The db's full logical content + schema.

    Used instead of raw bytes: `init_db` switches the file to WAL before any migration
    runs, so the header moves for reasons unrelated to this change. The dump is the
    assertion that actually matters — no schema change, no row changed, nothing dropped.
    """
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# THE migration tests
# --------------------------------------------------------------------------- #
def test_migration_numbers_each_chain_from_one_in_id_order(tmp_path):
    """An existing user boots the new broker: every TODO-SCOPED contract comes out
    numbered 1..N within its own chain, in existing `id` (i.e. chronological) order — so
    "v2 was the second proposal on this deliverable" becomes true instead of accidental."""
    path = _pre_db(tmp_path)
    assert _versions(path) == {cid: v for cid, _t, _d, v, _s in _PRE_CONTRACTS}

    db.init_db(path)

    assert _versions(path) == _EXPECTED_VERSIONS
    assert _chain(path, 1) == [1, 2, 3]   # payments: was v1, v3, v5
    assert _chain(path, 2) == [1, 2]      # refunds:  was v2, v4 — its FIRST is now v1
    assert _chain(path, 3) == [1]         # exports:  was v6
    assert _chain(path, _ADOPTED_TODO_ID) == [1]  # the adopted legacy chain, also from v1


def test_a_task_level_contract_is_adopted_by_a_new_todo(tmp_path):
    """The legacy pre-todo chain gets somewhere to live rather than being deleted.

    Those rows are real, signed, locked agreements. A contract is now an agreement about
    ONE deliverable, so each task's task-level chain is adopted by a NEW todo whose party
    list is the task's full cast — which is exactly who a task-level contract bound — and
    whose scope says where it came from rather than inventing one nobody agreed.
    """
    path = _pre_db(tmp_path)
    db.init_db(path)

    conn = db.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM contracts WHERE todo_id IS NULL"
        ).fetchone()["n"] == 0
        t = conn.execute(
            "SELECT * FROM todos WHERE id = ?", (_ADOPTED_TODO_ID,)
        ).fetchone()
        # MAX(number)+1, never a hardcoded 1 — the task already had three deliverables.
        assert t["number"] == _ADOPTED_TODO_NUMBER
        assert json.loads(t["parties_json"]) == ["backend", "frontend"]
        assert "before this task was split into todos" in t["scope"]
        # The signature on the adopted contract still resolves — signatures key on
        # `contract_id`, so re-pointing `todo_id` cannot disturb them.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM contract_signatures WHERE contract_id = 7"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_the_rebuild_deletes_nothing_and_keeps_every_signature(tmp_path):
    """Shedding `UNIQUE(task_id, version)` needs a full table rebuild, which is the one
    step in this project that could plausibly lose data. Every row, every id and every
    signature must come out the other side — signatures key on `contract_id`, so they
    survive a renumbering only if the ids do."""
    path = _pre_db(tmp_path)
    db.init_db(path)

    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"] == len(
            _PRE_CONTRACTS
        )
        # Ids untouched — so every foreign key still points at the same contract.
        assert [r["id"] for r in conn.execute("SELECT id FROM contracts ORDER BY id")] == [
            cid for cid, *_ in _PRE_CONTRACTS
        ]
        # Each signature still resolves to the contract it was written for, and that
        # contract still carries its own spec and status.
        joined = conn.execute(
            "SELECT s.contract_id, c.todo_id, c.version, c.status, c.spec_json "
            "FROM contract_signatures s JOIN contracts c ON c.id = s.contract_id "
            "ORDER BY s.contract_id"
        ).fetchall()
        assert len(joined) == len(_PRE_CONTRACTS)
        for r, (cid, _task, _todo_id, _old, status) in zip(joined, _PRE_CONTRACTS):
            assert r["contract_id"] == cid
            # Unchanged for every todo-scoped row; the legacy one moved onto its adopter.
            assert r["todo_id"] == _EXPECTED_TODO_IDS[cid]
            assert r["status"] == status
            assert json.loads(r["spec_json"])["endpoints"][0]["path"] == f"/c{cid}"
            assert r["version"] == _EXPECTED_VERSIONS[cid]
        # …and nothing is orphaned.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_the_rebuild_sheds_the_task_wide_unique_and_keeps_autoincrement(tmp_path):
    """The old constraint made todo #2's first proposal IMPOSSIBLE (it would collide with
    todo #1's v1), so it has to be gone — and the next insert must still get a fresh id
    above the copied ones rather than reusing one."""
    path = _pre_db(tmp_path)
    db.init_db(path)

    conn = db.connect(path)
    try:
        origins = {r["origin"] for r in conn.execute("PRAGMA index_list(contracts)")}
        assert "u" not in origins, "the declared UNIQUE(task_id, version) is still there"
        cur = conn.execute(
            "INSERT INTO contracts (task_id, version, spec_json, status, todo_id, created_at) "
            "VALUES ('signin', 4, '{}', 'draft', 1, ?)",
            (time.time(),),
        )
        assert cur.lastrowid > max(cid for cid, *_ in _PRE_CONTRACTS)
    finally:
        conn.rollback()
        conn.close()


def test_one_index_constrains_every_chain_and_the_null_partition_is_gone(tmp_path):
    """A chain is exactly one todo, so ONE plain `UNIQUE(todo_id, version)` says it all.

    There used to be two PARTIAL indexes here because a second, task-level kind of contract
    lived on `todo_id IS NULL` rows, and SQLite treats NULLs as DISTINCT in a unique index —
    so a single index would have constrained the todo chains and silently ignored every
    task-level row. Retiring that kind retires the trap: the column is NOT NULL, and the
    migration DROPS the old task-level index rather than leaving a dead constraint behind.
    """
    path = _pre_db(tmp_path)
    db.init_db(path)
    idx = _indexes(path)
    assert db.CONTRACT_TODO_VERSION_INDEX in idx
    assert db.CONTRACT_TASK_VERSION_INDEX not in idx

    conn = db.connect(path)
    try:
        # A duplicate inside a chain is refused…
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contracts (task_id, version, spec_json, status, todo_id, "
                "created_at) VALUES ('signin', 1, '{}', 'draft', 1, ?)",
                (time.time(),),
            )
        conn.rollback()
        # …and a contract with no deliverable cannot be written at all, which is what
        # makes the plain index sufficient: there is no null partition to miss.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contracts (task_id, version, spec_json, status, todo_id, "
                "created_at) VALUES ('signin', 9, '{}', 'draft', NULL, ?)",
                (time.time(),),
            )
        conn.rollback()
        # …but the SAME version on two different todos is the entire design, not a clash.
        conn.execute(
            "INSERT INTO contracts (task_id, version, spec_json, status, todo_id, created_at) "
            "VALUES ('signin', 3, '{}', 'draft', 2, ?)",
            (time.time(),),
        )
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM contracts WHERE version = 3"
        ).fetchone()["n"] == 2
    finally:
        conn.rollback()
        conn.close()


def test_migration_is_idempotent(tmp_path):
    """`init_db` runs on EVERY boot. The second and third runs must be no-ops — not a
    re-renumbering, not a second rebuild, and not an error from re-creating an index."""
    path = _pre_db(tmp_path)
    db.init_db(path)
    first = _dump(path)
    db.init_db(path)
    db.init_db(path)
    assert _versions(path) == _EXPECTED_VERSIONS
    assert _dump(path) == first


def test_a_fresh_database_gets_the_constraints_without_a_rebuild(tmp_path):
    """No migration to run, but the index still has to exist — otherwise the first racing
    pair of proposals is the thing that discovers it is missing."""
    path = tmp_path / "fresh.db"
    db.init_db(path)
    assert db.CONTRACT_TODO_VERSION_INDEX in _indexes(path)
    conn = db.connect(path)
    try:
        assert "u" not in {r["origin"] for r in conn.execute("PRAGMA index_list(contracts)")}
        # …and `todo_id` is NOT NULL from the CREATE TABLE, not only after a rebuild.
        assert all(
            r["notnull"]
            for r in conn.execute("PRAGMA table_info(contracts)")
            if r["name"] == "todo_id"
        )
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


def _snapshot_without_the_version_migration(monkeypatch, path) -> list[str]:
    """Run every OTHER migration, skip this one, and dump.

    That is the baseline a failed run must leave behind: the rest of ``init_db``
    has legitimately run and committed (it always did), so "nothing survived" is an
    assertion about `contracts` and its rows, not about tables `CREATE TABLE IF NOT
    EXISTS` adds on any boot.
    """
    monkeypatch.setattr(db, "_migrate_contract_versions", lambda conn: None)
    db.init_db(path)
    monkeypatch.undo()
    return _dump(path)


def test_migration_rolls_back_whole_when_the_index_step_throws(monkeypatch, tmp_path):
    """A half-migrated db is far worse than a failed boot, so rebuild + renumber +
    constrain are ONE transaction. Break the LAST step and nothing may survive — not the
    rebuilt table, not a single renumbered row."""
    path = _pre_db(tmp_path)
    before = _snapshot_without_the_version_migration(monkeypatch, path)

    def explode(real, sql):
        if db.CONTRACT_TODO_VERSION_INDEX in sql and "CREATE" in sql.upper():
            raise sqlite3.OperationalError("simulated disk failure")
        return None

    _sabotage(monkeypatch, explode)
    with pytest.raises(sqlite3.OperationalError):
        db.init_db(path)
    monkeypatch.undo()

    # The old constraint is still there, the versions are the old ones, and the dump is
    # byte-for-byte what it was — i.e. the rebuild rolled back too.
    conn = db.connect(path)
    try:
        assert "u" in {r["origin"] for r in conn.execute("PRAGMA index_list(contracts)")}
    finally:
        conn.close()
    assert _versions(path) == {cid: v for cid, _t, _d, v, _s in _PRE_CONTRACTS}
    assert _dump(path) == before
    # …and the next honest boot completes the job rather than skipping it.
    db.init_db(path)
    assert _versions(path) == _EXPECTED_VERSIONS


def test_the_duplicate_check_refuses_to_constrain_a_bad_table(monkeypatch, tmp_path):
    """The check the indexes CANNOT make for us before they exist: a `CREATE UNIQUE INDEX`
    over a table that already violates it fails with a bare "UNIQUE constraint failed",
    naming neither the chain nor the version. Sabotage the renumber so genuine duplicates
    survive, and the boot must fail with something a human can act on."""
    # A genuinely bad table: two rows claiming v1 on the SAME deliverable, which the old
    # UNIQUE(task_id, version) could not even represent — so the fixture drops it, exactly
    # as a db that has already been rebuilt once would look.
    path = _pre_db(tmp_path, contracts=[
        (1, "signin", 1, 1, "locked"),
        (2, "signin", 1, 2, "draft"),
    ])
    conn = sqlite3.connect(path)
    # legacy_alter_table so the RENAME does not rewrite `contract_signatures`' foreign key
    # to point at `c_old` — which the DROP would then leave dangling, and the rebuild's
    # `foreign_key_check` would (correctly) refuse over damage the FIXTURE did.
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.executescript(
        "ALTER TABLE contracts RENAME TO c_old;"
        "CREATE TABLE contracts (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "version INTEGER NOT NULL, spec_json TEXT NOT NULL, status TEXT NOT NULL, "
        "proposed_by INTEGER, todo_id INTEGER, declined_by INTEGER, decline_reason TEXT, "
        "declined_at REAL, locked_at REAL, created_at REAL NOT NULL);"
        "INSERT INTO contracts SELECT * FROM c_old;"
        "DROP TABLE c_old;"
        "UPDATE contracts SET version = 1;"          # both rows now claim todo #1's v1
    )
    conn.commit()
    conn.close()
    before = _snapshot_without_the_version_migration(monkeypatch, path)

    def skip_renumber(real, sql):
        if sql.strip().upper().startswith("UPDATE CONTRACTS SET VERSION"):
            return real.execute("SELECT 1")   # swallow it: nothing gets renumbered
        return None

    _sabotage(monkeypatch, skip_renumber)
    with pytest.raises(RuntimeError, match="duplicate versions"):
        db.init_db(path)
    monkeypatch.undo()

    # It names the chain and the version, so the report is actionable…
    _sabotage(monkeypatch, skip_renumber)
    with pytest.raises(RuntimeError) as e:
        db.init_db(path)
    monkeypatch.undo()
    assert "todo #1" in str(e.value) and "v1" in str(e.value)
    # …and it rolled back whole, so no index was created over the bad data.
    assert db.CONTRACT_TODO_VERSION_INDEX not in _indexes(path)
    assert _dump(path) == before


def test_several_task_level_versions_are_adopted_onto_ONE_todo_as_a_chain(tmp_path):
    """A task-level chain with several versions is one agreement's history, so it is
    adopted whole onto ONE new deliverable and renumbered 1..N in id order — not split
    across a todo each, which would read as several unrelated agreements."""
    path = _pre_db(tmp_path, contracts=[
        (1, "signin", None, 1, "declined"),
        (2, "signin", None, 2, "locked"),
    ])
    db.init_db(path)

    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT id, todo_id, version, status FROM contracts ORDER BY id"
        ).fetchall()
        adopted = {r["todo_id"] for r in rows}
        assert len(adopted) == 1, "one agreement, one deliverable"
        assert [(r["id"], r["version"], r["status"]) for r in rows] == [
            (1, 1, "declined"), (2, 2, "locked"),
        ]
        # Only one todo was created — the task already had three, so this is #4.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM todos WHERE task_id = 'signin'"
        ).fetchone()["n"] == 4
    finally:
        conn.close()


def test_a_dropped_todos_chain_is_renumbered_too_and_never_deleted(tmp_path):
    """A dropped deliverable's contracts are the humans' record of a decision. They are
    renumbered like any other chain and not one row is removed."""
    path = _pre_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE todos SET dropped_at = ?, dropped_by = 'host', drop_reason = 'descoped' "
        "WHERE number = 2",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    assert _chain(path, 2) == [1, 2]
    assert _versions(path) == _EXPECTED_VERSIONS


# --------------------------------------------------------------------------- #
# numbering, through the real flow
# --------------------------------------------------------------------------- #
_SPEC = {"endpoints": [{"method": "POST", "path": "/x"}]}


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


def _accepted(conn, ag, title, parties=("backend", "frontend")) -> dict:
    t = todos.propose_todo(conn, ag["backend"], title, f"scope of {title}", list(parties))
    for role in parties:
        if role != "backend":
            todos.accept_todo(conn, ag[role], t["number"])
    return t


def _overlapping_keys_task(conn):
    """A task whose todos' ids and numbers OVERLAP but DISAGREE — the same fixture
    `test_todo_numbers.py` uses, and for the same reason: an aligned fixture (id ==
    number) cannot tell a chain-scoped lookup from an id-scoped one, so it proves nothing.

    signin takes id 1; payments then holds ids 2 and 3 as numbers 1 and 2. So on payments,
    "#2" is a valid NUMBER (id 3) *and* a valid ID (number 1), pointing at two different
    deliverables.
    """
    a = _agents(conn, "signin")
    b = _agents(conn, "payments")
    _accepted(conn, a, "decoy")
    one = _accepted(conn, b, "payments one")
    two = _accepted(conn, b, "payments two")
    assert (one["id"], one["number"]) == (2, 1)
    assert (two["id"], two["number"]) == (3, 2)
    return b, one, two


def test_every_todo_first_proposal_is_v1_even_on_the_sixth(conn):
    """The reported bug, at the size it was reported at: six deliverables on one task, and
    the sixth's first proposal used to read as v9."""
    ag = _agents(conn, roles=("backend", "frontend"))
    firsts = []
    for i in range(1, 7):
        t = _accepted(conn, ag, f"deliverable {i}")
        firsts.append(state.propose_contract(conn, ag["backend"], _SPEC, t["number"])["version"])
        # Two extra revisions on the first one, so the old task-wide sequence would have
        # raced ahead of the todo count.
        if i == 1:
            for _ in range(2):
                state.decline_contract(conn, ag["frontend"], "not yet", t["number"])
                state.propose_contract(conn, ag["backend"], _SPEC, t["number"])
    assert firsts == [1, 1, 1, 1, 1, 1]


def test_a_chain_is_contiguous_and_ids_may_diverge_from_versions(conn):
    """Versions count within the chain while `contracts.id` keeps counting globally — the
    divergence that makes an unscoped `WHERE version = ?` lookup dangerous."""
    ag, one, two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, two["number"])   # id 1, v1
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])   # id 2, v1
    state.decline_contract(conn, ag["frontend"], "reshape", two["number"])
    r = state.propose_contract(conn, ag["backend"], _SPEC, two["number"])  # id 3, v2
    assert r["version"] == 2
    rows = conn.execute(
        "SELECT id, todo_id, version FROM contracts ORDER BY id"
    ).fetchall()
    assert [(r["id"], r["todo_id"], r["version"]) for r in rows] == [
        (1, two["id"], 1), (2, one["id"], 1), (3, two["id"], 2)
    ]


# --------------------------------------------------------------------------- #
# FIX 2 — the broker names the deliverable it is talking about
# --------------------------------------------------------------------------- #
def test_signing_todo_two_is_not_refused_because_todo_one_is_locked(conn):
    """THE observed failure. Todo #1's contract is locked; todo #2 has a signable draft.
    `lock_contract(version=1, todo=2)` used to fetch todo #1's row by `(task, version)`
    alone, see 'locked', and refuse with "contract version 1 is already locked and
    immutable; propose a new version to change it" — a true sentence about the wrong
    deliverable, whose advice was wrong for the right one."""
    ag, one, two = _overlapping_keys_task(conn)
    v_one = state.propose_contract(conn, ag["backend"], _SPEC, one["number"])["version"]
    for role in ("backend", "frontend"):
        state.lock_contract(conn, ag[role], v_one, one["number"])
    v_two = state.propose_contract(conn, ag["backend"], _SPEC, two["number"])["version"]
    assert (v_one, v_two) == (1, 1)   # both chains start at 1 — the collision that bit

    # The move the broker used to refuse.
    state.lock_contract(conn, ag["backend"], v_two, two["number"])
    assert state.lock_contract(conn, ag["frontend"], v_two, two["number"])["locked"] is True
    # …and todo #1's own lock is untouched by it.
    assert state.get_contract(conn, "payments", one["number"])["locked"] is True


def test_relocking_names_the_deliverable_it_is_refusing(conn):
    """The refusal is still correct when it IS the right deliverable — and now it says
    which one, so the reader can tell those two situations apart."""
    ag, one, _two = _overlapping_keys_task(conn)
    v = state.propose_contract(conn, ag["backend"], _SPEC, one["number"])["version"]
    for role in ("backend", "frontend"):
        state.lock_contract(conn, ag[role], v, one["number"])
    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], v, one["number"])
    msg = str(e.value)
    assert "immutable" in msg
    assert f"todo #{one['number']} ({one['title']})" in msg
    assert f"todo #{one['id']}" not in msg          # the number, never the internal id
    assert f"todo={one['number']}" in msg           # and the escape route is typeable


def test_a_mis_sign_names_both_todos(conn):
    """Signing a version that lives in ANOTHER chain is still caught, and the refusal
    names the deliverable it belongs to AND the one the caller asked for."""
    ag, one, two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])
    state.decline_contract(conn, ag["frontend"], "reshape", one["number"])
    v2 = state.propose_contract(conn, ag["backend"], _SPEC, one["number"])["version"]
    state.propose_contract(conn, ag["backend"], _SPEC, two["number"])  # #2 only has a v1
    assert v2 == 2

    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], v2, two["number"])
    msg = str(e.value)
    assert f"belongs to todo #{one['number']} ('{one['title']}')" in msg
    assert f"not todo #{two['number']}" in msg
    # It also says what the chain the caller named DOES have, so the next call is obvious.
    assert "v1" in msg


def test_signing_a_version_its_chain_never_had_says_what_the_chain_has(conn):
    """"Nothing to sign" would be a lie here — something IS proposed, just not v7. The
    refusal lists the chain's real versions instead of guessing."""
    ag, one, _two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])
    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], 7, one["number"])
    msg = str(e.value)
    assert f"todo #{one['number']} ('{one['title']}')" in msg
    assert "its chain has v1" in msg
    assert "per deliverable" in msg
    # The old text would have been wrong here: nothing is missing but the version.
    assert "nothing to sign" not in msg


def test_signing_with_nothing_proposed_on_that_todo_still_names_the_proposal(conn):
    """The `sign`-before-`pc` stall, one level down: the missing move is the PROPOSAL, and
    the error must say so for the deliverable the caller named."""
    ag, one, _two = _overlapping_keys_task(conn)
    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], 1, one["number"])
    msg = str(e.value)
    low = msg.lower()
    assert f"todo #{one['number']} ('{one['title']}')" in msg
    assert "nothing to sign" in low
    assert f"propose_contract(spec, todo={one['number']})" in msg
    assert "assumption" in low
    assert "decline" in low


def test_signing_without_a_todo_lists_the_deliverables_to_choose_from(conn):
    """A version alone never names one contract — every deliverable's chain starts at v1 —
    so a bare `lock_contract(1)` is refused, and the refusal has to hand back numbers the
    agent can actually type."""
    ag, one, two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])
    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], 1)
    msg = str(e.value)
    assert "lock_contract(version, todo=<N>)" in msg
    assert f"#{one['number']} ({one['title']})" in msg
    assert f"#{two['number']} ({two['title']})" in msg
    assert f"#{two['id']}" not in msg   # ids are 2 and 3; numbers are 1 and 2


def test_a_number_that_is_not_a_todo_is_refused_before_any_contract_lookup(conn):
    """The chain is resolved through `todos.get_row`, so a bogus `#N` earns the same clean
    refusal it earns everywhere else — not a confusing "no contract version"."""
    ag, _one, _two = _overlapping_keys_task(conn)
    with pytest.raises(ValueError, match="no todo #9 on task 'payments'"):
        state.lock_contract(conn, ag["backend"], 1, 9)


def test_a_non_party_is_refused_before_the_contracts_status(conn):
    """Permission is a fact about the deliverable the caller named; a status complaint only
    makes sense once the caller is entitled to hear it."""
    ag = _agents(conn, roles=("backend", "frontend", "mobile"))
    t = _accepted(conn, ag, "payments", parties=("backend", "frontend"))
    v = state.propose_contract(conn, ag["backend"], _SPEC, t["number"])["version"]
    state.lock_contract(conn, ag["backend"], v, t["number"])
    state.lock_contract(conn, ag["frontend"], v, t["number"])
    with pytest.raises(ValueError, match="not a party"):
        state.lock_contract(conn, ag["mobile"], v, t["number"])


def test_declining_without_a_todo_is_refused_on_a_task_with_todos(conn):
    """The sibling of the signing bug: with no chain named, "the newest contract" is
    whichever chain was written to last, so a bare decline could kill a proposal on a
    deliverable the caller never mentioned."""
    ag, one, two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])
    state.propose_contract(conn, ag["backend"], _SPEC, two["number"])
    with pytest.raises(ValueError) as e:
        state.decline_contract(conn, ag["backend"], "wrong shape")
    assert "decline_contract(reason, todo=<N>)" in str(e.value)
    # Both proposals are untouched.
    for t in (one, two):
        assert state.get_contract(conn, "payments", t["number"])["status"] == "proposed"


def test_declining_names_the_deliverable_when_it_refuses(conn):
    """Same ordering rule as signing: establish the deliverable, then say what is wrong."""
    ag, one, _two = _overlapping_keys_task(conn)
    v = state.propose_contract(conn, ag["backend"], _SPEC, one["number"])["version"]
    for role in ("backend", "frontend"):
        state.lock_contract(conn, ag[role], v, one["number"])
    with pytest.raises(ValueError) as e:
        state.decline_contract(conn, ag["frontend"], "too late", one["number"])
    msg = str(e.value)
    assert "already locked" in msg
    assert f"todo #{one['number']} ({one['title']})" in msg
    assert f"todo={one['number']}" in msg


def test_the_proposal_message_tells_the_peer_to_sign_with_the_todo(conn):
    """The peer reads this and types it back. `lock_contract(1)` is now ambiguous, so the
    push has to carry the deliverable — and the NUMBER, not the internal id."""
    ag, one, _two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])
    bodies = [m["content"] for m in service.fetch_unacked(conn, ag["frontend"])]
    proposal = [b for b in bodies if "Proposed contract" in b]
    assert proposal
    assert f"lock_contract(1, todo={one['number']})" in proposal[-1]
    assert f"todo={one['id']}" not in proposal[-1]


def test_get_contract_tells_the_reviewer_to_sign_with_the_todo(conn):
    ag, one, _two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])
    note = state.get_contract(conn, "payments", one["number"])["note"]
    assert f"lock_contract(1, todo={one['number']})" in note


def test_every_deliverable_numbers_its_own_chain_from_one(conn):
    """Two deliverables on one task, each proposing for the first time: both read v1, and
    the rows differ only by which todo they belong to. A task-wide sequence would have made
    the second one "v2" — a renegotiation that never happened."""
    ag = _agents(conn, roles=("backend", "frontend"))
    a = _accepted(conn, ag, "payments")
    b = _accepted(conn, ag, "refunds")
    assert state.propose_contract(conn, ag["backend"], _SPEC, a["number"])["version"] == 1
    assert state.propose_contract(conn, ag["backend"], _SPEC, b["number"])["version"] == 1
    rows = conn.execute("SELECT todo_id, version FROM contracts ORDER BY id").fetchall()
    assert [(r["todo_id"], r["version"]) for r in rows] == [(a["id"], 1), (b["id"], 1)]


def test_a_racing_pair_of_proposals_cannot_share_a_version_in_one_chain(tmp_path):
    """The unique index is the backstop for the MAX+1 read: two proposals on one
    deliverable that computed the same version cannot both land."""
    path = tmp_path / "race.db"
    set_config(Config(mode="local", db_path=path))
    db.init_db(path)
    conn = db.connect(path)
    try:
        ag = _agents(conn, roles=("backend", "frontend"))
        t = _accepted(conn, ag, "payments")
        state.propose_contract(conn, ag["backend"], _SPEC, t["number"])
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contracts (task_id, version, spec_json, status, todo_id, "
                "created_at) VALUES ('signin', 1, '{}', 'draft', ?, ?)",
                (t["id"], time.time()),
            )
    finally:
        conn.rollback()
        conn.close()


def test_the_dashboard_api_renders_each_chain_from_v1(conn):
    """The dashboard prints a contract as "#N vV" off `api._contract_for`, which already
    scopes by `todo_id` — so it needs no change, and this is the assertion that says so."""
    from sys_buddy import api

    ag, one, two = _overlapping_keys_task(conn)
    state.propose_contract(conn, ag["backend"], _SPEC, one["number"])
    state.propose_contract(conn, ag["backend"], _SPEC, two["number"])
    for t in (one, two):
        block = api._contract_for(conn, "payments", todo_id=t["id"])
        assert [v["id"] for v in block["versions"]] == ["v1"]
        assert block["default"] == "v1"
    # …and the task-level block is empty, not a jumble of the two chains.
    assert api._contract_for(conn, "payments")["exists"] is False


def test_the_agent_tool_surface_signs_with_the_number(conn):
    """End to end through `tools`, which is what an agent actually calls."""
    ag, one, _two = _overlapping_keys_task(conn)
    r = tools._op_propose(ag["backend"], _SPEC, one["number"])
    assert r["version"] == 1
    tools._op_lock(ag["backend"], r["version"], one["number"])
    assert tools._op_lock(ag["frontend"], r["version"], one["number"])["locked"] is True
