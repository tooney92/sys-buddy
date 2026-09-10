"""Specs for the desktop app's Resume bridge.

The front door that did not exist. Resuming a collaboration used to begin with "where do I
even open my board?" — answerable only by a CLI command plus a viewer token you had to have
kept, because viewer tokens are stored HASHED and can never be read back.

NOTE ON SAFETY: every test here stubs ``_ensure_broker``. The real one starts an in-process
broker on the owner's port, and a test that did that would fight the live app for :8787 —
the exact failure the project's notes warn about, where a second broker dies on errno 48
while requests go to the real one.
"""

from __future__ import annotations

import pytest

from sys_buddy import admin, gui, health, tunnels
from tests.conftest import seed_agent, seed_task


@pytest.fixture(autouse=True)
def _no_real_broker(monkeypatch):
    monkeypatch.setattr(gui, "_ensure_broker", lambda *a, **k: True)


def _api():
    return gui.GuiApi()


def test_dashboard_link_mints_a_host_viewer_and_builds_a_url(conn, monkeypatch):
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda *a, **k: None)
    res = _api().dashboard_link()
    assert res["ok"] is True
    assert "/ui?v=sbv_" in res["url"], "the dashboard needs a viewer token in the URL"
    assert res["tunnelled"] is False
    # and it really is an ALL-TASKS host viewer, not a per-task one
    row = conn.execute("SELECT task_id, label FROM viewers ORDER BY id DESC LIMIT 1").fetchone()
    assert row["task_id"] is None, "host viewer sees every task"
    assert row["label"] == "desktop-app"


def test_dashboard_link_prefers_the_live_tunnel(conn, monkeypatch):
    """A loopback link is useless to the peer standing next to you; if a tunnel is up, the
    link should already work off this machine."""
    monkeypatch.setattr(tunnels, "ngrok_for_port",
                        lambda *a, **k: {"public_url": "https://abc.ngrok-free.app"})
    res = _api().dashboard_link()
    assert res["url"].startswith("https://abc.ngrok-free.app/ui?v=sbv_")
    assert res["tunnelled"] is True


def test_the_token_is_minted_once_per_app_run(conn, monkeypatch):
    """Minting per click would pile up viewer rows; writing it to disk is the one thing
    'sys-buddy stores no credentials' forbids. So: once, in memory, for this run."""
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda *a, **k: None)
    api = _api()
    first, second = api.dashboard_link()["url"], api.dashboard_link()["url"]
    assert first == second
    n = conn.execute("SELECT COUNT(*) c FROM viewers WHERE label='desktop-app'").fetchone()["c"]
    assert n == 1, "one viewer row, not one per click"


def test_resume_tasks_lists_open_tasks_only(conn):
    seed_task(conn, "live-one", roles=("backend", "frontend"))
    seed_task(conn, "done-one", roles=("backend", "frontend"))
    admin.close_task("done-one")
    ids = [t["id"] for t in _api().resume_tasks()]
    assert "live-one" in ids
    assert "done-one" not in ids, "a closed task cannot be resumed"


def test_resume_report_returns_the_health_rows(conn, monkeypatch):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    seed_agent(conn, "signin", "backend", "Tony", "sbk_signin_be")
    conn.commit()
    monkeypatch.setattr(tunnels, "probe",
                        lambda url, timeout=8.0: {"url": url, "alive": True, "status": 200,
                                                  "detail": "answered"})
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda *a, **k: None)
    monkeypatch.setattr(tunnels, "discover_ngrok", lambda *a, **k: [])
    rep = _api().resume_report("signin")
    keys = [r["key"] for r in rep["rows"]]
    assert "broker" in keys and "staging" in keys and "tokens" in keys
    assert any(k.startswith("seat:") for k in keys), "a row per seat"


def test_resume_report_reports_an_unknown_task_as_an_error_not_a_crash(conn):
    """It crosses a pywebview bridge — an exception there is an unhandled rejection the
    user sees as nothing happening at all."""
    res = _api().resume_report("no-such-task")
    assert "error" in res and "unknown task" in res["error"]


def test_the_report_changes_nothing(conn, monkeypatch):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    monkeypatch.setattr(tunnels, "probe",
                        lambda url, timeout=8.0: {"url": url, "alive": True, "status": 200,
                                                  "detail": "answered"})
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda *a, **k: None)
    monkeypatch.setattr(tunnels, "discover_ngrok", lambda *a, **k: [])
    before = conn.execute("SELECT COUNT(*) c FROM viewers").fetchone()["c"]
    _api().resume_report("signin")
    assert conn.execute("SELECT COUNT(*) c FROM viewers").fetchone()["c"] == before
