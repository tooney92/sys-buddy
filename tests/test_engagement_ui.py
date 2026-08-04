"""Specs for the DASHBOARD half of engagement mode (docs/enhancements.md items 1–4).

The domain layer already refuses to let anyone build past an unagreed scope, and it
already knows how strongly a result knows what it claims. None of that is worth
anything to the person the mode exists for — a non-technical owner — unless he can
open a page and read it himself. This file holds ``ui.html`` to four promises:

* **Every key of the payload is read.** A field the broker computes and the renderer
  never mentions is a field nobody sees.
* **All three strengths are printed, in the broker's own words.** ``verified`` /
  ``evidence`` / ``not_checked`` are the whole point: a reader who cannot infer the
  difference must be told it, and a gentler local phrasing of ``not_checked`` would
  undo the feature.
* **The coverage wording cannot be misread as quality.** "3 of 4 checked" must never
  look like "3 of 4 are properly tested" — the design doc names this as the exact
  place that misreading happens.
* **Every engagement block is behind an engagement check.** The API omits these keys
  entirely on a ``contract`` or ``debug`` task, so a peer task must render precisely
  the page it rendered before this feature existed.

Written against ``ui.html`` AS TEXT, the same way ``test_contract_kinds_surfaces.py``
holds the renderer to ``contracts.KINDS`` — there is no build step and no module to
import, so the file itself is the artefact under test. Where the domain owns a
vocabulary (the strengths, the verdicts, the coverage counts) the expected values are
READ OUT OF the domain module rather than retyped here, so renaming one fails this
file instead of silently making the dashboard lie.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sys_buddy import state, tools, verification

UI_HTML = (Path(tools.__file__).parent / "ui.html").read_text()

# Every function that renders engagement-only markup. The guard test below walks this
# list, so a NEW engagement panel that forgets its guard fails here — which is the
# failure mode that matters, since a missing guard is invisible on an engagement task
# and only shows up as a broken peer task.
ENGAGEMENT_RENDERERS = [
    "engagementHTML",
    "scopeAgreementHTML",
    "registerHTML",
    "coverageHTML",
    "checkedAgainstHTML",
    "guidelinesCardHTML",
    "todoDeliverablesHTML",
    "reviewAsideHTML",
]


def _fn(name: str) -> str:
    """The source of one top-level function in ui.html.

    Every function in that file is written at column 0 and closed by a `}` at column 0,
    so the terminator is unambiguous. Slicing matters: a whole-file substring search
    would let a phrase three panels away satisfy a check about THIS one — the mistake
    `test_contract_kinds_surfaces._kind_blocks` was written to avoid.
    """
    lo, hi = _span(name)
    return UI_HTML[lo:hi]


def _span(name: str) -> tuple[int, int]:
    """Where that function starts and ends in the file, as offsets."""
    m = re.search(rf"^function {name}\(.*?^\}}", UI_HTML, re.S | re.M)
    assert m, f"ui.html has no top-level function {name}()"
    return m.start(), m.end()


# --------------------------------------------------------------------------- #
# the payload — every key the broker computes reaches the screen
# --------------------------------------------------------------------------- #
# The accessor, spelled the way the renderer spells it. A key present in the payload
# and absent here is a fact the owner paid for and cannot see.
PAYLOAD_ACCESSORS = [
    # the mode flag itself
    "d.engagement",
    # the scope agreement — the list is the contract-shaped object
    "d.deliverables",
    "dl.locked",
    "dl.version",
    "dl.accepted_by",
    "dl.awaiting",
    "dl.rows",
    # one deliverable, in the owner's words
    "row.id",
    "row.number",
    "row.text",
    "row.withdrawn",
    "row.withdraw_reason",
    "row.todos",
    "row.specs",
    "row.result",
    "row.result.strength",
    "row.result.strength_label",
    "row.result.detail",
    "r.verdict",
    # the per-dev claims underneath it
    "s.role",
    "s.name",
    "s.claim",
    "s.how",
    "s.staleness",
    # the standards a reviewer signs against
    "d.guidelines",
    # verification: the run, its target, and the two counts
    "d.verification",
    "v.run",
    "v.coverage",
    "c.deliverables",
    "c.with_spec",
    "c.with_result",
    "run.staging_url",
    # who ran it and when — `verification.latest_run` carries `by`, `by_name` and an
    # epoch `created_at`; the page formats the clock itself.
    "run.by_name",
    "run.created_at",
]


@pytest.mark.parametrize("accessor", PAYLOAD_ACCESSORS)
def test_the_renderer_reads_every_key_of_the_payload(accessor):
    assert accessor in UI_HTML, f"nothing in ui.html reads {accessor}"


def test_the_coverage_counts_are_the_ones_the_broker_actually_returns(conn):
    """Sourced from `verification.coverage`, not retyped: renaming a count in the domain
    must fail here rather than quietly blank a line on the owner's screen."""
    counts = verification.coverage(conn, "no-such-task")
    for key in counts:
        assert f"c.{key}" in UI_HTML, f"the dashboard never reads coverage['{key}']"


# --------------------------------------------------------------------------- #
# the three strengths — load-bearing, and visually distinct
# --------------------------------------------------------------------------- #
def _strength_view() -> str:
    m = re.search(r"var STRENGTH_VIEW=\{(.*?)\n\};", UI_HTML, re.S)
    assert m, "ui.html has no STRENGTH_VIEW table"
    return m.group(1)


@pytest.mark.parametrize("strength", verification.STRENGTHS)
def test_every_strength_the_broker_can_store_has_a_view(strength):
    """A strength the broker records and the page has no row for renders as a bare key
    (or nothing) on the one line a non-technical reader most needs words for."""
    assert re.search(rf"^\s*{strength}\s*:", _strength_view(), re.M), strength


@pytest.mark.parametrize("strength,label", sorted(verification.STRENGTH_LABELS.items()))
def test_the_strengths_print_the_brokers_own_words(strength, label):
    """ONE vocabulary across the dashboard, the CLI and the agent's summary. Left to
    invent its own phrasing, a surface reaches for a gentler one — and the gentle version
    of "Not checked" is exactly the false confidence this mode exists to remove."""
    assert label in UI_HTML, f"{strength}: ui.html does not print {label!r}"


def test_not_checked_is_the_loud_one():
    """Silence reads as a pass, so "nobody looked" cannot be the quiet grey pill. It is
    the only row rendered on a dashed alarm border, and it says so in words."""
    view = _strength_view()
    not_checked = view[view.index("not_checked:"):]
    assert "dashed:true" in not_checked
    assert "stuck" in not_checked, "not_checked is not rendered in the alarm palette"
    assert "dashed:false" in view[: view.index("not_checked:")], (
        "verified/evidence should not share not_checked's dashed treatment"
    )


def test_the_three_strengths_differ_by_more_than_their_wording():
    """A reader who cannot infer the difference will not read three near-identical pills
    carefully. Colour, glyph and words all move — the glyph function branches three ways
    and the palettes are three different ones."""
    view = _strength_view()
    palettes = re.findall(r"k:'([a-z_]+)'", view)
    assert len(set(palettes)) == 3, f"strengths share a palette: {palettes}"
    glyph = _fn("strengthGlyph")
    assert "verified" in glyph and "evidence" in glyph
    assert glyph.count("<svg") == 3, "the three strengths do not have three glyphs"


def test_a_verdict_backed_by_not_checked_never_renders_as_working():
    """The strength OUTRANKS the verdict where the eye lands first. `accepted` +
    `not_checked` means an agent formed a view without looking at anything; printing
    "✓ working" over it would be precisely the manufactured confidence the doc rules
    out. The order of the two branches is the guarantee, so it is pinned."""
    src = _fn("outcomeBadge")
    assert "r.strength==='not_checked'" in src
    assert src.index("not_checked") < src.index("VERDICT_VIEW[r.verdict]"), (
        "the verdict is read before the strength is ruled out"
    )


@pytest.mark.parametrize("verdict", verification.VERDICTS)
def test_the_outcome_uses_the_brokers_verdict_vocabulary(verdict):
    """`rejected` reuses the existing block/strikes idea — no parallel vocabulary for
    "this isn't done" — so the dashboard must key on the broker's own two words."""
    table = re.search(r"var VERDICT_VIEW=\{(.*?)\};", UI_HTML, re.S).group(1)
    assert re.search(rf"\b{verdict}:", table), verdict


def test_a_deliverable_nobody_wrote_a_check_for_says_so():
    """The single easiest thing for a clean-looking page to hide."""
    assert "No dev has written a check for this one" in _fn("deliverableRowHTML")


def test_a_stale_check_is_told_apart_from_broken_work():
    """Both render as a red ✗ and they call for opposite actions. Without this line the
    dashboard periodically accuses honest devs in front of the person paying them."""
    notes = re.search(r"var STALENESS_NOTE=\{(.*?)\n\};", UI_HTML, re.S).group(1)
    assert "stale:" in notes and "unknown:" in notes
    assert "out of date" in notes


# --------------------------------------------------------------------------- #
# coverage — presence, never quality
# --------------------------------------------------------------------------- #
# Phrases that would turn a count of what was LOOKED AT into a claim about how well it
# works. Every one of them is a sentence a non-technical owner would act on.
QUALITY_CLAIMS = [
    "properly tested",
    "fully tested",
    "well tested",
    "everything works",
    "all good",
    "guarantee",
    "proves it works",
    "passed",
]


@pytest.mark.parametrize("claim", QUALITY_CLAIMS)
def test_the_coverage_card_never_claims_quality(claim):
    src = _fn("coverageHTML") + _fn("missingListHTML")
    assert claim not in src.lower(), f"the coverage card says {claim!r}"


def test_the_coverage_number_never_appears_as_a_bare_fraction():
    """"3 of 4" alone is the exact string that gets read as "3 of 4 are properly tested".
    It only ever renders inside a sentence naming what the count IS."""
    src = _fn("coverageHTML")
    assert "deliverables were looked at in the latest run" in src
    assert "have a check written by a dev" in src


def test_the_coverage_card_says_it_measures_presence_not_quality():
    src = _fn("coverageHTML")
    assert "not how well it works" in src
    assert "never a promise of quality" in src
    # …and points at the one strength that does mean something was proven.
    assert verification.STRENGTH_LABELS[verification.VERIFIED] in src


def test_what_was_not_checked_is_rendered_at_least_as_prominently():
    """Two alarmed rows, each NAMING the deliverables rather than printing a number — a
    bare "1 unchecked" is skimmed past, "#4 Mobile layout" is not."""
    src = _fn("coverageHTML")
    assert src.count("missingListHTML(") == 2, "one of the two misses is not rendered"
    missing = _fn("missingListHTML")
    assert "IC.warn" in missing, "the misses are not flagged"
    assert "r.number" in missing and "r.text" in missing, "the misses are not named"


def test_nothing_skipped_still_renders_a_line():
    """The absence of the alarm row has to mean something, so the clean case is stated
    rather than left as blank space a reader has to interpret."""
    assert "Nothing was skipped" in _fn("coverageHTML")


def test_no_run_at_all_is_said_out_loud():
    """The worst reading of an empty verification card is "fine so far"."""
    assert "Nobody has run a check yet" in _fn("coverageHTML")


# --------------------------------------------------------------------------- #
# the staging URL — disclosure is the entire safeguard
# --------------------------------------------------------------------------- #
def test_the_target_a_run_checked_against_is_always_on_screen():
    """The DEV supplies the target, so the posture is accountability, not prevention:
    "checked against https://random.vercel.dev" in front of an owner whose site is
    acme.com is what makes a wrong target catchable. It renders on BOTH the register and
    the coverage card, because either one alone can be the screen he is looking at."""
    src = _fn("checkedAgainstHTML")
    assert "run.staging_url" in src
    assert "Checked against" in src
    assert "The dev supplies this address" in src
    assert "checkedAgainstHTML(d)" in _fn("registerHTML")
    assert "checkedAgainstHTML(d)" in _fn("coverageHTML")


def test_a_run_that_did_not_say_where_it_looked_says_that():
    """A record that cannot say what it checked is unfalsifiable, so the missing target
    is a stated fact rather than an empty line."""
    src = _fn("checkedAgainstHTML")
    assert "did not record where it looked" in src
    assert "cannot be argued with" in src or "unverified" in src


# --------------------------------------------------------------------------- #
# the scope agreement — what a blocked team stares at
# --------------------------------------------------------------------------- #
def test_an_unagreed_scope_names_who_is_being_waited_on():
    src = _fn("scopeAgreementHTML")
    assert "dl.awaiting" in src and "dl.accepted_by" in src
    assert "Waiting on" in src
    assert "nothing can be built" in src.lower()
    # the version, because a revision resets everyone's acceptance
    assert "dl.version" in src


def test_a_locked_scope_states_the_asymmetry():
    """Scope may SHRINK and never grow — the rule people get wrong after the lock."""
    src = _fn("scopeAgreementHTML")
    assert "withdraw" in src
    assert "new engagement" in src


def test_the_gate_explains_that_messaging_stays_open():
    """Gate the talking and the dev who thinks deliverable 2 is unbuildable cannot say
    so, and the session deadlocks on the exact discussion it exists to have."""
    assert "messaging stays open" in _fn("scopeAgreementHTML")


# --------------------------------------------------------------------------- #
# the reviewer's aside — guidelines beside the contract (item 2)
# --------------------------------------------------------------------------- #
def test_the_guidelines_sit_beside_the_contract_at_review_time():
    """Signing then means "I checked this against them" — an accountable moment, with
    the broker claiming to have verified nothing."""
    # BESIDE, literally: the todo modal's right-hand column is the contract card with the
    # aside directly under it, so a reviewer reads the rules and the thing they govern
    # without leaving the dialog.
    modal = _fn("todoModalHTML")
    assert "contract:t.contract})+" in modal
    assert "reviewAsideHTML(d,t)" in modal
    aside = _fn("reviewAsideHTML")
    assert "guidelinesCardHTML" in aside and "todoDeliverablesHTML" in aside


def test_the_guidelines_card_reads_the_rules_by_role_type():
    src = _fn("guidelinesCardHTML")
    assert "d.guidelines" in src
    assert "r.rule" in src, "the rule text is never printed"


def test_the_guidelines_card_says_the_broker_cannot_check_them():
    """Guidance column: the broker cannot check that Tailwind was used, and no surface
    may imply otherwise."""
    src = _fn("guidelinesCardHTML")
    assert "cannot check" in src


def test_absent_guidelines_render_nothing_at_all():
    """Absent means absent — no key in the payload, no empty panel on the dashboard."""
    assert "var g=d.guidelines;" in UI_HTML
    src = _fn("guidelinesCardHTML")
    assert "if(!g) return '';" in src


def test_a_todo_that_serves_no_deliverable_is_visible_as_internal():
    """Internal work names no deliverable. That is honest modelling, but it has to be
    SEEN, or a todo that quietly serves nothing looks like one that serves everything."""
    src = _fn("todoDeliverablesHTML")
    assert "Serves no deliverable" in src
    assert "Internal work" in src


def test_the_deliverables_a_todo_serves_come_from_the_link_the_payload_already_has():
    """The inverse of `rows[].todos` — no new field, and it is where you notice a
    contract has drifted past the agreed scope."""
    src = _fn("deliverablesOfTodo")
    assert "r.todos" in src and "todoNo(t)" in src


# --------------------------------------------------------------------------- #
# the guard — a contract/debug task must look EXACTLY as it does today
# --------------------------------------------------------------------------- #
def test_the_engagement_flag_comes_from_the_payload():
    assert "function isEngagement(d){ return !!(d && d.engagement); }" in UI_HTML


@pytest.mark.parametrize("name", ENGAGEMENT_RENDERERS)
def test_every_engagement_block_is_behind_the_engagement_check(name):
    """The guard is the FIRST statement, not a condition somewhere inside: a peer task's
    payload carries none of these keys, so any line that runs before the check is a line
    reading `undefined` on a task that has never heard of engagement mode."""
    body = _fn(name)
    head = body[body.index("{") + 1:]
    assert head.lstrip().startswith("if(!isEngagement(d)) return '';"), (
        f"{name}() does not open with the engagement guard"
    )


def test_the_task_view_renders_the_register_under_the_stepper():
    """For the person paying, the scope and his own deliverables ARE the headline
    screen — above the roster, the activity and the team's own panels."""
    assert "stepper+engagementHTML(d)+rosterPanel(d)" in UI_HTML


def test_the_engagement_entry_point_is_the_only_new_call_in_the_task_view():
    """One call site, one guard. Sprinkling `if(d.engagement)` through taskHTML is how a
    peer task grows an empty gap where a panel used to be."""
    task_view = _fn("taskHTML")
    assert task_view.count("engagementHTML(d)") == 1
    assert "d.deliverables" not in task_view
    assert "d.verification" not in task_view


@pytest.mark.parametrize("key", ["deliverables", "verification", "guidelines"])
def test_no_engagement_key_is_read_outside_a_guarded_renderer(key):
    """The whole promise of "byte-identical for a peer task" reduces to this: nothing
    reads an engagement key except a function that has already returned '' for a task
    without one."""
    # Guarded renderers, plus the row/spec helpers they are the ONLY callers of. Matched
    # by OFFSET rather than by substring: a text window around the hit spills into the
    # comment above the function and would fail a perfectly guarded read.
    allowed = [
        _span(n)
        for n in ENGAGEMENT_RENDERERS
        + [
            "deliverableRowHTML",
            "specRowHTML",
            "missingListHTML",
            "deliverablesOfTodo",
            "outcomeBadge",
        ]
    ]
    for match in re.finditer(rf"\bd\.{key}\b", UI_HTML):
        at = match.start()
        assert any(lo <= at < hi for lo, hi in allowed), (
            f"d.{key} is read outside a guarded engagement renderer: "
            f"{UI_HTML[at - 80: at + 80]!r}"
        )


# --------------------------------------------------------------------------- #
# `confirmed` — the commercial end of an engagement
# --------------------------------------------------------------------------- #
# A task reaches `confirmed` when the owner's agent has run a verification and every
# live deliverable came back accepted: the client saying "this is what I asked for".
# It is strictly AFTER `verified` (the builders are done) and engagement-only — a
# `contract` or `debug` task can never get there.
#
# Every check below is a rendering the state would otherwise get WRONG rather than
# merely miss, because each one fails quietly: a raw key printed as a label, a
# finished task sitting at step one, a node pulsing forever on delivered work. Spelled
# as the literal string rather than read off `state`, because ui.html is the artefact
# under test — the file has no build step and no import of the domain, so the string
# living in it is the whole of what ships.
CONFIRMED = "confirmed"


def _js_map(name: str) -> str:
    """The body of a top-level `var NAME={...};` table in ui.html."""
    m = re.search(rf"^var {name}=\{{(.*?)\}};", UI_HTML, re.S | re.M)
    assert m, f"ui.html has no {name} table"
    return m.group(1)


def _js_array(name: str) -> str:
    """The body of a top-level `var NAME=[...];` list in ui.html."""
    m = re.search(rf"^var {name}=\[(.*?)\];", UI_HTML, re.S | re.M)
    assert m, f"ui.html has no {name} list"
    return m.group(1)


def _entry(table: str, key: str) -> str:
    """The quoted value of one `key:'value'` pair."""
    m = re.search(rf"\b{key}:'([^']*)'", table)
    assert m, f"no {key} entry"
    return m.group(1)


def test_confirmed_has_a_chip_palette():
    """`stateChip` builds `var(--st-<SK[state]>-…)`. With no SK row the state falls back
    to the grey `open` palette, so the end of an engagement renders the same colour as a
    task nobody has started."""
    assert re.search(rf"\b{CONFIRMED}:", _js_map("SK")), "SK has no confirmed row"


def test_confirmed_borrows_the_verified_palette_instead_of_owning_one():
    """Green already means finished, and the two ends of an engagement differ by their
    word, not their colour. Reuse is also the safe choice: see the theme test below."""
    assert _entry(_js_map("SK"), CONFIRMED) == "verified"


def test_no_confirmed_palette_variable_was_introduced():
    assert "--st-confirmed-" not in UI_HTML, (
        "confirmed defined its own CSS variables instead of reusing --st-verified-*"
    )


@pytest.mark.parametrize("part", ["bg", "fg", "dot"])
def test_every_palette_a_state_names_is_defined_in_BOTH_themes(part):
    """The reason a new palette key is a bigger change than it looks. `stateChip` writes
    `var(--st-K-bg)` with NO fallback, so a triple defined in `:root` and forgotten in
    `[data-theme="dark"]` resolves to nothing at all — a chip with no background, no
    text colour and no dot, which is invisible rather than merely wrong, and only in the
    theme the author was not using."""
    for key in sorted(set(re.findall(r":'([a-z_]+)'", _js_map("SK")))):
        assert UI_HTML.count(f"--st-{key}-{part}:") == 2, (
            f"--st-{key}-{part} is not defined in exactly the two themes"
        )


def test_confirmed_prints_a_label_and_not_the_raw_state_string():
    """`SLAB[s]||s` — a missing row prints the key itself."""
    assert re.search(rf"\b{CONFIRMED}:", _js_map("SLAB")), "SLAB has no confirmed row"


def test_the_confirmed_label_reads_as_the_end():
    """`debugChip` sets the precedent with `resolved ✓`: the last word on a task carries
    the tick, because the bare adjective reads as one more stage in a list of stages —
    and here the stage before it is `verified`, which already sounds like the end."""
    label = _entry(_js_map("SLAB"), CONFIRMED)
    assert "✓" in label, f"the confirmed chip reads {label!r}, with nothing marking it final"
    assert label != _entry(_js_map("SLAB"), "verified"), (
        "confirmed and verified print the same word, so the owner's sign-off is invisible"
    )


def test_confirmed_sits_after_verified_on_the_stepper():
    """`ORDER` is the frontier index. A state missing from it takes `realO`'s 0 fallback,
    so the most finished task on the board renders parked at step one."""
    order = _js_map("ORDER")
    confirmed = re.search(rf"\b{CONFIRMED}:(\d+)", order)
    assert confirmed, "ORDER has no confirmed index"
    verified = re.search(r"\bverified:(\d+)", order)
    assert int(confirmed.group(1)) > int(verified.group(1)), (
        "confirmed does not rank after verified"
    )


def test_confirmed_is_a_node_on_the_stepper_and_it_is_the_last_one():
    """Without a node the `times['confirmed']` timestamp the API already collects has
    nowhere to print, and the stepper's final tick lands on `verified` — the builders
    finishing, not the client accepting."""
    steps = re.findall(r"\['([a-z_]+)','[^']*'\]", _js_array("STEPS"))
    assert CONFIRMED in steps, "STEPS has no confirmed node"
    assert steps[-1] == CONFIRMED, f"confirmed is not the final node: {steps}"
    assert steps.index(CONFIRMED) > steps.index("verified")


def test_the_confirmed_node_is_engagement_only():
    """A `contract`/`debug` task can never reach it, and a node that can never light is
    worse than no node — it reads as work outstanding forever. genSteps takes the flag
    and the task view is the one that supplies it."""
    src = _fn("genSteps")
    assert "STEPS.slice(" in src, "genSteps renders all of STEPS regardless of mode"
    assert "genSteps(d.state, d.times, d.agents, isEngagement(d))" in UI_HTML, (
        "the task view never tells genSteps whether this is an engagement"
    )


def test_a_confirmed_task_stops_pulsing():
    """The frontier node renders `current` — a pulsing ring — which is the dashboard's
    one way of saying someone is working on this right now. `verified` was special-cased
    twice to render done instead; `confirmed` is the same kind of end and needs both."""
    src = _fn("genSteps")
    assert "stateName==='confirmed'" in src, "genSteps does not know confirmed is finished"
    assert "stateName==='verified'" not in re.sub(
        r"var finished = .*", "", src
    ), "a hardcoded verified check survives outside the finished predicate"
    assert "(finished && i===o)" in src, "the confirmed frontier node never renders done"
    assert "!finished" in src, "the confirmed frontier node still renders as current"


@pytest.mark.parametrize("kind", ["confirmed", "resolved"])
def test_the_message_that_ends_a_task_is_not_an_anonymous_grey_bubble(kind):
    """`typeChip` falls back to `['message','open']`. The bubble that closes the whole
    task is the last one a reader scrolls to, and unmapped it is indistinguishable from
    chatter — which is what `resolved` has looked like since debug tasks shipped."""
    src = _fn("typeChip")
    assert re.search(rf"\b{kind}:\['", src), f"typeChip has no {kind} row"


# --------------------------------------------------------------------------- #
# "in flight" — a finished board is not a busy one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state_name", sorted(state.TERMINAL_STATES))
def test_the_terminal_states_are_all_known_to_the_dashboard(state_name):
    """Read out of `state.TERMINAL_STATES` rather than retyped, so a state that becomes
    terminal in the domain and is forgotten here fails instead of quietly being counted
    as live work forever."""
    assert re.search(rf"\b{state_name}:", _js_map("TERMINAL")), (
        f"{state_name} is not counted as terminal"
    )


def test_the_dashboard_invents_no_terminal_state_of_its_own():
    """The other direction: a word the page treats as finished and the broker does not."""
    keys = set(re.findall(r"(\w+):1", _js_map("TERMINAL")))
    assert keys == set(state.TERMINAL_STATES), (
        f"ui.html's TERMINAL disagrees with the domain: {keys ^ set(state.TERMINAL_STATES)}"
    )


def test_the_in_flight_count_excludes_finished_tasks():
    """The header counted `state.tasks.length` — every row on the page — so a board of
    ten finished tasks announced "10 in flight". A `confirmed` engagement is the sharpest
    case: the client has signed it off and the invoice is out."""
    src = _fn("listHTML")
    assert "state.tasks.length+' in flight'" not in src, (
        "the header still counts every task as in flight"
    )
    assert "TERMINAL[t.state]" in src, "the count does not consult the terminal states"
    assert "+inFlight+' in flight" in src
