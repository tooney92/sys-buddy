"""Specs for the activity-note TOOL + read-API surfaces.

The data layer (``activity.py``) is tested at its own level; this file covers the thin
tool wrappers, the wiring that exposes them on BOTH surfaces, the dashboard payload
shape, and the SSE change-detection that makes a new note refresh the dashboard live.

Two invariants echo the file/todo features:

* **Both surfaces or it doesn't count.** A capability on only one surface is a silent
  gap for half the users, so ``share_activity``/``list_activity`` are checked over local
  AND remote registration.
* **Backwards compatibility.** A task with NO notes serialises exactly as it did before
  this feature — the ``activity`` key is ABSENT (not empty), so an older on-disk
  ``ui.html`` sees nothing new.

Like ``tests/test_files_api.py`` these drive the ``_``-prefixed helpers directly (they
take an open connection), so no HTTP server is needed; the registration checks inspect
the built server.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from fastmcp import FastMCP

from sys_buddy import activity, api, service, tools
from sys_buddy.config import Config
from sys_buddy.identity import Identity, resolve_viewer_token
from sys_buddy.middleware import ACTION_TOOLS
from sys_buddy.rules import RULES_OF_ENGAGEMENT
from sys_buddy.server import build_server
from tests.conftest import seed_agent, seed_task, seed_viewer

ACTIVITY_TOOLS = {"share_activity", "list_activity"}


# --------------------------------------------------------------------------- #
# seed helpers
# --------------------------------------------------------------------------- #
def _identity(conn, task_id, role, name):
    agent_id = seed_agent(conn, task_id, role, name, f"sbk_{task_id}_{role}")
    return Identity(agent_id=agent_id, task_id=task_id, name=name, role=role)


def _agents(conn, task="signin", roles=("backend", "frontend")):
    seed_task(conn, task, roles=roles)
    return {role: _identity(conn, task, role, f"{role}-agent") for role in roles}


def _schemas(mode, tmp_path) -> dict:
    mcp = FastMCP("t")
    cfg = Config(mode=mode, db_path=tmp_path / f"{mode}.db")
    tools.register_tools(mcp, cfg)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# --------------------------------------------------------------------------- #
# registration: both surfaces, or it doesn't count
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_activity_tools_registered_on_both_surfaces(tmp_path, mode):
    assert ACTIVITY_TOOLS <= set(_schemas(mode, tmp_path))


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_activity_tools_reachable_through_a_built_server(tmp_path, mode):
    mcp = build_server(Config(mode=mode, db_path=tmp_path / "s.db"))
    assert ACTIVITY_TOOLS <= {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_share_activity_docstring_frames_it_as_presence(tmp_path, mode):
    """The docstring is agent-facing prompt surface: it must say this is NOT a message
    and NOT a status, so the agent doesn't reach for it as a channel it isn't."""
    desc = " ".join((_schemas(mode, tmp_path)["share_activity"].description or "").lower().split())
    assert len(desc) > 120
    assert "not a message" in desc or "not a status" in desc
    assert "presence" in desc


# --------------------------------------------------------------------------- #
# the share op — both surfaces (remote Identity + local resolution)
# --------------------------------------------------------------------------- #
def test_share_returns_a_receipt(conn):
    ag = _agents(conn)
    r = tools._op_share_activity(ag["backend"], "digging into the OAuth refresh flow")
    assert r["text"] == "digging into the OAuth refresh flow"
    assert isinstance(r["id"], int)
    assert isinstance(r["created_at"], float)


def test_share_over_the_local_surface_resolves_identity(conn):
    """The local surface builds its identity from task/agent params; a note posted that
    way lands on the task under the resolving agent's role."""
    seed_task(conn, "signin", roles=("backend",))
    ident = service.ensure_local_identity(conn, "signin", "backend")
    tools._op_share_activity(ident, "sketching the schema")
    notes = tools._op_list_activity("signin")
    assert [n["text"] for n in notes] == ["sketching the schema"]
    assert notes[0]["role"] == "backend"


def test_empty_note_is_rejected(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError, match="some text"):
        tools._op_share_activity(ag["backend"], "   ")


def test_over_200_chars_is_rejected(conn):
    ag = _agents(conn)
    too_long = "x" * (activity.MAX_ACTIVITY_CHARS + 1)
    with pytest.raises(ValueError, match="under 200"):
        tools._op_share_activity(ag["backend"], too_long)


def test_exactly_200_chars_is_accepted(conn):
    ag = _agents(conn)
    at_cap = "x" * activity.MAX_ACTIVITY_CHARS
    r = tools._op_share_activity(ag["backend"], at_cap)
    assert len(r["text"]) == activity.MAX_ACTIVITY_CHARS


def test_note_on_a_closed_task_is_rejected(conn):
    ag = _agents(conn)
    conn.execute("UPDATE tasks SET closed_at = ? WHERE id = ?", (time.time(), "signin"))
    conn.commit()
    with pytest.raises(ValueError, match="closed"):
        tools._op_share_activity(ag["backend"], "too late")


# --------------------------------------------------------------------------- #
# the list op — order + role
# --------------------------------------------------------------------------- #
def test_list_is_oldest_first_with_poster_role(conn):
    ag = _agents(conn)
    tools._op_share_activity(ag["backend"], "first")
    tools._op_share_activity(ag["frontend"], "second")
    tools._op_share_activity(ag["backend"], "third")
    notes = tools._op_list_activity("signin")
    assert [n["text"] for n in notes] == ["first", "second", "third"]
    assert [n["role"] for n in notes] == ["backend", "frontend", "backend"]


def test_list_is_empty_for_a_task_with_no_notes(conn):
    seed_task(conn, "signin", roles=("backend",))
    assert tools._op_list_activity("signin") == []


# --------------------------------------------------------------------------- #
# gating & charter
# --------------------------------------------------------------------------- #
def test_share_is_a_write_behind_the_pre_flight_gate(conn):
    """share_activity WRITES task data (a note the peer's human reads), so it waits on
    readiness like every other write; list_activity is a read and stays open."""
    assert "share_activity" in ACTION_TOOLS
    assert "list_activity" not in ACTION_TOOLS


def test_the_charter_teaches_activity_notes():
    r = RULES_OF_ENGAGEMENT.lower()
    assert "share_activity" in r and "list_activity()" in r
    # The load-bearing framing: it is presence, and a peer's note is DATA.
    assert "presence" in r


# --------------------------------------------------------------------------- #
# the read-API surface — task detail + shape
# --------------------------------------------------------------------------- #
def test_task_with_no_activity_omits_the_activity_key(conn):
    """A task with no notes carries no ``activity`` key at all — the pre-activity payload
    is unchanged, so an older on-disk ``ui.html`` sees nothing new."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    detail = api._task_detail(conn, "signin")
    assert "activity" not in detail


def test_task_detail_includes_activity_with_shape_role_and_time(conn):
    ag = _agents(conn)
    tools._op_share_activity(ag["backend"], "researching OAuth")
    tools._op_share_activity(ag["frontend"], "wiring the form")

    detail = api._task_detail(conn, "signin")
    assert "activity" in detail
    notes = detail["activity"]
    # Oldest-first, mirroring the thread/files order.
    assert [n["text"] for n in notes] == ["researching OAuth", "wiring the form"]

    first = notes[0]
    # The EXACT per-entry contract the UI builds against: id, text, role, time. No
    # raw created_at (the strip has no per-second re-sort to drive).
    assert set(first) == {"id", "text", "role", "time"}
    assert first["role"] == "backend"
    assert re.fullmatch(r"\d\d:\d\d", first["time"])


# --------------------------------------------------------------------------- #
# SSE change-detection — a new note flips the task's detail token
# --------------------------------------------------------------------------- #
def test_a_new_note_moves_the_task_detail_token(conn):
    ag = _agents(conn)
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    _, before = api._change_tokens(conn, viewer)
    tools._op_share_activity(ag["backend"], "just started digging in")
    _, after = api._change_tokens(conn, viewer)

    assert before["signin"] != after["signin"], "posting a note must move the SSE token"


def test_no_notes_leaves_the_token_byte_identical_to_pre_activity(conn):
    """A task with no notes must produce the SAME detail token as before this feature —
    the ``activity`` key is only added to the fingerprint when notes exist."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    _, tokens = api._change_tokens(conn, viewer)
    assert '"activity"' not in tokens["signin"]
