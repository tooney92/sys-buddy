"""Specs for the HOST-only ``/host/*`` write surface.

The dashboard is read-only for everyone (D11); the HOST — whoever holds the all-tasks
viewer token (``viewers.task_id IS NULL``) — is the one exception, and only for the few
management moves that must not be an agent tool (a human typing cannot be prompt-injected).
Every route reuses the SAME ``_host`` gate, so the two properties that carry this file are:

* **the host CAN act** — ``/host/invite`` mints a fresh invite for a seat, and
  ``/host/extend-tokens`` pushes back the live agent tokens on a task;
* **nobody else can** — a buddy, a guest and an unknown token each get one opaque 403,
  so the board stays read-only for everyone but the host here too.
"""

from __future__ import annotations

import asyncio
import json
import time

from sys_buddy import admin, onboarding, seats
from sys_buddy.config import Config, get_config
from sys_buddy.server import build_server
from tests.conftest import seed_agent, seed_task, seed_viewer


# --------------------------------------------------------------------------- #
# a request double carrying the slice the /host handlers read: a viewer token
# (query/header), a JSON body, and a URL/host so _origin can build a link.
# --------------------------------------------------------------------------- #
class _URL:
    def __init__(self, scheme="http", netloc="127.0.0.1:9292"):
        self.scheme = scheme
        self.netloc = netloc


class _Req:
    def __init__(self, token=None, body=None, host="127.0.0.1:9292", scheme="http"):
        self.query_params = {"v": token} if token else {}
        self.headers = {"host": host} if host else {}
        self.cookies = {}
        self.url = _URL(scheme=scheme, netloc=host or "")
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _route(dbfile, path):
    """The registered POST endpoint for ``path`` on a freshly built server."""
    mcp = build_server(Config(mode="local", db_path=dbfile))
    routes = [
        r for r in getattr(mcp, "_additional_http_routes", [])
        if str(getattr(r, "path", "")) == path
    ]
    assert routes, f"expected POST {path} to be registered"
    for r in routes:
        assert set(r.methods) <= {"POST"}, f"{r.path} accepts {r.methods}"
    return routes[0].endpoint


def _call(dbfile, path, req):
    return asyncio.run(_route(dbfile, path)(req))


def _body(resp):
    return json.loads(resp.body)


# --------------------------------------------------------------------------- #
# Task 1 — the invite TTL is now 30 minutes
# --------------------------------------------------------------------------- #
def test_invite_ttl_is_thirty_minutes():
    assert admin.INVITE_TTL_SECONDS == 30 * 60


def test_mint_invite_expiry_is_thirty_minutes_out(conn):
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    before = time.time()
    admin.mint_invite("checkout-api", "mobile")
    row = conn.execute(
        "SELECT expires_at, created_at FROM invites WHERE task_id = 'checkout-api'"
    ).fetchone()
    # ~30 minutes after it was written (allow a second of slack for the call itself).
    assert abs(row["expires_at"] - row["created_at"] - 30 * 60) < 1
    assert row["expires_at"] > before + 29 * 60


# --------------------------------------------------------------------------- #
# Task 3 — the roster surfaces a pending invite's absolute expiry
# --------------------------------------------------------------------------- #
def test_roster_surfaces_invite_expires_at_for_a_pending_seat(conn):
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    admin.mint_invite("checkout-api", "mobile")
    rows = {r["seat"]: r for r in seats.roster(conn, "checkout-api")}
    mobile = rows["mobile"]
    assert mobile["invite_pending"] is True
    assert mobile["invite_expires_at"] is not None
    assert mobile["invite_expires_at"] > time.time()


def test_roster_has_no_expiry_for_a_seat_with_no_invite(conn):
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    rows = {r["seat"]: r for r in seats.roster(conn, "checkout-api")}
    assert rows["mobile"]["invite_pending"] is False
    assert rows["mobile"]["invite_expires_at"] is None


def test_roster_drops_the_expiry_once_the_invite_is_used(conn):
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    admin.mint_invite("checkout-api", "mobile")
    conn.execute("UPDATE invites SET used_at = ? WHERE task_id = 'checkout-api'", (time.time(),))
    conn.commit()
    rows = {r["seat"]: r for r in seats.roster(conn, "checkout-api")}
    assert rows["mobile"]["invite_pending"] is False
    assert rows["mobile"]["invite_expires_at"] is None


# --------------------------------------------------------------------------- #
# Task 4 — POST /host/invite
# --------------------------------------------------------------------------- #
def test_host_invite_mints_a_link_for_the_host(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)

    resp = _call(dbfile, "/host/invite",
                 _Req(token="sbv_hosttok", body={"task": "checkout-api", "role": "mobile"}))
    assert resp.status_code == 201
    j = _body(resp)
    assert j["ok"] is True
    assert j["seat"] == "mobile"
    # The links point at the request's own origin (the tunnel), like /host/guest-link.
    assert j["join_url"].startswith("http://127.0.0.1:9292/join#c=")
    assert j["invite_link"].startswith(onboarding.INVITE_PREFIX)
    # The absolute expiry the client counts down from — the 30-minute window.
    assert j["expires_at"] > time.time() + 29 * 60
    # And it actually wrote an invite for that seat.
    row = conn.execute(
        "SELECT role FROM invites WHERE task_id = 'checkout-api' AND used_at IS NULL"
    ).fetchone()
    assert row["role"] == "mobile"


def test_host_invite_accepts_a_never_invited_seat(conn):
    """The board now offers [Invite] on a seat that was NEVER invited, not only [Reissue]
    on one with a pending invite. The SAME route serves both — `mint_invite` works whether
    or not a prior invite existed — so an unfilled seat with no invite history mints cleanly
    and the roster then reports it as pending."""
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)

    # No invite exists for `mobile` yet — the never-invited state.
    before = {r["seat"]: r for r in seats.roster(conn, "checkout-api")}
    assert before["mobile"]["invite_pending"] is False
    assert before["mobile"]["invite_expires_at"] is None

    resp = _call(dbfile, "/host/invite",
                 _Req(token="sbv_hosttok", body={"task": "checkout-api", "role": "mobile"}))
    assert resp.status_code == 201
    assert _body(resp)["seat"] == "mobile"

    after = {r["seat"]: r for r in seats.roster(conn, "checkout-api")}
    assert after["mobile"]["invite_pending"] is True
    assert after["mobile"]["invite_expires_at"] > time.time()


def test_host_invite_forbids_non_host_on_a_never_invited_seat(conn):
    """The read-only guarantee (D11) holds for the [Invite] trigger exactly as it does for
    [Reissue]: a buddy or unknown token cannot mint on a never-invited seat either."""
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_viewer(conn, "a-buddy", "sbv_buddytok", task_id="checkout-api")

    for tok in ("sbv_buddytok", "sbv_nope", None):
        resp = _call(dbfile, "/host/invite",
                     _Req(token=tok, body={"task": "checkout-api", "role": "mobile"}))
        assert resp.status_code == 403, f"token {tok!r} should be forbidden"
        assert _body(resp) == {"error": "forbidden"}
    # …and nothing was written for the seat.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM invites WHERE task_id = 'checkout-api'"
    ).fetchone()["n"] == 0


def test_host_invite_link_round_trips_to_the_origin_and_code(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)

    resp = _call(dbfile, "/host/invite",
                 _Req(token="sbv_hosttok", host="upbeat.ngrok-free.dev", scheme="https",
                      body={"task": "checkout-api", "role": "mobile"}))
    j = _body(resp)
    base_url, code = onboarding.parse_invite_link(j["invite_link"])
    assert base_url == "https://upbeat.ngrok-free.dev"
    assert j["join_url"] == onboarding.make_join_url("https://upbeat.ngrok-free.dev", code)


def test_host_invite_forbids_buddy_guest_and_unknown(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    # a real GUEST viewer (linked to a seat), a per-task BUDDY viewer, an unknown token,
    # and no token at all → every one gets one opaque 403.
    guest_tok = admin.add_guest("checkout-api", "Sam")["viewer_token"]
    seed_viewer(conn, "a-buddy", "sbv_buddytok", task_id="checkout-api")

    for tok in ("sbv_buddytok", guest_tok, "sbv_nope", None):
        resp = _call(dbfile, "/host/invite",
                     _Req(token=tok, body={"task": "checkout-api", "role": "mobile"}))
        assert resp.status_code == 403, f"token {tok!r} should be forbidden"
        assert _body(resp) == {"error": "forbidden"}


def test_host_invite_requires_task_and_role(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)
    resp = _call(dbfile, "/host/invite",
                 _Req(token="sbv_hosttok", body={"task": "checkout-api"}))
    assert resp.status_code == 400


def test_host_invite_reports_an_unknown_seat(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)
    resp = _call(dbfile, "/host/invite",
                 _Req(token="sbv_hosttok", body={"task": "checkout-api", "role": "nobody"}))
    assert resp.status_code == 400
    assert "error" in _body(resp)


# --------------------------------------------------------------------------- #
# Task 5 — POST /host/extend-tokens
# --------------------------------------------------------------------------- #
def test_host_extend_tokens_pushes_back_the_expiry(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_agent(conn, "checkout-api", "backend", "Kola", "sbk_be")
    # a token that has already lapsed — the case the feature exists for
    conn.execute("UPDATE agents SET expires_at = ? WHERE name = 'Kola'", (time.time() - 10,))
    conn.commit()
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)

    resp = _call(dbfile, "/host/extend-tokens",
                 _Req(token="sbv_hosttok", body={"task": "checkout-api", "hours": 24}))
    assert resp.status_code == 201
    j = _body(resp)
    assert j["ok"] is True and j["count"] == 1
    assert j["touched"][0]["seat"] == "backend"
    assert j["touched"][0]["was_expired"] is True
    new_exp = conn.execute(
        "SELECT expires_at FROM agents WHERE name = 'Kola'"
    ).fetchone()["expires_at"]
    assert new_exp > time.time() + 23 * 3600


def test_host_extend_tokens_never_clears_the_expiry(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_agent(conn, "checkout-api", "backend", "Kola", "sbk_be")
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)

    resp = _call(dbfile, "/host/extend-tokens",
                 _Req(token="sbv_hosttok", body={"task": "checkout-api", "never": True}))
    assert resp.status_code == 201
    assert _body(resp)["never"] is True
    exp = conn.execute("SELECT expires_at FROM agents WHERE name = 'Kola'").fetchone()["expires_at"]
    assert exp is None


def test_host_extend_tokens_forbids_non_host(conn):
    dbfile = get_config().db_path
    seed_task(conn, "checkout-api", roles=("backend", "mobile"))
    seed_agent(conn, "checkout-api", "backend", "Kola", "sbk_be")
    seed_viewer(conn, "a-buddy", "sbv_buddytok", task_id="checkout-api")

    for tok in ("sbv_buddytok", "sbv_nope", None):
        resp = _call(dbfile, "/host/extend-tokens",
                     _Req(token=tok, body={"task": "checkout-api"}))
        assert resp.status_code == 403, f"token {tok!r} should be forbidden"


def test_host_extend_tokens_requires_a_task(conn):
    dbfile = get_config().db_path
    seed_viewer(conn, "host", "sbv_hosttok", task_id=None)
    resp = _call(dbfile, "/host/extend-tokens", _Req(token="sbv_hosttok", body={}))
    assert resp.status_code == 400
