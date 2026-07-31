"""Integration spec for the server assembly (server.py).

Builds the app in-process (no port bound) and checks that all four surfaces are
wired: the MCP tool set, the pairing route, and the dashboard/API routes. A live
HTTP dogfood is exercised separately; this keeps a fast, non-flaky guard in CI.
"""

from __future__ import annotations

import asyncio

import pytest

from sys_buddy.config import Config
from sys_buddy.server import build_server

EXPECTED_TOOLS = {
    "send_message", "check_messages", "wait_for_message", "ack_messages",
    "channel_history", "propose_contract", "lock_contract", "get_contract",
    "report_status", "notify_human",
}
EXPECTED_ROUTES = {
    "/pair",
    # Read-only, and gated on the same invite code /pair is: it shows the joiner the
    # task, their seat and the cast BEFORE they burn a single-use invite on it.
    "/pair/preview",
    "/api/tasks", "/api/task/{id}", "/api/task/{id}/events", "/ui",
}


def _routes(mcp):
    return {getattr(r, "path", None) for r in getattr(mcp, "_additional_http_routes", [])}


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_all_tools_registered(tmp_path, mode):
    mcp = build_server(Config(mode=mode, db_path=tmp_path / "s.db"))
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert EXPECTED_TOOLS <= names


def test_ship_is_shorthand_only_and_never_a_broker_tool(tmp_path):
    """`ship #N` is vocabulary a human types to THEIR OWN agent — it maps to
    propose_contract then lock_contract and lives in the briefing, not the tool set.

    A broker tool would be a second way to do the same two things, and it would have to
    decide on the human's behalf that proposing implies signing. The broker enforces; the
    agent composes.
    """
    mcp = build_server(Config(mode="local", db_path=tmp_path / "s.db"))
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert not [n for n in names if "ship" in n]
    assert {"propose_contract", "lock_contract"} <= names


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_all_http_routes_registered(tmp_path, mode):
    mcp = build_server(Config(mode=mode, db_path=tmp_path / "s.db"))
    assert EXPECTED_ROUTES <= _routes(mcp)


def test_build_server_initialises_schema(tmp_path):
    """Booting on a fresh path creates the db (no separate `init` needed)."""
    dbfile = tmp_path / "fresh.db"
    assert not dbfile.exists()
    build_server(Config(mode="local", db_path=dbfile))
    assert dbfile.exists()
