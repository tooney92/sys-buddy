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

import re

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


# --------------------------------------------------------------------------- #
# apply_fix — one row, one targeted move
# --------------------------------------------------------------------------- #
def _seeded(conn, task="signin"):
    seed_task(conn, task, roles=("backend", "frontend"))
    seed_agent(conn, task, "backend", "Tony", f"sbk_{task}_be")
    seed_agent(conn, task, "frontend", "Peter", f"sbk_{task}_fe")
    conn.commit()
    return task


def _no_tunnel(monkeypatch):
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda *a, **k: None)


def test_extend_tokens_lifts_the_expiry_and_says_who(conn, monkeypatch):
    t = _seeded(conn)
    conn.execute("UPDATE agents SET expires_at=? WHERE task_id=?", (1.0, t))
    conn.commit()
    res = _api().apply_fix("extend_tokens", {"task": t})
    assert res["ok"] is True
    assert "@backend" in res["message"] and "@frontend" in res["message"], "name the seats"
    rows = conn.execute("SELECT expires_at FROM agents WHERE task_id=?", (t,)).fetchall()
    assert all(r["expires_at"] is None for r in rows)


def test_set_staging_updates_the_target_without_touching_contracts(conn, monkeypatch):
    """The whole reason the target lives outside the signed contract."""
    t = _seeded(conn)
    sigs = conn.execute("SELECT COUNT(*) c FROM contract_signatures").fetchone()["c"]
    res = _api().apply_fix("set_staging", {"task": t, "url": "https://new.example.com"})
    assert res["ok"] is True
    assert "No contract or signature changed" in res["message"]
    assert conn.execute("SELECT staging_url FROM tasks WHERE id=?", (t,)).fetchone()[0] \
        == "https://new.example.com"
    assert conn.execute("SELECT COUNT(*) c FROM contract_signatures").fetchone()["c"] == sigs


def test_set_staging_refuses_an_empty_url(conn):
    t = _seeded(conn)
    assert "error" in _api().apply_fix("set_staging", {"task": t, "url": "  "})


def test_viewer_link_copies_a_link_and_leaves_the_seat_alone(conn, monkeypatch):
    """The fix that replaces revoke-and-re-pair."""
    t = _seeded(conn)
    _no_tunnel(monkeypatch)
    before = conn.execute(
        "SELECT id FROM agents WHERE task_id=? AND handle='frontend'", (t,)).fetchone()["id"]
    res = _api().apply_fix("viewer_link", {"task": t, "who": "Peter"})
    assert res["ok"] is True
    assert "/ui?v=sbv_" in res["copy"], "the link is what gets copied"
    after = conn.execute(
        "SELECT id, revoked_at FROM agents WHERE task_id=? AND handle='frontend'", (t,)).fetchone()
    assert after["id"] == before and after["revoked_at"] is None, "seat untouched"


def test_viewer_link_uses_the_GUEST_path_for_a_guest(conn, monkeypatch):
    """A guest's link is write-capable; handing her a read-only one silently removes her
    message box."""
    t = _seeded(conn)
    _no_tunnel(monkeypatch)
    admin.add_guest(t, "Ada")
    res = _api().apply_fix("viewer_link", {"task": t, "who": "Ada"})
    assert res["ok"] is True
    row = conn.execute(
        "SELECT agent_id FROM viewers WHERE label='Ada' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["agent_id"] is not None, "a guest keeps her write-capable link"


def test_invite_copies_a_join_url_for_an_unfilled_seat(conn, monkeypatch):
    t = _seeded(conn, "fresh")
    _no_tunnel(monkeypatch)
    res = _api().apply_fix("invite", {"task": t, "role": "frontend"})
    assert res["ok"] is True
    assert "/join#c=" in res["copy"]
    assert "expires" in res["message"]


def test_repoint_copies_a_message_with_a_token_PLACEHOLDER(conn, monkeypatch):
    """The host does not have the peer's token — it is stored hashed — so the message
    cannot contain it, and must not pretend to."""
    from sys_buddy import onboarding
    _seeded(conn)
    _no_tunnel(monkeypatch)
    res = _api().apply_fix("repoint", {"url": "https://new.ngrok-free.app"})
    assert res["ok"] is True
    assert onboarding.TOKEN_PLACEHOLDER in res["copy"]
    # No REAL token — the prose may say "Bearer sbk_..." as a hint, so match the shape of
    # an actual credential rather than the bare prefix.
    assert not re.search(r"sbk_[A-Za-z0-9_-]{20,}", res["copy"]), "never a real token"
    assert "https://new.ngrok-free.app/mcp" in res["copy"]


def test_an_unknown_fix_is_an_error_not_a_crash(conn):
    assert "error" in _api().apply_fix("drop_everything", {})


def test_apply_fix_cannot_revoke(conn):
    """Deliberately absent. Revoking burns a peer's working credential, and a destructive
    move does not belong on a panel you glance at."""
    t = _seeded(conn)
    for action in ("revoke", "revoke_agent", "reinvite", "close"):
        assert "error" in _api().apply_fix(action, {"task": t, "who": "Peter"})
    live = conn.execute(
        "SELECT COUNT(*) c FROM agents WHERE task_id=? AND revoked_at IS NULL", (t,)
    ).fetchone()["c"]
    assert live == 2, "both seats still live"


# --------------------------------------------------------------------------- #
# repair_seat — the ONE destructive move, and the guard that keeps it deliberate
# --------------------------------------------------------------------------- #
def test_repair_seat_refuses_without_an_explicit_confirm(conn):
    """The panel is something people click through quickly. A move that revokes a working
    credential must not be reachable by accident, so the bridge insists on seeing that the
    UI actually asked."""
    t = _seeded(conn)
    res = _api().apply_fix("repair_seat", {"task": t, "who": "Peter", "role": "frontend"})
    assert "error" in res and "confirm" in res["error"]
    live = conn.execute(
        "SELECT COUNT(*) c FROM agents WHERE task_id=? AND revoked_at IS NULL", (t,)
    ).fetchone()["c"]
    assert live == 2, "nothing was revoked"


def test_repair_seat_with_confirm_revokes_and_mints_a_fresh_invite(conn, monkeypatch):
    t = _seeded(conn)
    _no_tunnel(monkeypatch)
    res = _api().apply_fix(
        "repair_seat", {"task": t, "who": "Peter", "role": "frontend", "confirm": True})
    assert res["ok"] is True
    assert "/join#c=" in res["copy"], "the fresh invite is what gets copied"
    row = conn.execute(
        "SELECT revoked_at FROM agents WHERE task_id=? AND handle='frontend'", (t,)).fetchone()
    assert row["revoked_at"] is not None, "the old token is dead"


def test_repair_seat_keeps_signatures_and_history(conn, monkeypatch):
    """The reason re-pairing is survivable at all: the seat's record is not erased."""
    t = _seeded(conn)
    _no_tunnel(monkeypatch)
    sigs = conn.execute("SELECT COUNT(*) c FROM contract_signatures").fetchone()["c"]
    msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    _api().apply_fix("repair_seat",
                     {"task": t, "who": "Peter", "role": "frontend", "confirm": True})
    assert conn.execute("SELECT COUNT(*) c FROM contract_signatures").fetchone()["c"] == sigs
    assert conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == msgs


def test_repair_seat_refuses_a_guest(conn, monkeypatch):
    """A guest has no agent token to lose — she joins by link. Offering to re-pair her
    would only be a way to break her."""
    t = _seeded(conn)
    _no_tunnel(monkeypatch)
    admin.add_guest(t, "Ada")
    res = _api().apply_fix(
        "repair_seat", {"task": t, "who": "Ada", "role": "guest", "confirm": True})
    assert "error" in res and "guest" in res["error"]


def test_a_joined_seat_offers_repair_as_a_SECONDARY_action(conn, monkeypatch):
    """It must never be the primary button — the primary is the non-destructive one."""
    t = _seeded(conn)
    monkeypatch.setattr(tunnels, "probe",
                        lambda url, timeout=8.0: {"url": url, "alive": True, "status": 200,
                                                  "detail": "answered"})
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda *a, **k: None)
    monkeypatch.setattr(tunnels, "discover_ngrok", lambda *a, **k: [])
    rep = health.session_report(t)
    row = next(r for r in rep["rows"] if r["key"] == "seat:frontend")
    assert row["action"] == "viewer_link", "the PRIMARY move is the harmless one"
    assert row["secondary"]["action"] == "repair_seat"
    assert "REVOKES" in row["secondary"]["confirm"], "state the cost before it is paid"


def test_a_guest_row_offers_no_repair_at_all(conn, monkeypatch):
    t = _seeded(conn)
    admin.add_guest(t, "Ada")
    monkeypatch.setattr(tunnels, "probe",
                        lambda url, timeout=8.0: {"url": url, "alive": True, "status": 200,
                                                  "detail": "answered"})
    monkeypatch.setattr(tunnels, "ngrok_for_port", lambda *a, **k: None)
    monkeypatch.setattr(tunnels, "discover_ngrok", lambda *a, **k: [])
    rep = health.session_report(t)
    row = next(r for r in rep["rows"] if r["key"] == "seat:guest")
    assert row["secondary"] is None
