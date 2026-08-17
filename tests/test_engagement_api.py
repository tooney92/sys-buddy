"""Specs for engagement mode on the read-only dashboard API.

Three invariants carry this file, and they are the same three the todo payload is held
to — plus one that is new here:

* **Backwards compatibility.** A ``contract`` or ``debug`` task must serialise EXACTLY
  as it did before engagement mode existed. The engagement keys are ABSENT, not empty,
  which is what keeps a deployed ``ui.html`` (served from disk, so possibly older than
  the running ``api.py``) working across a restart.
* **The dashboard refreshes on the moments that matter.** A builder accepting the scope
  and a verification result landing touch no message, event, contract or todo — so if
  they do not move the detail change token, the panel sits stale through precisely the
  two changes it exists to show.
* **The answer key never ships.** Guideline rules are stored with the ``must_mention``
  keys a correct assessment answer must contain. The dashboard is behind a read-scoped
  viewer token; publishing those keys would turn the assessment into a reading exercise.
  ``tests/test_guidelines.py`` makes that claim at the domain layer, and this file makes
  it again at the layer that actually puts bytes on the wire.

Like ``tests/test_api.py`` and ``tests/test_todos_api.py`` these drive the ``_``-prefixed
query helpers directly (they take an open connection), so no HTTP server is needed.
"""

from __future__ import annotations

import json
import time

import pytest

from sys_buddy import api, deliverables, guidelines, verification
from sys_buddy.identity import Identity, resolve_viewer_token
from tests.conftest import seed_agent, seed_task, seed_viewer


# --------------------------------------------------------------------------- #
# seed helpers
# --------------------------------------------------------------------------- #
def _engagement(conn, task_id="acme", roles=("backend", "frontend", "owner")):
    """An engagement task with its seats filled. Returns ``{handle: Identity}``.

    Seats are filled BACKEND FIRST on purpose: the host is identified positionally as
    the earliest agent row on the task (``guidelines._host_row``), and a dev hosting an
    engagement he did not commission is the normal case.
    """
    seed_task(conn, task_id, roles=tuple(roles))
    conn.execute("UPDATE tasks SET mode = 'engagement' WHERE id = ?", (task_id,))
    conn.commit()
    return {
        handle: Identity(
            agent_id=seed_agent(
                conn, task_id, handle, f"{handle}-agent", f"sbk_{handle}", handle=handle
            ),
            task_id=task_id,
            name=f"{handle}-agent",
            role=handle,
            role_type=handle,
        )
        for handle in roles
    }


def _scope(conn, ag, *, lock=True):
    """The owner's three-item list, accepted by both builders unless ``lock=False``."""
    rec = deliverables.propose_deliverables(
        conn,
        ag["owner"],
        [
            "a landing page with four buttons",
            "a contact form that emails me",
            "the site works on a phone",
        ],
    )
    if lock:
        deliverables.accept_deliverables(conn, ag["backend"])
        rec = deliverables.accept_deliverables(conn, ag["frontend"])
    return rec


def _todo(conn, task_id, number, title="the landing page", state="verified"):
    """A raw todo row — this file is about the payload, not the todo flow.

    Defaults to `verified` because that is the state an engagement is in by the time a
    verification run exists: the builders finish first, and only then does the owner go
    and look. A run is refused while any linked todo is still unfinished.
    """
    cur = conn.execute(
        "INSERT INTO todos (task_id, number, title, scope, parties_json, proposed_role, "
        "state, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, number, title, title, json.dumps(["frontend"]), "frontend", state,
         time.time()),
    )
    conn.commit()
    return cur.lastrowid


def _claim_everything(conn, task_id="acme"):
    """Raise a finished todo against EVERY deliverable.

    A verification run is refused while any live deliverable is unclaimed (no todo means
    no contract, so nothing was agreed about how it gets built) or while a linked todo is
    unfinished. Tests that just need to REACH a run want this; the tests about those two
    refusals live in `tests/test_verification.py` and build their own state.
    """
    unclaimed = conn.execute(
        """
        SELECT d.id, d.number FROM deliverables d
         WHERE d.task_id = ? AND d.withdrawn_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM todo_deliverables td WHERE td.deliverable_id = d.id)
        """,
        (task_id,),
    ).fetchall()
    for row in unclaimed:
        t = _todo(conn, task_id, 100 + row["number"], f"work {row['number']}")
        conn.execute(
            "INSERT INTO todo_deliverables (todo_id, deliverable_id) VALUES (?,?)",
            (t, row["id"]),
        )
    conn.commit()


def _viewer(conn, token="sbv_host"):
    seed_viewer(conn, "host", token, task_id=None)
    return resolve_viewer_token(conn, token)


def _detail_token(conn, task_id="acme", token="sbv_host"):
    _, tokens = api._change_tokens(conn, resolve_viewer_token(conn, token))
    return tokens[task_id]


# --------------------------------------------------------------------------- #
# the regression that protects the live dashboard
# --------------------------------------------------------------------------- #
def test_non_engagement_task_serialises_exactly_as_before(conn):
    """A `contract` task's payload is byte-identical: the engagement keys are ABSENT.

    Same claim, and the same key set, as
    ``test_todos_api.py::test_task_with_no_todos_serialises_exactly_as_before`` — an
    older ``ui.html`` reading this must see nothing new at all. Absent rather than
    empty is the whole convention: ``"deliverables": {"rows": []}`` on a task that can
    never have deliverables would invite a page to render an empty owner panel over a
    task that has no owner.
    """
    seed_task(conn, "signin", roles=("backend", "frontend"))
    detail = api._task_detail(conn, "signin")

    assert set(detail) == {
        "id", "title", "state", "mode", "roles", "seat_roles", "strikes", "times",
        "contract", "messages", "events", "agents", "roster", "readiness_preview",
        "staging_url", "dev_url",
    }
    for key in ("engagement", "deliverables", "guidelines", "verification"):
        assert key not in detail


def test_non_engagement_task_with_todos_is_untouched(conn):
    """The todo payload is unchanged too — engagement adds, it never rewrites."""
    ag = _engagement(conn, "signin", roles=("backend", "frontend"))
    conn.execute("UPDATE tasks SET mode = 'contract' WHERE id = 'signin'")
    conn.commit()
    _todo(conn, "signin", 1)

    detail = api._task_detail(conn, "signin")
    assert detail["mode"] == "contract"
    assert [t["number"] for t in detail["todos"]] == [1]
    assert "engagement" not in detail
    assert "deliverables" not in detail
    assert ag  # seats exist; the mode is what decides, not the cast


def test_a_contract_tasks_change_token_carries_no_engagement_key(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    _viewer(conn)
    assert "engagement" not in json.loads(_detail_token(conn, "signin"))


# --------------------------------------------------------------------------- #
# the payload the dashboard is built against
# --------------------------------------------------------------------------- #
def test_engagement_task_carries_the_full_shape(conn):
    ag = _engagement(conn)
    _scope(conn, ag)
    todo_id = _todo(conn, "acme", 1)
    deliverables.link_todo(conn, todo_id, [1])
    spec = verification.submit_spec(
        conn, ag["frontend"], 1, "added the four buttons", "the landing page hero"
    )
    _claim_everything(conn)
    run_id = verification.start_run(conn, ag["owner"], "https://staging.example.com")
    verification.record_result(
        conn, run_id, spec["deliverable_id"], "accepted", "verified", "clicked all four"
    )

    detail = api._task_detail(conn, "acme")

    assert detail["engagement"] is True
    assert set(detail["deliverables"]) == {
        "locked", "version", "accepted_by", "awaiting", "rows"
    }
    assert detail["deliverables"]["locked"] is True
    assert detail["deliverables"]["version"] == 1
    assert detail["deliverables"]["accepted_by"] == ["backend", "frontend"]
    assert detail["deliverables"]["awaiting"] == []

    rows = detail["deliverables"]["rows"]
    assert [r["number"] for r in rows] == [1, 2, 3]
    assert set(rows[0]) == {
        "id", "number", "text", "withdrawn", "withdraw_reason", "todos", "specs", "result"
    }
    assert rows[0]["text"] == "a landing page with four buttons"
    assert rows[0]["withdrawn"] is False
    assert rows[0]["withdraw_reason"] is None
    # The two registers of one record: the owner's outcome, and the team's work for it.
    assert rows[0]["todos"] == [1]
    # Every live deliverable is claimed here because an UNCLAIMED one blocks the run —
    # no todo means no contract, so nothing was agreed about how it gets built. The
    # empty-todos rendering is asserted below, on a task with no run.
    assert rows[1]["todos"] == [102]

    assert set(rows[0]["specs"][0]) == {"role", "name", "claim", "how", "staleness"}
    assert rows[0]["specs"][0]["role"] == "frontend"
    assert rows[0]["specs"][0]["name"] == "frontend-agent"
    assert rows[0]["specs"][0]["claim"] == "added the four buttons"
    assert rows[0]["specs"][0]["staleness"] in {"current", "stale", "unknown"}
    assert rows[1]["specs"] == []

    assert set(rows[0]["result"]) == {"verdict", "strength", "strength_label", "detail"}
    assert rows[0]["result"]["verdict"] == "accepted"
    assert rows[0]["result"]["strength"] == "verified"
    assert rows[0]["result"]["strength_label"] == verification.STRENGTH_LABELS["verified"]
    assert rows[0]["result"]["detail"] == "clicked all four"
    # Nobody looked at #2 and #3, and that is said out loud rather than left blank-ish.
    assert rows[1]["result"] is None

    assert set(detail["verification"]) == {"run", "coverage"}
    assert detail["verification"]["run"]["id"] == run_id
    assert detail["verification"]["run"]["staging_url"] == "https://staging.example.com"
    assert detail["verification"]["run"]["by"] == "owner"
    # Presence, not quality: three asked for, one has a spec, one got a result back.
    assert detail["verification"]["coverage"] == {
        "deliverables": 3, "with_spec": 1, "with_result": 1
    }


def test_an_unaccepted_list_shows_who_is_still_awaited(conn):
    ag = _engagement(conn)
    _scope(conn, ag, lock=False)
    deliverables.accept_deliverables(conn, ag["backend"])

    scope = api._task_detail(conn, "acme")["deliverables"]
    assert scope["locked"] is False
    assert scope["accepted_by"] == ["backend"]
    assert scope["awaiting"] == ["frontend"]


def test_an_engagement_with_no_deliverables_yet_still_carries_the_keys(conn):
    """The panel exists from the moment the task does — it is how the owner is asked."""
    _engagement(conn)
    detail = api._task_detail(conn, "acme")

    assert detail["engagement"] is True
    assert detail["deliverables"]["rows"] == []
    assert detail["deliverables"]["locked"] is False
    assert detail["deliverables"]["version"] is None
    assert detail["verification"] == {
        "run": None,
        "coverage": {"deliverables": 0, "with_spec": 0, "with_result": 0},
    }


def test_a_withdrawn_deliverable_stays_visible_and_says_why(conn):
    ag = _engagement(conn)
    _scope(conn, ag)
    deliverables.withdraw_deliverable(conn, ag["owner"], 3, "doing that next quarter")

    rows = api._task_detail(conn, "acme")["deliverables"]["rows"]
    assert [r["number"] for r in rows] == [1, 2, 3]
    assert rows[2]["withdrawn"] is True
    assert rows[2]["withdraw_reason"] == "doing that next quarter"
    # Scope shrank, so the denominator does too.
    assert api._task_detail(conn, "acme")["verification"]["coverage"]["deliverables"] == 2


def test_the_deliverable_level_verdict_wins_over_the_per_claim_ones(conn):
    """A run files both kinds of row; the owner's question is answered by the whole."""
    ag = _engagement(conn)
    _scope(conn, ag)
    james = verification.submit_spec(conn, ag["frontend"], 1, "added the shell", "the page")
    john = verification.submit_spec(conn, ag["backend"], 1, "added 3 buttons", "below hero")
    _claim_everything(conn)
    run_id = verification.start_run(conn, ag["owner"], None)
    verification.record_result(
        conn, run_id, james["deliverable_id"], "accepted", "verified", None,
        spec_id=james["id"],
    )
    verification.record_result(
        conn, run_id, john["deliverable_id"], "rejected", "verified", "found 2",
        spec_id=john["id"],
    )
    verification.record_result(
        conn, run_id, james["deliverable_id"], "rejected", "verified", "one button missing"
    )

    result = api._task_detail(conn, "acme")["deliverables"]["rows"][0]["result"]
    assert result["verdict"] == "rejected"
    assert result["detail"] == "one button missing"


def test_per_claim_verdicts_alone_still_show_a_result(conn):
    """A run that never filed a whole-deliverable row must not read as "not checked"."""
    ag = _engagement(conn)
    _scope(conn, ag)
    spec = verification.submit_spec(conn, ag["frontend"], 2, "wired the form", "the form")
    _claim_everything(conn)
    run_id = verification.start_run(conn, ag["owner"], None)
    verification.record_result(
        conn, run_id, spec["deliverable_id"], "rejected", "evidence", "no email sent",
        spec_id=spec["id"],
    )

    result = api._task_detail(conn, "acme")["deliverables"]["rows"][1]["result"]
    assert result["verdict"] == "rejected"
    assert result["strength_label"] == verification.STRENGTH_LABELS["evidence"]


def test_only_the_latest_run_is_shown(conn):
    """Every run checks everything, so the latest run IS the answer — no stale ticks."""
    ag = _engagement(conn)
    _scope(conn, ag)
    _claim_everything(conn)
    first = verification.start_run(conn, ag["owner"], None)
    d1 = api._task_detail(conn, "acme")["deliverables"]["rows"][0]["id"]
    verification.record_result(conn, first, d1, "rejected", "verified", "nothing there")
    _claim_everything(conn)
    second = verification.start_run(conn, ag["owner"], None)
    verification.record_result(conn, second, d1, "accepted", "verified", "all four")

    detail = api._task_detail(conn, "acme")
    assert detail["verification"]["run"]["id"] == second
    assert detail["deliverables"]["rows"][0]["result"]["verdict"] == "accepted"


# --------------------------------------------------------------------------- #
# guidelines — present when set, absent when not, answer key never
# --------------------------------------------------------------------------- #
def test_guidelines_key_is_absent_when_the_task_declares_none(conn):
    ag = _engagement(conn)
    _scope(conn, ag)
    detail = api._task_detail(conn, "acme")

    assert detail["engagement"] is True
    assert "guidelines" not in detail


def test_guidelines_carry_only_the_rule_text(conn):
    ag = _engagement(conn)
    guidelines.set_guidelines(
        conn,
        ag["backend"],  # the host — the first seat filled on the task
        "frontend",
        [
            {"rule": "Tailwind only, no inline styles", "must_mention": ["tailwind", "inline"]},
            {"rule": "Every input has a label", "must_mention": ["label"]},
        ],
    )
    detail = api._task_detail(conn, "acme")

    assert detail["guidelines"] == {
        "frontend": [
            {"rule": "Tailwind only, no inline styles"},
            {"rule": "Every input has a label"},
        ]
    }


def test_must_mention_never_appears_anywhere_in_the_payload(conn):
    """The answer key does not ship — not in the rules, not in a token, not anywhere.

    Asserted over the SERIALISED payload rather than the rules dict, because a leak of
    an answer key is a leak wherever it surfaces, and the point of this check is to
    catch a future field that carries it somewhere nobody thought to look.
    """
    ag = _engagement(conn)
    _scope(conn, ag)
    guidelines.set_guidelines(
        conn, ag["backend"], "frontend",
        [{"rule": "Tailwind only, no inline styles", "must_mention": ["tailwind", "inline"]}],
    )
    _viewer(conn)

    wire = json.dumps(api._task_detail(conn, "acme"))
    assert "must_mention" not in wire
    assert "tailwind" not in wire.lower().replace("tailwind only, no inline styles", "")

    token = _detail_token(conn)
    assert "must_mention" not in token


# --------------------------------------------------------------------------- #
# the change token — the panel must not sit stale
# --------------------------------------------------------------------------- #
def test_the_change_token_moves_when_a_builder_accepts(conn):
    ag = _engagement(conn)
    _scope(conn, ag, lock=False)
    _viewer(conn)

    before = _detail_token(conn)
    deliverables.accept_deliverables(conn, ag["backend"])
    after = _detail_token(conn)
    assert after != before

    # …and again on the acceptance that LOCKS the list, which opens the build gate.
    deliverables.accept_deliverables(conn, ag["frontend"])
    assert _detail_token(conn) != after


def test_the_change_token_moves_when_a_result_lands(conn):
    ag = _engagement(conn)
    _scope(conn, ag)
    spec = verification.submit_spec(conn, ag["frontend"], 1, "added the buttons", "the hero")
    _viewer(conn)

    before = _detail_token(conn)
    _claim_everything(conn)
    run_id = verification.start_run(conn, ag["owner"], None)
    started = _detail_token(conn)
    assert started != before, "the run itself is news — the owner is waiting on it"

    verification.record_result(
        conn, run_id, spec["deliverable_id"], "accepted", "verified", "all four"
    )
    assert _detail_token(conn) != started


def test_the_change_token_moves_on_a_spec_and_a_withdrawal(conn):
    ag = _engagement(conn)
    _scope(conn, ag)
    _viewer(conn)

    before = _detail_token(conn)
    verification.submit_spec(conn, ag["frontend"], 1, "added the buttons", "the hero")
    after_spec = _detail_token(conn)
    assert after_spec != before

    deliverables.withdraw_deliverable(conn, ag["owner"], 3, "next quarter")
    assert _detail_token(conn) != after_spec


def test_the_change_token_moves_when_guidelines_change(conn):
    ag = _engagement(conn)
    _viewer(conn)

    before = _detail_token(conn)
    guidelines.set_guidelines(
        conn, ag["backend"], "frontend",
        [{"rule": "Tailwind only", "must_mention": ["tailwind"]}],
    )
    assert _detail_token(conn) != before


# --------------------------------------------------------------------------- #
# scoping — the engagement block follows the same rules as everything else
# --------------------------------------------------------------------------- #
def test_a_buddy_sees_the_engagement_block(conn):
    """Specs and results are not secrets: both sides read the same record.

    The owner's whole reason for having a dashboard is that it shows facts the broker
    computed rather than a summary one agent wrote, and a builder must be able to see
    the verdict filed against his own claim.
    """
    ag = _engagement(conn)
    _scope(conn, ag)
    spec = verification.submit_spec(conn, ag["frontend"], 1, "added the buttons", "the hero")
    _claim_everything(conn)
    run_id = verification.start_run(conn, ag["owner"], None)
    verification.record_result(
        conn, run_id, spec["deliverable_id"], "accepted", "verified", None
    )

    buddy = api._task_detail(conn, "acme", is_host=False)
    host = api._task_detail(conn, "acme", is_host=True)
    assert buddy["engagement"] is True
    assert buddy["deliverables"] == host["deliverables"]
    assert buddy["verification"] == host["verification"]


def test_the_task_list_row_is_untouched(conn):
    """Engagement lands on the DETAIL payload only — the list row keeps its shape."""
    ag = _engagement(conn)
    _scope(conn, ag)
    viewer = _viewer(conn)

    (row,) = api._list_tasks_for(conn, viewer)
    # `closed` joined this set when the list learned to hide closed tasks: it is not a
    # `state`, so the row has to carry it separately. Nothing about ENGAGEMENT is here,
    # which is what this test is actually holding.
    assert set(row) == {
        "id", "title", "state", "mode", "roles", "seat_roles", "last", "strikes", "closed"
    }
    assert row["mode"] == "engagement"


def test_a_deliverable_nobody_has_picked_up_renders_with_no_todos(conn):
    """The empty case, on a task with no verification run.

    It cannot be shown alongside a run any more: an unclaimed deliverable refuses the
    run outright. But the dashboard still has to render the state — "No todo names this
    yet" is how the owner learns nobody has taken his scope on.
    """
    ag = _engagement(conn)
    _scope(conn, ag)
    detail = api._task_detail(conn, "acme")
    assert [r["todos"] for r in detail["deliverables"]["rows"]] == [[], [], []]
    assert detail["verification"]["run"] is None


# --------------------------------------------------------------------------- #
# per-dev attribution: who claimed what, and was his claim true
# --------------------------------------------------------------------------- #
def test_each_devs_claim_carries_its_own_verdict(conn):
    """The attribution the whole spec model exists for.

    `result` on the deliverable answers the owner's question — did I get the thing I
    asked for. THIS answers the other one: who claimed what, and was his claim true. A
    dev who claims work he did not do is caught at his own claim rather than hidden
    inside a deliverable that mostly works.
    """
    ag = _engagement(conn, roles=("frontend", "frontend-2", "owner"))
    deliverables.propose_deliverables(conn, ag["owner"], ["a landing page"])
    deliverables.accept_deliverables(conn, ag["frontend"])
    deliverables.accept_deliverables(conn, ag["frontend-2"])
    todo_id = _todo(conn, "acme", 1)
    deliverables.link_todo(conn, todo_id, [1])

    james = verification.submit_spec(conn, ag["frontend"], 1, "added the shell", "the hero")
    john = verification.submit_spec(conn, ag["frontend-2"], 1, "added 3 buttons", "below it")

    run = verification.start_run(conn, ag["owner"], "https://staging.example.com")
    verification.record_result(conn, run, james["deliverable_id"], "accepted", "verified",
                               "shell renders", spec_id=james["id"])
    verification.record_result(conn, run, john["deliverable_id"], "rejected", "verified",
                               "found 2", spec_id=john["id"])

    specs = api._task_detail(conn, "acme")["deliverables"]["rows"][0]["specs"]
    by_role = {s["role"]: s for s in specs}
    assert by_role["frontend"]["result"]["verdict"] == "accepted"
    assert by_role["frontend-2"]["result"]["verdict"] == "rejected"
    assert by_role["frontend-2"]["result"]["detail"] == "found 2"
    # The label is computed server-side so the page never maps `evidence` itself.
    assert by_role["frontend"]["result"]["strength_label"]


def test_an_unjudged_claim_carries_no_result_key_at_all(conn):
    """ABSENT, never a placeholder. An unmarked claim must not be mistakable for a
    passed one — the same reason `not_checked` is said out loud everywhere else."""
    ag = _engagement(conn)
    _scope(conn, ag)
    todo_id = _todo(conn, "acme", 1)
    deliverables.link_todo(conn, todo_id, [1])
    verification.submit_spec(conn, ag["frontend"], 1, "built it", "on the page")

    specs = api._task_detail(conn, "acme")["deliverables"]["rows"][0]["specs"]
    assert specs[0]["claim"] == "built it"
    assert "result" not in specs[0]
