"""The guest surface — the deliberately narrow set of writes the read-only dashboard
grants to ONE seat: a guest (concierge mode).

A GUEST is a non-technical human with no AI of their own. She holds no agent token and
never calls ``/mcp``; her whole credential is a VIEWER token that is LINKED to her seat
(``viewers.agent_id``, set when she redeems her invite). Every route here authenticates
that link and then acts as her seat — the broker builds her ``Identity`` from the linked
``agents`` row server-side, so she is a first-class signatory (a real ``agent_id``) who
simply proves herself by a different door than an agent does.

What she may do, and nothing more:

    POST /guest/message   send a note into the thread
    POST /guest/file      share a file (design, screenshot, PDF, zip) — DATA, per Rule 4
    POST /guest/todo      accept / decline a todo she is a party to
    POST /guest/contract  sign / push back a contract she is a party to

Every action is stamped SERVER-SIDE from her seat (never from the request), and each
domain call re-checks that she is actually a party (``assert_party``) — so a guest who is
not on a todo gets the same refusal any non-party would. This is a separate ``/guest``
surface, never a write route under ``/api/*`` (GET-only, D11); a viewer with no linked
seat — every host and buddy — is refused here, which is what keeps the dashboard
read-only for everyone but the guest.
"""

from __future__ import annotations

import sqlite3

from . import files, service, state, todos
from . import identity as _identity
from .api import _request_token
from .identity import Identity


def _guest_identity(conn: sqlite3.Connection, viewer) -> Identity | None:
    """The live seat a guest viewer may act as, or ``None``.

    Re-checked at USE time (as :func:`files._live_agent` re-checks a signed URL): a viewer
    token outlives the call that minted it, and a seat revoked in between must not still be
    able to act. A viewer with no linked seat (every host/buddy) returns ``None`` — which is
    what keeps the dashboard read-only for everyone but a guest.
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


def _authed_guest(conn: sqlite3.Connection, request) -> Identity | None:
    """Resolve the guest identity behind this request's viewer token, or ``None``."""
    viewer = _identity.resolve_viewer_token(conn, _request_token(request))
    return _guest_identity(conn, viewer)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def register_guest_routes(mcp, cfg) -> None:
    """Mount the ``/guest/*`` write surface — authenticated by a guest-linked VIEWER token,
    so the writes the dashboard grants reach nothing else."""
    from starlette.responses import JSONResponse

    from .db import connect

    async def _json(request) -> dict:
        try:
            return await request.json() or {}
        except Exception:
            return {}

    def _forbidden():
        # One opaque answer whether the token is unknown, read-only, or revoked: a viewer
        # with no linked guest seat learns only that it cannot act here.
        return JSONResponse({"error": "forbidden"}, status_code=403)

    @mcp.custom_route("/guest/message", methods=["POST"])
    async def guest_message(request):
        conn = connect()
        try:
            ident = _authed_guest(conn, request)
            if ident is None:
                return _forbidden()
            payload = await _json(request)
            body = str(payload.get("body", "") or "").strip()
            if not body:
                return JSONResponse({"error": "empty message"}, status_code=400)
            # Optional directed recipients — the seats she ticked in the composer. Absent or
            # empty means broadcast to everyone (the unchanged default). Each is validated
            # against the task's cast inside `post_message` (resolve_addressee), so a name
            # that is not on this task comes back a 400 rather than reaching anyone.
            raw_to = payload.get("to")
            to_roles = (
                [str(x) for x in raw_to if str(x).strip()]
                if isinstance(raw_to, list) else None
            )
            # Type is hard-coded, never taken from the request: a guest can only ever author
            # a conversational `note`, never a lifecycle event like `verified`.
            service.assert_sendable("note")
            try:
                receipt = service.post_message(conn, ident, "note", body, to_roles=to_roles)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"ok": True, "id": receipt["id"]}, status_code=201)
        finally:
            conn.close()

    @mcp.custom_route("/guest/file", methods=["POST"])
    async def guest_file(request):
        """Share a file as the guest. Raw bytes in the body (same shape as the agent
        ``/files`` route, minus multipart), ``?name=`` for the filename, Content-Type for
        the kind. It is DATA on the shared-file path — a peer's agent inspects it, never
        runs it (Rule 4) — with the same type/size validation everyone else gets."""
        conn = connect()
        try:
            ident = _authed_guest(conn, request)
            if ident is None:
                return _forbidden()
            data = await request.body()
            if not data:
                return JSONResponse({"error": "empty file"}, status_code=400)
            name = request.query_params.get("name") or "upload"
            content_type = (request.headers.get("content-type", "") or "").split(";")[0].strip()
            try:
                rec = files.upload_file(conn, ident, name, data, content_type)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse(
                {"ok": True, "id": rec.get("id") if isinstance(rec, dict) else None},
                status_code=201,
            )
        finally:
            conn.close()

    @mcp.custom_route("/guest/todo", methods=["POST"])
    async def guest_todo(request):
        """Accept or decline a todo the guest is a party to. `assert_party` inside the
        domain call is what refuses a guest who is not on this todo."""
        conn = connect()
        try:
            ident = _authed_guest(conn, request)
            if ident is None:
                return _forbidden()
            payload = await _json(request)
            number = _as_int(payload.get("number"))
            decision = str(payload.get("decision", "")).strip().lower()
            if number is None:
                return JSONResponse({"error": "a todo number is required"}, status_code=400)
            try:
                if decision == "accept":
                    todos.accept_todo(conn, ident, number)
                elif decision == "decline":
                    reason = str(payload.get("reason", "") or "").strip()
                    if not reason:
                        return JSONResponse(
                            {"error": "a reason is required to decline"}, status_code=400
                        )
                    todos.decline_todo(conn, ident, number, reason)
                else:
                    return JSONResponse(
                        {"error": "decision must be 'accept' or 'decline'"}, status_code=400
                    )
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"ok": True}, status_code=200)
        finally:
            conn.close()

    @mcp.custom_route("/guest/contract", methods=["POST"])
    async def guest_contract(request):
        """Sign (lock) or push back (decline) a contract the guest is a party to. Signing
        records her signature; the broker locks only when every party has signed — the
        guest counts in that quorum exactly like a builder."""
        conn = connect()
        try:
            ident = _authed_guest(conn, request)
            if ident is None:
                return _forbidden()
            payload = await _json(request)
            todo_no = _as_int(payload.get("todo"))
            decision = str(payload.get("decision", "")).strip().lower()
            try:
                if decision == "sign":
                    version = _as_int(payload.get("version"))
                    if version is None:
                        return JSONResponse(
                            {"error": "a contract version is required"}, status_code=400
                        )
                    state.lock_contract(conn, ident, version, todo_no)
                elif decision == "decline":
                    reason = str(payload.get("reason", "") or "").strip()
                    if not reason:
                        return JSONResponse(
                            {"error": "a reason is required to push back"}, status_code=400
                        )
                    state.decline_contract(conn, ident, reason, todo_no)
                else:
                    return JSONResponse(
                        {"error": "decision must be 'sign' or 'decline'"}, status_code=400
                    )
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"ok": True}, status_code=200)
        finally:
            conn.close()
