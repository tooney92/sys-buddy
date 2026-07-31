"""Specs for the SURFACES that make contract kinds visible.

``tests/test_contract_kinds.py`` proves the validator accepts four kinds. That is
invisible on its own: an agent that is never told ``screens`` exists writes a fake
endpoint instead, and a dashboard that maps only over ``data.endpoints`` renders a
locked ui contract as an empty card. This file holds the four teaching/rendering
surfaces to the same table:

* ``tools.py``   — the tool DESCRIPTION FastMCP hands the agent before it calls.
* ``rules.py`` / ``onboarding.py`` — the briefings it reads before it acts.
* ``readiness.py`` — the pre-flight it must pass, which used to fail a correct
  screens/types answer for not saying "endpoint".
* ``api.py`` / ``ui.html`` — the payload and the renderer.

Every check is written against ``contracts.KINDS`` rather than a hardcoded list of
four, so adding a kind fails here until its surfaces are wired up too.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastmcp import FastMCP

from sys_buddy import api, contracts, onboarding, readiness, todos, tools
from sys_buddy.config import Config
from sys_buddy.rules import RULES_OF_ENGAGEMENT
from tests.test_todos_api import _task_with_seats, _todo_contract

UI_HTML = (Path(tools.__file__).parent / "ui.html").read_text()

UNIT_KEYS = {kind.unit_key for kind in contracts.KINDS.values()}

# A worked EXAMPLE is required of every kind whose unit shape has no shared convention
# to lean on. `endpoints` carries thirty years of it — nobody needs to be shown what a
# method and a path look like — so an example there would be noise. `screens`, `types`
# and `criteria` carry none, and an agent that has to guess guesses wrong in a way the
# validator only catches AFTER the call. Keyed off the kind table so a NEW kind fails
# this file until someone decides which side of the line it falls on.
KINDS_NEEDING_A_WORKED_EXAMPLE = {
    name for name, kind in contracts.KINDS.items() if not kind.has_http_surface
}


def _schemas(mode, tmp_path) -> dict:
    mcp = FastMCP("t")
    tools.register_tools(mcp, Config(mode=mode, db_path=tmp_path / f"{mode}.db"))
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# --------------------------------------------------------------------------- #
# tools.py — the primary teaching surface
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_propose_contract_names_every_unit_key(tmp_path, mode):
    """FastMCP exposes the docstring as the tool description, so this IS the prompt an
    agent reads before proposing. Both registrations, or half the users never hear it."""
    desc = _schemas(mode, tmp_path)["propose_contract"].description or ""
    for key in UNIT_KEYS:
        assert key in desc, f"{mode}: '{key}' missing from propose_contract"


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_propose_contract_names_every_kind(tmp_path, mode):
    desc = (_schemas(mode, tmp_path)["propose_contract"].description or "").lower()
    for name in contracts.KINDS:
        assert re.search(rf"\b{name}\b", desc), f"{mode}: kind {name!r} not named"


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_propose_contract_says_interface_type_is_optional(tmp_path, mode):
    """The key names the kind, so declaring one is two chances to be wrong instead of
    none. An agent told to declare it will, and will eventually contradict its key."""
    desc = (_schemas(mode, tmp_path)["propose_contract"].description or "").lower()
    assert "interface_type" in desc
    assert "optional" in desc or "inferred" in desc


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_propose_contract_no_longer_demands_a_staging_url_of_every_kind(tmp_path, mode):
    """Only http has an HTTP surface to point at. The old wording ("`spec` must contain
    `endpoints` … and an absolute https `staging_url`") is now wrong for three kinds."""
    desc = _schemas(mode, tmp_path)["propose_contract"].description or ""
    assert "staging_url" in desc  # still named — http needs it
    assert "must contain `endpoints`" not in desc


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_propose_contract_teaches_the_ui_unit_before_the_agent_composes(tmp_path, mode):
    """TEACHING BY DOCSTRING, NOT BY REFUSAL.

    The validator already carries the good copy — `'states' must list the states this
    screen can be in, e.g. ["loading", "empty", "error"]` — but that fires only AFTER a
    wrong call, so the agent learns on the second attempt and burns a round trip. The
    channel that fires BEFORE it composes had the shape (`a name + non-empty states`)
    and no example, which is exactly where `states` gets misread as "the parts of the
    page". `method` + `path` needs no example; `screens` + `states` has no convention to
    lean on and does.
    """
    desc = _schemas(mode, tmp_path)["propose_contract"].description or ""
    assert "Receipt" in desc, f"{mode}: no worked example of a screens unit"
    for state in ("loading", "paid", "failed"):
        assert state in desc, f"{mode}: example is missing the {state!r} state"
    # The misreading it exists to kill, said out loud.
    assert "CONDITIONS" in desc
    assert "not its parts" in desc.lower() or "NOT its parts" in desc
    # …and the granularity choice named rather than left to be guessed.
    low = desc.lower()
    assert "screen or" in low and "component" in low


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_propose_contract_says_units_come_from_what_was_agreed(tmp_path, mode):
    """The kinds are NOT equally self-falsifying, and the docstring has to say so.

    An invented endpoint dies the first time the peer calls it — reality checks it. An
    invented screen state or acceptance criterion validates, locks, and is then
    "verified" against itself, because the contract is the thing being checked; the loop
    closes on itself and nobody finds out. The broker cannot enforce "these are the units
    you actually agreed" — only the peer's signature can — so the agent must be told to
    take units from the todo's scope and to STATE any assumption it is filling a gap with.
    """
    desc = (_schemas(mode, tmp_path)["propose_contract"].description or "").lower()
    assert "agreed" in desc and "scope" in desc
    assert "plausible" in desc, f"{mode}: doesn't warn against plausible invention"
    assert "assumption" in desc


def _kind_blocks(desc: str) -> dict[str, str]:
    """The slice of the docstring that belongs to EACH kind, bounded by the next kind's
    bullet rather than by a fixed character window.

    Bounding matters more than it looks: a naive `split(key)[1][:600]` window overruns
    into the following kinds and into the prose below the list, so a kind with no example
    of its own "passes" on a literal quoted three bullets away. The first version of this
    test did exactly that — `schema` matched the `"name"` inside the SCREENS example and
    `none` matched `"verified"` in unrelated prose — and reported all four kinds taught
    when two were not. A test that passes for the wrong reason is worse than no test.

    The LAST bullet needs the same care for the same reason. Running it to the end of
    the docstring swallows every paragraph below the list, and the prose down there
    quotes things too — the second version of this test still passed a stripped
    `criteria` because it matched `"verified"` in a sentence about invented units. So the
    final block ends at the blank line that ends the list, not at the end of the string.
    """
    marks = sorted(
        (desc.index(f"`{k.unit_key}`"), k.unit_key)
        for k in contracts.KINDS.values()
        if f"`{k.unit_key}`" in desc
    )
    out: dict[str, str] = {}
    for i, (pos, key) in enumerate(marks):
        if i + 1 < len(marks):
            end = marks[i + 1][0]
        else:
            para = desc.find("\n\n", pos)
            end = para if para != -1 else len(desc)
        out[key] = desc[pos:end]
    return out


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_every_convention_free_kind_carries_a_worked_example(tmp_path, mode):
    """Keyed off `contracts.KINDS`, so ADDING a kind fails here until its teaching copy
    exists. The unit key alone is not teaching — `criteria` and `types` are exactly as
    convention-free as `screens` was, and each carries its own literal example."""
    blocks = _kind_blocks(_schemas(mode, tmp_path)["propose_contract"].description or "")
    missing = []
    for name in KINDS_NEEDING_A_WORKED_EXAMPLE:
        block = blocks.get(contracts.KINDS[name].unit_key, "")
        # An example is a concrete quoted literal, not a restatement of the field names
        # (those are written in `backticks`, so they cannot satisfy this).
        #
        # DOTALL, because a docstring WRAPS: the local registration's criteria example
        # breaks across two lines, and a line-bound pattern silently found no example in
        # copy that plainly had one. A guard that fails on formatting is a guard people
        # delete.
        if not re.search(r'"[a-zA-Z][^"]{2,}"', block, re.S):
            missing.append(name)
    assert not missing, f"{mode}: kinds with no worked example of their own: {missing}"


def test_the_worked_examples_are_each_kinds_own(tmp_path):
    """The guard on the guard: each example must sit in ITS kind's block, so the test
    above cannot be satisfied by a neighbour's literal leaking across the boundary."""
    blocks = _kind_blocks(_schemas("remote", tmp_path)["propose_contract"].description or "")
    assert "Receipt" in blocks["screens"]
    assert "Receipt" not in blocks["types"] and "Receipt" not in blocks["criteria"]
    assert "Session" in blocks["types"] and "Session" not in blocks["screens"]
    assert "CSV" in blocks["criteria"] and "CSV" not in blocks["screens"]


# --------------------------------------------------------------------------- #
# rules.py / onboarding.py — the briefings
# --------------------------------------------------------------------------- #
def test_all_three_teaching_channels_agree_on_the_ui_unit():
    """A disagreement between the channels is worse than any one of them being thin: an
    agent reads the briefing at pairing, is graded by the pre-flight, and composes
    against the tool description. If they say different things it will follow whichever
    it saw last."""
    hint = readiness._grade_propose("nothing", "frontend", "signin", "contract")[1]
    for surface, text in (("rules", RULES_OF_ENGAGEMENT), ("pre-flight", hint)):
        low = text.lower()
        assert "receipt" in low, f"{surface}: no worked example of a screens unit"
        assert "condition" in low, f"{surface}: doesn't say states are CONDITIONS"
        assert "part" in low, f"{surface}: doesn't rule out 'states are the parts'"
        assert "component" in low, f"{surface}: granularity choice not named"



def test_the_rules_describe_a_contract_as_named_units():
    for key in UNIT_KEYS:
        assert key in RULES_OF_ENGAGEMENT, key


def test_the_briefing_describes_a_contract_as_named_units():
    briefing = onboarding.role_prompt("backend", "signin", mode="contract")
    for key in UNIT_KEYS:
        assert key in briefing, key


# --------------------------------------------------------------------------- #
# readiness.py — the pre-flight
# --------------------------------------------------------------------------- #
def _answer(unit_phrase: str) -> str:
    return (
        f"propose_contract carrying {unit_phrase}; if my peer proposes first I push back "
        "with send_message asking for changes, then sign with lock_contract"
    )


@pytest.mark.parametrize("unit_phrase", [
    "at least one endpoint with a method and path",
    "named types with their fields and shapes",
    "the screens and the states each one can be in",
    "acceptance criteria that can be checked",
])
def test_any_kind_of_unit_passes_the_propose_question(unit_phrase):
    """The old grader required the literal word "endpoint", so an agent that correctly
    described a designer↔frontend contract failed pre-flight for being right."""
    ok, _ = readiness._grade_propose(_answer(unit_phrase), "frontend", "signin", "contract")
    assert ok is True


def test_naming_no_unit_at_all_still_fails():
    ok, _ = readiness._grade_propose(
        "propose_contract, then lock_contract after sending a message", "frontend",
        "signin", "contract",
    )
    assert ok is False


def test_an_endpoints_answer_is_no_longer_held_to_naming_the_staging_url():
    """It MUST not be: the deployment target is host-owned and a spec carrying one is
    refused, so an agent that dutifully recited "…and the staging_url" would be
    describing a proposal the broker rejects. The rule it still has to know — that the
    target is the only fetchable URL and comes from get_contract — is graded by the
    `url` question (test_readiness.py)."""
    ok, _ = readiness._grade_propose(
        _answer("at least one endpoint with a method and a path"), "frontend", "signin",
        "contract",
    )
    assert ok is True


def test_the_propose_explanation_says_who_owns_the_target():
    _, hint = readiness._grade_propose("nothing", "frontend", "signin", "contract")
    assert "humans" in hint and "refused" in hint


def test_the_propose_hint_names_every_unit_key():
    _, hint = readiness._grade_propose("", "frontend", "signin", "contract")
    for key in UNIT_KEYS:
        assert key in hint, key


def test_the_propose_question_asks_about_a_contract_with_no_http_surface():
    q = next(
        item for item in readiness.questions("frontend", "contract")
        if item["id"] == "propose"
    )
    assert "HTTP" in q["q"]


def test_no_mcp_tool_name_was_renamed_out_of_the_graders():
    """This module grades free text by searching for literal tool names — renaming one
    silently makes its check unfalsifiable, and every agent passes."""
    _, hint = readiness._grade_propose("", "frontend", "signin", "contract")
    assert "propose_contract" in hint and "lock_contract" in hint


# --------------------------------------------------------------------------- #
# api.py — the payload
# --------------------------------------------------------------------------- #
_SPECS = {
    "http": {"endpoints": [{"method": "GET", "path": "/me"}],
             "staging_url": "https://s.example.com"},
    "schema": {"types": [{"name": "User", "fields": [{"name": "id", "type": "string"}]}]},
    "ui": {"screens": [{"name": "ForgotPassword", "states": ["loading", "error"]}]},
    "none": {"criteria": ["the CSV import rejects rows with a missing email"]},
}


@pytest.mark.parametrize("kind", sorted(_SPECS))
def test_units_of_emits_the_kind_and_its_units(kind):
    block = api._units_of(_SPECS[kind])
    assert block["kind"] == kind
    assert block["unit_key"] == contracts.KINDS[kind].unit_key
    assert block["units"] == _SPECS[kind][contracts.KINDS[kind].unit_key]


def test_http_still_carries_endpoints_so_an_older_dashboard_cannot_break():
    """ui.html is served from disk and a browser tab may be older than api.py."""
    assert api._units_of(_SPECS["http"])["endpoints"] == _SPECS["http"]["endpoints"]


@pytest.mark.parametrize("kind", ["schema", "ui", "none"])
def test_a_non_http_contract_reports_no_endpoints(kind):
    assert api._units_of(_SPECS[kind])["endpoints"] == []


def test_a_spec_with_no_units_falls_back_to_http():
    """Every contract written before kinds existed is http, and a row with nothing in it
    must render an empty API card rather than raise."""
    assert api._units_of({})["kind"] == "http"
    assert api._units_of({})["units"] == []


def test_a_malformed_unit_key_renders_empty_rather_than_breaking_the_panel():
    """Rows were written by whatever validator was deployed then — the dashboard is not
    the place to discover one is wrong."""
    assert api._units_of({"screens": "ForgotPassword"})["units"] == []
    assert api._units_of({"endpoints": {"a": 1}})["endpoints"] == []


@pytest.mark.parametrize("kind", sorted(_SPECS))
def test_the_contract_block_carries_the_kind_end_to_end(conn, kind):
    seats = _task_with_seats(conn, roles=("designer", "frontend"))
    todo = todos.propose_todo(
        conn, seats["designer"], "Password reset", "the screens", ["designer", "frontend"]
    )
    _todo_contract(conn, "signin", todo["id"], 1, _SPECS[kind])

    block = api._contract_for(conn, "signin", todo_id=todo["id"])
    assert block["data"]["v1"]["kind"] == kind
    assert len(block["data"]["v1"]["units"]) == 1


# --------------------------------------------------------------------------- #
# ui.html — the renderer
# --------------------------------------------------------------------------- #
def test_the_dashboard_has_a_view_row_for_every_kind():
    """One renderer, four kinds (design doc, "Rendering"). A kind the validator accepts
    but the page has no row for renders as a blank card on a LOCKED agreement."""
    table = re.search(r"var CONTRACT_KINDS=\{(.*?)\n\};", UI_HTML, re.S).group(1)
    for name in contracts.KINDS:
        assert re.search(rf"^\s*{name}\s*:", table, re.M), name


def test_the_dashboard_reads_both_field_spellings():
    """The validator accepts `name`/`n` and `type`/`t` because the terse pair is the
    house wire format for endpoint fields; a renderer reading one would drop the other's
    rows silently."""
    assert "f.name||f.n" in UI_HTML
    assert "f.type||f.t" in UI_HTML


def test_the_dashboard_takes_the_kind_from_the_payload():
    """The kind comes from the payload (api._units_of → contracts.infer_kind), never
    re-derived here: a second copy of the key→kind mapping in the browser is one that
    drifts from the validator. It falls back to http, which is what every contract
    written before kinds existed is — so an older broker's payload still renders."""
    assert "CONTRACT_KINDS[data&&data.kind] || CONTRACT_KINDS.http" in UI_HTML


def test_the_shortcodes_literal_is_still_strict_json():
    """Guarded by tests/test_shortcodes.py too — repeated here because this file edits
    the same <script>."""
    literal = re.search(r"var SHORTCODES=(\[.*?\n\]);", UI_HTML, re.S).group(1)
    assert isinstance(json.loads(literal), list)
