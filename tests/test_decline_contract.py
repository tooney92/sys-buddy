"""Specs for declining a PROPOSED contract.

The gap this closes: a contract had `lock` and nothing else, so an unsigned proposal
meant EITHER "I read it and object" OR "I haven't looked". Indistinguishable to the
broker, so indistinguishable on the dashboard, so the humans had to hold it in their
heads. Todos already had accept AND decline; contracts were the odd one out.
"""

from __future__ import annotations

import pytest

from sys_buddy import service, state, tools
from tests.conftest import seed_agent, seed_task


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


SPEC = {"endpoints": [{"method": "POST", "path": "/auth/login"}], "staging_url": "http://x"}


def _proposed(conn, ag):
    state.propose_contract(conn, ag["backend"], SPEC)
    return state._newest_contract(conn, "signin")


def test_decline_marks_the_version_declined(conn):
    ag = _task(conn)
    c = _proposed(conn, ag)

    res = state.decline_contract(conn, ag["frontend"], "the login path is wrong")

    assert res["status"] == "declined"
    assert res["declined_by"] == "frontend"
    assert state._newest_contract(conn, "signin")["status"] == "declined"


def test_a_declined_version_cannot_be_signed(conn):
    """The whole point of 'sticky': the answer is a NEW version, not this one."""
    ag = _task(conn)
    c = _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "wrong shape")

    with pytest.raises(ValueError) as e:
        state.lock_contract(conn, ag["backend"], c["version"])
    # the error must TEACH the next move, not just refuse
    assert "propose_contract" in str(e.value)


def test_declining_voids_signatures_already_on_that_version(conn):
    """A part-signed dead version would read as part-agreed. It isn't."""
    ag = _task(conn)
    c = _proposed(conn, ag)
    state.lock_contract(conn, ag["backend"], c["version"])  # backend signs first
    assert state._signatures_for(conn, c["id"]) == ["backend"]

    state.decline_contract(conn, ag["frontend"], "no")

    assert state._signatures_for(conn, c["id"]) == []


def test_a_new_version_supersedes_the_decline(conn):
    ag = _task(conn)
    _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "needs an email field")

    state.propose_contract(conn, ag["backend"], SPEC)
    newest = state._newest_contract(conn, "signin")

    assert newest["status"] != "declined"
    state.lock_contract(conn, ag["backend"], newest["version"])
    state.lock_contract(conn, ag["frontend"], newest["version"])
    assert state._newest_contract(conn, "signin")["status"] == "locked"


def test_decline_requires_a_reason(conn):
    ag = _task(conn)
    _proposed(conn, ag)
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            state.decline_contract(conn, ag["frontend"], blank)


def test_cannot_decline_when_nothing_is_proposed(conn):
    ag = _task(conn)
    with pytest.raises(ValueError) as e:
        state.decline_contract(conn, ag["frontend"], "no")
    assert "no contract has been proposed" in str(e.value).lower()


def test_cannot_decline_a_locked_contract(conn):
    """That's what reopen_negotiations is for — and the error must say so."""
    ag = _task(conn)
    c = _proposed(conn, ag)
    state.lock_contract(conn, ag["backend"], c["version"])
    state.lock_contract(conn, ag["frontend"], c["version"])

    with pytest.raises(ValueError) as e:
        state.decline_contract(conn, ag["frontend"], "changed my mind")
    assert "reopen_negotiations" in str(e.value)


def test_cannot_decline_twice(conn):
    ag = _task(conn)
    _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "first")
    with pytest.raises(ValueError):
        state.decline_contract(conn, ag["backend"], "second")


def test_the_proposer_may_decline_their_own_proposal(conn):
    """Spotting your own mistake before a peer wastes time reviewing it is a feature."""
    ag = _task(conn)
    _proposed(conn, ag)
    res = state.decline_contract(conn, ag["backend"], "I got the path wrong")
    assert res["declined_by"] == "backend"


def test_the_objection_reaches_the_peer_and_the_log(conn):
    """A decline nobody can see is the bug we're fixing."""
    ag = _task(conn)
    _proposed(conn, ag)
    state.decline_contract(conn, ag["frontend"], "the email field is missing")

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
    tid = t["id"]
    tools._op_accept_todo(ag["frontend"], tid)
    state.propose_contract(conn, ag["backend"], SPEC, tid)

    before = state.next_step(conn, "signin", tid)
    assert before["stage"] == "contract_proposed"
    assert "sign" in before["cmd"]

    state.decline_contract(conn, ag["frontend"], "wrong verb", tid)
    after = state.next_step(conn, "signin", tid)

    assert after["stage"] == "contract_declined"
    assert after["cmd"] == f"pc #{tid}"
    assert "frontend" in after["text"]
    assert "wrong verb" in after["text"]


def test_a_non_party_cannot_decline_a_todo_contract(conn):
    """Mirror of 'a non-party does not block the lock' — no say in a shape that
    doesn't bind you."""
    ag = _task(conn, roles=("backend", "frontend", "mobile"))
    t = tools._op_propose_todo(ag["backend"], "Sign-in", "scope", ["backend", "frontend"])
    tid = t["id"]
    tools._op_accept_todo(ag["frontend"], tid)
    state.propose_contract(conn, ag["backend"], SPEC, tid)

    with pytest.raises(ValueError):
        state.decline_contract(conn, ag["mobile"], "I don't like it", tid)
