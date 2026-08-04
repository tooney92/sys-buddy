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
    """The broker-stamped identity for a request. Never built from tool input.

    ``role`` holds the SEAT HANDLE — WHO you are on this task (``frontend-2``), not
    what kind of work you do. That is deliberate and it is why this feature was small:
    every party list, every quorum check and every provenance record already keyed on
    ``identity.role``, and a handle is precisely the thing they all needed to key on.
    Before seats and role types were split apart the two were the same string, so
    nothing that reads this field changed meaning for an existing task.

    ``role_type`` is WHAT KIND of work the seat does (``frontend``) — many seats may
    share one. It drives the briefing, the pre-flight question set, the ``@FE`` tags
    and the dashboard colour, and NOTHING that binds. Read it through :attr:`kind`,
    which falls back to the handle for the identities that predate the split (local
    mode, the broker's own seat, and every test that constructs one positionally).
    """

    agent_id: int
    task_id: str
    name: str
    role: str
    role_type: str | None = None

    @property
    def kind(self) -> str:
        """The role TYPE, falling back to the handle when none was stamped."""
        return self.role_type or self.role


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
        "SELECT id, task_id, name, role, handle, expires_at FROM agents "
        "WHERE token_hash = ? AND revoked_at IS NULL",
        (sha256_hex(token),),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        return None  # expired token — treat exactly like a revoked one
    # The token maps to one SEAT, so `role` is stamped from the handle; `role_type`
    # carries the kind of work alongside it. COALESCE is belt and braces for a row
    # written between the ALTER and the backfill — the migration asserts fullness.
    return Identity(
        agent_id=row["id"],
        task_id=row["task_id"],
        name=row["name"],
        role=row["handle"] or row["role"],
        role_type=row["role"],
    )


def explain_agent_token(conn: sqlite3.Connection, token: str) -> str:
    """Why a token did not resolve: ``'expired'``, ``'revoked'``, or ``'unknown'``.

    For the ERROR MESSAGE only, and called only on the failure path. `resolve_agent_token`
    returns None for all three, and the middleware reported them with one sentence —
    "invalid or revoked agent token" — which is actively misleading for the commonest case
    by far. A tunnelled broker expires agent tokens after 24h; an agent that hits that is
    told it may have been revoked, so its human goes looking for a revocation nobody
    performed. That happened, cost two people an evening, and the fix is to say which.

    Distinguishing them leaks almost nothing: an attacker holding no token still gets
    'unknown'. Only somebody who once held a real token learns it has expired — and that
    somebody is the legitimate agent trying to work out why it stopped.
    """
    if not token:
        return "unknown"
    row = conn.execute(
        "SELECT revoked_at, expires_at FROM agents WHERE token_hash = ?",
        (sha256_hex(token),),
    ).fetchone()
    if row is None:
        return "unknown"
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        return "expired"
    return "unknown"


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
