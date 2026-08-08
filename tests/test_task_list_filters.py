"""The host's task board: newest first, closed hidden, filtered and paged IN SQL.

``/api/tasks`` used to be ``ORDER BY created_at`` ascending with no ``closed_at`` filter
and no page — so a host with fifty tasks got all fifty in one payload with the ones they
were working on at the BOTTOM of the scroll, under every task they had ever finished.

Two properties these specs exist to defend, beyond the obvious ordering:

1. **Scoping is still a WHERE, not a post-filter.** ``_list_tasks_for``'s contract is that
   another task's existence is not observable to a buddy. Filters and pagination are a
   HOST-side ``WHERE`` bolted onto the host branch only; the buddy branch is the same
   ``WHERE id = ?`` it has always been. A buddy must never be able to make the API say
   anything about tasks that are not theirs — including "there are 40 of them".
2. **The filters are not dead controls on a buddy's screen.** A buddy's token resolves to
   exactly one task, so a status chip could only ever hide the one thing they came to
   look at. The API says who may filter by including ``page`` for a host and omitting it
   for a buddy, and the dashboard keys its controls off exactly that.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from sys_buddy import api
from sys_buddy.identity import resolve_viewer_token
from tests.conftest import seed_task, seed_viewer

UI = Path(__file__).resolve().parents[1] / "src" / "sys_buddy" / "ui.html"

DAY = 86400.0


def _at(conn, task_id, when):
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (when, task_id))
    conn.commit()
    return task_id


def _close(conn, task_id, when=None):
    conn.execute(
        "UPDATE tasks SET closed_at = ? WHERE id = ?", (when or time.time(), task_id)
    )
    conn.commit()
    return task_id


def _state(conn, task_id, st):
    conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (st, task_id))
    conn.commit()
    return task_id


def _host(conn):
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    return resolve_viewer_token(conn, "sbv_host")


def _buddy(conn, task_id):
    seed_viewer(conn, "dave", "sbv_dave", task_id=task_id)
    return resolve_viewer_token(conn, "sbv_dave")


def _ids(rows):
    return [r["id"] for r in rows]


# --------------------------------------------------------------------------- #
# newest first, closed hidden — the two that may fix the complaint on their own
# --------------------------------------------------------------------------- #
def test_the_board_is_newest_first(conn):
    now = time.time()
    _at(conn, seed_task(conn, "oldest"), now - 3 * DAY)
    _at(conn, seed_task(conn, "middle"), now - 2 * DAY)
    _at(conn, seed_task(conn, "newest"), now - DAY)
    assert _ids(api._list_tasks_for(conn, _host(conn))) == ["newest", "middle", "oldest"]


def test_closed_tasks_are_hidden_by_default(conn):
    seed_task(conn, "live")
    _close(conn, seed_task(conn, "done"))
    assert _ids(api._list_tasks_for(conn, _host(conn))) == ["live"]


def test_closed_tasks_are_one_filter_away_not_gone(conn):
    seed_task(conn, "live")
    _close(conn, seed_task(conn, "done"))
    viewer = _host(conn)
    assert _ids(api._list_tasks_for(conn, viewer, status="closed")) == ["done"]
    assert set(_ids(api._list_tasks_for(conn, viewer, status="all"))) == {"live", "done"}


def test_a_row_says_whether_it_is_closed(conn):
    """`closed` is not a `state` — a task is closed AT whatever state it reached — so
    without its own field a closed row surfaced by the filter is indistinguishable from a
    live one sitting at the same state."""
    seed_task(conn, "live")
    _close(conn, _state(conn, seed_task(conn, "done"), "verified"))
    rows = {r["id"]: r for r in api._list_tasks_for(conn, _host(conn), status="all")}
    assert rows["done"]["closed"] is True
    assert rows["live"]["closed"] is False


# --------------------------------------------------------------------------- #
# the status buckets are a partition
# --------------------------------------------------------------------------- #
def test_each_status_bucket_selects_its_own_tasks(conn):
    seed_task(conn, "fresh")                                  # state 'open'
    _state(conn, seed_task(conn, "marching"), "contract_locked")
    _state(conn, seed_task(conn, "done"), "verified")
    _state(conn, seed_task(conn, "wedged"), "stuck")
    _close(conn, seed_task(conn, "shut"))
    viewer = _host(conn)
    assert _ids(api._list_tasks_for(conn, viewer, status="open")) == ["fresh"]
    assert _ids(api._list_tasks_for(conn, viewer, status="in_flight")) == ["marching"]
    assert _ids(api._list_tasks_for(conn, viewer, status="verified")) == ["done"]
    assert _ids(api._list_tasks_for(conn, viewer, status="stuck")) == ["wedged"]
    assert _ids(api._list_tasks_for(conn, viewer, status="closed")) == ["shut"]


def test_the_buckets_partition_the_board(conn):
    """Every task lands in exactly one bucket, so picking a filter can never hide a row
    that another filter also claims — and nothing can fall through every bucket and become
    reachable only via `all`."""
    seed_task(conn, "fresh")
    _state(conn, seed_task(conn, "marching"), "testing")
    _state(conn, seed_task(conn, "done"), "confirmed")
    _state(conn, seed_task(conn, "resolved-one"), "resolved")
    _state(conn, seed_task(conn, "wedged"), "stuck")
    _close(conn, _state(conn, seed_task(conn, "shut"), "testing"))
    viewer = _host(conn)

    every = _ids(api._list_tasks_for(conn, viewer, status="all"))
    seen = []
    for bucket in ("open", "in_flight", "verified", "stuck", "closed"):
        seen += _ids(api._list_tasks_for(conn, viewer, status=bucket))
    assert sorted(seen) == sorted(every)
    assert len(seen) == len(set(seen)), "a task is in two buckets at once"


def test_a_closed_task_is_only_ever_in_the_closed_bucket(conn):
    """`closed` is orthogonal to the state machine, so a closed-while-stuck task must not
    also answer the `stuck` filter — that is how a finished task keeps haunting a board."""
    _close(conn, _state(conn, seed_task(conn, "shut"), "stuck"))
    viewer = _host(conn)
    assert _ids(api._list_tasks_for(conn, viewer, status="stuck")) == []
    assert _ids(api._list_tasks_for(conn, viewer, status="closed")) == ["shut"]


# --------------------------------------------------------------------------- #
# date range
# --------------------------------------------------------------------------- #
def test_the_date_range_is_a_where_not_a_render(conn):
    now = time.time()
    _at(conn, seed_task(conn, "ancient"), now - 30 * DAY)
    _at(conn, seed_task(conn, "recent"), now - 2 * DAY)
    viewer = _host(conn)
    rows = api._list_tasks_for(conn, viewer, since=now - 7 * DAY)
    assert _ids(rows) == ["recent"], "the payload still carries a task outside the range"
    assert _ids(api._list_tasks_for(conn, viewer, until=now - 7 * DAY)) == ["ancient"]


def test_a_date_param_is_local_midnight_and_the_end_is_inclusive():
    """The dashboard prints every other time in local time (`_hhmm` uses localtime), so a
    range that quietly meant UTC would drop or add a day's tasks at either end. And "to
    the 5th" means the whole of the 5th — the bound is the start of the 6th."""
    start = api._date_param("2026-08-05")
    end = api._date_param("2026-08-05", end_of_day=True)
    assert time.localtime(start)[:6] == (2026, 8, 5, 0, 0, 0)
    assert end - start == DAY
    assert api._date_param(None) is None
    assert api._date_param("") is None
    # Garbage means "no bound" — never an error screen, never a silently empty board.
    assert api._date_param("last tuesday") is None
    assert api._date_param("2026-13") is None


def test_a_bad_page_param_falls_back_instead_of_breaking(conn):
    assert api._int_param(None, 25) == 25
    assert api._int_param("", 25) == 25
    assert api._int_param("nope", 25) == 25
    assert api._int_param("-3", 25) == 25
    assert api._int_param("7", 25) == 7


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #
def test_pagination_windows_the_board(conn):
    now = time.time()
    for i in range(7):
        _at(conn, seed_task(conn, f"t{i}"), now - i * DAY)   # t0 newest
    viewer = _host(conn)
    assert _ids(api._list_tasks_for(conn, viewer, limit=3)) == ["t0", "t1", "t2"]
    assert _ids(api._list_tasks_for(conn, viewer, limit=3, offset=3)) == ["t3", "t4", "t5"]
    assert _ids(api._list_tasks_for(conn, viewer, limit=3, offset=6)) == ["t6"]
    assert api._list_tasks_for(conn, viewer, limit=3, offset=99) == []


def test_the_page_is_stable_when_tasks_share_a_second(conn):
    """Two tasks created in the same second must not swap places between one page request
    and the next — that is how a row appears on both pages, or on neither."""
    now = time.time()
    for name in ("a", "b", "c", "d"):
        _at(conn, seed_task(conn, name), now)
    viewer = _host(conn)
    first = _ids(api._list_tasks_for(conn, viewer, limit=2))
    second = _ids(api._list_tasks_for(conn, viewer, limit=2, offset=2))
    assert first + second == _ids(api._list_tasks_for(conn, viewer, limit=4))
    assert set(first) & set(second) == set()


def test_the_count_describes_the_same_population_as_the_rows(conn):
    now = time.time()
    for i in range(5):
        _at(conn, seed_task(conn, f"t{i}"), now - i * DAY)
    _close(conn, seed_task(conn, "shut"))
    viewer = _host(conn)
    assert api._count_tasks_for(conn, viewer) == 5           # closed excluded, as the rows are
    assert api._count_tasks_for(conn, viewer, status="all") == 6
    assert api._count_tasks_for(conn, viewer, since=now - 2.5 * DAY) == 3


# --------------------------------------------------------------------------- #
# the scoping property — the one that must not bend
# --------------------------------------------------------------------------- #
def test_a_buddy_still_sees_exactly_their_task_whatever_is_asked_for(conn):
    """Scoping is a WHERE, not a post-filter. Every knob below is a host-side knob; none
    of them may become a way for a buddy's token to describe anything else."""
    seed_task(conn, "signin")
    seed_task(conn, "billing")
    seed_task(conn, "search")
    viewer = _buddy(conn, "signin")
    for kwargs in (
        {},
        {"status": "all"},
        {"status": "closed"},
        {"limit": 50},
        {"limit": 50, "offset": 0},
        {"since": 0.0, "until": time.time() + DAY},
    ):
        assert _ids(api._list_tasks_for(conn, viewer, **kwargs)) == ["signin"], kwargs


def test_a_buddy_whose_task_is_closed_still_sees_it(conn):
    """The filters do NOT apply to the buddy branch, and this is why. A buddy's dashboard
    is their one task; hiding it because it is closed (or because it falls outside a range
    they never chose) would hand them an empty screen and no control to fix it."""
    _close(conn, seed_task(conn, "signin"))
    assert _ids(api._list_tasks_for(conn, _buddy(conn, "signin"))) == ["signin"]


def test_a_buddy_cannot_count_the_board(conn):
    seed_task(conn, "signin")
    seed_task(conn, "billing")
    seed_task(conn, "search")
    assert api._count_tasks_for(conn, _buddy(conn, "signin"), status="all") == 1


def test_only_a_host_is_told_there_are_pages(conn):
    """`page` is the API saying who may filter, and the dashboard renders its controls off
    exactly that — so a buddy gets no chips and no pager rather than dead ones."""
    seed_task(conn, "signin")
    seed_task(conn, "billing")
    host = api._tasks_payload(conn, _host(conn), limit=25)
    buddy = api._tasks_payload(conn, _buddy(conn, "signin"), limit=25)
    assert host["page"]["total"] == 2 and host["page"]["limit"] == 25
    assert "page" not in buddy
    assert _ids(buddy["tasks"]) == ["signin"]


def test_the_payload_still_carries_the_viewer_block(conn):
    seed_task(conn, "signin")
    body = api._tasks_payload(conn, _host(conn))
    assert body["viewer"]["mode"] == "host"
    assert "tasks" in body


# --------------------------------------------------------------------------- #
# the dashboard's chips and the broker's buckets are one vocabulary
# --------------------------------------------------------------------------- #
def test_the_dashboards_filter_chips_are_the_brokers_buckets():
    """Two lists of status names is one list too many: a chip the broker doesn't know is a
    button that silently does nothing, and a bucket with no chip is unreachable."""
    ui = UI.read_text(encoding="utf-8")
    block = re.search(r"var TASK_FILTERS=\[(.*?)\];", ui, re.S)
    assert block, "TASK_FILTERS no longer parses out of ui.html"
    chips = re.findall(r"\['([a-z_]+)','[^']+'\]", block.group(1))
    assert chips == list(api.TASK_STATUS_FILTERS)


def test_the_dashboard_asks_the_server_rather_than_filtering_what_it_has():
    ui = UI.read_text(encoding="utf-8")
    assert "function tasksQuery()" in ui
    assert "'/api/tasks'+tasksQuery()" in ui
    assert "state.tasksPage=d.page||null" in ui
    # The controls are drawn only when the API sent a page block, i.e. for a host.
    assert re.search(r"function tasksControlsHTML\(\)\{\s*var p=state\.tasksPage;\s*if\(!p\) return '';", ui)
    assert re.search(r"function tasksPagerHTML\(\)\{\s*var p=state\.tasksPage;\s*if\(!p", ui)


def test_an_unknown_status_is_the_default_rather_than_an_empty_board(conn):
    seed_task(conn, "live")
    assert _ids(api._list_tasks_for(conn, _host(conn), status="banana")) == ["live"]
    # ...though the route refuses it outright, so a typo shows up as a 400 and not as a
    # board that quietly ignored the filter you clicked.
    assert "banana" not in api.TASK_STATUS_FILTERS


def test_the_json_route_shape_is_json_serialisable(conn):
    seed_task(conn, "live")
    json.dumps(api._tasks_payload(conn, _host(conn), limit=25))
