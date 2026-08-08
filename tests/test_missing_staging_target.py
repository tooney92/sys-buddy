"""A LOCKED contract with nowhere to point is a known-bad state, and the panel says so.

``state.resolve_staging_url`` returns ``None`` whenever neither the todo's override nor
the task's URL is set — which is the ordinary outcome of creating a task and never running
``sys-buddy task staging-url``, not a corner case. On a locked contract that is a blocked
consumer: the shape is agreed, everyone has signed, and there is no target to test against.

The dashboard already warned when the target had MOVED since the lock ("the target moved,
the contract did not"). It said nothing at all when there was no target, so the host found
out two status reports later from a peer who was stuck. These specs pin the data the
warning keys on — the pair ``locked: True`` + ``staging_url: None``, which is what the
panel reads — and that the page renders the fix rather than just the complaint.
"""

from __future__ import annotations

from pathlib import Path

from sys_buddy import api, state

from tests.test_state import _agents, _lock_all, _valid_spec

UI = Path(__file__).resolve().parents[1] / "src" / "sys_buddy" / "ui.html"


def _locked(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    return ag


def _todo(conn):
    return conn.execute("SELECT id FROM todos WHERE task_id = 'signin'").fetchone()["id"]


def _version(block):
    return block["data"][block["default"]]


# --------------------------------------------------------------------------- #
# the state itself
# --------------------------------------------------------------------------- #
def test_a_locked_todo_can_resolve_to_no_target_at_all(conn):
    """The premise. Nothing in the todo flow forces a target to exist before a lock, so
    this is reachable by doing everything right."""
    _locked(conn)
    assert state.resolve_staging_url(conn, "signin", _todo(conn)) is None


def test_the_panel_is_handed_a_locked_contract_with_a_null_target(conn):
    """The exact pair the dashboard keys on: a version that IS locked, and a target that
    is None. `staging_url` is null here because there is none — NOT because it is being
    withheld, which is the other reason this field can be null (see `_contract_for`)."""
    _locked(conn)
    block = api._contract_for(conn, "signin", todo_id=_todo(conn), is_host=True)
    version = _version(block)
    assert version["locked"] is True
    assert version["staging_url"] is None
    # ...and nothing was signed against one either, so the "target moved" warning cannot
    # fire and cover for the missing one.
    assert version["staging_url_at_lock"] is None


def test_the_todo_row_agrees_that_it_resolves_to_nothing(conn):
    _locked(conn)
    (todo,) = api._todos_for(conn, "signin", is_host=True)
    assert todo["staging_url"] is None
    assert todo["staging_url_effective"] is None
    assert _version(todo["contract"])["locked"] is True


def test_setting_the_target_clears_the_condition_with_no_renegotiation(conn):
    """The fix the panel prints has to actually work: the target is configuration, so
    setting it moves no version, no signature and no lock."""
    _locked(conn)
    before = api._contract_for(conn, "signin", todo_id=_todo(conn), is_host=True)
    conn.execute("UPDATE tasks SET staging_url = ? WHERE id = 'signin'", ("https://x.example",))
    conn.commit()
    after = api._contract_for(conn, "signin", todo_id=_todo(conn), is_host=True)
    assert _version(after)["staging_url"] == "https://x.example"
    assert after["versions"] == before["versions"]
    assert _version(after)["signed"] == _version(before)["signed"]


def test_a_todo_override_satisfies_it_for_that_deliverable_only(conn):
    _locked(conn)
    todo_id = _todo(conn)
    conn.execute("UPDATE todos SET staging_url = ? WHERE id = ?", ("https://one.example", todo_id))
    conn.commit()
    assert state.resolve_staging_url(conn, "signin", todo_id) == "https://one.example"
    # The TASK still has none, which is why the warning is computed per contract panel
    # rather than once per task.
    assert state.resolve_staging_url(conn, "signin", None) is None


def test_a_draft_is_not_flagged(conn):
    """Only a LOCKED contract is blocked by this. A draft has nothing to test yet and the
    host may perfectly well set the target before anyone signs — warning there would be
    noise on the ordinary path."""
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    block = api._contract_for(conn, "signin", todo_id=_todo(conn), is_host=True)
    assert _version(block)["locked"] is False


# --------------------------------------------------------------------------- #
# ...and the page says so, with the command that fixes it
# --------------------------------------------------------------------------- #
def _ui() -> str:
    return UI.read_text(encoding="utf-8")


def test_the_panel_renders_the_warning_where_the_target_line_would_be():
    ui = _ui()
    assert "function noTargetHTML(d)" in ui
    # Fired on a live LOCKED version with no target — the same branch the "Connect to"
    # line comes out of, so the reader finds it exactly where they looked for the URL.
    assert "(!dead && data.locked ? noTargetHTML(d) : '')" in ui


def test_the_warning_prints_the_command_that_fixes_it():
    """It names `task staging-url` — the command whose invisibility caused this — and the
    per-todo `--todo N`, because a deliverable's target is set per deliverable."""
    ui = _ui()
    assert "'sys-buddy task staging-url '+taskId+' https://…'+scope" in ui
    assert "' --todo '+d.todoNo" in ui
    # The todo panel has to hand it the two things that line needs.
    assert "task:d.id, todoNo:todoNo(t)" in ui


def test_a_buddy_is_told_the_fact_but_not_handed_a_host_command():
    """Same rule as `dropTodoHTML`: a buddy cannot run a host CLI, so showing them the
    line would be a lie about who can unblock this."""
    ui = _ui()
    assert "var host = !!(state.viewer && state.viewer.mode!=='buddy');" in ui
    assert "Your host sets it" in ui
