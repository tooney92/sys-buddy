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
