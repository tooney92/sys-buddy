"""sys-buddy — an authenticated, contract-enforcing MCP broker for cross-human
AI agent collaboration.

``__version__`` is derived from the installed package metadata (which pip/uv write
from ``pyproject.toml`` at install time), NOT a second hand-edited literal. There is
therefore ONE place to bump the version — ``pyproject.toml`` — and the two can never
silently disagree. ``tests/test_version.py`` asserts the two stay in lockstep.

The fallback matters: running straight from a source checkout that was never installed
(no ``.dist-info``) raises ``PackageNotFoundError``. That should never happen in the
uv workflow (``uv run`` syncs first), but a bare ``python src/...`` would hit it, and a
version banner is not worth crashing the import over.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sys-buddy")
except PackageNotFoundError:  # running from an uninstalled source tree
    __version__ = "0.0.0+unknown"
