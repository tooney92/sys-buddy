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

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware

from .config import get_config
from .db import connect
from .identity import resolve_agent_token, set_current


def _bearer_token(headers) -> str:
    auth = headers.get("authorization", "") or headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


class AuthMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        cfg = get_config()

        # Local mode: no auth, identity comes from tool params. Step aside.
        if not cfg.is_remote:
            set_current(None)
            return await call_next(context)

        # Remote mode: resolve the bearer token to a broker-stamped identity.
        try:
            request = get_http_request()
        except RuntimeError:
            # No HTTP request in scope (e.g. stdio) — remote mode requires HTTP.
            raise ToolError("unauthorized: remote mode requires an HTTP request with a bearer token")

        token = _bearer_token(request.headers)
        conn = connect()
        try:
            identity = resolve_agent_token(conn, token)
        finally:
            conn.close()

        if identity is None:
            raise ToolError("unauthorized: invalid or revoked agent token")

        set_current(identity)
        try:
            return await call_next(context)
        finally:
            set_current(None)
