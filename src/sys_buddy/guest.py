"""The guest write surface — the ONE narrow exception to the read-only dashboard.

A GUEST (concierge mode) is a non-technical human with no AI of their own: she joins
through a browser message box, not an agent on ``/mcp``. Everyone else who holds a
viewer token is strictly read-only (D11) — so a guest needs a way to WRITE that does
not open that door for the rest.

The seam: a guest's viewer token is LINKED to her seat (``viewers.agent_id``, set only
by :func:`admin.add_guest`). This route authenticates that viewer token exactly as the
dashboard does — the ``sb_view`` cookie — and then refuses any viewer whose linked
``agent_id`` is NULL, i.e. every host and buddy. So the read-only posture holds for
everyone but the guest, and this stays a separate ``/guest`` surface rather than a write
route under ``/api/*`` (which is GET-only by D11).

Two things make the write safe: the message identity is stamped SERVER-SIDE from the
linked seat (never from the request), and the type is hard-coded to ``note`` — a guest
types words, she does not choose a type, and she must not be able to forge a lifecycle
event like ``verified`` from her browser.
"""

from __future__ import annotations

import sqlite3

from . import identity as _identity
from . import service
from .api import _request_token
from .identity import Identity


def _guest_identity(conn: sqlite3.Connection, viewer) -> Identity | None:
    """The live seat a guest viewer may write as, or ``None``.

    Re-checked at USE time (as :func:`files._live_agent` re-checks a signed URL): a
    viewer token outlives the call that minted it, and a seat revoked in between must
    not still be able to write. A viewer with no linked seat (every host/buddy) returns
    ``None`` here — which is what keeps the dashboard read-only for everyone but a guest.
    """
    if viewer is None or viewer.agent_id is None:
        return None
    row = conn.execute(
        "SELECT id, task_id, name, role, handle FROM agents "
        "WHERE id = ? AND revoked_at IS NULL",
        (int(viewer.agent_id),),
    ).fetchone()
    if row is None:
        return None
    # A guest's viewer is scoped to exactly one task, and the linked seat must be on it.
    # Refuse a viewer with no task scope outright rather than letting a NULL task_id skip
    # the match — a host-scoped (all-tasks) credential must never resolve to a write seat.
    if viewer.task_id is None or row["task_id"] != viewer.task_id:
        return None
    return Identity(
        agent_id=row["id"],
        task_id=row["task_id"],
        name=row["name"],
        role=row["handle"] or row["role"],
        role_type=row["role"],
    )


def register_guest_routes(mcp, cfg) -> None:
    """Mount ``POST /guest/message`` — the guest's message box.

    Deliberately NOT under ``/api/*`` (GET-only, D11) and NOT on ``/mcp`` (agent tokens
    only). Its own surface, authenticated by a guest-linked VIEWER token, so the one
    write the dashboard grants reaches nothing else.
    """
    from starlette.responses import JSONResponse

    from .db import connect

    @mcp.custom_route("/guest/message", methods=["POST"])
    async def guest_message(request):
        conn = connect()
        try:
            viewer = _identity.resolve_viewer_token(conn, _request_token(request))
            ident = _guest_identity(conn, viewer)
            if ident is None:
                # One opaque answer whether the token is unknown, read-only, or revoked:
                # a viewer with no linked guest seat learns only that it cannot write.
                return JSONResponse({"error": "forbidden"}, status_code=403)
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            body = str((payload or {}).get("body", "") or "").strip()
            if not body:
                return JSONResponse({"error": "empty message"}, status_code=400)
            # Type is hard-coded, never taken from the request: `assert_sendable` is
            # belt-and-braces on that constant, and the guarantee is that a guest browser
            # can only ever author a conversational `note`, never a lifecycle event.
            service.assert_sendable("note")
            try:
                receipt = service.post_message(conn, ident, "note", body)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"ok": True, "id": receipt["id"]}, status_code=201)
        finally:
            conn.close()
