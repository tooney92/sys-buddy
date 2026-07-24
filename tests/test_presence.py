"""Specs for the always-listening protocol and the presence signal it feeds.

Two halves, and the first one is the important one:

* **the listener invariant** — delivery is stamped against the SEAT, so a background
  listener sharing the agent's token consumes the new-flag on its behalf. That is what
  makes "the listener never acks, the main agent re-reads with check_messages" correct,
  and what would break silently if ``fetch_new``/``fetch_unacked`` ever converged.
* **presence** — ``agents.listening_until`` is an EXPIRY, not a boolean: it must go out
  the moment a wait ends AND lapse on its own if the clearing ``finally`` never runs
  (broker killed with agents parked).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from sys_buddy import api, db, service, tools
from tests.conftest import seed_agent, seed_task


def _mk(conn, task="signin", roles=("backend", "frontend")):
    seed_task(conn, task, roles=roles)
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in roles
    }


def _row(conn, ident):
    return conn.execute(
        "SELECT listening_until, listening_since FROM agents WHERE id = ?", (ident.agent_id,)
    ).fetchone()


# --- the listener invariant -------------------------------------------------
def test_listener_wake_consumes_the_new_flag_but_check_still_recovers(conn):
    """ONE IDENTITY, two readers. The background listener parks in wait_for_message
    (fetch_new) using the SAME seat as the main agent, so its wake stamps delivered
    for the seat: a second fetch_new — the main agent calling wait_for_message —
    comes back EMPTY, while check_messages (fetch_unacked) still returns the message
    in full. This is why the listener must never ack and the main agent must re-read
    with check_messages."""
    ag = _mk(conn)
    service.post_message(conn, ag["backend"], "question", "shape of the error body?")

    listener_wake = service.fetch_new(conn, ag["frontend"])   # the parked subagent
    main_agent_wait = service.fetch_new(conn, ag["frontend"])  # main agent, same seat
    main_agent_read = service.fetch_unacked(conn, ag["frontend"])  # check_messages

    assert len(listener_wake) == 1
    assert main_agent_wait == []                       # the seat's new-flag is spent
    assert len(main_agent_read) == 1                   # but the mail is still there
    assert main_agent_read[0]["id"] == listener_wake[0]["id"]
    assert 'trust="external"' in main_agent_read[0]["content"]  # envelope intact

    # ...and only the main agent's ack ends the redelivery.
    service.ack(conn, ag["frontend"], [m["id"] for m in main_agent_read])
    assert service.fetch_unacked(conn, ag["frontend"]) == []


# --- presence: mark / clear / expiry ---------------------------------------
def test_mark_listening_stamps_an_expiry_bounded_by_the_cap(conn):
    ag = _mk(conn)
    before = time.time()
    until = service.mark_listening(conn, ag["frontend"], timeout_seconds=500, cap=tools.WAIT_CAP)

    row = _row(conn, ag["frontend"])
    assert row["listening_until"] == until
    assert before + 499 <= until <= before + 501
    assert service.is_listening(row["listening_until"]) is True

    # A timeout beyond the cap is clamped to the cap — the expiry can never outlive
    # the longest possible wait.
    capped = service.mark_listening(conn, ag["frontend"], timeout_seconds=10_000, cap=tools.WAIT_CAP)
    assert capped <= time.time() + tools.WAIT_CAP + 1


def test_clear_listening_ends_the_window_now_and_is_not_null(conn):
    ag = _mk(conn)
    service.mark_listening(conn, ag["frontend"], 500, tools.WAIT_CAP)
    service.clear_listening(conn, ag["frontend"])

    row = _row(conn, ag["frontend"])
    assert row["listening_until"] is not None          # "expired as of now", not NULL
    assert service.is_listening(row["listening_until"]) is False  # dot goes out at once


def test_presence_expires_on_its_own_when_clear_never_runs(conn):
    """Simulated crash: the broker dies with a seat parked, so the clearing finally
    never runs. Because the column stores an expiry (not a boolean) the stale row
    stops claiming 'listening' by itself — no cleanup job, correct across a restart."""
    ag = _mk(conn)
    service.mark_listening(conn, ag["frontend"], 500, tools.WAIT_CAP)
    assert service.is_listening(_row(conn, ag["frontend"])["listening_until"]) is True

    # Wind the stamp back past its expiry — exactly the row a killed broker leaves.
    conn.execute(
        "UPDATE agents SET listening_until = ? WHERE id = ?",
        (time.time() - 1, ag["frontend"].agent_id),
    )
    conn.commit()

    assert service.is_listening(_row(conn, ag["frontend"])["listening_until"]) is False
    agent = next(a for a in api._agents_for(conn, "signin") if a["role"] == "frontend")
    assert agent["listening"] is False


def test_listening_streak_survives_the_respawn_gap(conn):
    """A listener respawns every ~WAIT_CAP seconds, so there is always a small gap.
    A gap under LISTEN_STREAK_GAP keeps the streak — otherwise 'listening — 42m'
    would reset to zero on every message cycle."""
    ag = _mk(conn)
    now = time.time()
    conn.execute(
        "UPDATE agents SET listening_until = ?, listening_since = ? WHERE id = ?",
        (now - 5, now - 2400, ag["frontend"].agent_id),  # ended 5s ago, running 40m
    )
    conn.commit()

    service.mark_listening(conn, ag["frontend"], 500, tools.WAIT_CAP)

    row = _row(conn, ag["frontend"])
    assert abs(row["listening_since"] - (now - 2400)) < 1  # same streak, ~40m old
    assert service.is_listening(row["listening_until"]) is True


def test_listening_streak_resets_after_a_long_gap(conn):
    ag = _mk(conn)
    now = time.time()
    conn.execute(
        "UPDATE agents SET listening_until = ?, listening_since = ? WHERE id = ?",
        (now - (service.LISTEN_STREAK_GAP + 60), now - 5000, ag["frontend"].agent_id),
    )
    conn.commit()

    service.mark_listening(conn, ag["frontend"], 500, tools.WAIT_CAP)

    row = _row(conn, ag["frontend"])
    assert row["listening_since"] >= now - 1  # a NEW streak, not the stale 83m one


def test_first_ever_mark_starts_a_streak(conn):
    ag = _mk(conn)
    assert _row(conn, ag["frontend"])["listening_since"] is None
    service.mark_listening(conn, ag["frontend"], 500, tools.WAIT_CAP)
    assert _row(conn, ag["frontend"])["listening_since"] is not None


# --- presence through the real wait path ------------------------------------
def test_wait_marks_presence_while_parked_and_clears_on_exit(conn, monkeypatch):
    """_op_wait stamps once it is really parked and clears in its finally. The stamp
    is observed from inside the poll loop, since by the time the call returns the
    finally has (correctly) already put the dot out."""
    ag = _mk(conn)
    seen = {}
    real_fetch_new = service.fetch_new

    def spy(c, ident, *a, **kw):
        seen["row"] = dict(_row(conn, ident))
        return real_fetch_new(c, ident, *a, **kw)

    monkeypatch.setattr(service, "fetch_new", spy)
    service.post_message(conn, ag["backend"], "question", "wake up")

    msgs = asyncio.run(tools._op_wait(ag["frontend"], timeout_seconds=5))

    assert len(msgs) == 1
    assert service.is_listening(seen["row"]["listening_until"]) is True  # parked → dot on
    assert seen["row"]["listening_since"] is not None
    after = _row(conn, ag["frontend"])
    assert service.is_listening(after["listening_until"]) is False       # returned → off


def test_wait_at_the_concurrency_cap_never_claims_to_be_listening(conn):
    """The cap check comes first: a backed-off call never opened a connection and
    never listened, so it must not stamp presence. (_active_waits stays per-process
    connection accounting — deliberately NOT the db signal.)"""
    ag = _mk(conn)
    tools._active_waits[ag["frontend"].agent_id] = tools.MAX_CONCURRENT_WAITS
    try:
        assert asyncio.run(tools._op_wait(ag["frontend"], timeout_seconds=5)) == []
    finally:
        tools._active_waits.pop(ag["frontend"].agent_id, None)

    row = _row(conn, ag["frontend"])
    assert row["listening_until"] is None and row["listening_since"] is None


# --- api payload ------------------------------------------------------------
def test_api_exposes_per_role_listening_and_since(conn):
    ag = _mk(conn)
    service.mark_listening(conn, ag["backend"], 500, tools.WAIT_CAP)

    agents = {a["role"]: a for a in api._agents_for(conn, "signin")}

    assert agents["backend"]["listening"] is True
    assert agents["backend"]["listening_since"] is not None
    assert agents["frontend"]["listening"] is False       # never parked
    assert agents["frontend"]["listening_since"] is None
    # Alongside — not instead of — the existing per-role data.
    assert agents["backend"]["ready"] is False
    assert agents["backend"]["readiness_status"] == "pending"

    detail = api._task_detail(conn, "signin")
    assert detail["agents"][0]["listening"] is True


def test_listening_moves_the_sse_task_token(conn):
    """The dashboard's live dot only updates if the change-detection token moves."""
    from sys_buddy.identity import ViewerIdentity

    ag = _mk(conn)
    viewer = ViewerIdentity(viewer_id=1, task_id=None, label="host")
    _, before = api._change_tokens(conn, viewer)
    service.mark_listening(conn, ag["backend"], 500, tools.WAIT_CAP)
    _, after = api._change_tokens(conn, viewer)

    assert before["signin"] != after["signin"]


# --- migration --------------------------------------------------------------
def test_init_db_adds_presence_columns_to_an_existing_db(tmp_path):
    """Idempotent ALTERs: an existing db (rows and all) gains the two columns, and a
    second init_db is a no-op rather than a duplicate-column error."""
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE agents (id INTEGER PRIMARY KEY, task_id TEXT, name TEXT, role TEXT, "
        "token_hash TEXT, pubkey TEXT, created_at REAL, revoked_at REAL)"  # pre-presence
    )
    c.execute(
        "INSERT INTO agents (task_id, name, role, created_at) VALUES ('signin','b','backend',1.0)"
    )
    c.commit()
    c.close()

    db.init_db(p)
    db.init_db(p)  # idempotent — must not raise "duplicate column name"

    c = sqlite3.connect(p)
    cols = {r[1] for r in c.execute("PRAGMA table_info(agents)").fetchall()}
    row = c.execute("SELECT name, listening_until, listening_since FROM agents").fetchone()
    c.close()
    assert {"listening_until", "listening_since"} <= cols
    assert row == ("b", None, None)  # pre-existing row preserved, presence unset
