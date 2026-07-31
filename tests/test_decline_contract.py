"""Specs for declining a PROPOSED contract.

The gap this closes: a contract had `lock` and nothing else, so an unsigned proposal
meant EITHER "I read it and object" OR "I haven't looked". Indistinguishable to the
broker, so indistinguishable on the dashboard, so the humans had to hold it in their
heads. Todos already had accept AND decline; contracts were the odd one out.
"""

from __future__ import annotations

import pytest

from sys_buddy import service, state, todos, tools
from tests.conftest import seed_accepted_todo, seed_agent, seed_task


def _task(conn, task="signin", roles=("backend", "frontend")):
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


# No `staging_url`: the deployment target is host-owned configuration now, and a
# spec carrying one is refused (see tests/test_contracts.py).
SPEC = {"endpoints": [{"method": "POST", "path": "/auth/login"}]}

# Every contract belongs to a deliverable, so these specs all run on the same one: todo
# #1, accepted by both parties. The selector is threaded through every call below for
# that reason — not as ceremony, but because "the contract" names nothing without it.
NUM = 1


def _proposed(conn, ag):
    """An accepted todo #1 with a proposed contract on it — the only shape there is."""
    assert seed_accepted_todo(conn, ag) == NUM
    state.propose_contract(conn, ag["backend"], SPEC, NUM)
    return state._newest_contract(conn, "signin")


def test_decline_marks_the_version_declined(conn):
    ag = _task(conn)
    c = _proposed(conn, ag)

    res = state.decline_contract(conn, ag["frontend"], "the login path is wrong", NUM)

    assert res["status"] == "declined"
    assert res["declined_by"] == "frontend"
    assert state._newest_contract(conn, "signin")["status"] == "declined"


def test_a_declined_version_cannot_be_signed(conn):
    """The whole point of 'sticky': the answer is a NEW version, not this one."""
    ag = _task(conn)
    c = _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "wrong shape", NUM)

    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], c["version"], NUM)
    # the error must TEACH the next move, not just refuse
    assert "propose_contract" in str(e.value)


def test_declining_voids_signatures_already_on_that_version(conn):
    """A part-signed dead version would read as part-agreed. It isn't."""
    ag = _task(conn)
    c = _proposed(conn, ag)
    state.lock_contract(conn, ag["backend"], c["version"], NUM)  # backend signs first
    assert state._signatures_for(conn, c["id"]) == ["backend"]

    state.decline_contract(conn, ag["frontend"], "no", NUM)

    assert state._signatures_for(conn, c["id"]) == []


def test_a_new_version_supersedes_the_decline(conn):
    ag = _task(conn)
    _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "needs an email field", NUM)

    state.propose_contract(conn, ag["backend"], SPEC, NUM)
    newest = state._newest_contract(conn, "signin")

    assert newest["status"] != "declined"
    state.lock_contract(conn, ag["backend"], newest["version"], NUM)
    state.lock_contract(conn, ag["frontend"], newest["version"], NUM)
    assert state._newest_contract(conn, "signin")["status"] == "locked"


def test_decline_requires_a_reason(conn):
    ag = _task(conn)
    _proposed(conn, ag)
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            state.decline_contract(conn, ag["frontend"], blank, NUM)


def test_cannot_decline_when_nothing_is_proposed(conn):
    ag = _task(conn)
    with pytest.raises(ValueError) as e:
        state.decline_contract(conn, ag["frontend"], "no")
    assert "no contract has been proposed" in str(e.value).lower()


def test_cannot_decline_a_locked_contract(conn):
    """That's what reopen_negotiations is for — and the error must say so."""
    ag = _task(conn)
    c = _proposed(conn, ag)
    state.lock_contract(conn, ag["backend"], c["version"], NUM)
    state.lock_contract(conn, ag["frontend"], c["version"], NUM)

    with pytest.raises(ValueError) as e:
        state.decline_contract(conn, ag["frontend"], "changed my mind", NUM)
    assert "reopen_negotiations" in str(e.value)


def test_cannot_decline_twice(conn):
    ag = _task(conn)
    _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "first", NUM)
    with pytest.raises(ValueError):
        state.decline_contract(conn, ag["backend"], "second", NUM)


def test_the_proposer_may_decline_their_own_proposal(conn):
    """Spotting your own mistake before a peer wastes time reviewing it is a feature."""
    ag = _task(conn)
    _proposed(conn, ag)
    res = state.decline_contract(conn, ag["backend"], "I got the path wrong", NUM)
    assert res["declined_by"] == "backend"


def test_the_objection_reaches_the_peer_and_the_log(conn):
    """A decline nobody can see is the bug we're fixing."""
    ag = _task(conn)
    _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "the email field is missing", NUM)

    delivered = service.fetch_unacked(conn, ag["backend"])
    assert any("email field is missing" in m["content"] for m in delivered)

    events = conn.execute(
        "SELECT detail_json FROM events WHERE task_id = 'signin'"
    ).fetchall()
    assert any("email field is missing" in (r["detail_json"] or "") for r in events)


def test_next_step_stops_saying_waiting_to_sign(conn):
    """Before: 'waiting on frontend to sign' — a lie once frontend has objected."""
    ag = _task(conn, roles=("backend", "frontend"))
    t = tools._op_propose_todo(ag["backend"], "Sign-in", "POST /auth/login",
                               ["backend", "frontend"])
    # `num` is the agent's selector (todos.number); `tid` the internal key next_step
    # takes. Keeping them separate matters even where they happen to be equal — the
    # agent-facing reply no longer carries an id at all.
    num = t["number"]
    tid = todos.get_row(conn, "signin", num)["id"]
    tools._op_accept_todo(ag["frontend"], num)
    state.propose_contract(conn, ag["backend"], SPEC, num)

    before = state.next_step(conn, "signin", tid)
    assert before["stage"] == "contract_proposed"
    assert "sign" in before["cmd"]

    state.decline_contract(conn, ag["frontend"], "wrong verb", num)
    after = state.next_step(conn, "signin", tid)

    assert after["stage"] == "contract_declined"
    assert after["cmd"] == f"pc #{num}"
    assert "frontend" in after["text"]
    assert "wrong verb" in after["text"]


def test_a_non_party_cannot_decline_a_todo_contract(conn):
    """Mirror of 'a non-party does not block the lock' — no say in a shape that
    doesn't bind you."""
    ag = _task(conn, roles=("backend", "frontend", "mobile"))
    t = tools._op_propose_todo(ag["backend"], "Sign-in", "scope", ["backend", "frontend"])
    num = t["number"]
    tools._op_accept_todo(ag["frontend"], num)
    state.propose_contract(conn, ag["backend"], SPEC, num)

    with pytest.raises(ValueError):
        state.decline_contract(conn, ag["mobile"], "I don't like it", num)
