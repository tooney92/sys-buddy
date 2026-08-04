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


# --- more than one seat of a role ------------------------------------------
def test_create_task_takes_several_seats_of_one_role(conn):
    """The desktop app could only ever express ONE seat per role type — its cast was a
    boolean map — so a two-frontend task was unreachable from the app even though this has
    worked at the domain level since v2.0.0. The app now sends a repeated role, which is
    the shape asserted here."""
    t = admin.create_task(
        None, title="Auth rework",
        roles=["backend", "backend", "frontend", "frontend", "frontend"],
    )
    assert t["roles"] == ["backend-1", "backend-2", "frontend-1", "frontend-2", "frontend-3"]
    assert t["seat_roles"] == {
        "backend-1": "backend", "backend-2": "backend",
        "frontend-1": "frontend", "frontend-2": "frontend", "frontend-3": "frontend",
    }


def test_one_of_each_is_unchanged_by_that(conn):
    """The regression that matters: a one-seat-per-type cast — every task made before the
    picker could count — still names its seats after the role type, with no suffix."""
    t = admin.create_task(None, title="Classic", roles=["backend", "frontend"])
    assert t["roles"] == ["backend", "frontend"]
    assert t["seat_roles"] == {"backend": "backend", "frontend": "frontend"}


def test_a_cast_of_only_frontends_is_legitimate(conn):
    """"Just FEs" is a real cast: two frontend SEATS are two agents who can hold a contract
    with each other, so the app's two-agent minimum counts seats rather than role types."""
    t = admin.create_task(None, title="FE pair", roles=["frontend", "frontend"])
    assert t["roles"] == ["frontend-1", "frontend-2"]


def test_the_app_can_express_more_than_one_of_a_role():
    """A source guard on the other half. The domain has always accepted a repeated role; the
    bug was that the desktop app's cast was `{backend:true}` — a boolean, with no notion of
    HOW MANY — so nothing could ever send one. If that map goes back to booleans, multi-seat
    casts become unreachable again from the only surface most hosts use."""
    from pathlib import Path

    from sys_buddy import gui

    html = (Path(gui.__file__).parent / "gui_app.html").read_text(encoding="utf-8")
    assert "var cast = { backend:1 }" in html, "the cast is a boolean again — no counts"
    assert "cast = { backend:true }" not in html
    # The `+` affordance and the expansion that turns a count into repeated roles.
    assert 'class="role-more"' in html
    assert "function castRoles()" in html and "for (var i = 0; i < countOf(r); i++)" in html
