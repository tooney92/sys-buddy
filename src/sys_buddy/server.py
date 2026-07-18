"""Server assembly — one process, four surfaces (SPEC §2).

Everything the three surfaces need is wired here and nowhere else:

    /mcp        MCP tools           (register_tools + AuthMiddleware)
    /pair       pairing REST        (register_pairing_routes)
    /ui + /api  dashboard + JSON    (register_api_routes)

``build_server`` is kept separate from ``run_server`` so tests can construct the
app in-process without binding a port. Boot always runs ``init_db`` first, so a
fresh machine "just works" without a separate ``sys-buddy init`` — the schema
self-heals (idempotent), closing the gap the predecessor covered by creating
tables on every connection.
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import api, pairing
from .config import Config, set_config
from .db import init_db
from .middleware import AuthMiddleware
from .tools import register_tools


def build_server(cfg: Config) -> FastMCP:
    set_config(cfg)
    init_db(cfg.db_path)  # idempotent; makes a fresh db just work on boot

    mcp = FastMCP("sys-buddy")
    mcp.add_middleware(AuthMiddleware())  # remote: token→identity; local: no-op
    register_tools(mcp, cfg)              # /mcp — messaging + contract/status tools
    pairing.register_pairing_routes(mcp, cfg)  # /pair — invite redemption
    api.register_api_routes(mcp, cfg)          # /ui + /api/* — dashboard (read-only)
    return mcp


def run_server(cfg: Config) -> None:
    from starlette.middleware import Middleware

    from .http_middleware import (
        DASHBOARD_CSP,
        REQUEST_MAX_BYTES,
        BodyLimitMiddleware,
        SecurityHeadersMiddleware,
    )

    mcp = build_server(cfg)
    mode = "remote · auth enforced" if cfg.is_remote else "local · loopback, no auth"
    print(f"sys-buddy [{mode}]")
    print(f"  db:        {cfg.db_path}")
    print(f"  mcp:       http://{cfg.host}:{cfg.port}/mcp")
    print(f"  dashboard: {cfg.base_url}/ui")

    secure = (cfg.public_url or "").lower().startswith("https://")
    http_middleware = [
        Middleware(BodyLimitMiddleware, max_bytes=REQUEST_MAX_BYTES),
        Middleware(SecurityHeadersMiddleware, hsts=secure, csp=DASHBOARD_CSP),
    ]
    mcp.run(transport="http", host=cfg.host, port=cfg.port, middleware=http_middleware)
