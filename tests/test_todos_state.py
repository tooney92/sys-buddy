"""Specs for todos in the state machine: quorum, scope, and the derived task state.

Three claims are load-bearing here and each has its own test:

* a todo-scoped contract's signatory set is the TODO's party list — a task seat the
  todo does not bind neither blocks the lock nor may sign it (SEATS ≠ PARTICIPANTS);
* ``ready``/``checked``/``blocked``/``verified`` are per-DELIVERABLE once a task has
  todos, and the TASK's state is a rollup no agent sets, so the task concludes on the
  LAST todo rather than the first;
* a task with NO todos, and any debug task, behaves exactly as it did before todos
  existed — the migration story is "do nothing".
"""

from __future__ import annotations

import pytest

from sys_buddy import service, state, todos
from tests.conftest import seed_agent, seed_task


def _agents(conn, task="signin", roles=("backend", "frontend", "mobile"), mode="contract"):
    """Seed a task (with `mode`) and return {role: Identity} for each declared role."""
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


def _valid_spec(path="/api/items") -> dict:
    return {
        "version": 1,
        "endpoints": [{"method": "POST", "path": path}],
        "staging_url": "https://api-staging.example.com",
    }


def _task_state(conn, task="signin") -> str:
    return conn.execute("SELECT state FROM tasks WHERE id = ?", (task,)).fetchone()["state"]


def _todo_row(conn, todo_id, task="signin"):
    return todos.get_row(conn, task, todo_id)


def _accepted_todo(conn, ag, proposer, parties, title="api123") -> int:
    """Propose a todo and have every other named party accept it."""
    t = todos.propose_todo(conn, ag[proposer], title, f"scope of {title}", list(parties))
    for p in parties:
        if p != proposer:
            todos.accept_todo(conn, ag[p], t["id"])
    return t["id"]


def _locked_todo(conn, ag, proposer, parties, title="api123", path="/api/items"):
    """…and give it a locked contract, signed by every party. Returns (todo_id, version)."""
    todo_id = _accepted_todo(conn, ag, proposer, parties, title)
    r = state.propose_contract(conn, ag[proposer], _valid_spec(path), todo_id)
    for p in parties:
        state.lock_contract(conn, ag[p], r["version"], todo_id)
    return todo_id, r["version"]


def _verify_todo(conn, ag, todo_id, producer, consumer):
    """Drive one deliverable ready → checked → verified."""
    state.report_status(conn, ag[producer], "ready", "live on staging", todo_id)
    state.report_status(conn, ag[consumer], "checked", "works against it", todo_id)
    return state.report_status(conn, ag[consumer], "verified", "done end to end", todo_id)


# --- quorum: the party list, not the task's roles ---------------------------
def test_lock_quorum_is_the_todos_party_list(conn):
    """The hinge of the feature: two of three seats sign, and it locks."""
    ag = _agents(conn)  # backend, frontend, mobile
    todo_id = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])

    r = state.propose_contract(conn, ag["backend"], _valid_spec(), todo_id)
    assert r["signatories"] == ["backend", "mobile"]

    first = state.lock_contract(conn, ag["backend"], r["version"], todo_id)
    assert first["locked"] is False
    assert first["remaining"] == ["mobile"]  # NOT frontend — it is not a party

    second = state.lock_contract(conn, ag["mobile"], r["version"], todo_id)
    assert second["locked"] is True
    assert second["signed"] == ["backend", "mobile"]
    assert _todo_row(conn, todo_id)["state"] == state.CONTRACT_LOCKED


def test_a_task_seat_that_is_not_a_party_neither_blocks_nor_signs(conn):
    ag = _agents(conn)
    todo_id = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    r = state.propose_contract(conn, ag["backend"], _valid_spec(), todo_id)

    with pytest.raises(ValueError, match="not a party"):
        state.lock_contract(conn, ag["frontend"], r["version"], todo_id)

    # …and its absence is not a blocker: the two parties lock it without frontend.
    state.lock_contract(conn, ag["backend"], r["version"], todo_id)
    assert state.lock_contract(conn, ag["mobile"], r["version"], todo_id)["locked"] is True


def test_task_level_quorum_is_untouched_by_the_existence_of_todos(conn):
    """A task with no todos still needs ALL roles — the old rule, unchanged."""
    ag = _agents(conn)
    r = state.propose_contract(conn, ag["backend"], _valid_spec())
    assert state.lock_contract(conn, ag["backend"], r["version"])["remaining"] == [
        "frontend", "mobile",
    ]
    state.lock_contract(conn, ag["frontend"], r["version"])
    assert state.lock_contract(conn, ag["mobile"], r["version"])["locked"] is True


def test_a_todo_selector_is_required_once_the_task_has_todos(conn):
    ag = _agents(conn)
    _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError, match="runs on todos"):
        state.propose_contract(conn, ag["backend"], _valid_spec())


def test_a_task_level_contract_cannot_be_signed_with_a_todo_selector(conn):
    ag = _agents(conn)
    r = state.propose_contract(conn, ag["backend"], _valid_spec())
    todo_id = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError, match="TASK-level"):
        state.lock_contract(conn, ag["backend"], r["version"], todo_id)


def test_signing_the_wrong_deliverable_is_refused(conn):
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"], title="payments")
    b = _accepted_todo(conn, ag, "backend", ["backend", "frontend"], title="refunds")
    r = state.propose_contract(conn, ag["backend"], _valid_spec(), a)
    with pytest.raises(ValueError, match=f"belongs to todo {a}"):
        state.lock_contract(conn, ag["backend"], r["version"], b)


def test_a_contract_needs_an_accepted_todo(conn):
    """Agree on WHAT before HOW: a contract on a pending todo is refused."""
    ag = _agents(conn)
    t = todos.propose_todo(conn, ag["backend"], "api123", "the scope", ["backend", "mobile"])
    with pytest.raises(ValueError, match="not accepted yet"):
        state.propose_contract(conn, ag["backend"], _valid_spec(), t["id"])
    # A non-party cannot contract it either, however far along it is.
    todos.accept_todo(conn, ag["mobile"], t["id"])
    with pytest.raises(ValueError, match="not a party"):
        state.propose_contract(conn, ag["frontend"], _valid_spec(), t["id"])


# --- two deliverables, one task --------------------------------------------
def test_two_todos_with_disjoint_parties_progress_independently(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, va = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b, vb = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")
    assert (va, vb) == (1, 2)

    # Each todo has its OWN producer — model B, one level down.
    state.report_status(conn, ag["backend"], "ready", "pay is live", a)
    with pytest.raises(ValueError, match="only the role that proposed"):
        state.report_status(conn, ag["mobile"], "ready", "not mine to declare", a)
    state.report_status(conn, ag["frontend"], "ready", "reports are live", b)

    state.report_status(conn, ag["mobile"], "checked", "pay works", a)
    assert _todo_row(conn, a)["state"] == state.TESTING
    assert _todo_row(conn, b)["state"] == state.BACKEND_LIVE  # untouched by a's progress


def test_version_numbers_stay_one_sequence_per_task(conn):
    """One MAX+1 sequence per TASK, so a todo's chain is v1, v4, v7 — not renumbered."""
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"], "payments")
    b = _accepted_todo(conn, ag, "backend", ["backend", "frontend"], "refunds")

    v1 = state.propose_contract(conn, ag["backend"], _valid_spec("/pay"), a)["version"]
    v2 = state.propose_contract(conn, ag["backend"], _valid_spec("/refund"), b)["version"]
    v3 = state.propose_contract(conn, ag["backend"], _valid_spec("/pay/v2"), a)["version"]
    assert [v1, v2, v3] == [1, 2, 3]

    chain_a = [
        r["version"]
        for r in conn.execute("SELECT version FROM contracts WHERE todo_id = ? ORDER BY version", (a,))
    ]
    assert chain_a == [1, 3]  # non-contiguous but unambiguous
    assert state.get_contract(conn, "signin", a)["version"] == 3
    assert state.get_contract(conn, "signin", b)["version"] == 2


def test_get_contract_scopes_awaiting_to_the_todos_parties(conn):
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    state.propose_contract(conn, ag["backend"], _valid_spec(), a)
    out = state.get_contract(conn, "signin", a)
    assert out["awaiting"] == ["backend", "mobile"]  # frontend is seated, not bound
    assert out["signatories"] == ["backend", "mobile"]
    assert out["staging_url"] is None  # still withheld until it locks


def test_get_contract_on_a_todo_with_no_contract_says_so(conn):
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    out = state.get_contract(conn, "signin", a)
    assert out["exists"] is False and out["todo_id"] == a
    assert "propose_contract" in out["note"]


# --- accept / decline / repropose ------------------------------------------
def test_proposing_is_consent_and_the_others_are_awaited(conn):
    ag = _agents(conn)
    t = todos.propose_todo(conn, ag["backend"], "api123", "the scope", ["backend", "mobile"])
    assert t["status"] == todos.PENDING
    assert t["accepted_by"] == ["backend"] and t["awaiting"] == ["mobile"]
    assert todos.accept_todo(conn, ag["mobile"], t["id"])["status"] == todos.ACCEPTED


def test_decline_is_recorded_beside_the_acceptances(conn):
    ag = _agents(conn)
    t = todos.propose_todo(conn, ag["backend"], "api123", "the scope", ["backend", "mobile"])
    out = todos.decline_todo(conn, ag["mobile"], t["id"], "the scope covers two features")
    assert out["status"] == todos.PENDING  # not a 'declined' STATUS
    assert out["declined_by"] == ["mobile"]
    assert "two features" in out["decline_reasons"]["mobile"]


def test_repropose_issues_a_new_version_and_resets_acceptances(conn):
    ag = _agents(conn)
    t = todos.propose_todo(conn, ag["backend"], "api123", "the scope", ["backend", "mobile"])
    todos.accept_todo(conn, ag["mobile"], t["id"])

    out = todos.repropose_todo(conn, ag["backend"], t["id"], scope="a narrower scope")
    assert out["version"] == 2
    assert out["accepted_by"] == ["backend"]  # mobile's v1 acceptance does not carry
    assert out["status"] == todos.PENDING
    assert todos.accept_todo(conn, ag["mobile"], t["id"])["status"] == todos.ACCEPTED


def test_repropose_resets_a_draft_contracts_signatures(conn):
    """The others signed a shape that bound two parties; it may now bind three."""
    ag = _agents(conn)
    t = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    r = state.propose_contract(conn, ag["backend"], _valid_spec(), t)
    state.lock_contract(conn, ag["backend"], r["version"], t)
    assert state.get_contract(conn, "signin", t)["signatures"] == ["backend"]

    todos.repropose_todo(conn, ag["backend"], t, parties=["backend", "mobile", "frontend"])
    assert state.get_contract(conn, "signin", t)["signatures"] == []
    assert state.get_contract(conn, "signin", t)["awaiting"] == [
        "backend", "mobile", "frontend",
    ]


def test_repropose_is_refused_once_a_contract_locked(conn):
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError, match="reopen_negotiations"):
        todos.repropose_todo(conn, ag["backend"], t, scope="something else")


def test_reopen_negotiations_is_per_todo(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b, _ = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")

    with pytest.raises(ValueError, match="runs on todos"):
        state.reopen_negotiations(conn, ag["backend"], "which one?")
    with pytest.raises(ValueError, match="not a party"):
        state.reopen_negotiations(conn, ag["frontend"], "not mine", a)

    out = state.reopen_negotiations(conn, ag["backend"], "shape changed", a)
    assert out["todo_state"] == state.CONTRACT_PROPOSED
    assert _todo_row(conn, b)["state"] == state.CONTRACT_LOCKED  # the sibling keeps its lock
    # The old lock still serves until a new version locks (non-destructive).
    assert state.get_contract(conn, "signin", a)["locked"] is True


# --- drop -------------------------------------------------------------------
def test_drop_needs_every_named_partys_consent(conn):
    ag = _agents(conn)
    t = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])

    half = todos.drop_todo(conn, ag["backend"], t, "we don't need it")
    assert half["status"] != todos.DROPPED
    assert half["drop_consents"] == ["backend"]

    done = todos.drop_todo(conn, ag["mobile"], t, "agreed")
    assert done["status"] == todos.DROPPED
    assert todos.has_todos(conn, "signin") is False  # nothing left to roll up


def test_drop_is_blocked_once_the_todo_is_verified(conn):
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    _verify_todo(conn, ag, t, "backend", "mobile")
    with pytest.raises(ValueError, match="verified and cannot be dropped"):
        todos.drop_todo(conn, ag["backend"], t, "changed our minds")
    with pytest.raises(ValueError, match="verified and cannot be dropped"):
        todos.host_drop_todo(conn, "signin", t, "the human wants it gone")


def test_a_dropped_todo_stops_accepting_contracts_and_reports(conn):
    ag = _agents(conn)
    t, version = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    todos.drop_todo(conn, ag["backend"], t, "obsolete")
    todos.drop_todo(conn, ag["mobile"], t, "obsolete")

    with pytest.raises(ValueError, match="dropped"):
        state.propose_contract(conn, ag["backend"], _valid_spec(), t)
    with pytest.raises(ValueError, match="dropped"):
        state.report_status(conn, ag["backend"], "ready", "live", t)


# --- who may act ------------------------------------------------------------
def test_a_non_party_cannot_act_on_a_todo(conn):
    ag = _agents(conn)
    t = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    for call in (
        lambda: todos.accept_todo(conn, ag["frontend"], t),
        lambda: todos.decline_todo(conn, ag["frontend"], t, "no"),
        lambda: todos.repropose_todo(conn, ag["frontend"], t, scope="mine now"),
        lambda: todos.drop_todo(conn, ag["frontend"], t, "bin it"),
        lambda: state.report_status(conn, ag["frontend"], "checked", "works", t),
    ):
        with pytest.raises(ValueError, match="not a party"):
            call()


def test_a_proposer_must_be_a_party_to_its_own_todo(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError, match="must be one of the parties"):
        todos.propose_todo(conn, ag["frontend"], "api123", "scope", ["backend", "mobile"])


def test_a_todo_cannot_reach_across_tasks(conn):
    ag = _agents(conn)
    other = _agents(conn, task="other", roles=("backend", "mobile"))
    t = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError, match=f"no todo {t} on task 'other'"):
        todos.accept_todo(conn, other["mobile"], t)


# --- report_status scope ----------------------------------------------------
@pytest.mark.parametrize("status", ["ready", "checked", "blocked", "verified"])
def test_report_status_requires_a_todo_once_the_task_has_todos(conn, status):
    ag = _agents(conn)
    _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError) as e:
        state.report_status(conn, ag["backend"], status, "no idea which one")
    msg = str(e.value)
    assert status in msg               # the word the AGENT typed, not the alias
    assert "todo=<id>" in msg          # how to fix it
    assert "get_todos()" in msg        # where to look
    assert "api123" in msg             # which todos are live


def test_stuck_works_at_both_levels_and_they_are_distinguishable(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b, _ = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")

    # With a todo: a FLAG on that deliverable. The task keeps its rollup state and the
    # sibling todo is untouched.
    per_todo = state.report_status(conn, ag["backend"], "stuck", "vendor API is down", a)
    assert per_todo["todo_id"] == a and per_todo["stuck"] is True
    assert per_todo["state"] == state.CONTRACT_LOCKED != state.STUCK
    assert _todo_row(conn, a)["stuck_at"] is not None
    assert _todo_row(conn, b)["stuck_at"] is None
    assert _task_state(conn) == state.CONTRACT_LOCKED

    # Without one: the whole collaboration escalates, terminally.
    whole = state.report_status(conn, ag["backend"], "stuck", "my token expired")
    assert whole == {"status": state.STATUS_STUCK, "state": state.STUCK}
    assert "todo_id" not in whole
    assert _task_state(conn) == state.STUCK
    # …and it outranks the rollup: nothing moves until a human reopens it.
    with pytest.raises(ValueError, match="terminal state 'stuck'"):
        state.report_status(conn, ag["frontend"], "ready", "reports are live", b)


def test_a_stuck_todo_clears_when_the_deliverable_moves_again(conn):
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    state.report_status(conn, ag["backend"], "stuck", "vendor API is down", t)
    state.report_status(conn, ag["backend"], "ready", "vendor is back, live now", t)
    assert _todo_row(conn, t)["stuck_at"] is None


def test_three_strikes_pull_the_cord_on_the_todo_only(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b, _ = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")
    state.report_status(conn, ag["backend"], "ready", "live", a)

    for i in (1, 2, 3):
        out = state.report_status(conn, ag["mobile"], "blocked", f"400 on POST #{i}", a)
        assert out["strikes"] == i
    assert _todo_row(conn, a)["stuck_at"] is not None
    assert _task_state(conn) != state.STUCK  # one bricked deliverable, not a dead task

    with pytest.raises(ValueError, match="pulled the cord"):
        state.report_status(conn, ag["mobile"], "blocked", "still broken", a)
    # The other deliverable is completely unaffected.
    state.report_status(conn, ag["frontend"], "ready", "reports are live", b)
    assert _todo_row(conn, b)["state"] == state.BACKEND_LIVE


def test_ready_needs_a_locked_contract_on_that_todo(conn):
    ag = _agents(conn)
    t = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError, match="no locked contract exists on it"):
        state.report_status(conn, ag["backend"], "ready", "live", t)

    r = state.propose_contract(conn, ag["backend"], _valid_spec(), t)
    state.lock_contract(conn, ag["backend"], r["version"], t)
    with pytest.raises(ValueError, match="awaiting signatures"):
        state.report_status(conn, ag["backend"], "ready", "live", t)


def test_checks_are_refused_before_the_todos_producer_is_ready(conn):
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError, match="before its producer is ready"):
        state.report_status(conn, ag["mobile"], "checked", "works", t)


def test_the_producer_of_a_todo_does_not_check_its_own_work(conn):
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    state.report_status(conn, ag["backend"], "ready", "live", t)
    with pytest.raises(ValueError, match="doesn't report checks on its own work"):
        state.report_status(conn, ag["backend"], "checked", "looks fine to me", t)


def test_verified_on_a_todo_needs_a_check_first(conn):
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    state.report_status(conn, ag["backend"], "ready", "live", t)
    with pytest.raises(ValueError, match="before checks have run"):
        state.report_status(conn, ag["mobile"], "verified", "trust me", t)


# --- the rollup: the task's state stops being agent-driven ------------------
def test_the_task_state_tracks_its_todos(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"], "payments")
    assert _task_state(conn) == state.OPEN

    r = state.propose_contract(conn, ag["backend"], _valid_spec("/pay"), a)
    assert _task_state(conn) == state.CONTRACT_PROPOSED
    for p in ("backend", "mobile"):
        state.lock_contract(conn, ag[p], r["version"], a)
    assert _task_state(conn) == state.CONTRACT_LOCKED

    # A second todo drags the task BACK to the furthest state its parts justify —
    # a rollup can go backwards, which is exactly why `verified` is no longer terminal.
    b, _ = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")
    state.report_status(conn, ag["backend"], "ready", "live", a)
    assert _task_state(conn) == state.BACKEND_LIVE


def test_the_task_concludes_only_when_the_last_todo_verifies(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b, _ = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")

    first = _verify_todo(conn, ag, a, "backend", "mobile")
    assert first["todo_state"] == state.VERIFIED
    assert first["rollup"]["verified"] == 1 and first["rollup"]["complete"] is False
    assert _task_state(conn) != state.VERIFIED  # one down, one to go

    last = _verify_todo(conn, ag, b, "frontend", "data")
    assert last["rollup"]["complete"] is True
    assert _task_state(conn) == state.VERIFIED


def test_a_dropped_todo_stops_holding_the_task_open(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b = _accepted_todo(conn, ag, "frontend", ["frontend", "data"], "reports")
    _verify_todo(conn, ag, a, "backend", "mobile")
    assert _task_state(conn) != state.VERIFIED

    todos.host_drop_todo(conn, "signin", b, "we shipped without reports")
    assert _task_state(conn) == state.VERIFIED  # the last LIVE todo is verified


def test_a_verified_task_reopens_when_a_new_todo_appears(conn):
    """`verified` must not become a one-way door a rollup cannot leave."""
    ag = _agents(conn)
    a, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    _verify_todo(conn, ag, a, "backend", "mobile")
    assert _task_state(conn) == state.VERIFIED

    b, _ = _locked_todo(conn, ag, "backend", ["backend", "frontend"], "refunds", "/refund")
    assert _task_state(conn) == state.CONTRACT_LOCKED
    assert todos.rollup(conn, "signin")["verified"] == 1


def test_a_verified_todo_is_not_recontracted(conn):
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    _verify_todo(conn, ag, t, "backend", "mobile")
    with pytest.raises(ValueError, match="already verified"):
        state.propose_contract(conn, ag["backend"], _valid_spec(), t)
    with pytest.raises(ValueError, match="verified"):
        state.reopen_negotiations(conn, ag["backend"], "one more field", t)


# --- regression: nothing changes for a task without todos ------------------
def test_a_task_with_no_todos_is_behaviourally_unchanged(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    r = state.propose_contract(conn, ag["backend"], _valid_spec())
    assert r == {"version": 1, "state": state.CONTRACT_PROPOSED}
    state.lock_contract(conn, ag["backend"], 1)
    assert state.lock_contract(conn, ag["frontend"], 1) == {
        "locked": True, "version": 1, "signed": ["backend", "frontend"],
        "state": state.CONTRACT_LOCKED,
    }
    # No todo id anywhere, and `verified` is still TERMINAL.
    assert state.report_status(conn, ag["backend"], "ready", "live") == {
        "status": state.STATUS_DEPLOYED, "state": state.BACKEND_LIVE, "strikes": 0,
    }
    assert state.report_status(conn, ag["frontend"], "checked", "works") == {
        "status": state.STATUS_TEST_PASSED, "state": state.TESTING, "strikes": 0,
    }
    assert state.report_status(conn, ag["frontend"], "verified", "done") == {
        "status": state.STATUS_VERIFIED, "state": state.VERIFIED,
    }
    with pytest.raises(ValueError, match="terminal state 'verified'"):
        state.report_status(conn, ag["backend"], "stuck", "too late")


def test_a_debug_task_is_behaviourally_unchanged(conn):
    ag = _agents(conn, roles=("backend", "frontend"), mode="debug")
    with pytest.raises(ValueError, match="debug tasks don't carry todos"):
        todos.propose_todo(conn, ag["backend"], "api123", "scope", ["backend", "frontend"])
    with pytest.raises(ValueError, match="debug tasks don't carry todos"):
        state.report_status(conn, ag["backend"], "resolved", "fixed", 1)
    with pytest.raises(ValueError, match="this is a debug task"):
        state.report_status(conn, ag["backend"], "ready", "live")
    assert state.report_status(conn, ag["backend"], "resolved", "fixed") == {
        "status": state.STATUS_RESOLVED, "state": state.RESOLVED,
    }


def test_todos_cannot_be_added_to_a_terminated_task(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.report_status(conn, ag["backend"], "stuck", "humans needed")
    with pytest.raises(ValueError, match="terminal state 'stuck'"):
        todos.propose_todo(conn, ag["backend"], "api123", "scope", ["backend", "frontend"])
