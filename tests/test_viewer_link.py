"""Specs for reissuing a BUDDY's dashboard link.

THE GAP THIS CLOSES. A guest who loses her dashboard link gets a new one in a click. A
buddy who loses his had NO path: viewer tokens are stored only hashed, so the original
cannot be read back, and the only recovery was to revoke a perfectly healthy seat and
re-pair it — costing him his agent token, his MCP config and his pre-flight, all to
recover a URL he mislaid. And a dashboard link is the single most losable thing the broker
hands out, because the HOST never holds a copy: pairing gives it to the buddy at redeem
time and nowhere else.

The load-bearing guard is that the reissued viewer is READ-ONLY. `viewers.agent_id` is what
opens the narrow `/guest/*` write surface; it belongs to guest seats alone. Reissuing a
buddy's link must never quietly promote him to something that can write from a browser.
"""

from __future__ import annotations

import pytest

from sys_buddy import admin, seats
from tests.conftest import seed_agent, seed_task


def _task(conn, task="signin"):
    seed_task(conn, task, roles=("backend", "frontend"))
    seed_agent(conn, task, "backend", "Tony", f"sbk_{task}_be")
    seed_agent(conn, task, "frontend", "Peter", f"sbk_{task}_fe")
    conn.commit()
    return task


def test_reissues_for_a_buddy_by_handle(conn):
    t = _task(conn)
    res = admin.reissue_viewer_link(t, "frontend")
    assert res["seat"] == "frontend"
    assert res["name"] == "Peter"
    assert res["viewer_token"].startswith("sbv_")


def test_reissues_by_display_name_too(conn):
    """A host thinks in people, not handles — "reissue for Peter" must work."""
    t = _task(conn)
    assert admin.reissue_viewer_link(t, "Peter")["seat"] == "frontend"


def test_the_reissued_viewer_is_READ_ONLY(conn):
    """The guard this whole feature rests on. A non-NULL agent_id is the guest WRITE
    surface; a reissued buddy link must never carry one."""
    t = _task(conn)
    admin.reissue_viewer_link(t, "frontend")
    row = conn.execute(
        "SELECT agent_id FROM viewers WHERE task_id=? ORDER BY id DESC LIMIT 1", (t,)
    ).fetchone()
    assert row["agent_id"] is None, "a reissued buddy viewer must be read-only"


def test_it_is_scoped_to_the_one_task_not_all_tasks(conn):
    """A host viewer sees every task. Handing a buddy one of those would be a privilege
    escalation dressed up as a convenience."""
    t = _task(conn)
    admin.reissue_viewer_link(t, "frontend")
    row = conn.execute(
        "SELECT task_id FROM viewers ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["task_id"] == t, "scoped to this task, never all-tasks"


def test_the_seat_is_unchanged_so_history_stays_his(conn):
    """Same seat, new credential — the whole point of reissuing rather than re-pairing."""
    t = _task(conn)
    before = conn.execute(
        "SELECT id FROM agents WHERE task_id=? AND handle='frontend'", (t,)
    ).fetchone()["id"]
    admin.reissue_viewer_link(t, "frontend")
    after = conn.execute(
        "SELECT id, revoked_at FROM agents WHERE task_id=? AND handle='frontend'", (t,)
    ).fetchone()
    assert after["id"] == before, "the agent seat is untouched"
    assert after["revoked_at"] is None, "and emphatically NOT revoked"


def test_a_guest_is_refused_and_pointed_at_guest_link(conn):
    """A guest's link is write-capable and has its own path. Silently minting her a
    read-only one here would take her message box away."""
    t = _task(conn)
    admin.add_guest(t, "Ada")
    with pytest.raises(ValueError, match="guest"):
        admin.reissue_viewer_link(t, "Ada")


def test_an_unknown_seat_is_refused_and_names_the_real_ones(conn):
    t = _task(conn)
    with pytest.raises(ValueError) as e:
        admin.reissue_viewer_link(t, "nobody")
    msg = str(e.value)
    assert "nobody" in msg
    assert "frontend" in msg and "backend" in msg, "say which seats DO exist"


def test_a_revoked_seat_is_refused(conn):
    """Reissuing a link for someone the host deliberately cut off would re-admit them to
    the board through the back door."""
    t = _task(conn)
    admin.revoke_agent("Peter", task=t)
    with pytest.raises(ValueError, match="no live seat"):
        admin.reissue_viewer_link(t, "frontend")


def test_a_blank_who_is_refused(conn):
    t = _task(conn)
    with pytest.raises(ValueError, match="name the seat"):
        admin.reissue_viewer_link(t, "   ")


def test_an_unknown_task_is_refused(conn):
    _task(conn)
    with pytest.raises(ValueError, match="unknown task"):
        admin.reissue_viewer_link("no-such-task", "frontend")
