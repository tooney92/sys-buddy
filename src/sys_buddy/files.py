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


# --------------------------------------------------------------------------- #
# the raw-bytes upload route
# --------------------------------------------------------------------------- #
# WHY THIS EXISTS. The MCP `upload_file` tool carries the file as a base64 STRING argument,
# which means the uploading agent has to *generate* the whole encoding token by token. That
# is the entire cost of sharing a file: storing a 328 KB screenshot takes the broker ~1.3 ms,
# and encoding it into a tool call costs the agent ~128,000 tokens. At the 8 MB cap it is
# ~3.2M tokens — more than a context window, so the documented limit was unreachable through
# the only path that existed. `get_file` is symmetrical: the reading agent pulls the same
# volume INTO its context.
#
# So bytes get their own door. An agent PUTs the file with `curl --data-binary`, the broker
# stores it, and nothing larger than a receipt passes through a model. Raw body rather than
# multipart: multipart needs `python-multipart` and gives an agent one more thing to get
# wrong, whereas `--data-binary @file` is a single obvious line.
#
# THE TASK ID IS IN THE PATH, and that is the point of the design rather than decoration.
# The token says who you are; the path says where you MEANT to write. When they disagree the
# broker refuses (403) instead of quietly following the token — so a misconfigured agent
# holding task A's token cannot land a file on task B, and cannot land it on A "by accident"
# either. Same rule the contract tools follow with `todo=N`: name the target, and be told
# when you named the wrong one.
def _bare_content_type(header: str) -> str:
    """``text/html; charset=utf-8`` → ``text/html``. Callers set charsets and boundaries and
    are not wrong to; the allow-list is about the type, not its parameters."""
    return (header or "").split(";")[0].strip().lower()


def handle_upload(
    conn, *, is_remote: bool, task_id: str, token: str = "", agent: str = "",
    name: str = "", kind: str | None = None, content_type: str = "", data: bytes = b"",
) -> tuple[dict, int]:
    """The whole upload decision — auth, task match, store — as ``(body, status)``.

    Separated from the route so it is testable on an open connection with no HTTP server,
    the way every other helper in this package is, and so the route below stays a thin
    adapter that only knows how to read a Request.
    """
    if is_remote:
        ident = resolve_agent_token(conn, token)
        if ident is None:
            return {"error": "unauthorized"}, 401
        # THE refusal that makes the path meaningful. Not a 404: the caller is a known agent
        # asking for something it may not do, and saying so plainly is what lets it fix its
        # own configuration instead of silently writing somewhere it did not intend.
        if ident.task_id != task_id:
            return {
                "error": f"your token belongs to task '{ident.task_id}', not '{task_id}' — "
                         f"upload to your own task"
            }, 403
    else:
        # Local mode has no tokens at all (the middleware auth gate is remote-only), so the
        # seat is NAMED, exactly as every local MCP tool names it. Without it there is no
        # identity to attribute the file to.
        if not agent.strip():
            return {"error": "local mode: name your seat with ?agent=<your agent name>"}, 400
        try:
            ident = service.ensure_local_identity(conn, task_id, agent.strip())
        except ValueError as e:
            return {"error": str(e)}, 400

    try:
        return upload_file(conn, ident, name, data, content_type, kind), 201
    except ValueError as e:
        # Every rejection `upload_file` raises is the caller's to fix — unsupported type,
        # empty body, over the cap, no name, closed task — so 400 with the message verbatim,
        # which is already written to be read by an agent.
        return {"error": str(e)}, 400


def register_upload_route(mcp, cfg) -> None:
    """Mount ``POST /files/{task_id}`` — the one route on this broker that takes bytes IN.

    Deliberately NOT under ``/api/*``: that surface is GET-only by D11 and stays that way.
    This is its own door on the agent side of the house, authenticated the way ``/mcp`` is
    (the agent's own bearer token) rather than with a dashboard viewer token — a viewer is a
    human watching, and must not be able to write files into a task.
    """
    from starlette.responses import JSONResponse

    from .db import connect

    @mcp.custom_route("/files/{task_id}", methods=["POST"])
    async def upload(request):
        auth = request.headers.get("authorization", "")
        conn = connect()
        try:
            body, status = handle_upload(
                conn,
                is_remote=cfg.is_remote,
                task_id=request.path_params["task_id"],
                token=auth[7:].strip() if auth[:7].lower() == "bearer " else "",
                agent=request.query_params.get("agent", ""),
                name=request.query_params.get("name", ""),
                kind=request.query_params.get("kind") or None,
                content_type=_bare_content_type(request.headers.get("content-type", "")),
                data=await request.body(),
            )
            return JSONResponse(body, status_code=status)
        finally:
            conn.close()
