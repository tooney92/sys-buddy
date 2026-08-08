"""The message thread reads NEWEST FIRST.

The thread box is 640px tall and a working task outgrows it in an afternoon, so
oldest-first meant scrolling to the bottom of the whole conversation every time you came
to read the one thing that had just happened.

Two things had to move together, and both are pinned here:

* ``buildThread`` sorts DESCENDING — both keys, ``ts`` and its ``ord`` tiebreak, so items
  written in the same instant reverse as a pair and the ordering stays total;
* the scroll pin flips from bottom to top, KEEPING the property v2.2.0 added — a reader
  who has deliberately scrolled away is left alone. That defect (a thread that jumped on
  every new message) is the same defect either way round, so the guard is inverted rather
  than dropped.

The API is untouched: ``_messages_for`` still returns oldest-first with a raw ``ts`` per
row, because a wire format that flips with a render choice is a wire format that breaks
every other reader. The reversal is the dashboard's, and it is asserted here against
``ui.html`` the way ``tests/test_shortcodes.py`` asserts the rest of that file.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from sys_buddy import api
from tests.conftest import seed_agent, seed_task
from tests.test_api import _message

UI = Path(__file__).resolve().parents[1] / "src" / "sys_buddy" / "ui.html"


def _ui() -> str:
    return UI.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the wire stays as it was
# --------------------------------------------------------------------------- #
def test_the_api_still_hands_over_oldest_first_with_a_raw_ts(conn):
    """The dashboard reverses; the API does not. `ts` is what makes a sub-second-accurate
    reversal possible at all — reversing an HH:MM string would shuffle everything written
    inside the same minute."""
    seed_task(conn, "signin")
    agent = seed_agent(conn, "signin", "backend", "Alex", "tok")
    now = time.time()
    for i in range(5):
        _message(conn, "signin", agent, "status", f"m{i}", at=now + i)
    msgs = api._messages_for(conn, "signin")
    assert [m["body"] for m in msgs] == ["m0", "m1", "m2", "m3", "m4"]
    assert [m["ts"] for m in msgs] == sorted(m["ts"] for m in msgs)


# --------------------------------------------------------------------------- #
# the thread reverses, on BOTH keys
# --------------------------------------------------------------------------- #
def test_build_thread_sorts_descending():
    ui = _ui()
    assert "items.sort(function(a,b){ return (b.ts-a.ts) || (b.ord-a.ord); })" in ui
    assert "items.sort(function(a,b){ return (b.min-a.min) || (b.ord-a.ord); })" in ui
    # The old ascending comparators must be gone, not merely joined by new ones.
    assert "(a.ts-b.ts)" not in ui
    assert "(a.min-b.min)" not in ui


def test_the_tiebreak_reverses_with_the_timestamp():
    """`ord` is what keeps same-instant items in a stable order. Reversing `ts` and
    leaving `ord` ascending would give newest-minute-first with stale order inside it —
    a thread that is neither one thing nor the other."""
    ui = _ui()
    for comparator in re.findall(r"items\.sort\(function\(a,b\)\{ return ([^;]+); \}\)", ui):
        assert "(b.ord-a.ord)" in comparator, comparator


def test_the_ts_fallback_survives_the_reversal():
    """A stale API (a broker mid-restart) hands back items with no `ts`. That path still
    has to sort — the same way round as the other one."""
    ui = _ui()
    assert "var allTs = items.length>0 && items.every(" in ui


# --------------------------------------------------------------------------- #
# the pin flips, and keeps the property it was added for
# --------------------------------------------------------------------------- #
def test_the_thread_opens_on_the_newest_message_which_is_now_the_top():
    ui = _ui()
    assert "if(!prev || prev.atTop) el.scrollTop=0;" in ui
    assert "el.scrollTop=el.scrollHeight" not in ui, "the pin is still on the bottom"


def test_a_reader_who_scrolled_away_is_left_alone():
    """The v2.2.0 fix, inverted rather than dropped: `render()` replaces the whole page on
    every sig change — and a new message IS one — so without this, a peer talking yanks
    the reader out of the history they had scrolled down into."""
    ui = _ui()
    assert "else el.scrollTop=prev.top;" in ui
    assert re.search(r"return \{top:el\.scrollTop, atTop:el\.scrollTop<=40\};", ui), (
        "the 'am I at the pinned end' test is gone or no longer carries its slack"
    )
    assert "atBottom" not in ui, "a stale bottom-pin test is still in the file"


def test_the_direction_is_stated_on_screen():
    """A reader who knows this thread from before has to be TOLD it turned around, and a
    new one needs the `Task created` divider at the bottom to read as the beginning rather
    than as a bug."""
    ui = _ui()
    assert "newest first</span></span>" in ui


def test_the_comment_no_longer_teaches_the_old_order():
    """These comments are the file's documentation. One left saying the newest message is
    at the bottom is worse than none — the next reader believes it over the code."""
    ui = _ui()
    assert "The newest message is at the BOTTOM" not in ui
    assert "Pinned to the bottom instead" not in ui
