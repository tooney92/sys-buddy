"""Specs for pairing and host-side admin (SPEC §9).

``redeem_invite`` is called directly with the ``conn`` fixture — no HTTP server. The
admin functions open their own connection off ``get_config().db_path``, which the
``conn`` fixture has already pointed at the isolated temp db, so they share the same
database as the fixture connection.
"""

from __future__ import annotations

import time

from sys_buddy import admin, pairing
from sys_buddy.identity import sha256_hex
from tests.conftest import seed_task


# --- create_task ------------------------------------------------------------
def test_create_task_inserts_open_task_and_event(conn):
    task = admin.create_task("signin", title="Sign in", roles=["backend", "frontend"])

    assert task["id"] == "signin"
    assert task["state"] == "open"
    row = conn.execute("SELECT state, roles_json FROM tasks WHERE id='signin'").fetchone()
    assert row["state"] == "open"
    assert '"backend"' in row["roles_json"] and '"frontend"' in row["roles_json"]
    ev = conn.execute("SELECT detail_json FROM events WHERE task_id='signin' AND kind='task'").fetchone()
    assert "Task created: signin" in ev["detail_json"]


def test_create_task_rejects_duplicate_id(conn):
    admin.create_task("signin", title="Sign in", roles=["backend"])
    try:
        admin.create_task("signin", title="Again", roles=["backend"])
        assert False, "expected duplicate id to be rejected"
    except ValueError as e:
        assert "already exists" in str(e)


def test_create_task_requires_a_backend_role(conn):
    """A task with no 'backend' role would deadlock (can lock but never deploy),
    so it's rejected at creation (regression: review #3)."""
    try:
        admin.create_task("x", title="No deployer", roles=["api", "web"])
        assert False, "expected missing-backend-role rejection"
    except ValueError as e:
        assert "backend" in str(e)


# --- mint_invite ------------------------------------------------------------
def test_mint_invite_stores_only_a_hash(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    code, expires = admin.mint_invite("signin", "frontend")

    assert code.startswith("signin-")
    assert expires  # a human-readable expiry string
    rows = conn.execute("SELECT code_hash, used_at FROM invites").fetchall()
    assert len(rows) == 1
    # The raw code is never in the db — only its sha256.
    assert rows[0]["code_hash"] == sha256_hex(code)
    assert rows[0]["code_hash"] != code
    assert rows[0]["used_at"] is None


def test_mint_invite_rejects_unknown_task(conn):
    try:
        admin.mint_invite("nope", "frontend")
        assert False
    except ValueError as e:
        assert "unknown task" in str(e)


def test_mint_invite_rejects_role_not_on_task(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    try:
        admin.mint_invite("signin", "mobile")
        assert False
    except ValueError as e:
        assert "role 'mobile'" in str(e)


# --- redeem_invite ----------------------------------------------------------
def _mint(conn, task="signin", roles=("backend", "frontend"), role="frontend"):
    seed_task(conn, task, roles=roles)
    return admin.mint_invite(task, role)[0]


def test_redeem_invite_creates_agent_and_viewer_and_burns_invite(conn):
    code = _mint(conn)
    result = pairing.redeem_invite(conn, code, "dave-frontend")

    assert result["task_id"] == "signin"
    assert result["role"] == "frontend"
    assert result["agent_token"].startswith("sbk_")
    assert result["viewer_token"].startswith("sbv_")

    # Agent row: token stored as hash, not raw; scoped to the invite's task+role.
    agent = conn.execute("SELECT task_id, role, token_hash FROM agents WHERE name='dave-frontend'").fetchone()
    assert agent["task_id"] == "signin" and agent["role"] == "frontend"
    assert agent["token_hash"] == sha256_hex(result["agent_token"])

    # Viewer row: scoped to the task, hashed token.
    viewer = conn.execute("SELECT task_id, token_hash FROM viewers WHERE label='dave-frontend'").fetchone()
    assert viewer["task_id"] == "signin"
    assert viewer["token_hash"] == sha256_hex(result["viewer_token"])

    # Invite burned.
    assert conn.execute("SELECT used_at FROM invites").fetchone()["used_at"] is not None
    # Token event written.
    assert conn.execute("SELECT 1 FROM events WHERE task_id='signin' AND kind='token'").fetchone()


def test_revoked_role_can_be_repaired(conn):
    """Revoking an agent must free its seat so a replacement can pair — the revoked
    row stays for provenance but no longer blocks the role (regression: review #2)."""
    code1 = _mint(conn)
    pairing.redeem_invite(conn, code1, "dave-frontend")
    assert admin.revoke_agent("dave-frontend") == 1

    code2 = admin.mint_invite("signin", "frontend")[0]
    result = pairing.redeem_invite(conn, code2, "erin-frontend")  # must NOT raise

    assert result["role"] == "frontend"
    live = conn.execute(
        "SELECT name FROM agents WHERE task_id='signin' AND role='frontend' AND revoked_at IS NULL"
    ).fetchall()
    assert [r["name"] for r in live] == ["erin-frontend"]  # exactly one live agent for the seat


def test_close_task_burns_outstanding_invites(conn):
    """A not-yet-redeemed invite must die when the task closes, or a buddy could
    still join a closed task (regression: review #1, security)."""
    code = _mint(conn)                # minted, not yet redeemed
    admin.close_task("signin")
    try:
        pairing.redeem_invite(conn, code, "late-buddy")
        assert False, "expected redemption of a closed task's invite to fail"
    except ValueError as e:
        assert "used" in str(e).lower() or "closed" in str(e).lower()


def test_redeem_invite_stores_pubkey_when_supplied(conn):
    code = _mint(conn)
    pairing.redeem_invite(conn, code, "dave-frontend", pubkey="ssh-ed25519 AAAA")
    pk = conn.execute("SELECT pubkey FROM agents WHERE name='dave-frontend'").fetchone()["pubkey"]
    assert pk == "ssh-ed25519 AAAA"


def test_invite_cannot_be_redeemed_twice(conn):
    code = _mint(conn)
    pairing.redeem_invite(conn, code, "dave-frontend")
    try:
        pairing.redeem_invite(conn, code, "someone-else")
        assert False, "expected a used invite to be rejected"
    except ValueError as e:
        assert "already been used" in str(e)


def test_expired_invite_is_rejected(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    code = admin.mint_invite("signin", "frontend")[0]
    # Force the invite into the past.
    conn.execute("UPDATE invites SET expires_at = ?", (time.time() - 1,))
    conn.commit()
    try:
        pairing.redeem_invite(conn, code, "dave-frontend")
        assert False, "expected an expired invite to be rejected"
    except ValueError as e:
        assert "expired" in str(e)


def test_unknown_code_is_rejected(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    try:
        pairing.redeem_invite(conn, "signin-BOGUSxyz", "dave-frontend")
        assert False
    except ValueError as e:
        assert "invalid invite code" in str(e)


def test_pairing_same_role_twice_is_rejected(conn):
    """UNIQUE(task_id, role) — the fixed-cast rule. A second buddy cannot claim a
    role that is already paired."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    code1 = admin.mint_invite("signin", "frontend")[0]
    code2 = admin.mint_invite("signin", "frontend")[0]
    pairing.redeem_invite(conn, code1, "dave-frontend")
    try:
        pairing.redeem_invite(conn, code2, "eve-frontend")
        assert False, "expected the second frontend pairing to be rejected"
    except ValueError as e:
        assert "already paired" in str(e) or "already taken" in str(e)


# --- host viewer ------------------------------------------------------------
def test_issue_host_viewer_is_all_tasks_and_returns_raw_token(conn):
    token = admin.issue_host_viewer("host")
    assert token.startswith("sbv_")
    row = conn.execute("SELECT task_id, token_hash FROM viewers WHERE label='host'").fetchone()
    assert row["task_id"] is None  # NULL = all tasks
    assert row["token_hash"] == sha256_hex(token)


# --- revocation -------------------------------------------------------------
def test_revoke_agent_flips_revoked_at(conn):
    code = _mint(conn)
    pairing.redeem_invite(conn, code, "dave-frontend")
    assert conn.execute("SELECT revoked_at FROM agents WHERE name='dave-frontend'").fetchone()["revoked_at"] is None

    n = admin.revoke_agent("dave-frontend")

    assert n == 1
    assert conn.execute("SELECT revoked_at FROM agents WHERE name='dave-frontend'").fetchone()["revoked_at"] is not None
    # Idempotent: re-revoking a dead agent revokes nothing.
    assert admin.revoke_agent("dave-frontend") == 0


def test_revoke_agent_unknown_returns_zero(conn):
    assert admin.revoke_agent("ghost") == 0


def test_revoke_viewer_flips_revoked_at(conn):
    admin.issue_host_viewer("dave")
    n = admin.revoke_viewer("dave")
    assert n == 1
    assert conn.execute("SELECT revoked_at FROM viewers WHERE label='dave'").fetchone()["revoked_at"] is not None
    assert admin.revoke_viewer("dave") == 0


# --- close_task -------------------------------------------------------------
def test_close_task_revokes_everything_for_the_task(conn):
    code = _mint(conn)
    pairing.redeem_invite(conn, code, "dave-frontend")  # agent + task viewer
    admin.issue_host_viewer("host")  # all-tasks viewer, must survive

    admin.close_task("signin")

    assert conn.execute("SELECT closed_at FROM tasks WHERE id='signin'").fetchone()["closed_at"] is not None
    assert conn.execute("SELECT revoked_at FROM agents WHERE task_id='signin'").fetchone()["revoked_at"] is not None
    assert conn.execute("SELECT revoked_at FROM viewers WHERE task_id='signin'").fetchone()["revoked_at"] is not None
    # The host's all-tasks viewer (task_id NULL) is untouched by closing one task.
    assert conn.execute("SELECT revoked_at FROM viewers WHERE label='host'").fetchone()["revoked_at"] is None


def test_close_unknown_task_is_rejected(conn):
    try:
        admin.close_task("nope")
        assert False
    except ValueError as e:
        assert "unknown task" in str(e)


# --- list_tasks -------------------------------------------------------------
def test_list_tasks_returns_id_state_title(conn):
    admin.create_task("signin", title="Sign in", roles=["backend"])
    admin.create_task("search", title="Search", roles=["backend"])
    rows = admin.list_tasks()
    ids = {r["id"] for r in rows}
    assert ids == {"signin", "search"}
    for r in rows:
        assert "state" in r and "title" in r
