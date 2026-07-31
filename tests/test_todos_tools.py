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
# Every tool that takes the selector. `get_contract` is here too: a party has to be able
# to READ the shape it is being asked to sign, per deliverable.
SELECTOR_TOOLS = {
    "propose_contract", "lock_contract", "decline_contract", "get_contract",
    "reopen_negotiations", "report_status",
}
# The ones that act on a CONTRACT, which is an agreement about ONE todo — so there is no
# call they could serve without the deliverable, and the signature says so.
REQUIRED_SELECTOR_TOOLS = {
    "propose_contract", "lock_contract", "decline_contract", "reopen_negotiations",
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
def test_every_contract_tool_declares_the_selector_required(tmp_path, mode):
    """A contract is an agreement about ONE todo, so an agent must not be ABLE to omit
    the deliverable — the schema stops it rather than a runtime error explaining it."""
    schemas = _schemas(mode, tmp_path)
    for name in REQUIRED_SELECTOR_TOOLS:
        props = schemas[name].parameters["properties"]
        assert "todo" in props, name
        assert "default" not in props["todo"], name
        assert "todo" in schemas[name].parameters.get("required", []), name


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_the_two_selector_exceptions_stay_optional(tmp_path, mode):
    """`get_contract` because READING is how an agent recovers a number it lost, and
    `report_status` because bare `stuck` escalates the WHOLE task on purpose — a required
    parameter would make a task-wide problem impossible to report."""
    schemas = _schemas(mode, tmp_path)
    for name in SELECTOR_TOOLS - REQUIRED_SELECTOR_TOOLS:
        props = schemas[name].parameters["properties"]
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
    assert [d["number"] for d in tools._op_get_todos("signin")] == [t["number"]]

    assert tools._op_accept_todo(ag["mobile"], t["number"])["status"] == todos.ACCEPTED

    r = tools._op_propose(ag["backend"], _spec(), t["number"])
    assert r["signatories"] == ["backend", "mobile"]
    assert tools._op_get_contract("signin", t["number"])["awaiting"] == ["backend", "mobile"]

    tools._op_lock(ag["backend"], r["version"], t["number"])
    assert tools._op_lock(ag["mobile"], r["version"], t["number"])["locked"] is True

    tools._op_report_status(ag["backend"], "ready", "live on staging", t["number"])
    tools._op_report_status(ag["mobile"], "checked", "works", t["number"])
    done = tools._op_report_status(ag["mobile"], "verified", "done", t["number"])
    assert done["todo_state"] == state.VERIFIED and done["rollup"]["complete"] is True


def test_decline_then_repropose_through_the_ops(conn):
    ag = _agents(conn)
    t = tools._op_propose_todo(ag["backend"], "api123", "too broad", ["backend", "mobile"])
    assert tools._op_decline_todo(ag["mobile"], t["number"], "split it in two")["declined_by"] == [
        "mobile"
    ]
    again = tools._op_repropose_todo(ag["backend"], t["number"], scope="just the POST")
    assert again["version"] == 2 and again["accepted_by"] == ["backend"]
    assert tools._op_accept_todo(ag["mobile"], t["number"])["status"] == todos.ACCEPTED


def test_drop_through_the_ops_is_mutual(conn):
    ag = _agents(conn)
    t = tools._op_propose_todo(ag["backend"], "api123", "scope", ["backend", "mobile"])
    tools._op_accept_todo(ag["mobile"], t["number"])
    assert tools._op_drop_todo(ag["backend"], t["number"], "not needed")["status"] != todos.DROPPED
    assert tools._op_drop_todo(ag["mobile"], t["number"], "agreed")["status"] == todos.DROPPED


def test_the_whole_flow_runs_through_the_ops_on_one_deliverable(conn):
    """End to end at the tool layer: agree WHAT, agree HOW, then march it — every step
    naming the same deliverable, because that is the only scope a contract has."""
    ag = _agents(conn, roles=("backend", "frontend"))
    t = tools._op_propose_todo(ag["backend"], "api123", "scope", ["backend", "frontend"])
    num = t["number"]
    tools._op_accept_todo(ag["frontend"], num)

    r = tools._op_propose(ag["backend"], _spec(), num)
    assert r["version"] == 1 and r["todo"] == num
    tools._op_lock(ag["backend"], 1, num)
    assert tools._op_lock(ag["frontend"], 1, num)["locked"] is True
    assert tools._op_get_contract("signin", num)["locked"] is True

    tools._op_report_status(ag["backend"], "ready", "live", num)
    tools._op_report_status(ag["frontend"], "checked", "works", num)
    done = tools._op_report_status(ag["frontend"], "verified", "done", num)
    assert done["status"] == state.STATUS_VERIFIED
    assert done["todo_state"] == state.VERIFIED
    # The last live todo verifying is what concludes the TASK — no agent set that.
    assert done["state"] == state.VERIFIED


def test_the_ops_surface_the_brokers_rejections_verbatim(conn):
    """The tool layer adds no rules of its own — it resolves an identity and asks."""
    ag = _agents(conn)
    t = tools._op_propose_todo(ag["backend"], "api123", "scope", ["backend", "mobile"])
    tools._op_accept_todo(ag["mobile"], t["number"])
    with pytest.raises(ValueError, match="not a party"):
        tools._op_accept_todo(ag["frontend"], t["number"])
    with pytest.raises(ValueError, match="must name the deliverable it shapes"):
        tools._op_propose(ag["backend"], _spec())
