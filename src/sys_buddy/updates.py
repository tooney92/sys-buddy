"""Version awareness for the desktop app.

Three version numbers can disagree, and each has a different reader:

* **installed** — the code on disk, ``sys_buddy.__version__`` in THIS process.
* **running**   — what the broker process actually booted with, read over
  ``/api/version``. Usually a DIFFERENT process (the GUI attaches to whatever broker
  is already on the port; a remote/Docker broker shares no memory), so this is the
  only way to learn it. A broker still serving last week's code after an upgrade —
  the exact trap that hid a schema migration once — shows up here as running != installed.
* **published** — the latest release on GitHub. Fetched ONLY when the user has opted
  in; the desktop app never phones out otherwise.

``status()`` gathers what it can and returns a banner payload; the GUI decides how to
show it. Network failures are swallowed to ``None`` — a version check must never break
the app or block launch.
"""

from __future__ import annotations

import json
import re
import urllib.request

from sys_buddy import __version__

GITHUB_REPO = "tooney92/sys-buddy"
_HTTP_TIMEOUT = 4.0

# Match a leading semver core (major.minor.patch), tolerating a "v" prefix and any
# pre-release/build suffix (e.g. "v1.1.1", "1.2.0+local"). Anything unparseable sorts
# as (0,0,0), so a garbled value never spuriously claims "update available".
_SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def _parse(v: str | None) -> tuple[int, int, int]:
    m = _SEMVER.match(v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _newer(candidate: str | None, baseline: str | None) -> bool:
    """True only when ``candidate`` is a strictly higher semver than ``baseline``."""
    return _parse(candidate) > _parse(baseline)


def installed_version() -> str:
    """The version of the code THIS process is running from."""
    return __version__


def running_version(base_url: str) -> str | None:
    """Ask the live broker what version IT booted with. None if unreachable — the
    broker may be down, or too old to have the endpoint (pre-1.1.2)."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/version", timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


def latest_release(repo: str = GITHUB_REPO) -> dict | None:
    """The latest published GitHub release: version, title, notes, and link. None on
    any failure. Called ONLY when the user has opted into update checks."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    return {
        "version": (d.get("tag_name") or "").lstrip("v"),
        "name": d.get("name") or d.get("tag_name"),
        "notes": d.get("body") or "",
        "url": d.get("html_url"),
    }


def status(base_url: str, *, check_github: bool = False, repo: str = GITHUB_REPO) -> dict:
    """The banner payload for the GUI.

    ``restart_needed`` — installed differs from the broker's running version, i.e. you
    upgraded but the old broker is still serving. This is the one the local check alone
    can catch, and the one that has actually bitten us.

    ``update_available`` — GitHub has a newer release than what's installed. Only ever
    True when ``check_github`` is set; with it off we make no network call and report
    ``published: None``.
    """
    installed = installed_version()
    running = running_version(base_url)

    published = notes = release_url = release_name = None
    if check_github:
        rel = latest_release(repo)
        if rel is not None:
            published, notes = rel["version"], rel["notes"]
            release_url, release_name = rel["url"], rel["name"]

    return {
        "installed": installed,
        "running": running,
        # running is None when the broker is unreachable — not a mismatch, just unknown.
        "restart_needed": running is not None and running != installed,
        "published": published,
        "update_available": bool(published) and _newer(published, installed),
        "release_name": release_name,
        "notes": notes,
        "release_url": release_url,
    }
