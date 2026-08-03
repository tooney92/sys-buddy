"""File sharing on a task — design bundles (zip), screenshots (png/jpg), HTML, PDFs. No video.

Two ways in, one way out:

* ``POST /files/{task_id}`` — raw bytes, the agent's own bearer token, task id in the path.
  This is the one that should be used; see ``register_upload_route`` for why base64 through a
  tool argument is so expensive.
* the MCP ``upload_file`` tool — base64 in an argument, for an agent with no shell.

Out is always ``get_file`` / ``GET /api/file/{id}``, never a URL from chat — the same
invariant that limits an agent to the signed ``staging_url``. An uploaded file is DATA: a
consumer inspects/extracts it, never runs it (Rules of Engagement). The broker only stores
and serves bytes; it does NOT unpack archives, so there is no server-side zip-bomb or
path-traversal surface — safe extraction is the consumer's job, just as interpreting a peer
message is. HTML is stored but never served renderable — see ``api._file_response``.

Bytes live in the row for v1 (a blob). If the db gets heavy, move to an on-disk ``files/``
dir with a path column; the public functions here don't change shape.
"""

from __future__ import annotations

import re
import time

from . import service
from .identity import resolve_agent_token
from .service import Identity

# 8 MB — fits a design zip / screenshot / PDF while staying bounded. NOTE for the tool/api
# layers: an upload arrives base64-encoded (~1.34x), and the HTTP body limit
# (``http_middleware``) is ~1 MiB, so the UPLOAD path must raise its limit to accept this.
MAX_FILE_BYTES = 8 * 1024 * 1024

# The only content types accepted. Video is deliberately excluded for now. The value is the
# default ``kind`` bucket for that type when the caller doesn't specify one.
#
# ``text/html`` is here so a dev can share a rendered report, an exported page or a
# self-contained mockup. It is the one type on this list that a BROWSER would execute, so it
# is never served renderable from the broker origin — ``api._file_response`` forces it to
# download and pins a no-op CSP on it. See the comment there; that is the entire reason HTML
# is safe to accept, and moving it back to ``inline`` would be stored XSS against a page that
# holds a viewer token.
ALLOWED_TYPES = {
    "image/png": "screenshot",
    "image/jpeg": "screenshot",
    "application/pdf": "other",
    "application/zip": "design",
    "text/html": "other",
}
KINDS = frozenset({"screenshot", "design", "other"})


def _kind_for(content_type: str, kind: str | None) -> str:
    """Honour an explicit kind if valid; otherwise bucket by content type."""
    if kind in KINDS:
        return kind
    return ALLOWED_TYPES.get(content_type, "other")


def upload_file(
    conn, identity: Identity, name: str, data: bytes,
    content_type: str, kind: str | None = None,
) -> dict:
    """Store a file on ``identity``'s task. ``data`` is raw bytes (the tool layer
    base64-decodes before calling). Returns a small receipt (no bytes).

    Rejects — with a clear, agent-readable ValueError — an unsupported type, an empty or
    oversized file, or a missing name; and refuses a closed/unknown task.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a file needs a name")
    if content_type not in ALLOWED_TYPES:
        # Listed FROM the allow-list, not spelled out beside it. The hand-written version of
        # this sentence said "PNG/JPG images, PDF, and ZIP" and stayed that way when
        # ``text/html`` was added — so the broker was refusing a type it accepted while
        # telling the agent that type did not exist.
        raise ValueError(
            f"unsupported file type '{content_type}'. Allowed: "
            f"{', '.join(sorted(ALLOWED_TYPES))} — no video."
        )
    if not data:
        raise ValueError("empty file — nothing to upload")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(
            f"file is ~{len(data) // 1024 // 1024} MB, over the "
            f"{MAX_FILE_BYTES // 1024 // 1024} MB limit"
        )

    task = conn.execute(
        "SELECT closed_at FROM tasks WHERE id = ?", (identity.task_id,)
    ).fetchone()
    if task is None:
        raise ValueError(f"unknown task '{identity.task_id}'")
    if task["closed_at"] is not None:
        raise ValueError(f"task '{identity.task_id}' is closed")

    resolved_kind = _kind_for(content_type, kind)
    now = time.time()
    cur = conn.execute(
        "INSERT INTO files (task_id, from_agent_id, name, kind, content_type, size, data, "
        "created_at) VALUES (?,?,?,?,?,?,?,?)",
        (identity.task_id, identity.agent_id, name, resolved_kind, content_type,
         len(data), data, now),
    )
    conn.commit()
    return {
        "id": cur.lastrowid, "name": name, "kind": resolved_kind,
        "content_type": content_type, "size": len(data),
    }


def list_files(conn, task_id: str) -> list[dict]:
    """Metadata for every file on the task (NO bytes), oldest first, with uploader role."""
    rows = conn.execute(
        "SELECT f.id, f.name, f.kind, f.content_type, f.size, f.created_at, a.role AS role "
        "FROM files f JOIN agents a ON a.id = f.from_agent_id "
        "WHERE f.task_id = ? ORDER BY f.id",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_file(conn, task_id: str, file_id: int) -> dict | None:
    """One file's bytes + metadata, scoped to ``task_id`` (so a token for task A can never
    read task B's file). None if there is no such file on this task."""
    r = conn.execute(
        "SELECT id, name, kind, content_type, size, data FROM files "
        "WHERE id = ? AND task_id = ?",
        (int(file_id), task_id),
    ).fetchone()
    if r is None:
        return None
    return {
        "id": r["id"], "name": r["name"], "kind": r["kind"],
        "content_type": r["content_type"], "size": r["size"], "data": r["data"],
    }


def safe_filename(name: str) -> str:
    """A filename safe to drop into a quoted ``Content-Disposition``. The suggested download
    name is cosmetic — the file is identified by id — so we simply strip the characters that
    would break the header or inject one (double-quote, backslash, CR/LF), never trusting the
    stored name to be header-clean."""
    return re.sub(r'[\r\n"\\]', "", name or "").strip() or "download"


def file_response(f: dict):
    """A file's raw bytes as an HTTP response, hardened the same way for EVERY caller.

    Both byte-serving routes come through here — the dashboard's ``GET /api/file/{id}`` under
    a viewer token, and an agent's ``GET /files/{task}/{id}`` under its own. One
    implementation on purpose: the HTML rule below is a security property, and a second copy
    of it is a second chance to get it wrong.

    Images render ``inline`` so the dashboard can point an ``<img src>`` at the URL;
    everything else (zip/pdf/html) is an ``attachment`` so the browser downloads it under its
    original name.

    ONLY images may be ``inline``, and the allow-list is positive for a reason. ``text/html``
    is an accepted upload type, and an HTML file rendered inline would execute on the broker's
    OWN origin — the origin that serves the dashboard and holds the viewer cookie. That is
    stored XSS with a credential attached, uploadable by any agent on the task. Two things
    stop it, both here: the disposition forces a download, and the CSP neuters the document if
    anything ever manages to render it anyway. ``X-Content-Type-Options: nosniff`` (set
    globally by SecurityHeadersMiddleware) is the third leg — without it a browser could sniff
    HTML out of a file uploaded as something else.
    """
    from starlette.responses import Response

    if f["content_type"].startswith("image/"):
        disposition = "inline"
    else:
        disposition = f'attachment; filename="{safe_filename(f["name"])}"'
    return Response(
        content=f["data"],
        media_type=f["content_type"],
        headers={
            "Content-Disposition": disposition,
            # Belt to the disposition's braces: nothing loads, nothing executes, nothing
            # phones home, even if this document somehow gets rendered as a page.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


# --------------------------------------------------------------------------- #
# the raw-bytes routes
# --------------------------------------------------------------------------- #
# WHY THESE EXIST. The MCP `upload_file` tool carries the file as a base64 STRING argument,
# which means the uploading agent has to *generate* the whole encoding token by token. That
# is the entire cost of sharing a file: storing a 328 KB screenshot takes the broker ~1.3 ms,
# and encoding it into a tool call costs the agent ~128,000 tokens. At the 8 MB cap it is
# ~3.2M tokens — more than a context window, so the documented limit was unreachable through
# the only path that existed.
#
# `get_file` has exactly the same disease pointing the other way: it returns the bytes as
# `content_base64`, so the READING agent pays the same ~128,000 tokens to pull a screenshot
# INTO its context — and a file it cannot fit is a file it cannot read at all. Fixing only
# the upload half would have left every shared file writable-but-unreadable at size.
#
# So bytes get their own door in both directions. An agent POSTs with `curl --data-binary`
# and GETs with `curl -o`, and nothing larger than a receipt passes through a model. Raw body
# rather than multipart on the way in: multipart needs `python-multipart` and gives an agent
# one more thing to get wrong, whereas `--data-binary @file` is a single obvious line.
#
# THE TASK ID IS IN THE PATH, and that is the point of the design rather than decoration.
# The token says who you are; the path says which task you MEANT. When they disagree the
# broker refuses (403) instead of quietly following the token — so a misconfigured agent
# holding task A's token cannot land a file on task B, and cannot land it on A "by accident"
# either. Same rule the contract tools follow with `todo=N`: name the target, and be told
# when you named the wrong one. It holds on the way OUT too, where it is also the scoping
# rule: an agent may only read files on the task its own token belongs to.
def _bare_content_type(header: str) -> str:
    """``text/html; charset=utf-8`` → ``text/html``. Callers set charsets and boundaries and
    are not wrong to; the allow-list is about the type, not its parameters."""
    return (header or "").split(";")[0].strip().lower()


def _agent_for(conn, *, is_remote: bool, task_id: str, token: str, agent: str, verb: str):
    """Resolve the acting agent for a ``/files`` request, or ``(body, status)`` to return.

    Shared by both directions so the task-match rule is written ONCE — the guarantee is only
    worth as much as its least careful copy. Answers ``(Identity, None)`` on success and
    ``(None, (body, status))`` on refusal.
    """
    if is_remote:
        ident = resolve_agent_token(conn, token)
        if ident is None:
            return None, ({"error": "unauthorized"}, 401)
        # THE refusal that makes the path meaningful. Not a 404: the caller is a known agent
        # asking for something it may not do, and saying so plainly is what lets it fix its
        # own configuration instead of silently acting somewhere it did not intend.
        if ident.task_id != task_id:
            return None, ({
                "error": f"your token belongs to task '{ident.task_id}', not '{task_id}' — "
                         f"{verb} your own task"
            }, 403)
        return ident, None

    # Local mode has no tokens at all (the middleware auth gate is remote-only), so the seat
    # is NAMED, exactly as every local MCP tool names it.
    if not agent.strip():
        return None, ({"error": "local mode: name your seat with ?agent=<your agent name>"}, 400)
    try:
        return service.ensure_local_identity(conn, task_id, agent.strip()), None
    except ValueError as e:
        return None, ({"error": str(e)}, 400)


def handle_upload(
    conn, *, is_remote: bool, task_id: str, token: str = "", agent: str = "",
    name: str = "", kind: str | None = None, content_type: str = "", data: bytes = b"",
) -> tuple[dict, int]:
    """The whole upload decision — auth, task match, store — as ``(body, status)``.

    Separated from the route so it is testable on an open connection with no HTTP server,
    the way every other helper in this package is, and so the route below stays a thin
    adapter that only knows how to read a Request.
    """
    ident, refusal = _agent_for(
        conn, is_remote=is_remote, task_id=task_id, token=token, agent=agent,
        verb="upload to",
    )
    if refusal is not None:
        return refusal

    try:
        return upload_file(conn, ident, name, data, content_type, kind), 201
    except ValueError as e:
        # Every rejection `upload_file` raises is the caller's to fix — unsupported type,
        # empty body, over the cap, no name, closed task — so 400 with the message verbatim,
        # which is already written to be read by an agent.
        return {"error": str(e)}, 400


def handle_download(
    conn, *, is_remote: bool, task_id: str, file_id, token: str = "", agent: str = "",
) -> tuple[dict, int] | dict:
    """Resolve one file's bytes for an AGENT, or ``(body, status)`` to refuse.

    Returns the file record on success (the caller turns it into a response) and an error
    tuple otherwise, so the auth decision stays testable without HTTP.

    A file that does not exist and a file on another task are both **404**, deliberately
    indistinguishable — the same rule ``api._file_for`` follows for viewers, and for the same
    reason: a distinguishable 403 would let an agent probe another task's file ids. Note the
    two 404 paths cannot even be reached from different tasks here, because the task match
    above has already refused a foreign task outright; the id scoping is the second lock on
    the same door.
    """
    ident, refusal = _agent_for(
        conn, is_remote=is_remote, task_id=task_id, token=token, agent=agent,
        verb="read files on",
    )
    if refusal is not None:
        return refusal
    try:
        fid = int(file_id)
    except (TypeError, ValueError):
        return {"error": "not found"}, 404
    rec = get_file(conn, ident.task_id, fid)
    if rec is None:
        return {"error": "not found"}, 404
    return rec


def register_upload_route(mcp, cfg) -> None:
    """Mount the two ``/files`` routes — the only ones that move raw bytes for an AGENT.

    Deliberately NOT under ``/api/*``: that surface is GET-only by D11 and, more to the point,
    authenticates VIEWERS. These are the agent side of the house, authenticated the way
    ``/mcp`` is — with the agent's own bearer token. A viewer must not be able to write files
    into a task (every buddy is issued one when they pair), and an agent should not have to
    borrow a human's credential to read one.

        POST /files/{task_id}            store bytes, receipt back
        GET  /files/{task_id}/{file_id}  read bytes back

    The dashboard keeps its own reader at ``GET /api/file/{id}`` under a viewer token; both
    serve through ``file_response`` so the HTML hardening is written once.
    """
    from starlette.responses import JSONResponse

    from .db import connect

    def _token(request) -> str:
        auth = request.headers.get("authorization", "")
        return auth[7:].strip() if auth[:7].lower() == "bearer " else ""

    @mcp.custom_route("/files/{task_id}", methods=["POST"])
    async def upload(request):
        conn = connect()
        try:
            body, status = handle_upload(
                conn,
                is_remote=cfg.is_remote,
                task_id=request.path_params["task_id"],
                token=_token(request),
                agent=request.query_params.get("agent", ""),
                name=request.query_params.get("name", ""),
                kind=request.query_params.get("kind") or None,
                content_type=_bare_content_type(request.headers.get("content-type", "")),
                data=await request.body(),
            )
            return JSONResponse(body, status_code=status)
        finally:
            conn.close()

    @mcp.custom_route("/files/{task_id}/{file_id}", methods=["GET"])
    async def download(request):
        conn = connect()
        try:
            result = handle_download(
                conn,
                is_remote=cfg.is_remote,
                task_id=request.path_params["task_id"],
                file_id=request.path_params["file_id"],
                token=_token(request),
                agent=request.query_params.get("agent", ""),
            )
            if isinstance(result, tuple):
                body, status = result
                return JSONResponse(body, status_code=status)
            return file_response(result)
        finally:
            conn.close()
