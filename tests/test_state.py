"""Specs for the enforced state machine, contract lock, and strikes (SPEC §5/6/8).

These assert the guiding principle: the broker enforces in code, not prompt. Every
rejection here is a ``ValueError`` an agent cannot argue with.
"""

from __future__ import annotations

import pytest

from sys_buddy import admin, service, state, todos
from tests.conftest import seed_agent, seed_task


def _agents(conn, task="signin", roles=("backend", "frontend"), todo=True, ready=True):
    """Seed a task with an accepted todo #1, and return {role: Identity} per role.

    Every contract belongs to a deliverable, so a task with no todos has nothing to
    contract — the setup has to agree on WHAT before any test can exercise HOW. Built
    through the real ops (``propose_todo`` → ``accept_todo``) rather than an INSERT, so
    the fixture cannot drift from the flow it stands in for.

    ``todo=False`` for the handful of tests that assert what the broker says when there
    is no deliverable yet.

    Everyone passes pre-flight by default (``ready=False`` to opt out). That is not a
    convenience — in remote mode an agent that has not passed cannot call a single
    action tool (``middleware``), so a fixture that proposed a todo with ready=0 agents
    was standing in for a state the broker can never actually be in.
    """
    seed_task(conn, task, roles=roles)
    ag = {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in roles
    }
    if ready:
        for ident in ag.values():
            conn.execute(
                "UPDATE agents SET ready = 1, readiness_status = 'passed' WHERE id = ?",
                (ident.agent_id,),
            )
        conn.commit()
    if todo:
        first = roles[0]
        todos.propose_todo(
            conn, ag[first], "Sign-in endpoint", "POST /api/auth/login", list(roles)
        )
        # The proposer's own acceptance is implied by proposing; everyone else must agree.
        for role in roles[1:]:
            todos.accept_todo(conn, ag[role], 1)
    return ag


def _valid_spec() -> dict:
    """A valid `http` spec — and note what is NOT in it. The deployment target is
    host-owned configuration on the task/todo, so a spec carrying a `staging_url` is
    REFUSED (see the block at the bottom of this file). Tests that need a resolvable
    target set it with :func:`_target`."""
    return {
        "version": 1,
        "endpoints": [{"method": "POST", "path": "/api/auth/login"}],
    }


def _target(conn, url, task="signin"):
    """Set the HOST's deployment target on a task, the way `sys-buddy task staging-url`
    does. Written straight to the row here so the helper stays usable in local mode and
    on tasks these tests seed by hand."""
    conn.execute("UPDATE tasks SET staging_url = ? WHERE id = ?", (url, task))
    conn.commit()


def _task_state(conn, task="signin") -> str:
    return conn.execute("SELECT state FROM tasks WHERE id = ?", (task,)).fetchone()["state"]


def _strikes(conn, task="signin") -> int:
    return conn.execute("SELECT strikes FROM tasks WHERE id = ?", (task,)).fetchone()["strikes"]


def _todo(conn, task="signin", number=1):
    return conn.execute(
        "SELECT * FROM todos WHERE task_id = ? AND number = ?", (task, number)
    ).fetchone()


def _todo_state(conn, task="signin", number=1) -> str:
    return _todo(conn, task, number)["state"]


def _todo_stuck(conn, task="signin", number=1) -> bool:
    """A stuck TODO is FLAGGED, not moved to a `stuck` state.

    Deliberate asymmetry with tasks: the todo keeps the state it actually reached, so the
    rollup can still say how far it got, and one stalled deliverable does not brick the
    task. `stuck` as a *state* would throw that progress away.
    """
    return _todo(conn, task, number)["stuck_at"] is not None


def _todo_strikes(conn, task="signin", number=1) -> int:
    """Strikes live on the TODO, not the task.

    Every status report names a deliverable now, so the ping-pong counter it increments is
    that deliverable's. A stuck todo must not brick the task the way a task-level `stuck`
    did — the other deliverables keep marching — so the task's own `strikes`/`stuck` stay
    where they were and these assertions moved down a level with the behaviour.
    """
    return _todo(conn, task, number)["strikes"]


def _lock_all(conn, ag, version=1):
    """Have every role sign the given version so it locks."""
    for ident in ag.values():
        result = state.lock_contract(conn, ident, version, 1)
    return result


def _to_backend_live(conn, ag, spec=None):
    """Drive a task through propose → lock (all) → deploy so it is backend_live."""
    state.propose_contract(conn, ag["backend"], spec or _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    return state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "deployed to staging", 1)


# --- propose / validation ---------------------------------------------------
def test_propose_valid_contract_moves_to_proposed(conn):
    ag = _agents(conn)
    result = state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    assert result == {
        "version": 1,
        "state": state.CONTRACT_PROPOSED,
        # The reply names the deliverable and its signatory set, so the proposer never has
        # to guess which `todo=N` to pass next or who it is waiting on.
        "todo": 1,
        "todo_state": state.CONTRACT_PROPOSED,
        "signatories": ["backend", "frontend"],
    }
    assert _task_state(conn) == state.CONTRACT_PROPOSED


def test_propose_invalid_contract_raises_with_errors(conn):
    ag = _agents(conn)  # everyone has passed pre-flight, so only the spec is in play
    bad = {"version": 1, "endpoints": [{"method": "FOO", "path": "/x"}]}
    with pytest.raises(ValueError, match="FOO"):
        state.propose_contract(conn, ag["backend"], bad, 1)
    assert _task_state(conn) == state.OPEN  # no transition on invalid


def test_a_spec_carrying_a_staging_url_is_refused(conn):
    """The target is HOST-owned. Refused, not silently dropped: dropping it would let a
    prompt-injected "test against evil.com" appear to succeed, leaving the agent
    believing the contract points there."""
    ag = _agents(conn)
    spec = {**_valid_spec(), "staging_url": "https://evil.example.com"}
    with pytest.raises(ValueError, match="not yours to set"):
        state.propose_contract(conn, ag["backend"], spec, 1)
    assert _task_state(conn) == state.OPEN
    # And nothing of it reached the row.
    assert conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"] == 0


def test_propose_needs_no_target_at_all(conn):
    """A proposal is a SHAPE. It validates with no target configured anywhere — the
    host may not have set one yet, and that must not block agreeing on the interface."""
    ag = _agents(conn)
    result = state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    assert result["state"] == state.CONTRACT_PROPOSED


def test_propose_blocked_until_every_party_passes_preflight_remote(conn):
    """Both seats here ARE parties to todo #1, so the gate still bites.

    Scoping it to the party list (see ``test_todos_state``) narrowed WHO is asked, not
    whether anyone is: a contract agreed with an agent that never proved it understands
    the protocol is worthless.
    """
    from sys_buddy.config import Config, get_config, set_config
    set_config(Config(mode="remote", db_path=get_config().db_path))
    ag = _agents(conn)
    # The frontend's pre-flight lapses — the state a seat comes back in after a revoke
    # and re-pair, and the only way to reach it once `propose_todo` refuses to bind an
    # unready seat at all.
    conn.execute(
        "UPDATE agents SET ready = 0, readiness_status = 'pending' WHERE id = ?",
        (ag["frontend"].agent_id,),
    )
    conn.commit()
    with pytest.raises(ValueError, match="pre-flight"):
        state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    # Once both pass, it goes through.
    conn.execute("UPDATE agents SET ready = 1 WHERE id = ?", (ag["frontend"].agent_id,))
    conn.commit()
    result = state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    assert result["state"] == state.CONTRACT_PROPOSED


def test_reopen_negotiations_drops_locked_task_back(conn):
    ag = _agents(conn)
    _to_backend_live(conn, ag)  # propose → lock → deploy (backend_live)
    assert _task_state(conn) == state.BACKEND_LIVE
    result = state.reopen_negotiations(conn, ag["frontend"], "need a new field on /login", 1)
    assert result["state"] == state.CONTRACT_PROPOSED
    assert _task_state(conn) == state.CONTRACT_PROPOSED
    # The previously-locked contract still serves as the working blueprint.
    assert state.get_contract(conn, "signin", 1)["exists"] is True


def test_reopen_negotiations_rejected_before_any_lock(conn):
    ag = _agents(conn)
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)  # proposed, not locked
    with pytest.raises(ValueError, match="nothing to reopen"):
        state.reopen_negotiations(conn, ag["frontend"], "too soon", 1)


def test_reproposal_increments_version_and_reopens(conn):
    ag = _agents(conn)
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    assert _task_state(conn) == state.CONTRACT_LOCKED
    # A v2 proposal from a later state reopens negotiation.
    result = state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    assert result["version"] == 2
    assert _task_state(conn) == state.CONTRACT_PROPOSED


# --- lock requires ALL roles ------------------------------------------------
def test_lock_requires_all_roles_two_of_three_is_not_locked(conn):
    ag = _agents(conn, roles=("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)

    r1 = state.lock_contract(conn, ag["backend"], 1, 1)
    r2 = state.lock_contract(conn, ag["frontend"], 1, 1)

    assert r1["locked"] is False and r2["locked"] is False
    assert set(r2["signed"]) == {"backend", "frontend"}
    assert r2["remaining"] == ["mobile"]
    assert _task_state(conn) == state.CONTRACT_PROPOSED  # still not locked

    r3 = state.lock_contract(conn, ag["mobile"], 1, 1)
    assert r3["locked"] is True
    assert _task_state(conn) == state.CONTRACT_LOCKED


def test_lock_is_idempotent_per_agent(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    state.lock_contract(conn, ag["backend"], 1, 1)
    r = state.lock_contract(conn, ag["backend"], 1, 1)  # sign twice
    assert r["signed"] == ["backend"]  # not double-counted


def test_relocking_locked_contract_is_rejected(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    with pytest.raises(ValueError, match="immutable"):
        state.lock_contract(conn, ag["backend"], 1, 1)


def test_signing_with_nothing_proposed_names_the_missing_proposal(conn):
    """The `sign`-before-`pc` stall, fixed at the highest-attention surface.

    Observed live: a human said "lock the contract" where nothing had been proposed. The
    agent was right that there was nothing to sign, but "no contract version 1" told it
    only what was WRONG, so it went back to its human — three rounds for one move. The
    error now names the move that is actually missing (the PROPOSAL), says the agent may
    supply it, and says how (a stated assumption), because errors are where an agent is
    paying most attention.
    """
    ag = _agents(conn, roles=("backend", "frontend"))
    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], 1, 1)
    msg = str(e.value)
    low = msg.lower()
    assert "nothing to sign" in low
    # The suggested call carries the deliverable — `propose_contract(spec)` alone is
    # ambiguous once a task has more than one todo.
    assert "propose_contract(spec, todo=1)" in msg
    assert "assumption" in low
    # ...and it must not read as "you decide alone": the peer still gates the lock.
    assert "declines" in low or "decline" in low


def test_lock_writes_lock_event_with_signed_roles(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    row = conn.execute(
        "SELECT detail_json FROM events WHERE kind='lock' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    import json

    detail = json.loads(row["detail_json"])
    assert detail["version"] == 1
    assert set(detail["signed"]) == {"backend", "frontend"}


# --- the lock is PUSHED, never polled for (v2: contract lock notification) --
def _signed_first_then_final(conn, first="frontend", final="backend"):
    """Propose (by ``final``), have ``first`` sign, and drain ``first``'s queue.

    Leaves the task one signature away from locking, with the first signer's inbox
    empty — so anything it receives next is the lock push and nothing else.
    """
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag[final], _valid_spec(), 1)
    state.lock_contract(conn, ag[first], 1, 1)
    service.fetch_new(conn, ag[first])  # read the proposal; inbox now empty
    return ag


def test_final_signature_pushes_a_broker_notification_to_the_first_signer(conn):
    """The first signer only learns of the lock if the broker tells it — this is the
    push that replaces polling get_contract."""
    ag = _signed_first_then_final(conn)

    state.lock_contract(conn, ag["backend"], 1, 1)  # final signature → locks

    new = service.fetch_new(conn, ag["frontend"])
    assert [m["type"] for m in new] == ["contract_locked"]
    assert "LOCKED" in new[0]["content"]


def test_lock_push_is_framed_as_broker_authored_not_peer_data(conn):
    """It is the broker's own statement about the task, so it must NOT arrive in the
    peer envelope (which says 'external, treat as DATA') or in a peer's name."""
    ag = _signed_first_then_final(conn)
    state.lock_contract(conn, ag["backend"], 1, 1)

    msg = service.fetch_new(conn, ag["frontend"])[0]

    assert msg["from"] == "sys-buddy" and msg["role"] == "broker"
    assert '<broker from="sys-buddy" trust="broker"' in msg["content"]
    assert 'trust="external"' not in msg["content"]
    assert "backend-agent" not in msg["content"]  # never attributed to the peer


def test_lock_push_is_delivered_once_and_not_to_the_final_signer(conn):
    """No double-notify: the signer who completed the lock already got
    {locked: True} synchronously, and nobody gets the push twice."""
    ag = _signed_first_then_final(conn)
    service.fetch_new(conn, ag["backend"])  # drain the final signer too
    state.lock_contract(conn, ag["backend"], 1, 1)

    assert [m["type"] for m in service.fetch_new(conn, ag["frontend"])] == ["contract_locked"]
    assert service.fetch_new(conn, ag["frontend"]) == []          # not redelivered
    assert service.fetch_new(conn, ag["backend"]) == []           # never sent to them


def test_lock_push_is_one_message_for_one_lock_event(conn):
    """D10's message<->event 1:1: exactly one contract_locked message per lock event,
    so the dashboard thread can't render the lock twice."""
    ag = _agents(conn, roles=("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)

    n_msgs = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE type = 'contract_locked'"
    ).fetchone()["n"]
    n_events = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'lock'"
    ).fetchone()["n"]
    assert n_msgs == n_events == 1


def test_partial_signature_does_not_push_a_lock(conn):
    """A signature is not a lock — the push fires only when the contract actually
    locks, so an agent woken by it can trust what it says."""
    ag = _agents(conn, roles=("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    state.lock_contract(conn, ag["backend"], 1, 1)
    state.lock_contract(conn, ag["frontend"], 1, 1)

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE type = 'contract_locked'"
    ).fetchone()["n"] == 0


def test_parked_wait_for_message_wakes_on_the_lock(conn, monkeypatch):
    """The whole point: an agent asleep in wait_for_message is woken BY the lock.

    Before this existed, the lock was only an event + a synchronous return value, so a
    parked first signer slept straight through the lock it was waiting for.
    """
    import asyncio

    from sys_buddy import tools

    ag = _signed_first_then_final(conn)
    monkeypatch.setattr(tools, "POLL_INTERVAL", 0.05)  # keep the test quick

    async def scenario():
        waiter = asyncio.create_task(tools._op_wait(ag["frontend"], timeout_seconds=5))
        await asyncio.sleep(0.2)
        assert not waiter.done()  # genuinely parked: nothing has happened yet
        state.lock_contract(conn, ag["backend"], 1, 1)  # the final signature lands
        return await asyncio.wait_for(waiter, 5)

    woken_with = asyncio.run(scenario())

    assert [m["type"] for m in woken_with] == ["contract_locked"]
    assert state.get_contract(conn, "signin", 1)["locked"] is True


# --- get_contract: staging_url from the contract, not chat ------------------
def test_get_contract_returns_locked_staging_url(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://api-staging.example.com")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    c = state.get_contract(conn, "signin", 1)
    assert c["exists"] is True
    assert c["staging_url"] == "https://api-staging.example.com"
    # …and the historical record of what was live at the moment it locked.
    assert c["staging_url_at_lock"] == "https://api-staging.example.com"


def test_re_pointing_the_target_moves_no_contract_and_no_signature(conn):
    """The defect this feature exists for: an ngrok URL rotates on a tunnel restart, and
    under the old rules the only sanctioned fix was to renegotiate a shape that had not
    changed."""
    import json as _json

    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://old-tunnel.ngrok-free.dev")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    before = conn.execute(
        "SELECT version, spec_json, locked_at, staging_url_at_lock FROM contracts"
    ).fetchall()
    sigs_before = conn.execute("SELECT COUNT(*) AS n FROM contract_signatures").fetchone()["n"]

    admin.set_staging_url("signin", "https://new-tunnel.ngrok-free.dev")

    c = state.get_contract(conn, "signin", 1)
    assert c["staging_url"] == "https://new-tunnel.ngrok-free.dev"   # live
    assert c["staging_url_at_lock"] == "https://old-tunnel.ngrok-free.dev"  # agreed
    after = conn.execute(
        "SELECT version, spec_json, locked_at, staging_url_at_lock FROM contracts"
    ).fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM contract_signatures"
    ).fetchone()["n"] == sigs_before
    # No new version was minted either.
    assert len(after) == 1
    assert "staging_url" not in _json.loads(after[0]["spec_json"])


def test_a_todo_override_beats_the_task_target(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://task-wide.example.com")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    admin.set_staging_url("signin", "https://just-this-one.example.com", todo=1)
    assert state.get_contract(conn, "signin", 1)["staging_url"] == (
        "https://just-this-one.example.com"
    )
    # Clearing the override falls back to the task's, rather than to nothing.
    admin.set_staging_url("signin", None, todo=1)
    assert state.get_contract(conn, "signin", 1)["staging_url"] == "https://task-wide.example.com"


def test_a_legacy_contract_keeps_resolving_from_its_own_spec(conn):
    """Back-compat: a contract signed before the target moved out of the spec still
    answers, without its `spec_json` being rewritten."""
    import json as _json

    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    legacy = {**_valid_spec(), "staging_url": "https://legacy.example.com"}
    conn.execute("UPDATE contracts SET spec_json = ?", (_json.dumps(legacy),))
    conn.commit()
    assert state.get_contract(conn, "signin", 1)["staging_url"] == "https://legacy.example.com"
    # …and the host's value OUTRANKS it, which is the whole point: the owner hit a
    # locked contract carrying a URL that could never connect.
    _target(conn, "https://the-host-fixed-it.example.com")
    assert state.get_contract(conn, "signin", 1)["staging_url"] == (
        "https://the-host-fixed-it.example.com"
    )


def test_get_contract_shows_proposal_before_lock_without_staging_url(conn):
    # A proposed-but-unlocked contract is REVIEWABLE via get_contract (so an assessor
    # isn't told to review something that reads exists:false) — but its staging_url is
    # withheld until it locks, so no unsigned URL is ever fetchable (rule 2).
    ag = _agents(conn, roles=("backend", "frontend"))
    _target(conn, "https://api-staging.example.com")   # configured, and STILL withheld
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    c = state.get_contract(conn, "signin", 1)
    assert c["exists"] is True
    assert c["status"] == "proposed"
    assert c["locked"] is False
    assert c["staging_url"] is None
    assert "staging_url" not in c["spec"]          # stripped from the shape too
    assert c["spec"]["endpoints"]                  # but the shape IS visible
    assert c["awaiting"] == ["backend", "frontend"]


def test_get_contract_absent_with_no_contract_at_all(conn):
    _agents(conn, roles=("backend", "frontend"))
    absent = state.get_contract(conn, "signin", 1)
    assert absent["exists"] is False
    # Not a bare flag: the todo exists, so the reply says what is missing and which call
    # supplies it. A bare {"exists": False} left the agent to infer the next move.
    assert "propose_contract(spec, todo=1)" in absent["note"]


def test_get_contract_reflects_partial_signatures(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    state.lock_contract(conn, ag["backend"], version=1, number=1)   # one of two signs
    c = state.get_contract(conn, "signin", 1)
    assert c["status"] == "proposed" and c["locked"] is False
    assert c["signatures"] == ["backend"]
    assert c["awaiting"] == ["frontend"]
    assert c["staging_url"] is None                       # still withheld


# --- deploy gating ----------------------------------------------------------
def test_deploy_rejected_with_unsigned_proposal(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)  # proposed, not locked
    with pytest.raises(ValueError, match="cannot report 'ready'"):
        state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "go", 1)


def test_deploy_rejected_from_open_with_no_contract(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    with pytest.raises(ValueError, match="no locked contract"):
        state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "go", 1)


def test_deploy_rejected_mid_renegotiation(conn):
    """Once live, proposing v2 reopens negotiation; the backend must not be able to
    deploy again until all roles re-sign the new version (regression: review #4)."""
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)                                   # v1 locked + deployed
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)   # v2 draft, unsigned
    assert _task_state(conn) == state.CONTRACT_PROPOSED
    with pytest.raises(ValueError, match="awaiting signatures"):
        state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "sneaky redeploy", 1)


def test_only_producer_can_report_ready(conn):
    # backend PROPOSES the contract → backend is the producer (model B); frontend can't report ready.
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)
    with pytest.raises(ValueError, match="proposed the contract"):
        state.report_status(conn, ag["frontend"], state.STATUS_DEPLOYED, "sneaky", 1)


def test_deploy_moves_to_backend_live_and_posts_message(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    r = _to_backend_live(conn, ag)
    assert r["state"] == state.BACKEND_LIVE
    # deploy_confirmed message is visible to the other agent (dashboard thread)
    inbox = service.fetch_unacked(conn, ag["frontend"])
    assert any(m["type"] == "deploy_confirmed" for m in inbox)


# --- test gating & roles ----------------------------------------------------
def test_test_rejected_before_backend_live(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)  # contract_locked, not yet live
    with pytest.raises(ValueError, match="before its producer is ready"):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "green", 1)


def test_producer_cannot_report_checks(conn):
    # backend proposed → backend is the producer, so it can't report its own checks.
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    with pytest.raises(ValueError, match="doesn't report checks"):
        state.report_status(conn, ag["backend"], state.STATUS_TEST_PASSED, "green", 1)


def test_first_test_moves_to_testing(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "green", 1)
    assert r["state"] == state.TESTING


# --- strikes ----------------------------------------------------------------
def test_three_strikes_forces_stuck_and_refuses_more_tests(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    for _ in range(3):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red", 1)
    # The TODO is flagged stuck; the task is not bricked by one stalled deliverable,
    # and the todo keeps `testing` so the rollup still shows how far it got.
    assert _todo_stuck(conn) is True
    assert _todo_state(conn) == state.TESTING
    assert _todo_strikes(conn) == 3
    # further test cycles are refused, and the refusal is scoped to this deliverable:
    # the other todos on the task are explicitly unaffected.
    with pytest.raises(ValueError, match="pulled the cord") as e:
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red again", 1)
    assert "other todos on this task are unaffected" in str(e.value)


def test_each_fail_increments_strike_and_writes_test_event(conn):
    import json

    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red", 1)
    assert _todo_strikes(conn) == 1
    row = conn.execute(
        "SELECT detail_json FROM events WHERE kind='test' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    detail = json.loads(row["detail_json"])
    # `todo_id` rides along so the dashboard can attribute the strike to a deliverable.
    assert detail == {"pass": False, "strike": 1, "todo_id": 1}


def test_new_version_deploy_resets_strikes(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red", 1)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red", 1)
    assert _todo_strikes(conn) == 2

    # Renegotiate: propose v2, all re-sign, backend redeploys the new version.
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)  # v2
    _lock_all(conn, ag, version=2)
    r = state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "v2 live", 1)
    assert r["strikes"] == 0  # fresh attempt, not the same loop


def test_same_contract_redeploy_keeps_strikes(conn):
    """Redeploying the SAME locked contract is the same fix loop — strikes persist."""
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_FAILED, "red", 1)
    r = state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "fixed, redeploy", 1)
    assert r["strikes"] == 1  # not reset


# --- terminal states --------------------------------------------------------
def test_verified_is_terminal(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "green", 1)
    r = state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "e2e green", 1)
    assert r["state"] == state.VERIFIED
    # Terminal at the DELIVERABLE level: finished work is not re-contracted, you propose a
    # new todo for the follow-up.
    with pytest.raises(ValueError, match="already verified"):
        state.propose_contract(conn, ag["backend"], _valid_spec(), 1)


def test_voluntary_stuck_flags_the_todo_without_bricking_the_task(conn):
    """A todo-scoped `stuck` is a FLAG, and — unlike a task-level one — not terminal.

    Worth stating explicitly, because it is an asymmetry the move to todos introduced. A
    task-level `stuck` moved the task INTO a terminal `stuck` state, so nothing more could
    be reported. A todo keeps the state it reached and only raises `stuck_at`, which
    nothing reads as a guard — so further reports on a stuck deliverable still land. The
    three-strike path, by contrast, DOES refuse (see
    `test_three_strikes_forces_stuck_and_refuses_more_tests`).

    Asserting today's behaviour rather than the behaviour the old name implied; whether a
    voluntarily-stuck todo should also be closed to reports is a product call.
    """
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_STUCK, "giving up", 1)
    assert _todo_stuck(conn) is True
    # The TASK is not dragged down by one stalled deliverable.
    assert r["state"] == state.BACKEND_LIVE
    assert _task_state(conn) == state.BACKEND_LIVE


def test_transition_event_shape_for_times_map(conn):
    """The API derives times[state] from transition events; assert the shape."""
    import json

    ag = _agents(conn, roles=("backend", "frontend"))
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    row = conn.execute(
        "SELECT detail_json FROM events WHERE kind='transition' ORDER BY id LIMIT 1"
    ).fetchone()
    assert json.loads(row["detail_json"]) == {"from": "open", "to": "contract_proposed"}


# --- task-agnostic status aliases -------------------------------------------
# 'ready'/'checked'/'blocked' are pure aliases of 'deployed'/'test_passed'/
# 'test_failed'; each must produce identical behavior to the word it stands for.
def _to_locked(conn, ag):
    """Drive a task through propose → lock (all) so it is ready to deploy."""
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)


def test_ready_is_alias_of_deployed(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_locked(conn, ag)
    r = state.report_status(conn, ag["backend"], state.STATUS_READY, "part ready", 1)
    assert r["status"] == state.STATUS_DEPLOYED
    assert r["state"] == "backend_live"
    assert r["strikes"] == 0
    assert _task_state(conn) == "backend_live"


def test_checked_is_alias_of_test_passed(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_CHECKED, "works", 1)
    assert r["status"] == state.STATUS_TEST_PASSED
    assert r["state"] == "testing"
    assert r["strikes"] == 0
    assert _todo_strikes(conn) == 0


def test_blocked_is_alias_of_test_failed_and_strikes(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    r = state.report_status(conn, ag["frontend"], state.STATUS_BLOCKED, "broken", 1)
    assert r["status"] == state.STATUS_TEST_FAILED
    assert r["state"] == "testing"
    assert r["strikes"] == 1
    assert _todo_strikes(conn) == 1  # same strike increment as 'test_failed'


def test_blocked_three_times_forces_stuck_like_test_failed(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_backend_live(conn, ag)
    for _ in range(3):
        state.report_status(conn, ag["frontend"], state.STATUS_BLOCKED, "red", 1)
    assert _todo_stuck(conn) is True
    assert _todo_strikes(conn) == 3


def test_new_word_and_old_word_reach_identical_state(conn):
    """A task driven entirely with ready/checked ends where deployed/test_passed would."""
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_locked(conn, ag)
    state.report_status(conn, ag["backend"], state.STATUS_READY, "ready", 1)
    state.report_status(conn, ag["frontend"], state.STATUS_CHECKED, "works", 1)
    r = state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "done", 1)
    assert r["status"] == state.STATUS_VERIFIED
    assert _task_state(conn) == "verified"


def test_unknown_status_message_lists_new_vocabulary(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    _to_locked(conn, ag)
    with pytest.raises(ValueError) as exc:
        state.report_status(conn, ag["backend"], "bogus", "x", 1)
    msg = str(exc.value)
    for word in ("ready", "checked", "blocked", "verified", "stuck"):
        assert word in msg


# --- model B: producer = whoever proposes (no hardcoded 'backend') ----------
def test_non_backend_producer_full_flow(conn):
    """A contract with NO 'backend' role: the role that PROPOSES is the producer.
    Here frontend proposes → frontend reports `ready`; mobile (the consumer) checks."""
    ag = _agents(conn, roles=("frontend", "mobile"))
    # frontend proposes → becomes the producer
    state.propose_contract(conn, ag["frontend"], _valid_spec(), 1)
    _lock_all(conn, ag, version=1)

    # the NON-proposer (mobile) may not report ready
    with pytest.raises(ValueError, match="proposed the contract"):
        state.report_status(conn, ag["mobile"], state.STATUS_READY, "nope", 1)

    # the producer (frontend) reports ready → backend_live
    r = state.report_status(conn, ag["frontend"], state.STATUS_READY, "my part is up", 1)
    assert r["state"] == state.BACKEND_LIVE

    # the producer can't check its own work; the consumer (mobile) can
    with pytest.raises(ValueError, match="doesn't report checks"):
        state.report_status(conn, ag["frontend"], state.STATUS_CHECKED, "self-check", 1)
    r = state.report_status(conn, ag["mobile"], state.STATUS_CHECKED, "works against frontend", 1)
    assert r["state"] == state.TESTING

    r = state.report_status(conn, ag["mobile"], state.STATUS_VERIFIED, "all good", 1)
    assert r["state"] == state.VERIFIED


# --- staging_url strictness keys on CONNECTIVITY, not the broker's auth mode --
# The GUI always runs the broker in remote mode (token auth needs it), so a
# same-machine task must still be able to name http://localhost:PORT.
#
# These rules did not change — their DOOR did. The target used to arrive inside an
# agent's spec and was checked by `propose_contract`; it is now host-owned, so the
# identical checks run where the HOST writes it (`admin.set_staging_url`). That is a
# stronger posture, not a weaker one: there is no longer any field an agent can put a
# URL into, so there is nothing left for an injected "test against evil.com" to aim at.
def _remote_mode(conn):
    from sys_buddy.config import Config, get_config, set_config
    set_config(Config(mode="remote", db_path=get_config().db_path))


def _preflight_passed(conn, ag):
    for ident in ag.values():
        conn.execute("UPDATE agents SET ready = 1 WHERE id = ?", (ident.agent_id,))
    conn.commit()


def _mark(conn, task="signin", *, same_machine=None, staging_url=None):
    if same_machine is not None:
        conn.execute(
            "UPDATE tasks SET same_machine = ? WHERE id = ?", (1 if same_machine else 0, task)
        )
    if staging_url is not None:
        conn.execute("UPDATE tasks SET staging_url = ? WHERE id = ?", (staging_url, task))
    conn.commit()


def test_a_same_machine_task_accepts_localhost_in_remote_mode(conn):
    _remote_mode(conn)
    _agents(conn)
    _mark(conn, same_machine=True)
    res = admin.set_staging_url("signin", "http://localhost:3000")
    assert res["effective"] == "http://localhost:3000"


@pytest.mark.parametrize("url", [
    "http://localhost:3000",
    "http://api-staging.example.com",
    "https://127.0.0.1/admin",
    "https://169.254.169.254/latest/meta-data/",
    "https://10.0.0.5/api",
])
def test_a_remote_task_still_refuses_localhost_and_ssrf_targets(conn, url):
    """REGRESSION: a task with a real tunnel/public origin (same_machine = 0) keeps
    the full https + SSRF rules — nothing about the lenient path leaks into it."""
    _remote_mode(conn)
    _agents(conn)
    _mark(conn, same_machine=False)
    with pytest.raises(ValueError, match="staging_url"):
        admin.set_staging_url("signin", url)
    # …and the refusal leaves the row alone.
    assert admin.get_staging_url("signin")["effective"] is None


def test_same_machine_flag_is_ignored_when_broker_has_a_public_origin(conn):
    """Defence in depth: even a same_machine=1 row stays strict while THIS process is
    reachable at a public origin — a peer may well be off-box."""
    from sys_buddy.config import Config, get_config, set_config
    set_config(Config(
        mode="remote", db_path=get_config().db_path, public_url="https://abc.ngrok-free.app"
    ))
    _agents(conn)
    _mark(conn, same_machine=True)
    with pytest.raises(ValueError, match="staging_url"):
        admin.set_staging_url("signin", "http://localhost:3000")


def test_task_defaults_to_strict_when_nothing_declared(conn):
    """A task created by any path that doesn't declare connectivity is strict."""
    _remote_mode(conn)
    _agents(conn)  # seeded without touching same_machine
    with pytest.raises(ValueError, match="staging_url"):
        admin.set_staging_url("signin", "http://localhost:3000")


# --- the host owns the target; the contract records what it was at lock ------
def _contract_spec(conn, task="signin", version=1) -> dict:
    row = conn.execute(
        "SELECT spec_json FROM contracts WHERE task_id = ? AND version = ?", (task, version)
    ).fetchone()
    import json as _json
    return _json.loads(row["spec_json"])


def test_nothing_is_injected_into_the_spec_any_more(conn):
    """The proposal used to inherit the task target INTO `spec_json`. It no longer does:
    the signed document holds the shape and only the shape."""
    ag = _agents(conn)
    _mark(conn, same_machine=True, staging_url="http://localhost:4321")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    assert "staging_url" not in _contract_spec(conn)
    # It still RESOLVES, from the host's row, once the contract locks.
    _lock_all(conn, ag, version=1)
    assert state.get_contract(conn, "signin", 1)["staging_url"] == "http://localhost:4321"


def test_the_lock_records_the_target_that_was_live_at_signing(conn):
    ag = _agents(conn)
    _mark(conn, staging_url="https://at-signing.example.com")
    state.propose_contract(conn, ag["backend"], _valid_spec(), 1)
    assert conn.execute(
        "SELECT staging_url_at_lock FROM contracts"
    ).fetchone()["staging_url_at_lock"] is None      # a draft agreed to no target
    _lock_all(conn, ag, version=1)
    assert conn.execute(
        "SELECT staging_url_at_lock FROM contracts"
    ).fetchone()["staging_url_at_lock"] == "https://at-signing.example.com"


def test_changing_the_target_writes_an_event_the_humans_can_read(conn):
    _agents(conn)
    _mark(conn, staging_url="https://before.example.com")
    admin.set_staging_url("signin", "https://after.example.com")
    import json as _json
    texts = [
        _json.loads(r["detail_json"]).get("text", "")
        for r in conn.execute(
            "SELECT detail_json FROM events WHERE task_id = 'signin' AND kind = 'task'"
        ).fetchall()
    ]
    assert any("after.example.com" in t and "before.example.com" in t for t in texts)
