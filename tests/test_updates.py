"""Version-awareness logic (sys_buddy.updates).

The comparison and the three-state banner decision are pure logic and are tested
directly. Network calls (the broker's /api/version, GitHub's latest release) are
monkeypatched — a version check must degrade gracefully, never raise, and never call
GitHub unless the user opted in.
"""

from __future__ import annotations

import pytest

from sys_buddy import updates


# --- semver comparison ------------------------------------------------------
@pytest.mark.parametrize("cand,base,expected", [
    ("1.1.2", "1.1.1", True),
    ("1.2.0", "1.1.9", True),
    ("2.0.0", "1.9.9", True),
    ("1.1.1", "1.1.1", False),   # equal is not newer
    ("1.1.0", "1.1.1", False),   # older
    ("v1.1.2", "1.1.1", True),   # tolerates a leading v
    ("1.1.2+local", "1.1.1", True),  # tolerates a build suffix
    ("garbage", "1.1.1", False),  # unparseable never claims newer
])
def test_newer(cand, base, expected):
    assert updates._newer(cand, base) is expected


# --- status: restart-needed (the trap we actually hit) ----------------------
def test_status_flags_restart_when_broker_is_stale(monkeypatch):
    monkeypatch.setattr(updates, "installed_version", lambda: "1.1.1")
    monkeypatch.setattr(updates, "running_version", lambda base: "1.1.0")
    s = updates.status("http://x", check_github=False)
    assert s["restart_needed"] is True
    assert s["installed"] == "1.1.1" and s["running"] == "1.1.0"
    assert s["published"] is None  # no opt-in → no GitHub call


def test_status_no_restart_when_versions_match(monkeypatch):
    monkeypatch.setattr(updates, "installed_version", lambda: "1.1.1")
    monkeypatch.setattr(updates, "running_version", lambda base: "1.1.1")
    s = updates.status("http://x", check_github=False)
    assert s["restart_needed"] is False


def test_status_unreachable_broker_is_not_a_mismatch(monkeypatch):
    """A broker that's down (running=None) is UNKNOWN, not stale — no false alarm."""
    monkeypatch.setattr(updates, "installed_version", lambda: "1.1.1")
    monkeypatch.setattr(updates, "running_version", lambda base: None)
    s = updates.status("http://x", check_github=False)
    assert s["restart_needed"] is False
    assert s["running"] is None


# --- status: GitHub opt-in --------------------------------------------------
def test_github_not_called_without_optin(monkeypatch):
    monkeypatch.setattr(updates, "installed_version", lambda: "1.1.1")
    monkeypatch.setattr(updates, "running_version", lambda base: "1.1.1")

    def _boom(repo=updates.GITHUB_REPO):
        raise AssertionError("latest_release must not be called when check_github is False")

    monkeypatch.setattr(updates, "latest_release", _boom)
    s = updates.status("http://x", check_github=False)
    assert s["update_available"] is False and s["published"] is None


def test_update_available_when_github_has_newer(monkeypatch):
    monkeypatch.setattr(updates, "installed_version", lambda: "1.1.1")
    monkeypatch.setattr(updates, "running_version", lambda base: "1.1.1")
    monkeypatch.setattr(updates, "latest_release", lambda repo=updates.GITHUB_REPO: {
        "version": "1.2.0", "name": "v1.2.0 — todos", "notes": "- a\n- b",
        "url": "https://github.com/tooney92/sys-buddy/releases/tag/v1.2.0",
    })
    s = updates.status("http://x", check_github=True)
    assert s["update_available"] is True
    assert s["published"] == "1.2.0"
    assert s["notes"] == "- a\n- b"
    assert s["release_url"].endswith("v1.2.0")


def test_no_update_when_github_matches_installed(monkeypatch):
    monkeypatch.setattr(updates, "installed_version", lambda: "1.2.0")
    monkeypatch.setattr(updates, "running_version", lambda base: "1.2.0")
    monkeypatch.setattr(updates, "latest_release", lambda repo=updates.GITHUB_REPO: {
        "version": "1.2.0", "name": "v1.2.0", "notes": "", "url": "https://x",
    })
    s = updates.status("http://x", check_github=True)
    assert s["update_available"] is False


def test_github_failure_degrades_quietly(monkeypatch):
    """If GitHub is unreachable, latest_release returns None — status must not raise
    and must simply report no update rather than blowing up the banner."""
    monkeypatch.setattr(updates, "installed_version", lambda: "1.1.1")
    monkeypatch.setattr(updates, "running_version", lambda base: "1.1.1")
    monkeypatch.setattr(updates, "latest_release", lambda repo=updates.GITHUB_REPO: None)
    s = updates.status("http://x", check_github=True)
    assert s["update_available"] is False and s["published"] is None
