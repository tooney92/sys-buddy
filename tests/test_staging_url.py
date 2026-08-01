"""The deployment target is HOST-OWNED CONFIGURATION, not part of the signed shape.

Two defects, one change.

**It churned for reasons nobody negotiated.** An ngrok free URL rotates on every tunnel
restart, so a locked contract went stale routinely — and under the old rules the only
sanctioned fix was a full renegotiation producing a version identical but for one
string. Making people re-sign on non-events teaches them to sign without reading, which
costs every signature that WOULD have caught something. (The owner hit this live: a
locked contract carrying ``https://a1b2c3d4.ngrok-free.dev:3000``, which
cannot connect — ngrok terminates on 443 — and was immutable inside a signed document.)

**It was an agent-controlled field on the most security-sensitive value in the system.**
An injected "test against evil.com" landed in a proposal and was defended only by the
consumer noticing during review. With the host owning the target there is no field for
it to land in at all, so the posture is STRONGER, not laxer.

What must survive, and is asserted here: ``staging_url`` remains the ONLY fetchable URL
the broker hands out; ``get_contract`` still withholds it until every party has signed;
and the URL rules (https + SSRF) are unchanged — only the door they guard has moved.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time

import pytest

from sys_buddy import admin, api, contracts, db, state, todos
from sys_buddy.config import get_config

from tests.conftest import seed_agent, seed_task
from tests.test_state import _agents, _lock_all, _valid_spec


def _target(conn, url, task="signin"):
    conn.execute("UPDATE tasks SET staging_url = ? WHERE id = ?", (url, task))
    conn.commit()


# --------------------------------------------------------------------------- #
# an agent may not supply one — REFUSED, never silently dropped
# --------------------------------------------------------------------------- #
def test_the_refusal_says_who_owns_it_and_where_to_read_it():
    """Refused rather than ignored, deliberately. Silently dropping an injected target
    would let "test against evil.com" appear to SUCCEED — the agent would believe the
    contract points there and could repeat it in chat. A refusal contradicts the
    injection out loud, and it rides in the same collected-errors list as every other
    fix so one revision still corrects everything."""
    errors = contracts.validate_spec(
        {"endpoints": [{"method": "GET", "path": "/x"}], "staging_url": "https://evil.example"}
    )
    joined = " ".join(errors)
    assert "not yours to set" in joined
    assert "host" in joined and "get_contract" in joined


def test_the_target_never_reaches_spec_json(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError, match="not yours to set"):
        state.propose_contract(
            conn, ag["backend"], {**_valid_spec(), "staging_url": "https://evil.example"}, 1
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"] == 0


# --------------------------------------------------------------------------- #
# resolution: todo override → task → legacy spec, read LIVE
# --------------------------------------------------------------------------- #
def _locked(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    return ag


def test_resolution_prefers_the_todo_override_then_the_task(conn):
    _locked(conn)
    todo_id = conn.execute("SELECT id FROM todos WHERE task_id = 'signin'").fetchone()["id"]

    assert state.resolve_staging_url(conn, "signin", todo_id) is None
    _target(conn, "https://task.example.com")
    assert state.resolve_staging_url(conn, "signin", todo_id) == "https://task.example.com"
    conn.execute("UPDATE todos SET staging_url = ? WHERE id = ?",
                 ("https://just-this-todo.example.com", todo_id))
    conn.commit()
    assert state.resolve_staging_url(conn, "signin", todo_id) == (
        "https://just-this-todo.example.com"
    )
    # A DIFFERENT deliverable is unaffected — an override is per-deliverable because a
    # real task deploys its pieces to different places.
    assert state.resolve_staging_url(conn, "signin", None) == "https://task.example.com"


def test_a_legacy_spec_is_the_last_resort_not_the_first(conn):
    """Back-compat without letting a stale baked-in URL outrank the host. This ordering
    IS the fix for the reported case: a locked contract holding a URL that could never
    connect must be overridable without re-signing anything."""
    legacy = {"endpoints": [{"method": "GET", "path": "/x"}],
              "staging_url": "https://a1b2c3d4.ngrok-free.dev:3000"}
    assert state.resolve_staging_url(conn, "signin", None, legacy) == (
        "https://a1b2c3d4.ngrok-free.dev:3000"
    )
    seed_task(conn, "signin", roles=("backend", "frontend"))
    _target(conn, "https://a1b2c3d4.ngrok-free.dev")
    assert state.resolve_staging_url(conn, "signin", None, legacy) == (
        "https://a1b2c3d4.ngrok-free.dev"
    )


# --------------------------------------------------------------------------- #
# get_contract still WITHHOLDS until every party has signed
# --------------------------------------------------------------------------- #
def test_the_target_resolves_for_EVERY_kind_not_just_http(conn):
    """DELIBERATE and load-bearing: resolution is KIND-AGNOSTIC.

    The tempting "fix" is to gate it on `contracts` `has_http_surface` so a
    screens/types/criteria contract resolves nothing. That is wrong. The flag answers
    *does this contract describe HTTP?*, not *does the consumer need somewhere to go and
    look?* — and the two diverge for the very kind that invites the mistake: a `ui`
    contract is verified by OPENING THE DEPLOYED APP, so gating would strip the URL from
    the kind that most obviously needs one and make `ok #N` unanswerable.

    Nothing is loosened by resolving it: no agent can write the value, and it is still
    withheld until every party has signed (see the test below). See DECISIONS.md D13.
    """
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://deployed-app.example.com")
    specs = {
        1: {"endpoints": [{"method": "GET", "path": "/x"}]},
        2: {"screens": [{"name": "Receipt", "states": ["loading", "paid"]}]},
        3: {"types": [{"name": "Session", "fields": [{"name": "id", "type": "string"}]}]},
        4: {"criteria": ["the CSV import rejects a row with no email"]},
    }
    for n, spec in specs.items():
        if n > 1:
            t = todos.propose_todo(conn, ag["backend"], f"deliverable {n}", "scope",
                                   ["backend", "frontend"])
            todos.accept_todo(conn, ag["frontend"], t["number"])
        state.propose_contract(conn, ag["backend"], spec, n)
        state.lock_contract(conn, ag["backend"], version=1, number=n)
        state.lock_contract(conn, ag["frontend"], version=1, number=n)
        kind = contracts.infer_kind(spec)
        assert state.get_contract(conn, "signin", n)["staging_url"] == (
            "https://deployed-app.example.com"
        ), f"kind {kind!r} resolved no target"


def test_a_configured_target_is_still_withheld_before_full_signature(conn):
    """The incentive to actually READ the shape has to survive the target becoming
    configuration — so the withholding keys on the LOCK, never on whether a target
    happens to be set."""
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://api-staging.example.com")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)

    assert state.get_contract(conn, "signin", 1)["staging_url"] is None
    state.lock_contract(conn, ag["backend"], version=1, number=1)      # one of two
    c = state.get_contract(conn, "signin", 1)
    assert c["staging_url"] is None and c["awaiting"] == ["frontend"]

    state.lock_contract(conn, ag["frontend"], version=1, number=1)     # the last one
    assert state.get_contract(conn, "signin", 1)["staging_url"] == (
        "https://api-staging.example.com"
    )


def test_get_todos_never_carries_the_target(conn):
    """`get_todos` is the AGENTS' view. Publishing a todo's target there would hand out
    the one fetchable URL without anybody signing anything — dissolving exactly what
    get_contract's withholding exists to create."""
    _locked(conn)
    admin.set_staging_url("signin", "https://api-staging.example.com", todo=1)
    for t in todos.get_todos(conn, "signin"):
        assert "staging_url" not in t


def test_the_host_sees_their_own_configuration_everywhere_it_appears(conn):
    """The host CHOSE the value; hiding a person's own configuration from them protects
    nothing. Every surface that carries the target shows it to the host."""
    _locked(conn)
    admin.set_staging_url("signin", "https://api-staging.example.com")
    admin.set_staging_url("signin", "https://override.example.com", todo=1)
    detail = api._task_detail(conn, "signin", is_host=True)
    assert detail["staging_url"] == "https://api-staging.example.com"
    row = detail["todos"][0]
    assert row["staging_url"] == "https://override.example.com"
    assert row["staging_url_effective"] == "https://override.example.com"
    # The contract block shows the LIVE target plus what was live at the lock.
    v1 = row["contract"]["data"]["v1"]
    assert v1["staging_url"] == "https://override.example.com"
    assert v1["staging_url_at_lock"] is None   # nothing was configured when it locked


def test_the_todo_row_withholds_the_target_from_a_buddy_too(conn):
    """The contract block was fixed to withhold; the TODO ROW beside it was not.

    `/api` carried `staging_url` and `staging_url_effective` on every todo regardless of
    signature, so a buddy — who is handed a `viewer_token` the moment they pair — could
    read the target off an unsigned deliverable one key away from the block that had just
    been taught to refuse. Two fields that disagree about who has earned the target means
    the stricter one is decoration.
    """
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://payments-stg.example.com")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)   # proposed, unsigned

    def row_seen_by(*, is_host):
        return api._task_detail(conn, "signin", is_host=is_host)["todos"][0]

    buddy = row_seen_by(is_host=False)
    assert buddy["staging_url"] is None
    assert buddy["staging_url_effective"] is None
    # …and the scope is NOT withheld: a buddy still has to be able to read what is
    # being proposed. Withholding the target is the incentive to read it.
    assert buddy["title"] and buddy["parties"]

    assert row_seen_by(is_host=True)["staging_url_effective"] == "https://payments-stg.example.com"

    # Signing is what releases it — the same rule the contract block follows.
    _lock_all(conn, ag, 1)
    assert row_seen_by(is_host=False)["staging_url_effective"] == "https://payments-stg.example.com"


def test_the_task_wide_target_is_host_only(conn):
    """A buddy reaches the target through a todo they SIGNED. The task-wide value would
    hand it over before any signature, which is the bypass the withholding exists for."""
    _locked(conn)
    admin.set_staging_url("signin", "https://api-staging.example.com")
    assert api._task_detail(conn, "signin", is_host=True)["staging_url"] == "https://api-staging.example.com"
    assert api._task_detail(conn, "signin", is_host=False)["staging_url"] is None


# --------------------------------------------------------------------------- #
# the host action: configuration, not renegotiation
# --------------------------------------------------------------------------- #
def test_changing_the_target_mints_no_version_and_disturbs_no_signature(conn):
    _locked(conn)
    _target(conn, "https://old.ngrok-free.dev")
    before = [tuple(r) for r in conn.execute("SELECT * FROM contracts").fetchall()]
    sigs = [tuple(r) for r in conn.execute("SELECT * FROM contract_signatures").fetchall()]

    admin.set_staging_url("signin", "https://new.ngrok-free.dev")

    assert [tuple(r) for r in conn.execute("SELECT * FROM contracts").fetchall()] == before
    assert [
        tuple(r) for r in conn.execute("SELECT * FROM contract_signatures").fetchall()
    ] == sigs
    assert state.get_contract(conn, "signin", 1)["staging_url"] == "https://new.ngrok-free.dev"


def test_clearing_a_todo_override_falls_back_to_the_task(conn):
    _locked(conn)
    admin.set_staging_url("signin", "https://task.example.com")
    admin.set_staging_url("signin", "https://todo.example.com", todo=1)
    assert admin.get_staging_url("signin", 1)["effective"] == "https://todo.example.com"
    res = admin.set_staging_url("signin", None, todo=1)
    assert res["previous"] == "https://todo.example.com"
    assert res["effective"] == "https://task.example.com"


def test_the_host_change_is_validated_by_the_same_url_rules(conn):
    """Same rules, new door. The value is still the only thing the broker will ever hand
    a test-runner to fetch, so it still faces https + the SSRF guard."""
    from sys_buddy.config import Config, set_config

    set_config(Config(mode="remote", db_path=get_config().db_path))
    _agents(conn)
    with pytest.raises(ValueError, match="169.254.169.254"):
        admin.set_staging_url("signin", "https://169.254.169.254/latest/meta-data/")
    assert admin.get_staging_url("signin")["effective"] is None


def test_there_is_no_agent_tool_that_sets_or_requests_a_target():
    """The security property is structural, not a matter of what agents are told. If a
    tool ever appears here, an injection has somewhere to aim again."""
    import inspect

    from sys_buddy import tools

    src = inspect.getsource(tools)
    assert "def set_staging_url" not in src
    assert "def request_staging_url" not in src


def test_the_change_lands_in_the_event_log(conn):
    _locked(conn)
    admin.set_staging_url("signin", "https://visible.example.com", todo=1)
    details = [
        json.loads(r["detail_json"])
        for r in conn.execute(
            "SELECT detail_json FROM events WHERE task_id = 'signin' AND kind = 'task'"
        ).fetchall()
    ]
    hit = [d for d in details if d.get("staging_url") == "https://visible.example.com"]
    assert hit and hit[0]["todo"] == 1
    assert "todo #1" in hit[0]["text"]


def test_the_cli_shows_and_changes_it(conn, capsys):
    from sys_buddy import cli

    dbfile = str(get_config().db_path)
    _locked(conn)
    cli.cmd_task_staging_url(argparse.Namespace(
        task="signin", url="https://cli.example.com", todo=None, clear=False, db=dbfile
    ))
    out = capsys.readouterr().out
    assert "https://cli.example.com" in out
    # It says plainly that this is not a renegotiation, because that is the whole point.
    assert "No contract, version or signature changed" in out

    cli.cmd_task_staging_url(argparse.Namespace(
        task="signin", url=None, todo=1, clear=False, db=dbfile
    ))
    shown = capsys.readouterr().out
    assert "inherits the task" in shown
    assert "https://cli.example.com" in shown


# --------------------------------------------------------------------------- #
# the lock RECORDS what was live, so "what did we agree to?" keeps an answer
# --------------------------------------------------------------------------- #
def test_the_record_and_the_live_value_are_separate_answers(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://at-the-time.example.com")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    admin.set_staging_url("signin", "https://today.example.com")

    c = state.get_contract(conn, "signin", 1)
    assert c["staging_url"] == "https://today.example.com"
    assert c["staging_url_at_lock"] == "https://at-the-time.example.com"
    # And the SIGNED document was never touched to carry either of them.
    spec_json = conn.execute("SELECT spec_json FROM contracts").fetchone()["spec_json"]
    assert "staging_url" not in json.loads(spec_json)


# --------------------------------------------------------------------------- #
# the migration — off the spec, onto the task. Nothing is rewritten.
# --------------------------------------------------------------------------- #
_OLD_URL = "https://a1b2c3d4.ngrok-free.dev:3000"

# Captured at import, before any test can monkeypatch the module attribute.
_REAL_MIGRATION = db._migrate_staging_url_off_the_spec

_LEGACY_SPEC = {
    "version": 1,
    "endpoints": [{"method": "POST", "path": "/api/auth/login"}],
    "staging_url": _OLD_URL,
}


def _db_with_a_legacy_contract(tmp_path, *, task_url=None, status="locked"):
    """A CURRENT-schema database holding a contract of the OLD shape: the target inside
    ``spec_json``. That is what every existing installation looks like, and the migration
    has to rescue it without rewriting a signed document."""
    path = tmp_path / "legacy.db"
    db.init_db(path)
    conn = db.connect(path)
    now = time.time()
    conn.execute(
        "INSERT INTO tasks (id, title, state, roles_json, seat_roles_json, staging_url, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        ("signin", "Sign-in", "open", json.dumps(["backend", "frontend"]),
         json.dumps({"backend": "backend", "frontend": "frontend"}), task_url, now),
    )
    conn.execute(
        "INSERT INTO todos (id, task_id, number, title, scope, parties_json, "
        "proposed_role, created_at) VALUES (1,'signin',1,'Login','scope',?,'backend',?)",
        (json.dumps(["backend", "frontend"]), now),
    )
    conn.execute(
        "INSERT INTO contracts (id, task_id, version, spec_json, status, todo_id, "
        "locked_at, created_at) VALUES (1,'signin',1,?,?,1,?,?)",
        (json.dumps(_LEGACY_SPEC), status, now if status == "locked" else None, now),
    )
    conn.commit()
    conn.close()
    return path


def test_the_migration_seeds_the_task_and_records_the_lock(tmp_path):
    path = _db_with_a_legacy_contract(tmp_path)
    db.init_db(path)          # boot the new broker
    conn = db.connect(path)
    try:
        # The task inherits the target its locked contract carried — without this the
        # value would still be readable inside the old spec but would not RESOLVE.
        assert conn.execute(
            "SELECT staging_url FROM tasks WHERE id = 'signin'"
        ).fetchone()["staging_url"] == _OLD_URL
        # …and the contract records what it locked against.
        row = conn.execute("SELECT spec_json, staging_url_at_lock FROM contracts").fetchone()
        assert row["staging_url_at_lock"] == _OLD_URL
        # NOTHING was rewritten: the signed document is byte-for-byte the one they signed.
        assert json.loads(row["spec_json"]) == _LEGACY_SPEC
    finally:
        conn.close()


def test_the_migration_never_overwrites_a_target_the_host_already_chose(tmp_path):
    """The HOST owns this value. A migration must not replace a human's choice with an
    agent's old proposal — which is precisely the stale one they were fixing."""
    path = _db_with_a_legacy_contract(tmp_path, task_url="https://host-chose.example.com")
    db.init_db(path)
    conn = db.connect(path)
    try:
        assert conn.execute(
            "SELECT staging_url FROM tasks WHERE id = 'signin'"
        ).fetchone()["staging_url"] == "https://host-chose.example.com"
        # The record of what the contract locked against is still written — it is a
        # different fact and both are wanted.
        assert conn.execute(
            "SELECT staging_url_at_lock FROM contracts"
        ).fetchone()["staging_url_at_lock"] == _OLD_URL
    finally:
        conn.close()


def test_a_draft_seeds_nothing(tmp_path):
    """A contract nobody signed agreed to no target, so it neither records one nor
    hands one to the task."""
    path = _db_with_a_legacy_contract(tmp_path, status="draft")
    db.init_db(path)
    conn = db.connect(path)
    try:
        assert conn.execute(
            "SELECT staging_url FROM tasks WHERE id = 'signin'"
        ).fetchone()["staging_url"] is None
        assert conn.execute(
            "SELECT staging_url_at_lock FROM contracts"
        ).fetchone()["staging_url_at_lock"] is None
    finally:
        conn.close()
        # …but the value is NOT lost: the draft's own spec still resolves it.
        c = db.connect(path)
        assert state.resolve_staging_url(c, "signin", 1, _LEGACY_SPEC) == _OLD_URL
        c.close()


def test_the_migration_is_idempotent(tmp_path):
    """`init_db` runs on EVERY boot, so the second and third must be no-ops."""
    path = _db_with_a_legacy_contract(tmp_path)
    db.init_db(path)
    conn = sqlite3.connect(path)
    after_first = list(conn.iterdump())
    conn.close()
    db.init_db(path)
    db.init_db(path)
    conn = sqlite3.connect(path)
    assert list(conn.iterdump()) == after_first
    conn.close()


def test_the_migration_rolls_back_whole_when_a_step_throws(monkeypatch, tmp_path):
    """A HALF-migrated db is worse than a failed boot — a contract that quietly points
    nowhere — so the whole thing is one transaction rolled back on any ``BaseException``.

    Neither new column carries a constraint, and a unique index could not help even if
    one did: SQLite treats NULLs as DISTINCT, so a half-backfilled column sails past
    every constraint. Only the explicit count in the migration catches it.
    """
    path = _db_with_a_legacy_contract(tmp_path)
    # Boot once WITHOUT the migration so the columns exist and the data is untouched —
    # that is the exact "before" state a failed run must leave behind.
    monkeypatch.setattr(db, "_migrate_staging_url_off_the_spec", lambda conn: None)
    db.init_db(path)
    conn = sqlite3.connect(path)
    before = list(conn.iterdump())
    conn.close()
    assert any("staging_url_at_lock" in line for line in before)

    real = db._spec_staging_url
    calls = {"n": 0}

    def blows_up_mid_write(spec_json):
        calls["n"] += 1
        if calls["n"] > 2:          # past the read phase, inside the UPDATE loop
            raise RuntimeError("disk went away")
        return real(spec_json)

    monkeypatch.setattr(db, "_spec_staging_url", blows_up_mid_write)
    # Put the REAL migration back — the stub above only existed to build the baseline.
    monkeypatch.setattr(db, "_migrate_staging_url_off_the_spec", _REAL_MIGRATION)

    with pytest.raises(RuntimeError, match="disk went away"):
        db.init_db(path)

    conn = sqlite3.connect(path)
    assert list(conn.iterdump()) == before   # not one row moved
    conn.close()


def test_a_fresh_database_carries_both_columns(tmp_path):
    path = tmp_path / "fresh.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        contract_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(contracts)").fetchall()
        }
        todo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
        assert "staging_url_at_lock" in contract_cols
        assert "staging_url" in todo_cols
    finally:
        conn.close()


def test_the_contract_rebuild_would_refuse_to_drop_the_new_column():
    """The guard that makes a future migration fail LOUDLY rather than copy the table
    without a column. Adding one to `contracts` without adding it here is silent data
    loss on exactly the databases the rebuild exists to save."""
    assert "staging_url_at_lock" in db._CONTRACT_COLUMNS
    assert "staging_url_at_lock" in db._CONTRACTS_REBUILD_DDL


# --------------------------------------------------------------------------- #
# the DASHBOARD withholds it too — the hole a UI review found
# --------------------------------------------------------------------------- #
def test_the_dashboard_withholds_the_target_from_a_buddy_until_it_locks(conn):
    """`get_contract` withholding from agents is only half the rule.

    Every buddy is issued a `viewer_token` when they pair (`pairing.py`), so a party who
    did not want to read the shape could simply OPEN THE DASHBOARD and read the target
    off an unsigned draft — the incentive to read before signing is that signing is what
    releases it. `/api` was emitting `staging_url` on every version regardless of lock,
    which handed that incentive away through the one surface nobody was checking.

    The host is deliberately exempt: the host CHOSE the value, and hiding a person's own
    configuration from them protects nothing.
    """
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://payments-stg.example.com")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)

    todo_id = conn.execute(
        "SELECT id FROM todos WHERE task_id = 'signin' AND number = 1"
    ).fetchone()["id"]

    def target_seen_by(*, is_host):
        block = api._contract_for(conn, "signin", todo_id=todo_id, is_host=is_host)
        (version,) = block["data"].values()
        return version["locked"], version["staging_url"]

    # Proposed, not signed: a buddy sees the shape but NOT where it runs.
    assert target_seen_by(is_host=False) == (False, None)
    assert target_seen_by(is_host=True) == (False, "https://payments-stg.example.com")

    # Both parties sign — now it locks, and the buddy has earned the target.
    _lock_all(conn, ag, version=1)
    assert target_seen_by(is_host=False) == (True, "https://payments-stg.example.com")
