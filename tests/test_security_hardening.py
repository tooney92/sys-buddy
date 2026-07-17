"""Specs for the Batch A security-hardening fixes (post-audit):

- H1  verified is reachable only from 'testing' (a test must have run first)
- M2  agent-supplied content is size-capped (DoS / prompt-injection amplifier)
- send_message accepts only conversational types (no forged broker chips)
- a contract's endpoint count is bounded
- H2  a closed task refuses invite redemption (no live seat on a dead task)
"""

from __future__ import annotations

import time

import pytest

from sys_buddy import admin, pairing, service, state
from tests.conftest import seed_task
from tests.test_state import _agents, _to_backend_live, _task_state, _valid_spec


# --- H1: verified only from 'testing' ---------------------------------------
def test_verified_rejected_from_backend_live(conn):
    ag = _agents(conn)
    _to_backend_live(conn, ag)  # backend_live, zero tests run
    with pytest.raises(ValueError, match="before tests have run"):
        state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "trust me")
    assert _task_state(conn) == state.BACKEND_LIVE  # no transition


def test_verified_allowed_after_a_test_result(conn):
    ag = _agents(conn)
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "12/12")
    r = state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "green")
    assert r["state"] == state.VERIFIED


# --- M2: content size caps --------------------------------------------------
def _too_big() -> str:
    return "x" * (service.MAX_CONTENT_BYTES + 1)


def test_oversized_message_body_rejected(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError, match="KB limit"):
        service.post_message(conn, ag["backend"], "status_update", _too_big())


def test_oversized_status_detail_rejected(conn):
    ag = _agents(conn)
    _to_backend_live(conn, ag)
    with pytest.raises(ValueError, match="KB limit"):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, _too_big())


def test_oversized_contract_spec_rejected(conn):
    ag = _agents(conn)
    spec = _valid_spec()
    spec["blob"] = _too_big()
    with pytest.raises(ValueError, match="KB limit"):
        state.propose_contract(conn, ag["backend"], spec)


# --- send_message type allow-list -------------------------------------------
@pytest.mark.parametrize("mtype", ["contract_lock", "note", "system", "deploy_confirmed"])
def test_send_rejects_non_conversational_types(mtype):
    with pytest.raises(ValueError):
        service.assert_sendable(mtype)


@pytest.mark.parametrize("mtype", ["question", "answer", "status_update", "contract_proposal"])
def test_send_allows_conversational_types(mtype):
    service.assert_sendable(mtype)  # must not raise


# --- contract endpoint bound ------------------------------------------------
def test_contract_with_too_many_endpoints_rejected(conn):
    ag = _agents(conn)
    spec = _valid_spec()
    spec["endpoints"] = [{"method": "GET", "path": f"/e{i}"} for i in range(101)]
    with pytest.raises(ValueError, match="too many endpoints"):
        state.propose_contract(conn, ag["backend"], spec)


# --- H2: closed task refuses pairing ----------------------------------------
def test_redeem_on_closed_task_is_rejected(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    code = admin.mint_invite("signin", "frontend")[0]
    conn.execute("UPDATE tasks SET closed_at = ? WHERE id = 'signin'", (time.time(),))
    conn.commit()
    with pytest.raises(ValueError, match="closed"):
        pairing.redeem_invite(conn, code, "late-frontend")
    assert conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"] == 0
