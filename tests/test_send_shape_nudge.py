"""Specs for the receipt nudge on ``send_message``.

An agent never sees its own message rendered, so the only feedback loop for "this is a
wall of text" is the receipt the broker hands back. The nudge closes that loop — and its
single most important property is the one it does NOT do: a message that already carries
shape must come back with a clean receipt, because a nudge that fires often is one every
agent learns to scroll past, and then it protects nobody.

The check is a rendering fact, not a style opinion: ``ui.proseBlocks`` splits on newlines
and nothing else, so a body with no newline in it renders as one unbroken block however
well it is written. Nothing about bullet density, sentence length or tone is in scope.
"""

from __future__ import annotations

import inspect
import json
import re

import pytest

from sys_buddy import onboarding, service, tools
from tests.conftest import seed_agent, seed_task

# A real status update, shaped the way the briefing teaches: lead sentence, heading,
# bullets, floor-passing line. Comfortably over the threshold in total length — the
# point being that LENGTH is not what fires the nudge.
WELL_SHAPED = """Login is wired end to end and the happy path passes locally.

WHAT CHANGED:
- POST /api/auth/login now returns 401 with {"error":"bad_credentials"} instead of 400
- the refresh cookie is set HttpOnly + SameSite=Lax on both the login and refresh paths
- rate limiting moved in front of the handler, so a wrong password costs a token too

OPEN QUESTIONS:
- do you want the 401 body to carry a retry_after, or is the header enough for you?
- I left the old 400 in place for one release so nothing of yours breaks mid-deploy

Your floor — tell me which of those two you want and I'll land it.
"""

WALL = (
    "Login is wired end to end and the happy path passes locally. POST /api/auth/login "
    "now returns 401 with a bad_credentials error instead of 400, the refresh cookie is "
    "set HttpOnly and SameSite=Lax on both the login and the refresh paths, and rate "
    "limiting moved in front of the handler so a wrong password costs a token too. Do "
    "you want the 401 body to carry a retry_after, or is the header enough for you? I "
    "left the old 400 in place for one release so nothing of yours breaks mid-deploy."
)


def _agents(conn, task="signin"):
    """A two-seat task; returns ``{role: Identity}``. Both seats exist so the receipt
    carries a real recipient count — the string these tests pin is the whole feature."""
    seed_task(conn, task, roles=("backend", "frontend"))
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in ("backend", "frontend")
    }


def _ident(conn, role="backend", task="signin"):
    return _agents(conn, task)[role]


# --- the headline: a shaped message gets a clean receipt --------------------
def test_a_well_shaped_message_is_not_nudged(conn):
    """THE test. If this ever fails the feature is worse than not shipping it."""
    receipt = tools._op_send(_ident(conn), "status_update", WELL_SHAPED)

    assert receipt == "Delivered to task 'signin' (1 recipient(s)). id=1"


def test_a_short_one_liner_is_not_nudged(conn):
    receipt = tools._op_send(_ident(conn), "answer", "Yes — 401 with bad_credentials.")

    assert "NOTE" not in receipt


def test_a_long_body_with_any_line_breaks_is_not_nudged(conn):
    """Two paragraphs and nothing else is already readable; only the block is flagged."""
    body = WALL[:200] + "\n\n" + WALL[200:]

    assert "NOTE" not in tools._op_send(_ident(conn), "status_update", body)


# --- the case it exists for -------------------------------------------------
def test_a_wall_of_text_is_nudged_on_the_real_send_path(conn):
    receipt = tools._op_send(_ident(conn), "status_update", WALL)

    assert receipt.startswith("Delivered to task 'signin' (1 recipient(s)). id=1\n")
    assert "NOTE" in receipt
    assert str(len(WALL)) in receipt  # the count is the evidence, not an adjective


def test_the_nudge_never_blocks_delivery(conn):
    """It is a receipt line, not a gate: the message is on the wire before it runs."""
    ag = _agents(conn)
    tools._op_send(ag["backend"], "status_update", WALL)

    inbox = service.fetch_unacked(conn, ag["frontend"])
    assert len(inbox) == 1
    assert WALL in json.dumps(inbox[0]["content"])  # delivered whole, untouched


# --- the threshold ----------------------------------------------------------
def test_the_threshold_is_the_only_thing_separating_the_two(conn):
    ident = _ident(conn)
    under = "x" * tools._WALL_OF_TEXT_CHARS
    over = "x" * (tools._WALL_OF_TEXT_CHARS + 1)

    assert "NOTE" not in tools._op_send(ident, "status_update", under)
    assert "NOTE" in tools._op_send(ident, "status_update", over)


def test_a_trailing_newline_does_not_buy_a_pass():
    """It would still render as one block — `proseBlocks` drops the empty tail line."""
    assert tools._shape_note(WALL + "\n") != ""
    assert tools._shape_note("\n\n  " + WALL + "  \n") != ""


# --- never raises, under any input ------------------------------------------
@pytest.mark.parametrize(
    "body",
    [None, "", "   ", "\n\n\n", 0, 12345, b"bytes", ["not", "a", "string"], object()],
)
def test_degenerate_bodies_return_a_note_of_nothing(body):
    """A courtesy line must never be what breaks an agent's turn."""
    assert tools._shape_note(body) == ""


def test_one_enormous_line_is_nudged_not_fatal():
    note = tools._shape_note("y" * 50_000)

    assert "50000 characters" in note


# --- one implementation, both surfaces --------------------------------------
def test_both_send_message_registrations_go_through_the_shared_op():
    """Remote and local differ only in how identity is resolved. A receipt formatted a
    second time in either registration is how the two would drift."""
    src = inspect.getsource(tools)
    for register in ("_register_remote", "_register_local"):
        body = src.split(f"def {register}(")[1].split("\ndef ")[0]
        send = body.split("def send_message(")[1].split("\n    @mcp.tool")[0]
        assert "_op_send(" in send
        assert "Delivered to task" not in send  # the receipt is written once, in _op_send


# --- and it must not teach a shape the briefing doesn't ---------------------
def test_the_note_only_names_shapes_the_briefing_teaches():
    """The nudge is deliberately terser than ``onboarding.WRITING_A_MESSAGE`` — a receipt
    names the fix for the mistake at hand, not the whole convention. Terser is fine;
    DIFFERENT is not, so the shapes it does name are held to the briefing's words."""
    note = tools._shape_note("z" * 500)
    briefing = onboarding.WRITING_A_MESSAGE

    assert "blank line" in note.lower() and "blank line" in briefing.lower()
    assert "`- `" in note and "`- `" in briefing
    assert "paragraph" in note and "paragraph" in briefing
    assert "bullet" in note and "bullet" in briefing
    # No markup may leak into a nudge that exists to say "this isn't markdown".
    assert not re.search(r"\*\*|^#", note, re.M)
