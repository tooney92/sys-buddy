"""Every element id in a shipped HTML asset must be unique.

THE BUG THIS CATCHES. The Resume view added a button with `id="btn-open-dash"` — a name the
host-run screen was already using. `getElementById` returns the FIRST match in the document,
and the new view sits earlier in the file, so the host screen's own wiring
(`var openDash = document.getElementById('btn-open-dash')`) silently bound to the new,
hidden button instead of its own. The host's "Open dashboard" button then did nothing at
all: no error, no console warning, just a dead button on the screen a host uses right after
creating a task.

Nothing about that is detectable from either feature in isolation, which is exactly why it
shipped. A duplicate id is cheap to assert and impossible to argue with, so assert it.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

ASSETS = ("gui_app.html", "ui.html", "join.html")
# Only plain literal ids. Anything containing a quote, brace or `+` is a fragment of a
# JavaScript template (`id="row-' + n + '"`), whose uniqueness is a runtime property.
LITERAL_ID = re.compile(r'id="([A-Za-z][A-Za-z0-9_-]*)"')


def _asset(name: str) -> str:
    return (Path(__file__).resolve().parent.parent / "src" / "sys_buddy" / name).read_text()


@pytest.mark.parametrize("name", ASSETS)
def test_no_duplicate_element_ids(name):
    counts = Counter(LITERAL_ID.findall(_asset(name)))
    dupes = {i: n for i, n in counts.items() if n > 1}
    assert not dupes, (
        f"{name} declares these ids more than once: {dupes}. "
        "getElementById returns only the first, so whichever feature is later in the file "
        "silently loses its wiring — see this file's docstring."
    )


def test_the_two_dashboard_buttons_are_distinct():
    """A named regression: the Resume view and the host-run screen each own a button that
    opens the dashboard, and they must not share an id."""
    html = _asset("gui_app.html")
    assert 'id="btn-resume-dash"' in html, "Resume view's button"
    assert 'id="btn-open-dash"' in html, "host-run screen's button"
    assert html.count('id="btn-open-dash"') == 1
