"""Specs for the todo SUMMARY — the one line a human reads to know what is being built.

`scope` is written for the agent that has to build the thing. It grows into a wall of
text — in scope, out of scope, constraints, open questions — which is correct for its
job and unreadable at a glance. On a real task the scope of a single todo ran to
roughly a screen and a half of unbroken prose, and the person paying for it could not
tell what it was.

Two claims carry this file:

* **a summary that is just the opening of scope is REFUSED.** That is the failure mode
  of asking an agent for a summary: it hands back the first N characters of what it
  already wrote, which costs the reader a truncated spec instead of buying them a
  sentence. The broker cannot check that a summary is ACCURATE — that is guidance, and
  the briefing carries it — but it can check that somebody actually wrote one.
* **absence is visible.** A todo with no summary renders as saying so, never as an
  empty line, or nobody notices the board has stopped being readable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sys_buddy import todos, tools
from tests.test_todos_state import _agents

UI_HTML = (Path(tools.__file__).parent / "ui.html").read_text()

LONG_SCOPE = (
    "Sign-in, session and sign-out for staff.\n\n"
    "IN SCOPE\n"
    "- a sign-in screen\n"
    "- a sign-in endpoint that establishes a session\n"
    "- session persistence across app restarts\n\n"
    "OUT OF SCOPE\n"
    "- password reset, self-registration, SSO, MFA\n"
)


def _propose(conn, ag, summary=None, scope=LONG_SCOPE):
    return todos.propose_todo(
        conn, ag["backend"], "Authentication", scope, ["backend", "frontend"],
        summary=summary,
    )


# --- it is optional, and its absence is loud --------------------------------
def test_a_todo_without_a_summary_still_works(conn):
    """Optional at the tool boundary so nothing that exists today breaks."""
    ag = _agents(conn)
    made = _propose(conn, ag)
    row = todos.get_row(conn, "signin", made["number"])
    assert todos.to_dict(conn, row)["summary"] is None


def test_an_empty_summary_is_none_not_empty_string(conn):
    """`None` and `""` render differently and mean the same thing. One of them."""
    ag = _agents(conn)
    made = _propose(conn, ag, summary="   ")
    row = todos.get_row(conn, "signin", made["number"])
    assert todos.to_dict(conn, row)["summary"] is None


def test_the_dashboard_says_a_summary_is_missing(conn):
    """Absence must be visible, not silent — an empty line reads as "nothing to say"."""
    assert "no summary" in UI_HTML


# --- what it must not be ----------------------------------------------------
def test_the_opening_of_scope_is_refused_as_a_summary(conn):
    """THE failure mode. Asked for a summary, an agent hands back what it already wrote."""
    ag = _agents(conn)
    with pytest.raises(ValueError, match="must not be the opening of scope"):
        _propose(conn, ag, summary=LONG_SCOPE[:120])


def test_the_check_ignores_whitespace_and_case(conn):
    """Re-wrapping the same sentence is still the same sentence."""
    ag = _agents(conn)
    sneaky = "  SIGN-IN,   SESSION AND SIGN-OUT   FOR STAFF.  "
    with pytest.raises(ValueError, match="must not be the opening of scope"):
        _propose(conn, ag, summary=sneaky)


def test_a_genuine_summary_passes(conn):
    ag = _agents(conn)
    made = _propose(
        conn, ag,
        summary="Staff can sign in and stay signed in, so everything else has an "
                "identity to hang off.",
    )
    row = todos.get_row(conn, "signin", made["number"])
    assert todos.to_dict(conn, row)["summary"].startswith("Staff can sign in")


def test_an_over_long_summary_is_refused(conn):
    """It is a headline. If it needs a paragraph it belongs in scope, which has room."""
    ag = _agents(conn)
    with pytest.raises(ValueError, match="at most"):
        _propose(conn, ag, summary="x" * (todos.MAX_SUMMARY + 1))


# --- reproposing ------------------------------------------------------------
def test_reproposing_without_a_summary_keeps_the_existing_one(conn):
    """Changing the party list must not silently strip the sentence humans read —
    the same rule title and scope already follow."""
    ag = _agents(conn)
    made = _propose(conn, ag, summary="Staff can sign in and stay signed in.")
    todos.repropose_todo(conn, ag["backend"], made["number"],
                         parties=["backend", "frontend", "mobile"])
    row = todos.get_row(conn, "signin", made["number"])
    assert todos.to_dict(conn, row)["summary"] == "Staff can sign in and stay signed in."


def test_reproposing_can_replace_the_summary(conn):
    ag = _agents(conn)
    made = _propose(conn, ag, summary="Staff can sign in.")
    todos.repropose_todo(conn, ag["backend"], made["number"],
                         summary="Staff sign in, stay signed in, and can sign out.")
    row = todos.get_row(conn, "signin", made["number"])
    assert todos.to_dict(conn, row)["summary"].endswith("can sign out.")


# --- scope rendering, without a markup parser -------------------------------
def test_the_dashboard_splits_scope_without_parsing_markup(conn):
    """Deliberately NOT markdown: everything an agent writes is DATA (charter rule 1),
    and turning untrusted text into HTML is what escaping exists to prevent. We split on
    shapes that carry no markup — blank lines, leading '- ', ALL-CAPS or trailing ':' —
    and every fragment still goes through esc()."""
    assert "function scopeHTML(" in UI_HTML
    body = UI_HTML.split("function scopeHTML(")[1].split("\nfunction ")[0]
    # Every fragment it emits is escaped — that is the whole safety property.
    assert body.count("esc(") >= 3
    # It splits on SHAPE, never on markup syntax: no emphasis, no links, no images.
    for markup in ("**", "](", "<img", "innerHTML"):
        assert markup not in body, f"scopeHTML must not handle {markup!r}"
    # And the raw scope is no longer dumped as one blob anywhere.
    assert "esc(t.scope)" not in UI_HTML


def test_the_full_scope_is_collapsed_behind_a_toggle(conn):
    """The summary is the headline; the wall of text is one click away, not gone."""
    assert "Full scope" in UI_HTML
    assert "<details" in UI_HTML


def test_the_briefing_tells_agents_to_always_write_one(conn):
    """Optional at the tool boundary means agents skip it unless the briefing insists —
    and if they skip it, the board stays unreadable and the feature bought nothing."""
    from sys_buddy import onboarding

    p = onboarding.role_prompt("backend", "signin", staging_url=None)
    assert "summary" in p.lower()
    assert "always write" in p.lower()
