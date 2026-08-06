#!/usr/bin/env python3
"""Fail if uv.lock's sys-buddy version disagrees with pyproject.toml.

release-please bumps the project version and leaves the lockfile alone, so after every
release `uv.lock` names the previous one. Nothing about the published package is wrong —
but any `uv run` then rewrites the file, so every contributor inherits a spurious diff.
This has been relocked by hand twice; the check is what makes a third time impossible.

Kept as a file rather than an inline `run:` block because the obvious inline version needs
nested quotes and a heredoc inside an indented YAML scalar, which does not survive the
round trip. (Tried it. It did not.)
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

proj = re.search(
    r'^version\s*=\s*"([^"]+)"',
    (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    re.M,
)
lock = re.search(
    r'\[\[package\]\]\nname = "sys-buddy"\nversion = "([^"]+)"',
    (ROOT / "uv.lock").read_text(encoding="utf-8"),
)

if proj is None:
    sys.exit("::error::could not find a version in pyproject.toml")
if lock is None:
    sys.exit("::error::could not find the sys-buddy package entry in uv.lock")

print(f"pyproject: {proj.group(1)}   uv.lock: {lock.group(1)}")
if proj.group(1) != lock.group(1):
    sys.exit(
        f"::error::uv.lock pins sys-buddy {lock.group(1)} but pyproject.toml says "
        f"{proj.group(1)}. Run `uv lock` and commit it — otherwise every `uv run` "
        f"dirties the working tree."
    )
print("OK — lockfile agrees.")
