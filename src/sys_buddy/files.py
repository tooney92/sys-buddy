"""File sharing on a task — design bundles (zip), screenshots (png/jpg), PDFs. No video.

Uploaded by an agent (the `designer` typically) or a human; fetched by peers THROUGH the
broker (``get_file`` / ``GET /api/file/{id}``), never via a URL from chat — the same
invariant that limits an agent to the signed ``staging_url``. An uploaded file is DATA: a
consumer inspects/extracts it, never runs it (Rules of Engagement). The broker only stores
and serves bytes; it does NOT unpack archives, so there is no server-side zip-bomb or
path-traversal surface — safe extraction is the consumer's job, just as interpreting a peer
message is.

Bytes live in the row for v1 (a blob). If the db gets heavy, move to an on-disk ``files/``
dir with a path column; the public functions here don't change shape.
"""

from __future__ import annotations

import time

from .service import Identity

# 8 MB — fits a design zip / screenshot / PDF while staying bounded. NOTE for the tool/api
# layers: an upload arrives base64-encoded (~1.34x), and the HTTP body limit
# (``http_middleware``) is ~1 MiB, so the UPLOAD path must raise its limit to accept this.
MAX_FILE_BYTES = 8 * 1024 * 1024

# The only content types accepted. Video is deliberately excluded for now. The value is the
# default ``kind`` bucket for that type when the caller doesn't specify one.
ALLOWED_TYPES = {
    "image/png": "screenshot",
    "image/jpeg": "screenshot",
    "application/pdf": "other",
    "application/zip": "design",
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
        raise ValueError(
            f"unsupported file type '{content_type}'. Allowed: PNG/JPG images, PDF, and "
            f"ZIP — no video."
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
