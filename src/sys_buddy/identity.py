"""Identity: tokens, hashing, and resolving a bearer token to *who you are*.

This is the load-bearing security primitive. In remote mode the agent never says
who it is — it presents a bearer token, and the broker looks up the matching
``agents`` row to stamp identity (SPEC §4, §9). Because the token maps to exactly
one ``(task, role)``, resolving it yields the agent's full scope in one query.

Rules:
- Never store a raw token or invite code — only its sha256 (SPEC §9).
- A revoked token (``revoked_at`` set) resolves to nothing. Revocation is instant
  because it is checked on every call.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass

# Token prefixes make it obvious at a glance which credential you're holding.
AGENT_PREFIX = "sbk_"   # MCP agent token
VIEWER_PREFIX = "sbv_"  # read-only dashboard token

_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"  # no 0/O/1/I/l


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_agent_token() -> str:
    return AGENT_PREFIX + secrets.token_urlsafe(32)


def new_viewer_token() -> str:
    return VIEWER_PREFIX + secrets.token_urlsafe(32)


def new_invite_code(task_id: str, length: int = 8) -> str:
    """A copy-pasteable single-use code, e.g. ``signin-J7fK2mQx``."""
    suffix = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(length))
    return f"{task_id}-{suffix}"


@dataclass(frozen=True)
class Identity:
    """The broker-stamped identity for a request. Never built from tool input."""

    agent_id: int
    task_id: str
    name: str
    role: str


@dataclass(frozen=True)
class ViewerIdentity:
    viewer_id: int
    label: str
    task_id: str | None  # None = host (all tasks)

    @property
    def is_host(self) -> bool:
        return self.task_id is None


# The current request's identity, set by the auth middleware, read by tools.
_current: ContextVar[Identity | None] = ContextVar("sys_buddy_identity", default=None)


def set_current(identity: Identity | None) -> None:
    _current.set(identity)


def get_current() -> Identity | None:
    return _current.get()


def require_current() -> Identity:
    ident = _current.get()
    if ident is None:
        raise PermissionError("no authenticated identity on this request")
    return ident


def resolve_agent_token(conn: sqlite3.Connection, token: str) -> Identity | None:
    """Return the Identity for a live agent token, or None if invalid/revoked."""
    if not token:
        return None
    row = conn.execute(
        "SELECT id, task_id, name, role, expires_at FROM agents "
        "WHERE token_hash = ? AND revoked_at IS NULL",
        (sha256_hex(token),),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        return None  # expired token — treat exactly like a revoked one
    return Identity(agent_id=row["id"], task_id=row["task_id"], name=row["name"], role=row["role"])


def resolve_viewer_token(conn: sqlite3.Connection, token: str) -> ViewerIdentity | None:
    """Return the ViewerIdentity for a live viewer token, or None if invalid/revoked."""
    if not token:
        return None
    row = conn.execute(
        "SELECT id, label, task_id FROM viewers "
        "WHERE token_hash = ? AND revoked_at IS NULL",
        (sha256_hex(token),),
    ).fetchone()
    if row is None:
        return None
    return ViewerIdentity(viewer_id=row["id"], label=row["label"], task_id=row["task_id"])
