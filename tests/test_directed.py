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
