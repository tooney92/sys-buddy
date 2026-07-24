"""The version is single-sourced: pyproject.toml is canonical, and
``sys_buddy.__version__`` derives from the installed package metadata.

These tests fail the build the moment the two drift — which is the whole point.
The broker's ``/api/version`` reports ``__version__``, so a stale number here would
mislead the desktop app's "restart needed" nag and the dashboard banner. Catching the
drift in CI is strictly better than a user's broker reporting the wrong version.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import sys_buddy

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_version() -> str:
    return tomllib.loads(_PYPROJECT.read_text())["project"]["version"]


def test_runtime_version_matches_pyproject():
    """The resolved ``__version__`` equals what pyproject declares. If this fails,
    the environment is out of sync — re-run ``uv sync`` (or ``uv run``, which syncs)."""
    assert sys_buddy.__version__ == _declared_version()


def test_version_is_not_the_uninstalled_fallback():
    """A real run must resolve a real version, never the source-tree fallback — that
    sentinel means the package metadata was missing when the module was imported."""
    assert sys_buddy.__version__ != "0.0.0+unknown"
