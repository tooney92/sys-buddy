"""Verification specs and runs (``docs/enhancements.md`` items 3–4).

The load-bearing tests here are the two that make the feature honest rather than
merely present:

* **the stamp is written by the BROKER** — the party being audited never supplies
  the evidence that says whether his own check is current, and
* **an absolute URL is refused** — the entire mechanical safety story, because the
  testing agent's only base URL is the one the host provided.

Rows are inserted with plain SQL rather than through the deliverable/todo ops on
purpose: this module is about specs and results, and building its fixtures out of
another module's flow would make these tests fail for reasons that have nothing to
do with verification.
"""

from __future__ import annotations

import json
import time

import pytest

from sys_buddy import verification
from sys_buddy.identity import Identity

from tests.conftest import seed_agent, seed_task


# --- fixtures ---------------------------------------------------------------
def engagement_task(conn, task_id="landing", roles=("owner", "frontend", "frontend-2")):
    seed_task(conn, task_id, roles=roles)
    conn.execute("UPDATE tasks SET mode = 'engagement' WHERE id = ?", (task_id,))
    conn.commit()
    return task_id


def seat(conn, task_id, handle, role_type, name=None) -> Identity:
    agent_id = seed_agent(
        conn, task_id, role_type, name or handle, f"sbk_{handle}", handle=handle
    )
    return Identity(agent_id, task_id, name or handle, handle, role_type)


def add_deliverable(conn, task_id, number, text) -> int:
    cur = conn.execute(
        "INSERT INTO deliverables (task_id, number, text, created_at) VALUES (?,?,?,?)",
        (task_id, number, text, time.time()),
    )
    conn.commit()
    return cur.lastrowid


def add_todo(conn, task_id, number, title="work", parties=("frontend",)) -> int:
    cur = conn.execute(
        "INSERT INTO todos (task_id, number, title, scope, parties_json, "
        "proposed_role, created_at) VALUES (?,?,?,?,?,?,?)",
        (task_id, number, title, title, json.dumps(list(parties)), "frontend",
         time.time()),
    )
    conn.commit()
    return cur.lastrowid


def link(conn, todo_id, deliverable_id) -> None:
    conn.execute(
        "INSERT INTO todo_deliverables (todo_id, deliverable_id) VALUES (?,?)",
        (todo_id, deliverable_id),
    )
    conn.commit()


def lock_contract(conn, task_id, todo_id, version) -> int:
    cur = conn.execute(
        "INSERT INTO contracts (task_id, version, spec_json, status, todo_id, "
        "locked_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (task_id, version, "{}", "locked", todo_id, time.time(), time.time()),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def engagement(conn):
    """One engagement, one deliverable, one linked todo whose contract is locked at v1."""
    task_id = engagement_task(conn)
    owner = seat(conn, task_id, "owner", "owner", "Anna")
    james = seat(conn, task_id, "frontend", "frontend", "James")
    john = seat(conn, task_id, "frontend-2", "frontend", "John")
    d1 = add_deliverable(conn, task_id, 1, "Landing page")
    t2 = add_todo(conn, task_id, 2)
    link(conn, t2, d1)
    lock_contract(conn, task_id, t2, 1)
    return {
        "task_id": task_id,
        "owner": owner,
        "james": james,
        "john": john,
        "d1": d1,
        "t2": t2,
    }


# --- the stamp is broker-written, and correct -------------------------------
def test_the_broker_stamps_the_locked_contract_versions(conn, engagement):
    """The dev supplies ONE binding — the deliverable. Everything else is derived."""
    spec = verification.submit_spec(
        conn, engagement["john"], 1, "added 3 buttons", "below the hero on /"
    )
    assert spec["stamped"] == {"2": 1}


def test_the_stamp_covers_every_linked_todo(conn, engagement):
    """deliverable #1 → todos #2 and #5 → their locked versions → {2: 1, 5: 2}."""
    t5 = add_todo(conn, engagement["task_id"], 5)
    link(conn, t5, engagement["d1"])
    lock_contract(conn, engagement["task_id"], t5, 1)
    lock_contract(conn, engagement["task_id"], t5, 2)

    spec = verification.submit_spec(
        conn, engagement["john"], 1, "added 3 buttons", "below the hero"
    )
    assert spec["stamped"] == {"2": 1, "5": 2}


def test_the_dev_cannot_supply_the_stamp(conn, engagement):
    """There is no parameter for it, and that is the point: if the party being audited
    chooses the stamp, the stamp proves nothing."""
    with pytest.raises(TypeError):
        verification.submit_spec(
            conn, engagement["john"], 1, "claim", "how", stamped={"2": 99}
        )


def test_a_deliverable_with_no_linked_todos_stamps_empty_and_reads_unknown(conn):
    """Not 'current' — there is nothing to compare against, and saying current would
    imply a freshness check that never happened."""
    task_id = engagement_task(conn, "orphan")
    dev = seat(conn, task_id, "frontend", "frontend", "James")
    add_deliverable(conn, task_id, 1, "Set up the database")

    spec = verification.submit_spec(conn, dev, 1, "ran the migrations", "see /admin")
    assert spec["stamped"] == {}
    assert spec["staleness"] == verification.UNKNOWN
    assert verification.staleness(conn, spec["id"]) == verification.UNKNOWN
    assert verification.is_stale(conn, spec["id"]) is False


def test_a_todo_with_no_locked_contract_contributes_nothing(conn, engagement):
    """A draft is not an agreement, so there is no version to snapshot."""
    t7 = add_todo(conn, engagement["task_id"], 7)
    link(conn, t7, engagement["d1"])
    conn.execute(
        "INSERT INTO contracts (task_id, version, spec_json, status, todo_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (engagement["task_id"], 1, "{}", "draft", t7, time.time()),
    )
    conn.commit()

    spec = verification.submit_spec(conn, engagement["john"], 1, "claim", "/how")
    assert spec["stamped"] == {"2": 1}


# --- staleness: "the work is broken" vs "the check is out of date" -----------
def test_is_stale_flips_when_a_linked_contract_moves_to_a_new_version(conn, engagement):
    spec = verification.submit_spec(
        conn, engagement["john"], 1, "added 3 buttons", "below the hero"
    )
    assert verification.is_stale(conn, spec["id"]) is False
    assert verification.staleness(conn, spec["id"]) == verification.CURRENT

    # The owner later asks for a fourth button; the contract is renegotiated and
    # re-locked. John's note is now describing an older agreement.
    lock_contract(conn, engagement["task_id"], engagement["t2"], 2)

    assert verification.is_stale(conn, spec["id"]) is True
    assert verification.staleness(conn, spec["id"]) == verification.STALE
    assert verification.specs_for(conn, engagement["d1"])[0]["stale"] is True


def test_a_new_linked_todo_also_makes_a_spec_stale(conn, engagement):
    """Scope arriving under the deliverable is the same class of drift."""
    spec = verification.submit_spec(conn, engagement["john"], 1, "claim", "/how")
    t9 = add_todo(conn, engagement["task_id"], 9)
    link(conn, t9, engagement["d1"])
    lock_contract(conn, engagement["task_id"], t9, 1)

    assert verification.is_stale(conn, spec["id"]) is True


# --- paths, never URLs ------------------------------------------------------
def test_a_plain_path_is_accepted(conn, engagement):
    spec = verification.submit_spec(
        conn,
        engagement["john"],
        1,
        "added 3 buttons to the landing page",
        "they're at /pricing, below the hero — pricing, features, contact",
    )
    assert spec["how"].startswith("they're at /pricing")


def test_an_absolute_url_in_how_is_refused(conn, engagement):
    with pytest.raises(ValueError) as excinfo:
        verification.submit_spec(
            conn, engagement["john"], 1, "added 3 buttons",
            "they're at https://evil.com/pricing",
        )
    assert "'how'" in str(excinfo.value)
    assert "PATHS" in str(excinfo.value)


def test_an_absolute_url_in_claim_is_refused(conn, engagement):
    with pytest.raises(ValueError) as excinfo:
        verification.submit_spec(
            conn, engagement["john"], 1, "shipped https://evil.com", "below the hero"
        )
    assert "'claim'" in str(excinfo.value)


def test_a_refused_url_writes_no_spec(conn, engagement):
    """Refused, never silently stripped — and nothing lands in the table either."""
    with pytest.raises(ValueError):
        verification.submit_spec(
            conn, engagement["john"], 1, "claim", "go to https://evil.com"
        )
    assert verification.specs_for(conn, engagement["d1"]) == []


@pytest.mark.parametrize(
    "text",
    [
        "https://evil.com/pricing",
        "http://evil.com",
        "//evil.com/pricing",
        "www.evil.com",
        "evil.com/pricing",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "FTP://files.example.org",
    ],
)
def test_absolute_targets_are_caught(text):
    assert verification.has_absolute_url(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "they're at /pricing, below the hero",
        "open the home page and scroll",
        "see src/components/Hero.tsx and vite.config.dev",
        "the file is README.md in the repo root",
        "/api/v1/contact accepts a POST",
    ],
)
def test_ordinary_prose_and_paths_are_not_caught(text):
    assert verification.has_absolute_url(text) is False


def test_both_fields_are_reported_in_one_refusal(conn, engagement):
    """Every fix in one shot, like contracts.validate_spec — not one round trip each."""
    errors = verification.validate_claim("see https://a.com", "and //b.com/x")
    assert len(errors) == 2


# --- one spec per dev per deliverable, and both are kept --------------------
def test_two_devs_may_each_leave_a_spec_on_one_deliverable(conn, engagement):
    """They do not conflict: each asserts what THAT dev added, and both are evaluated."""
    verification.submit_spec(conn, engagement["james"], 1, "added the shell", "on /")
    verification.submit_spec(conn, engagement["john"], 1, "added 3 buttons", "below hero")

    specs = verification.specs_for(conn, engagement["d1"])
    assert [(s["role"], s["name"], s["claim"]) for s in specs] == [
        ("frontend", "James", "added the shell"),
        ("frontend-2", "John", "added 3 buttons"),
    ]


def test_the_same_dev_twice_on_one_deliverable_is_refused_with_a_useful_message(
    conn, engagement
):
    verification.submit_spec(conn, engagement["john"], 1, "added 3 buttons", "below hero")
    with pytest.raises(ValueError) as excinfo:
        verification.submit_spec(conn, engagement["john"], 1, "added 4 buttons", "hero")
    message = str(excinfo.value)
    assert "already left a spec" in message
    assert "frontend-2" in message and "#1" in message


def test_one_dev_may_spec_two_different_deliverables(conn, engagement):
    add_deliverable(conn, engagement["task_id"], 2, "Contact form")
    verification.submit_spec(conn, engagement["john"], 1, "added 3 buttons", "below hero")
    verification.submit_spec(conn, engagement["john"], 2, "added the form", "on /contact")
    assert len(verification.specs_for(conn, engagement["d1"])) == 1


def test_an_unknown_deliverable_number_is_refused(conn, engagement):
    with pytest.raises(ValueError, match="no deliverable #9"):
        verification.submit_spec(conn, engagement["john"], 9, "claim", "/how")


def test_a_withdrawn_deliverable_takes_no_spec(conn, engagement):
    conn.execute(
        "UPDATE deliverables SET withdrawn_at = ?, withdraw_reason = ? WHERE id = ?",
        (time.time(), "not this milestone", engagement["d1"]),
    )
    conn.commit()
    with pytest.raises(ValueError, match="withdrawn"):
        verification.submit_spec(conn, engagement["john"], 1, "claim", "/how")


def test_empty_claim_or_how_is_refused(conn, engagement):
    with pytest.raises(ValueError, match="'claim'"):
        verification.submit_spec(conn, engagement["john"], 1, "   ", "/how")
    with pytest.raises(ValueError, match="'how'"):
        verification.submit_spec(conn, engagement["john"], 1, "claim", "")


# --- engagement mode only ---------------------------------------------------
def test_a_peer_task_never_reaches_verification(conn):
    task_id = seed_task(conn, "signin")
    dev = seat(conn, task_id, "frontend", "frontend", "James")
    with pytest.raises(ValueError, match="engagement"):
        verification.submit_spec(conn, dev, 1, "claim", "/how")
    with pytest.raises(ValueError, match="engagement"):
        verification.start_run(conn, dev, "https://staging.example.com")


# --- runs -------------------------------------------------------------------
def test_the_owner_starts_a_run_and_the_target_is_recorded(conn, engagement):
    run_id = verification.start_run(
        conn, engagement["owner"], "https://random-app.vercel.dev"
    )
    run = verification.latest_run(conn, engagement["task_id"])
    assert run["id"] == run_id
    # Disclosure, not permission: a record that does not say where it looked is
    # unfalsifiable, so the value is stored verbatim.
    assert run["staging_url"] == "https://random-app.vercel.dev"
    assert run["by"] == "owner"


def test_a_dev_cannot_start_a_run(conn, engagement):
    """A run started by the party being audited proves nothing."""
    with pytest.raises(ValueError, match="only the owner"):
        verification.start_run(conn, engagement["john"], "https://staging.example.com")


def test_a_run_with_no_target_is_allowed(conn, engagement):
    run_id = verification.start_run(conn, engagement["owner"], None)
    assert verification.latest_run(conn, engagement["task_id"])["staging_url"] is None
    assert isinstance(run_id, int)


def test_every_run_is_appended_never_replaced(conn, engagement):
    """'You said it was done and it wasn't, twice' is the evidence an owner needs."""
    first = verification.start_run(conn, engagement["owner"], "https://s.example.com")
    verification.record_result(
        conn, first, engagement["d1"], "rejected", "verified", "found 2 buttons"
    )
    second = verification.start_run(conn, engagement["owner"], "https://s.example.com")
    verification.record_result(
        conn, second, engagement["d1"], "accepted", "verified", "all 3 present"
    )

    rows = conn.execute("SELECT COUNT(*) AS n FROM verification_results").fetchone()
    assert rows["n"] == 2

    latest = verification.latest_results(conn, engagement["task_id"])
    assert latest["run"]["id"] == second
    assert [r["verdict"] for r in latest["by_deliverable"][engagement["d1"]]] == [
        "accepted"
    ]


# --- results ----------------------------------------------------------------
def test_a_result_can_judge_one_devs_claim(conn, engagement):
    james = verification.submit_spec(
        conn, engagement["james"], 1, "added the shell", "on /"
    )
    john = verification.submit_spec(
        conn, engagement["john"], 1, "added 3 buttons", "below hero"
    )
    run_id = verification.start_run(conn, engagement["owner"], "https://s.example.com")
    verification.record_result(
        conn, run_id, engagement["d1"], "accepted", "verified", None, spec_id=james["id"]
    )
    verification.record_result(
        conn, run_id, engagement["d1"], "rejected", "verified", "found 2",
        spec_id=john["id"],
    )

    results = verification.latest_results(conn, engagement["task_id"])
    per_spec = {
        r["spec_id"]: r["verdict"] for r in results["by_deliverable"][engagement["d1"]]
    }
    assert per_spec == {james["id"]: "accepted", john["id"]: "rejected"}


@pytest.mark.parametrize("strength", ["verified", "evidence", "not_checked"])
def test_every_strength_is_accepted(conn, engagement, strength):
    run_id = verification.start_run(conn, engagement["owner"], None)
    result = verification.record_result(
        conn, run_id, engagement["d1"], "accepted", strength, None
    )
    assert result["strength"] == strength
    assert result["strength_label"] == verification.STRENGTH_LABELS[strength]


def test_a_bad_strength_is_refused_and_the_three_are_named(conn, engagement):
    run_id = verification.start_run(conn, engagement["owner"], None)
    with pytest.raises(ValueError) as excinfo:
        verification.record_result(
            conn, run_id, engagement["d1"], "accepted", "probably_fine", "looks ok"
        )
    message = str(excinfo.value)
    assert "'strength'" in message and "probably_fine" in message
    for strength in verification.STRENGTHS:
        assert strength in message


def test_a_bad_verdict_is_refused(conn, engagement):
    run_id = verification.start_run(conn, engagement["owner"], None)
    with pytest.raises(ValueError, match="'verdict'"):
        verification.record_result(
            conn, run_id, engagement["d1"], "sort_of", "verified", None
        )


def test_a_bad_verdict_and_a_bad_strength_come_back_together(conn, engagement):
    run_id = verification.start_run(conn, engagement["owner"], None)
    with pytest.raises(ValueError) as excinfo:
        verification.record_result(conn, run_id, engagement["d1"], "nope", "maybe", None)
    assert "'verdict'" in str(excinfo.value) and "'strength'" in str(excinfo.value)


def test_a_spec_from_another_deliverable_cannot_be_judged_here(conn, engagement):
    d2 = add_deliverable(conn, engagement["task_id"], 2, "Contact form")
    spec = verification.submit_spec(conn, engagement["john"], 1, "buttons", "below hero")
    run_id = verification.start_run(conn, engagement["owner"], None)
    with pytest.raises(ValueError, match="claim about deliverable"):
        verification.record_result(
            conn, run_id, d2, "accepted", "verified", None, spec_id=spec["id"]
        )


def test_one_verdict_per_claim_per_run(conn, engagement):
    """The NULL-distinct trap: the schema deliberately has no unique index here, so
    the domain layer is the only thing standing between a run and two answers."""
    run_id = verification.start_run(conn, engagement["owner"], None)
    verification.record_result(conn, run_id, engagement["d1"], "accepted", "verified", None)
    with pytest.raises(ValueError, match="already recorded"):
        verification.record_result(
            conn, run_id, engagement["d1"], "rejected", "verified", None
        )


def test_a_deliverable_from_another_task_is_refused(conn, engagement):
    other = engagement_task(conn, "other", roles=("owner",))
    stray = add_deliverable(conn, other, 1, "Something else")
    run_id = verification.start_run(conn, engagement["owner"], None)
    with pytest.raises(ValueError, match="not to this run's task"):
        verification.record_result(conn, run_id, stray, "accepted", "verified", None)


def test_latest_results_is_empty_before_anyone_checks(conn, engagement):
    assert verification.latest_results(conn, engagement["task_id"]) == {
        "run": None,
        "by_deliverable": {},
    }


# --- coverage: two counts, both mechanical ----------------------------------
def test_coverage_counts_a_deliverable_with_no_spec(conn, engagement):
    add_deliverable(conn, engagement["task_id"], 2, "Contact form")
    add_deliverable(conn, engagement["task_id"], 3, "Pricing page")
    add_deliverable(conn, engagement["task_id"], 4, "Set up the database")
    for number in (1, 2, 3):
        verification.submit_spec(conn, engagement["james"], number, "built it", "/there")

    assert verification.coverage(conn, engagement["task_id"]) == {
        "deliverables": 4,
        "with_spec": 3,   # #4: nobody left a check
        "with_result": 0,  # nothing has run
    }


def test_coverage_counts_a_deliverable_with_no_result(conn, engagement):
    """'Everything passed' must not be able to hide a deliverable nobody looked at."""
    d2 = add_deliverable(conn, engagement["task_id"], 2, "Contact form")
    verification.submit_spec(conn, engagement["james"], 1, "built it", "/there")
    verification.submit_spec(conn, engagement["james"], 2, "built it", "/contact")
    run_id = verification.start_run(conn, engagement["owner"], "https://s.example.com")
    verification.record_result(conn, run_id, engagement["d1"], "accepted", "verified", None)

    assert verification.coverage(conn, engagement["task_id"]) == {
        "deliverables": 2,
        "with_spec": 2,
        "with_result": 1,
    }
    assert d2 not in verification.latest_results(conn, engagement["task_id"])[
        "by_deliverable"
    ]


def test_two_specs_on_one_deliverable_count_once(conn, engagement):
    """Coverage measures PRESENCE per deliverable, not how many people wrote notes."""
    verification.submit_spec(conn, engagement["james"], 1, "added the shell", "on /")
    verification.submit_spec(conn, engagement["john"], 1, "added 3 buttons", "below hero")
    assert verification.coverage(conn, engagement["task_id"])["with_spec"] == 1


def test_coverage_counts_only_the_latest_run(conn, engagement):
    """A run covers everything, so results from an older run describe another build."""
    add_deliverable(conn, engagement["task_id"], 2, "Contact form")
    d2 = conn.execute(
        "SELECT id FROM deliverables WHERE task_id = ? AND number = 2",
        (engagement["task_id"],),
    ).fetchone()["id"]

    first = verification.start_run(conn, engagement["owner"], None)
    verification.record_result(conn, first, engagement["d1"], "accepted", "verified", None)
    verification.record_result(conn, first, d2, "accepted", "verified", None)
    assert verification.coverage(conn, engagement["task_id"])["with_result"] == 2

    verification.start_run(conn, engagement["owner"], None)
    assert verification.coverage(conn, engagement["task_id"])["with_result"] == 0


def test_a_withdrawn_deliverable_leaves_coverage(conn, engagement):
    d2 = add_deliverable(conn, engagement["task_id"], 2, "Contact form")
    conn.execute(
        "UPDATE deliverables SET withdrawn_at = ? WHERE id = ?", (time.time(), d2)
    )
    conn.commit()
    assert verification.coverage(conn, engagement["task_id"])["deliverables"] == 1


def test_coverage_on_an_engagement_with_nothing_agreed_yet(conn):
    task_id = engagement_task(conn, "fresh", roles=("owner",))
    assert verification.coverage(conn, task_id) == {
        "deliverables": 0,
        "with_spec": 0,
        "with_result": 0,
    }


# --- the event log ----------------------------------------------------------
def test_every_run_and_result_lands_in_the_event_log(conn, engagement):
    verification.submit_spec(conn, engagement["john"], 1, "added 3 buttons", "below hero")
    run_id = verification.start_run(conn, engagement["owner"], "https://s.example.com")
    verification.record_result(conn, run_id, engagement["d1"], "accepted", "verified", None)

    rows = conn.execute(
        "SELECT detail_json FROM events WHERE kind = 'verification' ORDER BY id"
    ).fetchall()
    actions = [json.loads(row["detail_json"])["action"] for row in rows]
    assert actions == ["spec_submitted", "run_started", "result_recorded"]
