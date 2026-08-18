"""The host surface — the writes reserved for the HOST's all-tasks viewer token.

The dashboard is read-only for everyone (D11); the guest seat gets the narrow ``/guest/*``
surface. The HOST — whoever holds the all-tasks viewer token (``viewers.task_id IS NULL``,
i.e. :attr:`ViewerIdentity.is_host`) — gets ``/host/*`` for the few things only the person
running the broker should be able to do from the board. Today that is ONE route: reissue a
guest's dashboard link.

Why it exists: a viewer token is stored only HASHED, so a lost guest link cannot be read
back. Rather than store the credential in plaintext — the one thing "sys-buddy stores no
credentials" forbids — the host regenerates a fresh link on demand (same guest seat, new
token; see :func:`admin.reissue_guest_link`). The link is built from the REQUEST's own
origin, so it automatically points at the URL the host is already reaching the board through
(the tunnel), which is exactly the URL the guest must use.
"""

from __future__ import annotations

from . import admin
from . import identity as _identity
from .api import _request_token


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
