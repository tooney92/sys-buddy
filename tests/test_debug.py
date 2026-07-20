"""Specs for the 'debug' task mode (SPEC: debug tasks collaborate then resolve).

Debug tasks skip the contract workflow entirely: they need no 'backend' role,
and the only terminal transition is open→resolved (any role). Contract statuses
(deployed/test_passed/test_failed/verified) do not apply to a debug task, and
'resolved' does not apply to a contract task. Every rejection is a ValueError the
broker enforces in code, not prompt.
"""

from __future__ import annotations

import pytest

from sys_buddy import admin, service, state


def _debug_agents(conn, task="bug", roles=("dev", "reviewer")):
    """Create a debug task and return {role: Identity} for each declared role."""
    from tests.conftest import seed_agent

    admin.create_task(task, title="Login 500", roles=list(roles), mode="debug")
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in roles
    }


def _task_row(conn, task):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task,)).fetchone()


# --- creation ---------------------------------------------------------------
def test_debug_task_needs_no_backend_role(conn):
    task = admin.create_task(
        "bug", title="Login 500", roles=["dev", "reviewer"], mode="debug"
    )
    assert task is not None
    row = _task_row(conn, "bug")
    assert row["mode"] == "debug"


def test_contract_task_requires_two_roles_not_backend(conn):
    # Model B: no 'backend' requirement — but a contract still needs >= 2 roles.
    with pytest.raises(ValueError, match="at least two roles"):
        admin.create_task("x", title="x", roles=["web"])
    # a 2-role contract with NO backend role is valid now
    t = admin.create_task("ok", title="ok", roles=["web", "api"])
    assert t["roles"] == ["web", "api"]


# --- resolve is the debug terminal ------------------------------------------
def test_debug_resolve_moves_to_terminal(conn):
    ag = _debug_agents(conn)
    r = state.report_status(conn, ag["dev"], state.STATUS_RESOLVED, "fixed the cookie")
    assert r["state"] == state.RESOLVED
    assert _task_row(conn, "bug")["state"] == state.RESOLVED
    # resolved is terminal — any further report is refused.
    with pytest.raises(ValueError):
        state.report_status(conn, ag["reviewer"], state.STATUS_RESOLVED, "again")


# --- contract statuses don't apply to debug ---------------------------------
def test_debug_rejects_contract_statuses(conn):
    ag = _debug_agents(conn)
    with pytest.raises(ValueError):
        state.report_status(conn, ag["dev"], state.STATUS_DEPLOYED, "deploy?")
    with pytest.raises(ValueError):
        state.report_status(conn, ag["dev"], state.STATUS_VERIFIED, "verify?")


# --- 'resolved' doesn't apply to a contract task ----------------------------
def test_contract_rejects_resolved(conn):
    from tests.conftest import seed_agent, seed_task

    seed_task(conn, "signin", roles=("backend", "frontend"))
    dev = service.Identity(
        agent_id=seed_agent(conn, "signin", "backend", "backend-agent", "sbk_backend"),
        task_id="signin",
        name="backend-agent",
        role="backend",
    )
    with pytest.raises(ValueError):
        state.report_status(conn, dev, state.STATUS_RESOLVED, "resolved?")
