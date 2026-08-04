"""Recovering from an expired agent token, and being told that is what happened.

From a live incident. A tunnelled broker gives agent tokens a 24h TTL so a leaked one
self-expires, and the code that does it says "agents refresh with rotate_token". They
cannot: `rotate_token` authenticates with the token it would replace, so the moment one
expires the agent is locked out of the only tool that could have saved it — and
`report_status("stuck")` is gone too, so it cannot even escalate. Nothing warned
beforehand, and the failure said "invalid or revoked", which sent two people hunting a
revocation nobody had performed.

Two fixes are pinned here: a host-side way to extend, and an error that says which.
"""

from __future__ import annotations

import time

import pytest

from sys_buddy import admin, identity
from tests.conftest import seed_agent, seed_task


# --------------------------------------------------------------------------- #
# extend_agent_tokens
# --------------------------------------------------------------------------- #
def test_extend_pushes_expiry_out_and_revives_a_dead_token(conn):
    """The whole point: an agent that is locked out right now is working again after this,
    with no re-pair and no session restart, because the broker re-reads the token per call."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    seed_agent(conn, "signin", "backend", "al", "sbk_al")
    dead = time.time() - 3600
    conn.execute("UPDATE agents SET expires_at = ? WHERE task_id = 'signin'", (dead,))
    conn.commit()
    assert identity.resolve_agent_token(conn, "sbk_al") is None   # locked out

    touched = admin.extend_agent_tokens("signin", hours=24)
    assert len(touched) == 1
    assert touched[0]["seat"] == "backend"
    # The flag a host reacts to — this is the seat they were worried about.
    assert touched[0]["was_expired"] is True

    assert identity.resolve_agent_token(conn, "sbk_al") is not None  # working again


def test_never_clears_the_expiry_entirely(conn):
    """What you want for a long session or a demo — the same state a same-machine broker
    mints, where no expiry is set at all."""
    seed_task(conn, "signin", roles=("backend",))
    seed_agent(conn, "signin", "backend", "al", "sbk_al")
    conn.execute("UPDATE agents SET expires_at = ? WHERE task_id = 'signin'",
                 (time.time() + 60,))
    conn.commit()

    admin.extend_agent_tokens("signin", never=True)
    row = conn.execute("SELECT expires_at FROM agents WHERE task_id='signin'").fetchone()
    assert row["expires_at"] is None


def test_extend_never_re_admits_a_revoked_seat(conn):
    """The rule that keeps this safe to run. "Extend the tokens" must not quietly undo a
    revocation — a host who cut somebody off did it on purpose."""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    seed_agent(conn, "signin", "backend", "al", "sbk_al")
    seed_agent(conn, "signin", "frontend", "eve", "sbk_eve")
    conn.execute("UPDATE agents SET revoked_at = ? WHERE handle = 'frontend'", (time.time(),))
    conn.commit()

    touched = admin.extend_agent_tokens("signin", hours=24)
    assert [t["seat"] for t in touched] == ["backend"]
    assert identity.resolve_agent_token(conn, "sbk_eve") is None   # still out
    row = conn.execute("SELECT expires_at FROM agents WHERE handle='frontend'").fetchone()
    assert row["expires_at"] is None or row["expires_at"] < time.time() + 1


def test_extend_refuses_an_unknown_task(conn):
    with pytest.raises(ValueError):
        admin.extend_agent_tokens("no-such-task")


def test_extend_on_a_task_with_no_agents_is_not_an_error(conn):
    seed_task(conn, "signin", roles=("backend",))
    assert admin.extend_agent_tokens("signin") == []


# --------------------------------------------------------------------------- #
# saying WHICH
# --------------------------------------------------------------------------- #
def test_explain_distinguishes_expired_revoked_and_unknown(conn):
    seed_task(conn, "signin", roles=("backend", "frontend", "mobile"))
    seed_agent(conn, "signin", "backend", "al", "sbk_expired")
    seed_agent(conn, "signin", "frontend", "eve", "sbk_revoked")
    seed_agent(conn, "signin", "mobile", "mo", "sbk_live")
    conn.execute("UPDATE agents SET expires_at = ? WHERE handle='backend'",
                 (time.time() - 60,))
    conn.execute("UPDATE agents SET revoked_at = ? WHERE handle='frontend'", (time.time(),))
    conn.commit()

    assert identity.explain_agent_token(conn, "sbk_expired") == "expired"
    assert identity.explain_agent_token(conn, "sbk_revoked") == "revoked"
    assert identity.explain_agent_token(conn, "sbk_nonsense") == "unknown"
    assert identity.explain_agent_token(conn, "") == "unknown"
    # A LIVE token has no failure to explain; the caller never asks, but it must not lie.
    assert identity.explain_agent_token(conn, "sbk_live") == "unknown"


def test_the_middleware_can_actually_call_explain():
    """A NameError waiting to happen, and it nearly shipped. `explain_agent_token` is used
    inside a request handler, so a missing import raises at REQUEST time, not import time —
    `import middleware` succeeds either way and every other test still passes. Assert the
    name is genuinely in the module's namespace."""
    from sys_buddy import middleware

    assert hasattr(middleware, "explain_agent_token"), (
        "middleware references explain_agent_token but does not import it — every "
        "unauthorized request would raise NameError instead of an auth error"
    )


def test_the_expired_message_names_the_fix_and_who_runs_it():
    """An agent reading this cannot act on it — `rotate_token` needs a working token. So the
    message must name the HOST's command, and say that nothing else is needed afterwards,
    or the agent's human starts re-pairing and loses the session's context for nothing."""
    from pathlib import Path

    from sys_buddy import middleware

    src = Path(middleware.__file__).read_text(encoding="utf-8")
    expired = src.split('if reason == "expired":')[1][:900]
    assert "EXPIRED" in expired
    assert "extend-tokens" in expired
    assert "rotate_token" in expired          # says why self-service will not work
    assert "no re-pair" in expired            # says what is NOT needed
    # And the old one-size-fits-all sentence must be gone.
    assert "invalid or revoked agent token" not in src
