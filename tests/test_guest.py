"""Specs for the GUEST seat (concierge mode).

A guest is a non-technical human who joins with NO AI of their own — present through a
browser message box, not an agent on ``/mcp``. Two properties carry this file:

* **A guest CAN write** — one narrow surface (``POST /guest/message``), authorised by a
  viewer token LINKED to her seat (``viewers.agent_id``), with the message stamped
  server-side from that seat and its type hard-coded to ``note`` (never a lifecycle event).
* **Nobody else can** — every host/buddy viewer has a NULL ``agent_id``, so the write
  path refuses them and the dashboard stays read-only (D11) for everyone but the guest.
"""

from __future__ import annotations

import asyncio

import pytest

from sys_buddy import admin, guest, seats, service
from sys_buddy.config import Config, get_config
from sys_buddy.identity import resolve_viewer_token, sha256_hex
from sys_buddy.server import build_server
from tests.conftest import seed_task, seed_viewer


def _task(conn, task_id="acme-site"):
    seed_task(conn, task_id, roles=("frontend",))
    return task_id


# --------------------------------------------------------------------------- #
# provisioning — admin.add_guest
# --------------------------------------------------------------------------- #
def test_add_guest_creates_a_ready_tokenless_seat(conn):
    _task(conn)
    res = admin.add_guest("acme-site", "Ada")

    assert res["seat"]
    assert res["viewer_token"].startswith("sbv_")
    row = conn.execute(
        "SELECT role, ready, token_hash FROM agents WHERE handle = ? AND task_id = 'acme-site'",
        (res["seat"],),
    ).fetchone()
    assert row["role"] == seats.GUEST_ROLE
    assert row["ready"] == 1               # auto-ready: a guest does no gradeable work
    assert row["token_hash"] is None       # no agent token — she never calls /mcp


def test_add_guest_links_the_viewer_to_the_seat(conn):
    _task(conn)
    res = admin.add_guest("acme-site", "Ada")

    viewer = resolve_viewer_token(conn, res["viewer_token"])
    assert viewer is not None
    assert viewer.is_guest is True         # the linked-agent flag the dashboard keys on
    assert viewer.task_id == "acme-site"   # task-scoped, like any buddy


def test_add_guest_refuses_a_blank_name(conn):
    _task(conn)
    with pytest.raises(ValueError):
        admin.add_guest("acme-site", "   ")


def test_add_guest_refuses_a_closed_task(conn):
    _task(conn)
    conn.execute("UPDATE tasks SET closed_at = 1 WHERE id = 'acme-site'")
    conn.commit()
    with pytest.raises(ValueError):
        admin.add_guest("acme-site", "Ada")


# --------------------------------------------------------------------------- #
# the identity seam — who may write
# --------------------------------------------------------------------------- #
def test_guest_viewer_builds_a_writable_identity(conn):
    _task(conn)
    res = admin.add_guest("acme-site", "Ada")
    viewer = resolve_viewer_token(conn, res["viewer_token"])

    ident = guest._guest_identity(conn, viewer)
    assert ident is not None
    assert ident.role_type == seats.GUEST_ROLE
    assert ident.task_id == "acme-site"


def test_a_normal_viewer_gets_no_writable_identity(conn):
    """A host/buddy viewer has no linked agent, so it can never build a write identity —
    which is what keeps the whole dashboard read-only for everyone but a guest."""
    _task(conn)
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)      # host: all-tasks, no link
    seed_viewer(conn, "a-buddy", "sbv_buddytok", task_id="acme-site")  # buddy, no link

    for tok in ("sbv_hosttok", "sbv_buddytok"):
        v = resolve_viewer_token(conn, tok)
        assert v is not None and v.is_guest is False
        assert guest._guest_identity(conn, v) is None


def test_a_revoked_guest_seat_can_no_longer_write(conn):
    _task(conn)
    res = admin.add_guest("acme-site", "Ada")
    conn.execute(
        "UPDATE agents SET revoked_at = 1 WHERE handle = ? AND task_id = 'acme-site'",
        (res["seat"],),
    )
    conn.commit()
    viewer = resolve_viewer_token(conn, res["viewer_token"])
    assert guest._guest_identity(conn, viewer) is None


def test_a_host_scoped_viewer_linked_to_an_agent_is_refused(conn):
    """Belt-and-braces: a viewer with NULL task_id (all-tasks scope) must never resolve to
    a write seat, even if its agent_id is somehow set — an all-tasks credential writing as
    one seat would break the task-scoping invariant."""
    _task(conn)
    res = admin.add_guest("acme-site", "Ada")
    agent_id = conn.execute(
        "SELECT id FROM agents WHERE handle = ? AND task_id = 'acme-site'", (res["seat"],)
    ).fetchone()["id"]
    # a host-scoped viewer (task_id NULL) hand-linked to the guest agent
    conn.execute(
        "INSERT INTO viewers (task_id, label, token_hash, created_at, agent_id) "
        "VALUES (NULL, 'rogue-host', ?, ?, ?)",
        (sha256_hex("sbv_rogue"), 1.0, agent_id),
    )
    conn.commit()
    viewer = resolve_viewer_token(conn, "sbv_rogue")
    assert viewer is not None and viewer.is_guest is True  # it IS linked…
    assert guest._guest_identity(conn, viewer) is None      # …but the write is still refused


# --------------------------------------------------------------------------- #
# the write lands, stamped from the guest seat, as a `note`
# --------------------------------------------------------------------------- #
def test_guest_message_is_stamped_from_the_guest_seat(conn):
    _task(conn)
    res = admin.add_guest("acme-site", "Ada")
    ident = guest._guest_identity(conn, resolve_viewer_token(conn, res["viewer_token"]))

    service.post_message(conn, ident, "note", "I love the header — can we make it green?")

    hist = service.channel_history(conn, "acme-site")
    mine = [m for m in hist if "green" in m["body"]]
    assert len(mine) == 1
    assert mine[0]["role"] == res["seat"]   # attributed to the guest's seat, unforgeably
    assert mine[0]["type"] == "note"


# --------------------------------------------------------------------------- #
# the HTTP route — POST /guest/message
# --------------------------------------------------------------------------- #
class _Req:
    """The slice of a Starlette request the guest handler reads."""

    def __init__(self, token=None, body=None):
        self.query_params = {"v": token} if token else {}
        self.headers = {}
        self.cookies = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _guest_endpoint(dbfile):
    mcp = build_server(Config(mode="local", db_path=dbfile))
    routes = [
        r for r in getattr(mcp, "_additional_http_routes", [])
        if str(getattr(r, "path", "")) == "/guest/message"
    ]
    assert routes, "expected POST /guest/message to be registered"
    for r in routes:
        # A guest WRITES here and does nothing else — no read verb, no other method.
        assert set(r.methods) <= {"POST"}, f"{r.path} accepts {r.methods}"
    return routes[0].endpoint


def test_route_accepts_the_guest_and_refuses_everyone_else(conn):
    dbfile = get_config().db_path
    _task(conn)
    res = admin.add_guest("acme-site", "Ada")
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)
    endpoint = _guest_endpoint(dbfile)

    # the guest → 201, and her message is stored
    ok = asyncio.run(endpoint(_Req(token=res["viewer_token"], body={"body": "hello team"})))
    assert ok.status_code == 201

    # a host viewer → 403 (read-only stays read-only)
    forbidden = asyncio.run(
        endpoint(_Req(token="sbv_hosttok", body={"body": "i should not be able to"}))
    )
    assert forbidden.status_code == 403

    # no credential at all → 403
    anon = asyncio.run(endpoint(_Req(body={"body": "nope"})))
    assert anon.status_code == 403

    # an empty message → 400, nothing stored
    empty = asyncio.run(endpoint(_Req(token=res["viewer_token"], body={"body": "   "})))
    assert empty.status_code == 400

    bodies = [m["body"] for m in service.channel_history(conn, "acme-site")]
    assert "hello team" in bodies
    assert "i should not be able to" not in bodies
    assert "nope" not in bodies


# --------------------------------------------------------------------------- #
# the invite flow — a guest is a role in the cast, self-names on the join page
# --------------------------------------------------------------------------- #
def test_redeeming_a_guest_invite_yields_a_view_link_not_an_agent_token(conn):
    """A guest is picked like any role and gets an ordinary invite. Redeeming it — the
    guest typing her own name on the join page — produces a viewer LINK, not an agent
    token, and the seat is a real, writable guest."""
    from sys_buddy import pairing

    created = admin.create_task(None, title="Ada's site", roles=["backend", "frontend", "guest"])
    task = created["id"]
    code, _ = admin.mint_invite(task, "guest")

    res = pairing.redeem_invite(conn, code, "Ada")
    assert res["guest"] is True
    assert res["agent_token"] is None          # no AI, no token
    assert res["role_type"] == seats.GUEST_ROLE

    v = resolve_viewer_token(conn, res["viewer_token"])
    assert v is not None and v.is_guest is True
    assert v.task_id == task
    assert guest._guest_identity(conn, v) is not None   # she can write


def test_guest_seat_is_not_a_builder(conn):
    """`builder_handles` — who must sign a deliverable list — excludes the guest, exactly
    as it excludes the owner. Otherwise a guest with no AI would block every lock."""
    created = admin.create_task(
        None, title="Site", roles=["backend", "frontend", "guest"], mode="engagement",
    )
    task = created["id"]
    builders = seats.builder_handles(conn, task)
    assert "guest" not in [seats.slug(b) for b in builders]
    assert set(builders) == {"backend", "frontend"}
