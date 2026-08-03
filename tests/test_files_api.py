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


def test_download_never_renders_html_in_the_browser(conn):
    """The security property that lets us accept ``text/html`` at all.

    An HTML file rendered inline would execute on the broker's OWN origin — the origin that
    serves the dashboard and holds the viewer cookie — so an agent could upload stored XSS
    with a credential attached. It must download, and be neutered if it somehow renders.
    """
    seed_task(conn, "signin", roles=("designer",))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    evil = b"<script>fetch('/api/tasks').then(r=>r.text()).then(t=>/*exfil*/0)</script>"
    rec = _upload(conn, designer, "report.html", evil, "text/html")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    resp = api._file_response(api._file_for(conn, viewer, rec["id"]))
    assert resp.body == evil          # stored verbatim — it is DATA, we do not sanitise it
    assert resp.headers["content-disposition"] == 'attachment; filename="report.html"'
    assert "inline" not in resp.headers["content-disposition"]
    assert resp.headers["content-security-policy"] == "default-src 'none'; sandbox"


def test_only_images_are_served_inline(conn):
    """Positive allow-list, asserted over every accepted type — so a type added later cannot
    quietly inherit ``inline`` and reopen the hole above."""
    seed_task(conn, "signin", roles=("designer",))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")

    for content_type in files.ALLOWED_TYPES:
        rec = _upload(conn, designer, "f.bin", b"some-bytes", content_type)
        resp = api._file_response(api._file_for(conn, viewer, rec["id"]))
        inline = resp.headers["content-disposition"] == "inline"
        assert inline is content_type.startswith("image/"), content_type


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
    """``/api/file/{id}`` is GET-only, like every other route on this surface. Uploads exist,
    but on their OWN surface (``POST /files/{task_id}``) with the agent's own credential —
    never against the dashboard origin, which authenticates viewers."""
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
    """Belt-and-braces on the source: the write verb lives in ``files.py``, not here."""
    src = Path(api.__file__).read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f'"{verb}"' not in src


# --------------------------------------------------------------------------- #
# POST /files/{task_id} — bytes in, on their own surface
# --------------------------------------------------------------------------- #
def test_upload_route_is_registered_and_post_only(tmp_path):
    from sys_buddy.config import Config
    from sys_buddy.server import build_server

    mcp = build_server(Config(mode="remote", db_path=tmp_path / "s.db"))
    routes = [
        r for r in getattr(mcp, "_additional_http_routes", [])
        if str(getattr(r, "path", "")) == "/files/{task_id}"
    ]
    assert routes, "expected POST /files/{task_id} to be registered"
    for route in routes:
        # Writing is POST and nothing else. Reading has its own path one segment deeper, so a
        # GET here would be a third way to read a file with no id to read.
        assert set(route.methods) <= {"POST"}, f"{route.path} accepts {route.methods}"


def test_download_route_is_registered_and_read_only(tmp_path):
    from sys_buddy.config import Config
    from sys_buddy.server import build_server

    mcp = build_server(Config(mode="remote", db_path=tmp_path / "s.db"))
    routes = [
        r for r in getattr(mcp, "_additional_http_routes", [])
        if str(getattr(r, "path", "")) == "/files/{task_id}/{file_id}"
    ]
    assert routes, "expected GET /files/{task_id}/{file_id} to be registered"
    for route in routes:
        assert set(route.methods) <= {"GET", "HEAD"}, f"{route.path} accepts writes"


def test_upload_stores_raw_bytes_under_the_token_holders_task(conn):
    """The happy path: no base64 anywhere, and the receipt matches the tool's."""
    seed_task(conn, "signin", roles=("designer",))
    seed_agent(conn, "signin", "designer", "dana-designer", "sbk_dana")

    body, status = files.handle_upload(
        conn, is_remote=True, task_id="signin", token="sbk_dana",
        name="screen.png", content_type="image/png", data=PNG,
    )
    assert status == 201
    assert body["name"] == "screen.png"
    assert body["kind"] == "screenshot"      # inferred from the content type
    assert body["size"] == len(PNG)
    stored = files.get_file(conn, "signin", body["id"])
    assert stored["data"] == PNG             # byte-for-byte, no encoding round trip


def test_upload_refuses_a_token_from_another_task(conn):
    """THE reason the task id is in the path. The token says who you are; the path says where
    you meant to write. Disagreement is refused, not silently resolved in the token's favour —
    otherwise a misconfigured agent lands files on a task nobody expected."""
    seed_task(conn, "signin", roles=("designer",))
    seed_task(conn, "payments", roles=("designer",))
    seed_agent(conn, "signin", "designer", "dana-designer", "sbk_dana")

    body, status = files.handle_upload(
        conn, is_remote=True, task_id="payments", token="sbk_dana",
        name="screen.png", content_type="image/png", data=PNG,
    )
    assert status == 403
    assert "belongs to task 'signin'" in body["error"]
    assert files.list_files(conn, "payments") == []   # and nothing landed


def test_upload_rejects_a_bad_or_missing_token(conn):
    seed_task(conn, "signin", roles=("designer",))
    for token in ("", "sbk_not_a_real_token"):
        body, status = files.handle_upload(
            conn, is_remote=True, task_id="signin", token=token,
            name="screen.png", content_type="image/png", data=PNG,
        )
        assert status == 401, token
    assert files.list_files(conn, "signin") == []


def test_upload_rejects_a_viewer_token(conn):
    """A viewer is a human watching a dashboard. Watching is not writing — and every buddy
    holds one, so accepting it here would let any of them write files into the task."""
    seed_task(conn, "signin", roles=("designer",))
    seed_viewer(conn, "host", "sbv_host", task_id=None)

    body, status = files.handle_upload(
        conn, is_remote=True, task_id="signin", token="sbv_host",
        name="screen.png", content_type="image/png", data=PNG,
    )
    assert status == 401
    assert files.list_files(conn, "signin") == []


def test_upload_accepts_html_and_buckets_it_as_other(conn):
    seed_task(conn, "signin", roles=("designer",))
    seed_agent(conn, "signin", "designer", "dana-designer", "sbk_dana")

    body, status = files.handle_upload(
        conn, is_remote=True, task_id="signin", token="sbk_dana",
        name="report.html", content_type="text/html", data=b"<h1>hi</h1>",
    )
    assert status == 201
    assert body["content_type"] == "text/html"
    assert body["kind"] == "other"


@pytest.mark.parametrize("header", [
    "text/html; charset=utf-8", "TEXT/HTML", " text/html ", "image/png;charset=binary",
])
def test_upload_normalises_the_content_type_header(conn, header):
    """Real clients send charsets and casing. The allow-list is about the TYPE, so a correct
    upload must not be refused over a parameter — `curl -H 'Content-Type: text/html;
    charset=utf-8'` is not a malformed request."""
    seed_task(conn, "signin", roles=("designer",))
    seed_agent(conn, "signin", "designer", "dana-designer", "sbk_dana")

    body, status = files.handle_upload(
        conn, is_remote=True, task_id="signin", token="sbk_dana", name="f",
        content_type=files._bare_content_type(header), data=b"bytes",
    )
    assert status == 201, body


def test_upload_passes_through_the_service_rejections(conn):
    """The route adds no validation of its own — an unsupported type, an empty body and a
    missing name are `upload_file`'s rules, surfaced as 400 with its message verbatim."""
    seed_task(conn, "signin", roles=("designer",))
    seed_agent(conn, "signin", "designer", "dana-designer", "sbk_dana")
    common = dict(conn=conn, is_remote=True, task_id="signin", token="sbk_dana")

    body, status = files.handle_upload(
        **common, name="clip.mp4", content_type="video/mp4", data=b"bytes")
    assert status == 400 and "unsupported file type" in body["error"]
    # The refusal must NAME every accepted type. The hand-written list said "PNG/JPG, PDF and
    # ZIP" and did not learn about text/html, so the broker refused a type it accepted while
    # telling the agent that type did not exist.
    for accepted in files.ALLOWED_TYPES:
        assert accepted in body["error"], f"{accepted} missing from the rejection message"

    body, status = files.handle_upload(
        **common, name="empty.png", content_type="image/png", data=b"")
    assert status == 400 and "empty file" in body["error"]

    body, status = files.handle_upload(
        **common, name="  ", content_type="image/png", data=PNG)
    assert status == 400 and "needs a name" in body["error"]

    body, status = files.handle_upload(
        **common, name="big.png", content_type="image/png",
        data=b"x" * (files.MAX_FILE_BYTES + 1))
    assert status == 400 and "over the" in body["error"]

    assert files.list_files(conn, "signin") == []


def test_download_hands_an_agent_the_raw_bytes(conn):
    """The other half. ``get_file`` returns the bytes as base64, so the READING agent paid the
    same ~128,000 tokens to pull a screenshot INTO its context — and a file it cannot fit is a
    file it cannot read at all."""
    seed_task(conn, "signin", roles=("designer", "backend"))
    designer = _identity(conn, "signin", "designer", "dana-designer")
    seed_agent(conn, "signin", "backend", "al-backend", "sbk_al")
    rec = _upload(conn, designer, "screen.png", PNG, "image/png")

    # A DIFFERENT agent on the same task reads it — that is the point of sharing.
    got = files.handle_download(
        conn, is_remote=True, task_id="signin", file_id=rec["id"], token="sbk_al")
    assert not isinstance(got, tuple), got
    assert got["data"] == PNG            # raw, no encoding round trip
    assert got["name"] == "screen.png"


def test_download_refuses_a_token_from_another_task(conn):
    """The task match is the scoping rule on the way out: an agent reads its OWN task's files
    and nothing else."""
    seed_task(conn, "signin", roles=("designer",))
    seed_task(conn, "payments", roles=("designer",))
    dana = _identity(conn, "signin", "designer", "dana-designer")
    rec = _upload(conn, dana, "screen.png", PNG, "image/png")
    seed_agent(conn, "payments", "designer", "pat-designer", "sbk_pat")

    # Naming the file's real task with a foreign token — refused on the path mismatch.
    body, status = files.handle_download(
        conn, is_remote=True, task_id="signin", file_id=rec["id"], token="sbk_pat")
    assert status == 403
    assert "belongs to task 'payments'" in body["error"]

    # …and naming their OWN task with the other task's file id is a 404, not a 403: a
    # distinguishable refusal would let an agent probe another task's file ids.
    body, status = files.handle_download(
        conn, is_remote=True, task_id="payments", file_id=rec["id"], token="sbk_pat")
    assert status == 404
    assert body == {"error": "not found"}


def test_download_404s_a_missing_or_unparseable_id(conn):
    seed_task(conn, "signin", roles=("designer",))
    seed_agent(conn, "signin", "designer", "dana-designer", "sbk_dana")
    for file_id in (9999, "not-a-number", None):
        body, status = files.handle_download(
            conn, is_remote=True, task_id="signin", file_id=file_id, token="sbk_dana")
        assert (body, status) == ({"error": "not found"}, 404), file_id


def test_download_rejects_a_viewer_token(conn):
    """A viewer reads through ``/api/file/{id}``, which is the dashboard's door. This one is
    the agents' door and takes an agent's credential — one door, one kind of key."""
    seed_task(conn, "signin", roles=("designer",))
    dana = _identity(conn, "signin", "designer", "dana-designer")
    rec = _upload(conn, dana, "screen.png", PNG, "image/png")
    seed_viewer(conn, "host", "sbv_host", task_id=None)

    body, status = files.handle_download(
        conn, is_remote=True, task_id="signin", file_id=rec["id"], token="sbv_host")
    assert (body, status) == ({"error": "unauthorized"}, 401)


def test_both_byte_routes_share_one_hardening_path(conn):
    """The dashboard's reader and the agents' reader serve through ONE builder, so the HTML
    rule cannot hold on one door and lapse on the other. Asserted as identical headers rather
    than as an identity check on the function, because the headers are the property that
    matters — a future second implementation would fail this even if it kept the name."""
    seed_task(conn, "signin", roles=("designer",))
    dana = _identity(conn, "signin", "designer", "dana-designer")
    rec = _upload(conn, dana, "report.html", b"<script>0</script>", "text/html")
    seed_viewer(conn, "host", "sbv_host", task_id=None)
    viewer = resolve_viewer_token(conn, "sbv_host")
    seed_agent(conn, "signin", "backend", "al-backend", "sbk_al")

    via_dashboard = api._file_response(api._file_for(conn, viewer, rec["id"]))
    via_agent = files.file_response(files.handle_download(
        conn, is_remote=True, task_id="signin", file_id=rec["id"], token="sbk_al"))
    for resp in (via_dashboard, via_agent):
        assert resp.headers["content-disposition"] == 'attachment; filename="report.html"'
        assert resp.headers["content-security-policy"] == "default-src 'none'; sandbox"


def test_upload_in_local_mode_names_the_seat(conn):
    """Local mode has no tokens, so the seat is named — the same convention every local MCP
    tool uses. Unnamed is a 400, because there is no identity to attribute the file to."""
    body, status = files.handle_upload(
        conn, is_remote=False, task_id="signin", agent="",
        name="screen.png", content_type="image/png", data=PNG,
    )
    assert status == 400 and "?agent=" in body["error"]

    body, status = files.handle_upload(
        conn, is_remote=False, task_id="signin", agent="backend",
        name="screen.png", content_type="image/png", data=PNG,
    )
    assert status == 201
    assert files.list_files(conn, "signin")[0]["role"] == "backend"
