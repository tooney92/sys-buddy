"""N participants per role type — seats, role types, resolution, and the roster.

The defect these cover: ``UNIQUE(task_id, role) WHERE revoked_at IS NULL`` meant at
most one live agent per role, so a session with two frontend developers could not be
described. One of them had to pair as ``mobile`` — a lie that then propagated into
every message, signature and event for the life of the task.

The fix splits ``role`` into a HANDLE (who — the seat) and a ROLE TYPE (what kind of
work), keeping ``tasks.roles_json``'s SHAPE and changing only what its strings MEAN.
The load-bearing consequence is at the bottom of this file: **both frontends must sign**
falls out of the unchanged quorum logic, because ``parties_json`` now reads
``["frontend", "frontend-2"]``.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from sys_buddy import admin, db, pairing, seats, service, state, todos
from sys_buddy.identity import resolve_agent_token

from conftest import seed_agent, seed_task


# --------------------------------------------------------------------------- #
# handle derivation
# --------------------------------------------------------------------------- #
def test_the_first_seat_of_a_role_is_named_for_it_and_the_rest_are_suffixed():
    assert seats.suggest_handle("frontend", []) == "frontend"
    assert seats.suggest_handle("frontend", ["frontend"]) == "frontend-2"
    assert seats.suggest_handle("frontend", ["frontend", "frontend-2"]) == "frontend-3"


def test_the_next_seat_joins_the_numbered_family_rather_than_taking_the_bare_name():
    """Once a role type is numbered, the bare handle is NOT free to hand out: it is the
    role TYPE, and giving it to a seat would put the collision back."""
    assert seats.suggest_handle("frontend", ["frontend-1", "frontend-2"]) == "frontend-3"
    assert seats.suggest_handle("frontend", ["frontend-1"]) == "frontend-2"


def test_a_shared_role_type_numbers_every_one_of_its_seats_including_the_first():
    # `frontend-1`, not `frontend` — a handle spelled like a role type that two seats
    # share means both "this seat" and "all of them", and an UNJOINED seat has no
    # display name to disambiguate it with.
    assert seats.numbered_handle("frontend", []) == "frontend-1"
    assert seats.numbered_handle("frontend", ["frontend-1"]) == "frontend-2"
    # …except where the bare handle is already SOMEONE'S, because `frontend-1` is then
    # that seat's alias and cannot point at a second person.
    assert seats.numbered_handle("frontend", ["frontend"]) == "frontend-2"


def test_a_handle_is_never_reused_even_after_the_seat_before_it_is_gone():
    # Same rule as todo `#N`, and for the same reason: a handle is quoted in message
    # history, so re-pointing one silently rewrites the past. `taken` is every handle
    # ever DECLARED, so the gap left by an earlier seat is not filled.
    assert seats.suggest_handle("frontend", ["frontend", "frontend-3"]) == "frontend-2"
    assert seats.suggest_handle("frontend", ["frontend", "frontend-2", "frontend-3"]) == "frontend-4"


def test_a_multiword_role_type_slugs_into_a_typeable_handle():
    handles, seat_roles = seats.normalise_cast(["project manager", "backend"])
    assert handles == ["project-manager", "backend"]
    assert seat_roles["project-manager"] == "project manager"


def test_a_cast_entry_may_name_its_own_seat():
    handles, seat_roles = seats.normalise_cast(
        [{"role": "frontend", "handle": "sarah"}, "frontend"]
    )
    # A host-named seat still COUNTS as a frontend seat, so the derived one is numbered
    # rather than handed the bare `frontend`.
    assert handles == ["sarah", "frontend-1"]
    assert seat_roles == {"sarah": "frontend", "frontend-1": "frontend"}


def test_a_seat_may_not_be_named_after_a_role_type_that_two_seats_share():
    """The collision, refused at the source rather than resolved afterwards: `@frontend`
    would mean both that one seat and every frontend on the task."""
    with pytest.raises(ValueError) as e:
        seats.normalise_cast([{"role": "frontend", "handle": "frontend"}, "frontend"])
    assert "frontend-1" in str(e.value)


def test_a_seat_may_not_be_named_after_another_seats_role_type():
    """The other shape of one token, two readings — and the reason nothing needs an
    exception for callers that address a DECLARED seat any more."""
    with pytest.raises(ValueError, match="two different things"):
        seats.normalise_cast(
            [{"role": "backend", "handle": "qa"}, {"role": "qa", "handle": "backend"}]
        )


def test_a_seat_keeps_the_bare_role_type_when_it_is_the_only_one_of_its_kind():
    """Every task written before this rule — untouched. With one seat per role type the
    handle and the type are the same string and name the same single seat."""
    handles, seat_roles = seats.normalise_cast(["backend", "frontend", "qa"])
    assert handles == ["backend", "frontend", "qa"]
    assert seat_roles == {"backend": "backend", "frontend": "frontend", "qa": "qa"}


# --------------------------------------------------------------------------- #
# create_task / add_seat
# --------------------------------------------------------------------------- #
def test_two_frontends_and_a_qa_is_now_a_describable_cast(conn):
    task = admin.create_task(
        "big", title="Big", roles=["backend", "backend", "frontend", "frontend", "qa"]
    )
    # A role type declared twice numbers BOTH its seats, so no handle is ever spelled
    # the same way as a type several seats hold. `qa`, declared once, keeps the bare
    # handle — as every pre-split task does.
    assert task["roles"] == ["backend-1", "backend-2", "frontend-1", "frontend-2", "qa"]
    assert task["seat_roles"] == {
        "backend-1": "backend",
        "backend-2": "backend",
        "frontend-1": "frontend",
        "frontend-2": "frontend",
        "qa": "qa",
    }


def test_the_broker_role_is_reserved_as_a_role_type_too(conn):
    with pytest.raises(ValueError, match="reserved"):
        admin.create_task("b1", title="B", roles=[{"role": "broker", "handle": "x"}, "backend"])


def test_add_seat_appends_a_seat_and_mints_its_invite(conn):
    admin.create_task("late", title="Late", roles=["backend", "frontend"])
    added = admin.add_seat("late", "qa")

    assert added["seat"] == "qa" and added["role"] == "qa"
    assert seats.handles_of(conn, "late") == ["backend", "frontend", "qa"]
    # Invites are per SEAT, so the code that comes back redeems into exactly this one.
    invite = conn.execute("SELECT role FROM invites WHERE task_id = 'late'").fetchone()
    assert invite["role"] == "qa"


def test_add_seat_derives_a_suffix_for_a_role_type_already_present(conn):
    admin.create_task("more", title="More", roles=["backend", "frontend"])
    assert admin.add_seat("more", "frontend")["seat"] == "frontend-2"


def test_add_seat_never_hands_out_the_bare_role_type_once_a_seat_already_holds_it(conn):
    """Even when the seat already there was named something else — the type is about to
    be shared, so the new seat is numbered rather than named after it."""
    admin.create_task("named", title="Named",
                      roles=["backend", {"role": "frontend", "handle": "fe"}])
    assert admin.add_seat("named", "frontend")["seat"] == "frontend-1"


# --------------------------------------------------------------------------- #
# the one seat a handle CANNOT be renumbered for — and the address it gains
# --------------------------------------------------------------------------- #
def test_a_second_seat_of_a_type_gives_the_first_an_address_without_moving_it(conn):
    """The only path left to a handle spelled like a shared role type: the first seat's
    handle was minted when it was the ONLY frontend, and a handle is immutable — it is
    quoted in message history and signatures. So it stays, and gains an ADDRESS."""
    admin.create_task("grew", title="Grew", roles=["backend", "frontend"])
    admin.add_seat("grew", "frontend")

    cast, roles = seats.cast_of(conn, "grew")
    assert cast == ["backend", "frontend", "frontend-2"], "the stored handle never moved"
    assert seats.seat_aliases(cast, roles) == {"frontend-1": "frontend"}
    # An address, not a rename: it canonicalises straight back to the stored handle, so
    # party lists, quorum and message history keep resolving through that.
    res = service.resolve_role("frontend-1", cast, roles)
    assert res.handles == ["frontend"] and res.canonical == "frontend"


def test_the_alias_exists_only_while_two_seats_share_the_type(conn):
    """It is DERIVED, never stored — so on a one-frontend task there is nothing to
    shadow and `@frontend-1` names nobody."""
    admin.create_task("solo", title="Solo", roles=["backend", "frontend"])
    cast, roles = seats.cast_of(conn, "solo")
    assert seats.seat_aliases(cast, roles) == {}
    assert service.resolve_role("frontend-1", cast, roles) is None


def test_no_alias_is_minted_where_it_would_point_at_two_seats(conn):
    """An address that names two seats is the very problem being fixed."""
    cast = ["frontend", "frontend-1"]
    roles = {"frontend": "frontend", "frontend-1": "frontend"}
    assert seats.seat_aliases(cast, roles) == {}


def test_both_seats_of_a_grown_task_can_be_named_in_a_party_list_while_unjoined(conn):
    """The hole this closes. Before, the first seat was reachable only by the display
    name of whoever joined it — which does not exist until they do."""
    admin.create_task("bind2", title="Bind", roles=["backend", "frontend"])
    admin.add_seat("bind2", "frontend")
    cast, roles = seats.cast_of(conn, "bind2")

    assert service.resolve_seat("frontend-1", cast, roles, task_id="bind2") == "frontend"
    assert service.resolve_seat("frontend-2", cast, roles, task_id="bind2") == "frontend-2"
    # …and the TYPE is still refused, naming both in the form you can type back.
    with pytest.raises(ValueError) as e:
        service.resolve_seat("frontend", cast, roles, task_id="bind2")
    assert "@frontend-1" in str(e.value) and "@frontend-2" in str(e.value)


def test_a_seats_handle_cannot_be_reused(conn):
    admin.create_task("fixed", title="Fixed", roles=["backend", "frontend"])
    with pytest.raises(ValueError, match="never reused"):
        admin.add_seat("fixed", "qa", handle="backend")


def test_a_new_seat_does_not_retroactively_join_existing_todos(conn):
    """``parties_json`` names who AGREED, and a person who was not there did not agree."""
    ag = _cast(conn, "hist", ["backend", "frontend"])
    number = todos.propose_todo(
        conn, ag["backend"], "Login", "POST /login", ["backend", "frontend"]
    )["number"]
    todos.accept_todo(conn, ag["frontend"], number)

    admin.add_seat("hist", "qa")

    row = todos.get_row(conn, "hist", number)
    assert todos.parties_of(row) == ["backend", "frontend"]


# --------------------------------------------------------------------------- #
# resolution: exact handle → role type → tag → agent name
# --------------------------------------------------------------------------- #
# A task that GREW its second frontend (`admin.add_seat`), so the first seat still holds
# the bare handle `frontend` — the shape a cast declared today never has, and the only
# one `seats.seat_aliases` mints an address for.
CAST = ["backend", "frontend", "frontend-2"]
ROLES = {"backend": "backend", "frontend": "frontend", "frontend-2": "frontend"}
NAMES = {"backend": "Tony", "frontend": "Sarah", "frontend-2": "Priya"}


def test_a_handle_resolves_to_exactly_one_seat():
    res = service.resolve_role("frontend-2", CAST, ROLES, NAMES)
    assert res.handles == ["frontend-2"] and res.kind == "handle"


def test_a_role_type_fans_out_to_every_seat_that_holds_it():
    res = service.resolve_role("frontend", CAST, ROLES, NAMES)
    # `frontend` is BOTH a handle and a role type here, and the TYPE wins. It used to be
    # the handle, which made one token mean "both frontends" in a message and "Sarah's
    # seat" in a party list — a distinction no human holds, and one that can bind the
    # wrong person to a contract.
    assert res.kind == "role" and res.handles == ["frontend", "frontend-2"]
    # A role type that is not also a handle fans out the same way.
    cast = ["fe-a", "fe-b", "backend"]
    roles = {"fe-a": "frontend", "fe-b": "frontend", "backend": "backend"}
    assert service.resolve_role("frontend", cast, roles).handles == ["fe-a", "fe-b"]


def test_a_role_type_with_one_seat_still_names_that_seat():
    """The claim that makes the reorder free: on every task written before seats and
    role types were split, a handle and its role type are the SAME string, so
    type-first returns exactly what handle-first returned."""
    res = service.resolve_role("backend", CAST, ROLES, NAMES)
    assert res.handles == ["backend"] and res.canonical == "backend"


def test_a_seat_handle_that_is_not_a_role_type_still_names_exactly_one_seat():
    res = service.resolve_role("frontend-2", CAST, ROLES, NAMES)
    assert res.kind == "handle" and res.handles == ["frontend-2"]


def test_a_token_that_is_a_role_type_AND_an_unrelated_seat_is_refused_both_ways():
    """One token, two honest readings — so neither is chosen. Only reachable when a
    host overrides a derived handle; the derived ones never collide this way."""
    cast = ["qa", "backend"]                      # `qa` here is a BACKEND seat…
    roles = {"qa": "backend", "backend": "qa"}    # …and `backend` does the QA work
    names = {"qa": "Tony", "backend": "Ade"}
    res = service.resolve_role("qa", cast, roles, names)
    assert res.kind == "collision" and set(res.handles) == {"backend", "qa"}
    for fn in (service.resolve_addressee, service.resolve_seat):
        with pytest.raises(ValueError) as e:
            fn("qa", cast, roles, names, task_id="t")
        assert "@qa (Tony)" in str(e.value) and "@backend (Ade)" in str(e.value)


def test_a_tag_expands_to_the_role_type_and_then_fans_out():
    res = service.resolve_role("FE", CAST, ROLES, NAMES)
    assert res.kind == "tag" and res.handles == ["frontend", "frontend-2"]
    assert res.canonical == "frontend"


def test_an_agent_name_resolves_to_the_seat_that_holds_it():
    res = service.resolve_role("priya", CAST, ROLES, NAMES)
    assert res.kind == "name" and res.handles == ["frontend-2"]


def test_a_seat_named_like_a_tag_is_never_shadowed_by_it():
    assert service.resolve_role("be", ["be", "backend"]).handles == ["be"]


def test_two_people_with_one_name_is_refused_and_the_refusal_names_the_seats():
    names = {"backend": "Tony", "frontend": "Sarah", "frontend-2": "Sarah"}
    with pytest.raises(ValueError) as e:
        service.resolve_addressee("sarah", CAST, ROLES, names, task_id="t")
    assert "@frontend-1 (Sarah)" in str(e.value) and "@frontend-2 (Sarah)" in str(e.value)


def test_a_fan_out_is_refused_where_something_BINDS_and_the_refusal_names_the_seats():
    with pytest.raises(ValueError) as e:
        service.resolve_seat("FE", CAST, ROLES, NAMES, task_id="t")
    assert "@frontend-1 (Sarah)" in str(e.value) and "@frontend-2 (Priya)" in str(e.value)


def test_a_refusal_spells_a_shadowed_seat_the_way_it_can_be_TYPED():
    """A refusal that answers "`@frontend` is ambiguous" with "did you mean `@frontend`?"
    is no answer. The candidates it lists are the tokens that resolve."""
    with pytest.raises(ValueError) as e:
        service.resolve_seat("frontend", CAST, ROLES, NAMES, task_id="t")
    assert "@frontend (" not in str(e.value)
    assert service.resolve_seat("frontend-1", CAST, ROLES, NAMES, task_id="t") == "frontend"


# --------------------------------------------------------------------------- #
# the seam: fan-out in a MESSAGE, refusal in a PARTY LIST
# --------------------------------------------------------------------------- #
def _cast(conn, task_id, roles, names=None):
    """Build a real multi-seat task through the real flow, and return {handle: Identity}."""
    task = admin.create_task(task_id, title=task_id, roles=roles)
    out = {}
    for i, seat in enumerate(task["roles"]):
        code, _ = admin.mint_invite(task_id, seat)
        who = (names or {}).get(seat) or f"dev-{i}"
        res = pairing.redeem_invite(conn, code, who)
        out[seat] = resolve_agent_token(conn, res["agent_token"])
    conn.execute("UPDATE agents SET ready = 1, readiness_status = 'passed' WHERE task_id = ?",
                 (task_id,))
    conn.commit()
    return out


def test_FE_in_a_message_reaches_every_frontend_seat(conn):
    ag = _cast(conn, "fan", ["backend", "frontend", "frontend"])

    receipt = service.post_message(conn, ag["backend"], "question", "both of you", to_role="FE")
    # Stored as the ROLE TYPE — that single string is what makes the fan-out work at
    # read time, with no storage change at all.
    assert receipt["to_role"] == "frontend"

    assert len(service.fetch_unacked(conn, ag["frontend-1"])) == 1
    assert len(service.fetch_unacked(conn, ag["frontend-2"])) == 1


def test_a_seat_can_still_be_addressed_alone(conn):
    ag = _cast(conn, "one", ["backend", "frontend", "frontend"])

    receipt = service.post_message(conn, ag["backend"], "question", "just you",
                                   to_role="frontend-2")
    assert receipt["to_role"] == "frontend-2"
    assert len(service.fetch_unacked(conn, ag["frontend-2"])) == 1
    assert service.fetch_unacked(conn, ag["frontend-1"]) == []


def test_FE_in_a_party_list_is_refused_with_both_seats_named(conn):
    ag = _cast(conn, "bind", ["backend", "frontend", "frontend"],
               names={"frontend-1": "Sarah", "frontend-2": "Priya"})

    with pytest.raises(ValueError) as e:
        todos.propose_todo(conn, ag["backend"], "Session refresh", "refresh tokens",
                           ["backend", "FE"])
    msg = str(e.value)
    assert "@frontend-1 (Sarah)" in msg and "@frontend-2 (Priya)" in msg
    assert "A todo binds specific people, so say which." in msg


def test_a_party_list_may_name_a_person_by_their_own_name(conn):
    ag = _cast(conn, "byname", ["backend", "frontend", "frontend"],
               names={"frontend-1": "Sarah", "frontend-2": "Priya"})
    t = todos.propose_todo(conn, ag["backend"], "Sign-in", "POST /login",
                           ["backend", "Priya"])
    assert t["parties"] == ["backend", "frontend-2"]


# --------------------------------------------------------------------------- #
# the owner's ruling: BOTH frontends must sign
# --------------------------------------------------------------------------- #
_SPEC = {
    "version": 1,
    "endpoints": [{"method": "POST", "path": "/api/auth/login"}],
}


def test_both_frontends_must_sign_before_a_contract_locks(conn):
    ag = _cast(conn, "quorum", ["backend", "frontend", "frontend"],
               names={"frontend-1": "Sarah", "frontend-2": "Priya"})
    # The party list names the seats it binds. `frontend` here would be the role TYPE
    # and is refused (see `test_FE_in_a_party_list_is_refused_with_both_seats_named`);
    # every seat of a shared type is numbered, so each has a token of its own to say.
    t = todos.propose_todo(conn, ag["backend"], "Sign-in", "POST /login",
                           ["backend", "frontend-1", "Priya"])
    assert t["parties"] == ["backend", "frontend-1", "frontend-2"]
    number = t["number"]
    todos.accept_todo(conn, ag["frontend-1"], number)
    todos.accept_todo(conn, ag["frontend-2"], number)

    state.propose_contract(conn, ag["backend"], _SPEC, number)
    first = state.lock_contract(conn, ag["backend"], 1, number)
    assert first["locked"] is False and set(first["remaining"]) == {"frontend-1", "frontend-2"}

    second = state.lock_contract(conn, ag["frontend-1"], 1, number)
    assert second["locked"] is False, "one frontend is not both frontends"
    assert second["remaining"] == ["frontend-2"]

    third = state.lock_contract(conn, ag["frontend-2"], 1, number)
    assert third["locked"] is True


def test_a_todo_that_binds_only_one_frontend_locks_without_the_other(conn):
    """The mirror image: a seat the todo does not bind is not in its quorum and does
    not block it. SEATS ≠ PARTICIPANTS, now across two seats of one role type."""
    ag = _cast(conn, "subset", ["backend", "frontend", "frontend"])
    number = todos.propose_todo(
        conn, ag["backend"], "Sign-in", "POST /login", ["backend", "frontend-2"]
    )["number"]
    todos.accept_todo(conn, ag["frontend-2"], number)

    state.propose_contract(conn, ag["backend"], _SPEC, number)
    state.lock_contract(conn, ag["backend"], 1, number)
    assert state.lock_contract(conn, ag["frontend-2"], 1, number)["locked"] is True


def test_each_frontend_records_its_own_acceptance(conn):
    ag = _cast(conn, "accepts", ["backend", "frontend", "frontend"],
               names={"frontend-1": "Sarah", "frontend-2": "Priya"})
    t = todos.propose_todo(conn, ag["backend"], "Sign-in", "POST /login",
                           ["backend", "Sarah", "Priya"])
    assert t["parties"] == ["backend", "frontend-1", "frontend-2"]
    assert t["awaiting"] == ["frontend-1", "frontend-2"]

    todos.accept_todo(conn, ag["frontend-1"], t["number"])
    row = todos.get_row(conn, "accepts", t["number"])
    assert todos.status_of(conn, row) == todos.PENDING, "one of two frontends is not all"

    todos.accept_todo(conn, ag["frontend-2"], t["number"])
    row = todos.get_row(conn, "accepts", t["number"])
    assert todos.status_of(conn, row) == todos.ACCEPTED


# --------------------------------------------------------------------------- #
# the roster — ONE source, and it lists the seats NOBODY has taken
# --------------------------------------------------------------------------- #
def test_the_roster_lists_the_unjoined_seat(conn):
    """The state that silently stalls a task. A roster built only from joined agents
    cannot show it, which is the whole reason the cast is declared rather than inferred."""
    admin.create_task("stall", title="Stall", roles=["backend", "frontend", "frontend", "qa"])
    for seat in ("backend", "frontend-1", "frontend-2"):
        code, _ = admin.mint_invite("stall", seat)
        pairing.redeem_invite(conn, code, f"dev-{seat}")
    admin.mint_invite("stall", "qa")  # invited, never redeemed

    r = seats.roster_summary(conn, "stall")
    assert (r["seats"], r["joined"]) == (4, 3)
    qa = [row for row in r["rows"] if row["seat"] == "qa"][0]
    assert qa["joined"] is False
    assert qa["name"] is None
    assert qa["invite_pending"] is True
    assert qa["role"] == "qa"


def test_the_roster_carries_the_role_type_the_seat_holds(conn):
    _cast(conn, "kinds", ["backend", "frontend", "frontend"])
    rows = {r["seat"]: r for r in seats.roster(conn, "kinds")}
    assert rows["frontend-2"]["role"] == "frontend"
    assert rows["frontend-2"]["joined"] is True
    assert rows["frontend-2"]["readiness_status"] == "passed"


def test_the_roster_never_hides_an_agent_holding_an_undeclared_seat(conn):
    """A roster that hides a participant is worse than an untidy one."""
    seed_task(conn, "odd", roles=("backend", "frontend"))
    seed_agent(conn, "odd", "mobile", "stowaway", "sbk_x", handle="mobile")
    rows = [r["seat"] for r in seats.roster(conn, "odd")]
    assert rows == ["backend", "frontend", "mobile"]
    assert [r for r in seats.roster(conn, "odd") if r["seat"] == "mobile"][0]["declared"] is False


def test_the_agent_facing_roster_and_the_dashboards_read_the_same_source(conn):
    from sys_buddy import api, tools

    _cast(conn, "same", ["backend", "frontend", "frontend"])
    assert tools._op_roster("same") == api._task_detail(conn, "same")["roster"]


# --------------------------------------------------------------------------- #
# every roster row carries a token a human can actually TYPE
# --------------------------------------------------------------------------- #
def test_a_grown_second_seat_gives_the_first_one_an_addressable_token(conn):
    """The defect: on a task that GREW a second frontend, the first seat keeps the bare
    handle `frontend` — and `@frontend` now resolves to the role TYPE. So the roster was
    displaying the one token on that task that names two people, and a party list pasted
    from it is refused."""
    _cast(conn, "grew", ["backend", "frontend"])
    admin.add_seat("grew", "frontend")  # → @frontend-2; @frontend becomes the TYPE

    rows = {r["seat"]: r for r in seats.roster(conn, "grew")}
    # The STORED handle does not move — it is quoted in signatures and message history.
    assert set(rows) == {"backend", "frontend", "frontend-2"}
    # …but every row offers a token that resolves back to exactly it.
    assert rows["frontend"]["address"] == "frontend-1"
    assert rows["frontend-2"]["address"] == "frontend-2"
    assert rows["backend"]["address"] == "backend"

    cast, roles = seats.cast_of(conn, "grew")
    for row in rows.values():
        assert service.resolve_seat(row["address"], cast, roles, task_id="grew") == row["seat"]


def test_the_address_is_the_handle_when_the_cast_declared_both_seats_up_front(conn):
    """A cast declared with two frontends numbers BOTH, so nothing is shadowed and
    nothing is re-spelled."""
    _cast(conn, "declared", ["backend", "frontend", "frontend"])
    rows = {r["seat"]: r for r in seats.roster(conn, "declared")}
    assert {r["seat"]: r["address"] for r in rows.values()} == {
        "backend": "backend", "frontend-1": "frontend-1", "frontend-2": "frontend-2",
    }


def test_a_single_seat_task_is_unchanged(conn):
    _cast(conn, "solo", ["backend", "frontend"])
    for row in seats.roster(conn, "solo"):
        assert row["address"] == row["seat"]


def test_the_cli_roster_prints_the_addressable_token(conn, capsys):
    import argparse

    from sys_buddy import cli
    from sys_buddy.config import get_config

    dbfile = str(get_config().db_path)
    _cast(conn, "cliroster", ["backend", "frontend"])
    admin.add_seat("cliroster", "frontend")
    cli.cmd_task_roster(argparse.Namespace(task="cliroster", db=dbfile))
    out = capsys.readouterr().out
    assert "@frontend-1" in out and "@frontend-2" in out
    # The ambiguous spelling is never offered as a seat's own token.
    assert "@frontend " not in out


def test_the_invite_line_prints_the_addressable_token(conn):
    """`sys-buddy invite <task> frontend-1` opens the shadowed seat — and says so with
    a token the host can type again, not with the handle it is stored under."""
    _cast(conn, "inv", ["backend", "frontend"])
    admin.add_seat("inv", "frontend")
    assert admin.seat_for("inv", "frontend-1") == "frontend-1"
    assert admin.seat_for("inv", "frontend-2") == "frontend-2"


# --------------------------------------------------------------------------- #
# the migration
# --------------------------------------------------------------------------- #
_LEGACY = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, state TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'contract', roles_json TEXT NOT NULL,
    strikes INTEGER NOT NULL DEFAULT 0, same_machine INTEGER NOT NULL DEFAULT 0,
    staging_url TEXT, created_at REAL NOT NULL, closed_at REAL
);
CREATE TABLE agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
    name TEXT NOT NULL, role TEXT NOT NULL, token_hash TEXT, pubkey TEXT,
    created_at REAL NOT NULL, revoked_at REAL
);
CREATE UNIQUE INDEX idx_agents_live_role
    ON agents(task_id, role) WHERE revoked_at IS NULL;
"""


def _legacy_db(tmp_path, name="legacy.db"):
    """A database shaped as it was before seats existed: no ``handle``, no
    ``seat_roles_json``, and the one-live-agent-per-ROLE index still in place."""
    path = tmp_path / name
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(_LEGACY)
    now = time.time()
    c.execute(
        "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
        ("signin", "Sign-in", "open", json.dumps(["backend", "frontend"]), now),
    )
    for role in ("backend", "frontend"):
        c.execute(
            "INSERT INTO agents (task_id, name, role, created_at) VALUES (?,?,?,?)",
            ("signin", f"{role}-dev", role, now),
        )
    # A revoked seat: its historical row must survive, and it must not occupy the seat.
    c.execute(
        "INSERT INTO agents (task_id, name, role, created_at, revoked_at) VALUES (?,?,?,?,?)",
        ("signin", "old-backend", "backend", now, now),
    )
    c.commit()
    c.close()
    return path


def test_the_migration_gives_every_agent_a_handle_and_loses_nobody(tmp_path):
    path = _legacy_db(tmp_path)
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM agents").fetchone()[0]

    db.init_db(path)

    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == before
    assert c.execute("SELECT COUNT(*) FROM agents WHERE handle IS NULL").fetchone()[0] == 0
    assert {r["handle"] for r in c.execute("SELECT handle FROM agents")} == {"backend", "frontend"}
    # The backfill is `handle = role`, which is exactly right: with one seat per role
    # the two ARE the same string, so no existing identity moved.
    assert c.execute("SELECT COUNT(*) FROM agents WHERE handle != role").fetchone()[0] == 0
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


def test_the_migration_swaps_the_role_index_for_the_seat_index(tmp_path):
    path = _legacy_db(tmp_path)
    db.init_db(path)
    c = db.connect(path)
    names = {r["name"] for r in c.execute("PRAGMA index_list(agents)")}
    assert db.AGENT_ROLE_INDEX not in names, "the one-live-agent-per-ROLE rule is gone"
    assert db.AGENT_HANDLE_INDEX in names
    # …and it still constrains: one live agent per SEAT.
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO agents (task_id, name, role, handle, created_at) VALUES (?,?,?,?,?)",
            ("signin", "impostor", "backend", "backend", time.time()),
        )
    c.close()


def test_the_migration_backfills_seat_roles_as_the_identity_map(tmp_path):
    path = _legacy_db(tmp_path)
    db.init_db(path)
    c = db.connect(path)
    row = c.execute("SELECT seat_roles_json FROM tasks WHERE id = 'signin'").fetchone()
    assert json.loads(row["seat_roles_json"]) == {"backend": "backend", "frontend": "frontend"}
    c.close()


def test_a_second_boot_is_a_no_op(tmp_path):
    path = _legacy_db(tmp_path)
    db.init_db(path)
    c = db.connect(path)
    snapshot = [tuple(r) for r in c.execute("SELECT id, handle, role, revoked_at FROM agents")]
    tasks = [tuple(r) for r in c.execute("SELECT id, roles_json, seat_roles_json FROM tasks")]
    c.close()

    db.init_db(path)

    c = db.connect(path)
    assert [tuple(r) for r in c.execute("SELECT id, handle, role, revoked_at FROM agents")] == snapshot
    assert [tuple(r) for r in c.execute("SELECT id, roles_json, seat_roles_json FROM tasks")] == tasks
    c.close()


def test_a_boot_after_a_partial_upgrade_finishes_the_job(tmp_path):
    """The trap the explicit NULL assertion exists for: SQLite treats NULLs as DISTINCT
    in a unique index, so a half-backfilled ``handle`` sails past the constraint and
    leaves those seats unreachable. The migration's trigger is read off the DATA, so a
    boot after a crashed backfill completes it rather than skipping it."""
    path = _legacy_db(tmp_path)
    c = sqlite3.connect(path)
    c.execute("ALTER TABLE agents ADD COLUMN handle TEXT")
    c.execute("UPDATE agents SET handle = role WHERE role = 'backend'")  # died halfway
    c.commit()
    c.close()

    db.init_db(path)

    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM agents WHERE handle IS NULL").fetchone()[0] == 0
    c.close()


def test_the_migration_refuses_a_database_with_two_live_agents_on_one_seat(tmp_path):
    """Refusing loudly beats a bare "UNIQUE constraint failed" that names neither the
    task nor the seat — and beats leaving the constraint off altogether."""
    path = _legacy_db(tmp_path, "dupes.db")
    c = sqlite3.connect(path)
    c.execute("DROP INDEX idx_agents_live_role")
    c.execute(
        "INSERT INTO agents (task_id, name, role, created_at) VALUES (?,?,?,?)",
        ("signin", "second-backend", "backend", time.time()),
    )
    c.commit()
    c.close()

    with pytest.raises(RuntimeError, match="duplicate live seats"):
        db.init_db(path)

    # …and it left the database alone.
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 4
    c.close()


def test_a_pre_split_task_reads_correctly_with_no_migration_of_its_meaning(conn):
    """The claim the whole design rests on: an existing task is ALREADY correct under
    the new reading, because with one seat per role the handle and the role type are
    the same string."""
    seed_task(conn, "old", roles=("backend", "frontend"))
    handles, seat_roles = seats.cast_of(conn, "old")
    assert handles == ["backend", "frontend"]
    assert seat_roles == {"backend": "backend", "frontend": "frontend"}
