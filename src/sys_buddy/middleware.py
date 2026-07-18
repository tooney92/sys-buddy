"""Auth middleware: token → identity on every tool call (SPEC §14 step 2).

The single choke point where remote-mode identity is established. It runs before
every MCP tool, reads the bearer token from the HTTP request, resolves it to an
``agents`` row, and stamps the identity into a contextvar the tools read. A bad or
revoked token is rejected here — the tool never runs.

In **local mode** this is a no-op: there is no auth on loopback, and identity is
self-declared via tool parameters (SPEC §3). Same code path, middleware just steps
aside.
"""

from __future__ import annotations

import time

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware

from . import audit
from .config import get_config
from .db import connect
from .identity import resolve_agent_token, set_current

# Anti-brute-force on the auth path (OWASP API2): throttle repeated *failed* token
# attempts per client IP — successful calls are never counted, so a busy agent's
# poll loop is unaffected. Stricter than normal API throttling, by design.
AUTH_FAIL_MAX = 10
AUTH_FAIL_WINDOW = 60.0
_AUTH_FAILS: dict[str, list[float]] = {}


def _auth_failure_limited(ip: str, now: float) -> bool:
    hits = [t for t in _AUTH_FAILS.get(ip, ()) if now - t < AUTH_FAIL_WINDOW]
    hits.append(now)
    _AUTH_FAILS[ip] = hits
    return len(hits) > AUTH_FAIL_MAX


def _bearer_token(headers) -> str:
    auth = headers.get("authorization", "") or headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


class AuthMiddleware(Middleware):
    async def on_request(self, context, call_next):
        """Authenticate EVERY MCP request in remote mode — not just tool calls but the
        ``initialize`` handshake and ``tools/list`` too, so nothing (not even the tool
        catalogue) is reachable without a valid bearer token. Notifications pass
        through (they carry no action); the custom /pair, /ui, /api routes have their
        own gating and are not MCP requests."""
        cfg = get_config()

        # Local mode: no auth, identity comes from tool params. Step aside.
        if not cfg.is_remote:
            set_current(None)
            return await call_next(context)

        # Remote mode: resolve the bearer token to a broker-stamped identity.
        try:
            request = get_http_request()
        except RuntimeError:
            # No HTTP request in scope → an in-process/trusted call (introspection,
            # tests), never the network. A real remote client always carries an HTTP
            # request, so this can't be an attacker bypass; pass it through without an
            # identity. A tool call reaching here still fails later at require_current().
            set_current(None)
            return await call_next(context)

        token = _bearer_token(request.headers)
        conn = connect()
        try:
            identity = resolve_agent_token(conn, token)
        finally:
            conn.close()

        if identity is None:
            ip = request.client.host if request.client else "?"
            if _auth_failure_limited(ip, time.time()):
                audit.event("auth_ratelimit", ip=ip)
                raise ToolError("too many failed auth attempts; slow down and retry shortly")
            audit.event("auth_fail", ip=ip)
            raise ToolError("unauthorized: invalid or revoked agent token")

        set_current(identity)
        try:
            return await call_next(context)
        finally:
            set_current(None)
