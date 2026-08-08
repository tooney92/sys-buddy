"""Specs for removing ONE party from a todo — the two situations, and the two mechanisms.

The charter used to say "No tool removes a peer from a todo, and you should not ask for
one." That answered the wrong half of the question. It kept a real property — an agent
must never be able to delete the peer who disagrees with it — but it left two ordinary
situations with no move at all, because the only tool was ``drop_todo``, which is MUTUAL
and abandons the WHOLE deliverable:

* **"we don't need mobile after all"** — mobile is here and agrees. Removing one party
  meant asking two other people to throw away work they still wanted;
* **"mobile has an outage"** — its agent cannot call a tool, which is what an outage IS,
  so a self-removal is useless exactly when it is needed and a mutual drop deadlocks on
  the missing party.

So there are two mechanisms, and the split is the whole design (D14):

    leave_todo(N, reason)                   any party's agent, and it can name ONLY ITSELF
    sys-buddy todo drop-party … --seat X    a HUMAN, from the CLI, for a seat gone dark

The load-bearing claims, one section each:

* **quorum RECOMPUTES.** Derived readings fix themselves; the three LATCHED gates (a
  draft contract's lock, an issue's resolution, a mutual drop) are re-run. If they were
  not, the outage case would end with a todo unblocked on paper and frozen in fact.
* **no agent can name a peer**, under any argument, on either surface.
* **the refusals**: the last party, a todo that would go below the party floor, verified.
* **a locked contract still stands**, and ``get_contract`` says a signatory has left
  rather than showing the signature as though they were still bound.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from sys_buddy import admin, api, cli, service, state, todos, tools
from sys_buddy.config import Config, set_config
from sys_buddy.db import connect, init_db
from sys_buddy.identity import Identity
from sys_buddy.middleware import ACTION_TOOLS
from sys_buddy.rules import RULES_OF_ENGAGEMENT
from tests.conftest import seed_agent, seed_task

UI_HTML = (Path(tools.__file__).parent / "ui.html").read_text()

SPEC = {"endpoints": [{"method": "POST", "path": "/api/auth/login"}]}


def _agents(conn, task="signin", roles=("backend", "frontend", "mobile"), mode=None):
    seed_task(conn, task, roles=roles)
    if mode:
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


def _accepted(conn, ag, parties, task="signin", title="Sign-in", propose=todos.propose_todo):
    """A todo/issue every named party has accepted, built through the REAL ops."""
    t = propose(conn, ag[parties[0]], title, f"scope of {title}", list(parties))
    for role in parties[1:]:
        todos.accept_todo(conn, ag[role], t["number"])
    return t["number"]


def _row(conn, n, task="signin"):
    return todos.get_row(conn, task, n)


def _dict(conn, n, task="signin"):
    return todos.to_dict(conn, _row(conn, n, task))


def _events(conn, task="signin"):
    return [
        json.loads(r["detail_json"])
        for r in conn.execute(
            "SELECT detail_json FROM events WHERE task_id = ? AND kind = 'todo' ORDER BY id",
            (task,),
        ).fetchall()
    ]


# --------------------------------------------------------------------------- #
# 1. THE HEADLINE — quorum recomputes, so the outage case actually resolves
# --------------------------------------------------------------------------- #
def test_removing_the_silent_party_locks_a_contract_that_was_only_waiting_on_it(conn):
    """THE test. Three seats, a contract two of them have signed, the third gone dark.

    Everything up to the removal is the real flow; nothing is inserted by hand. Before:
    the contract is a draft awaiting mobile and the task's rollup sits at
    `contract_proposed` — no move exists for anybody, because the one call that would
    unblock it belongs to the agent that cannot call anything. After: the SAME rule
    ("every party has signed") over a smaller "all", and it locks.
    """
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], SPEC, n)
    state.lock_contract(conn, ag["backend"], 1, n)
    signed = state.lock_contract(conn, ag["frontend"], 1, n)

    # STUCK: the lock is one signature short and that signature is never coming.
    assert signed["locked"] is False and signed["remaining"] == ["mobile"]
    assert state.get_contract(conn, "signin", n)["status"] == "proposed"
    assert todos.rollup(conn, "signin")["state"] == "contract_proposed"

    todos.host_remove_party(conn, "signin", n, "mobile", "agent offline since Tuesday")

    # RESOLVED: no agent acted, and the deliverable is moving again.
    gc = state.get_contract(conn, "signin", n)
    assert gc["status"] == "locked" and gc["signatures"] == ["backend", "frontend"]
    assert todos.parties_of(_row(conn, n)) == ["backend", "frontend"]
    assert _row(conn, n)["state"] == "contract_locked"
    assert todos.rollup(conn, "signin")["state"] == "contract_locked"


def test_the_todo_then_marches_all_the_way_to_verified_without_the_removed_party(conn):
    """The unblock is real, not cosmetic: the rest of the flow runs to completion.

    A quorum that "recomputes" but leaves the todo unable to reach `verified` would have
    fixed nothing — this walks ready → checked → verified over the two remaining seats and
    checks the TASK concludes, which is the thing the human actually wanted back.
    """
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], SPEC, n)
    state.lock_contract(conn, ag["backend"], 1, n)
    state.lock_contract(conn, ag["frontend"], 1, n)
    todos.host_remove_party(conn, "signin", n, "mobile", "agent offline since Tuesday")

    state.report_status(conn, ag["backend"], "ready", "deployed", number=n)
    state.report_status(conn, ag["frontend"], "checked", "works", number=n)
    state.report_status(conn, ag["frontend"], "verified", "end to end", number=n)

    roll = todos.rollup(conn, "signin")
    assert roll["complete"] is True and roll["state"] == "verified"


def test_a_self_leave_unblocks_the_same_lock(conn):
    """The cooperative half of the same gate: mobile is present and takes itself off."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], SPEC, n)
    state.lock_contract(conn, ag["backend"], 1, n)
    state.lock_contract(conn, ag["frontend"], 1, n)

    out = todos.leave_todo(conn, ag["mobile"], n, "we dropped the mobile client")

    assert out["parties"] == ["backend", "frontend"]
    assert state.get_contract(conn, "signin", n)["status"] == "locked"


def test_removing_the_silent_party_resolves_an_issue_everybody_else_called_fixed(conn):
    """The DEBUG half of the recompute — `fixed #N` is the other all-must-agree gate.

    Two of three have reported the bug fixed; the third never will. The issue sits at
    `accepted` and the debug task stays `open`, which is exactly right until the party
    list changes and wrong the instant it does.
    """
    ag = _agents(conn, task="bug", roles=("dev", "reviewer", "qa"), mode="debug")
    n = _accepted(conn, ag, ("dev", "reviewer", "qa"), task="bug", title="refresh 500s",
                  propose=todos.propose_issue)
    state.report_status(conn, ag["dev"], "fixed", "patched the token path", number=n)
    state.report_status(conn, ag["reviewer"], "fixed", "confirmed", number=n)

    assert todos.status_of(conn, _row(conn, n, "bug")) == todos.ACCEPTED
    assert todos.rollup(conn, "bug")["state"] == "open"

    todos.host_remove_party(conn, "bug", n, "qa", "qa's agent has been down for days")

    assert todos.status_of(conn, _row(conn, n, "bug")) == todos.RESOLVED
    assert todos.rollup(conn, "bug")["state"] == "resolved"


def test_a_mutual_drop_completes_when_its_last_outstanding_consent_walks_away(conn):
    """The third latched gate. Two parties consented to dropping; the third is the one
    that has gone. Removing it must finish the drop rather than leave a todo everybody
    already agreed to abandon sitting in the rollup forever."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.drop_todo(conn, ag["backend"], n, "descoped")
    todos.drop_todo(conn, ag["frontend"], n, "descoped")
    assert todos.status_of(conn, _row(conn, n)) != todos.DROPPED

    todos.host_remove_party(conn, "signin", n, "mobile", "gone dark")

    assert todos.status_of(conn, _row(conn, n)) == todos.DROPPED


def test_acceptance_status_recomputes_with_no_gate_at_all(conn):
    """The DERIVED half, for contrast: a todo waiting on the departing party's acceptance
    needs nothing re-run — `status_of` reads the party list live, so it is simply correct
    the moment the list changes. This is why only three gates are re-run, not everything.
    """
    ag = _agents(conn)
    t = todos.propose_todo(conn, ag["backend"], "Sign-in", "scope", ["backend", "frontend", "mobile"])
    todos.accept_todo(conn, ag["frontend"], t["number"])
    assert todos.status_of(conn, _row(conn, t["number"])) == todos.PENDING

    todos.host_remove_party(conn, "signin", t["number"], "mobile", "gone dark")

    assert todos.status_of(conn, _row(conn, t["number"])) == todos.ACCEPTED
    assert _dict(conn, t["number"])["awaiting"] == []


def test_settling_never_locks_a_contract_a_remaining_party_has_not_signed(conn):
    """The recompute only ever UNBLOCKS. A smaller "all" can satisfy an all-must-agree
    test the bigger one did not; it can never satisfy one the remaining parties fail."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], SPEC, n)
    state.lock_contract(conn, ag["backend"], 1, n)  # frontend has NOT signed

    todos.host_remove_party(conn, "signin", n, "mobile", "gone dark")

    gc = state.get_contract(conn, "signin", n)
    assert gc["status"] == "proposed" and gc["awaiting"] == ["frontend"]


# --------------------------------------------------------------------------- #
# 2. NO AGENT REMOVES A PEER — structural, not a permission check
# --------------------------------------------------------------------------- #
def _schemas(mode, tmp_path) -> dict:
    mcp = FastMCP("t")
    cfg = Config(mode=mode, db_path=tmp_path / f"{mode}.db")
    tools.register_tools(mcp, cfg)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_leave_todo_is_registered_on_both_surfaces(tmp_path, mode):
    """A capability on ONE surface is a silent gap for half the users."""
    assert "leave_todo" in _schemas(mode, tmp_path)


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_no_tool_surface_lets_you_name_the_seat_that_leaves(tmp_path, mode):
    """THE property. Not "naming a peer is refused" — naming a peer is UNSPELLABLE.

    The remote tool takes (todo, reason); the local one adds (task, agent) because that is
    how every local tool identifies its CALLER on a loopback broker. Neither has a
    seat/handle/party/role argument, so there is no request an agent could construct that
    would remove somebody else, however it is worded.
    """
    schema = _schemas(mode, tmp_path)["leave_todo"]
    params = set((schema.parameters or {}).get("properties", {}))
    assert not params & {"seat", "handle", "party", "role", "who", "agent_id", "from_role"}
    assert params == ({"todo", "reason"} if mode == "remote"
                      else {"task", "agent", "todo", "reason"})


def test_the_op_behind_both_tools_takes_no_seat_either(tmp_path):
    """Belt and braces one level down: the shared op is `(ident, todo, reason)`. `ident`
    is stamped by the broker from the caller's token, never supplied by the caller."""
    assert list(inspect.signature(tools._op_leave_todo).parameters) == [
        "ident", "todo", "reason"
    ]


def test_leave_todo_removes_the_caller_and_nobody_else(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.leave_todo(conn, ag["mobile"], n, "not needed")
    assert todos.parties_of(_row(conn, n)) == ["backend", "frontend"]


def test_a_non_party_cannot_leave_a_todo_it_was_never_on(conn):
    """The mirror of "a non-party does not block it". Without this, `leave_todo` on a todo
    that does not name you would silently succeed and write a departure for a binding that
    never existed."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend"))
    with pytest.raises(ValueError, match="not a party"):
        todos.leave_todo(conn, ag["mobile"], n, "get me out")


def test_leave_todo_is_gated_by_pre_flight(conn):
    """It is a WRITE on an agreement — it changes who is bound, and it can lock a contract
    on its own — so it waits on the readiness gate like every other write."""
    assert "leave_todo" in ACTION_TOOLS


def test_the_charter_no_longer_tells_agents_no_tool_exists(conn):
    """The charter is the one place an agent is told what it may do. Leaving the old
    absolute in ("No tool removes a peer from a todo, and you should not ask for one")
    would keep agents asking their humans to drop whole todos."""
    assert "leave_todo" in RULES_OF_ENGAGEMENT
    assert "No tool removes a peer from a todo" not in RULES_OF_ENGAGEMENT
    assert "NO AGENT REMOVES A PEER" in RULES_OF_ENGAGEMENT
    assert "drop-party" in RULES_OF_ENGAGEMENT


# --------------------------------------------------------------------------- #
# 3. THE REFUSALS — the same three for a self-leave and a host removal
# --------------------------------------------------------------------------- #
def _engagement(conn, task="acme"):
    """An engagement whose deliverable list is LOCKED, so todos can be proposed on it.

    Engagement is the one mode where a SOLO todo is legal, which makes it the only place
    the "last party" and "down to one" rules can be exercised for real rather than by
    hand-editing a party list.
    """
    ag = _agents(conn, task=task, roles=("backend", "owner", "frontend"), mode="engagement")
    tools._op_propose_deliverables(ag["owner"], ["a landing page with four buttons"])
    tools._op_accept_deliverables(ag["backend"])
    tools._op_accept_deliverables(ag["frontend"])
    return ag


def test_the_last_party_cannot_leave(conn):
    """An empty todo is orphaned: no quorum to reach, nobody who may act on it, and it
    still counts toward the task. That case already has a tool, and the refusal names it.
    """
    ag = _engagement(conn)
    t = todos.propose_todo(conn, ag["backend"], "Landing page", "scope", ["backend"])
    with pytest.raises(ValueError) as e:
        todos.leave_todo(conn, ag["backend"], t["number"], "done with it")
    assert "ONLY party" in str(e.value) and "drop_todo" in str(e.value)


def test_leaving_may_not_take_a_peer_todo_below_two_parties(conn):
    """DECIDED: a one-party todo stays illegal where it was already illegal.

    `_validate_parties` refuses to PROPOSE a peer todo with one seat, so removal must not
    manufacture one. This is not symmetry for its own sake — it would DEADLOCK: the sole
    party proposed the contract and is therefore the producer, and a producer may not
    report checks on its own work off an engagement, so the todo can never reach `testing`
    and `verified` is unreachable forever.
    """
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "mobile"))
    with pytest.raises(ValueError) as e:
        todos.leave_todo(conn, ag["mobile"], n, "not needed after all")
    assert "at least 2 seats" in str(e.value)
    assert "drop_todo" in str(e.value)          # …and it names the move that IS right
    assert todos.parties_of(_row(conn, n)) == ["backend", "mobile"]


def test_the_host_gets_exactly_the_same_refusal(conn):
    """A rule a human could bypass would be a rule that gets bypassed. The two paths
    differ in WHO may act, never in what the resulting todo may look like."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "mobile"))
    with pytest.raises(ValueError, match="at least 2 seats"):
        todos.host_remove_party(conn, "signin", n, "mobile", "gone dark")


def test_an_engagement_may_go_down_to_one_party(conn):
    """The documented exception, and it stays one: an engagement's outer ring — the
    client's own agent verifying against the deliverable list — supplies the counterparty
    a peer task has to name, which is why a solo engagement todo is proposable at all."""
    ag = _engagement(conn)
    t = todos.propose_todo(conn, ag["backend"], "Landing page", "scope", ["backend", "frontend"])
    todos.accept_todo(conn, ag["frontend"], t["number"])

    todos.leave_todo(conn, ag["frontend"], t["number"], "backend is doing this alone")

    assert todos.parties_of(_row(conn, t["number"], "acme")) == ["backend"]


def test_nobody_leaves_a_verified_todo(conn):
    """The same terminal rule `drop_todo` obeys, for the same reason: the rollup already
    reports the deliverable as done, and the record of who delivered it is part of that."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    state.propose_contract(conn, ag["backend"], SPEC, n)
    for role in ("backend", "frontend", "mobile"):
        state.lock_contract(conn, ag[role], 1, n)
    state.report_status(conn, ag["backend"], "ready", "live", number=n)
    state.report_status(conn, ag["frontend"], "checked", "works", number=n)
    state.report_status(conn, ag["frontend"], "verified", "done", number=n)

    with pytest.raises(ValueError, match="verified"):
        todos.leave_todo(conn, ag["mobile"], n, "too late")
    with pytest.raises(ValueError, match="verified"):
        todos.host_remove_party(conn, "signin", n, "mobile", "too late")


def test_a_departure_always_carries_a_reason(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    with pytest.raises(ValueError, match="reason"):
        todos.leave_todo(conn, ag["mobile"], n, "   ")
    with pytest.raises(ValueError, match="reason"):
        todos.host_remove_party(conn, "signin", n, "mobile", "")


def test_the_host_cannot_remove_a_seat_that_is_not_a_party(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend"))
    with pytest.raises(ValueError) as e:
        todos.host_remove_party(conn, "signin", n, "mobile", "gone dark")
    assert "not a party" in str(e.value) and "backend, frontend" in str(e.value)


def test_nothing_can_be_removed_from_a_dropped_todo(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.host_drop_todo(conn, "signin", n, "descoped")
    with pytest.raises(ValueError, match="dropped"):
        todos.leave_todo(conn, ag["mobile"], n, "too late")
    with pytest.raises(ValueError, match="dropped"):
        todos.host_remove_party(conn, "signin", n, "mobile", "too late")


# --------------------------------------------------------------------------- #
# 4. A LOCKED CONTRACT SIGNED BY A DEPARTING PARTY
# --------------------------------------------------------------------------- #
def _locked(conn, ag, parties=("backend", "frontend", "mobile")):
    n = _accepted(conn, ag, parties)
    state.propose_contract(conn, ag[parties[0]], SPEC, n)
    for role in parties:
        state.lock_contract(conn, ag[role], 1, n)
    return n


def test_a_locked_contract_still_stands_when_a_signatory_leaves(conn):
    """DECIDED: it stands. It was validly agreed by everyone it bound at the time and the
    shape has not changed; voiding it would revoke an agreement nobody withdrew from and
    strand work already built against it."""
    ag = _agents(conn)
    n = _locked(conn, ag)
    todos.leave_todo(conn, ag["mobile"], n, "we dropped the mobile client")

    gc = state.get_contract(conn, "signin", n)
    assert gc["locked"] is True and gc["status"] == "locked"
    assert gc["signatures"] == ["backend", "frontend", "mobile"]
    assert gc["spec"] == SPEC


def test_get_contract_says_a_signatory_has_left_rather_than_showing_it_silently(conn):
    """…and the OTHER half of that decision, which is the part that matters.

    `signatures` and `signatories` now disagree, and a reader has no way to tell a
    departure from a bug. Left unsaid, this would be the feature's own failure mode one
    level down: a signature displayed as though the person behind it were still bound.
    """
    ag = _agents(conn)
    n = _locked(conn, ag)
    todos.leave_todo(conn, ag["mobile"], n, "we dropped the mobile client")

    gc = state.get_contract(conn, "signin", n)
    assert gc["signatories"] == ["backend", "frontend"]      # who is bound NOW
    assert gc["departed_signatories"] == ["mobile"]          # who signed and has gone
    assert "since left" in gc["departed_note"]
    assert "reopen_negotiations" in gc["departed_note"]      # the move, if it now matters


def test_a_contract_with_no_departed_signatory_says_nothing_extra(conn):
    """The key is ABSENT rather than empty on the ordinary case, so an older dashboard or
    a briefing that never heard of departures renders byte-identically."""
    ag = _agents(conn)
    n = _locked(conn, ag)
    assert "departed_signatories" not in state.get_contract(conn, "signin", n)


def test_provenance_survives_on_the_todo_too(conn):
    """`get_contract` names WHO; `get_todos` carries the mode, the reason and the time —
    a reader must be able to tell "left" from "was removed" without leaving the record."""
    ag = _agents(conn)
    n = _locked(conn, ag)
    todos.leave_todo(conn, ag["mobile"], n, "we dropped the mobile client")

    dep = _dict(conn, n)["departed"]
    assert len(dep) == 1
    assert dep[0]["seat"] == "mobile" and dep[0]["mode"] == todos.LEFT
    assert dep[0]["by"] == "mobile" and dep[0]["reason"] == "we dropped the mobile client"
    assert dep[0]["version"] == 1 and isinstance(dep[0]["at"], float)


def test_a_host_removal_is_recorded_as_removed_by_the_host(conn):
    ag = _agents(conn)
    n = _locked(conn, ag)
    todos.host_remove_party(conn, "signin", n, "mobile", "agent has been down for days")

    dep = _dict(conn, n)["departed"][0]
    assert dep["mode"] == todos.REMOVED and dep["by"] == todos.HOST


def test_a_seat_named_again_by_a_repropose_stops_reading_as_departed(conn):
    """The log appends forever; the LIVE party list wins over it. Otherwise a party who
    came back would go on being struck through on every surface that reads the history."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.leave_todo(conn, ag["mobile"], n, "not needed")
    todos.repropose_todo(conn, ag["backend"], n, parties=["backend", "frontend", "mobile"])

    row = _row(conn, n)
    assert todos.departed_seats(conn, row) == []
    assert _dict(conn, n)["departed"][0]["seat"] == "mobile"   # the FACT is still logged


# --------------------------------------------------------------------------- #
# 5. REJOINING — repropose_todo, unchanged, and who may do it
# --------------------------------------------------------------------------- #
def test_a_remaining_party_can_bring_a_departed_seat_back(conn):
    """CONFIRMED: `repropose_todo` is the path, and nothing about leaving breaks it. It
    already resets every acceptance and every draft signature, so the returning party is
    indistinguishable from an original one."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.leave_todo(conn, ag["mobile"], n, "not needed")

    out = todos.repropose_todo(conn, ag["backend"], n,
                               parties=["backend", "frontend", "mobile"])
    assert out["parties"] == ["backend", "frontend", "mobile"]
    assert out["version"] == 2
    assert out["accepted_by"] == ["backend"]        # everyone re-accepts, including mobile
    assert set(out["awaiting"]) == {"frontend", "mobile"}

    todos.accept_todo(conn, ag["frontend"], n)
    todos.accept_todo(conn, ag["mobile"], n)
    assert todos.status_of(conn, _row(conn, n)) == todos.ACCEPTED


def test_a_departed_seat_cannot_repropose_itself_back_in(conn):
    """The right asymmetry, and it falls out of the existing `assert_party` rather than
    needing a rule: leaving is your own business, returning is the remaining parties'."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.leave_todo(conn, ag["mobile"], n, "not needed")
    with pytest.raises(ValueError, match="not a party"):
        todos.repropose_todo(conn, ag["mobile"], n,
                             parties=["backend", "frontend", "mobile"])


def test_a_draft_contracts_signatures_still_reset_when_a_returning_party_is_added(conn):
    """The existing rule keeps applying: the others signed a shape that bound two parties
    and it may now bind three, so they sign again. Leaving does not weaken it."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.leave_todo(conn, ag["mobile"], n, "not needed")
    state.propose_contract(conn, ag["backend"], SPEC, n)
    state.lock_contract(conn, ag["backend"], 1, n)

    todos.repropose_todo(conn, ag["backend"], n, parties=["backend", "frontend", "mobile"])
    assert state.get_contract(conn, "signin", n)["signatures"] == []


# --------------------------------------------------------------------------- #
# 6. NOTHING VANISHES — events, messages, and the broker's voice
# --------------------------------------------------------------------------- #
def test_a_self_leave_writes_an_event_and_tells_the_others_in_its_own_voice(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.leave_todo(conn, ag["mobile"], n, "we dropped the mobile client")

    ev = [e for e in _events(conn) if e["action"] == "todo_party_left"]
    assert len(ev) == 1
    assert ev[0]["seat"] == "mobile" and ev[0]["by"] == "mobile"
    assert ev[0]["reason"] == "we dropped the mobile client"
    assert ev[0]["parties"] == ["backend", "frontend"]
    assert ev[0]["todo_number"] == n

    msgs = [m for m in api._messages_for(conn, "signin") if m["type"] == "todo_leave"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "mobile"          # a peer said this, and it was true
    assert "LEFT" in msgs[0]["body"]


def test_a_host_removal_speaks_as_the_broker_and_can_never_be_forged(conn):
    """Nobody's agent authored it, and — more importantly — no agent CAN. A message
    claiming a peer was removed from an agreement would be a lie an injection could aim
    at, so the type is broker-only on the send path."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.host_remove_party(conn, "signin", n, "mobile", "agent offline since Tuesday")

    msgs = [m for m in api._messages_for(conn, "signin") if m["type"] == "todo_party_removed"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == service.BROKER_ROLE
    body = msgs[0]["body"]
    assert "REMOVED" in body and "agent offline since Tuesday" in body
    assert "no agent can remove anyone" in body.lower()
    assert "still on the TASK" in body

    assert "todo_party_removed" in service.BROKER_TYPES
    with pytest.raises(ValueError, match="authored by the broker"):
        service.assert_sendable("todo_party_removed")


def test_the_event_log_names_the_seat_that_went_not_just_who_acted(conn):
    """`Todo #1 party_removed by host` reads as though the HOST left. The one fact a
    reader needs — WHO is no longer on it — would be the fact the line omitted."""
    ag = _agents(conn, roles=("backend", "frontend", "mobile", "qa"))
    n = _accepted(conn, ag, ("backend", "frontend", "mobile", "qa"))
    todos.host_remove_party(conn, "signin", n, "mobile", "gone dark")
    todos.leave_todo(conn, ag["frontend"], n, "not ours")

    rendered = [
        api._render_detail("todo", e) for e in _events(conn)
        if e["action"] in ("todo_party_left", "todo_party_removed")
    ]
    assert any("mobile REMOVED by host: gone dark" in r for r in rendered)
    assert any("frontend LEFT: not ours" in r for r in rendered)


def test_nothing_already_recorded_is_deleted(conn):
    """Acceptances, fixes and signatures are statements somebody actually MADE. Ending an
    obligation is not the same as rewriting history, and every quorum reads the party list
    live — so a departed party's rows are simply never counted again."""
    ag = _agents(conn)
    n = _locked(conn, ag)
    todos.leave_todo(conn, ag["mobile"], n, "not needed")

    assert "mobile" in todos.decisions(conn, _row(conn, n)["id"], 1)
    assert state.get_contract(conn, "signin", n)["signatures"] == [
        "backend", "frontend", "mobile"
    ]


# --------------------------------------------------------------------------- #
# 7. THE HOST'S CLI — the human-driven half
# --------------------------------------------------------------------------- #
def _cli_seed(tmp_path, roles=("backend", "frontend", "mobile")) -> tuple[str, dict]:
    dbfile = tmp_path / "t.db"
    set_config(Config(mode="local", db_path=dbfile))
    init_db(dbfile)
    admin.create_task("signin", title="Sign-in", roles=list(roles))
    conn = connect(dbfile)
    seats = {}
    try:
        for role in roles:
            cur = conn.execute(
                "INSERT INTO agents (task_id, name, role, handle, token_hash, created_at) "
                "VALUES (?,?,?,?,NULL,?)",
                ("signin", f"{role}-agent", role, role, time.time()),
            )
            seats[role] = Identity(
                agent_id=cur.lastrowid, task_id="signin", name=f"{role}-agent", role=role
            )
        conn.commit()
        t = todos.propose_todo(conn, seats["backend"], "Payments API", "scope",
                               list(roles))
        for role in roles[1:]:
            todos.accept_todo(conn, seats[role], t["number"])
        state.propose_contract(conn, seats["backend"], SPEC, t["number"])
        state.lock_contract(conn, seats["backend"], 1, t["number"])
        state.lock_contract(conn, seats["frontend"], 1, t["number"])
    finally:
        conn.close()
    return str(dbfile), t["number"]


def test_the_cli_removes_a_party_and_prints_what_it_unblocked(tmp_path, capsys):
    """The last lines matter as much as the removal: the host ran this because a todo was
    frozen, and "removed mobile" alone leaves them to go and check whether it worked."""
    db, n = _cli_seed(tmp_path)
    assert cli.main([
        "--db", db, "todo", "drop-party", "signin", str(n),
        "--seat", "mobile", "--reason", "agent offline since Tuesday",
    ]) == 0
    out = capsys.readouterr().out
    assert f"Removed mobile from todo #{n} 'Payments API'" in out
    assert "still bound: backend, frontend" in out
    assert "agent offline since Tuesday" in out
    assert "still on the TASK" in out
    assert "state contract_locked" in out


def test_the_cli_accepts_the_todo_number_as_a_flag_too(tmp_path, capsys):
    """`--todo N` is how it reads in prose and in the dashboard's own hint; the positional
    matches its sibling `todo drop`. Both work; supplying both is an error, not a guess."""
    db, n = _cli_seed(tmp_path)
    assert cli.main([
        "--db", db, "todo", "drop-party", "signin",
        "--todo", str(n), "--seat", "mobile", "--reason", "offline",
    ]) == 0
    assert "Removed mobile" in capsys.readouterr().out


def test_the_cli_refuses_a_todo_number_given_twice_or_not_at_all(tmp_path, capsys):
    db, n = _cli_seed(tmp_path)
    assert cli.main([
        "--db", db, "todo", "drop-party", "signin", str(n), "--todo", str(n),
        "--seat", "mobile", "--reason", "offline",
    ]) == 2
    assert "given twice" in capsys.readouterr().err
    assert cli.main([
        "--db", db, "todo", "drop-party", "signin", "--seat", "mobile", "--reason", "x",
    ]) == 2
    assert "which todo?" in capsys.readouterr().err


def test_todo_list_shows_who_left_and_offers_the_smaller_move_first(tmp_path, capsys):
    """The usual stall is ONE party gone dark while the others still want the deliverable,
    so the host's list must offer removing that party before abandoning everyone's work."""
    db, n = _cli_seed(tmp_path)
    cli.main(["--db", db, "todo", "drop-party", "signin", str(n),
              "--seat", "mobile", "--reason", "offline since Tuesday"])
    capsys.readouterr()

    assert cli.cmd_todo_list(SimpleNamespace(db=db, task="signin")) == 0
    out = capsys.readouterr().out
    assert "left: mobile (removed: offline since Tuesday)" in out
    assert "sys-buddy todo drop-party signin <N> --seat <handle>" in out
    assert out.index("drop-party") < out.index('todo drop signin <N> --reason')


def test_the_host_can_type_a_display_name_or_a_role_for_the_seat(tmp_path):
    """Same resolver every binding seat reference uses, so the host types what is in their
    head and is corrected rather than guessed at."""
    db, n = _cli_seed(tmp_path)
    t, _roll = admin.host_remove_party("signin", n, "@mobile", "offline")
    assert t["parties"] == ["backend", "frontend"]


# --------------------------------------------------------------------------- #
# 8. THE DASHBOARD — a party that vanishes with no trace is the failure mode
# --------------------------------------------------------------------------- #
def _fn(name: str) -> str:
    m = re.search(r"^function " + re.escape(name) + r"\(", UI_HTML, re.M)
    assert m, f"{name}() is gone from ui.html"
    return UI_HTML[m.start():UI_HTML.index("\n}", m.start())]


def test_the_board_renders_departed_seats_apart_from_live_ones():
    """UNDER the party pills, never mixed in: the top row is "who is bound", this is "who
    used to be". Mixed, a struck-through name reads as a party that has not answered."""
    src = _fn("todoDepartedPillsHTML")
    assert "line-through" in src
    assert "removed" in src and "left" in src            # WHICH of the two, on the pill
    assert "live.indexOf(d.seat)>=0" in src              # a returning party is not gone
    assert "todoDepartedPillsHTML(t)+todoDepartedNoteHTML(t)" in UI_HTML


def test_the_board_prints_the_drop_party_command_and_never_issues_it():
    """D11 is unchanged: the dashboard prints the line the human types in their OWN
    terminal. A leaked ?v= link must only ever be able to LOOK."""
    src = _fn("dropTodoHTML")
    assert "sys-buddy todo drop-party " in src
    assert "state.viewer && state.viewer.mode!=='buddy'" in src   # host viewers only
    # …and it appears at the stage the removal exists for — a contract one signature
    # short — which the old `pending || stuck` gate missed entirely.
    assert "nx.who" in src


def test_the_api_payload_carries_the_departures(conn):
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.host_remove_party(conn, "signin", n, "mobile", "gone dark")
    row = next(t for t in api._todos_for(conn, "signin") if t["number"] == n)
    assert row["departed"][0]["seat"] == "mobile"
    assert row["departed"][0]["mode"] == "removed"


def test_a_completed_drop_is_credited_to_whoever_caused_it(conn):
    """`dropped_by` is "who finalised it". When a departure completes a mutual drop, that
    is the seat that left (or `host`) — crediting a remaining party would name somebody
    who consented days ago and did nothing today."""
    ag = _agents(conn)
    n = _accepted(conn, ag, ("backend", "frontend", "mobile"))
    todos.drop_todo(conn, ag["backend"], n, "descoped")
    todos.drop_todo(conn, ag["frontend"], n, "descoped")
    todos.leave_todo(conn, ag["mobile"], n, "not ours")
    assert _dict(conn, n)["dropped_by"] == "mobile"

    ag2 = _agents(conn, task="two")
    m = _accepted(conn, ag2, ("backend", "frontend", "mobile"), task="two")
    todos.drop_todo(conn, ag2["backend"], m, "descoped")
    todos.drop_todo(conn, ag2["frontend"], m, "descoped")
    todos.host_remove_party(conn, "two", m, "mobile", "gone dark")
    assert _dict(conn, m, "two")["dropped_by"] == todos.HOST
