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

    state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "all green")
    assert _slack_events(conn) == 2  # verified fired another


def test_third_strike_writes_a_stuck_slack_event(conn):
    set_config(Config(mode="local", db_path=get_config().db_path, slack_webhook=None))
    ag = _drive_to_live(conn)
    for _ in range(3):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red")
    # 1 (lock) + 1 (auto-stuck at 3 strikes)
    assert _slack_events(conn) == 2
    assert conn.execute("SELECT state FROM tasks WHERE id='signin'").fetchone()["state"] == "stuck"
