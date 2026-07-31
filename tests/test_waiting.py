"""The soft `waiting` nudge (v2): a give-up rung BELOW `stuck`.

`report_status("waiting", ...)` pings the humans without changing the task's state,
touching strikes, or ending anything — for "the peer's gone quiet, a human's attention
would help." Distinct from `stuck`, which is terminal and freezes the collaboration.
"""

from __future__ import annotations

import pytest

from sys_buddy import service, slack, state
from tests.conftest import seed_agent, seed_task
from tests.test_state import _agents, _to_backend_live


def _debug_agents(conn, task="debugtask"):
    """A debug-mode task with one seat."""
    seed_task(conn, task, roles=("backend",))
    conn.execute("UPDATE tasks SET mode = 'debug' WHERE id = ?", (task,))
    conn.commit()
    return service.Identity(
        agent_id=seed_agent(conn, task, "backend", "backend-agent", "sbk_dbg"),
        task_id=task, name="backend-agent", role="backend",
    )


def test_waiting_does_not_change_state_or_end_the_task(conn):
    """The whole point: a nudge, not a transition. State is untouched and the task stays
    live, so the flow can carry on and the agent can nudge again later."""
    ag = _agents(conn)
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["backend"], state.STATUS_WAITING, "mobile's gone quiet")
    assert r["status"] == state.STATUS_WAITING
    # State is whatever it was — a nudge never transitions. The task's state is a rollup
    # of its todos, and the one deliverable here is backend_live.
    assert r["state"] == state.BACKEND_LIVE
    # Not terminal: nudge again, and the normal (todo-scoped) flow still works afterwards.
    state.report_status(conn, ag["backend"], state.STATUS_WAITING, "still waiting")
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "green", 1)
    done = state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "e2e green", 1)
    assert done["state"] == state.VERIFIED


def test_waiting_works_on_a_debug_task(conn):
    """Mode-agnostic — a debug task has a silent-peer problem too, and 'resolved' is its
    only other word, so 'waiting' must be allowed here (it's handled before the mode gate)."""
    ag = _debug_agents(conn)
    r = state.report_status(conn, ag, state.STATUS_WAITING, "peer hasn't replied")
    assert r["status"] == state.STATUS_WAITING


def test_waiting_rejects_a_todo_number(conn):
    """'waiting' is a task-level nudge — a todo `#N` is a category error; use 'stuck' with
    a todo to flag one deliverable."""
    ag = _agents(conn)
    with pytest.raises(ValueError, match="task-level nudge"):
        state.report_status(conn, ag["backend"], state.STATUS_WAITING, "x", number=1)


def test_waiting_rejected_on_a_terminal_task(conn):
    """Nothing left to wait for once the task is terminal."""
    ag = _agents(conn)
    state.report_status(conn, ag["backend"], state.STATUS_STUCK, "giving up")  # → terminal
    with pytest.raises(ValueError):
        state.report_status(conn, ag["backend"], state.STATUS_WAITING, "too late")


def test_waiting_pings_slack_and_posts_to_the_thread(conn, monkeypatch):
    """The nudge is only useful if it reaches a human — it pings Slack — and it lands on
    the thread + event log so the dashboard shows it."""
    sent = []
    # The registry only calls a channel it considers configured, so arm one first —
    # that gate is the behaviour, not an inconvenience: an unconfigured channel must
    # never be reported as having been notified.
    from sys_buddy.config import Config, get_config, set_config

    set_config(
        Config(mode="local", db_path=get_config().db_path,
               slack_webhook="https://hooks.example/x")
    )
    monkeypatch.setattr(slack, "notify", lambda text: sent.append(text) or "")
    ag = _agents(conn)
    state.report_status(conn, ag["backend"], state.STATUS_WAITING, "mobile is silent")

    assert any("WAITING" in s for s in sent)  # Slack nudged
    msgs = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE task_id='signin' AND type='waiting'"
    ).fetchone()["n"]
    assert msgs == 1  # thread bubble
    evs = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE task_id='signin' AND kind='waiting'"
    ).fetchone()["n"]
    assert evs == 1  # event log
