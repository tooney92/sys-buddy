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

from sys_buddy import admin, guest, seats, service, state, todos
from sys_buddy.config import Config, get_config
from sys_buddy.identity import Identity, resolve_viewer_token, sha256_hex
from sys_buddy.server import build_server
from tests.conftest import seed_agent, seed_task, seed_viewer


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
    """The slice of a Starlette request the guest handlers read."""

    def __init__(self, token=None, body=None, raw=None, query=None, headers=None):
        self.query_params = dict(query or {})
        if token:
            self.query_params["v"] = token
        self.headers = dict(headers or {})
        self.cookies = {}
        self._body = body   # for .json()
        self._raw = raw     # for .body()

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    async def body(self):
        return self._raw or b""


def _endpoint(dbfile, path="/guest/message"):
    mcp = build_server(Config(mode="local", db_path=dbfile))
    routes = [
        r for r in getattr(mcp, "_additional_http_routes", [])
        if str(getattr(r, "path", "")) == path
    ]
    assert routes, f"expected POST {path} to be registered"
    for r in routes:
        assert set(r.methods) <= {"POST"}, f"{r.path} accepts {r.methods}"
    return routes[0].endpoint


def _guest_endpoint(dbfile):
    return _endpoint(dbfile, "/guest/message")


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


# --------------------------------------------------------------------------- #
# the guest as a PARTY — accepts todos, signs contracts, uploads files, all via
# the dashboard (a viewer token linked to her seat), no agent token anywhere
# --------------------------------------------------------------------------- #
def _spec():
    """A minimal valid http contract spec (mirrors tests/test_state._valid_spec)."""
    return {"version": 1, "endpoints": [{"method": "POST", "path": "/api/contact"}]}


def _task_guest_party(conn):
    """Two builders + a guest, all three bound to todo #1 (the guest still pending).

    The builders are seeded agents; the guest joins as a real seat with a linked viewer.
    Local mode, so the readiness gate on binding is skipped (see _assert_parties_ready).
    """
    created = admin.create_task(None, title="Ada site", roles=["backend", "frontend"])
    task = created["id"]
    be = Identity(agent_id=seed_agent(conn, task, "backend", "Bee", "sbk_be"),
                  task_id=task, name="Bee", role="backend")
    fe = Identity(agent_id=seed_agent(conn, task, "frontend", "Eff", "sbk_fe"),
                  task_id=task, name="Eff", role="frontend")
    g = admin.add_guest(task, "Ada")
    todos.propose_todo(conn, be, "Landing page", "hero + 3 sections",
                       ["backend", "frontend", g["seat"]])
    todos.accept_todo(conn, fe, 1)   # backend auto-accepted; guest still pending
    return task, g, be, fe


def test_guest_accepts_a_todo_via_the_dashboard(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    row = todos.get_row(conn, task, 1)

    # before: she is a party who has NOT accepted; the builders have
    before = todos.decisions(conn, row["id"], row["version"])
    assert before.get(g["seat"], {}).get("decision") != todos.ACCEPT

    r = asyncio.run(_endpoint(dbfile, "/guest/todo")(
        _Req(token=g["viewer_token"], body={"number": 1, "decision": "accept"})))
    assert r.status_code == 200

    # after: her acceptance is recorded against her seat, and every party now agrees
    after = todos.decisions(conn, row["id"], row["version"])
    assert after.get(g["seat"], {}).get("decision") == todos.ACCEPT
    assert all(after.get(p, {}).get("decision") == todos.ACCEPT for p in todos.parties_of(row))


def test_guest_signs_a_contract_via_the_dashboard(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    # the guest accepts the todo first (WHAT), so a contract (HOW) can be proposed
    asyncio.run(_endpoint(dbfile, "/guest/todo")(
        _Req(token=g["viewer_token"], body={"number": 1, "decision": "accept"})))

    state.propose_contract(conn, be, _spec(), 1)
    state.lock_contract(conn, be, 1, 1)   # backend signs
    state.lock_contract(conn, fe, 1, 1)   # frontend signs — still open, guest hasn't

    row = conn.execute("SELECT status FROM contracts WHERE task_id=? AND version=1", (task,)).fetchone()
    assert row["status"] != "locked"      # her signature is genuinely required

    r = asyncio.run(_endpoint(dbfile, "/guest/contract")(
        _Req(token=g["viewer_token"], body={"todo": 1, "version": 1, "decision": "sign"})))
    assert r.status_code == 200

    row = conn.execute("SELECT status FROM contracts WHERE task_id=? AND version=1", (task,)).fetchone()
    assert row["status"] == "locked"      # the guest's sign completed the quorum


def test_guest_signs_a_reproposed_contract_version(conn):
    # Renegotiation: v1 locks, someone reopens and proposes v2, and every party must sign
    # v2 again — the guest included. The dashboard used to key the "sign" button off
    # contract.default (the latest LOCKED version, still v1), so it hid v2 and the guest
    # could never re-lock. This pins the endpoint half: she CAN sign the newer version.
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    asyncio.run(_endpoint(dbfile, "/guest/todo")(
        _Req(token=g["viewer_token"], body={"number": 1, "decision": "accept"})))

    # v1: proposed and locked by all three (guest signs via her endpoint)
    state.propose_contract(conn, be, _spec(), 1)
    state.lock_contract(conn, be, 1, 1)
    state.lock_contract(conn, fe, 1, 1)
    asyncio.run(_endpoint(dbfile, "/guest/contract")(
        _Req(token=g["viewer_token"], body={"todo": 1, "version": 1, "decision": "sign"})))
    assert conn.execute("SELECT status FROM contracts WHERE task_id=? AND version=1",
                        (task,)).fetchone()["status"] == "locked"

    # reopen → propose v2 → builders re-sign; still open until the guest signs v2
    state.reopen_negotiations(conn, be, "the scope grew", 1)
    state.propose_contract(conn, be, _spec(), 1)
    state.lock_contract(conn, be, 2, 1)
    state.lock_contract(conn, fe, 2, 1)
    assert conn.execute("SELECT status FROM contracts WHERE task_id=? AND version=2",
                        (task,)).fetchone()["status"] != "locked"

    # the guest signs the RE-PROPOSED v2 through the same endpoint — and it locks
    r = asyncio.run(_endpoint(dbfile, "/guest/contract")(
        _Req(token=g["viewer_token"], body={"todo": 1, "version": 2, "decision": "sign"})))
    assert r.status_code == 200
    assert conn.execute("SELECT status FROM contracts WHERE task_id=? AND version=2",
                        (task,)).fetchone()["status"] == "locked"


def test_guest_uploads_a_file_via_the_dashboard(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    ep = _endpoint(dbfile, "/guest/file")
    png = b"\x89PNG\r\n\x1a\n" + b"fake-logo-bytes"

    r = asyncio.run(ep(_Req(token=g["viewer_token"], raw=png,
                            query={"name": "logo.png"}, headers={"content-type": "image/png"})))
    assert r.status_code == 201
    row = conn.execute("SELECT name, from_agent_id FROM files WHERE task_id=?", (task,)).fetchone()
    assert row["name"] == "logo.png"      # stored, stamped from the guest's seat


def test_guest_party_actions_are_forbidden_for_a_non_guest(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)
    for path, body in (
        ("/guest/todo", {"number": 1, "decision": "accept"}),
        ("/guest/contract", {"todo": 1, "version": 1, "decision": "sign"}),
    ):
        r = asyncio.run(_endpoint(dbfile, path)(_Req(token="sbv_hosttok", body=body)))
        assert r.status_code == 403


# --------------------------------------------------------------------------- #
# directing a guest message at specific people (the composer's "To" tick-boxes)
# --------------------------------------------------------------------------- #
def _got(msgs, text):
    """Did this note (by its unique body text) reach this reader? The party setup already
    generates baseline traffic, so we check for OUR note rather than an exact inbox count."""
    return any(text in (m.get("content") or "") for m in msgs)


def test_guest_directs_a_message_to_one_person(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    r = asyncio.run(_endpoint(dbfile, "/guest/message")(
        _Req(token=g["viewer_token"],
             body={"body": "logo the right one?", "to": ["backend"]})))
    assert r.status_code == 201
    assert _got(service.fetch_unacked(conn, be), "logo the right one")       # reaches backend
    assert not _got(service.fetch_unacked(conn, fe), "logo the right one")   # and NOT frontend


def test_guest_directs_a_message_to_several_people(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    r = asyncio.run(_endpoint(dbfile, "/guest/message")(
        _Req(token=g["viewer_token"],
             body={"body": "both of you please review", "to": ["backend", "frontend"]})))
    assert r.status_code == 201
    assert _got(service.fetch_unacked(conn, be), "both of you please review")
    assert _got(service.fetch_unacked(conn, fe), "both of you please review")


def test_guest_message_without_to_still_broadcasts(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    r = asyncio.run(_endpoint(dbfile, "/guest/message")(
        _Req(token=g["viewer_token"], body={"body": "hello everyone here"})))
    assert r.status_code == 201
    assert _got(service.fetch_unacked(conn, be), "hello everyone here")
    assert _got(service.fetch_unacked(conn, fe), "hello everyone here")


def test_guest_message_to_someone_off_the_task_is_rejected(conn):
    dbfile = get_config().db_path
    task, g, be, fe = _task_guest_party(conn)
    r = asyncio.run(_endpoint(dbfile, "/guest/message")(
        _Req(token=g["viewer_token"],
             body={"body": "hi", "to": ["backend", "nobody-here"]})))
    assert r.status_code == 400   # resolve_addressee refuses an addressee not on the cast
