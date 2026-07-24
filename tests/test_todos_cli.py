"""Specs for the HOST's todo surface: ``sys-buddy todo list`` / ``todo drop``.

The drop is the one place a human writes to a todo, and it exists because no peer may
ever remove a peer: a mutual ``drop_todo`` needs every named party's consent — including
the party whose human went offline and is the whole reason you want it gone — so that
path deadlocks on exactly the person who is missing. Hence a HUMAN escape hatch, in the
CLI (a host running ``sys-buddy serve`` headless has no GUI to click) and never on the
dashboard (D11).

The load-bearing behaviour under test: the drop leaves an EXPLANATION in the thread,
attributed to the broker rather than to a real agent, so the absent party's agent finds
a decision instead of vanished work.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from sys_buddy import admin, api, cli, service, todos
from sys_buddy.config import Config, set_config
from sys_buddy.db import connect, init_db
from sys_buddy.identity import Identity


# --------------------------------------------------------------------------- #
# seed: a real task with real todos, built through the real flow
# --------------------------------------------------------------------------- #
def _seed(tmp_path, *, roles=("backend", "frontend", "mobile")) -> tuple[str, dict]:
    """A db path plus ``{role: Identity}``, with the CLI's config already pointed at it."""
    dbfile = tmp_path / "t.db"
    set_config(Config(mode="local", db_path=dbfile))
    init_db(dbfile)
    admin.create_task("signin", title="Sign-in", roles=list(roles))

    conn = connect(dbfile)
    seats = {}
    try:
        for role in roles:
            cur = conn.execute(
                "INSERT INTO agents (task_id, name, role, token_hash, created_at) "
                "VALUES (?,?,?,NULL,?)",
                ("signin", f"{role}-agent", role, time.time()),
            )
            seats[role] = Identity(
                agent_id=cur.lastrowid, task_id="signin", name=f"{role}-agent", role=role
            )
        conn.commit()
    finally:
        conn.close()
    return str(dbfile), seats


def _propose(db, seats, title="Payments API", parties=("backend", "mobile")):
    conn = connect(db)
    try:
        return todos.propose_todo(
            conn, seats[parties[0]], title, f"scope of {title}", list(parties)
        )
    finally:
        conn.close()


def _thread(db):
    """The task thread as ``[(role, type, body), ...]`` — the dashboard's view of it."""
    conn = connect(db)
    try:
        return [(m["role"], m["type"], m["body"]) for m in api._messages_for(conn, "signin")]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# todo list — the host has to FIND the id before they can act
# --------------------------------------------------------------------------- #
def test_todo_list_shows_status_parties_and_the_blocking_party(tmp_path, capsys):
    db, seats = _seed(tmp_path)
    _propose(db, seats)

    assert cli.cmd_todo_list(SimpleNamespace(db=db, task="signin")) == 0
    out = capsys.readouterr().out
    assert "Payments API" in out
    assert "pending" in out
    assert "backend,mobile" in out
    assert "awaiting: mobile" in out
    assert "0/1 verified" in out
    assert "1 awaiting acceptance" in out
    # …and it tells the human exactly what to type, the same division as D11.
    assert 'sys-buddy todo drop signin <id> --reason "..."' in out


def test_todo_list_on_a_task_with_no_todos(tmp_path, capsys):
    db, _seats = _seed(tmp_path)
    assert cli.cmd_todo_list(SimpleNamespace(db=db, task="signin")) == 0
    assert "No todos on task 'signin'" in capsys.readouterr().out


def test_todo_list_unknown_task_errors_clearly(tmp_path, capsys):
    db, _seats = _seed(tmp_path)
    assert cli.main(["--db", db, "todo", "list", "ghost"]) == 1
    assert "unknown task 'ghost'" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# todo drop — the host's unilateral escape hatch
# --------------------------------------------------------------------------- #
def test_host_drop_posts_who_and_why_as_the_broker(tmp_path, capsys):
    db, seats = _seed(tmp_path)
    todo = _propose(db, seats)

    assert cli.main(
        ["--db", db, "todo", "drop", "signin", str(todo["id"]),
         "--reason", "mobile's human is offline this week"]
    ) == 0
    out = capsys.readouterr().out
    assert f"Dropped todo #{todo['id']} 'Payments API'" in out
    assert "mobile's human is offline this week" in out

    # The explanation lands in the thread — and in the BROKER's voice, never in a
    # peer's: no agent authored this, and the framing must not claim one did.
    dropped = [m for m in _thread(db) if m[1] == "todo_dropped"]
    assert len(dropped) == 1
    role, _type, body = dropped[0]
    assert role == service.BROKER_ROLE
    assert "DROPPED by the host" in body
    assert "mobile's human is offline this week" in body
    assert f"#{todo['id']}" in body
    # The synthetic broker seat is revoked at birth, so it never shows up as a
    # participant on the dashboard or counts toward the pre-flight gate.
    conn = connect(db)
    try:
        seats_shown = {a["role"] for a in api._agents_for(conn, "signin")}
    finally:
        conn.close()
    assert service.BROKER_ROLE not in seats_shown


def test_host_drop_marks_the_todo_dropped_by_host(tmp_path, capsys):
    db, seats = _seed(tmp_path)
    todo = _propose(db, seats)
    cli.cmd_todo_drop(
        SimpleNamespace(db=db, task="signin", todo=str(todo["id"]), reason="descoped")
    )
    capsys.readouterr()

    conn = connect(db)
    try:
        (t,) = todos.get_todos(conn, "signin")
    finally:
        conn.close()
    assert t["status"] == todos.DROPPED
    assert t["dropped_by"] == todos.HOST
    assert t["drop_reason"] == "descoped"
    # No peer consent was needed OR recorded — this was not a mutual drop.
    assert t["drop_consents"] == []


def test_host_drop_refused_on_a_verified_todo(tmp_path, capsys):
    """Abandoning finished work would make the task's "concludes when the last todo
    verifies" rollup lie — so even the host cannot drop a verified todo."""
    db, seats = _seed(tmp_path)
    todo = _propose(db, seats)
    conn = connect(db)
    try:
        conn.execute(
            "UPDATE todos SET state = 'verified', verified_at = ? WHERE id = ?",
            (time.time(), todo["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    assert cli.main(
        ["--db", db, "todo", "drop", "signin", str(todo["id"]), "--reason", "changed my mind"]
    ) == 1
    assert "verified and cannot be dropped" in capsys.readouterr().err
    assert [m for m in _thread(db) if m[1] == "todo_dropped"] == []


def test_host_drop_refused_twice(tmp_path, capsys):
    db, seats = _seed(tmp_path)
    todo = _propose(db, seats)
    args = ["--db", db, "todo", "drop", "signin", str(todo["id"]), "--reason", "descoped"]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert cli.main(args) == 1
    assert "already dropped" in capsys.readouterr().err


def test_host_drop_unknown_task_and_unknown_todo_error_clearly(tmp_path, capsys):
    db, seats = _seed(tmp_path)
    _propose(db, seats)

    assert cli.main(["--db", db, "todo", "drop", "ghost", "1", "--reason", "x"]) == 1
    assert "unknown task 'ghost'" in capsys.readouterr().err

    assert cli.main(["--db", db, "todo", "drop", "signin", "99", "--reason", "x"]) == 1
    assert "no todo 99 on task 'signin'" in capsys.readouterr().err


def test_host_drop_requires_a_reason(tmp_path):
    """The reason is the ONLY thing the absent party's agent will see, so argparse
    refuses the command outright rather than letting a silent drop through."""
    db, seats = _seed(tmp_path)
    todo = _propose(db, seats)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", db, "todo", "drop", "signin", str(todo["id"])])
    assert exc.value.code == 2


def test_host_drop_updates_the_task_rollup(tmp_path, capsys):
    db, seats = _seed(tmp_path)
    keep = _propose(db, seats, title="Refunds", parties=("backend", "frontend"))
    gone = _propose(db, seats, title="Payments API", parties=("backend", "mobile"))

    cli.cmd_todo_drop(
        SimpleNamespace(db=db, task="signin", todo=str(gone["id"]), reason="offline")
    )
    out = capsys.readouterr().out
    assert "0/1 verified" in out  # the dropped todo stopped counting

    conn = connect(db)
    try:
        roll = todos.rollup(conn, "signin")
        live = [t["id"] for t in todos.live_todos(conn, "signin")]
    finally:
        conn.close()
    assert roll["total"] == 1
    assert roll["dropped"] == 1
    assert live == [keep["id"]]


def test_todo_drop_is_audit_logged(tmp_path, caplog):
    """A host writing to a live collaboration is exactly what the operator's audit trail
    is for — same as revoke_agent/task_closed. No reason text: the audit log takes only
    non-sensitive identifiers."""
    db, seats = _seed(tmp_path)
    todo = _propose(db, seats)
    with caplog.at_level("INFO", logger="sys_buddy.audit"):
        admin.host_drop_todo("signin", todo["id"], "offline")
    lines = [r.getMessage() for r in caplog.records]
    assert any(f"todo_dropped task=signin todo={todo['id']} by=host" == m for m in lines)


def test_todo_surface_is_list_and_drop_only():
    """The host's todo surface is deliberately narrow: LIST (read) and DROP (the one
    escape hatch). No peer-removal command ever gets written — see todos.py — so an
    invented one must not silently resolve to something."""
    parser = cli.build_parser()
    assert parser.parse_args(["todo", "list", "signin"]).func is cli.cmd_todo_list
    assert parser.parse_args(
        ["todo", "drop", "signin", "1", "--reason", "x"]
    ).func is cli.cmd_todo_drop
    for invented in (["todo", "remove-party", "signin", "1"], ["todo", "accept", "signin", "1"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(invented)
        assert exc.value.code == 2
