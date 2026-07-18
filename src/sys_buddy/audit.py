"""Security audit log — a durable, greppable trail of security-relevant events.

Emits one structured line per event to stderr (auth failures, rate-limit trips,
pairing, revocations, task close). This is the signal an operator watches to spot a
brute-force attempt or an unexpected revocation.

HARD RULE: never pass a secret here — no tokens, no invite codes, no webhook URLs.
Only non-sensitive identifiers (task, role, name, ip, counts, reasons).
"""

from __future__ import annotations

import logging
import sys

_log = logging.getLogger("sys_buddy.audit")
if not _log.handlers:  # configure once; independent of the app's logging setup
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s sys-buddy.audit %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def _format(kind: str, fields: dict) -> str:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{kind} {parts}".rstrip()


def event(kind: str, **fields) -> str:
    """Record a security event. Returns the formatted line (handy for tests)."""
    line = _format(kind, fields)
    _log.info(line)
    return line
