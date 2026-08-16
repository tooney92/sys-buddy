"""Specs for optional directed messages (`to_role`).

A message with no `to_role` (or empty) broadcasts to every OTHER agent on the
task, exactly as before. A message with `to_role="<role>"` is delivered only to
agents whose role matches; an unknown role is rejected.
"""

from __future__ import annotations

import pytest

from sys_buddy import service
from tests.conftest import seed_agent, seed_task


def _mk(conn, task="signin", roles=("backend", "frontend", "mobile")):
    seed_task(conn, task, roles=roles)
    ids = {}
    for role in roles:
        ids[role] = service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
    return ids


def test_broadcast_reaches_all_other_roles(conn):
    ag = _mk(conn)
    service.post_message(conn, ag["backend"], "question", "hi", to_role=None)

    assert len(service.fetch_unacked(conn, ag["frontend"])) == 1
    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1
    assert service.fetch_unacked(conn, ag["backend"]) == []


def test_directed_reaches_only_target_role(conn):
    ag = _mk(conn)
    service.post_message(conn, ag["backend"], "question", "just you", to_role="mobile")

    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1
    assert service.fetch_unacked(conn, ag["frontend"]) == []
    assert service.fetch_unacked(conn, ag["backend"]) == []


def test_directed_envelope_shows_to(conn):
    ag = _mk(conn)
    service.post_message(conn, ag["backend"], "question", "just you", to_role="mobile")

    body = service.fetch_unacked(conn, ag["mobile"])[0]["content"]

    assert 'to="mobile"' in body


def test_directed_rejects_unknown_role(conn):
    ag = _mk(conn)
    with pytest.raises(ValueError):
        service.post_message(conn, ag["backend"], "question", "x", to_role="designer")


def test_broadcast_still_default(conn):
    ag = _mk(conn)
    service.post_message(conn, ag["backend"], "question", "hi all")

    assert len(service.fetch_unacked(conn, ag["frontend"])) == 1
    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1


# --------------------------------------------------------------------------- #
# role tags (BE/FE/MB/DE)
# --------------------------------------------------------------------------- #
# A human types `sm @BE ...` to their own agent; the agent is briefed to expand the
# tag, and the broker resolves it too so a tag reaching the wire still delivers.
# Everything downstream must see the CANONICAL role, never the tag.
def test_tag_resolves_to_canonical_role(conn):
    ag = _mk(conn)
    receipt = service.post_message(conn, ag["backend"], "question", "just you", to_role="MB")

    assert receipt["to_role"] == "mobile"
    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1
    assert service.fetch_unacked(conn, ag["frontend"]) == []


def test_tag_is_case_insensitive(conn):
    ag = _mk(conn)
    for tag in ("be", "BE", "Be"):
        res = service.resolve_role(tag, ["backend", "frontend"])
        assert res.handles == ["backend"] and res.canonical == "backend"


def test_full_role_name_is_case_insensitive(conn):
    assert service.resolve_role("Backend", ["backend"]).handles == ["backend"]
    assert service.resolve_role("MOBILE", ["mobile"]).handles == ["mobile"]


def test_envelope_shows_expanded_role_not_the_tag(conn):
    ag = _mk(conn)
    service.post_message(conn, ag["backend"], "question", "just you", to_role="MB")

    body = service.fetch_unacked(conn, ag["mobile"])[0]["content"]

    assert 'to="mobile"' in body
    assert "MB" not in body


def test_tag_for_role_not_on_task_is_still_rejected(conn):
    # DE is a known tag, but this task has no designer — resolving a tag must never
    # invent a role or silently broadcast.
    ag = _mk(conn)
    with pytest.raises(ValueError):
        service.post_message(conn, ag["backend"], "question", "x", to_role="DE")


def test_real_role_named_like_a_tag_wins_over_the_tag(conn):
    # A task is free to declare a role literally called "be"; the exact match must
    # win so a real role can never be shadowed by the tag table. It matches as a ROLE
    # TYPE now rather than as a handle — on a one-seat-per-type task those are the same
    # string, so the seat it names is unchanged. The tag is still not consulted.
    res = service.resolve_role("be", ["be", "backend"])
    assert res.handles == ["be"] and res.kind == "role"


def test_designer_tag_resolves_when_declared(conn):
    ag = _mk(conn, task="design", roles=("backend", "designer"))
    receipt = service.post_message(conn, ag["backend"], "question", "look", to_role="DE")

    assert receipt["to_role"] == "designer"
    assert len(service.fetch_unacked(conn, ag["designer"])) == 1


# --------------------------------------------------------------------------- #
# several directed recipients on ONE message (to_roles) — "Tony AND James"
# --------------------------------------------------------------------------- #
def test_multi_directed_reaches_each_listed_and_no_one_else(conn):
    ag = _mk(conn, roles=("backend", "frontend", "mobile", "designer"))
    service.post_message(
        conn, ag["backend"], "question", "you two", to_roles=["frontend", "mobile"]
    )
    assert len(service.fetch_unacked(conn, ag["frontend"])) == 1
    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1
    assert service.fetch_unacked(conn, ag["designer"]) == []   # not listed → does not see it
    assert service.fetch_unacked(conn, ag["backend"]) == []    # the sender never gets their own


def test_multi_directed_envelope_lists_all_targets(conn):
    ag = _mk(conn, roles=("backend", "frontend", "mobile"))
    service.post_message(
        conn, ag["backend"], "question", "you two", to_roles=["frontend", "mobile"]
    )
    body = service.fetch_unacked(conn, ag["mobile"])[0]["content"]
    assert 'to="frontend mobile"' in body   # a recipient sees it was directed at a specific few


def test_single_element_to_roles_is_a_plain_directed(conn):
    ag = _mk(conn)
    receipt = service.post_message(
        conn, ag["backend"], "question", "just you", to_roles=["mobile"]
    )
    # Collapses onto the ordinary single-recipient path — no message_recipients rows needed.
    assert receipt["to_role"] == "mobile"
    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1
    assert service.fetch_unacked(conn, ag["frontend"]) == []


def test_multi_directed_dedupes_a_tag_and_its_role(conn):
    ag = _mk(conn)
    # "MB" and "mobile" are the same seat — dedupe to one, so it is a single directed message.
    receipt = service.post_message(
        conn, ag["backend"], "question", "hi", to_roles=["mobile", "MB"]
    )
    assert receipt["to_role"] == "mobile"
    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1


def test_empty_to_roles_broadcasts(conn):
    ag = _mk(conn)
    service.post_message(conn, ag["backend"], "question", "hi all", to_roles=[])
    assert len(service.fetch_unacked(conn, ag["frontend"])) == 1
    assert len(service.fetch_unacked(conn, ag["mobile"])) == 1


def test_multi_directed_rejects_an_unknown_addressee(conn):
    ag = _mk(conn)
    with pytest.raises(ValueError):
        service.post_message(
            conn, ag["backend"], "question", "x", to_roles=["frontend", "designer"]
        )
