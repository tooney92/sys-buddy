"""Joining: one Sarah per TASK, the cast shown before you pick, and the stalled seat.

Three defects, one flow:

1. A display name scoped to a ``(task, role)`` would let Sarah/frontend and
   Sarah/backend coexist, and every ``@sarah`` from then on would be ambiguous — a
   permanent hazard on the most common action, bought for a one-time annoyance at join.
   So: **unique per task**, case-insensitively.
2. A joiner picked that name blind. The join surface now shows the task, the SEAT the
   invite fills, and who is already here — but only once the invite has VALIDATED,
   because a surface that lists the cast for any code is a team directory for anyone who
   guesses.
3. "waiting on Priya" and "waiting on a seat nobody ever accepted" looked identical, and
   they need different actions: nudge a colleague, or chase an invite.
"""

from __future__ import annotations

import io
import sqlite3
import time

import pytest

from sys_buddy import admin, cli, db, pairing, seats, service, state, todos
from sys_buddy.identity import resolve_agent_token, sha256_hex

from conftest import seed_agent, seed_task


def _pair(conn, task_id, seat, name):
    code, _ = admin.mint_invite(task_id, seat)
    return pairing.redeem_invite(conn, code, name)


# --------------------------------------------------------------------------- #
# 1. a name is unique per TASK, not per (task, role)
# --------------------------------------------------------------------------- #
def test_a_second_sarah_is_refused_even_in_a_different_role(conn):
    """The whole point: `@sarah` has to be safe to type. Scoped to the role you could
    have Sarah/frontend and Sarah/backend, and every `@sarah` after that is ambiguous."""
    admin.create_task("pay", title="Payments platform", roles=["backend", "frontend"])
    _pair(conn, "pay", "backend", "Sarah")

    with pytest.raises(ValueError) as e:
        _pair(conn, "pay", "frontend", "Sarah")
    assert '"Sarah" is already on this task' in str(e.value)
    assert 'pick another name (e.g. "Sarah K")' in str(e.value)


def test_the_clash_is_case_insensitive_and_whitespace_trimmed(conn):
    admin.create_task("fold", title="Fold", roles=["backend", "frontend", "qa"])
    _pair(conn, "fold", "backend", "Sarah")

    for attempt in ("  sarah ", "SARAH"):
        with pytest.raises(ValueError, match="already on this task"):
            _pair(conn, "fold", "frontend", attempt)


def test_the_same_name_on_a_DIFFERENT_task_is_fine(conn):
    """Uniqueness is per task, and only per task — two teams may both have a Sarah."""
    admin.create_task("t1", title="One", roles=["backend", "frontend"])
    admin.create_task("t2", title="Two", roles=["backend", "frontend"])
    _pair(conn, "t1", "backend", "Sarah")
    assert _pair(conn, "t2", "backend", "Sarah")["handle"] == "backend"


def test_a_revoked_agent_frees_its_name_again(conn):
    """Uniqueness is over LIVE agents, the same scope as the seat index — otherwise a
    re-pair after a revocation would be blocked by the ghost of the previous one."""
    admin.create_task("rev", title="Rev", roles=["backend", "frontend"])
    _pair(conn, "rev", "backend", "Sarah")
    admin.revoke_agent("Sarah", task="rev")

    assert _pair(conn, "rev", "frontend", "Sarah")["handle"] == "frontend"


def test_the_name_is_stored_trimmed(conn):
    admin.create_task("trim", title="Trim", roles=["backend", "frontend"])
    _pair(conn, "trim", "backend", "  Sarah   K ")
    row = conn.execute("SELECT name FROM agents WHERE task_id='trim'").fetchone()
    assert row["name"] == "Sarah K"


def test_a_blank_name_is_refused_with_a_reason(conn):
    admin.create_task("blank", title="Blank", roles=["backend", "frontend"])
    with pytest.raises(ValueError, match="pick a name"):
        _pair(conn, "blank", "backend", "   ")


def test_a_name_never_becomes_a_key_so_it_cannot_move_a_signature(conn):
    """Handles stay the identifier. The agent row, the party list and the signature all
    key on the SEAT, so a name is free to be display-only — and must stay that way."""
    admin.create_task("keys", title="Keys", roles=["backend", "frontend"])
    a = _pair(conn, "keys", "backend", "Sarah")
    b = _pair(conn, "keys", "frontend", "Ade")
    ids = {h: resolve_agent_token(conn, r["agent_token"])
           for h, r in (("backend", a), ("frontend", b))}

    number = todos.propose_todo(conn, ids["backend"], "Sign-in", "POST /login",
                                ["backend", "frontend"])["number"]
    todos.accept_todo(conn, ids["frontend"], number)

    conn.execute("UPDATE agents SET name = 'Sarah Renamed' WHERE task_id='keys' "
                 "AND handle='backend'")
    conn.commit()
    row = todos.get_row(conn, "keys", number)
    assert todos.parties_of(row) == ["backend", "frontend"]


# --------------------------------------------------------------------------- #
# legacy databases: tolerated, never rewritten
# --------------------------------------------------------------------------- #
def test_a_database_that_already_holds_two_sarahs_still_boots_and_reads(tmp_path):
    """Enforcement is at INSERT time, inside redeem_invite's immediate transaction —
    NOT a unique index. A DB-level index could not be added to a database that already
    holds duplicates without either failing the boot or renaming somebody retroactively,
    and both are worse than letting history stand."""
    path = tmp_path / "dupes.db"
    db.init_db(path)
    c = db.connect(path)
    c.execute(
        "INSERT INTO tasks (id, title, state, roles_json, seat_roles_json, created_at) "
        "VALUES ('old','Old','open','[\"backend\",\"frontend\"]',"
        "'{\"backend\":\"backend\",\"frontend\":\"frontend\"}',?)",
        (time.time(),),
    )
    for handle in ("backend", "frontend"):
        c.execute(
            "INSERT INTO agents (task_id, name, role, handle, token_hash, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("old", "Sarah", handle, handle, sha256_hex("tok-" + handle), time.time()),
        )
    c.commit()
    c.close()

    db.init_db(path)  # a second boot must not trip over the duplicate

    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM agents WHERE name='Sarah'").fetchone()[0] == 2
    # Nobody was renamed.
    assert {r["name"] for r in c.execute("SELECT name FROM agents")} == {"Sarah"}
    c.close()


def test_a_duplicate_name_inherited_from_a_legacy_db_is_refused_not_guessed(conn):
    """It cannot be created any more, but it can be INHERITED — and an ambiguous
    `@sarah` must still name the candidates rather than pick one."""
    seed_task(conn, "legacy", roles=("backend", "frontend"))
    seed_agent(conn, "legacy", "backend", "Sarah", "sbk_a", handle="backend")
    seed_agent(conn, "legacy", "frontend", "Sarah", "sbk_b", handle="frontend")

    cast, roles = seats.cast_of(conn, "legacy")
    with pytest.raises(ValueError) as e:
        service.resolve_addressee("sarah", cast, roles, seats.names_of(conn, "legacy"),
                                  task_id="legacy")
    assert "@backend (Sarah)" in str(e.value) and "@frontend (Sarah)" in str(e.value)


# --------------------------------------------------------------------------- #
# 2. the join surface shows the cast — AFTER the code validates
# --------------------------------------------------------------------------- #
def test_the_preview_names_the_task_the_seat_and_who_is_already_here(conn):
    admin.create_task("pv", title="Payments platform",
                      roles=["backend", "frontend", "backend", "frontend"])
    _pair(conn, "pv", "backend-1", "Tony")
    _pair(conn, "pv", "frontend-1", "Sarah")
    _pair(conn, "pv", "backend-2", "Ade")
    code, _ = admin.mint_invite("pv", "frontend-2")

    p = pairing.invite_preview(conn, code)

    assert p["title"] == "Payments platform"
    # The seat and its role type come off the INVITE, so the joiner knows what they are
    # being onboarded as before typing anything.
    assert p["seat"] == "frontend-2" and p["role_type"] == "frontend"
    assert [(c["name"], c["role"]) for c in p["cast"]] == [
        ("Tony", "backend"), ("Sarah", "frontend"), ("Ade", "backend"),
    ]
    assert p["taken_names"] == ["ade", "sarah", "tony"]


def test_the_preview_refuses_a_code_it_cannot_validate(conn):
    """Never before the code validates: otherwise a random guess enumerates the team."""
    admin.create_task("secret", title="Secret", roles=["backend", "frontend"])
    _pair(conn, "secret", "backend", "Tony")

    for bad, msg in (("nope", "invalid invite code"),):
        with pytest.raises(ValueError, match=msg):
            pairing.invite_preview(conn, bad)


def test_the_preview_refuses_a_used_and_an_expired_code_in_the_same_words(conn):
    admin.create_task("burn", title="Burn", roles=["backend", "frontend", "qa"])
    used, _ = admin.mint_invite("burn", "backend")
    pairing.redeem_invite(conn, used, "Tony")
    with pytest.raises(ValueError, match="already been used"):
        pairing.invite_preview(conn, used)

    old, _ = admin.mint_invite("burn", "frontend")
    conn.execute("UPDATE invites SET expires_at = ? WHERE code_hash = ?",
                 (time.time() - 1, sha256_hex(old)))
    conn.commit()
    with pytest.raises(ValueError, match="expired"):
        pairing.invite_preview(conn, old)


def test_the_preview_does_not_burn_the_invite(conn):
    """The code is spent on the click, never on the load."""
    admin.create_task("keep", title="Keep", roles=["backend", "frontend"])
    code, _ = admin.mint_invite("keep", "frontend")

    pairing.invite_preview(conn, code)

    assert pairing.redeem_invite(conn, code, "Sarah")["handle"] == "frontend"


def test_the_preview_answers_whether_a_name_is_free(conn):
    admin.create_task("chk", title="Chk", roles=["backend", "frontend"])
    _pair(conn, "chk", "backend", "Sarah")
    code, _ = admin.mint_invite("chk", "frontend")

    taken = pairing.invite_preview(conn, code, "sarah")
    assert taken["name_available"] is False
    assert "already on this task" in taken["name_error"]

    free = pairing.invite_preview(conn, code, "Sarah K")
    assert free["name_available"] is True and free["name_error"] is None


def test_the_advisory_check_is_not_the_guarantee(conn):
    """The live check may race a simultaneous joiner; the insert-time check is what
    decides. Both exist, and they must not be confused for one another."""
    admin.create_task("race", title="Race", roles=["backend", "frontend"])
    code, _ = admin.mint_invite("race", "frontend")
    assert pairing.invite_preview(conn, code, "Sarah")["name_available"] is True

    # …someone else takes it in the gap.
    _pair(conn, "race", "backend", "Sarah")

    with pytest.raises(ValueError, match="already on this task"):
        pairing.redeem_invite(conn, code, "Sarah")


def test_a_refused_join_leaves_the_invite_unspent(conn):
    """The whole redemption is one immediate transaction, so a refusal rolls the burn
    back with it — otherwise every name collision would cost a single-use invite."""
    admin.create_task("atomic", title="Atomic", roles=["backend", "frontend"])
    _pair(conn, "atomic", "backend", "Sarah")
    code, _ = admin.mint_invite("atomic", "frontend")

    with pytest.raises(ValueError, match="already on this task"):
        pairing.redeem_invite(conn, code, "Sarah")

    assert pairing.redeem_invite(conn, code, "Sarah K")["handle"] == "frontend"


def test_the_first_seat_of_a_duplicated_role_type_can_still_be_invited(conn):
    """`@frontend` is the role TYPE everywhere, with no exception for invites any more —
    because a cast declaring two frontends numbers BOTH seats. Each has a token of its
    own, which matters precisely here: a seat nobody has joined has no display name to
    be disambiguated by, so if it had no handle of its own it could never be filled."""
    admin.create_task("inv", title="Inv", roles=["backend", "frontend", "frontend"])
    code, _ = admin.mint_invite("inv", "frontend-1")
    assert pairing.invite_preview(conn, code)["seat"] == "frontend-1"

    # …and the TYPE is refused, naming both candidates in the form you can type back.
    with pytest.raises(ValueError) as e:
        admin.mint_invite("inv", "frontend")
    assert "@frontend-1" in str(e.value) and "@frontend-2" in str(e.value)


def test_a_seat_that_only_became_ambiguous_LATER_can_still_be_invited(conn):
    """The other way in: the task had ONE frontend, so that seat holds the bare handle
    `frontend`, and a handle is immutable. Adding a second frontend makes `@frontend`
    the type — and the first seat reachable as `@frontend-1` instead."""
    admin.create_task("grew", title="Grew", roles=["backend", "frontend"])
    admin.add_seat("grew", "frontend")

    code, _ = admin.mint_invite("grew", "frontend-1")
    assert pairing.invite_preview(conn, code)["seat"] == "frontend", "an address, not a rename"


# --------------------------------------------------------------------------- #
# the CLI: print the cast, then ask (no per-keystroke check in a terminal)
# --------------------------------------------------------------------------- #
def test_the_cli_prints_the_cast_and_the_seat_it_is_offering(capsys):
    cli._print_preview({
        "title": "Payments platform", "task_id": "pay",
        "seat": "frontend-2", "role_type": "frontend",
        "cast": [{"name": "Tony", "role": "backend"}, {"name": "Sarah", "role": "frontend"}],
    })
    out = capsys.readouterr().out
    assert "You're joining: Payments platform" in out
    assert "@frontend-2  ·  frontend" in out
    assert "Tony (backend), Sarah (frontend)" in out


def test_the_cli_re_prompts_until_the_name_is_free(monkeypatch, capsys):
    pv = {"taken_names": ["tony", "sarah"]}
    typed = iter(["Sarah", "sarah", "Sarah K"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(typed))

    assert cli._ask_name(pv, None) == "Sarah K"
    err = capsys.readouterr().err
    assert err.count("already on this task") == 2


def test_the_cli_takes_a_free_name_from_the_flag_without_asking(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("should not prompt"))
    assert cli._ask_name({"taken_names": ["tony"]}, "Sarah") == "Sarah"


def test_the_cli_without_a_tty_does_not_hang_on_a_taken_name(monkeypatch):
    """A scripted join must fail with the broker's refusal, not block on a prompt
    nobody is there to answer."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert cli._ask_name({"taken_names": ["sarah"]}, "Sarah") == "Sarah"
    assert cli._ask_name({"taken_names": []}, None) is None


# --------------------------------------------------------------------------- #
# 3. a stalled todo reads differently from a slow one
# --------------------------------------------------------------------------- #
def _two_seats_one_empty(conn):
    """A task where `designer` was declared and invited but nobody ever redeemed it."""
    admin.create_task("stall", title="Stall", roles=["backend", "frontend", "designer"])
    ids = {}
    for seat, who in (("backend", "Tony"), ("frontend", "Sarah")):
        res = _pair(conn, "stall", seat, who)
        ids[seat] = resolve_agent_token(conn, res["agent_token"])
    admin.mint_invite("stall", "designer")  # invited, never redeemed
    conn.execute("UPDATE agents SET ready = 1, readiness_status = 'passed' "
                 "WHERE task_id = 'stall'")
    conn.commit()
    return ids


def test_a_todo_may_name_a_seat_nobody_has_joined(conn):
    """Deliberate — you can propose ahead of someone's arrival."""
    ids = _two_seats_one_empty(conn)
    t = todos.propose_todo(conn, ids["backend"], "Palette", "tokens",
                           ["backend", "designer"])
    assert t["parties"] == ["backend", "designer"]


def test_get_todos_says_which_awaited_seat_never_joined(conn):
    ids = _two_seats_one_empty(conn)
    todos.propose_todo(conn, ids["backend"], "Palette", "tokens", ["backend", "designer"])
    todos.propose_todo(conn, ids["backend"], "Sign-in", "POST /login",
                       ["backend", "frontend"])

    by_title = {t["title"]: t for t in todos.get_todos(conn, "stall")}
    assert by_title["Palette"]["awaiting"] == ["designer"]
    assert by_title["Palette"]["unjoined"] == ["designer"]
    # The one waiting on a person who IS here reads differently — same `awaiting`,
    # empty `unjoined`.
    assert by_title["Sign-in"]["awaiting"] == ["frontend"]
    assert by_title["Sign-in"]["unjoined"] == []


def test_the_next_step_spells_out_never_joined_for_the_agent_reading_it(conn):
    ids = _two_seats_one_empty(conn)
    t = todos.propose_todo(conn, ids["backend"], "Palette", "tokens",
                           ["backend", "designer"])
    row = todos.get_row(conn, "stall", t["number"])

    nxt = state.next_step(conn, "stall", row["id"])
    assert nxt["unjoined"] == ["designer"]
    assert "NEVER JOINED" in nxt["text"]
    # No deadline is invented — the escape hatch is a host decision, not a timer.
    assert "host drops the todo" in nxt["text"]


def test_the_next_step_on_a_todo_whose_parties_are_all_here_says_nothing_of_the_kind(conn):
    ids = _two_seats_one_empty(conn)
    t = todos.propose_todo(conn, ids["backend"], "Sign-in", "POST /login",
                           ["backend", "frontend"])
    row = todos.get_row(conn, "stall", t["number"])

    nxt = state.next_step(conn, "stall", row["id"])
    assert nxt["unjoined"] == [] and "NEVER JOINED" not in nxt["text"]


def test_the_rollup_counts_the_todos_blocked_on_a_seat_nobody_joined(conn):
    ids = _two_seats_one_empty(conn)
    todos.propose_todo(conn, ids["backend"], "Palette", "tokens", ["backend", "designer"])
    todos.propose_todo(conn, ids["backend"], "Sign-in", "POST /login",
                       ["backend", "frontend"])

    roll = todos.rollup(conn, "stall")
    assert roll["pending"] == 2 and roll["unjoined"] == 1


def test_the_count_is_a_subset_of_pending_and_clears_when_they_join(conn):
    ids = _two_seats_one_empty(conn)
    todos.propose_todo(conn, ids["backend"], "Palette", "tokens", ["backend", "designer"])
    assert todos.rollup(conn, "stall")["unjoined"] == 1

    _pair(conn, "stall", "designer", "Priya")

    assert todos.rollup(conn, "stall")["unjoined"] == 0
