"""Specs for the ENGAGEMENT TOOL surface — deliverables, guidelines, verification.

Mirrors ``tests/test_todos_tools.py``, and for the same three reasons:

* a tool registered on only one surface (remote token-stamped, or local
  ``task``/``agent``) is a silent capability gap for half the users, so registration is
  checked over BOTH;
* the tool bodies are one-liners over the ``_op_*`` helpers, so the ops are exercised
  end to end and the rules stay where they live — in ``deliverables``, ``guidelines``
  and ``verification``, never re-implemented here;
* the DESCRIPTIONS are the prompt an agent reads before it calls, so the sentences that
  stop the three known mistakes (add-after-lock, a spec that is a script or a URL, a
  strength that quietly reads as a pass) are pinned like any other behaviour.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from sys_buddy import service, tools, verification
from sys_buddy.config import Config
from sys_buddy.middleware import ACTION_TOOLS
from sys_buddy.server import build_server
from tests.conftest import seed_agent, seed_task

# Everything engagement mode adds. Split by the question the readiness gate asks of each
# one — is this an agreement/claim, or is it reading what somebody else agreed?
ENGAGEMENT_WRITES = {
    "propose_deliverables", "add_deliverable", "revise_deliverable",
    "withdraw_deliverable", "accept_deliverables", "push_back",
    "set_guidelines", "submit_spec", "start_verification", "record_verification",
}
ENGAGEMENT_READS = {"get_deliverables", "guidelines_check"}
ENGAGEMENT_TOOLS = ENGAGEMENT_WRITES | ENGAGEMENT_READS | {"submit_guidelines"}

TAILWIND = {
    "rule": "Tailwind only, no inline styles",
    "must_mention": ["tailwind", "inline"],
}

SCOPE = [
    "a landing page with four buttons",
    "a contact form that emails me",
    "the site works on a phone",
]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _agents(conn, task="acme", roles=("backend", "owner", "frontend")):
    """An ENGAGEMENT task and ``{seat: Identity}`` for its cast.

    The seat handle doubles as the role type (one seat per type), so ``owner`` is the
    owner seat. ``backend`` is seeded FIRST on purpose: a dev hosts an engagement, and
    ``guidelines`` identifies the host as the earliest agent row on the task.
    """
    seed_task(conn, task, roles=roles)
    conn.execute("UPDATE tasks SET mode = 'engagement' WHERE id = ?", (task,))
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


def _locked(conn, ag):
    """The owner sets the scope and both builders accept it, so the list locks."""
    tools._op_propose_deliverables(ag["owner"], SCOPE)
    tools._op_accept_deliverables(ag["backend"])
    return tools._op_accept_deliverables(ag["frontend"])


def _claim_everything(conn, ag, task="acme"):
    """Raise a finished todo against EVERY deliverable.

    A verification run is refused while any live deliverable is unclaimed (no todo
    means no contract, so nothing was agreed about how it gets built) or while any
    linked todo is unfinished. Tests that just need to reach a run want this; the tests
    that are ABOUT those two refusals build their own state.
    """
    import json as _json
    import time as _time
    for number in range(1, len(SCOPE) + 1):
        d = conn.execute(
            "SELECT id FROM deliverables WHERE task_id = ? AND number = ?", (task, number)
        ).fetchone()["id"]
        t = conn.execute(
            "INSERT INTO todos (task_id, number, title, scope, parties_json, "
            "proposed_role, state, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (task, number, f"work {number}", "scope", _json.dumps(["frontend"]),
             "frontend", "verified", _time.time()),
        ).lastrowid
        conn.execute(
            "INSERT INTO todo_deliverables (todo_id, deliverable_id) VALUES (?,?)", (t, d)
        )
    conn.commit()


def _schemas(mode, tmp_path) -> dict:
    mcp = FastMCP("t")
    cfg = Config(mode=mode, db_path=tmp_path / f"{mode}.db")
    tools.register_tools(mcp, cfg)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# --------------------------------------------------------------------------- #
# registration: both surfaces, or it doesn't count
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_the_engagement_tools_are_registered_on_both_surfaces(tmp_path, mode):
    assert ENGAGEMENT_TOOLS <= set(_schemas(mode, tmp_path))


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_they_are_reachable_through_a_built_server(tmp_path, mode):
    mcp = build_server(Config(mode=mode, db_path=tmp_path / "s.db"))
    assert ENGAGEMENT_TOOLS <= {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_every_engagement_tool_documents_the_protocol(tmp_path, mode):
    """Docstrings are agent-facing prompt surface here, not developer comments."""
    schemas = _schemas(mode, tmp_path)
    for name in ENGAGEMENT_TOOLS:
        assert len((schemas[name].description or "").strip()) > 120, name


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_the_descriptions_name_the_three_mistakes_agents_actually_make(tmp_path, mode):
    """Each of these is a trap an agent walks into unless the tool says so BEFORE the
    call: scope that cannot grow, a spec that is prose about paths, and a strength that
    a non-technical reader would otherwise read as a pass."""
    # Whitespace-normalised: a description is wrapped prose, so a sentence the agent
    # reads as one line may be two in the source.
    d = {
        n: " ".join((t.description or "").split())
        for n, t in _schemas(mode, tmp_path).items()
    }

    # After the lock the owner may withdraw but NEVER add.
    assert "start a new engagement" in d["add_deliverable"]
    assert "withdraw" in d["add_deliverable"].lower()
    assert "only move left after the lock" in d["withdraw_deliverable"]

    # A push-back names ONE deliverable, and the answer is a new VERSION everyone
    # accepts again.
    assert "one deliverable" in d["push_back"].lower()
    assert "NEW VERSION" in d["push_back"] and "again" in d["push_back"]

    # A spec is PROSE and may only contain paths.
    assert "PROSE, not a script" in d["submit_spec"]
    assert "PATHS ONLY" in d["submit_spec"] and "REFUSED" in d["submit_spec"]

    # All three strengths are spelled out, including the one silence would hide.
    for word in ("verified", "evidence", "not_checked", "silence reads as a pass"):
        assert word in d["record_verification"].lower(), word


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_the_shorthand_table_and_the_tools_stay_in_sync(tmp_path, mode):
    """The vocabulary is one table read by three audiences. Every shorthand a human may
    type must be taught by a tool that is actually registered — a cheatsheet naming a
    tool the broker doesn't answer to is how `upto` went missing from the last one."""
    schemas = _schemas(mode, tmp_path)
    assert [row["code"] for row in tools.ENGAGEMENT_SHORTCODES] == [
        "dl", "dl <text>", "yes dl", "no dl #2 <why>",
    ]
    for row in tools.ENGAGEMENT_SHORTCODES:
        assert row["tools"] and row["desc"], row
        for name in row["tools"]:
            assert name in schemas, name
        taught = [n for n in row["tools"] if row["code"] in (schemas[n].description or "")]
        assert taught, f"nothing teaches `{row['code']}`"


def test_the_engagement_writes_sit_behind_the_pre_flight_gate():
    """Agreeing the scope, setting standards, claiming work and filing a verdict are all
    writes with authority — the same gate propose_contract has. Reading the scope, and
    the guidelines gate itself, stay open."""
    assert ENGAGEMENT_WRITES <= ACTION_TOOLS
    for name in ENGAGEMENT_READS:
        assert name not in ACTION_TOOLS, name
    # A gate behind a gate is a deadlock, and this one buys nothing early: every write
    # above still waits on pre-flight.
    assert "submit_guidelines" not in ACTION_TOOLS


# --------------------------------------------------------------------------- #
# deliverables, end to end through the ops
# --------------------------------------------------------------------------- #
def test_the_owner_sets_the_scope_and_the_builders_lock_it(conn):
    ag = _agents(conn)
    rec = tools._op_propose_deliverables(ag["owner"], SCOPE)
    assert [d["number"] for d in rec["deliverables"]] == [1, 2, 3]
    assert rec["version"] == 1 and rec["locked"] is False
    assert rec["awaiting"] == ["backend", "frontend"]

    assert tools._op_accept_deliverables(ag["backend"])["locked"] is False
    assert tools._op_accept_deliverables(ag["frontend"])["locked"] is True


def test_the_read_is_the_same_record_and_carries_no_internal_ids(conn):
    """One key out, one key in: an agent is handed `number` and nothing it could pass
    back to act on a DIFFERENT deliverable."""
    ag = _agents(conn)
    _locked(conn, ag)
    rec = tools._op_get_deliverables("acme")
    assert rec["locked"] is True and rec["owner"] == "owner"
    assert "list_id" not in rec
    for d in rec["deliverables"]:
        assert "id" not in d
        assert d["specs"] == []


def test_a_push_back_names_one_deliverable_and_the_revision_resets_everyone(conn):
    ag = _agents(conn)
    tools._op_propose_deliverables(ag["owner"], SCOPE)
    tools._op_accept_deliverables(ag["backend"])

    rec = tools._op_push_back(ag["frontend"], 2, "no way to check an email from outside")
    assert rec["pushed_back_by"]["frontend"]["deliverable"] == 2
    assert rec["locked"] is False

    revised = tools._op_revise_deliverable(
        ag["owner"], 2, "a contact form whose submissions show on /admin/messages"
    )
    # A new version, and the earlier acceptance does NOT carry over.
    assert revised["version"] == 2 and revised["accepted_by"] == []
    assert revised["awaiting"] == ["backend", "frontend"]

    tools._op_accept_deliverables(ag["backend"])
    assert tools._op_accept_deliverables(ag["frontend"])["locked"] is True


def test_after_the_lock_scope_may_shrink_but_never_grow(conn):
    ag = _agents(conn)
    _locked(conn, ag)

    with pytest.raises(ValueError) as e:
        tools._op_add_deliverable(ag["owner"], "and a blog")
    assert "start a new engagement for additional work" in str(e.value)

    rec = tools._op_withdraw_deliverable(ag["owner"], 3, "we'll do mobile next month")
    assert rec["locked"] is True  # a withdrawal does not reopen the agreement
    withdrawn = [d for d in rec["deliverables"] if d["withdrawn"]]
    assert [d["number"] for d in withdrawn] == [3]


def test_the_ops_surface_the_brokers_rejections_verbatim(conn):
    """The tool layer adds no rules of its own — it resolves an identity and asks."""
    ag = _agents(conn)
    with pytest.raises(ValueError, match="only the owner"):
        tools._op_propose_deliverables(ag["frontend"], SCOPE)

    tools._op_propose_deliverables(ag["owner"], SCOPE)
    with pytest.raises(ValueError, match="you wrote this list"):
        tools._op_accept_deliverables(ag["owner"])
    with pytest.raises(ValueError, match="no deliverable #9"):
        tools._op_push_back(ag["backend"], 9, "which one?")


def test_a_peer_task_is_untouched_by_any_of_it(conn):
    """A `contract` task has no client, no scope and no gate — every write refuses and
    says why, and the read is simply empty."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    ident = service.Identity(
        agent_id=seed_agent(conn, "signin", "backend", "be", "sbk_be"),
        task_id="signin",
        name="be",
        role="backend",
    )
    assert tools._op_get_deliverables("signin")["deliverables"] == []
    with pytest.raises(ValueError, match="ENGAGEMENT mode only"):
        tools._op_propose_deliverables(ident, ["something"])


# --------------------------------------------------------------------------- #
# guidelines — the second gate, mirroring readiness_check / submit_readiness
# --------------------------------------------------------------------------- #
def test_the_host_sets_standards_and_the_check_hands_back_rules_without_the_answers(conn):
    ag = _agents(conn)
    written = tools._op_set_guidelines(ag["backend"], "frontend", [TAILWIND])
    assert written["role_type"] == "frontend" and written["reassess"] == ["frontend"]

    check = tools._op_guidelines_check(ag["frontend"])
    assert check["required"] is True
    assert [q["id"] for q in check["questions"]] == ["rule-1"]
    assert check["guidelines"] == [{"rule": TAILWIND["rule"]}]
    # `must_mention` IS the answer — an agent that could read it would pass without ever
    # reading the standard, which is the one thing this gate exists to prevent.
    assert "must_mention" not in check["guidelines"][0]
    assert "tailwind" not in str(check["questions"]).lower()


def test_a_role_with_no_standards_has_nothing_to_answer(conn):
    ag = _agents(conn)
    check = tools._op_guidelines_check(ag["frontend"])
    assert check["required"] is False
    assert check["questions"] == [] and check["guidelines"] == []


def test_submitting_guidelines_stamps_its_own_column_and_leaves_pre_flight_alone(conn):
    ag = _agents(conn)
    tools._op_set_guidelines(ag["backend"], "frontend", [TAILWIND])

    failed = tools._op_submit_guidelines(ag["frontend"], {"rule-1": "we use CSS"})
    assert failed["passed"] is False and "#1" in failed["report"]
    row = conn.execute(
        "SELECT ready, guidelines_ready, guidelines_report FROM agents WHERE id = ?",
        (ag["frontend"].agent_id,),
    ).fetchone()
    assert row["guidelines_ready"] == 0 and row["guidelines_report"]

    passed = tools._op_submit_guidelines(
        ag["frontend"], {"rule-1": "style with tailwind classes, never an inline style"}
    )
    assert passed["passed"] is True
    row = conn.execute(
        "SELECT ready, guidelines_ready FROM agents WHERE id = ?",
        (ag["frontend"].agent_id,),
    ).fetchone()
    assert row["guidelines_ready"] == 1
    # Two gates, not one: passing this one does not pre-flight you.
    assert row["ready"] == 0


def test_nobody_sets_standards_for_the_owner_not_even_the_host(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError, match="nobody may set guidelines for the 'owner' role"):
        tools._op_set_guidelines(ag["backend"], "owner", [TAILWIND])
    with pytest.raises(ValueError, match="only the HOST"):
        tools._op_set_guidelines(ag["frontend"], "frontend", [TAILWIND])


# --------------------------------------------------------------------------- #
# specs and verification runs
# --------------------------------------------------------------------------- #
def test_a_dev_leaves_a_claim_and_gets_back_a_spec_id_not_a_row_id(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    spec = tools._op_submit_spec(
        ag["frontend"], 1, "added 3 buttons to the landing page",
        "below the hero on /, pricing / features / contact",
    )
    assert spec["deliverable"] == 1 and "deliverable_id" not in spec
    assert spec["role"] == "frontend" and spec["staleness"] == verification.UNKNOWN

    # It rides along on the read, so the agent that verifies is handed the scope AND
    # where the devs say it lives in one call.
    rec = tools._op_get_deliverables("acme")
    assert [s["id"] for s in rec["deliverables"][0]["specs"]] == [spec["id"]]


def test_an_absolute_url_in_a_spec_is_refused_with_the_fix(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    with pytest.raises(ValueError) as e:
        tools._op_submit_spec(
            ag["frontend"], 1, "added the buttons", "they're at https://evil.com/pricing"
        )
    assert "absolute URL" in str(e.value) and "PATHS" in str(e.value)


def test_only_the_owner_starts_a_run_and_it_covers_everything(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    _claim_everything(conn, ag)
    with pytest.raises(ValueError, match="only the owner starts a verification run"):
        tools._op_start_verification(ag["frontend"], "https://staging.example.com")

    tools._op_withdraw_deliverable(ag["owner"], 3, "next milestone")
    run = tools._op_start_verification(ag["owner"], "https://staging.example.com")
    assert run["staging_url"] == "https://staging.example.com"
    # Every LIVE deliverable — there are no partial runs, and a withdrawn one is no
    # longer something to check.
    assert run["to_check"] == [1, 2]


def test_a_result_files_against_the_open_run_and_can_name_one_devs_claim(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    _claim_everything(conn, ag)
    spec = tools._op_submit_spec(
        ag["frontend"], 1, "added 3 buttons", "below the hero on /"
    )
    tools._op_start_verification(ag["owner"], "https://staging.example.com")

    per_claim = tools._op_record_verification(
        ag["owner"], 1, "rejected", "verified", "found 2 buttons, not 3", spec["id"]
    )
    assert per_claim["deliverable"] == 1 and "deliverable_id" not in per_claim
    assert per_claim["spec_id"] == spec["id"]
    assert per_claim["strength_label"] == verification.STRENGTH_LABELS["verified"]

    whole = tools._op_record_verification(
        ag["owner"], 2, "accepted", "evidence", "read the handler; never sent a mail"
    )
    assert whole["spec_id"] is None and whole["strength"] == verification.EVIDENCE
    assert verification.coverage(conn, "acme") == {
        "deliverables": 3, "with_spec": 1, "with_result": 2
    }


def test_recording_before_a_run_exists_says_what_to_do(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    with pytest.raises(ValueError) as e:
        tools._op_record_verification(ag["owner"], 1, "accepted", "verified", "looks fine")
    assert "start_verification" in str(e.value)


def test_a_made_up_strength_is_refused_and_teaches_the_three(conn):
    ag = _agents(conn)
    _locked(conn, ag)
    _claim_everything(conn, ag)
    tools._op_start_verification(ag["owner"], "https://staging.example.com")
    with pytest.raises(ValueError) as e:
        tools._op_record_verification(ag["owner"], 1, "accepted", "looks_good", "fine")
    msg = str(e.value)
    assert "not_checked" in msg and "silence reads as a pass" in msg
