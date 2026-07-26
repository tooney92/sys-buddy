"""Specs for file sharing on the read-only dashboard API.

Two invariants carry this file, exactly as they do for todos:

* **Backwards compatibility.** A task with NO files must serialise as it did before
  file sharing existed — the deployed ``ui.html`` reads that payload and is served from
  disk, so it can be newer OR older than the running ``api.py`` across a restart. The
  ``files`` key is therefore ABSENT, not empty, on a task with no files; a newer page
  reads ``d.files || []`` and is happy either way.
* **Read-only (D11).** ``GET /api/file/{id}`` hands bytes OUT; it never takes them in.
  Uploads happen through the MCP ``upload_file`` tool, never against this origin. And
  the bytes it hands out are token-scoped: a token for task A can never read task B's
  file — enforced on the server, not the client.

Like ``tests/test_api.py`` / ``tests/test_todos_api.py`` these drive the ``_``-prefixed
helpers directly (they take an open connection), so no HTTP server is needed. The one
exception is the GET-only route-registration check, which inspects the built server.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sys_buddy import api, files
from sys_buddy.identity import Identity, resolve_viewer_token
from tests.conftest import seed_agent, seed_task, seed_viewer

# A few valid payloads. Content is opaque to the broker — only the declared
# content_type drives inline-vs-attachment — so tiny stand-ins are enough.
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
ZIP = b"PK\x03\x04" + b"fake-zip-bytes"
PDF = b"%PDF-1.4\n" + b"fake-pdf-bytes"


# --------------------------------------------------------------------------- #
# seed helpers
# --------------------------------------------------------------------------- #
def _identity(conn, task_id, role, name):
    """A live agent + the Identity the upload path expects."""
    agent_id = seed_agent(conn, task_id, role, name, f"sbk_{task_id}_{role}")
    return Identity(agent_id=agent_id, task_id=task_id, name=name, role=role)


def _upload(conn, ident, name, data, content_type, kind=None):
    """Upload through the real service, so list_files' role join has a real agent."""
    return files.upload_file(conn, ident, name, data, content_type, kind=kind)


# --------------------------------------------------------------------------- #
# the regression that protects the live dashboard
# --------------------------------------------------------------------------- #
def test_task_with_no_files_omits_the_files_key(conn):
    """A task with no files carries no ``files`` key at all — the pre-file payload is
    unchanged, so an older on-disk ``ui.html`` sees nothing new. (A newer page reads
    ``d.files || []``, so absent and empty are indistinguishable to it.)"""
    seed_task(conn, "signin", roles=("backend", "frontend"))
    detail = api._task_detail(conn, "signin")
    assert "files" not in detail


def test_task_detail_lists_files_with_shape_role_and_time(conn):
    seed_task(conn, "signin", roles=("designer", "frontend"))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    _upload(conn, designer, "designs.zip", ZIP, "application/zip")
    _upload(conn, designer, "screen.png", PNG, "image/png")

    detail = api._task_detail(conn, "signin")
    assert "files" in detail
    fs = detail["files"]
    # Oldest-first: the order they were uploaded (list_files ORDER BY id).
    assert [f["name"] for f in fs] == ["designs.zip", "screen.png"]

    zip_entry = fs[0]
    # The exact per-entry contract the UI builds against: list_files output + `time`.
    assert set(zip_entry) == {
        "id", "name", "kind", "content_type", "size", "created_at", "role", "time",
    }
    assert zip_entry["kind"] == "design"           # zip buckets to 'design'
    assert zip_entry["content_type"] == "application/zip"
    assert zip_entry["size"] == len(ZIP)
    assert zip_entry["role"] == "designer"         # uploader's role, joined in
    assert re.fullmatch(r"\d\d:\d\d", zip_entry["time"])  # mono HH:MM
    assert zip_entry["time"] == api._hhmm(zip_entry["created_at"])

    # The png buckets to 'screenshot'.
    assert fs[1]["kind"] == "screenshot"


# --------------------------------------------------------------------------- #
# GET /api/file/{id} — the bytes, inline vs attachment
# --------------------------------------------------------------------------- #
def test_download_serves_image_inline_with_content_type(conn):
    seed_task(conn, "signin", roles=("designer",))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    rec = _upload(conn, designer, "screen.png", PNG, "image/png")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    f = api._file_for(conn, viewer, rec["id"])
    assert f is not None
    resp = api._file_response(f)
    assert resp.body == PNG
    assert resp.media_type == "image/png"
    # Images render in-page so the dashboard can <img src> this URL.
    assert resp.headers["content-disposition"] == "inline"


def test_download_serves_zip_as_attachment_with_filename(conn):
    seed_task(conn, "signin", roles=("designer",))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    rec = _upload(conn, designer, "designs.zip", ZIP, "application/zip")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    resp = api._file_response(api._file_for(conn, viewer, rec["id"]))
    assert resp.body == ZIP
    assert resp.media_type == "application/zip"
    # Non-images download under their original name.
    assert resp.headers["content-disposition"] == 'attachment; filename="designs.zip"'


def test_download_serves_pdf_as_attachment(conn):
    seed_task(conn, "signin", roles=("designer",))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    rec = _upload(conn, designer, "spec.pdf", PDF, "application/pdf")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    resp = api._file_response(api._file_for(conn, viewer, rec["id"]))
    assert resp.media_type == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="spec.pdf"'


def test_download_filename_is_header_sanitised(conn):
    """A stored name with header-breaking characters can't inject a header — the quote,
    backslash and CR/LF are stripped from the suggested download name."""
    assert api._safe_filename('a"b\\c\r\nd.zip') == "abcd.zip"
    assert api._safe_filename("") == "download"
    assert api._safe_filename('"""') == "download"


# --------------------------------------------------------------------------- #
# task-scoping — a token for task A must never read task B's file
# --------------------------------------------------------------------------- #
def test_buddy_cannot_read_another_tasks_file(conn):
    # A file lives on 'signin'; the buddy token is bound to 'billing'.
    seed_task(conn, "signin", roles=("designer",))
    seed_task(conn, "billing", roles=("designer",))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    rec = _upload(conn, designer, "designs.zip", ZIP, "application/zip")

    seed_viewer(conn, "dave", "sbv_dave", task_id="billing")
    buddy = resolve_viewer_token(conn, "sbv_dave")
    # Refused — and indistinguishable from a missing file (both -> None -> 404), so the
    # buddy can't even confirm the file exists.
    assert api._file_for(conn, buddy, rec["id"]) is None

    # The task's own buddy CAN read it; so can the host.
    seed_viewer(conn, "signin-buddy", "sbv_signin", task_id="signin")
    own = resolve_viewer_token(conn, "sbv_signin")
    assert api._file_for(conn, own, rec["id"]) is not None
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    host = resolve_viewer_token(conn, "sbv_host")
    assert api._file_for(conn, host, rec["id"]) is not None


def test_missing_or_nonnumeric_file_is_none(conn):
    seed_task(conn, "signin", roles=("designer",))
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    host = resolve_viewer_token(conn, "sbv_host")
    assert api._file_for(conn, host, 9999) is None   # no such id
    assert api._file_for(conn, host, "not-a-number") is None  # bad path param


# --------------------------------------------------------------------------- #
# D11 — the file route is read-only, no upload path here
# --------------------------------------------------------------------------- #
def test_file_route_is_get_only(tmp_path):
    """``/api/file/{id}`` is GET-only, like every other route on this surface — there
    is no POST/PUT upload here; uploads go through the MCP ``upload_file`` tool."""
    from sys_buddy.config import Config
    from sys_buddy.server import build_server

    mcp = build_server(Config(mode="remote", db_path=tmp_path / "s.db"))
    file_routes = [
        r for r in getattr(mcp, "_additional_http_routes", [])
        if str(getattr(r, "path", "")).startswith("/api/file")
    ]
    assert file_routes, "expected the /api/file route to be registered"
    for route in file_routes:
        assert set(route.methods) <= {"GET", "HEAD"}, f"{route.path} accepts writes"


def test_api_source_declares_no_write_verb_for_files():
    """Belt-and-braces on the source: adding the file route introduced no write verb."""
    src = Path(api.__file__).read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f'"{verb}"' not in src
