"""The host surface — the writes reserved for the HOST's all-tasks viewer token.

The dashboard is read-only for everyone (D11); the guest seat gets the narrow ``/guest/*``
surface. The HOST — whoever holds the all-tasks viewer token (``viewers.task_id IS NULL``,
i.e. :attr:`ViewerIdentity.is_host`) — gets ``/host/*`` for the few things only the person
running the broker should be able to do from the board:

* ``POST /host/guest-link`` — reissue a guest's dashboard link (same seat, new token);
* ``POST /host/invite`` — mint/reissue a single-use invite for one seat, returning both
  the join URL and the paste-able ``sb1_`` invite link;
* ``POST /host/extend-tokens`` — push back (or lift) the expiry on a task's live agent
  tokens, the recovery move for a session whose 24h tokens are about to lapse.

Every one of them is authenticated by the SAME :func:`_host` gate (host all-tasks token
only) and hands any other token one opaque 403, so the board stays read-only for buddies
and guests here too. None of these is an agent tool — a human typing a command cannot be
prompt-injected, which is the whole reason the abusable "eject/extend/invite" moves live
behind the host token rather than in the tool registry.

Why the guest-link one exists (and why the others build links the same way): a viewer or
invite credential is stored only HASHED, so a lost link cannot be read back. Rather than
store the credential in plaintext — the one thing "sys-buddy stores no credentials" forbids
— the host regenerates a fresh one on demand. Every link is built from the REQUEST's own
origin (:func:`_origin`), so it automatically points at the URL the host is already reaching
the board through (the tunnel), which is exactly the URL the peer must use.
"""

from __future__ import annotations

import time

from . import admin
from . import identity as _identity
from .api import _request_token
from .onboarding import make_invite_link, make_join_url


def _origin(request) -> str:
    """The public origin the host is viewing the board through — ``scheme://host`` — so a
    reissued link points at the same place (the tunnel), not at loopback. Honours a
    forwarding proxy's ``X-Forwarded-Proto``; assumes https for any non-loopback host, since
    a token must never ride a cleartext origin off the machine."""
    host = request.headers.get("host") or request.url.netloc
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if not proto:
        loopback = host.startswith(("localhost", "127.0.0.1"))
        proto = (request.url.scheme or "http") if loopback else "https"
    return f"{proto}://{host}"


def register_host_routes(mcp, cfg) -> None:
    """Mount the ``/host/*`` surface — authenticated by the all-tasks HOST viewer token."""
    from starlette.responses import JSONResponse

    from .db import connect

    async def _json(request) -> dict:
        try:
            return await request.json() or {}
        except Exception:
            return {}

    def _host(conn, request):
        """The viewer behind this request IFF it is the host's all-tasks token, else None."""
        viewer = _identity.resolve_viewer_token(conn, _request_token(request))
        return viewer if (viewer is not None and viewer.is_host) else None

    @mcp.custom_route("/host/guest-link", methods=["POST"])
    async def host_guest_link(request):
        """Reissue a fresh dashboard link for a guest on a task — same seat, new token.

        HOST viewer token ONLY: a buddy, a guest or an unknown token gets one opaque 403,
        so the board stays read-only for everyone but the host here too. Body is
        ``{"task": ..., "who": <seat handle or display name>}``."""
        conn = connect()
        try:
            if _host(conn, request) is None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            payload = await _json(request)
            task = str(payload.get("task", "") or "").strip()
            who = str(payload.get("who", "") or "").strip()
            if not task or not who:
                return JSONResponse(
                    {"error": "a task and a guest (who) are required"}, status_code=400
                )
            try:
                res = admin.reissue_guest_link(task, who)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            link = f"{_origin(request)}/ui?v={res['viewer_token']}"
            return JSONResponse(
                {"ok": True, "link": link, "seat": res["seat"], "name": res["name"]},
                status_code=201,
            )
        finally:
            conn.close()

    @mcp.custom_route("/host/invite", methods=["POST"])
    async def host_invite(request):
        """Mint a fresh single-use invite for ONE seat and hand back both link forms.

        The dashboard equivalent of ``sys-buddy invite``: a host who sees an unfilled
        seat on the roster (or a buddy whose original invite lapsed — invites live 30
        minutes) reissues one from the board instead of dropping back to a terminal.
        HOST viewer token ONLY; a buddy, guest or unknown token gets one opaque 403.

        Body ``{"task": ..., "role": <seat handle or role>}``. ``admin.mint_invite``
        resolves the role to exactly one seat (refusing an input that names two), so the
        seat we echo back is the one the code actually fills. Returns the join URL (what a
        human opens in a browser) AND the paste-able ``sb1_`` invite link (what an agent
        redeems), BOTH built from the request's own origin so they point at the host's
        tunnel exactly like ``/host/guest-link`` does — never at loopback.

        ``expires_at`` is the absolute epoch the code dies at, so the dashboard can count
        the 30-minute window down live; ``expires`` is the same instant as a human string.
        """
        conn = connect()
        try:
            if _host(conn, request) is None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
        finally:
            conn.close()
        payload = await _json(request)
        task = str(payload.get("task", "") or "").strip()
        role = str(payload.get("role", "") or "").strip()
        if not task or not role:
            return JSONResponse(
                {"error": "a task and a role (seat) are required"}, status_code=400
            )
        try:
            # Resolve the seat FIRST (this also refuses an ambiguous role), so a guest seat
            # can be rejected before any invite is written.
            seat = admin.seat_for(task, role)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # A guest has no agent — she joins through a dashboard link (guest-link), not an
        # agent invite. The roster hides Invite for guests; enforce it at the route too, so
        # a hand-rolled POST can't mint a meaningless agent invite for a viewer-only seat.
        if any(seat == g["seat"] for g in admin.list_guests(task)):
            return JSONResponse(
                {"error": "that seat is a guest — reissue a guest link, not an invite"},
                status_code=400,
            )
        try:
            # `admin.mint_invite` writes the invite and returns the raw code + the human
            # expiry. Only the code's sha256 is stored; the raw code rides back out once,
            # packed into the links below and never again.
            code, expires = admin.mint_invite(task, role)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        origin = _origin(request)
        # The window mint_invite just wrote (now + INVITE_TTL_SECONDS). Recomputed here to
        # a few ms rather than re-parsing the human string, so the client counts down from
        # an honest absolute instant.
        expires_at = time.time() + admin.INVITE_TTL_SECONDS
        return JSONResponse(
            {
                "ok": True,
                "seat": seat,
                "join_url": make_join_url(origin, code),
                "invite_link": make_invite_link(origin, code),
                "expires": expires,
                "expires_at": expires_at,
            },
            status_code=201,
        )

    @mcp.custom_route("/host/extend-tokens", methods=["POST"])
    async def host_extend_tokens(request):
        """Push back (or lift) the expiry on every live agent token on a task.

        The recovery move for the 24h-TTL trap (see :func:`admin.extend_agent_tokens`): an
        agent whose token has expired cannot rotate or even escalate, and the only fixes
        used to be a full re-pair or a hand-edit of the db. A host does it from the board
        here. HOST viewer token ONLY; anyone else gets one opaque 403.

        Body ``{"task": ..., "hours": <number, optional>, "never": <bool, optional>}``.
        ``never`` clears the expiry outright (a long session or demo); otherwise ``hours``
        (default 24) sets the new window. Only LIVE seats are touched — a revoked seat
        stays revoked, so "extend" can never quietly re-admit somebody a host cut off.

        Returns ``{ok, task, never, hours, count, touched}`` — ``touched`` is one row per
        seat moved (seat, name, was, now, was_expired) so the board can report exactly who
        was extended, not a bare count.
        """
        conn = connect()
        try:
            if _host(conn, request) is None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
        finally:
            conn.close()
        payload = await _json(request)
        task = str(payload.get("task", "") or "").strip()
        if not task:
            return JSONResponse({"error": "a task is required"}, status_code=400)
        never = bool(payload.get("never"))
        hours_raw = payload.get("hours")
        try:
            # Default 24h — the same TTL a tunnelled broker mints — when the caller does
            # not name one. A non-numeric hours is a client bug, not a silent 24h.
            hours = 24.0 if hours_raw in (None, "") else float(hours_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "hours must be a number"}, status_code=400)
        if not never and hours <= 0:
            return JSONResponse({"error": "hours must be positive"}, status_code=400)
        try:
            touched = admin.extend_agent_tokens(task, hours=hours, never=never)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "task": task,
                "never": never,
                "hours": None if never else hours,
                "count": len(touched),
                "touched": touched,
            },
            status_code=201,
        )
