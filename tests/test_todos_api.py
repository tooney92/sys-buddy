"""Specs for todos on the read-only dashboard API.

Two invariants carry this file:

* **Backwards compatibility.** A task with NO todos must serialise EXACTLY as it did
  before todos existed — the deployed ``ui.html`` reads that payload, and it is served
  from disk so it can be newer OR older than the running ``api.py`` across a restart.
  The todo keys are therefore absent, not empty, on a pre-todo task.
* **Read-only (D11).** The viewer token is read-scoped, so a leaked ``?v=`` link must
  only ever be able to LOOK. There is no write route on this surface — not even for the
  host's drop, which lives in the CLI and the desktop app.

Like ``tests/test_api.py`` these drive the ``_``-prefixed query helpers directly (they
take an open connection), so no HTTP server is needed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sys_buddy import api, service, todos
from sys_buddy.identity import Identity, resolve_viewer_token
from tests.conftest import seed_agent, seed_task, seed_viewer


# --------------------------------------------------------------------------- #
# seed helpers
# --------------------------------------------------------------------------- #
def _identity(conn, task_id, role, name):
    """A live agent + the Identity the todo writes expect."""
    agent_id = seed_agent(conn, task_id, role, name, f"sbk_{role}")
    return Identity(agent_id=agent_id, task_id=task_id, name=name, role=role)


def _task_with_seats(conn, roles=("backend", "frontend", "mobile")):
    seed_task(conn, "signin", roles=roles)
    return {r: _identity(conn, "signin", r, f"{r}-agent") for r in roles}


def _todo_contract(conn, task_id, todo_id, version, spec, status="draft"):
    cur = conn.execute(
        "INSERT INTO contracts (task_id, todo_id, version, spec_json, status, locked_at, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (
            task_id, todo_id, version, json.dumps(spec), status,
            time.time() if status == "locked" else None, time.time(),
        ),
    )
    conn.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# the regression that protects the live dashboard
# --------------------------------------------------------------------------- #
def test_task_with_no_todos_serialises_exactly_as_before(conn):
    """The pre-todo payload is byte-identical: the todo keys are ABSENT, not empty.

    An older ``ui.html`` reading this must see nothing new at all, and a NEWER one must
    be able to tell "this task has no todos" from "this broker predates todos" — it
    can't, and doesn't need to: both mean "render today's seven-node stepper".
    """
    seed_task(conn, "signin", roles=("backend", "frontend"))
    detail = api._task_detail(conn, "signin")
    assert set(detail) == {
        "id", "title", "state", "mode", "roles", "strikes", "times", "contract",
        "messages", "events", "agents", "readiness_preview",
    }
    assert "todos" not in detail
    assert "todo_rollup" not in detail
    assert "has_todos" not in detail


def test_task_list_row_has_no_todo_key_without_todos(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    (row,) = api._list_tasks_for(conn, viewer)
    assert set(row) == {"id", "title", "state", "mode", "roles", "last", "strikes"}


def test_task_level_contract_ignores_todo_contracts(conn):
    """The task card keeps the task's OWN chain.

    Folding six todos' contracts into it would show a jumble of unrelated shapes under
    one "API contract" heading — and a pre-todo task is unaffected either way, since all
    of its contracts have ``todo_id IS NULL``.
    """
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    _todo_contract(conn, "signin", todo["id"], 1, {"endpoints": [{"path": "/pay"}]})

    assert api._contract_for(conn, "signin")["exists"] is False
    scoped = api._contract_for(conn, "signin", todo_id=todo["id"])
    assert scoped["exists"] is True
    assert scoped["versions"] == [{"id": "v1", "locked": False}]


# --------------------------------------------------------------------------- #
# the todo panel
# --------------------------------------------------------------------------- #
def test_api_exposes_parties_statuses_and_acceptances(conn):
    seats = _task_with_seats(conn)
    todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay; refunds out of scope",
        ["backend", "mobile"],
    )
    detail = api._task_detail(conn, "signin")
    (t,) = detail["todos"]

    # SEATS ≠ PARTICIPANTS: frontend is seated on the task and is not a party here.
    assert t["parties"] == ["backend", "mobile"]
    assert t["title"] == "Payments API"
    assert t["scope"].startswith("POST /pay")
    assert t["proposed_by"] == "backend"
    # Proposing IS the creator's own consent; mobile is the one still blocking.
    assert t["status"] == "pending"
    assert t["accepted_by"] == ["backend"]
    assert t["declined_by"] == []
    assert t["awaiting"] == ["mobile"]
    assert t["version"] == 1
    assert t["stuck"] is False
    assert t["dropped_by"] is None
    assert t["drop_reason"] is None
    assert t["time"]  # HH:MM, same mono format as the rest of the dashboard

    todos.accept_todo(conn, seats["mobile"], t["id"])
    (t,) = api._task_detail(conn, "signin")["todos"]
    assert t["status"] == "accepted"
    assert t["accepted_by"] == ["backend", "mobile"]
    assert t["awaiting"] == []


def test_api_exposes_declines_with_reasons(conn):
    seats = _task_with_seats(conn)
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "mobile"]
    )
    todos.decline_todo(conn, seats["mobile"], todo["id"], "needs idempotency keys")

    (t,) = api._task_detail(conn, "signin")["todos"]
    assert t["status"] == "pending"
    assert t["declined_by"] == ["mobile"]
    assert t["decline_reasons"]["mobile"] == "needs idempotency keys"


def test_rollup_counts_on_task_view_and_list_row(conn):
    """"2 of 6 verified" + "⚠ 1 awaiting acceptance", on BOTH surfaces.

    The list row carries it so a host can triage without opening anything.
    """
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    made = []
    for n in range(4):
        made.append(
            todos.propose_todo(
                conn, seats["backend"], f"Deliverable {n}", f"scope {n}",
                ["backend", "frontend"],
            )
        )
    # #0, #1 verified · #2 accepted · #3 left pending (frontend hasn't answered).
    for t in made[:3]:
        todos.accept_todo(conn, seats["frontend"], t["id"])
    for t in made[:2]:
        conn.execute(
            "UPDATE todos SET state = 'verified', verified_at = ? WHERE id = ?",
            (time.time(), t["id"]),
        )
    conn.commit()

    roll = api._task_detail(conn, "signin")["todo_rollup"]
    assert (roll["verified"], roll["total"]) == (2, 4)
    assert roll["pending"] == 1
    assert roll["stuck"] == 0
    assert roll["complete"] is False

    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    (row,) = api._list_tasks_for(conn, viewer)
    assert row["todo_rollup"] == roll


def test_rollup_surfaces_the_stuck_flag(conn):
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    conn.execute(
        "UPDATE todos SET stuck_at = ?, stuck_reason = ? WHERE id = ?",
        (time.time(), "staging is down", todo["id"]),
    )
    conn.commit()

    detail = api._task_detail(conn, "signin")
    assert detail["todo_rollup"]["stuck"] == 1
    (t,) = detail["todos"]
    assert t["stuck"] is True
    assert t["stuck_reason"] == "staging is down"


def test_pending_todos_sort_first(conn):
    """A pending todo is the only thing on that screen BLOCKING a human — a request,
    not progress — so it sorts to the top however late it was proposed."""
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    first = todos.propose_todo(
        conn, seats["backend"], "Accepted one", "scope", ["backend", "frontend"]
    )
    todos.accept_todo(conn, seats["frontend"], first["id"])
    second = todos.propose_todo(
        conn, seats["backend"], "Verified one", "scope", ["backend", "frontend"]
    )
    todos.accept_todo(conn, seats["frontend"], second["id"])
    conn.execute("UPDATE todos SET state = 'verified' WHERE id = ?", (second["id"],))
    conn.commit()
    # Proposed LAST and still awaiting frontend.
    third = todos.propose_todo(
        conn, seats["backend"], "Pending one", "scope", ["backend", "frontend"]
    )
    dropped = todos.propose_todo(
        conn, seats["backend"], "Dropped one", "scope", ["backend", "frontend"]
    )
    todos.host_drop_todo(conn, "signin", dropped["id"], "not needed after all")

    ordered = api._task_detail(conn, "signin")["todos"]
    assert [t["status"] for t in ordered] == ["pending", "accepted", "verified", "dropped"]
    assert ordered[0]["id"] == third["id"]
    # …and a dropped one sinks: it no longer counts toward the task.
    assert ordered[-1]["id"] == dropped["id"]


def test_todo_carries_its_own_contract_block(conn):
    """The right-hand panel shows today's contract card scoped to the selected todo —
    same shape, same renderer, one level down."""
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    spec = {"endpoints": [{"method": "POST", "path": "/pay"}], "staging_url": "https://s.example"}
    cid = _todo_contract(conn, "signin", todo["id"], 1, spec, status="locked")
    conn.execute(
        "INSERT INTO contract_signatures (contract_id, agent_id, signed_at) VALUES (?,?,?)",
        (cid, seats["backend"].agent_id, time.time()),
    )
    conn.commit()

    (t,) = api._task_detail(conn, "signin")["todos"]
    assert t["status"] == "contracted"
    assert t["contract_versions"] == [1]
    assert t["locked_versions"] == [1]
    assert t["contract"]["exists"] is True
    assert t["contract"]["default"] == "v1"
    assert t["contract"]["data"]["v1"]["endpoints"] == spec["endpoints"]
    assert [s["role"] for s in t["contract"]["data"]["v1"]["signed"]] == ["backend"]


def test_all_todos_dropped_keeps_them_visible_but_drops_the_rollup(conn):
    """A task whose todos were ALL dropped runs its own state machine again
    (``todos.has_todos`` is False), but the rows stay visible so the human reads a
    decision rather than finding a hole."""
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    todos.host_drop_todo(conn, "signin", todo["id"], "descoped")

    detail = api._task_detail(conn, "signin")
    assert detail["has_todos"] is False
    assert detail["todo_rollup"] is None
    assert [t["status"] for t in detail["todos"]] == ["dropped"]


# --------------------------------------------------------------------------- #
# the ⟨api123⟩ chip, and the todo event log
# --------------------------------------------------------------------------- #
def test_messages_carry_the_todo_they_belong_to(conn):
    """ONE thread per task — six would fragment a conversation that is genuinely one —
    so a message is ATTRIBUTED to a deliverable with a chip, never filed under it."""
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    a = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    b = todos.propose_todo(
        conn, seats["backend"], "Refunds", "POST /refund", ["backend", "frontend"]
    )
    todos.accept_todo(conn, seats["frontend"], b["id"])
    service.post_message(conn, seats["frontend"], "question", "unrelated chatter")

    msgs = api._messages_for(conn, "signin")
    by_type = {(m["type"], m.get("todo")) for m in msgs}
    assert ("todo_proposal", a["id"]) in by_type
    assert ("todo_proposal", b["id"]) in by_type
    assert ("todo_accept", b["id"]) in by_type
    # A message that belongs to no todo carries no chip at all.
    (chatter,) = [m for m in msgs if m["type"] == "question"]
    assert "todo" not in chatter


def test_todo_reference_is_validated_against_the_task(conn):
    """A body naming a todo that isn't on this task gets NO chip — better a missing
    chip than one pointing at nothing (or at another task's deliverable)."""
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    conn.execute(
        "INSERT INTO messages (task_id, from_agent_id, type, body_json, state_at_send, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (
            "signin", seats["backend"].agent_id, "todo_proposal",
            '"Proposed todo #999: nothing"', "open", time.time(),
        ),
    )
    conn.commit()
    (m,) = api._messages_for(conn, "signin")
    assert "todo" not in m


def test_chip_comes_from_the_column_not_the_body_text(conn):
    """The authoritative source is ``messages.todo_id``, set at post time — so a
    message that belongs to a deliverable is chipped even when its body says nothing
    like "todo #N". This is what stops the chip depending on prose staying in sync."""
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    # A body with NO "todo #N" reference anywhere in it.
    service.post_message(
        conn, seats["backend"], "status_update",
        "shipping the endpoint now, no reference in this text",
        todo_id=todo["id"],
    )
    (m,) = [m for m in api._messages_for(conn, "signin") if m["type"] == "status_update"]
    assert m["todo"] == todo["id"]


def test_chip_falls_back_to_the_body_for_pre_column_rows(conn):
    """A row written before the ``messages.todo_id`` column existed carries NULL, so
    the chip is recovered by scraping "todo #N" — but only for a REAL todo on the task."""
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    # Simulate a legacy row: todo_id column left NULL, deliverable named only in prose.
    conn.execute(
        "INSERT INTO messages (task_id, from_agent_id, type, body_json, state_at_send, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (
            "signin", seats["backend"].agent_id, "status_update",
            json.dumps(f"legacy note about todo #{todo['id']}"), "open", time.time(),
        ),
    )
    conn.commit()
    (m,) = [m for m in api._messages_for(conn, "signin") if m["type"] == "status_update"]
    assert m["todo"] == todo["id"]


def test_todo_events_render_and_filter(conn):
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    todos.host_drop_todo(conn, "signin", todo["id"], "mobile's human went offline")

    rendered = api._events_for(conn, "signin", "todo")
    assert [e[1] for e in rendered] == ["todo", "todo"]
    assert "proposed by backend" in rendered[0][2]
    assert "dropped by host" in rendered[1][2]
    assert "mobile's human went offline" in rendered[1][2]


# --------------------------------------------------------------------------- #
# live updates
# --------------------------------------------------------------------------- #
def test_change_token_unchanged_for_a_task_with_no_todos(conn):
    """Adding todos to the API must not perturb a pre-todo task's SSE token."""
    seed_task(conn, "signin")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    first = api._change_tokens(conn, viewer)
    assert api._change_tokens(conn, viewer) == first
    assert "todos" not in first[1]["signin"]


@pytest.mark.parametrize("act", ["accept", "drop", "stuck"])
def test_change_tokens_move_on_todo_activity(conn, act):
    seats = _task_with_seats(conn, roles=("backend", "frontend"))
    todo = todos.propose_todo(
        conn, seats["backend"], "Payments API", "POST /pay", ["backend", "frontend"]
    )
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    list_before, tasks_before = api._change_tokens(conn, viewer)

    if act == "accept":
        todos.accept_todo(conn, seats["frontend"], todo["id"])
    elif act == "drop":
        todos.host_drop_todo(conn, "signin", todo["id"], "descoped")
    else:
        conn.execute(
            "UPDATE todos SET stuck_at = ?, stuck_reason = 'staging down' WHERE id = ?",
            (time.time(), todo["id"]),
        )
        conn.commit()

    list_after, tasks_after = api._change_tokens(conn, viewer)
    assert tasks_after["signin"] != tasks_before["signin"]
    # The list row shows the rollup too, so it must move with it.
    assert list_after != list_before


# --------------------------------------------------------------------------- #
# D11 — read-only, and no write path at all
# --------------------------------------------------------------------------- #
def test_no_mutating_route_on_the_dashboard_surface(tmp_path):
    """The whole ``/api`` + ``/ui`` surface is GET-only.

    The viewer token is read-scoped (D7): a leaked ``?v=`` link must only ever be able
    to LOOK. A single write route — including one for the host's todo drop — would be
    the first crack in that and would need its own auth story, so the host acts through
    the CLI or the desktop app instead.
    """
    from sys_buddy.config import Config
    from sys_buddy.server import build_server

    mcp = build_server(Config(mode="remote", db_path=tmp_path / "s.db"))
    dashboard = [
        r for r in getattr(mcp, "_additional_http_routes", [])
        if str(getattr(r, "path", "")).startswith(("/api", "/ui"))
    ]
    assert dashboard, "expected the dashboard routes to be registered"
    for route in dashboard:
        assert set(route.methods) <= {"GET", "HEAD"}, f"{route.path} accepts writes"


def test_api_module_registers_no_write_verbs():
    """Belt and braces on the source: no route on this module declares a write verb."""
    src = Path(api.__file__).read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f'"{verb}"' not in src
