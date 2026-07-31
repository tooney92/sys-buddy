"""Specs for ``state.next_step`` — the dashboard's "what happens next" line.

The bug this feature exists to kill: a human stares at a locked todo that will not
move, and nothing on screen says the next move is his OWN agent typing ``ready #1``.

So the one thing these tests actually guard is **agreement with the enforcer**. A
"next step" that drifts from what ``report_status``/``propose_contract``/
``lock_contract`` allow is worse than none — it sends a human to type a command the
broker will refuse. Hence :func:`test_the_named_command_is_the_one_the_broker_accepts`
and its mirror :func:`test_no_other_party_may_make_the_named_move`, which don't assert
on wording at all: they take the move ``next_step`` names and CALL it, at every stage.

Everything else here is coverage of the branches (one per stage, plus the terminal and
human-owned ones) and of the promise that the sentence always names a literal shorthand
to type.
"""

from __future__ import annotations

import pytest

from sys_buddy import api, service, state, todos
from tests.conftest import seed_agent, seed_task

PARTIES = ("backend", "frontend")


# --------------------------------------------------------------------------- #
# seed helpers — same shape as tests/test_todos_state.py
# --------------------------------------------------------------------------- #
def _agents(conn, task="signin", roles=("backend", "frontend", "mobile")):
    seed_task(conn, task, roles=roles)
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


def _spec(path="/api/items") -> dict:
    return {
        "version": 1,
        "endpoints": [{"method": "POST", "path": path}],
    }


def _proposed(conn, ag, proposer="backend", parties=PARTIES, title="login") -> int:
    """Returns the todo's per-task NUMBER — the `#N` a human types and every tool takes."""
    return todos.propose_todo(
        conn, ag[proposer], title, f"scope of {title}", list(parties)
    )["number"]


def _accepted(conn, ag, proposer="backend", parties=PARTIES, title="login") -> int:
    tid = _proposed(conn, ag, proposer, parties, title)
    for p in parties:
        if p != proposer:
            todos.accept_todo(conn, ag[p], tid)
    return tid


def _locked(conn, ag, producer="backend", parties=PARTIES, title="login"):
    """…with a contract proposed by `producer` and signed by everyone. The proposer of
    the LOCK is what makes them the producer (model B) — that is the whole point."""
    tid = _accepted(conn, ag, producer, parties, title)
    v = state.propose_contract(conn, ag[producer], _spec(), tid)["version"]
    for p in parties:
        state.lock_contract(conn, ag[p], v, tid)
    return tid, v


def _live(conn, ag, producer="backend", parties=PARTIES):
    tid, _v = _locked(conn, ag, producer, parties)
    state.report_status(conn, ag[producer], "ready", "up", number=tid)
    return tid


def _testing(conn, ag, producer="backend", parties=PARTIES):
    tid = _live(conn, ag, producer, parties)
    consumer = next(p for p in parties if p != producer)
    state.report_status(conn, ag[consumer], "checked", "works", number=tid)
    return tid


def _internal_id(conn, num, task="signin") -> int:
    """``next_step`` is called by the DASHBOARD off a row it already holds, so it takes
    the internal ``todos.id``; these tests carry the human ``#N``."""
    return todos.get_row(conn, task, num)["id"]


def _next(conn, tid, task="signin"):
    return state.next_step(conn, task, _internal_id(conn, tid, task))


# --------------------------------------------------------------------------- #
# one branch per stage
# --------------------------------------------------------------------------- #
def test_pending_names_the_parties_who_have_not_accepted(conn):
    ag = _agents(conn)
    tid = _proposed(conn, ag)  # proposing IS backend's acceptance
    n = _next(conn, tid)
    assert n["stage"] == "pending"
    assert n["who"] == ["frontend"]  # not backend — it already accepted by proposing
    assert n["cmd"] == f"yes #{tid}"
    assert n["alt"] == f"no #{tid} <why>"


def test_accepted_says_one_of_you_proposes_and_invents_no_producer(conn):
    """Before a lock there is genuinely no producer — the producer is DERIVED from
    whoever proposed the locked contract, so naming one here would be a guess."""
    ag = _agents(conn)
    tid = _accepted(conn, ag)
    n = _next(conn, tid)
    assert n["stage"] == "accepted"
    assert sorted(n["who"]) == ["backend", "frontend"]
    assert n["cmd"] == f"pc #{tid}"
    assert "no producer yet" in n["text"]


def test_contract_proposed_names_only_the_outstanding_signatures(conn):
    ag = _agents(conn)
    tid = _accepted(conn, ag)
    v = state.propose_contract(conn, ag["backend"], _spec(), tid)["version"]
    n = _next(conn, tid)
    assert n["stage"] == "contract_proposed"
    assert n["who"] == ["backend", "frontend"]
    assert n["cmd"] == f"sign #{tid}"

    state.lock_contract(conn, ag["backend"], v, tid)
    assert _next(conn, tid)["who"] == ["frontend"]


def test_contract_locked_names_the_producer_and_ready(conn):
    """The exact screen the owner stared at: locked, and nothing said whose move it was."""
    ag = _agents(conn)
    tid, _v = _locked(conn, ag, producer="backend")
    n = _next(conn, tid)
    assert n["stage"] == "contract_locked"
    assert n["who"] == ["backend"]
    assert n["cmd"] == f"ready #{tid}"
    assert "nothing is building yet" in n["text"]


def test_the_producer_is_per_todo_and_never_hardcoded(conn):
    """Model B: backend produces todo #1 while frontend produces todo #2. A next-step
    that assumed 'backend' would send frontend's human to a command he cannot run."""
    ag = _agents(conn)
    a, _ = _locked(conn, ag, producer="backend", title="one")
    b, _ = _locked(conn, ag, producer="frontend", title="two")
    assert _next(conn, a)["who"] == ["backend"]
    assert _next(conn, b)["who"] == ["frontend"]


def test_backend_live_names_the_consumers_not_the_producer(conn):
    ag = _agents(conn)
    tid = _live(conn, ag, producer="backend")
    n = _next(conn, tid)
    assert n["stage"] == "backend_live"
    assert n["who"] == ["frontend"]
    assert n["cmd"] == f"ok #{tid}"
    assert n["alt"] == f"block #{tid}"


def test_testing_offers_done(conn):
    ag = _agents(conn)
    tid = _testing(conn, ag)
    n = _next(conn, tid)
    assert n["stage"] == "testing"
    assert n["cmd"] == f"done #{tid}"


def test_verified_says_it_is_done_and_asks_for_nothing(conn):
    ag = _agents(conn)
    tid = _testing(conn, ag)
    state.report_status(conn, ag["frontend"], "verified", "confirmed", number=tid)
    n = _next(conn, tid)
    assert n["stage"] == "verified"
    assert n["done"] is True
    assert n["cmd"] is None and n["who"] == []


def test_dropped_asks_for_nothing(conn):
    ag = _agents(conn)
    tid = _accepted(conn, ag)
    todos.host_drop_todo(conn, "signin", tid, "not needed")
    n = _next(conn, tid)
    assert n["stage"] == "dropped"
    assert n["done"] is True and n["cmd"] is None


def test_three_strikes_hands_it_to_the_humans_with_no_command(conn):
    """Past the cord no agent report is accepted at all, so offering a shorthand would
    be advertising a command the broker refuses."""
    ag = _agents(conn)
    tid = _live(conn, ag, producer="backend")
    for _ in range(state.MAX_STRIKES):
        state.report_status(conn, ag["frontend"], "blocked", "nope", number=tid)
    n = _next(conn, tid)
    assert n["stage"] == "cord_pulled"
    assert n["human"] is True
    assert n["cmd"] is None
    with pytest.raises(ValueError):
        state.report_status(conn, ag["frontend"], "blocked", "again", number=tid)


def test_a_stuck_flag_does_not_erase_the_move(conn):
    """`stuck` is an orthogonal FLAG, not a march state — the deliverable is exactly
    where it was, so the next move is unchanged and the human still needs to see it."""
    ag = _agents(conn)
    tid, _v = _locked(conn, ag, producer="backend")
    state.report_status(conn, ag["backend"], "stuck", "waiting on infra", number=tid)
    n = _next(conn, tid)
    assert n["stage"] == "contract_locked"
    assert n["cmd"] == f"ready #{tid}"


def test_a_newer_proposal_in_flight_asks_for_signatures_not_ready(conn):
    """`_report_todo_deployed` refuses 'ready' while the NEWEST version is unsigned even
    though an older one is locked — so the next step must ask for the signature, not the
    build. This is the branch most likely to drift if the rules were copied into JS."""
    ag = _agents(conn)
    tid = _live(conn, ag, producer="backend")
    state.reopen_negotiations(conn, ag["backend"], "shape changed", number=tid)
    v2 = state.propose_contract(conn, ag["backend"], _spec("/api/v2"), tid)["version"]
    n = _next(conn, tid)
    assert n["stage"] == "contract_proposed"
    assert n["cmd"] == f"sign #{tid}"
    with pytest.raises(ValueError, match="awaiting signatures"):
        state.report_status(conn, ag["backend"], "ready", "up", number=tid)
    for p in PARTIES:
        state.lock_contract(conn, ag[p], v2, tid)
    assert _next(conn, tid)["cmd"] == f"ready #{tid}"


# --------------------------------------------------------------------------- #
# the claim that matters: it agrees with the enforcer
# --------------------------------------------------------------------------- #
_SHORTHAND = {"yes": None, "pc": None, "sign": None,
              "ready": "ready", "ok": "checked", "block": "blocked", "done": "verified"}


def _perform(conn, ag, role, cmd, tid, version=None):
    """Actually run the shorthand ``next_step`` printed, through the real writes."""
    verb = cmd.split()[0]
    if verb == "yes":
        return todos.accept_todo(conn, ag[role], tid)
    if verb == "pc":
        return state.propose_contract(conn, ag[role], _spec(), tid)
    if verb == "sign":
        return state.lock_contract(conn, ag[role], version, tid)
    return state.report_status(conn, ag[role], _SHORTHAND[verb], "detail", number=tid)


def test_the_named_command_is_the_one_the_broker_accepts(conn):
    """Walk a todo start to finish doing ONLY what ``next_step`` says, by the role it
    names. Every step must be accepted — that is what "derived from the same rules"
    means in practice, and it is the assertion that catches drift."""
    ag = _agents(conn)
    tid = _proposed(conn, ag, proposer="backend")
    version = None
    stages = []
    for _ in range(12):
        n = _next(conn, tid)
        stages.append(n["stage"])
        if n["done"] or n["human"]:
            break
        assert n["cmd"], f"stage {n['stage']} offered no command"
        assert f"#{tid}" in n["cmd"], "the shorthand must carry the todo number"
        # Every named role must be able to make the move it is told to make.
        for role in n["who"]:
            out = _perform(conn, ag, role, n["cmd"], tid, version)
            if n["stage"] == "accepted":
                version = out["version"]
            if _next(conn, tid)["stage"] != n["stage"]:
                break  # the stage moved on; stop asking the rest for a stale move
    assert stages == [
        "pending", "accepted", "contract_proposed",
        "contract_locked", "backend_live", "testing", "verified",
    ]


@pytest.mark.parametrize("stage_at", ["contract_locked", "backend_live"])
def test_no_other_party_may_make_the_named_move(conn, stage_at):
    """The mirror: a party ``next_step`` does NOT name must be refused. If the line
    named the wrong role, this is where it shows up."""
    ag = _agents(conn)
    tid = _live(conn, ag, producer="backend") if stage_at == "backend_live" \
        else _locked(conn, ag, producer="backend")[0]
    n = _next(conn, tid)
    outsiders = [p for p in PARTIES if p not in n["who"]]
    assert outsiders, "the stage must not name everyone, or this proves nothing"
    for role in outsiders:
        with pytest.raises(ValueError):
            _perform(conn, ag, role, n["cmd"], tid)


def test_a_non_party_is_never_named(conn):
    """mobile is seated on the TASK but not bound by this todo — SEATS ≠ PARTICIPANTS.
    Naming it would send a human to a command ``assert_party`` refuses outright."""
    ag = _agents(conn)
    tid = _proposed(conn, ag, parties=PARTIES)
    for _ in range(4):
        n = _next(conn, tid)
        assert "mobile" not in n["who"]
        if n["done"] or not n["cmd"]:
            break
        for role in n["who"]:
            _perform(conn, ag, role, n["cmd"], tid,
                     version=state._newest_contract(
                         conn, "signin", todo_id=_internal_id(conn, tid)
                     )["version"]
                     if state._newest_contract(
                         conn, "signin", todo_id=_internal_id(conn, tid)
                     ) else None)
    with pytest.raises(ValueError, match="not a party"):
        todos.accept_todo(conn, ag["mobile"], tid)


# --------------------------------------------------------------------------- #
# shape + the API surface
# --------------------------------------------------------------------------- #
def test_every_step_carries_a_sentence_and_the_broker_tool_it_maps_to(conn):
    ag = _agents(conn)
    for tid in (
        _proposed(conn, ag, title="a"),
        _accepted(conn, ag, title="b"),
        _locked(conn, ag, title="c")[0],
        _live(conn, ag, producer="backend"),
    ):
        n = _next(conn, tid)
        assert n["text"].strip()
        assert n["who_label"]
        # The shorthand to TYPE, and the broker tool it maps to — both carry the
        # per-task NUMBER, so a human can copy either one and hit the right deliverable.
        assert f"#{tid}" in n["cmd"]
        assert str(tid) in n["tool"]


def test_who_label_reads_as_prose(conn):
    assert state._who_label(["backend"]) == "backend"
    assert state._who_label(["backend", "frontend"]) == "backend or frontend"
    assert state._who_label(["a", "b", "c"]) == "a, b or c"


def test_api_ships_next_on_every_todo(conn):
    """The dashboard reads it from the payload — it must never be computed in JS."""
    ag = _agents(conn)
    _proposed(conn, ag, title="a")
    _locked(conn, ag, title="b")
    rows = api._todos_for(conn, "signin")
    assert len(rows) == 2
    for r in rows:
        assert r["next"]["text"]
        assert set(r["next"]) >= {"stage", "who", "who_label", "cmd", "alt", "tool",
                                  "text", "human", "done"}


def test_next_is_absent_for_a_task_with_no_todos(conn):
    """Backwards compatibility: a pre-todo task's payload is byte-identical to before,
    so there is no ``next`` to add anywhere."""
    _agents(conn)
    assert api._todos_for(conn, "signin") == []
