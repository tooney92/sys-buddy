"""Specs for deliverables — the owner's scope, and the one gate an engagement has.

Five claims are load-bearing here and each has its own test:

* the OWNER authors the list and never accepts it — he wrote the words;
* EVERY builder accepts before the list locks, and one push-back holds all of it;
* a revision mints a new version and earlier acceptances do NOT carry over;
* after the lock scope may only SHRINK — a withdrawal works, an addition is refused
  with the sentence that says why ("start a new engagement for additional work");
* none of it touches a `contract` or `debug` task, whose gate is simply open.
"""

from __future__ import annotations

import pytest

from sys_buddy import deliverables, service
from tests.conftest import seed_agent, seed_task


def _agents(conn, task="acme", roles=("owner", "backend", "frontend"), mode="engagement"):
    """Seed a task (with `mode`) and return {seat: Identity} for each declared seat.

    Seat handle doubles as role type here, which is what `seats.cast_of` falls back to
    for any task that declares one seat per role — so `owner` is the owner seat.
    """
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


def _proposed(conn, ag, task="acme", texts=None):
    """The owner sets a three-item list. Returns the record."""
    texts = texts or [
        "a landing page with four buttons",
        "a contact form that emails me",
        "the site works on a phone",
    ]
    return deliverables.propose_deliverables(conn, ag["owner"], texts)


def _locked(conn, ag, task="acme"):
    """…and every builder accepts it, so the list locks."""
    _proposed(conn, ag, task)
    deliverables.accept_deliverables(conn, ag["backend"])
    return deliverables.accept_deliverables(conn, ag["frontend"])


# --------------------------------------------------------------------------- #
# the owner authors
# --------------------------------------------------------------------------- #
def test_owner_proposes_numbered_deliverables_and_a_v1_list(conn):
    ag = _agents(conn)
    rec = _proposed(conn, ag)

    assert [d["number"] for d in rec["deliverables"]] == [1, 2, 3]
    assert rec["deliverables"][1]["text"] == "a contact form that emails me"
    assert rec["version"] == 1
    assert rec["locked"] is False
    assert rec["owner"] == "owner"
    assert rec["builders"] == ["backend", "frontend"]
    assert rec["awaiting"] == ["backend", "frontend"]


def test_a_builder_cannot_set_the_deliverables(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError) as e:
        deliverables.propose_deliverables(conn, ag["backend"], ["something"])
    assert "only the owner" in str(e.value)


def test_a_deliverable_needs_words(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError):
        deliverables.propose_deliverables(conn, ag["owner"], ["   "])
    with pytest.raises(ValueError):
        deliverables.propose_deliverables(conn, ag["owner"], [])


def test_proposing_twice_is_refused_and_points_at_the_right_tool(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.propose_deliverables(conn, ag["owner"], ["one more thing"])
    assert "add_deliverable" in str(e.value)


# --------------------------------------------------------------------------- #
# engagement mode ONLY — a peer session is untouched
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["contract", "debug"])
def test_a_peer_task_has_no_deliverables_at_all(conn, mode):
    ag = _agents(conn, roles=("owner", "backend", "frontend"), mode=mode)
    with pytest.raises(ValueError) as e:
        deliverables.propose_deliverables(conn, ag["owner"], ["a landing page"])
    assert "ENGAGEMENT mode only" in str(e.value)
    assert f"is a '{mode}' task" in str(e.value)

    with pytest.raises(ValueError):
        deliverables.accept_deliverables(conn, ag["backend"])
    with pytest.raises(ValueError):
        deliverables.add_deliverable(conn, ag["owner"], "more")


def test_the_gate_is_open_on_a_peer_task(conn):
    """`deliverables_locked` reads as "may this task build?" — and a contract task may."""
    _agents(conn, mode="contract")
    assert deliverables.deliverables_locked(conn, "acme") is True
    deliverables.assert_can_build(conn, "acme", "propose a todo")  # no raise


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_an_engagement_is_gated_until_every_builder_accepts(conn):
    ag = _agents(conn)
    assert deliverables.deliverables_locked(conn, "acme") is False
    with pytest.raises(ValueError) as e:
        deliverables.assert_can_build(conn, "acme", "propose a todo")
    assert "no deliverables yet" in str(e.value)

    _proposed(conn, ag)
    assert deliverables.deliverables_locked(conn, "acme") is False

    # ONE builder is not quorum.
    rec = deliverables.accept_deliverables(conn, ag["backend"])
    assert rec["accepted_by"] == ["backend"]
    assert rec["awaiting"] == ["frontend"]
    assert rec["locked"] is False
    assert deliverables.deliverables_locked(conn, "acme") is False
    with pytest.raises(ValueError) as e:
        deliverables.assert_can_build(conn, "acme", "propose a todo")
    assert "@frontend" in str(e.value)

    rec = deliverables.accept_deliverables(conn, ag["frontend"])
    assert rec["locked"] is True
    assert rec["locked_at"] is not None
    assert rec["accepted_by"] == ["backend", "frontend"]
    assert deliverables.deliverables_locked(conn, "acme") is True


def test_the_owner_does_not_accept_his_own_list(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.accept_deliverables(conn, ag["owner"])
    assert "you wrote this list" in str(e.value)
    # …and his non-acceptance is not what is holding the lock.
    deliverables.accept_deliverables(conn, ag["backend"])
    deliverables.accept_deliverables(conn, ag["frontend"])
    assert deliverables.deliverables_locked(conn, "acme") is True


def test_accepting_before_anything_is_proposed_is_refused(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError) as e:
        deliverables.accept_deliverables(conn, ag["backend"])
    assert "no deliverables to accept yet" in str(e.value)


def test_accepting_a_locked_list_is_refused(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.accept_deliverables(conn, ag["backend"])
    assert "already LOCKED" in str(e.value)


# --------------------------------------------------------------------------- #
# push-back
# --------------------------------------------------------------------------- #
def test_a_push_back_names_one_deliverable_and_blocks_the_lock(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    deliverables.accept_deliverables(conn, ag["backend"])
    rec = deliverables.push_back(conn, ag["frontend"], 2, "too vague to check")

    assert rec["locked"] is False
    assert rec["pushed_back_by"]["frontend"] == {
        "deliverable": 2, "reason": "too vague to check"
    }
    assert rec["awaiting"] == ["frontend"]
    assert deliverables.deliverables_locked(conn, "acme") is False


def test_a_push_back_needs_a_reason(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.push_back(conn, ag["frontend"], 2, "  ")
    assert "reason" in str(e.value)


def test_the_owner_cannot_push_back_on_his_own_list(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.push_back(conn, ag["owner"], 1, "hmm")
    assert "you wrote this list" in str(e.value)


def test_push_back_after_the_lock_is_refused(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.push_back(conn, ag["backend"], 1, "changed my mind")
    assert "already LOCKED" in str(e.value)


def test_a_push_back_on_a_deliverable_that_does_not_exist(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.push_back(conn, ag["backend"], 9, "nope")
    assert "no deliverable #9" in str(e.value)


# --------------------------------------------------------------------------- #
# a revision resets the agreement
# --------------------------------------------------------------------------- #
def test_a_revision_mints_a_version_and_clears_every_acceptance(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    deliverables.accept_deliverables(conn, ag["backend"])
    deliverables.push_back(conn, ag["frontend"], 2, "too vague to check")

    rec = deliverables.revise_deliverable(
        conn, ag["owner"], 2, "a contact form that emails me and shows a thank-you"
    )
    assert rec["version"] == 2
    assert rec["accepted_by"] == []          # backend agreed to different words
    assert rec["pushed_back_by"] == {}
    assert rec["awaiting"] == ["backend", "frontend"]
    assert rec["deliverables"][1]["text"].endswith("shows a thank-you")

    # Everyone accepts v2 and only then does it lock.
    deliverables.accept_deliverables(conn, ag["backend"])
    assert deliverables.deliverables_locked(conn, "acme") is False
    deliverables.accept_deliverables(conn, ag["frontend"])
    assert deliverables.deliverables_locked(conn, "acme") is True


def test_only_the_owner_revises(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.revise_deliverable(conn, ag["backend"], 1, "something else")
    assert "only the owner" in str(e.value)


def test_revising_to_the_same_words_is_refused(conn):
    ag = _agents(conn)
    rec = _proposed(conn, ag)
    same = rec["deliverables"][0]["text"]
    with pytest.raises(ValueError) as e:
        deliverables.revise_deliverable(conn, ag["owner"], 1, same)
    assert "already reads" in str(e.value)


def test_revising_after_the_lock_is_refused(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.revise_deliverable(conn, ag["owner"], 1, "five buttons actually")
    assert "locked" in str(e.value)
    assert "withdraw_deliverable" in str(e.value)


# --------------------------------------------------------------------------- #
# before the lock: adding is fine, and it re-opens the agreement
# --------------------------------------------------------------------------- #
def test_adding_before_the_lock_mints_a_version_and_resets_acceptances(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    deliverables.accept_deliverables(conn, ag["backend"])

    rec = deliverables.add_deliverable(conn, ag["owner"], "a privacy policy page")
    assert [d["number"] for d in rec["deliverables"]] == [1, 2, 3, 4]
    assert rec["version"] == 2
    assert rec["accepted_by"] == []
    assert deliverables.deliverables_locked(conn, "acme") is False


def test_a_builder_cannot_add_a_deliverable(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.add_deliverable(conn, ag["frontend"], "also a blog")
    assert "only the owner" in str(e.value)


# --------------------------------------------------------------------------- #
# after the lock: withdraw only, never add
# --------------------------------------------------------------------------- #
def test_adding_after_the_lock_is_refused_and_says_why(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.add_deliverable(conn, ag["owner"], "also a blog")
    msg = str(e.value)
    assert "scope is locked; start a new engagement for additional work" in msg
    # …and nothing was written.
    assert len(deliverables.get_deliverables(conn, "acme")) == 3


def test_withdrawing_after_the_lock_works_and_keeps_the_list_locked(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    rec = deliverables.withdraw_deliverable(
        conn, ag["owner"], 2, "we'll do the form in the next milestone"
    )

    withdrawn = rec["deliverables"][1]
    assert withdrawn["number"] == 2
    assert withdrawn["withdrawn"] is True
    assert withdrawn["withdraw_reason"] == "we'll do the form in the next milestone"
    # Shrinking scope asks nothing new of anyone, so it does NOT reopen the agreement.
    assert rec["locked"] is True
    assert rec["version"] == 1
    assert deliverables.deliverables_locked(conn, "acme") is True
    # And it is still VISIBLE — "I never asked for that" has to stay answerable.
    assert len(deliverables.get_deliverables(conn, "acme")) == 3


def test_only_the_owner_withdraws_and_a_reason_is_required(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    with pytest.raises(ValueError) as e:
        deliverables.withdraw_deliverable(conn, ag["backend"], 1, "don't fancy it")
    assert "only the owner" in str(e.value)
    with pytest.raises(ValueError):
        deliverables.withdraw_deliverable(conn, ag["owner"], 1, "")

    deliverables.withdraw_deliverable(conn, ag["owner"], 1, "ok")
    with pytest.raises(ValueError) as e:
        deliverables.withdraw_deliverable(conn, ag["owner"], 1, "again")
    assert "already withdrawn" in str(e.value)


def test_numbers_are_max_plus_one_and_never_reused(conn):
    ag = _agents(conn)
    _proposed(conn, ag)
    deliverables.withdraw_deliverable(conn, ag["owner"], 2, "not this milestone")
    rec = deliverables.add_deliverable(conn, ag["owner"], "a privacy policy page")
    assert [d["number"] for d in rec["deliverables"]] == [1, 2, 3, 4]
    assert rec["deliverables"][3]["text"] == "a privacy policy page"


# --------------------------------------------------------------------------- #
# the two registers
# --------------------------------------------------------------------------- #
def _todo_id(conn, task="acme", title="Contact endpoint") -> int:
    """A bare todo row — this module only needs its id, not the todo flow."""
    import time

    cur = conn.execute(
        "INSERT INTO todos (task_id, number, title, scope, parties_json, version, state, "
        "proposed_role, created_at) VALUES (?,?,?,?,?,1,'open','backend',?)",
        (task, 1, title, "POST /api/contact", '["backend", "frontend"]', time.time()),
    )
    conn.commit()
    return cur.lastrowid


def test_a_todo_names_the_deliverables_it_serves(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    tid = _todo_id(conn)

    assert deliverables.link_todo(conn, tid, [1, 2]) == [1, 2]
    assert [d["number"] for d in deliverables.deliverables_of_todo(conn, tid)] == [1, 2]
    # Idempotent — re-linking is a no-op, not an error.
    deliverables.link_todo(conn, tid, [2])
    assert len(deliverables.deliverables_of_todo(conn, tid)) == 2
    # …and the link is visible from the owner's side too.
    assert deliverables.get_deliverables(conn, "acme")[0]["todos"] == [1]


def test_internal_work_names_nothing(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    tid = _todo_id(conn, title="CI pipeline")
    assert deliverables.deliverables_of_todo(conn, tid) == []


def test_a_todo_cannot_serve_a_withdrawn_deliverable(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    deliverables.withdraw_deliverable(conn, ag["owner"], 3, "dropping mobile for now")
    tid = _todo_id(conn)
    with pytest.raises(ValueError) as e:
        deliverables.link_todo(conn, tid, [3])
    assert "withdrawn" in str(e.value)


def test_linking_an_unknown_deliverable_is_refused(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    tid = _todo_id(conn)
    with pytest.raises(ValueError) as e:
        deliverables.link_todo(conn, tid, [7])
    assert "no deliverable #7" in str(e.value)


# --------------------------------------------------------------------------- #
# the cast, and the archivable record
# --------------------------------------------------------------------------- #
def test_the_owner_seat_is_a_ROLE_TYPE_not_a_handle(conn):
    """The client's seat may be called anything; what marks it is its role type."""
    seed_task(conn, "acme", roles=("client", "backend"))
    conn.execute(
        "UPDATE tasks SET mode = 'engagement', seat_roles_json = ? WHERE id = 'acme'",
        ('{"client": "owner", "backend": "backend"}',),
    )
    conn.commit()
    assert deliverables.owner_seat(conn, "acme") == "client"
    assert deliverables.builder_seats(conn, "acme") == ["backend"]


def test_an_engagement_with_no_owner_seat_cannot_have_deliverables(conn):
    ag = _agents(conn, roles=("backend", "frontend"))
    with pytest.raises(ValueError) as e:
        deliverables.propose_deliverables(conn, ag["backend"], ["a landing page"])
    assert "declares no owner seat" in str(e.value)


def test_the_record_carries_what_the_owners_receipt_needs(conn):
    ag = _agents(conn)
    rec = _locked(conn, ag)
    assert rec["task_id"] == "acme"
    assert rec["engagement"] is True
    assert rec["accepted_by"] == ["backend", "frontend"]
    assert rec["locked_at"] is not None
    one = rec["deliverables"][0]
    assert one["id"] and one["number"] == 1 and one["created_at"]
    assert one["text"] == "a landing page with four buttons"


def test_every_change_is_in_the_event_log(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    deliverables.withdraw_deliverable(conn, ag["owner"], 2, "next milestone")
    actions = [
        __import__("json").loads(r["detail_json"])["action"]
        for r in conn.execute(
            "SELECT detail_json FROM events WHERE task_id = 'acme' AND kind = 'deliverable' "
            "ORDER BY id"
        ).fetchall()
    ]
    assert actions == [
        "deliverables_proposed",
        "deliverables_accepted",
        "deliverables_accepted",
        "deliverables_locked",
        "deliverable_withdrawn",
    ]
