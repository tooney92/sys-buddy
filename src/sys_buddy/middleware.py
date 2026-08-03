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
from .identity import get_current, resolve_agent_token, set_current

# The action tools that change collaboration state. They stay LOCKED until the agent
# passes the pre-flight readiness check (agents.ready = 1) — read-only tools (rules,
# check_messages, wait_for_message, get_contract, get_todos, list_files, get_file,
# list_activity, readiness_check, submit_readiness) are never gated, so the agent can read
# the briefing + answer the check first.
#
# The todo writes are gated for exactly the reason propose_contract is: proposing or
# accepting a todo IS an agreement (it binds seats to a deliverable and fixes who must
# sign its contract), and an agreement made by an agent that never proved it read the
# briefing is worthless. get_todos stays open — reading the work is not agreeing to it.
# upload_file is gated too: it WRITES task data (a file others act on), so it waits on
# readiness like every other write. list_files/get_file are reads and stay open.
# share_activity is gated for the same reason as upload_file: it WRITES task data (a note
# the peer's human reads on the dashboard), so it waits on readiness; list_activity reads.
#
# ENGAGEMENT MODE splits the same way, and the split is the same question every time: is
# this an AGREEMENT or a claim (gated), or is it reading what somebody else agreed (open)?
#   * The deliverable writes — propose_deliverables, add_deliverable, revise_deliverable,
#     withdraw_deliverable, accept_deliverables, push_back — are the strongest agreement
#     on the broker: the list is what the client commissioned and it gates every todo and
#     contract under it. An owner who never proved he read the briefing setting the scope,
#     or a builder accepting it, is exactly the worthless agreement the gate exists for.
#     `push_back` is gated too although it REFUSES rather than agrees: it holds the whole
#     list, which is an action on everyone else's work.
#   * set_guidelines WRITES configuration that lands in other agents' context and clears
#     their assessment. Host-only already, and host-only is not the same as pre-flighted.
#   * submit_spec is a CLAIM about what you built, made to the person paying — the same
#     authority as report_status, which is gated, and it is read by the agent that
#     verifies. start_verification and record_verification write the RECORD of what was
#     checked; a verdict is the most consequential write in an engagement.
#   * get_deliverables stays OPEN — reading the scope is not agreeing to it, and a builder
#     has to be able to read the list before deciding whether to accept it, exactly as
#     get_todos is open.
#   * guidelines_check and submit_guidelines stay OPEN because they ARE a gate, like
#     readiness_check/submit_readiness. Gating a gate is how a session deadlocks, and
#     passing this one early buys nothing: every write above still waits on pre-flight.
ACTION_TOOLS = frozenset({
    "send_message",
    "propose_contract",
    "lock_contract",
    "report_status",
    "propose_todo",
    "accept_todo",
    "decline_todo",
    "repropose_todo",
    "drop_todo",
    "upload_file",
    "share_activity",
    # engagement mode: the scope, the standards, the claims and the verdicts
    "propose_deliverables",
    "add_deliverable",
    "revise_deliverable",
    "withdraw_deliverable",
    "accept_deliverables",
    "push_back",
    "set_guidelines",
    "submit_spec",
    "start_verification",
    "record_verification",
})

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
    async def on_call_tool(self, context, call_next):
        """Readiness gate: in remote mode, an authenticated agent cannot call an ACTION
        tool until it has passed the pre-flight check (agents.ready). Auth itself runs
        in on_request; here we only gate the *action* tools by readiness."""
        cfg = get_config()
        if cfg.is_remote:
            ident = get_current()  # stamped by on_request earlier in this request
            tool = context.message.name
            if ident is not None and tool in ACTION_TOOLS:
                conn = connect()
                try:
                    row = conn.execute(
                        "SELECT ready FROM agents WHERE id = ?", (ident.agent_id,)
                    ).fetchone()
                finally:
                    conn.close()
                if not (row and row["ready"]):
                    raise ToolError(
                        f"locked: read rules(), then pass readiness_check() and "
                        f"submit_readiness(...) before calling {tool}."
                    )
        return await call_next(context)

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
