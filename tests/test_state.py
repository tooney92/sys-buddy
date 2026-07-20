"""Specs for the enforced state machine, contract lock, and strikes (SPEC §5/6/8).

These assert the guiding principle: the broker enforces in code, not prompt. Every
rejection here is a ``ValueError`` an agent cannot argue with.
"""

from __future__ import annotations

import pytest

from sys_buddy import service, state
from tests.conftest import seed_agent, seed_task


def _agents(conn, task="signin", roles=("backend", "frontend")):
    """Seed a task and return {role: Identity} for each declared role."""
    seed_task(conn, task, roles=roles)
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in roles
    }


def _valid_spec(url="https://api-staging.example.com") -> dict:
    return {
        "version": 1,
        "endpoints": [{"method": "POST", "path": "/api/auth/login"}],
        "staging_url": url,
    }


def _task_state(conn, task="signin") -> str:
    return conn.execute("SELECT state FROM tasks WHERE id = ?", (task,)).fetchone()["state"]


def _strikes(conn, task="signin") -> int:
    return conn.execute("SELECT strikes FROM tasks WHERE id = ?", (task,)).fetchone()["strikes"]


def _lock_all(conn, ag, version=1):
    """Have every role sign the given version so it locks."""
    for ident in ag.values():
        result = state.lock_contract(conn, ident, version)
    return result


def _to_backend_live(conn, ag, spec=None):
    """Drive a task through propose → lock (all) → deploy so it is backend_live."""
    state.propose_contract(conn, ag["backend"], spec or _valid_spec())
    _lock_all(conn, ag, version=1)
    return state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "deployed to staging")


# --- propose / validation ---------------------------------------------------
def test_propose_valid_contract_moves_to_proposed(conn):
    ag = _agents(conn)
    result = state.propose_contract(conn, ag["backend"], _valid_spec())
    assert result == {"version": 1, "state": state.CONTRACT_PROPOSED}
    assert _task_state(conn) == state.CONTRACT_PROPOSED


def test_propose_invalid_contract_raises_with_errors(conn):
    # staging_url strictness is remote-only, so exercise the https rule in remote mode.
    from sys_buddy.config import Config, get_config, set_config
    set_config(Config(mode="remote", db_path=get_config().db_path))
    ag = _agents(conn)
    for ident in ag.values():  # clear the remote pre-flight gate first
        conn.execute("UPDATE agents SET ready = 1 WHERE id = ?", (ident.agent_id,))
    conn.commit()
    bad = _valid_spec(url="http://insecure.example.com")  # non-https
    with pytest.raises(ValueError, match="https"):
        state.propose_contract(conn, ag["backend"], bad)
    assert _task_state(conn) == state.OPEN  # no transition on invalid


def test_propose_allows_localhost_url_locally(conn):
    # Local mode (the conftest default): the frontend just hits the backend on the
    # same box, so http/localhost is a valid staging_url — no deploy needed.
    ag = _agents(conn)
    result = state.propose_contract(
        conn, ag["backend"], _valid_spec(url="http://localhost:3000")
    )
    assert result["state"] == state.CONTRACT_PROPOSED


def test_propose_blocked_until_all_pass_preflight_remote(conn):
    from sys_buddy.config import Config, get_config, set_config
    set_config(Config(mode="remote", db_path=get_config().db_path))
    ag = _agents(conn)
    # Only the backend has passed pre-flight; the frontend hasn't.
    conn.execute("UPDATE agents SET ready = 1 WHERE id = ?", (ag["backend"].agent_id,))
    conn.commit()
    with pytest.raises(ValueError, match="pre-flight"):
        state.propose_contract(conn, ag["backend"], _valid_spec())
    # Once both pass, it goes through.
    conn.execute("UPDATE agents SET ready = 1 WHERE id = ?", (ag["frontend"].agent_id,))
    conn.commit()
    result = state.propose_contract(conn, ag["backend"], _valid_spec())
    assert result["state"] == state.CONTRACT_PROPOSED


def test_reopen_negotiations_drops_locked_task_back(conn):
    ag = _agents(conn)
    _to_backend_live(conn, ag)  # propose → lock → deploy (backend_live)
    assert _task_state(conn) == state.BACKEND_LIVE
    result = state.reopen_negotiations(conn, ag["frontend"], "need a new field on /login")
    assert result["state"] == state.CONTRACT_PROPOSED
    assert _task_state(conn) == state.CONTRACT_PROPOSED
    # The previously-locked contract still serves as the working blueprint.
    assert state.get_contract(conn, "signin")["exists"] is True


def test_reopen_negotiations_rejected_before_any_lock(conn):
    ag = _agents(conn)
    state.propose_contract(conn, ag["backend"], _valid_spec())  # proposed, not locked
    with pytest.raises(ValueError, match="nothing to reopen"):
        state.reopen_negotiations(conn, ag["frontend"], "too soon")


def test_reproposal_increments_version_and_reopens(conn):
    ag = _agents(conn)
    state.propose_contract(conn, ag["backend"], _valid_spec())
    _lock_all(conn, ag, version=1)
    assert _task_state(conn) == state.CONTRACT_LOCKED
    # A v2 proposal from a later state reopens negotiation.
    result = state.propose_contract(conn, ag["backend"], _valid_spec())
    assert result["version"] == 2
    assert _task_state(conn) == state.CONTRACT_PROPOSED


# --- lock requires ALL roles ------------------------------------------------
def test_lock_requires_all_roles_two_of_three_is_not_locked(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], _valid_spec())

    r1 = state.lock_contract(conn, ag["backend"], 1)
    r2 = state.lock_contract(conn, ag["frontend"], 1)

    assert r1["locked"] is False and r2["locked"] is False
    assert set(r2["signed"]) == {"backend", "frontend"}
    assert r2["remaining"] == ["mobile"]
    assert _task_state(conn) == state.CONTRACT_PROPOSED  # still not locked

    r3 = state.lock_contract(conn, ag["mobile"], 1)
    assert r3["locked"] is True
    assert _task_state(conn) == state.CONTRACT_LOCKED


def test_lock_is_idempotent_per_agent(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    state.lock_contract(conn, ag["backend"], 1)
    r = state.lock_contract(conn, ag["backend"], 1)  # sign twice
    assert r["signed"] == ["backend"]  # not double-counted


def test_relocking_locked_contract_is_rejected(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    _lock_all(conn, ag, version=1)
    with pytest.raises(ValueError, match="immutable"):
        state.lock_contract(conn, ag["backend"], 1)


def test_lock_writes_lock_event_with_signed_roles(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    _lock_all(conn, ag, version=1)
    row = conn.execute(
        "SELECT detail_json FROM events WHERE kind='lock' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    import json

    detail = json.loads(row["detail_json"])
    assert detail["version"] == 1
    assert set(detail["signed"]) == {"backend", "frontend"}


# --- get_contract: staging_url from the contract, not chat ------------------
def test_get_contract_returns_locked_staging_url(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    _lock_all(conn, ag, version=1)
    c = state.get_contract(conn, "signin")
    assert c["exists"] is True
    assert c["staging_url"] == "https://api-staging.example.com"


def test_get_contract_absent_before_lock(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    assert state.get_contract(conn, "signin") == {"exists": False}


# --- deploy gating ----------------------------------------------------------
def test_deploy_rejected_with_unsigned_proposal(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())  # proposed, not locked
    with pytest.raises(ValueError, match="cannot report 'ready'"):
        state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "go")


def test_deploy_rejected_from_open_with_no_contract(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    with pytest.raises(ValueError, match="no locked contract"):
        state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "go")


def test_deploy_rejected_mid_renegotiation(conn):
    """Once live, proposing v2 reopens negotiation; the backend must not be able to
    deploy again until all roles re-sign the new version (regression: review #4)."""
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)                                   # v1 locked + deployed
    state.propose_contract(conn, ag["backend"], _valid_spec())   # v2 draft, unsigned
    assert _task_state(conn) == state.CONTRACT_PROPOSED
    with pytest.raises(ValueError, match="awaiting signatures"):
        state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "sneaky redeploy")


def test_only_producer_can_report_ready(conn):
    # backend PROPOSES the contract → backend is the producer (model B); frontend can't report ready.
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    _lock_all(conn, ag, version=1)
    with pytest.raises(ValueError, match="proposed the contract"):
        state.report_status(conn, ag["frontend"], state.STATUS_DEPLOYED, "sneaky")


def test_deploy_moves_to_backend_live_and_posts_message(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    r = _to_backend_live(conn, ag)
    assert r["state"] == state.BACKEND_LIVE
    # deploy_confirmed message is visible to the other agent (dashboard thread)
    inbox = service.fetch_unacked(conn, ag["frontend"])
    assert any(m["type"] == "deploy_confirmed" for m in inbox)


# --- test gating & roles ----------------------------------------------------
def test_test_rejected_before_backend_live(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    _lock_all(conn, ag, version=1)  # contract_locked, not yet live
    with pytest.raises(ValueError, match="before the producer is ready"):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "green")


def test_producer_cannot_report_checks(conn):
    # backend proposed → backend is the producer, so it can't report its own checks.
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    with pytest.raises(ValueError, match="doesn't report checks"):
        state.report_status(conn, ag["backend"], state.STATUS_TEST_PASSED, "green")


def test_first_test_moves_to_testing(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "green")
    assert r["state"] == state.TESTING


# --- strikes ----------------------------------------------------------------
def test_three_strikes_forces_stuck_and_refuses_more_tests(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    for _ in range(3):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red")
    assert _task_state(conn) == state.STUCK
    assert _strikes(conn) == 3
    # further test cycles are refused — terminal
    with pytest.raises(ValueError, match="terminal"):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red again")


def test_each_fail_increments_strike_and_writes_test_event(conn):
    import json

    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red")
    assert _strikes(conn) == 1
    row = conn.execute(
        "SELECT detail_json FROM events WHERE kind='test' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    detail = json.loads(row["detail_json"])
    assert detail == {"pass": False, "strike": 1}


def test_new_version_deploy_resets_strikes(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red")
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red")
    assert _strikes(conn) == 2

    # Renegotiate: propose v2, all re-sign, backend redeploys the new version.
    state.propose_contract(conn, ag["backend"], _valid_spec())  # v2
    _lock_all(conn, ag, version=2)
    r = state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "v2 live")
    assert r["strikes"] == 0  # fresh attempt, not the same loop


def test_same_contract_redeploy_keeps_strikes(conn):
    """Redeploying the SAME locked contract is the same fix loop — strikes persist."""
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red")
    r = state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "fixed, redeploy")
    assert r["strikes"] == 1  # not reset


# --- terminal states --------------------------------------------------------
def test_verified_is_terminal(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "green")
    r = state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "e2e green")
    assert r["state"] == state.VERIFIED
    with pytest.raises(ValueError, match="terminal"):
        state.propose_contract(conn, ag["backend"], _valid_spec())


def test_stuck_is_terminal(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_STUCK, "giving up")
    assert r["state"] == state.STUCK
    with pytest.raises(ValueError, match="terminal"):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "nope")


def test_transition_event_shape_for_times_map(conn):
    """The API derives times[state] from transition events; assert the shape."""
    import json

    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec())
    row = conn.execute(
        "SELECT detail_json FROM events WHERE kind='transition' ORDER BY id LIMIT 1"
    ).fetchone()
    assert json.loads(row["detail_json"]) == {"from": "open", "to": "contract_proposed"}


# --- task-agnostic status aliases -------------------------------------------
# 'ready'/'checked'/'blocked' are pure aliases of 'deployed'/'test_passed'/
# 'test_failed'; each must produce identical behavior to the word it stands for.
def _to_locked(conn, ag):
    """Drive a task through propose → lock (all) so it is ready to deploy."""
    state.propose_contract(conn, ag["backend"], _valid_spec())
    _lock_all(conn, ag, version=1)


def test_ready_is_alias_of_deployed(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_locked(conn, ag)
    r = state.report_status(conn, ag["backend"], state.STATUS_READY, "part ready")
    assert r == {"status": state.STATUS_DEPLOYED, "state": "backend_live", "strikes": 0}
    assert _task_state(conn) == "backend_live"


def test_checked_is_alias_of_test_passed(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_CHECKED, "works")
    assert r == {"status": state.STATUS_TEST_PASSED, "state": "testing", "strikes": 0}
    assert _strikes(conn) == 0


def test_blocked_is_alias_of_test_failed_and_strikes(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_BLOCKED, "broken")
    assert r == {"status": state.STATUS_TEST_FAILED, "state": "testing", "strikes": 1}
    assert _strikes(conn) == 1  # same strike increment as 'test_failed'


def test_blocked_three_times_forces_stuck_like_test_failed(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    for _ in range(3):
        state.report_status(conn, ag["frontend"], state.STATUS_BLOCKED, "red")
    assert _task_state(conn) == "stuck"
    assert _strikes(conn) == 3


def test_new_word_and_old_word_reach_identical_state(conn):
    """A task driven entirely with ready/checked ends where deployed/test_passed would."""
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_locked(conn, ag)
    state.report_status(conn, ag["backend"], state.STATUS_READY, "ready")
    state.report_status(conn, ag["frontend"], state.STATUS_CHECKED, "works")
    r = state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "done")
    assert r["status"] == state.STATUS_VERIFIED
    assert _task_state(conn) == "verified"


def test_unknown_status_message_lists_new_vocabulary(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_locked(conn, ag)
    with pytest.raises(ValueError) as exc:
        state.report_status(conn, ag["backend"], "bogus", "x")
    msg = str(exc.value)
    for word in ("ready", "checked", "blocked", "verified", "stuck"):
        assert word in msg


# --- model B: producer = whoever proposes (no hardcoded 'backend') ----------
def test_non_backend_producer_full_flow(conn):
    """A contract with NO 'backend' role: the role that PROPOSES is the producer.
    Here frontend proposes → frontend reports `ready`; mobile (the consumer) checks."""
    ag = _agents(conn, roles=("frontend", "mobile"))
    # frontend proposes → becomes the producer
    state.propose_contract(conn, ag["frontend"], _valid_spec())
    _lock_all(conn, ag, version=1)

    # the NON-proposer (mobile) may not report ready
    with pytest.raises(ValueError, match="proposed the contract"):
        state.report_status(conn, ag["mobile"], state.STATUS_READY, "nope")

    # the producer (frontend) reports ready → backend_live
    r = state.report_status(conn, ag["frontend"], state.STATUS_READY, "my part is up")
    assert r["state"] == state.BACKEND_LIVE

    # the producer can't check its own work; the consumer (mobile) can
    with pytest.raises(ValueError, match="doesn't report checks"):
        state.report_status(conn, ag["frontend"], state.STATUS_CHECKED, "self-check")
    r = state.report_status(conn, ag["mobile"], state.STATUS_CHECKED, "works against frontend")
    assert r["state"] == state.TESTING

    r = state.report_status(conn, ag["mobile"], state.STATUS_VERIFIED, "all good")
    assert r["state"] == state.VERIFIED
