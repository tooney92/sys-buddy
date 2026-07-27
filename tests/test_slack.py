"""Specs for Slack notifications (SPEC §8/§14) — the third ported bug fix.

The load-bearing property: notifying a human is best-effort and must NEVER raise,
so a Slack outage can't derail an agent's turn.
"""

from __future__ import annotations

from sys_buddy import slack, state
from sys_buddy.config import Config, get_config, set_config
from tests.conftest import seed_agent, seed_task


def test_notify_is_soft_when_no_webhook_configured(conn):
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook=None))
    msg = slack.notify("anything")
    assert "final response" in msg  # tells the agent to relay it itself, doesn't raise


def test_notify_never_raises_on_network_failure(conn, monkeypatch):
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook="https://hooks.example/x"))

    def boom(*a, **k):
        raise TimeoutError("slack is down")

    monkeypatch.setattr(slack.urllib.request, "urlopen", boom)
    msg = slack.notify("terminal event")  # must not raise
    assert "failed" in msg.lower()


def _drive_to_live(conn, roles=("backend", "frontend")):
    seed_task(conn, "signin", roles=roles)
    ag = {
        r: state.Identity(seed_agent(conn, "signin", r, f"{r}-a", f"sbk_{r}"), "signin", f"{r}-a", r)
        for r in roles
    }
    state.propose_contract(conn, ag["backend"], {
        "version": 1, "endpoints": [{"method": "POST", "path": "/x"}],
        "staging_url": "https://s.example.com",
    })
    for ident in ag.values():
        state.lock_contract(conn, ident, 1)
    state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "live")
    return ag


def _slack_events(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM events WHERE task_id='signin' AND kind='slack'").fetchone()["n"]


def test_lock_and_verified_each_write_a_slack_event(conn):
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook=None))
    ag = _drive_to_live(conn)
    assert _slack_events(conn) == 1  # the lock fired one

    # verified is only reachable from 'testing' (SPEC §5) — a test result first.
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "12/12")
    state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "all green")
    assert _slack_events(conn) == 2  # verified fired another (test_passed writes none)


def test_third_strike_writes_a_stuck_slack_event(conn):
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook=None))
    ag = _drive_to_live(conn)
    for _ in range(3):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red")
    # 1 (lock) + 1 (auto-stuck at 3 strikes)
    assert _slack_events(conn) == 2
    assert conn.execute("SELECT state FROM tasks WHERE id='signin'").fetchone()["state"] == "stuck"


# --------------------------------------------------------------------------- #
# webhook validation + configured-state reflection
# --------------------------------------------------------------------------- #
# The webhook is a replayable bearer credential, so it is process-local and NEVER
# persisted (every other credential in the db is a sha256 hash). These specs pin the
# two properties that protect that: https-only, and the dashboard sees a boolean.
def test_validate_webhook_requires_https():
    assert slack.validate_webhook("http://hooks.slack.com/services/x") is not None
    assert slack.validate_webhook("https://hooks.slack.com/services/x") is None


def test_validate_webhook_rejects_non_web_schemes():
    for bad in ("file:///etc/passwd", "gopher://x/y", "ftp://h/x"):
        assert slack.validate_webhook(bad) is not None


def test_validate_webhook_rejects_blank():
    assert slack.validate_webhook("") is not None
    assert slack.validate_webhook("   ") is not None


def test_validate_webhook_allows_non_slack_hosts():
    # Self-hosted relays and test doubles are legitimate; https is the boundary, not
    # the vendor hostname.
    assert slack.validate_webhook("https://relay.internal/hooks/abc") is None


def test_is_configured_reflects_process_config(conn):
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook=None))
    assert slack.is_configured() is False

    set_config(
        Config(mode="local", db_path=get_config().db_path, slack_webhook="https://hooks.example/x")
    )
    assert slack.is_configured() is True


def test_is_configured_false_for_an_unusable_webhook(conn):
    # A misconfigured http:// webhook would never send, so the dashboard must not
    # advertise Slack as active — that would be worse than showing nothing.
    set_config(
        Config(mode="local", db_path=get_config().db_path, slack_webhook="http://hooks.example/x")
    )
    assert slack.is_configured() is False


def test_viewer_block_never_leaks_the_webhook(conn):
    from sys_buddy import api
    from sys_buddy.identity import resolve_viewer_token
    from tests.conftest import seed_viewer

    secret = "https://hooks.slack.com/services/T00/B00/SUPERSECRET"
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook=secret))
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)

    block = api.viewer_block(resolve_viewer_token(conn, "sbv_host"))

    assert block["slack_active"] is True
    assert secret not in repr(block)
    assert "SUPERSECRET" not in repr(block)
