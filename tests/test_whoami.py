"""Specs for the broker telling an agent WHO it resolved them as.

THE GAP THIS CLOSES. The broker stamps an identity on every single call and used to keep
it entirely to itself: it scoped by the caller and wrote the caller into audit events,
but never said the answer out loud. So an agent could call `roster()`, read the whole
cast, and still not know which row it WAS.

That is not academic. An agent whose MCP client held another seat's token — a stale,
same-named server entry in a higher-precedence scope quietly winning — ran a full session
believing the seat its PROMPT named while every message and contract signature was filed
under the seat its TOKEN named. Nothing it could call would have told it, and unpicking it
cost an evening. `join.html` carries the human-facing half ("Connected as the wrong
seat?"); this is the broker-side half, so the mixup is caught on the FIRST call.

Two surfaces answer it, and the tests below pin that they answer it the SAME way — a
roster that marked one seat while `rules()` named another would be worse than silence.
"""

from __future__ import annotations

from sys_buddy import tools
from sys_buddy.identity import Identity
from tests.conftest import seed_agent, seed_task


def _ident(conn, task, handle, name, role_type=None):
    agent_id = seed_agent(conn, task, handle, name, f"sbk_{task}_{handle}")
    return Identity(
        agent_id=agent_id, task_id=task, name=name, role=handle, role_type=role_type
    )


# --------------------------------------------------------------------------- #
# roster: which row is me
# --------------------------------------------------------------------------- #
def test_roster_marks_exactly_the_caller_and_no_other(conn):
    """The ABSOLUTE assertion, not a relative one: exactly ONE row is marked, and it is
    the caller's. A test that only checked "my row is marked" would still pass if every
    row were marked, which is the same as marking none."""
    seed_task(conn, "signin", roles=("backend", "frontend", "mobile"))
    me = _ident(conn, "signin", "frontend", "Peter")

    summary = tools._op_roster("signin", me=me.role)

    marked = [r["seat"] for r in summary["rows"] if r.get("you")]
    assert marked == ["frontend"], "exactly the caller's seat carries `you`"
    unmarked = [r["seat"] for r in summary["rows"] if not r.get("you")]
    assert set(unmarked) == {"backend", "mobile"}, "every other seat is explicitly not-you"


def test_roster_you_block_names_the_caller(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    _ident(conn, "signin", "frontend", "Peter")

    you = tools._op_roster("signin", me="frontend")["you"]

    assert you["seat"] == "frontend"
    assert you["address"] == "frontend", "the token to TYPE for this seat"
    assert you["role"] == "frontend", "the KIND of work the seat does"
    assert you["name"] == "Peter", "the human behind it, once they have joined"


def test_two_agents_on_one_task_each_see_themselves(conn):
    """The failure this whole feature exists for is an agent believing it is a seat it is
    not, so the answer must actually depend on WHO asked — the same roster read by two
    callers must mark two different rows."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    _ident(conn, "signin", "backend", "Tony")
    _ident(conn, "signin", "frontend", "Peter")

    be = tools._op_roster("signin", me="backend")
    fe = tools._op_roster("signin", me="frontend")

    assert be["you"]["seat"] == "backend"
    assert fe["you"]["seat"] == "frontend"
    assert [r["seat"] for r in be["rows"] if r["you"]] == ["backend"]
    assert [r["seat"] for r in fe["rows"] if r["you"]] == ["frontend"]


def test_roster_without_a_caller_is_untouched(conn):
    """Local mode names its own caller in the call and has no token that could disagree,
    so it passes no `me` — and must serialise exactly as it did before this feature."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    _ident(conn, "signin", "backend", "Tony")

    summary = tools._op_roster("signin")

    assert "you" not in summary, "no caller, no answer — not a null, ABSENT"
    assert all("you" not in r for r in summary["rows"])


def test_you_address_is_the_typeable_token_for_a_shadowed_seat(conn):
    """A seat whose handle is shadowed by a role type several seats share is addressed by
    its derived alias (`@frontend-1`), not its bare handle. `you.address` must be that
    alias — an agent quoting itself into a `parties` list has to copy the string the
    broker will actually resolve, or it is refused as ambiguous."""
    seed_task(conn, "signin", roles=("backend", "frontend", "frontend"))
    rows = tools._op_roster("signin")["rows"]
    shadowed = [r for r in rows if r["role"] == "frontend"]
    assert shadowed, "the task declares frontend seats"
    handle = shadowed[0]["seat"]

    you = tools._op_roster("signin", me=handle)["you"]

    assert you["address"] == shadowed[0]["address"], (
        "the address the roster renders for that seat, never re-derived separately"
    )


def test_a_caller_with_no_row_still_gets_its_handle_back(conn):
    """A seat revoked mid-session has no roster row. The answer must degrade to "you are
    still this handle" rather than vanish — an agent that gets NO identity back learns
    nothing, which is the state this feature exists to end."""
    seed_task(conn, "signin", roles=("backend", "frontend"))

    you = tools._op_roster("signin", me="ghost")["you"]

    assert you["seat"] == "ghost"
    assert you["address"] == "ghost"
    assert you["name"] is None


# --------------------------------------------------------------------------- #
# rules(): the earliest possible catch
# --------------------------------------------------------------------------- #
def test_rules_opens_by_naming_the_caller(conn):
    """`rules()` is the FIRST tool every briefing tells an agent to call, so it is the
    earliest moment a wrong-seat client can be caught — before any work is done."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    me = _ident(conn, "signin", "frontend", "Peter", role_type="frontend")

    text = tools._op_rules(me)

    first = text.splitlines()[0]
    assert first.startswith("YOU ARE @frontend"), first
    assert "signin" in first, "the task, so a client on the wrong TASK is caught too"
    assert "frontend" in first, "the role type"
    assert "Peter" in first, "the display name the humans see on the board"


def test_rules_says_the_answer_comes_from_the_token_not_the_prompt(conn):
    """The whole point: when the briefing and the token disagree, the BRIEFING is what is
    wrong. If the text does not say so, an agent reading an unexpected seat will assume
    the broker is confused and carry on — which is exactly what went wrong."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    me = _ident(conn, "signin", "backend", "Tony")

    text = tools._op_rules(me)

    assert "TOKEN" in text, "names the source of truth"
    assert "stop and tell your human" in text.lower(), "and what to DO about a mismatch"


def test_rules_still_carries_the_rules(conn):
    """The identity line is a PREFIX, not a replacement — the charter must survive it."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    me = _ident(conn, "signin", "backend", "Tony")

    text = tools._op_rules(me)

    assert "YOU ARE @backend" in text
    assert len(text.splitlines()) > 5, "the rules themselves are still appended"
    assert "staging_url" in text, "a load-bearing rule, still present"


def test_rules_and_roster_never_name_the_caller_differently(conn):
    """Two surfaces answering "who am I" must answer identically. If they can disagree,
    the feature has replaced one ambiguity with a worse one."""
    seed_task(conn, "signin", roles=("backend", "frontend", "frontend"))
    rows = tools._op_roster("signin")["rows"]
    handle = [r for r in rows if r["role"] == "frontend"][0]["seat"]
    me = _ident(conn, "signin", handle, "Peter", role_type="frontend")

    address = tools._op_roster("signin", me=handle)["you"]["address"]

    assert f"YOU ARE @{address}" in tools._op_rules(me)
