"""Specs for the read-only dashboard API (SPEC §11), focused on server-side scoping.

These exercise the query/scoping helpers directly (they take an open connection),
which is where the security-relevant logic lives — no HTTP server required. The
guiding invariant under test: a buddy token can observe exactly one task, enforced
by the server, never by the client.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from sys_buddy import api
from sys_buddy.identity import resolve_viewer_token
from tests.conftest import seed_agent, seed_task, seed_viewer


# --------------------------------------------------------------------------- #
# local seed helpers for tables conftest doesn't cover
# --------------------------------------------------------------------------- #
def _event(conn, task_id, kind, detail, at=None):
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, kind, json.dumps(detail), at if at is not None else time.time()),
    )
    conn.commit()


def _message(conn, task_id, agent_id, mtype, body, at=None):
    conn.execute(
        "INSERT INTO messages (task_id, from_agent_id, type, body_json, state_at_send, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, agent_id, mtype, json.dumps(body), "open", at if at is not None else time.time()),
    )
    conn.commit()


def _contract(conn, task_id, version, spec, status="draft", locked_at=None):
    cur = conn.execute(
        "INSERT INTO contracts (task_id, version, spec_json, status, locked_at, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, version, json.dumps(spec), status, locked_at, time.time()),
    )
    conn.commit()
    return cur.lastrowid


def _sign(conn, contract_id, agent_id, at=None):
    conn.execute(
        "INSERT INTO contract_signatures (contract_id, agent_id, signed_at) VALUES (?,?,?)",
        (contract_id, agent_id, at if at is not None else time.time()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# viewer scoping — the load-bearing behaviour
# --------------------------------------------------------------------------- #
def test_host_viewer_sees_all_tasks(conn):
    seed_task(conn, "signin")
    seed_task(conn, "billing")
    seed_task(conn, "search")
    seed_viewer(conn, "host", "sbv_host", task_id=None)

    viewer = resolve_viewer_token(conn, "sbv_host")
    tasks = api._list_tasks_for(conn, viewer)
    assert {t["id"] for t in tasks} == {"signin", "billing", "search"}


def test_buddy_viewer_sees_only_their_task(conn):
    # Three tasks exist; the buddy token is bound to exactly one.
    seed_task(conn, "signin")
    seed_task(conn, "billing")
    seed_task(conn, "search")
    seed_viewer(conn, "dave", "sbv_dave", task_id="signin")

    viewer = resolve_viewer_token(conn, "sbv_dave")
    tasks = api._list_tasks_for(conn, viewer)
    # Server-side scoping: the other tasks are not merely hidden, they're not returned.
    assert [t["id"] for t in tasks] == ["signin"]


def test_buddy_denied_other_task(conn):
    seed_task(conn, "signin")
    seed_task(conn, "billing")
    seed_viewer(conn, "dave", "sbv_dave", task_id="signin")

    viewer = resolve_viewer_token(conn, "sbv_dave")
    assert api.viewer_can_see(viewer, "signin") is True
    assert api.viewer_can_see(viewer, "billing") is False


def test_host_can_see_any_task(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    assert api.viewer_can_see(viewer, "signin") is True
    assert api.viewer_can_see(viewer, "anything") is True


def test_invalid_viewer_token_unauthorized(conn):
    seed_task(conn, "signin")
    assert resolve_viewer_token(conn, "sbv_nope") is None
    assert resolve_viewer_token(conn, "") is None


def test_revoked_viewer_token_unauthorized(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "dave", "sbv_dave", task_id="signin")
    conn.execute("UPDATE viewers SET revoked_at = ? WHERE label = 'dave'", (time.time(),))
    conn.commit()
    assert resolve_viewer_token(conn, "sbv_dave") is None


def test_viewer_block_shape(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    seed_viewer(conn, "dave", "sbv_dave", task_id="signin")

    host = api.viewer_block(resolve_viewer_token(conn, "sbv_host"))
    assert host == {"mode": "host", "label": "host"}
    assert "task_id" not in host

    buddy = api.viewer_block(resolve_viewer_token(conn, "sbv_dave"))
    assert buddy == {"mode": "buddy", "label": "dave", "task_id": "signin"}


# --------------------------------------------------------------------------- #
# task detail shape + derivations
# --------------------------------------------------------------------------- #
def test_task_detail_has_required_top_level_keys(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    detail = api._task_detail(conn, "signin")
    for key in ("id", "title", "state", "roles", "strikes", "times", "contract", "messages", "events"):
        assert key in detail
    assert detail["roles"] == ["backend", "frontend"]


def test_task_detail_missing_task_is_none(conn):
    assert api._task_detail(conn, "ghost") is None


def test_empty_task_has_sensible_empty_states(conn):
    seed_task(conn, "signin")
    detail = api._task_detail(conn, "signin")
    assert detail["messages"] == []
    assert detail["events"] == []
    # No contract yet.
    assert detail["contract"] == {"exists": False, "versions": [], "default": None, "data": {}}
    # times always has open; nothing else without transitions.
    assert set(detail["times"]) == {"open"}


def test_times_derived_from_transition_events(conn):
    seed_task(conn, "signin")
    base = time.time()
    _event(conn, "signin", "transition", {"from": "open", "to": "contract_proposed"}, at=base)
    _event(conn, "signin", "transition", {"from": "contract_proposed", "to": "contract_locked"}, at=base + 60)
    _event(conn, "signin", "transition", {"from": "contract_locked", "to": "backend_live"}, at=base + 120)

    times = api._times_for(conn, "signin", base - 300)
    assert "open" in times
    assert times["contract_proposed"] == api._hhmm(base)
    assert times["contract_locked"] == api._hhmm(base + 60)
    assert times["backend_live"] == api._hhmm(base + 120)
    # Unreached states are absent (verified/stuck optional per §11).
    assert "verified" not in times


# --------------------------------------------------------------------------- #
# contract block
# --------------------------------------------------------------------------- #
def test_contract_block_versions_and_default(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    be = seed_agent(conn, "signin", "backend", "al-backend", "sbk_be")
    fe = seed_agent(conn, "signin", "frontend", "dave-frontend", "sbk_fe")

    spec1 = {"endpoints": [{"method": "POST", "path": "/login"}], "staging_url": "https://s.example.com"}
    spec2 = {"endpoints": [{"method": "GET", "path": "/me"}], "staging_url": "https://s.example.com"}
    c1 = _contract(conn, "signin", 1, spec1, status="locked", locked_at=time.time())
    _contract(conn, "signin", 2, spec2, status="draft")
    _sign(conn, c1, be)
    _sign(conn, c1, fe)

    block = api._contract_for(conn, "signin")
    assert block["exists"] is True
    assert block["versions"] == [{"id": "v1", "locked": True}, {"id": "v2", "locked": False}]
    # default = latest *locked* version, not merely the latest.
    assert block["default"] == "v1"
    assert block["data"]["v1"]["endpoints"] == spec1["endpoints"]
    assert {s["role"] for s in block["data"]["v1"]["signed"]} == {"backend", "frontend"}
    assert block["data"]["v2"]["signed"] == []


def test_contract_default_is_latest_when_none_locked(conn):
    seed_task(conn, "signin")
    _contract(conn, "signin", 1, {"endpoints": []}, status="draft")
    _contract(conn, "signin", 2, {"endpoints": []}, status="draft")
    block = api._contract_for(conn, "signin")
    assert block["default"] == "v2"


# --------------------------------------------------------------------------- #
# messages + strike derivation
# --------------------------------------------------------------------------- #
def test_messages_join_role_and_decode_body(conn):
    seed_task(conn, "signin")
    be = seed_agent(conn, "signin", "backend", "al-backend", "sbk_be")
    _message(conn, "signin", be, "status_update", "backend is up")

    msgs = api._messages_for(conn, "signin")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "backend"
    assert msgs[0]["type"] == "status_update"
    assert msgs[0]["body"] == "backend is up"
    assert "time" in msgs[0]
    assert isinstance(msgs[0]["ts"], (int, float))  # raw ts for thread ordering


def test_test_result_messages_get_strike_from_events(conn):
    seed_task(conn, "signin")
    fe = seed_agent(conn, "signin", "frontend", "dave-frontend", "sbk_fe")
    base = time.time()
    _message(conn, "signin", fe, "test_result", "login failed", at=base)
    _event(conn, "signin", "test", {"pass": False, "strike": 1}, at=base)
    _message(conn, "signin", fe, "test_result", "login failed again", at=base + 10)
    _event(conn, "signin", "test", {"pass": False, "strike": 2}, at=base + 10)

    msgs = api._messages_for(conn, "signin")
    strikes = [m.get("strike") for m in msgs]
    assert strikes == [1, 2]


# --------------------------------------------------------------------------- #
# event log + filtering
# --------------------------------------------------------------------------- #
def test_events_render_and_filter(conn):
    seed_task(conn, "signin")
    _event(conn, "signin", "transition", {"from": "open", "to": "contract_proposed"})
    _event(conn, "signin", "lock", {"version": 2, "signed": ["backend", "frontend"]})
    _event(conn, "signin", "deploy", {"text": "deployed to staging"})
    _event(conn, "signin", "test", {"pass": False, "strike": 1})

    all_events = api._events_for(conn, "signin", "all")
    assert len(all_events) == 4
    # shape: [time, kind, detail, created_at] — 4th is the raw ts for client-side sorting
    assert all(len(row) == 4 for row in all_events)
    assert all(isinstance(row[3], (int, float)) for row in all_events)

    kinds = [row[1] for row in all_events]
    assert kinds == ["transition", "lock", "deploy", "test"]

    # human-rendered detail strings
    details = {row[1]: row[2] for row in all_events}
    assert details["transition"] == "open → contract_proposed"
    assert details["lock"] == "Contract v2 locked (backend, frontend)"
    assert details["deploy"] == "deployed to staging"
    assert details["test"] == "Tests failed (strike 1)"

    # filter narrows to one kind
    only_lock = api._events_for(conn, "signin", "lock")
    assert [r[1] for r in only_lock] == ["lock"]

    only_transition = api._events_for(conn, "signin", "transition")
    assert [r[1] for r in only_transition] == ["transition"]


def test_events_empty_task(conn):
    seed_task(conn, "signin")
    assert api._events_for(conn, "signin", "all") == []


# --------------------------------------------------------------------------- #
# time_ago formatting
# --------------------------------------------------------------------------- #
def test_time_ago_buckets():
    now = 1_000_000.0
    assert api._time_ago(now - 2, now=now) == "just now"
    assert api._time_ago(now - 30, now=now) == "30s ago"
    assert api._time_ago(now - 120, now=now) == "2m ago"
    assert api._time_ago(now - 7200, now=now) == "2h ago"
    assert api._time_ago(now - 172800, now=now) == "2d ago"
    assert api._time_ago(None) == ""


def test_list_tasks_includes_last_and_strikes(conn):
    seed_task(conn, "signin")
    conn.execute("UPDATE tasks SET strikes = 2 WHERE id = 'signin'")
    conn.commit()
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    tasks = api._list_tasks_for(conn, viewer)
    assert tasks[0]["strikes"] == 2
    assert "last" in tasks[0]


# --------------------------------------------------------------------------- #
# SSE change-detection tokens (pure helper — the load-bearing stream logic)
# --------------------------------------------------------------------------- #
def test_change_tokens_stable_when_nothing_changes(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    first = api._change_tokens(conn, viewer)
    second = api._change_tokens(conn, viewer)
    assert first == second  # (list_token, {task: token}) is a pure function of db state


def test_change_tokens_list_moves_on_new_task(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    list_before, tasks_before = api._change_tokens(conn, viewer)
    seed_task(conn, "billing")
    list_after, tasks_after = api._change_tokens(conn, viewer)

    assert list_before != list_after            # a new visible task moves the list token
    assert set(tasks_before) == {"signin"}
    assert set(tasks_after) == {"signin", "billing"}


def test_change_tokens_task_moves_on_new_message(conn):
    seed_task(conn, "signin")
    be = seed_agent(conn, "signin", "backend", "al-backend", "sbk_be")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    list_before, tasks_before = api._change_tokens(conn, viewer)
    _message(conn, "signin", be, "status_update", "backend is up")
    list_after, tasks_after = api._change_tokens(conn, viewer)

    # New traffic moves BOTH the per-task detail token and the list token
    # (last-activity moved), so the stream fires `task` and `tasks`.
    assert tasks_before["signin"] != tasks_after["signin"]
    assert list_before != list_after


def test_change_tokens_task_moves_on_new_event(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    _, tasks_before = api._change_tokens(conn, viewer)
    _event(conn, "signin", "deploy", {"text": "deployed to staging"})
    _, tasks_after = api._change_tokens(conn, viewer)
    assert tasks_before["signin"] != tasks_after["signin"]


def test_change_tokens_task_moves_on_status_change(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    list_before, tasks_before = api._change_tokens(conn, viewer)
    conn.execute("UPDATE tasks SET state = 'contract_locked' WHERE id = 'signin'")
    conn.commit()
    list_after, tasks_after = api._change_tokens(conn, viewer)
    assert tasks_before["signin"] != tasks_after["signin"]
    assert list_before != list_after


def test_change_tokens_task_moves_on_contract_signature(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    be = seed_agent(conn, "signin", "backend", "al-backend", "sbk_be")
    cid = _contract(conn, "signin", 1, {"endpoints": []}, status="locked", locked_at=time.time())
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    _, tasks_before = api._change_tokens(conn, viewer)
    _sign(conn, cid, be)
    _, tasks_after = api._change_tokens(conn, viewer)
    assert tasks_before["signin"] != tasks_after["signin"]


def test_change_tokens_scoped_to_viewer(conn):
    # A buddy's tokens cover only their one task — same visibility as /api/tasks.
    seed_task(conn, "signin")
    seed_task(conn, "billing")
    seed_viewer(conn, "dave", "sbv_dave", task_id="signin")
    viewer = resolve_viewer_token(conn, "sbv_dave")

    _, task_tokens = api._change_tokens(conn, viewer)
    assert set(task_tokens) == {"signin"}


# --------------------------------------------------------------------------- #
# SSE stream generator (_sse_events) — drive it, read a few frames, then stop
# --------------------------------------------------------------------------- #
class _FakeRequest:
    """Minimal stand-in for a Starlette Request: only ``is_disconnected`` is used.

    ``disconnect_after`` = number of ``is_disconnected`` polls that return False
    before it starts returning True (None = never disconnect).
    """

    def __init__(self, disconnect_after=None):
        self._polls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self):
        self._polls += 1
        if self._disconnect_after is not None and self._polls > self._disconnect_after:
            return True
        return False


async def _read_frames(gen, n, timeout=5.0):
    frames = []
    for _ in range(n):
        frames.append(await asyncio.wait_for(gen.__anext__(), timeout))
    return frames


def test_stream_baseline_silent_then_ping(conn):
    """A freshly-opened stream emits no synthetic event — only keepalive pings."""
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    async def inner():
        gen = api._sse_events(_FakeRequest(), viewer, poll=0, ping_every=0)
        try:
            frames = await _read_frames(gen, 2)
        finally:
            await gen.aclose()
        return frames

    frames = asyncio.run(inner())
    # Nothing changed since connect, so every frame is a keepalive comment.
    assert all(f == ": ping\n\n" for f in frames)


def test_stream_emits_tasks_and_task_on_change(conn):
    seed_task(conn, "signin")
    be = seed_agent(conn, "signin", "backend", "al-backend", "sbk_be")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    async def inner():
        gen = api._sse_events(_FakeRequest(), viewer, poll=0, ping_every=0)
        try:
            # First frame establishes the baseline (a ping, no change yet).
            first = await _read_frames(gen, 1)
            # Now cause a real change and read the emitted frames.
            _message(conn, "signin", be, "status_update", "backend is up")
            after = await _read_frames(gen, 2)
        finally:
            await gen.aclose()
        return first, after

    first, after = asyncio.run(inner())
    assert first == [": ping\n\n"]

    # The list token moved (new traffic) AND the task's detail token moved.
    assert after[0].startswith("event: tasks\ndata: ")
    assert after[1].startswith("event: task\ndata: ")

    data_line = after[1].split("\n")[1][len("data: "):]
    payload = json.loads(data_line)
    assert payload["id"] == "signin"
    assert "token" in payload

    tasks_payload = json.loads(after[0].split("\n")[1][len("data: "):])
    assert set(tasks_payload) == {"token"}


def test_stream_stops_on_disconnect(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    async def inner():
        # is_disconnected() returns True on the very first poll → loop breaks at once.
        gen = api._sse_events(_FakeRequest(disconnect_after=0), viewer, poll=0)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 5.0)

    asyncio.run(inner())


def test_stream_stops_on_idle_backstop(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    async def inner():
        # idle_timeout=0 → no change means the idle backstop trips immediately.
        gen = api._sse_events(_FakeRequest(), viewer, poll=0, idle_timeout=0)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 5.0)

    asyncio.run(inner())
