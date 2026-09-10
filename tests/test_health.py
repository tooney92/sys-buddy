"""Specs for tunnel discovery, reachability probes, and the session health report.

Resuming a day-old collaboration broke in four independent ways at once — rotated broker
tunnel, tokens hours from expiry, dead staging target, a peer's mislaid dashboard link —
and only the LAST wanted a re-invite. So the report exists to keep "resume" from being a
single destructive button: every row is probed, and each carries its own targeted fix.

The load-bearing rule, and the one a naive implementation gets wrong:
**alive means "answered", not "answered 200".** A healthy API returns 404 at `/`.
"""

from __future__ import annotations

import time
import urllib.error

import pytest

from sys_buddy import admin, health, tunnels
from tests.conftest import seed_agent, seed_task


# --------------------------------------------------------------------------- #
# probe — the 404-is-healthy rule
# --------------------------------------------------------------------------- #
def test_a_404_counts_as_ALIVE(monkeypatch):
    """The rule a naive `status == 200` check gets wrong every session: a Rails/API
    backend answers 404 at `/`, and that means it is up, not down."""
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    monkeypatch.setattr(tunnels.urllib.request, "urlopen", boom)
    p = tunnels.probe("https://app.example.com")
    assert p["alive"] is True
    assert p["status"] == 404


def test_a_500_also_counts_as_alive(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 500, "err", {}, None)
    monkeypatch.setattr(tunnels.urllib.request, "urlopen", boom)
    assert tunnels.probe("https://app.example.com")["alive"] is True


def test_an_unreachable_host_is_dead(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(tunnels.urllib.request, "urlopen", boom)
    p = tunnels.probe("https://gone.example.com")
    assert p["alive"] is False
    assert "unreachable" in p["detail"]


def test_probe_never_raises(monkeypatch):
    """A health check that crashes is a worse way of saying 'no'."""
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(tunnels.urllib.request, "urlopen", boom)
    assert tunnels.probe("https://x.example.com")["alive"] is False


def test_an_empty_url_is_dead_not_a_crash():
    assert tunnels.probe("")["alive"] is False


# --------------------------------------------------------------------------- #
# ngrok discovery
# --------------------------------------------------------------------------- #
def _fake_agent(monkeypatch, payload):
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps(payload).encode()
    monkeypatch.setattr(tunnels.urllib.request, "urlopen", lambda *a, **k: _R())


def test_discover_skips_the_http_twin(monkeypatch):
    """ngrok publishes http AND https for one tunnel. The token rides this URL, so the
    cleartext twin must never be the answer handed to a peer."""
    _fake_agent(monkeypatch, {"tunnels": [
        {"name": "t", "public_url": "http://abc.ngrok-free.app", "config": {"addr": "http://localhost:8787"}},
        {"name": "t", "public_url": "https://abc.ngrok-free.app", "config": {"addr": "http://localhost:8787"}},
    ]})
    got = tunnels.discover_ngrok()
    assert [t["public_url"] for t in got] == ["https://abc.ngrok-free.app"]


def test_no_ngrok_is_an_absence_not_an_error(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(tunnels.urllib.request, "urlopen", boom)
    assert tunnels.discover_ngrok() == []


def test_matches_the_tunnel_by_forwarded_port(monkeypatch):
    """'A tunnel exists' is not 'a tunnel to the BROKER exists'. One aimed at the wrong
    port surfaces on a peer's machine as an inexplicable auth error."""
    _fake_agent(monkeypatch, {"tunnels": [
        {"name": "a", "public_url": "https://wrong.ngrok-free.app", "config": {"addr": "http://localhost:3000"}},
        {"name": "b", "public_url": "https://right.ngrok-free.app", "config": {"addr": "http://localhost:8787"}},
    ]})
    assert tunnels.ngrok_for_port(8787)["public_url"] == "https://right.ngrok-free.app"
    assert tunnels.ngrok_for_port(9999) is None


# --------------------------------------------------------------------------- #
# remembering the public URL — the fact that makes "CHANGED" sayable
# --------------------------------------------------------------------------- #
def test_remember_public_url_returns_the_previous_one(conn):
    assert admin.remember_public_url("https://one.ngrok.app") is None
    assert admin.remember_public_url("https://two.ngrok.app") == "https://one.ngrok.app"
    assert admin.get_setting(admin.LAST_PUBLIC_URL) == "https://two.ngrok.app"


def test_remembering_the_same_url_is_a_no_op(conn):
    admin.remember_public_url("https://one.ngrok.app")
    assert admin.remember_public_url("https://one.ngrok.app") == "https://one.ngrok.app"


def test_no_public_url_records_nothing(conn):
    assert admin.remember_public_url(None) is None
    assert admin.get_setting(admin.LAST_PUBLIC_URL) is None


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #
def _stub(monkeypatch, *, staging_alive=True, tunnel="https://now.ngrok-free.app"):
    monkeypatch.setattr(tunnels, "probe", lambda url, timeout=8.0: {
        "url": url, "alive": (staging_alive or "127.0.0.1" in url),
        "status": 404 if staging_alive else None,
        "detail": "answered 404" if staging_alive else "unreachable (dead)"})
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda port, timeout=3.0:
                        {"public_url": tunnel, "forwards_to": f"http://localhost:{port}"} if tunnel else None)
    monkeypatch.setattr(tunnels, "discover_ngrok", lambda timeout=3.0: [])


def _task(conn, task="signin", staging="https://app.example.com", expires_at=None):
    seed_task(conn, task, roles=("backend", "frontend"))
    conn.execute("UPDATE tasks SET staging_url=? WHERE id=?", (staging, task))
    seed_agent(conn, task, "backend", "Tony", f"sbk_{task}_be")
    if expires_at is not None:
        conn.execute("UPDATE agents SET expires_at=? WHERE task_id=?", (expires_at, task))
    conn.commit()
    return task


def _row(rep, key):
    return next(r for r in rep["rows"] if r["key"] == key)


def test_a_dead_staging_target_is_reported_dead_with_the_fix(conn, monkeypatch):
    t = _task(conn)
    _stub(monkeypatch, staging_alive=False)
    rep = health.session_report(t)
    row = _row(rep, "staging")
    assert row["status"] == health.DEAD
    assert "staging-url" in row["cli"], "the row carries the command that fixes it"
    assert rep["overall"] == health.DEAD


def test_a_live_staging_target_answering_404_is_OK(conn, monkeypatch):
    t = _task(conn)
    _stub(monkeypatch, staging_alive=True)
    assert _row(health.session_report(t), "staging")["status"] == health.OK


def test_a_rotated_tunnel_is_reported_as_CHANGED(conn, monkeypatch):
    """The fact that actually matters: not what the URL is, but that it MOVED — because
    that is the moment every peer's MCP config went stale."""
    t = _task(conn)
    _stub(monkeypatch, tunnel="https://now.ngrok-free.app")
    row = _row(health.session_report(t, known_public_url="https://then.ngrok-free.app"),
               "public_url")
    assert row["status"] == health.WARN
    assert "CHANGED" in row["detail"]
    assert "then.ngrok-free.app" in row["detail"], "name the old one so it is recognisable"


def test_an_unchanged_tunnel_is_quiet(conn, monkeypatch):
    t = _task(conn)
    _stub(monkeypatch, tunnel="https://same.ngrok-free.app")
    row = _row(health.session_report(t, known_public_url="https://same.ngrok-free.app"),
               "public_url")
    assert row["status"] == health.OK


def test_tokens_close_to_expiry_warn_while_there_is_still_time(conn, monkeypatch):
    t = _task(conn, expires_at=time.time() + 3 * 3600)
    _stub(monkeypatch)
    row = _row(health.session_report(t), "tokens")
    assert row["status"] == health.WARN
    assert "extend-tokens" in row["cli"]


def test_expired_tokens_are_dead(conn, monkeypatch):
    t = _task(conn, expires_at=time.time() - 60)
    _stub(monkeypatch)
    assert _row(health.session_report(t), "tokens")["status"] == health.DEAD


def test_comfortable_tokens_are_quiet(conn, monkeypatch):
    t = _task(conn, expires_at=time.time() + 40 * 3600)
    _stub(monkeypatch)
    assert _row(health.session_report(t), "tokens")["status"] == health.OK


def test_a_joined_seat_offers_its_dashboard_link(conn, monkeypatch):
    """The row that used to require revoking a healthy seat."""
    t = _task(conn)
    _stub(monkeypatch)
    row = _row(health.session_report(t), "seat:backend")
    assert row["status"] == health.OK
    assert "viewer-link" in row["cli"], "buddies get viewer-link, not guest-link"


def test_a_guest_seat_is_offered_guest_link_not_viewer_link(conn, monkeypatch):
    t = _task(conn)
    admin.add_guest(t, "Ada")
    _stub(monkeypatch)
    row = _row(health.session_report(t), "seat:guest")
    assert "guest-link" in row["cli"]


def test_the_report_changes_nothing(conn, monkeypatch):
    """It is a diagnosis. If running it mutated anything, it could not be safe to run on a
    session that is working fine."""
    t = _task(conn)
    _stub(monkeypatch)
    before = conn.execute("SELECT COUNT(*) c FROM viewers").fetchone()["c"]
    agents_before = conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"]
    health.session_report(t)
    assert conn.execute("SELECT COUNT(*) c FROM viewers").fetchone()["c"] == before
    assert conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"] == agents_before


def test_an_unknown_task_is_refused(conn, monkeypatch):
    _stub(monkeypatch)
    with pytest.raises(ValueError, match="unknown task"):
        health.session_report("no-such-task")
