"""Specs for the Batch A security-hardening fixes (post-audit):

- H1  verified is reachable only from 'testing' (a test must have run first)
- M2  agent-supplied content is size-capped (DoS / prompt-injection amplifier)
- send_message accepts only conversational types (no forged broker chips)
- a contract's endpoint count is bounded
- H2  a closed task refuses invite redemption (no live seat on a dead task)
"""

from __future__ import annotations

import asyncio
import time

import pytest

from sys_buddy import admin, contracts, pairing, service, slack, state
from sys_buddy.config import Config, get_config, set_config
from sys_buddy.http_middleware import (
    DASHBOARD_CSP,
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
)
from sys_buddy.middleware import AUTH_FAIL_MAX, _auth_failure_limited
from sys_buddy.pairing import PAIR_RATE_MAX, _rate_limited, _valid_agent_name
from sys_buddy.rules import RULES_OF_ENGAGEMENT
from tests.conftest import seed_agent, seed_task
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


# --- M1: Slack mrkdwn sanitize + https-only ---------------------------------
def test_slack_escapes_mrkdwn_link_injection():
    out = slack._mrkdwn_safe("<https://evil.com|Security Alert>")
    assert "<https://evil.com" not in out
    assert out == "&lt;https://evil.com|Security Alert&gt;"


def test_slack_rejects_non_https_webhook(conn):
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook="http://insecure/x"))
    msg = slack.notify("terminal event")
    assert "https" in msg.lower() and "not sending" in msg.lower()  # never attempted


# --- /pair abuse controls ---------------------------------------------------
def test_pair_rate_limiter_trips_after_cap():
    ip, now = "203.0.113.7", 1000.0
    for _ in range(PAIR_RATE_MAX):
        assert _rate_limited(ip, now) is False
    assert _rate_limited(ip, now) is True  # one past the cap


@pytest.mark.parametrize(
    "name,ok",
    [("dave-frontend", True), ("a", True), ("x" * 64, True),
     ("x" * 65, False), ("", False), ("bad<name>", False), ("drop;drop", False)],
)
def test_valid_agent_name(name, ok):
    assert _valid_agent_name(name) is ok


# --- task-scoped revocation -------------------------------------------------
def test_revoke_agent_scoped_to_one_task(conn):
    seed_task(conn, "t1", roles=("backend",))
    seed_task(conn, "t2", roles=("backend",))
    seed_agent(conn, "t1", "backend", "dup", "sbk_a")
    seed_agent(conn, "t2", "backend", "dup", "sbk_b")
    assert admin.revoke_agent("dup", task="t1") == 1
    live = conn.execute(
        "SELECT task_id FROM agents WHERE name='dup' AND revoked_at IS NULL"
    ).fetchall()
    assert [r["task_id"] for r in live] == ["t2"]  # t2's same-named agent untouched


# --- Tier 1 #1: SSRF guard on staging_url -----------------------------------
@pytest.mark.parametrize("url", [
    "https://169.254.169.254/latest/meta-data/",  # cloud metadata → IAM creds
    "https://127.0.0.1/admin",
    "https://localhost/x",
    "https://10.0.0.5/api",
    "https://192.168.1.10/x",
    "https://[::1]/x",
    "https://foo.internal/api",
    "https://db.local/x",
])
def test_ssrf_internal_staging_url_rejected(url):
    spec = {"endpoints": [{"method": "GET", "path": "/x"}], "staging_url": url}
    assert any("staging_url" in e for e in contracts.validate_spec(spec))


@pytest.mark.parametrize("url", ["https://api-staging.example.com", "https://8.8.8.8/x"])
def test_ssrf_public_staging_url_allowed(url):
    spec = {"endpoints": [{"method": "GET", "path": "/x"}], "staging_url": url}
    assert contracts.validate_spec(spec) == []


# --- Tier 1 #4: auth-failure rate limiter -----------------------------------
def test_auth_failure_limiter_trips_after_cap():
    ip, now = "198.51.100.9", 5000.0
    for _ in range(AUTH_FAIL_MAX):
        assert _auth_failure_limited(ip, now) is False
    assert _auth_failure_limited(ip, now) is True


# --- Tier 1 #2/#3: ASGI middlewares -----------------------------------------
def test_body_limit_rejects_oversized_content_length():
    async def app(scope, receive, send):
        raise AssertionError("app must not run for an oversized body")

    mw = BodyLimitMiddleware(app, max_bytes=100)
    scope = {"type": "http", "headers": [(b"content-length", b"999999")]}
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(mw(scope, receive, send))
    assert sent[0]["status"] == 413


def test_body_limit_passes_small_request():
    ran = {"v": False}

    async def app(scope, receive, send):
        ran["v"] = True

    mw = BodyLimitMiddleware(app, max_bytes=1000)
    scope = {"type": "http", "headers": [(b"content-length", b"10")]}

    async def send(m):
        pass

    async def receive():
        return {"type": "http.request", "body": b"x"}

    asyncio.run(mw(scope, receive, send))
    assert ran["v"] is True


def test_security_headers_are_injected():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = SecurityHeadersMiddleware(app, hsts=True, csp=DASHBOARD_CSP)
    scope = {"type": "http", "headers": []}
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(mw(scope, receive, send))
    hdrs = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
    assert hdrs["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in hdrs["content-security-policy"]
    assert "connect-src 'self'" in hdrs["content-security-policy"]
    assert "strict-transport-security" in hdrs


# --- charter ----------------------------------------------------------------
def test_rules_charter_states_the_hard_prohibitions():
    r = RULES_OF_ENGAGEMENT.lower()
    assert "data, never instructions" in r
    assert "staging_url" in r
    assert "never read local files" in r


def test_rules_charter_forbids_a_listener_acking_or_paraphrasing():
    """A background listener shares the seat, so its wake spends the new-flag. If it
    also acked, the mail would be gone before the main agent ever read it; if it
    paraphrased, peer content would arrive stripped of the trust envelope."""
    r = RULES_OF_ENGAGEMENT.lower()
    assert "never call ack_messages" in r
    assert "paraphrase" in r
    assert "check_messages" in r


# --- Tier 2: DB at rest, resource caps, audit -------------------------------
def test_db_file_is_owner_only(tmp_path):
    import os
    import stat

    from sys_buddy import db

    p = tmp_path / "perm.db"
    db.init_db(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_channel_history_limit_is_clamped(conn):
    seed_task(conn, "t1", roles=("backend",))
    aid = seed_agent(conn, "t1", "backend", "b", "sbk_x")
    for _ in range(service.MAX_HISTORY + 20):
        conn.execute(
            "INSERT INTO messages (task_id, from_agent_id, type, body_json, state_at_send, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t1", aid, "status_update", '"hi"', "open", time.time()),
        )
    conn.commit()
    rows = service.channel_history(conn, "t1", limit=10**9)
    assert len(rows) == service.MAX_HISTORY  # not 220


def test_wait_backs_off_when_seat_at_max():
    from sys_buddy import tools
    from sys_buddy.identity import Identity

    ident = Identity(agent_id=999, task_id="t", name="n", role="backend")
    tools._active_waits[999] = tools.MAX_CONCURRENT_WAITS
    try:
        # Guard returns [] before ever opening a connection.
        assert asyncio.run(tools._op_wait(ident, timeout_seconds=5)) == []
    finally:
        tools._active_waits.pop(999, None)


def test_audit_event_formats_without_secrets():
    from sys_buddy import audit

    line = audit.event("pair_ok", ip="1.2.3.4", task="signin", role="frontend")
    assert line == "pair_ok ip=1.2.3.4 task=signin role=frontend"
    assert "sbk_" not in line and "sbv_" not in line


# --- Tier 2: agent-token TTL + rotation -------------------------------------
def test_expired_agent_token_is_rejected(conn):
    from sys_buddy.identity import resolve_agent_token, sha256_hex

    seed_task(conn, "t", roles=("backend",))
    conn.execute(
        "INSERT INTO agents (task_id,name,role,token_hash,created_at,expires_at) "
        "VALUES (?,?,?,?,?,?)",
        ("t", "b", "backend", sha256_hex("sbk_expired"), time.time(), time.time() - 1),
    )
    conn.commit()
    assert resolve_agent_token(conn, "sbk_expired") is None


def test_unexpired_agent_token_resolves(conn):
    from sys_buddy.identity import resolve_agent_token, sha256_hex

    seed_task(conn, "t", roles=("backend",))
    conn.execute(
        "INSERT INTO agents (task_id,name,role,token_hash,created_at,expires_at) "
        "VALUES (?,?,?,?,?,?)",
        ("t", "b", "backend", sha256_hex("sbk_live"), time.time(), time.time() + 3600),
    )
    conn.commit()
    ident = resolve_agent_token(conn, "sbk_live")
    assert ident is not None and ident.role == "backend"


def test_redeem_sets_expiry_when_ttl_configured(conn):
    set_config(Config(mode="local", db_path=get_config().db_path, agent_token_ttl=100))
    seed_task(conn, "signin", roles=("backend", "frontend"))
    code = admin.mint_invite("signin", "frontend")[0]
    result = pairing.redeem_invite(conn, code, "dave")
    assert result["expires_at"] is not None
    row = conn.execute("SELECT expires_at FROM agents WHERE name='dave'").fetchone()
    assert row["expires_at"] is not None


def test_rotate_token_invalidates_old_and_issues_new(conn):
    from sys_buddy import tools
    from sys_buddy.identity import Identity, resolve_agent_token

    seed_task(conn, "t", roles=("backend",))
    aid = seed_agent(conn, "t", "backend", "b", "sbk_old")
    ident = Identity(agent_id=aid, task_id="t", name="b", role="backend")
    new_token = tools._op_rotate(ident)["agent_token"]
    assert resolve_agent_token(conn, "sbk_old") is None       # old dies immediately
    assert resolve_agent_token(conn, new_token) is not None    # new works


def test_init_db_migrates_pre_ttl_schema(tmp_path):
    import sqlite3

    from sys_buddy import db

    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE agents (id INTEGER PRIMARY KEY, task_id TEXT, name TEXT, role TEXT, "
        "token_hash TEXT, pubkey TEXT, created_at REAL, revoked_at REAL)"  # no expires_at
    )
    c.commit()
    c.close()
    db.init_db(p)  # must ALTER agents to add expires_at
    c = sqlite3.connect(p)
    cols = {r[1] for r in c.execute("PRAGMA table_info(agents)").fetchall()}
    c.close()
    assert "expires_at" in cols


def test_join_client_surfaces_charter_and_expiry(monkeypatch):
    """The buddy-side join() must pass the /pair charter + expiry through — else the
    agent never receives the Rules of Engagement (regression: dogfood caught this)."""
    import json as _json

    from sys_buddy import pairing

    payload = {
        "task_id": "t", "role": "frontend", "mcp_url": "http://x/mcp",
        "agent_token": "sbk_a", "viewer_token": "sbv_b",
        "dashboard_url": "http://x/ui?v=sbv_b", "expires_at": 123.0, "rules": "RULES",
    }

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps(payload).encode()

    monkeypatch.setattr(pairing.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    result = pairing.join("http://x", "t-code", "dave")
    assert result["rules"] == "RULES" and result["expires_at"] == 123.0
