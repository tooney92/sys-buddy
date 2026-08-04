"""Version-awareness logic (sys_buddy.updates).

The comparison and the three-state banner decision are pure logic and are tested
directly. Network calls (the broker's /api/version, GitHub's latest release) are
monkeypatched — a version check must degrade gracefully and never raise.

The check is UNCONDITIONAL now. It used to be opt-in behind a footer checkbox that
defaulted to off, which meant it told nobody anything: four releases shipped while the
owner ran 1.4.0 and hit bugs live that were already fixed upstream. `check_github`
survives as a parameter so the offline half stays testable, and the tests below pin both
the new default and the fact that the desktop app no longer passes a flag at all.
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
    assert s["published"] is None  # explicitly local-only → no release call


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


# --- status: the explicit local-only path -----------------------------------
def test_no_release_call_when_explicitly_local_only(monkeypatch):
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


# --- the check is unconditional --------------------------------------------
def test_status_checks_for_a_release_by_default(monkeypatch):
    """The regression that matters. The default used to be False, and the GUI passed a
    flag read from a footer checkbox that started unticked — so the update banner was
    unreachable and four releases went by unnoticed. Calling `status()` with no flag must
    now actually look."""
    called = []
    monkeypatch.setattr(updates, "running_version", lambda *a, **k: None)
    monkeypatch.setattr(
        updates, "latest_release",
        lambda *a, **k: (called.append(1), {"version": "9.9.9", "name": "v9.9.9",
                                            "notes": "n", "url": "u"})[1],
    )
    s = updates.status("http://x")
    assert called, "status() did not check for a release"
    assert s["update_available"] is True
    assert s["published"] == "9.9.9"


def test_the_offline_half_is_still_reachable(monkeypatch):
    """`check_github=False` is kept so an operator or a test can ask for the local-only
    answer — the restart-needed check needs no network and must stay usable alone."""
    def boom(*a, **k):
        raise AssertionError("must not check for a release when explicitly told not to")

    monkeypatch.setattr(updates, "running_version", lambda *a, **k: "1.0.0")
    monkeypatch.setattr(updates, "latest_release", boom)
    s = updates.status("http://x", check_github=False)
    assert s["published"] is None
    assert s["update_available"] is False


# --- where a human reads what changed ---------------------------------------
def test_releases_url_is_always_present_even_when_offline(monkeypatch):
    """The banner links to the SITE's releases page — the same history in prose, newest
    first, with breaking changes called out — rather than a raw GitHub tag. It is a
    constant, so it survives the network call failing."""
    monkeypatch.setattr(updates, "running_version", lambda *a, **k: None)
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: None)   # offline
    s = updates.status("http://x")
    assert s["releases_url"] == updates.RELEASES_URL
    assert s["releases_url"] == "https://sys-buddy.com/releases"
    assert s["release_url"] is None      # no GitHub answer, but the site link stands


def test_the_desktop_app_does_not_re_gate_the_check():
    """A source-level drift guard on the other half of this fix. The app must call
    `check_version()` with NO argument — passing a flag from the page is exactly what made
    this opt-out-able — and the checkbox that used to feed it must stay gone."""
    from pathlib import Path

    from sys_buddy import gui

    html = (Path(gui.__file__).parent / "gui_app.html").read_text(encoding="utf-8")
    assert "check_version()" in html, "the app should call check_version with no flag"
    for gone in ("upd-optin", "optedIn", "sysbuddy.checkUpdates"):
        assert gone not in html, f"{gone} is back — the update check has been re-gated"
    assert "releases_url" in html, "the banner should prefer the site's releases page"


def test_the_gui_bridge_takes_no_flag_and_checks(monkeypatch):
    """The half a source-grep cannot catch, and the bug this nearly shipped as.

    `GuiApi.check_version` used to be `(self, check_github=False)` and forward that flag. So
    the moment the page stopped passing one — which is the fix above — the bridge would
    have quietly defaulted to False and disabled the check completely, while every
    `updates.status` test still passed. The bridge must take NO argument and must actually
    look."""
    import inspect

    from sys_buddy import gui

    sig = inspect.signature(gui.GuiApi.check_version)
    assert list(sig.parameters) == ["self"], (
        "check_version must take no flag — a parameter here is one somebody can default "
        "to False again"
    )

    called = []
    monkeypatch.setattr(updates, "running_version", lambda *a, **k: None)
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: (
        called.append(1), {"version": "9.9.9", "name": "v9.9.9", "notes": "", "url": "u"}
    )[1])
    s = gui.GuiApi().check_version()
    assert called, "the bridge did not check for a release"
    assert s["update_available"] is True
    assert s["releases_url"] == "https://sys-buddy.com/releases"
