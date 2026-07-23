"""Specs for the todo TOOL surface.

Two registrations, one codebase (tools.py): the remote token-stamped tools and the
local ``task``/``agent`` ones. A tool that exists on only one of them is a silent
capability gap for half the users, so the registration test runs over both modes —
and the ops themselves are exercised end to end, because the tool bodies are
one-liners over exactly these functions.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from sys_buddy import onboarding, service, state, todos, tools
from sys_buddy.config import Config
from sys_buddy.middleware import ACTION_TOOLS
from sys_buddy.rules import RULES_OF_ENGAGEMENT
from sys_buddy.server import build_server
from tests.conftest import seed_agent, seed_task

TODO_TOOLS = {
    "get_todos", "propose_todo", "accept_todo", "decline_todo", "repropose_todo",
    "drop_todo",
}
# The tools that gained the selector. `get_contract` is here too: a party has to be
# able to READ the shape it is being asked to sign, per deliverable.
SELECTOR_TOOLS = {
    "propose_contract", "lock_contract", "get_contract", "reopen_negotiations",
    "report_status",
}


def _agents(conn, task="signin", roles=("backend", "frontend", "mobile")):
    seed_task(conn, task, roles=roles)
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in roles
    }


def _spec() -> dict:
    return {
        "version": 1,
        "endpoints": [{"method": "POST", "path": "/api/items"}],
        "staging_url": "https://api-staging.example.com",
    }


def _schemas(mode, tmp_path) -> dict:
    mcp = FastMCP("t")
    cfg = Config(mode=mode, db_path=tmp_path / f"{mode}.db")
    tools.register_tools(mcp, cfg)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# --- registration: both surfaces, or it doesn't count ----------------------
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_todo_tools_are_registered_on_both_surfaces(tmp_path, mode):
    assert TODO_TOOLS <= set(_schemas(mode, tmp_path))


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_the_todo_selector_is_optional_everywhere_it_appears(tmp_path, mode):
    """Optional, and defaulted to "not given" — which is why every pre-todo caller
    keeps working without knowing todos exist."""
    schemas = _schemas(mode, tmp_path)
    for name in SELECTOR_TOOLS:
        props = schemas[name].parameters["properties"]
        assert "todo" in props, name
        assert props["todo"]["default"] == 0, name
        assert "todo" not in schemas[name].parameters.get("required", []), name


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_the_todo_tools_are_reachable_through_a_built_server(tmp_path, mode):
    mcp = build_server(Config(mode=mode, db_path=tmp_path / "s.db"))
    assert TODO_TOOLS <= {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_every_todo_tool_documents_the_protocol(tmp_path, mode):
    """Docstrings are agent-facing prompt surface here, not developer comments."""
    schemas = _schemas(mode, tmp_path)
    for name in TODO_TOOLS:
        assert len((schemas[name].description or "").strip()) > 120, name


def test_the_charter_teaches_the_todo_protocol():
    """rules() is where an agent learns the protocol; a tool it is never told about
    is a tool it never calls."""
    r = RULES_OF_ENGAGEMENT.lower()
    for fragment in ("get_todos()", "propose_todo", "accept_todo", "todo=n"):
        assert fragment in r
    # The two rules an agent must not guess at: whose signature counts, and that the
    # todo id is required.
    assert "not the whole cast" in r
    assert "required" in r


@pytest.mark.parametrize("role", ["backend", "frontend"])
def test_the_contract_briefing_mentions_todos_conditionally(role):
    """Named, but framed as "only if this task uses them" — most tasks have none."""
    text = onboarding.role_prompt(role, "signin")
    assert "propose_todo" in text and "get_todos()" in text
    assert "only if this task uses them" in text
    # A debug task never carries todos, so its briefing must not mention them.
    assert "propose_todo" not in onboarding.role_prompt(role, "signin", mode="debug")


def test_the_todo_writes_sit_behind_the_pre_flight_gate():
    """Proposing or accepting a todo IS an agreement — same authority as a contract,
    so the same readiness gate. Reading the work is not agreeing to it."""
    assert {"propose_todo", "accept_todo", "decline_todo", "repropose_todo", "drop_todo"} <= (
        ACTION_TOOLS
    )
    assert "get_todos" not in ACTION_TOOLS


# --- the ops, end to end ----------------------------------------------------
def test_the_full_todo_flow_through_the_ops(conn):
    ag = _agents(conn)

    t = tools._op_propose_todo(
        ag["backend"], "api123", "POST /items and its 400 shape", ["backend", "mobile"]
    )
    assert t["status"] == todos.PENDING
    assert [d["id"] for d in tools._op_get_todos("signin")] == [t["id"]]

    assert tools._op_accept_todo(ag["mobile"], t["id"])["status"] == todos.ACCEPTED

    r = tools._op_propose(ag["backend"], _spec(), t["id"])
    assert r["signatories"] == ["backend", "mobile"]
    assert tools._op_get_contract("signin", t["id"])["awaiting"] == ["backend", "mobile"]

    tools._op_lock(ag["backend"], r["version"], t["id"])
    assert tools._op_lock(ag["mobile"], r["version"], t["id"])["locked"] is True

    tools._op_report_status(ag["backend"], "ready", "live on staging", t["id"])
    tools._op_report_status(ag["mobile"], "checked", "works", t["id"])
    done = tools._op_report_status(ag["mobile"], "verified", "done", t["id"])
    assert done["todo_state"] == state.VERIFIED and done["rollup"]["complete"] is True


def test_decline_then_repropose_through_the_ops(conn):
    ag = _agents(conn)
    t = tools._op_propose_todo(ag["backend"], "api123", "too broad", ["backend", "mobile"])
    assert tools._op_decline_todo(ag["mobile"], t["id"], "split it in two")["declined_by"] == [
        "mobile"
    ]
    again = tools._op_repropose_todo(ag["backend"], t["id"], scope="just the POST")
    assert again["version"] == 2 and again["accepted_by"] == ["backend"]
    assert tools._op_accept_todo(ag["mobile"], t["id"])["status"] == todos.ACCEPTED


def test_drop_through_the_ops_is_mutual(conn):
    ag = _agents(conn)
    t = tools._op_propose_todo(ag["backend"], "api123", "scope", ["backend", "mobile"])
    tools._op_accept_todo(ag["mobile"], t["id"])
    assert tools._op_drop_todo(ag["backend"], t["id"], "not needed")["status"] != todos.DROPPED
    assert tools._op_drop_todo(ag["mobile"], t["id"], "agreed")["status"] == todos.DROPPED


def test_omitting_the_selector_keeps_the_pre_todo_behaviour(conn):
    """A no-todo task drives the whole flow through the ops with no selector at all."""
    ag = _agents(conn, roles=("backend", "frontend"))
    r = tools._op_propose(ag["backend"], _spec())
    assert r == {"version": 1, "state": state.CONTRACT_PROPOSED}
    tools._op_lock(ag["backend"], 1)
    assert tools._op_lock(ag["frontend"], 1)["locked"] is True
    assert tools._op_get_contract("signin")["locked"] is True
    tools._op_report_status(ag["backend"], "ready", "live")
    tools._op_report_status(ag["frontend"], "checked", "works")
    assert tools._op_report_status(ag["frontend"], "verified", "done") == {
        "status": state.STATUS_VERIFIED, "state": state.VERIFIED,
    }


def test_the_ops_surface_the_brokers_rejections_verbatim(conn):
    """The tool layer adds no rules of its own — it resolves an identity and asks."""
    ag = _agents(conn)
    t = tools._op_propose_todo(ag["backend"], "api123", "scope", ["backend", "mobile"])
    tools._op_accept_todo(ag["mobile"], t["id"])
    with pytest.raises(ValueError, match="not a party"):
        tools._op_accept_todo(ag["frontend"], t["id"])
    with pytest.raises(ValueError, match="runs on todos"):
        tools._op_propose(ag["backend"], _spec())
