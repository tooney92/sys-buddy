"""Specs for the auth core (SPEC §9): tokens, hashing, and identity resolution."""

from __future__ import annotations

import time

from sys_buddy import identity
from tests.conftest import seed_agent, seed_task, seed_viewer


# --- token generation -------------------------------------------------------
def test_tokens_have_expected_prefixes():
    assert identity.new_agent_token().startswith("sbk_")
    assert identity.new_viewer_token().startswith("sbv_")


def test_tokens_are_unique():
    tokens = {identity.new_agent_token() for _ in range(100)}
    assert len(tokens) == 100  # no collisions


def test_invite_code_is_namespaced_to_task():
    code = identity.new_invite_code("signin")
    assert code.startswith("signin-")
    # no visually ambiguous characters in the random suffix
    suffix = code.split("-", 1)[1]
    assert not (set(suffix) & set("0O1Il"))


def test_hash_is_not_reversible_and_stable():
    tok = identity.new_agent_token()
    assert identity.sha256_hex(tok) == identity.sha256_hex(tok)  # stable
    assert tok not in identity.sha256_hex(tok)                   # raw not embedded


# --- agent token resolution -------------------------------------------------
def test_valid_token_resolves_to_full_identity(conn):
    seed_task(conn, "signin", roles=("backend", "frontend"))
    seed_agent(conn, "signin", "frontend", "dave-frontend", "sbk_valid")

    ident = identity.resolve_agent_token(conn, "sbk_valid")

    assert ident is not None
    assert ident.role == "frontend"
    assert ident.task_id == "signin"
    assert ident.name == "dave-frontend"


def test_forged_token_resolves_to_nothing(conn):
    seed_task(conn, "signin")
    seed_agent(conn, "signin", "frontend", "dave-frontend", "sbk_valid")

    assert identity.resolve_agent_token(conn, "sbk_not_a_real_token") is None
    assert identity.resolve_agent_token(conn, "") is None


def test_revoked_token_is_rejected(conn):
    seed_task(conn, "signin")
    seed_agent(conn, "signin", "frontend", "dave-frontend", "sbk_valid")

    # revoke it
    conn.execute("UPDATE agents SET revoked_at = ? WHERE name = 'dave-frontend'", (time.time(),))
    conn.commit()

    assert identity.resolve_agent_token(conn, "sbk_valid") is None


# --- viewer token resolution ------------------------------------------------
def test_buddy_viewer_is_scoped_to_one_task(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "dave", "sbv_buddy", task_id="signin")

    v = identity.resolve_viewer_token(conn, "sbv_buddy")

    assert v is not None
    assert v.task_id == "signin"
    assert v.is_host is False


def test_host_viewer_sees_all_tasks(conn):
    seed_viewer(conn, "host", "sbv_host", task_id=None)

    v = identity.resolve_viewer_token(conn, "sbv_host")

    assert v is not None
    assert v.is_host is True


def test_revoked_viewer_is_rejected(conn):
    seed_task(conn, "signin")
    seed_viewer(conn, "dave", "sbv_buddy", task_id="signin")
    conn.execute("UPDATE viewers SET revoked_at = ? WHERE label = 'dave'", (time.time(),))
    conn.commit()

    assert identity.resolve_viewer_token(conn, "sbv_buddy") is None


# --------------------------------------------------------------------------- #
# ensure_local_identity is keyed on the NAME, not the role
# --------------------------------------------------------------------------- #
# Regression: the lookup used to match on `role`, but inserts set role=name. Once an
# agent's role differed from its name (reassigned, or seeded then corrected), the lookup
# missed and inserted a SECOND agent with the same name — and appended that name to
# roles_json as a phantom role, so the task showed duplicate agents and pre-flight chips
# that no flow could ever clear.
def test_ensure_local_identity_is_idempotent_after_a_role_change(conn):
    from sys_buddy import service

    first = service.ensure_local_identity(conn, "demo", "be-agent")
    conn.execute(
        "UPDATE agents SET role = ?, handle = ? WHERE id = ?",
        ("backend", "backend", first.agent_id),
    )
    conn.commit()

    again = service.ensure_local_identity(conn, "demo", "be-agent")

    assert again.agent_id == first.agent_id, "must find the existing agent, not duplicate it"
    assert again.role == "backend", "must return the CURRENT seat, not the name"
    assert again.kind == "backend", "must return the CURRENT role type, not the name"

    rows = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE task_id = ? AND name = ?", ("demo", "be-agent")
    ).fetchone()[0]
    assert rows == 1


def test_ensure_local_identity_does_not_grow_roles_json_on_reseen_agent(conn):
    import json

    from sys_buddy import service

    ident = service.ensure_local_identity(conn, "demo2", "be-agent")
    conn.execute("UPDATE agents SET role = ? WHERE id = ?", ("backend", ident.agent_id))
    conn.commit()

    before = json.loads(
        conn.execute("SELECT roles_json FROM tasks WHERE id = ?", ("demo2",)).fetchone()[0]
    )
    service.ensure_local_identity(conn, "demo2", "be-agent")
    after = json.loads(
        conn.execute("SELECT roles_json FROM tasks WHERE id = ?", ("demo2",)).fetchone()[0]
    )

    assert before == after, "re-seeing an agent must not append a phantom role"


def test_ensure_local_identity_still_provisions_a_genuinely_new_agent(conn):
    import json

    from sys_buddy import service

    service.ensure_local_identity(conn, "demo3", "be-agent")
    second = service.ensure_local_identity(conn, "demo3", "fe-agent")

    assert second.role == "fe-agent"  # name doubles as role on first sight
    roles = json.loads(
        conn.execute("SELECT roles_json FROM tasks WHERE id = ?", ("demo3",)).fetchone()[0]
    )
    assert roles == ["be-agent", "fe-agent"]
