"""Specs for the shorthand vocabulary — the ONE table the whole product speaks from.

The shorthand ("what a human types to their own agent") is read by three audiences and
was, until this file existed, written down three times: the dashboard's Commands
cheatsheet, the dashboard's Workflows panel, and the agent briefing in
``onboarding.role_prompt``. They had already drifted — the panels disagreed on notation
and the cheatsheet still taught a bare ``reopen`` the broker would refuse.

So ``ui.html`` now carries a single ``SHORTCODES`` array, both panels render from it, and
these tests hold the briefing to the same words. Same discipline as
``tests/test_connect_clients.py``, which pins the connect blocks to the doc verbatim: a
second copy of a literal that HUMANS TYPE is not a style problem, it teaches a command
that gets refused.

The notation ruling these tests enforce: ``#N`` is REQUIRED on every todo-scoped
command, because every task is delivered by one or more todos and there is no task-wide
form. ``stuck`` is the single exception — ``stuck #N`` flags one deliverable, a bare
``stuck`` escalates the whole task.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from sys_buddy import onboarding, state
from sys_buddy.config import Config
from sys_buddy.server import build_server

SRC = Path(__file__).resolve().parents[1] / "src" / "sys_buddy"
UI = SRC / "ui.html"
ONBOARDING = SRC / "onboarding.py"

REQUIRED_FIELDS = {"id", "code", "tools", "group", "modes", "scope", "desc"}
# The one command whose `#N` is optional. Named once, here, so every test below reads as
# "everything except this".
OPTIONAL_SCOPE_ID = "stuck"


def _ui() -> str:
    return UI.read_text(encoding="utf-8")


def shortcodes() -> list[dict]:
    """``SHORTCODES`` lifted straight out of ui.html.

    Parsed as STRICT JSON on purpose. The dashboard is one self-contained file with no
    build step, so the only way a Python test can hold it to anything is to read it —
    and keeping the literal JSON-parseable (double quotes, no comments inside) is what
    makes that a two-line parse instead of a JS interpreter.
    """
    m = re.search(r"var SHORTCODES\s*=\s*(\[.*?\n\]);", _ui(), re.S)
    assert m, "SHORTCODES no longer parses out of ui.html — is it still one JSON array?"
    return json.loads(m.group(1))


def _codes() -> list[str]:
    """Every literal a human types, minus the rows built at render time."""
    return [s["code"] for s in shortcodes() if not s.get("gen")]


# --------------------------------------------------------------------------- #
# the table itself
# --------------------------------------------------------------------------- #
def test_shortcodes_is_a_well_formed_table():
    rows = shortcodes()
    assert rows, "empty vocabulary"
    for s in rows:
        missing = REQUIRED_FIELDS - set(s)
        assert not missing, f"{s.get('id')} is missing {missing}"
        assert s["code"].strip() == s["code"] and s["code"], s["id"]
        assert isinstance(s["tools"], list)
        assert s["scope"] in ("required", "optional", "none"), s["id"]
        assert s["modes"] and set(s["modes"]) <= {"contract", "debug"}, s["id"]
        assert s["desc"].strip(), s["id"]


def test_every_id_is_unique_because_SH_looks_up_by_it():
    ids = [s["id"] for s in shortcodes()]
    assert len(ids) == len(set(ids)), "duplicate id — SH() would silently return one of them"


def test_every_tool_named_in_the_table_is_a_real_broker_tool(tmp_path):
    """A cheatsheet may not name a tool the broker doesn't register — that is how a
    design mockup's imaginary tools end up documented as shipping (see the warning above
    MCP_TOOLS in ui.html). An empty list means the agent composes the answer locally and
    calls nothing, which is the honest entry for `st`."""
    mcp = build_server(Config(mode="remote", db_path=tmp_path / "s.db"))
    registered = {t.name for t in asyncio.run(mcp.list_tools())}
    for s in shortcodes():
        for tool in s["tools"]:
            assert tool in registered, f"{s['id']} names {tool}, which is not registered"


def test_the_contract_vocabulary_is_absent_from_a_debug_task():
    """A debug task has no contract and never carries todos, so a code that needs either
    must not be offered on one."""
    debug = {s["id"] for s in shortcodes() if "debug" in s["modes"]}
    assert not debug & {"pc", "gc", "sign", "ship", "decline", "reopen"}
    assert not [s for s in shortcodes() if "debug" in s["modes"] and s["scope"] != "none"]


# --------------------------------------------------------------------------- #
# the notation ruling: #N is required, `stuck` is the only exception
# --------------------------------------------------------------------------- #
def test_scope_agrees_with_the_literal_it_describes():
    """`scope` is the RULE and `code` is what a human reads; a table where they disagree
    is worse than one field, because each looks authoritative."""
    for s in shortcodes():
        code = s["code"]
        if s["scope"] == "required":
            assert "#N" in code, f"{s['id']} claims scope=required but its code has no #N"
            assert "[#N]" not in code, f"{s['id']} is required — drop the brackets"
        elif s["scope"] == "optional":
            assert "[#N]" in code, f"{s['id']} claims scope=optional but doesn't show it"
        else:
            assert "#N" not in code, f"{s['id']} claims scope=none but its code scopes a todo"


def test_stuck_is_the_only_command_with_an_optional_todo_id():
    optional = [s["id"] for s in shortcodes() if s["scope"] == "optional"]
    assert optional == [OPTIONAL_SCOPE_ID], (
        "every task is delivered by todos and there is no task-wide form, so #N is "
        f"required everywhere but {OPTIONAL_SCOPE_ID}"
    )


def test_stuck_carries_both_halves_of_its_exception():
    """Prose has to name the bare form AND the scoped one (`SH('stuck','bare')` /
    `SH('stuck','scoped')`), or it goes back to hand-typing them."""
    stuck = {s["id"]: s for s in shortcodes()}[OPTIONAL_SCOPE_ID]
    assert stuck["bare"] == "stuck"
    assert stuck["scoped"] == "stuck #N"
    assert "optional" in stuck["desc"]


@pytest.mark.parametrize("path", [UI, ONBOARDING])
def test_no_optional_todo_id_notation_survives_except_for_stuck(path):
    """REGRESSION: `pc [#N]` / `sign [#N]` / `ship [#N]` taught an id the broker now
    refuses to do without. The bracket spelling is legal for exactly one command, so
    every remaining occurrence must be talking about that one."""
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "[#N]" in line or "[#n]" in line:
            assert "stuck" in line.lower(), f"{path.name}:{lineno} still teaches an optional #N"


# --------------------------------------------------------------------------- #
# both panels render FROM the table
# --------------------------------------------------------------------------- #
def _chip_literals() -> list[str]:
    """Every string literal handed to shChip()/flowRow() in ui.html.

    These are the Workflows panel's chips. A COMMAND in here is a hand-typed second copy
    of the vocabulary; a field or tool name (`status`, `staging_url`, `submit_readiness`)
    is not a command and legitimately stays literal.
    """
    ui = _ui()
    out = re.findall(r"shChip\('([^']*)'\)", ui)
    out += re.findall(r"flowRow\('([^']*)'", ui)
    return out


def test_the_workflows_panel_hand_types_no_commands():
    codes = set(_codes()) | {"stuck #N", "stuck"}
    for lit in _chip_literals():
        assert lit not in codes, f"shChip('{lit}') hand-types a command — use SH()"
        # `#N` on its own is the token being explained, not a command.
        assert lit == "#N" or "#N" not in lit, f"shChip('{lit}') hand-types a todo id"


def test_the_commands_cheatsheet_hand_types_no_rows():
    """`cmd(code, desc)` used to be called ~25 times with the whole vocabulary inline. It
    now takes its rows from SHORTCODES, so a literal call is a new third copy."""
    assert not re.search(r"\bcmd\('", _ui()), "shortcodesHTML is hand-typing rows again"


def test_both_panels_go_through_the_one_table():
    ui = _ui()
    # the cheatsheet iterates it...
    assert "SHORTCODES.forEach" in ui
    # ...and the walkthrough asks for literals by id.
    assert ui.count("SH('") > 15, "workflowsPanelBody stopped resolving commands via SH()"


def test_the_stepper_sentence_is_generated_from_TODO_STEPS():
    """The whole point of the exercise. This sentence hand-typed "four labels fold six
    march values: planning · building · ready · verified" and stayed that way after
    `testing` became the fifth label — a dashboard describing a stepper it isn't drawing.
    """
    ui = _ui()
    assert "four labels" not in ui
    assert "four nodes" not in ui
    assert "planning · building · ready · verified" not in ui, "the labels are hand-typed again"
    assert "words(TODO_STEPS.length)" in ui
    assert "words(Object.keys(TODO_MARCH).length)" in ui


def test_the_number_a_human_types_is_the_per_task_one():
    """The dashboard prints `#N` beside every todo and inside the host's drop command.
    That N is `todos.number` — per task, starting at 1 — not the global row `id`, which
    would show `#7` where the human types `#1`. `id` stays for internal keying."""
    ui = _ui()
    assert "function todoNo(t)" in ui
    assert "t.number" in ui
    # No human-facing `#` may interpolate the raw id again.
    assert not re.search(r">#'\+\s*t\.id", ui), "a `#` render is back on the internal id"
    assert "todo drop '+d.id+' '+todoNo(t)" in ui


# --------------------------------------------------------------------------- #
# the agent briefing speaks the same vocabulary
# --------------------------------------------------------------------------- #
def _prompt(mode: str = "contract") -> str:
    return onboarding.role_prompt("backend", "signin", mode=mode)


def test_every_shorthand_in_the_table_is_taught_to_the_agent():
    """The dashboard is a MIRROR of the agent prompt. A code on the cheatsheet that the
    agent was never taught is a command the human types into silence."""
    for s in shortcodes():
        if s.get("gen"):
            continue  # built from ROLE_TAGS at render time; the tags are tested below
        for mode in s["modes"]:
            prompt = _prompt(mode)
            assert "`" + s["code"].split(" ")[0] in prompt, (
                f"{s['id']} is on the cheatsheet but not in the {mode} briefing"
            )


def test_every_todo_scoped_command_is_taught_with_exactly_the_tables_notation():
    """Verbatim, not paraphrased: these strings are typed by a human and parsed by an
    agent, so `sign #N` and `sign [#N]` are different instructions."""
    prompt = _prompt("contract")
    for s in shortcodes():
        if s["scope"] == "none" or s.get("gen"):
            continue
        assert f"`{s['code']}`" in prompt, f"the briefing spells {s['id']} differently"


def test_the_briefing_never_teaches_an_optional_id_for_a_required_command():
    prompt = _prompt("contract")
    for s in shortcodes():
        if s["scope"] != "required":
            continue
        assert s["code"].replace(" #N", " [#N]") not in prompt
        # ...nor a DEFINITION of the bare stem: "`pc` propose_contract" is the row that
        # teaches it, and that row must carry the id. (Prose is free to say "`pc` THEN
        # `sign`" — naming a command mid-sentence is not offering a task-wide form.)
        stem = s["code"].split(" ")[0]
        for tool in s["tools"]:
            assert f"`{stem}` {tool}" not in prompt, f"the briefing defines `{stem}` without #N"


def test_the_briefing_says_the_id_is_required_and_why():
    """"Required on a task with todos" was a hedge from when a task could have none.
    Every task is delivered by todos now, so the id is unconditional."""
    prompt = _prompt("contract")
    assert "REQUIRED" in prompt
    assert "required on a task with todos" not in prompt
    assert "one or more todos" in prompt


def test_the_briefing_carries_the_stuck_exception():
    prompt = _prompt("contract")
    assert "`stuck [#N]`" in prompt
    assert "`stuck #N` flags" in prompt
    assert "bare `stuck`" in prompt


def test_the_role_tags_row_matches_the_tags_the_briefing_teaches():
    """The one row whose code and description are BUILT (from ROLE_TAGS), so it can't be
    compared as a literal — compare the tags themselves instead."""
    ui = _ui()
    tags = dict(re.findall(r"(\w+):'(\w+)'", re.search(r"var ROLE_TAGS=\{([^}]*)\}", ui).group(1)))
    assert tags, "ROLE_TAGS no longer parses out of ui.html"
    prompt = _prompt("contract")
    for tag, role in tags.items():
        assert f"`{tag.upper()}` {role}" in prompt, f"the briefing doesn't teach @{tag.upper()}"


# --------------------------------------------------------------------------- #
# ...and so does the "what happens next" line the broker composes
# --------------------------------------------------------------------------- #
def test_next_step_shorthand_is_in_the_table():
    """`state.next_step` prints the literal a human types on the todo card — a THIRD
    speaker of this vocabulary. Every stem it can emit must be a row here, mapped to the
    same broker tool, or the card advertises a command the cheatsheet doesn't explain."""
    by_id = {s["id"]: s for s in shortcodes()}
    for stem, tool in state._NEXT_TOOLS.items():
        assert stem in by_id, f"next_step can print `{stem}`, which is not in SHORTCODES"
        assert tool.split("(")[0] in by_id[stem]["tools"], (
            f"next_step maps `{stem}` to {tool}, the table maps it to {by_id[stem]['tools']}"
        )
