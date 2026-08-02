"""Specs for THE GATE: on commissioned work, nothing is built before the scope is agreed.

This is the load-bearing rule of engagement mode and the reason the mode exists. An
owner who cannot stop work cannot control scope, and every other guarantee in
`docs/enhancements.md` item 1 rests on this one holding.

Three claims, and the second is the one that would be easiest to get wrong:

* **todos and contracts are refused until the deliverable list locks** — an engagement
  with no agreed scope has nothing to build, exactly as a task with no todos has
  nothing to contract;
* **messaging is NOT gated.** Pushing back IS a conversation. Gate the talking and a
  dev who thinks deliverable #2 is unbuildable cannot say so, and the session deadlocks
  on the very discussion it exists to have;
* **peer sessions are untouched.** `contract` and `debug` tasks have no client, no
  list and no gate, and must behave exactly as they did before this feature.

The gate lives in the DOMAIN layer (`todos.propose_todo`, `state.propose_contract`) and
deliberately not in `middleware.ACTION_TOOLS`, whose readiness gate sits inside
`if cfg.is_remote:`. A rule placed there would silently not apply to `sys-buddy local` —
which is how this gets demoed, and how CLAUDE.md says to test. It would look enforced
and not be. `test_the_gate_holds_in_local_mode` pins that.
"""

from __future__ import annotations

import pytest

from sys_buddy import deliverables, service, state, todos
from sys_buddy.config import Config, set_config
from tests.conftest import seed_agent, seed_task


ROLES = ("owner", "backend", "frontend")


def _agents(conn, task="acme", roles=ROLES, mode="engagement"):
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


def _agree_the_scope(conn, ag, task="acme"):
    """Owner proposes, every builder accepts — the list locks and building may start."""
    deliverables.propose_deliverables(
        conn, ag["owner"], ["A landing page with 3 buttons", "A contact form that emails me"]
    )
    for seat in ("backend", "frontend"):
        deliverables.accept_deliverables(conn, ag[seat])
    assert deliverables.deliverables_locked(conn, task)


def _spec():
    return {"version": 1, "endpoints": [{"method": "POST", "path": "/api/contact"}]}


# --- the gate is shut until the scope is agreed ------------------------------
def test_no_todo_before_the_deliverables_are_agreed(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError) as e:
        todos.propose_todo(conn, ag["backend"], "Contact API", "scope", ["backend", "frontend"])
    msg = str(e.value).lower()
    assert "deliverable" in msg
    # The refusal must be ACTIONABLE, not merely a refusal: it names who is still
    # awaited, because otherwise a blocked team has no idea what to do next.
    assert "backend" in msg or "frontend" in msg or "accept" in msg


def test_no_todo_while_one_builder_has_not_accepted(conn):
    """A partly-accepted list is not an agreement. One holdout blocks the build."""
    ag = _agents(conn)
    deliverables.propose_deliverables(conn, ag["owner"], ["A landing page"])
    deliverables.accept_deliverables(conn, ag["backend"])
    assert not deliverables.deliverables_locked(conn, "acme")
    with pytest.raises(ValueError):
        todos.propose_todo(conn, ag["backend"], "Contact API", "scope", ["backend", "frontend"])


def test_a_push_back_keeps_the_gate_shut(conn):
    """The dev who says "three pages with bespoke components isn't feasible" is having
    exactly the conversation this gate exists to force — before anyone builds."""
    ag = _agents(conn)
    deliverables.propose_deliverables(conn, ag["owner"], ["A landing page", "A contact form"])
    deliverables.accept_deliverables(conn, ag["backend"])
    deliverables.push_back(conn, ag["frontend"], 2, "too vague to check")
    assert not deliverables.deliverables_locked(conn, "acme")
    with pytest.raises(ValueError):
        todos.propose_todo(conn, ag["backend"], "Contact API", "scope", ["backend", "frontend"])


def test_the_gate_opens_when_every_builder_has_accepted(conn):
    ag = _agents(conn)
    _agree_the_scope(conn, ag)
    made = todos.propose_todo(
        conn, ag["backend"], "Contact API", "scope", ["backend", "frontend"]
    )
    assert made["number"] == 1


def test_contracts_are_gated_too(conn):
    """Gating todos covers this transitively — no todos means no contracts — so this is
    the belt to that braces: a task switched INTO engagement while it already held todos
    would otherwise let a contract through on scope nobody agreed."""
    ag = _agents(conn, mode="contract")
    num = todos.propose_todo(
        conn, ag["backend"], "Contact API", "scope", ["backend", "frontend"]
    )["number"]
    todos.accept_todo(conn, ag["frontend"], num)
    conn.execute("UPDATE tasks SET mode = 'engagement' WHERE id = 'acme'")
    conn.commit()
    with pytest.raises(ValueError) as e:
        state.propose_contract(conn, ag["backend"], _spec(), num)
    assert "deliverable" in str(e.value).lower()


# --- but talking is never gated ---------------------------------------------
def test_messaging_is_open_before_the_scope_is_agreed(conn):
    """The most important negative test in this file.

    Pushing back is a CONVERSATION. If the gate covered messaging, a dev who thinks a
    deliverable is unbuildable could not say so, and the engagement would deadlock on
    the discussion it was built to have.
    """
    ag = _agents(conn)
    deliverables.propose_deliverables(conn, ag["owner"], ["A landing page"])
    assert not deliverables.deliverables_locked(conn, "acme")

    service.post_message(
        conn, ag["frontend"], "question",
        "three pages with bespoke components isn't feasible in this timeline — can we cut one?",
    )
    service.post_message(conn, ag["owner"], "answer", "fair — let's drop the reports page.")
    bodies = [
        r["body_json"]
        for r in conn.execute("SELECT body_json FROM messages ORDER BY id").fetchall()
    ]
    assert any("feasible" in b for b in bodies)
    assert any("drop the reports page" in b for b in bodies)


def test_the_owner_can_still_revise_while_the_gate_is_shut(conn):
    """The way OUT of the gate must not itself be gated."""
    ag = _agents(conn)
    deliverables.propose_deliverables(conn, ag["owner"], ["A landing page", "A contact form"])
    deliverables.push_back(conn, ag["frontend"], 1, "which pages?")
    deliverables.revise_deliverable(conn, ag["owner"], 1, "A landing page with 3 buttons")
    for seat in ("backend", "frontend"):
        deliverables.accept_deliverables(conn, ag[seat])
    assert deliverables.deliverables_locked(conn, "acme")
    todos.propose_todo(conn, ag["backend"], "Contact API", "scope", ["backend", "frontend"])


# --- peer sessions are untouched --------------------------------------------
@pytest.mark.parametrize("mode", ["contract", "debug"])
def test_a_peer_task_has_no_gate_at_all(conn, mode):
    """A team that never opens an engagement should not be able to tell this shipped."""
    ag = _agents(conn, roles=("backend", "frontend"), mode=mode)
    if mode == "debug":
        with pytest.raises(ValueError, match="debug tasks don't carry todos"):
            todos.propose_todo(conn, ag["backend"], "x", "y", ["backend", "frontend"])
        return
    made = todos.propose_todo(conn, ag["backend"], "Contact API", "scope",
                              ["backend", "frontend"])
    todos.accept_todo(conn, ag["frontend"], made["number"])
    state.propose_contract(conn, ag["backend"], _spec(), made["number"])


def test_the_gate_holds_in_local_mode(conn, tmp_path):
    """The gate must NOT live in middleware, whose readiness check sits inside
    `if cfg.is_remote:` and therefore does nothing on `sys-buddy local`.

    Local is how this gets demoed and how CLAUDE.md says to test, so a gate that
    evaporates there would look enforced and not be.
    """
    set_config(Config(mode="local", db_path=tmp_path / "x.db", port=9292))
    try:
        ag = _agents(conn)
        with pytest.raises(ValueError):
            todos.propose_todo(conn, ag["backend"], "Contact API", "scope",
                               ["backend", "frontend"])
        _agree_the_scope(conn, ag)
        todos.propose_todo(conn, ag["backend"], "Contact API", "scope",
                           ["backend", "frontend"])
    finally:
        set_config(Config(mode="local", db_path=tmp_path / "x.db", port=8787))


# --------------------------------------------------------------------------- #
# solo todos: legitimate on an engagement, refused on a peer task
# --------------------------------------------------------------------------- #
def test_a_solo_todo_is_allowed_on_an_engagement(conn):
    """One dev building a landing page alone does not need a second dev conscripted to
    rubber-stamp it.

    An engagement has an outer ring a peer task does not: the deliverable list agreed
    with the client, and a verification run that checks it. Tony still agreed deliverable
    #1 with the owner, still locks a contract saying what he will build, and still gets
    checked against it by the owner's agent. The invariant holds — the counterparty is
    the client instead of a peer.
    """
    ag = _agents(conn)
    _agree_the_scope(conn, ag)
    made = todos.propose_todo(conn, ag["frontend"], "Landing page", "shell + buttons",
                              ["frontend"])
    assert made["number"] == 1
    # …and a solo contract locks on the one signature, because there is nobody else bound.
    state.propose_contract(conn, ag["frontend"],
                           {"screens": [{"name": "Landing", "states": ["default"]}]},
                           made["number"])
    state.lock_contract(conn, ag["frontend"], 1, made["number"])
    row = todos.get_row(conn, "acme", made["number"])
    assert row["state"] == "contract_locked"


@pytest.mark.parametrize("mode", ["contract", "debug"])
def test_a_solo_todo_is_still_refused_on_a_peer_task(conn, mode):
    """On a peer task the second seat IS the accountability — a contract with one
    signatory is a note to self, and nobody is positioned to say the work was done.
    Nothing about engagement mode may loosen that."""
    ag = _agents(conn, roles=("backend", "frontend"), mode=mode)
    with pytest.raises(ValueError) as e:
        todos.propose_todo(conn, ag["backend"], "Solo work", "scope", ["backend"])
    assert "TWO" in str(e.value) or "debug tasks don't carry todos" in str(e.value)


def test_a_todo_with_no_parties_is_refused_even_on_an_engagement(conn):
    """Relaxing "at least two" must not become "none" — somebody has to be doing it."""
    ag = _agents(conn)
    _agree_the_scope(conn, ag)
    with pytest.raises(ValueError, match="at least one"):
        todos.propose_todo(conn, ag["frontend"], "Nobody's work", "scope", [])


def test_the_producer_may_check_a_SOLO_engagement_todo(conn):
    """Otherwise a solo todo deadlocks the entire engagement.

    "The producer does not check their own work" is right whenever somebody else can.
    On a solo engagement todo nobody can, so the rule stops being a safeguard and
    becomes a trap: the todo never reaches `verified`, the owner's run is refused
    forever, and nothing is ever checked by anyone.

    Letting the producer through grants nothing. The real check is the outer ring — the
    client's agent goes to the deployed app, and the task only reaches `confirmed` if
    every deliverable comes back accepted. Tony marking his own todo done says "I have
    finished"; Ada's agent still decides whether it works.
    """
    ag = _agents(conn)
    _agree_the_scope(conn, ag)
    num = todos.propose_todo(conn, ag["frontend"], "Mobile layout", "fit a phone",
                             ["frontend"])["number"]
    state.propose_contract(conn, ag["frontend"],
                           {"screens": [{"name": "Landing", "states": ["mobile"]}]}, num)
    state.lock_contract(conn, ag["frontend"], 1, num)
    state.report_status(conn, ag["frontend"], state.STATUS_DEPLOYED, "live", num)
    state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "checked", num)
    state.report_status(conn, ag["frontend"], state.STATUS_VERIFIED, "done", num)
    assert todos.get_row(conn, "acme", num)["state"] == "verified"


def test_the_producer_still_cannot_check_a_shared_todo_on_an_engagement(conn):
    """Narrow by design: two parties means somebody else CAN check, so they must."""
    ag = _agents(conn)
    _agree_the_scope(conn, ag)
    num = todos.propose_todo(conn, ag["frontend"], "Shared work", "scope",
                             ["frontend", "backend"])["number"]
    todos.accept_todo(conn, ag["backend"], num)
    state.propose_contract(conn, ag["frontend"],
                           {"screens": [{"name": "S", "states": ["a"]}]}, num)
    state.lock_contract(conn, ag["frontend"], 1, num)
    state.lock_contract(conn, ag["backend"], 1, num)
    state.report_status(conn, ag["frontend"], state.STATUS_DEPLOYED, "live", num)
    with pytest.raises(ValueError, match="doesn't report checks on its own work"):
        state.report_status(conn, ag["frontend"], state.STATUS_TEST_PASSED, "self", num)


@pytest.mark.parametrize("mode", ["contract"])
def test_a_peer_producer_can_never_check_their_own_work(conn, mode):
    """A peer task has no outer ring at all, so the original rule stands unconditionally
    — there is nothing there to catch a false claim."""
    ag = _agents(conn, roles=("backend", "frontend"), mode=mode)
    num = todos.propose_todo(conn, ag["backend"], "API", "scope",
                             ["backend", "frontend"])["number"]
    todos.accept_todo(conn, ag["frontend"], num)
    state.propose_contract(conn, ag["backend"], _spec(), num)
    state.lock_contract(conn, ag["backend"], 1, num)
    state.lock_contract(conn, ag["frontend"], 1, num)
    state.report_status(conn, ag["backend"], state.STATUS_DEPLOYED, "live", num)
    with pytest.raises(ValueError, match="doesn't report checks on its own work"):
        state.report_status(conn, ag["backend"], state.STATUS_TEST_PASSED, "self", num)
