"""Specs for the host-side admin id-derivation helpers.

Humans should only have to type a Title; ``new_task_id`` turns it into a slug + a
short random suffix, and ``create_task`` derives an id when none is supplied. The
``conn`` fixture points ``get_config().db_path`` at an isolated temp db, which the
``admin`` functions (which open their own connections) share.
"""

from __future__ import annotations

import re

from sys_buddy import admin


# --- new_task_id ------------------------------------------------------------
def test_new_task_id_slugifies_title():
    tid = admin.new_task_id("New API")
    assert tid.startswith("new-api-")
    # slug + 4-hex-char suffix, lowercase, hyphen-separated.
    assert re.fullmatch(r"new-api-[0-9a-f]{4}", tid)


def test_new_task_id_collapses_punctuation_and_repeats():
    tid = admin.new_task_id("  Fix the  Login/Auth!! bug  ")
    assert re.fullmatch(r"fix-the-login-auth-bug-[0-9a-f]{4}", tid)


def test_new_task_id_falls_back_when_title_slugs_to_empty():
    tid = admin.new_task_id("!!! @@@ ###")
    assert tid.startswith("task-")


def test_new_task_id_is_unique_across_calls():
    ids = {admin.new_task_id("same title") for _ in range(200)}
    assert len(ids) > 1  # random suffix keeps same-titled tasks apart


# --- create_task id derivation ----------------------------------------------
def test_create_task_derives_id_from_title_when_id_falsy(conn):
    t = admin.create_task(None, title="Sign In Flow", roles=["backend", "frontend"])
    assert t["id"].startswith("sign-in-flow-")
    assert t["title"] == "Sign In Flow"
    # The derived id is a real row.
    assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (t["id"],)).fetchone() is not None


def test_create_task_empty_string_id_also_derives(conn):
    t = admin.create_task("", title="Search", roles=["backend", "frontend"])
    assert t["id"].startswith("search-")


def test_create_task_explicit_id_used_verbatim(conn):
    t = admin.create_task("signin", title="Sign in", roles=["backend", "frontend"])
    assert t["id"] == "signin"


def test_create_task_rejects_the_reserved_broker_role(conn):
    """`broker` is how the broker's own pushes (contract_locked) are attributed in the
    envelope and the dashboard — a seat by that name could impersonate the broker."""
    import pytest

    with pytest.raises(ValueError, match="reserved"):
        admin.create_task("signin", title="Sign in", roles=["backend", "Broker"])


# --- modes ------------------------------------------------------------------
def test_create_task_accepts_every_declared_mode(conn):
    """The regression that matters. `engagement` shipped in v2.1.0 — schema, domain layer,
    tools, dashboard and briefings — but `create_task` still validated against the old pair,
    and it is the ONLY path that creates a task (the CLI and the desktop app both funnel
    here). So the feature had no door for two releases: everything downstream of creation
    worked and nobody could create one.

    Parametrised over `admin.MODES` rather than a literal list, so a workflow added later
    fails here until it is actually creatable."""
    import pytest

    for mode in admin.MODES:
        t = admin.create_task(
            None, title=f"task {mode}", roles=["backend", "frontend"], mode=mode
        )
        assert t["mode"] == mode, mode


def test_create_task_still_rejects_an_unknown_mode(conn):
    import pytest

    with pytest.raises(ValueError, match="unknown mode"):
        admin.create_task("signin", title="Sign in", roles=["backend"], mode="supervisor")


def test_engagement_is_creatable_without_an_owner_seat(conn):
    """Deliberately NOT refused here. The cast is not frozen at setup (`add_seat` exists),
    so a host may create the engagement and invite the client afterwards — and
    `deliverables._assert_owner` is where the absence is caught, with a message that names
    the fix. Enforcing it at creation would break the add-the-client-later flow."""
    t = admin.create_task(None, title="No client yet", roles=["backend"], mode="engagement")
    assert t["mode"] == "engagement"
