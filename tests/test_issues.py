"""Specs for ISSUES on a debug task — a todo with the contract half removed.

A debug task used to be ONE problem you fixed and closed with
``report_status("resolved")``. It now carries several ISSUES, and an issue is to a debug
task what a todo is to a contract task, minus the contract: there is no HOW to agree on a
bug, only whether it is real and whether it is fixed.

    issue "refresh 500s"   anyone raises one; raising IS the raiser's own accept
      → pending
    yes #2                 every OTHER named party accepts
      → accepted           ← the work happens here
    fixed #2               each party says independently that it is fixed
      → still accepted while any party has not
    fixed #2               the last party lands it
      → resolved

Four claims carry the feature, and each has its own section below:

* **the same table.** Numbering, party lists, accept/decline, drop, stuck and the rollup
  are the ones ``todos.py`` already had and already tested. ``propose_issue`` is
  ``propose_todo``'s own implementation under the other name.
* **`fixed #N` needs EVERY named party** — the all-must-agree rule ``lock_contract``
  applies to signatures, over the same "all". Partial is a normal state, not an error.
* **the task auto-resolves by ROLLUP, and does not latch.** Resolved-by-counting goes
  backwards when a new issue appears; resolved because a person said so is terminal.
* **backwards compatible, opt-in per task.** A debug task with NO issues behaves exactly
  as it did; the bare form is refused only once it has some.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sys_buddy import service, state, todos, tools
from tests.conftest import seed_agent, seed_task

UI_HTML = (Path(tools.__file__).parent / "ui.html").read_text()


def _fn(name: str) -> str:
    """The source of one top-level function in ui.html — same helper (and same reason) as
    ``tests/test_engagement_ui.py``: there is no build step, so the file IS the artefact."""
    m = re.search(r"^function " + re.escape(name) + r"\(", UI_HTML, re.M)
    assert m, f"{name}() is gone from ui.html"
    end = UI_HTML.index("\n}", m.start())
    return UI_HTML[m.start():end]


def _agents(conn, task="bug", roles=("dev", "reviewer"), mode="debug"):
    """Seed a task in `mode` and return {role: Identity} for each declared role."""
    seed_task(conn, task, roles=roles)
    conn.execute("UPDATE tasks SET mode = ? WHERE id = ?", (mode, task))
    conn.commit()
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in roles
    }


def _task_state(conn, task="bug") -> str:
    return conn.execute("SELECT state FROM tasks WHERE id = ?", (task,)).fetchone()["state"]


def _raise(conn, ag, by="dev", title="refresh 500s", parties=("dev", "reviewer")) -> int:
    """Raise an issue and return its human ``#N``."""
    made = todos.propose_issue(
        conn, ag[by], title, f"{title} — repro: hit it twice; fixed when it does not",
        list(parties),
    )
    return made["number"]


def _accepted(conn, ag, by="dev", parties=("dev", "reviewer"), title="refresh 500s") -> int:
    n = _raise(conn, ag, by=by, title=title, parties=parties)
    for role in parties:
        if role != by:
            todos.accept_todo(conn, ag[role], n)
    return n


def _fixed(conn, ag, role, n, detail="pushed the fix"):
    return state.report_status(conn, ag[role], state.STATUS_FIXED, detail, n)


def _status(conn, n, task="bug") -> str:
    return todos.status_of(conn, todos.get_row(conn, task, n))


# --------------------------------------------------------------------------- #
# raising: the raiser's own accept, and the numbering it inherits
# --------------------------------------------------------------------------- #
def test_raising_an_issue_is_the_raisers_own_acceptance(conn):
    """Same rule as a todo, and it is why nobody is asked twice: the raiser has already
    said this is a problem by raising it."""
    ag = _agents(conn)
    n = _raise(conn, ag)
    row = todos.to_dict(conn, todos.get_row(conn, "bug", n))
    assert row["accepted_by"] == ["dev"]
    assert row["awaiting"] == ["reviewer"]
    assert row["status"] == todos.PENDING


def test_every_other_party_accepts_before_the_work_happens(conn):
    ag = _agents(conn)
    n = _raise(conn, ag)
    todos.accept_todo(conn, ag["reviewer"], n)
    assert _status(conn, n) == todos.ACCEPTED


def test_an_issue_can_be_declined_because_that_is_not_a_bug(conn):
    """Declining stays available — it is free with the reuse, and it is the only way to
    say "that is not a bug" with a reason attached."""
    ag = _agents(conn)
    n = _raise(conn, ag)
    todos.decline_todo(conn, ag["reviewer"], n, "works as designed — that 500 is a redirect")
    row = todos.to_dict(conn, todos.get_row(conn, "bug", n))
    assert row["declined_by"] == ["reviewer"]
    assert row["status"] == todos.PENDING
    assert "redirect" in row["decline_reasons"]["reviewer"]


def test_issues_are_numbered_per_task_from_one_like_todos(conn):
    """Nothing about numbering is re-implemented: it is the same column, the same MAX+1,
    the same never-reused handle a human types."""
    ag = _agents(conn)
    assert _raise(conn, ag, title="first") == 1
    assert _raise(conn, ag, title="second") == 2


def test_an_issue_is_a_row_in_the_todos_table(conn):
    """The decision that keeps the change small — stated as a test so a later `issues`
    table cannot be added without this failing."""
    ag = _agents(conn)
    _raise(conn, ag)
    assert conn.execute("SELECT COUNT(*) AS n FROM todos").fetchone()["n"] == 1
    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "issues" not in tables


# --------------------------------------------------------------------------- #
# `fixed #N` — every named party, and partial is normal
# --------------------------------------------------------------------------- #
def test_one_party_reporting_fixed_does_not_resolve_the_issue(conn):
    """The whole point: an issue closed on one agent's word is what a debug session had
    before, and nobody was positioned to say the fix worked."""
    ag = _agents(conn)
    n = _accepted(conn, ag)
    out = _fixed(conn, ag, "dev", n)
    assert out["resolved"] is False
    assert out["fixed_by"] == ["dev"]
    assert out["awaiting_fix"] == ["reviewer"]
    assert _status(conn, n) == todos.ACCEPTED
    assert _task_state(conn) == state.OPEN


def test_the_last_party_reporting_fixed_resolves_the_issue(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "dev", n)
    out = _fixed(conn, ag, "reviewer", n, "confirmed against my side")
    assert out["resolved"] is True
    assert out["awaiting_fix"] == []
    assert _status(conn, n) == todos.RESOLVED
    assert todos.get_row(conn, "bug", n)["verified_at"] is not None


def test_saying_fixed_twice_is_the_same_statement_not_a_second_one(conn):
    """An upsert, so re-reporting can never be a way to resolve an issue single-handed."""
    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "dev", n)
    _fixed(conn, ag, "dev", n, "still fixed")
    assert _status(conn, n) == todos.ACCEPTED
    assert todos.awaiting_fix(conn, todos.get_row(conn, "bug", n)) == ["reviewer"]


def test_fixed_needs_the_issue_number(conn):
    """"It's fixed" is meaningless with three issues open — the same reason every
    todo-scoped command carries its `#N`."""
    ag = _agents(conn)
    _accepted(conn, ag)
    with pytest.raises(ValueError, match="per-ISSUE"):
        state.report_status(conn, ag["dev"], state.STATUS_FIXED, "done")


def test_fixed_is_refused_before_the_issue_is_accepted(conn):
    """Agree it is real before declaring it fixed — the same gate a contract needs, for
    the same reason: the other party may still say it is not a bug at all."""
    ag = _agents(conn)
    n = _raise(conn, ag)
    with pytest.raises(ValueError, match="not accepted yet"):
        _fixed(conn, ag, "dev", n)


def test_only_a_named_party_may_report_fixed(conn):
    ag = _agents(conn, roles=("dev", "reviewer", "qa"))
    n = _accepted(conn, ag, parties=("dev", "reviewer"))
    with pytest.raises(ValueError, match="not a party"):
        _fixed(conn, ag, "qa", n)


def test_a_resolved_issue_takes_no_further_report(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "dev", n)
    _fixed(conn, ag, "reviewer", n)
    with pytest.raises(ValueError, match="is RESOLVED"):
        _fixed(conn, ag, "dev", n)


def test_restating_an_issue_resets_every_fix(conn):
    """Fix records are version-scoped like acceptances: nobody's "that's fixed" carries
    over to a problem that has since been restated."""
    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "dev", n)
    todos.repropose_todo(conn, ag["dev"], n, scope="actually it is the cache, not the cookie")
    row = todos.get_row(conn, "bug", n)
    assert todos.fixes(conn, row["id"], row["version"]) == {}
    assert todos.awaiting_fix(conn, row) == ["dev", "reviewer"]


def test_an_issue_can_be_flagged_stuck_without_bricking_the_others(conn):
    """Free with the reuse, and the same distinction as a contract task: `stuck #N` flags
    one issue, a bare `stuck` escalates the whole session."""
    ag = _agents(conn)
    a = _accepted(conn, ag, title="first")
    b = _accepted(conn, ag, title="second")
    state.report_status(conn, ag["dev"], state.STATUS_STUCK, "cannot reproduce", a)
    assert todos.get_row(conn, "bug", a)["stuck_at"] is not None
    assert _task_state(conn) == state.OPEN
    # …and the other issue still marches.
    _fixed(conn, ag, "dev", b)
    _fixed(conn, ag, "reviewer", b)
    assert _status(conn, b) == todos.RESOLVED


# --------------------------------------------------------------------------- #
# the task rollup — derived, and it does not latch
# --------------------------------------------------------------------------- #
def test_the_task_resolves_when_every_issue_is_fixed(conn):
    ag = _agents(conn)
    a = _accepted(conn, ag, title="first")
    b = _accepted(conn, ag, title="second")
    for role in ("dev", "reviewer"):
        _fixed(conn, ag, role, a)
    assert _task_state(conn) == state.OPEN, "one issue left — the task is not resolved"
    for role in ("dev", "reviewer"):
        _fixed(conn, ag, role, b)
    assert _task_state(conn) == state.RESOLVED
    roll = todos.rollup(conn, "bug")
    assert roll["state"] == state.RESOLVED
    assert roll["fixed"] == roll["total"] == 2
    assert roll["complete"] is True


def test_a_new_issue_un_resolves_a_rolled_up_task(conn):
    """Resolved BY ROLLUP is a count, not a verdict, so it goes backwards with no human
    involved — the same distinction `verified` already draws for a contract task."""
    ag = _agents(conn)
    a = _accepted(conn, ag, title="first")
    for role in ("dev", "reviewer"):
        _fixed(conn, ag, role, a)
    assert _task_state(conn) == state.RESOLVED
    b = _raise(conn, ag, title="it is back")
    assert _task_state(conn) == state.OPEN
    # …and the new issue is fully usable: nothing had to be unlocked.
    todos.accept_todo(conn, ag["reviewer"], b)
    for role in ("dev", "reviewer"):
        _fixed(conn, ag, role, b)
    assert _task_state(conn) == state.RESOLVED


def test_a_human_escalated_resolved_stays_terminal(conn):
    """The other half of the same rule: a person's last word is not a rollup, and
    reopening it needs a person."""
    ag = _agents(conn)
    state.report_status(conn, ag["dev"], state.STATUS_RESOLVED, "fixed the cookie")
    assert _task_state(conn) == state.RESOLVED
    with pytest.raises(ValueError, match="terminal state"):
        todos.propose_issue(conn, ag["dev"], "another", "detail", ["dev", "reviewer"])
    with pytest.raises(ValueError, match="terminal state|reopening"):
        state.report_status(conn, ag["reviewer"], state.STATUS_RESOLVED, "again")


def test_the_provenance_of_resolved_is_the_event_log_not_a_flag(conn):
    """`state.human_resolved` is what separates the two, and it reads the log the
    `resolved` report already writes — no second copy of the same fact."""
    ag = _agents(conn)
    a = _accepted(conn, ag)
    for role in ("dev", "reviewer"):
        _fixed(conn, ag, role, a)
    assert state.human_resolved(conn, "bug") is False
    assert todos.rollup_resolved(conn, "bug") is True

    ag2 = _agents(conn, task="bug2")
    state.report_status(conn, ag2["dev"], state.STATUS_RESOLVED, "fixed it")
    assert state.human_resolved(conn, "bug2") is True
    assert todos.rollup_resolved(conn, "bug2") is False


def test_a_contract_task_rollup_is_untouched_by_all_this(conn):
    """The debug vocabulary must not leak one word into the contract path: a contract
    task's rollup still derives the SIX march states and still says `verified`."""
    from tests.test_todos_state import _agents as _c_agents
    from tests.test_todos_state import _accepted_todo, _valid_spec

    ag = _c_agents(conn, task="signin", roles=("backend", "frontend"))
    n = _accepted_todo(conn, ag, "backend", ["backend", "frontend"])
    state.propose_contract(conn, ag["backend"], _valid_spec(), n)
    roll = todos.rollup(conn, "signin")
    assert roll["state"] == state.CONTRACT_PROPOSED
    assert "resolved" not in roll["state"]
    assert todos.status_of(conn, todos.get_row(conn, "signin", n)) == todos.CONTRACTED
    state.lock_contract(conn, ag["backend"], 1, n)
    state.lock_contract(conn, ag["frontend"], 1, n)
    state.report_status(conn, ag["backend"], "ready", "live", n)
    state.report_status(conn, ag["frontend"], "checked", "works", n)
    state.report_status(conn, ag["frontend"], "verified", "all good", n)
    assert todos.status_of(conn, todos.get_row(conn, "signin", n)) == todos.VERIFIED
    assert todos.rollup(conn, "signin")["state"] == state.VERIFIED
    assert _task_state(conn, "signin") == state.VERIFIED


# --------------------------------------------------------------------------- #
# backwards compatibility: additive, opt-in per task
# --------------------------------------------------------------------------- #
def test_a_debug_task_with_no_issues_still_works_the_old_way(conn):
    """There is a live session running this shape. It must not acquire new rules
    mid-flight: one problem, `resolved`, terminal."""
    ag = _agents(conn)
    assert todos.has_todos(conn, "bug") is False
    out = state.report_status(conn, ag["dev"], state.STATUS_RESOLVED, "fixed the cookie")
    assert out == {"status": state.STATUS_RESOLVED, "state": state.RESOLVED}
    assert _task_state(conn) == state.RESOLVED


def test_a_bare_resolved_is_refused_once_the_task_has_issues(conn):
    """Same shape of guard as the contract task's "name the deliverable", with debug
    wording: it names the live issues and the command that closes one."""
    ag = _agents(conn)
    n = _accepted(conn, ag)
    with pytest.raises(ValueError) as e:
        state.report_status(conn, ag["dev"], state.STATUS_RESOLVED, "all done")
    msg = str(e.value)
    assert "runs on ISSUES" in msg
    assert "report_status('fixed', detail, todo=<N>)" in msg
    assert f"#{n}" in msg, "the refusal must list the live issues by the number a human types"
    assert _task_state(conn) == state.OPEN


def test_resolved_with_a_number_is_refused_and_redirected(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag)
    with pytest.raises(ValueError, match="takes no number"):
        state.report_status(conn, ag["dev"], state.STATUS_RESOLVED, "done", n)


def test_the_contract_progress_words_still_do_not_apply_to_a_debug_task(conn):
    ag = _agents(conn)
    _accepted(conn, ag)
    for word in ("ready", "checked", "blocked", "verified"):
        with pytest.raises(ValueError, match="its work is ISSUES"):
            state.report_status(conn, ag["dev"], word, "?")


def test_fixed_is_refused_on_a_contract_task(conn):
    ag = _agents(conn, task="signin", roles=("backend", "frontend"), mode="contract")
    with pytest.raises(ValueError, match="'fixed' is for an ISSUE"):
        state.report_status(conn, ag["backend"], state.STATUS_FIXED, "?", 1)


# --------------------------------------------------------------------------- #
# contracts stay refused — but the message had to change
# --------------------------------------------------------------------------- #
def test_a_contract_is_refused_on_a_debug_task_and_points_at_fixed(conn):
    """The old refusal said "debug tasks don't carry todos", which is now false. The true
    statement is narrower: there is no HOW to agree on a bug."""
    ag = _agents(conn)
    n = _accepted(conn, ag)
    spec = {"version": 1, "endpoints": [{"method": "POST", "path": "/api/items"}]}
    for call in (
        lambda: state.propose_contract(conn, ag["dev"], spec, n),
        lambda: state.propose_contract(conn, ag["dev"], spec),
        lambda: state.lock_contract(conn, ag["dev"], 1, n),
        lambda: state.decline_contract(conn, ag["dev"], "no", n),
        lambda: state.reopen_negotiations(conn, ag["dev"], "replan", n),
    ):
        with pytest.raises(ValueError) as e:
            call()
        msg = str(e.value)
        assert "debug tasks don't use contracts" in msg
        assert "don't carry todos" not in msg
        assert "report_status('fixed', detail, todo=<N>)" in msg


# --------------------------------------------------------------------------- #
# two names, ONE mechanism
# --------------------------------------------------------------------------- #
def test_propose_todo_on_a_debug_task_redirects_to_propose_issue(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError) as e:
        todos.propose_todo(conn, ag["dev"], "refresh 500s", "detail", ["dev", "reviewer"])
    assert "propose_issue(title, scope, parties)" in str(e.value)


def test_propose_issue_on_a_contract_task_redirects_to_propose_todo(conn):
    ag = _agents(conn, task="signin", roles=("backend", "frontend"), mode="contract")
    with pytest.raises(ValueError) as e:
        todos.propose_issue(conn, ag["backend"], "api123", "detail", ["backend", "frontend"])
    assert "propose_todo(title, scope, parties)" in str(e.value)


def test_the_two_names_are_the_same_implementation(conn):
    """Not "behave the same" — literally the same code, so they cannot drift. The only
    difference either name makes is which mode it refuses on."""
    ag = _agents(conn)
    made = todos.propose_issue(conn, ag["dev"], "refresh 500s", "detail", ["dev", "reviewer"])
    # Same rows, same derived shape a todo has — plus the two issue-only fields.
    assert made["number"] == 1 and made["status"] == todos.PENDING
    assert made["fixed_by"] == [] and made["awaiting_fix"] == ["dev", "reviewer"]
    assert made["task_rollup"]["state"] == state.OPEN


def test_the_mode_is_never_a_parameter(conn):
    """Derived from `tasks.mode`, full stop. A `debug=` argument would be a second source
    of truth for the same fact, and the two can disagree."""
    import inspect

    for fn in (todos.propose_issue, todos.propose_todo, todos._propose,
               state.report_status, todos.record_fix, todos.status_of):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"debug", "mode", "is_debug", "kind"}, fn.__name__


# --------------------------------------------------------------------------- #
# the wire shape the dashboard and the agents read
# --------------------------------------------------------------------------- #
def test_an_issue_ships_who_has_said_fixed(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "reviewer", n)
    row = todos.to_dict(conn, todos.get_row(conn, "bug", n))
    assert row["fixed_by"] == ["reviewer"]
    assert row["awaiting_fix"] == ["dev"]
    assert row["status"] == todos.ACCEPTED


def test_a_contract_todo_ships_no_fix_fields_at_all(conn):
    """Omitted, not empty: a contract deliverable has no `fixed` step, and an always-blank
    field would invite an agent to look for a move that does not exist there."""
    from tests.test_todos_state import _agents as _c_agents

    ag = _c_agents(conn, task="signin", roles=("backend", "frontend"))
    made = todos.propose_todo(conn, ag["backend"], "api123", "scope", ["backend", "frontend"])
    assert "fixed_by" not in made and "awaiting_fix" not in made


def test_the_next_line_names_the_issue_move_and_who_owes_it(conn):
    """`next_step` is computed beside the gates, so the dashboard can never advertise a
    command the broker would refuse — including in the issue vocabulary."""
    ag = _agents(conn)
    n = _raise(conn, ag)
    tid = todos.get_row(conn, "bug", n)["id"]

    pending = state.next_step(conn, "bug", tid)
    assert pending["cmd"] == f"yes #{n}"
    assert pending["stage"] == "pending"

    todos.accept_todo(conn, ag["reviewer"], n)
    accepted = state.next_step(conn, "bug", tid)
    assert accepted["cmd"] == f"fixed #{n}"
    assert accepted["tool"] == f"report_status('fixed', detail, todo={n})"
    assert sorted(accepted["who"]) == ["dev", "reviewer"]

    _fixed(conn, ag, "dev", n)
    half = state.next_step(conn, "bug", tid)
    assert half["cmd"] == f"fixed #{n}"
    assert half["who"] == ["reviewer"], "only the party who has not reported it owes a move"

    _fixed(conn, ag, "reviewer", n)
    done = state.next_step(conn, "bug", tid)
    assert done["done"] is True and done["stage"] == "resolved"
    assert done["cmd"] is None


def test_the_api_serves_a_debug_task_its_issues(conn):
    """The dashboard reads the same three todo keys it always did — that is why the panel
    needed no new endpoint."""
    from sys_buddy import api

    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "dev", n)
    detail = api._task_detail(conn, "bug")
    assert detail["mode"] == "debug"
    assert detail["has_todos"] is True
    assert detail["todo_rollup"]["fixed"] == 0
    (row,) = detail["todos"]
    assert row["status"] == todos.ACCEPTED
    assert row["fixed_by"] == ["dev"] and row["awaiting_fix"] == ["reviewer"]
    assert row["next"]["cmd"] == f"fixed #{n}"


# --------------------------------------------------------------------------- #
# the dashboard — one renderer, one vocabulary over
# --------------------------------------------------------------------------- #
def test_the_debug_flag_comes_from_the_payloads_mode():
    """`tasks.mode` on the wire, and nothing else. A second dashboard-side flag would be
    the same duplication the tools refuse."""
    assert "function isDebugTask(d){ return !!(d && d.mode==='debug'); }" in UI_HTML


def test_the_panel_is_titled_issues_and_counts_fixed_on_a_debug_task():
    src = _fn("todoListHTML")
    assert "(debug?'Issues':'Todos')" in src
    assert "rollupBadgeHTML(d.todo_rollup, debug?'fixed':'verified')" in src
    # The count is the BROKER's, under the broker's own name for it.
    assert "r.fixed" in _fn("rollupBadgeHTML")


def test_the_debug_task_view_renders_the_issues_panel():
    """The same `todoListHTML` the contract view uses — a second list is a second thing to
    fix. Guarded by hasTodos, so a session that has raised none looks exactly as before."""
    src = _fn("debugTaskHTML")
    assert "todoListHTML(d)" in src
    assert "issues=hasTodos(d)" in src
    assert "(issues?" in src


def test_the_modal_drops_the_contract_column_entirely_on_a_debug_task():
    """Not an empty column: there is no contract on an issue, and a card explaining the
    absence of something that cannot exist is worse than one column."""
    src = _fn("todoModalHTML")
    assert "var right = debug ? '' :" in src
    assert "todoFixPillsHTML(t)" in src, "the band must carry who said fixed"
    # …and the stepper is not drawn there — five nodes that can never light.
    band = src[src.index("STATUS BAND"):]
    assert "debug\n          ? '<div style=\"flex:1;min-width:220px\">'+todoFixPillsHTML(t)" in band


def test_the_status_chip_is_always_drawn_for_an_issue():
    """It is the only thing carrying pending → accepted → resolved there, because an issue
    has no stepper."""
    assert "function todoStatusChip(t, always)" in UI_HTML
    assert "if(!always && !t.stuck && !TODO_STATUS_SHOWN[t.status]) return '';" in UI_HTML
    assert "resolved:'verified'" in UI_HTML, "the derived `resolved` status needs a palette"


def test_the_fix_row_says_the_all_must_agree_rule_out_loud():
    """"1 of 2 said fixed" is what makes a half-fixed issue read as WAITING rather than
    done — the partial state is normal and the screen must not hide it."""
    src = _fn("todoFixPillsHTML")
    assert "t.awaiting_fix" in src and "t.fixed_by" in src
    assert "' of '+parties.length+' said fixed'" in src


def test_a_fix_report_is_its_own_message_type_not_a_test_result(conn):
    """There is no test and no strike on an issue, so the thread must not chip it as one —
    and `fixed` is reserved, so no agent can post a confirmation it never made."""
    from sys_buddy import service

    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "dev", n)
    _fixed(conn, ag, "reviewer", n)
    kinds = [
        r["type"] for r in conn.execute(
            "SELECT type FROM messages WHERE task_id = 'bug' ORDER BY id"
        ).fetchall()
    ]
    assert "fixed" in kinds and "test_result" not in kinds
    assert kinds[-1] == "resolved", "the last word on an issue is `resolved`, not `verified`"
    assert "fixed" in service.RESERVED_TYPES
    with pytest.raises(ValueError, match="lifecycle event"):
        service.assert_sendable("fixed")


def test_the_event_log_calls_them_issues_on_a_debug_task(conn):
    """One stored event kind, one shape — only the word the human reads changes, and it is
    read off `tasks.mode` rather than written into the log."""
    from sys_buddy import api

    ag = _agents(conn)
    n = _accepted(conn, ag)
    _fixed(conn, ag, "dev", n)
    rendered = [e[2] for e in api._events_for(conn, "bug", "todo")]
    assert any(line.startswith(f"Issue #{n}") for line in rendered), rendered
    assert not any(line.startswith("Todo #") for line in rendered)
