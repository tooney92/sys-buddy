"""Specs for todos in the state machine: quorum, scope, and the derived task state.

Three claims are load-bearing here and each has its own test:

* a todo-scoped contract's signatory set is the TODO's party list — a task seat the
  todo does not bind neither blocks the lock nor may sign it (SEATS ≠ PARTICIPANTS);
* ``ready``/``checked``/``blocked``/``verified`` are per-DELIVERABLE once a task has
  todos, and the TASK's state is a rollup no agent sets, so the task concludes on the
  LAST todo rather than the first;
* a task with NO todos has nothing to contract — there is exactly ONE kind of contract,
  an agreement about one deliverable — and any debug task carries no todos at all.
"""

from __future__ import annotations

import pytest

from sys_buddy import contracts, service, state, todos
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
    }


def _task_state(conn, task="signin") -> str:
    return conn.execute("SELECT state FROM tasks WHERE id = ?", (task,)).fetchone()["state"]


def _todo_row(conn, num, task="signin"):
    """By the human `#N` — the same resolution every tool does."""
    return todos.get_row(conn, task, num)


def _internal_id(conn, num, task="signin") -> int:
    """The `todos.id` behind a `#N`, for the few places that join on the real key."""
    return _todo_row(conn, num, task)["id"]


def _accepted_todo(conn, ag, proposer, parties, title="api123") -> int:
    """Propose a todo and have every other named party accept it. Returns its NUMBER —
    the per-task `#N` every tool takes, never the global `todos.id`."""
    t = todos.propose_todo(conn, ag[proposer], title, f"scope of {title}", list(parties))
    for p in parties:
        if p != proposer:
            todos.accept_todo(conn, ag[p], t["number"])
    return t["number"]


def _locked_todo(conn, ag, proposer, parties, title="api123", path="/api/items"):
    """…and give it a locked contract, signed by every party. Returns (number, version)."""
    num = _accepted_todo(conn, ag, proposer, parties, title)
    r = state.propose_contract(conn, ag[proposer], _valid_spec(path), num)
    for p in parties:
        state.lock_contract(conn, ag[p], r["version"], num)
    return num, r["version"]


def _verify_todo(conn, ag, num, producer, consumer):
    """Drive one deliverable ready → checked → verified."""
    state.report_status(conn, ag[producer], "ready", "live on staging", num)
    state.report_status(conn, ag[consumer], "checked", "works against it", num)
    return state.report_status(conn, ag[consumer], "verified", "done end to end", num)


# --- quorum: the party list, not the task's roles ---------------------------
def test_lock_quorum_is_the_todos_party_list(conn):
    """The hinge of the feature: two of three seats sign, and it locks."""
    ag = _agents(conn)  # backend, frontend, mobile
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])

    r = state.propose_contract(conn, ag["backend"], _valid_spec(), num)
    assert r["signatories"] == ["backend", "mobile"]

    first = state.lock_contract(conn, ag["backend"], r["version"], num)
    assert first["locked"] is False
    assert first["remaining"] == ["mobile"]  # NOT frontend — it is not a party

    second = state.lock_contract(conn, ag["mobile"], r["version"], num)
    assert second["locked"] is True
    assert second["signed"] == ["backend", "mobile"]
    assert _todo_row(conn, num)["state"] == state.CONTRACT_LOCKED


def test_a_task_seat_that_is_not_a_party_neither_blocks_nor_signs(conn):
    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    r = state.propose_contract(conn, ag["backend"], _valid_spec(), num)

    with pytest.raises(ValueError, match="not a party"):
        state.lock_contract(conn, ag["frontend"], r["version"], num)

    # …and its absence is not a blocker: the two parties lock it without frontend.
    state.lock_contract(conn, ag["backend"], r["version"], num)
    assert state.lock_contract(conn, ag["mobile"], r["version"], num)["locked"] is True


# --- pre-flight gates the INTERACTION, not the task -------------------------
# Same rule as the two tests above, one field along: a seat a todo does not name is
# not in its quorum AND is not in its readiness check. Remote-only, because local
# self-declared identities never run pre-flight at all.
def _remote(conn):
    from sys_buddy.config import Config, get_config, set_config

    set_config(Config(mode="remote", db_path=get_config().db_path))


def _passes_preflight(conn, ag, *roles) -> None:
    for role in roles:
        conn.execute(
            "UPDATE agents SET ready = 1, readiness_status = 'passed' WHERE id = ?",
            (ag[role].agent_id,),
        )
    conn.commit()


def _lapses(conn, ag, role) -> None:
    """A seat that was revoked and re-paired comes back with ``ready = 0``. It is also
    the only way to reach an unready PARTY now that ``propose_todo`` refuses to bind
    one in the first place."""
    conn.execute(
        "UPDATE agents SET ready = 0, readiness_status = 'pending' WHERE id = ?",
        (ag[role].agent_id,),
    )
    conn.commit()


def test_an_unready_non_party_does_not_block_a_contract(conn):
    """The owner's live bug, reproduced: three seats, both todos binding two of them,
    and the third — party to NEITHER deliverable — froze the task by never running
    pre-flight. The readiness gate was task-wide; the agreement never was.
    """
    _remote(conn)
    ag = _agents(conn)  # backend, frontend, mobile
    _passes_preflight(conn, ag, "backend", "frontend")  # mobile never does

    num = _accepted_todo(conn, ag, "backend", ["backend", "frontend"])
    r = state.propose_contract(conn, ag["backend"], _valid_spec(), num)

    assert r["signatories"] == ["backend", "frontend"]
    assert _todo_row(conn, num)["state"] == state.CONTRACT_PROPOSED
    # …and the two of them can finish the agreement without mobile ever appearing.
    state.lock_contract(conn, ag["backend"], r["version"], num)
    assert state.lock_contract(conn, ag["frontend"], r["version"], num)["locked"] is True


def test_an_unready_party_still_blocks_the_contract(conn):
    """Narrowing WHO is asked did not stop anyone being asked."""
    _remote(conn)
    ag = _agents(conn)
    _passes_preflight(conn, ag, "backend", "frontend", "mobile")
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    _lapses(conn, ag, "mobile")

    with pytest.raises(ValueError) as exc:
        state.propose_contract(conn, ag["backend"], _valid_spec(), num)
    msg = str(exc.value)
    assert "pre-flight" in msg
    assert "mobile" in msg          # names who it is waiting on…
    assert "frontend" not in msg    # …and never a seat this todo does not bind

    _passes_preflight(conn, ag, "mobile")
    assert state.propose_contract(conn, ag["backend"], _valid_spec(), num)["version"] == 1


def test_a_todo_refuses_to_bind_a_seat_that_has_not_passed_preflight(conn):
    """Blocked at the moment you CHOOSE to depend on someone — the one point where the
    caller can still act on it, by waiting or by binding someone else."""
    _remote(conn)
    ag = _agents(conn)
    _passes_preflight(conn, ag, "backend", "frontend")  # mobile never does

    with pytest.raises(ValueError) as exc:
        todos.propose_todo(
            conn, ag["backend"], "Push tokens", "device registration",
            ["backend", "mobile"],
        )
    msg = str(exc.value)
    assert "mobile" in msg           # names them…
    assert "submit_readiness" in msg  # …and what they must do
    assert "send_message" in msg      # messaging an unready seat is never refused

    # The same work, bound to the seats that ARE ready, goes through untouched.
    assert todos.propose_todo(
        conn, ag["backend"], "Push tokens", "device registration",
        ["backend", "frontend"],
    )["number"] == 1


def test_a_todo_selector_is_required_to_propose_a_contract(conn):
    """There is one kind of contract, so the deliverable is never optional — and the
    refusal lists the live todos so the agent can pick one without a second call."""
    ag = _agents(conn)
    _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError, match="must name the deliverable it shapes") as exc:
        state.propose_contract(conn, ag["backend"], _valid_spec())
    assert "#1 (api123)" in str(exc.value)


def test_signing_the_wrong_deliverable_is_refused(conn):
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"], title="payments")
    b = _accepted_todo(conn, ag, "backend", ["backend", "frontend"], title="refunds")
    r = state.propose_contract(conn, ag["backend"], _valid_spec(), a)
    with pytest.raises(ValueError, match=f"belongs to todo #{a}"):
        state.lock_contract(conn, ag["backend"], r["version"], b)


def test_a_contract_needs_an_accepted_todo(conn):
    """Agree on WHAT before HOW: a contract on a pending todo is refused."""
    ag = _agents(conn)
    t = todos.propose_todo(conn, ag["backend"], "api123", "the scope", ["backend", "mobile"])
    with pytest.raises(ValueError, match="not accepted yet"):
        state.propose_contract(conn, ag["backend"], _valid_spec(), t["number"])
    # A non-party cannot contract it either, however far along it is.
    todos.accept_todo(conn, ag["mobile"], t["number"])
    with pytest.raises(ValueError, match="not a party"):
        state.propose_contract(conn, ag["frontend"], _valid_spec(), t["number"])


# --- two deliverables, one task --------------------------------------------
def test_two_todos_with_disjoint_parties_progress_independently(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, va = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b, vb = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")
    # Each deliverable owns its own contract chain, so BOTH first proposals are v1.
    assert (va, vb) == (1, 1)

    # Each todo has its OWN producer — model B, one level down.
    state.report_status(conn, ag["backend"], "ready", "pay is live", a)
    with pytest.raises(ValueError, match="only the role that proposed"):
        state.report_status(conn, ag["mobile"], "ready", "not mine to declare", a)
    state.report_status(conn, ag["frontend"], "ready", "reports are live", b)

    state.report_status(conn, ag["mobile"], "checked", "pay works", a)
    assert _todo_row(conn, a)["state"] == state.TESTING
    assert _todo_row(conn, b)["state"] == state.BACKEND_LIVE  # untouched by a's progress


def test_version_numbers_are_a_sequence_per_TODO(conn):
    """MAX+1 within the CHAIN, so every deliverable's first proposal is v1 and its chain
    is contiguous from there. A task-wide sequence made todo #2's first proposal "v2" —
    which reads as a renegotiation that never happened."""
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"], "payments")
    b = _accepted_todo(conn, ag, "backend", ["backend", "frontend"], "refunds")

    v1 = state.propose_contract(conn, ag["backend"], _valid_spec("/pay"), a)["version"]
    v2 = state.propose_contract(conn, ag["backend"], _valid_spec("/refund"), b)["version"]
    v3 = state.propose_contract(conn, ag["backend"], _valid_spec("/pay/v2"), a)["version"]
    # payments: 1 then 2. refunds: its own 1, interleaved and unaffected.
    assert [v1, v2, v3] == [1, 1, 2]

    def chain(num):
        return [
            r["version"]
            for r in conn.execute(
                "SELECT version FROM contracts WHERE todo_id = ? ORDER BY version",
                (_internal_id(conn, num),),
            )
        ]

    assert chain(a) == [1, 2]
    assert chain(b) == [1]
    assert state.get_contract(conn, "signin", a)["version"] == 2
    assert state.get_contract(conn, "signin", b)["version"] == 1


def test_a_declined_version_is_not_reused_inside_its_chain(conn):
    """MAX+1, never COUNT+1: a declined v1 keeps its number and the replacement is v2,
    so "v1" in the thread means exactly one proposal forever."""
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"], "payments")
    assert state.propose_contract(conn, ag["backend"], _valid_spec("/pay"), a)["version"] == 1
    state.decline_contract(conn, ag["mobile"], "wrong verb", a)
    assert state.propose_contract(conn, ag["backend"], _valid_spec("/pay"), a)["version"] == 2


def test_the_same_version_on_two_todos_is_two_different_contracts(conn):
    """The point of per-chain numbering: `v1` is only meaningful with a deliverable beside
    it, and the broker must never resolve one chain's number against another's."""
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"], "payments")
    b = _accepted_todo(conn, ag, "backend", ["backend", "frontend"], "refunds")
    state.propose_contract(conn, ag["backend"], _valid_spec("/pay"), a)
    state.propose_contract(conn, ag["backend"], _valid_spec("/refund"), b)

    assert state.get_contract(conn, "signin", a)["spec"]["endpoints"][0]["path"] == "/pay"
    assert state.get_contract(conn, "signin", b)["spec"]["endpoints"][0]["path"] == "/refund"
    # Signing v1 on each locks that one and leaves the other alone.
    state.lock_contract(conn, ag["backend"], 1, a)
    assert state.lock_contract(conn, ag["mobile"], 1, a)["locked"] is True
    assert state.get_contract(conn, "signin", b)["locked"] is False


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
    assert out["exists"] is False and out["todo"] == a
    assert "propose_contract" in out["note"]


def test_the_empty_contract_note_says_a_sign_instruction_means_propose(conn):
    """"Sign it" on a todo with nothing proposed is the direction to PROPOSE it.

    The note already said a proposal has to come first; an agent read it and still went
    back to its human three times. It now names the move it can make itself — propose
    under a stated assumption, then sign — since the peer's signature still gates the lock.
    """
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    note = state.get_contract(conn, "signin", a)["note"].lower()
    assert "assumption" in note
    assert "sign" in note and "propose" in note
    assert "reviews and signs" in note or "declines" in note


def test_signing_a_todo_with_nothing_proposed_names_the_scoped_proposal(conn):
    """Same teaching one level down: the error names propose_contract WITH the todo id,
    so the agent does not re-propose at task level and split the chain."""
    ag = _agents(conn)
    a = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], 1, a)
    msg = str(e.value)
    assert f"propose_contract(spec, todo={a})" in msg
    assert "nothing to sign" in msg.lower() and "assumption" in msg.lower()


# --- accept / decline / repropose ------------------------------------------
def test_proposing_is_consent_and_the_others_are_awaited(conn):
    ag = _agents(conn)
    t = todos.propose_todo(conn, ag["backend"], "api123", "the scope", ["backend", "mobile"])
    assert t["status"] == todos.PENDING
    assert t["accepted_by"] == ["backend"] and t["awaiting"] == ["mobile"]
    assert todos.accept_todo(conn, ag["mobile"], t["number"])["status"] == todos.ACCEPTED


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
    todos.accept_todo(conn, ag["mobile"], t["number"])

    out = todos.repropose_todo(conn, ag["backend"], t["id"], scope="a narrower scope")
    assert out["version"] == 2
    assert out["accepted_by"] == ["backend"]  # mobile's v1 acceptance does not carry
    assert out["status"] == todos.PENDING
    assert todos.accept_todo(conn, ag["mobile"], t["number"])["status"] == todos.ACCEPTED


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
    with pytest.raises(ValueError, match=f"no todo #{t} on task 'other'"):
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
    assert "todo=<N>" in msg          # how to fix it
    assert "get_todos()" in msg        # where to look
    assert "api123" in msg             # which todos are live


def test_stuck_works_at_both_levels_and_they_are_distinguishable(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "data"))
    a, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"], "payments", "/pay")
    b, _ = _locked_todo(conn, ag, "frontend", ["frontend", "data"], "reports", "/report")

    # With a todo: a FLAG on that deliverable. The task keeps its rollup state and the
    # sibling todo is untouched.
    per_todo = state.report_status(conn, ag["backend"], "stuck", "vendor API is down", a)
    assert per_todo["todo"] == a and per_todo["stuck"] is True
    assert per_todo["state"] == state.CONTRACT_LOCKED != state.STUCK
    assert _todo_row(conn, a)["stuck_at"] is not None
    assert _todo_row(conn, b)["stuck_at"] is None
    assert _task_state(conn) == state.CONTRACT_LOCKED

    # Without one: the whole collaboration escalates, terminally.
    whole = state.report_status(conn, ag["backend"], "stuck", "my token expired")
    assert whole == {"status": state.STATUS_STUCK, "state": state.STUCK}
    assert "todo" not in whole and "todo_id" not in whole
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


@pytest.mark.parametrize("status", ["ready", "checked", "blocked", "verified", "stuck"])
def test_a_verified_todo_cannot_be_marched_backwards(conn, status):
    """`verified` is still terminal for the TODO — otherwise a later report would
    unverify a finished deliverable and the task's rollup would be a lie."""
    ag = _agents(conn)
    t, _ = _locked_todo(conn, ag, "backend", ["backend", "mobile"])
    _verify_todo(conn, ag, t, "backend", "mobile")
    with pytest.raises(ValueError, match="is verified"):
        state.report_status(conn, ag["backend"], status, "wait, one more thing", t)
    assert _todo_row(conn, t)["state"] == state.VERIFIED
    assert _task_state(conn) == state.VERIFIED


# --- a task with no todos has nothing to contract ---------------------------
def test_a_task_with_no_todos_has_nothing_to_contract(conn):
    """The refusal has to teach the next call, not just say no: a contract is an
    agreement about ONE deliverable, so a task with none is pointed at propose_todo
    rather than left guessing which argument it got wrong."""
    ag = _agents(conn, roles=("backend", "frontend"))
    with pytest.raises(ValueError, match="has no todos yet") as exc:
        state.propose_contract(conn, ag["backend"], _valid_spec())
    assert "propose_todo(title, scope, parties)" in str(exc.value)
    # …and signing refuses for the same reason, rather than "no such version 1",
    # which would send the agent looking for a contract that could never exist.
    with pytest.raises(ValueError, match="no todos, so it has no contracts"):
        state.lock_contract(conn, ag["backend"], 1)


def test_a_debug_task_with_no_issues_is_behaviourally_unchanged(conn):
    """A debug session that has raised NO issues behaves exactly as it did before issues
    existed — bare `resolved`, terminal. There are live sessions running that way and the
    feature is opt-in per task; the opt-in is raising the first issue."""
    ag = _agents(conn, roles=("backend", "frontend"), mode="debug")
    # `propose_todo` is the wrong NAME here now, not a refused capability: a debug task's
    # work is issues, and the refusal redirects rather than saying "not supported".
    with pytest.raises(ValueError, match="propose_issue"):
        todos.propose_todo(conn, ag["backend"], "api123", "scope", ["backend", "frontend"])
    with pytest.raises(ValueError, match="takes no number"):
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


# --------------------------------------------------------------------------- #
# the proposal announcement counts the units of ITS OWN kind
# --------------------------------------------------------------------------- #
def _one_unit_spec(kind) -> dict:
    """A minimal VALID spec of `kind`, carrying exactly one unit."""
    return {
        "http": {"endpoints": [{"method": "POST", "path": "/api/items"}]},
        "schema": {"types": [{"name": "Item", "fields": [{"name": "id", "type": "string"}]}]},
        "ui": {"screens": [{"name": "ItemList", "states": ["loading", "loaded"]}]},
        "none": {"criteria": ["the list renders once items load"]},
    }[kind.name]


@pytest.mark.parametrize("kind_name", sorted(contracts.KINDS))
def test_the_proposal_message_counts_the_units_of_its_own_kind(conn, kind_name):
    """A `ui` contract of one screen announced itself as "(0 endpoints)".

    The count was hardcoded to `spec["endpoints"]`, so every kind but http reported
    zero — which reads as an EMPTY contract to the one agent whose job is to review it,
    on the surface that is its only prompt to go and look. Parameterised over the kind
    table so a new kind fails here until its announcement counts the right thing.
    """
    kind = contracts.KINDS[kind_name]
    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    state.propose_contract(conn, ag["backend"], _one_unit_spec(kind), num)

    body = conn.execute(
        "SELECT body_json FROM messages WHERE type = 'contract_proposal' ORDER BY id DESC LIMIT 1"
    ).fetchone()["body_json"]
    # Singular, because there is exactly one unit — and never another kind's noun.
    assert f"(1 {kind.unit_label})" in body
    for other in contracts.KINDS.values():
        if other.name != kind.name:
            assert other.unit_key not in body


def test_criteria_are_not_pluralised_into_criterions(conn):
    """`unit_key` carries the plural precisely because appending "s" to `unit_label`
    is wrong for the one kind whose plural is irregular."""
    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    state.propose_contract(conn, ag["backend"], {"criteria": [
        "the list renders once items load",
        "the empty state renders when there are none",
    ]}, num)
    body = conn.execute(
        "SELECT body_json FROM messages WHERE type = 'contract_proposal' ORDER BY id DESC LIMIT 1"
    ).fetchone()["body_json"]
    assert "(2 criteria)" in body
    assert "criterions" not in body


# --------------------------------------------------------------------------- #
# a superseded draft cannot be signed
# --------------------------------------------------------------------------- #
def test_signing_a_superseded_draft_is_refused(conn):
    """Proposing v2 does NOT mark v1 as anything — it stays `draft` forever.

    So without this guard both parties could sign the OLD shape while v2 was the live
    proposal, and the deliverable would end up contracted against something nobody was
    discussing. Found on a real task whose dashboard offered v1 and v2 as equal tabs;
    reproduced by signing v1 to completion and watching it lock.

    Only DRAFTS supersede this way. A LOCKED v1 followed by a v2 is the ordinary
    renegotiation flow, and signing v1 again is already refused as immutable.
    """
    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    state.propose_contract(conn, ag["backend"], _valid_spec("/v1"), num)
    state.propose_contract(conn, ag["backend"], _valid_spec("/v2"), num)

    with pytest.raises(ValueError, match="superseded by v2") as e:
        state.lock_contract(conn, ag["backend"], 1, num)
    # It must name the version to sign INSTEAD, or the agent has to go looking.
    assert "lock_contract(2" in str(e.value)


def test_the_current_version_still_signs(conn):
    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    state.propose_contract(conn, ag["backend"], _valid_spec("/v1"), num)
    state.propose_contract(conn, ag["backend"], _valid_spec("/v2"), num)
    state.lock_contract(conn, ag["backend"], 2, num)
    r = state.lock_contract(conn, ag["mobile"], 2, num)
    assert r["locked"] is True


def test_a_locked_v1_under_a_v2_still_reports_immutable_not_superseded(conn):
    """The renegotiation flow keeps its own, better-targeted message: v1 is not dead
    scope, it is a decided agreement, and the advice differs."""
    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    state.propose_contract(conn, ag["backend"], _valid_spec("/v1"), num)
    state.lock_contract(conn, ag["backend"], 1, num)
    state.lock_contract(conn, ag["mobile"], 1, num)
    state.propose_contract(conn, ag["backend"], _valid_spec("/v2"), num)

    with pytest.raises(ValueError, match="already locked and immutable"):
        state.lock_contract(conn, ag["backend"], 1, num)


def test_the_dashboard_marks_a_superseded_draft(conn):
    """It must not be offered as an equal tab beside the live one — nobody can sign it."""
    from sys_buddy import api

    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    state.propose_contract(conn, ag["backend"], _valid_spec("/v1"), num)
    state.propose_contract(conn, ag["backend"], _valid_spec("/v2"), num)

    todo_id = todos.get_row(conn, "signin", num)["id"]
    versions = api._contract_for(conn, "signin", todo_id=todo_id)["versions"]
    by_id = {v["id"]: v for v in versions}
    assert by_id["v1"]["superseded"] is True
    # Named, so the panel can send the reader to the one they should be reading.
    assert by_id["v1"]["superseded_by"] == "v2"
    assert by_id["v2"]["superseded"] is False
    assert by_id["v2"]["superseded_by"] is None


def test_the_dashboard_marks_a_locked_version_superseded_by_a_later_lock(conn):
    """Renegotiation is the ORDINARY way a contract changes, so a chain holds more than one
    locked version — and only the newest is the agreement in force. The panel rendered every
    one of them as "Locked · signed by all parties", identical green chips and all, so an old
    version read as authoritative and a reader could integrate against dead scope."""
    from sys_buddy import api

    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    todo_id = todos.get_row(conn, "signin", num)["id"]

    state.propose_contract(conn, ag["backend"], _valid_spec("/v1"), num)
    state.lock_contract(conn, ag["backend"], 1, num)
    state.lock_contract(conn, ag["mobile"], 1, num)

    # A DRAFT above a lock supersedes nothing: the last lock keeps serving until the new
    # version is signed by everyone, which is `state._reopen_todo`'s promise.
    state.propose_contract(conn, ag["backend"], _valid_spec("/v2"), num)
    by_id = {
        v["id"]: v
        for v in api._contract_for(conn, "signin", todo_id=todo_id)["versions"]
    }
    assert by_id["v1"]["superseded"] is False
    assert by_id["v1"]["superseded_by"] is None

    # …and the moment v2 LOCKS, v1 stops being the agreement.
    state.lock_contract(conn, ag["backend"], 2, num)
    state.lock_contract(conn, ag["mobile"], 2, num)
    block = api._contract_for(conn, "signin", todo_id=todo_id)
    by_id = {v["id"]: v for v in block["versions"]}
    assert by_id["v1"]["superseded"] is True
    assert by_id["v1"]["superseded_by"] == "v2"
    assert by_id["v2"]["superseded"] is False
    assert by_id["v2"]["superseded_by"] is None
    # The per-version payload carries it too — the panel renders from `data[selected]`.
    assert block["data"]["v1"]["superseded"] is True
    assert block["data"]["v1"]["superseded_by"] == "v2"
    assert block["data"]["v2"]["superseded"] is False
    # v1's signatures are still real and still listed; only their PALETTE changes.
    assert {s["role"] for s in block["data"]["v1"]["signed"]} == {"backend", "mobile"}
    # `default` is unmoved — the newest lock was already the one the panel opens on.
    assert block["default"] == "v2"


def test_a_declined_version_is_not_superseded(conn):
    """Declined is decided, not replaced: it was never in force, so nothing took over from
    it, and it keeps its own message rather than being told to read a later version."""
    from sys_buddy import api

    ag = _agents(conn)
    num = _accepted_todo(conn, ag, "backend", ["backend", "mobile"])
    todo_id = todos.get_row(conn, "signin", num)["id"]
    state.propose_contract(conn, ag["backend"], _valid_spec("/v1"), num)
    state.decline_contract(conn, ag["mobile"], "wrong shape", num)
    state.propose_contract(conn, ag["backend"], _valid_spec("/v2"), num)

    by_id = {
        v["id"]: v
        for v in api._contract_for(conn, "signin", todo_id=todo_id)["versions"]
    }
    assert by_id["v1"]["status"] == "declined"
    assert by_id["v1"]["superseded"] is False
    assert by_id["v1"]["superseded_by"] is None
