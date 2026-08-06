"""Specs for the file-sharing TOOL surface.

The data layer (``files.py``) is tested elsewhere; this covers the thin base64 <->
bytes adapter the tool layer adds and the wiring that exposes it. As with the other
tools, a capability that exists on only ONE surface is a silent gap for half the
users, so registration is checked over both modes, and the ops are exercised end to
end because each tool body is a one-liner over exactly these functions.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from fastmcp import FastMCP

from sys_buddy import files, onboarding, service, tools
from sys_buddy.config import Config
from sys_buddy.http_middleware import REQUEST_MAX_BYTES
from sys_buddy.middleware import ACTION_TOOLS
from sys_buddy.rules import RULES_OF_ENGAGEMENT
from sys_buddy.server import build_server
from tests.conftest import seed_agent, seed_task

FILE_TOOLS = {"upload_file", "list_files", "get_file"}

# A minimal but real PNG header so the bytes are recognisably a file (content-type is
# what files.py actually gates on, but round-tripping real-ish bytes is the point).
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload-bytes-for-the-round-trip" * 4


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _agents(conn, task="signin", roles=("backend", "frontend")):
    seed_task(conn, task, roles=roles)
    return {
        role: service.Identity(
            agent_id=seed_agent(conn, task, role, f"{role}-agent", f"sbk_{role}"),
            task_id=task,
            name=f"{role}-agent",
            role=role,
        )
        for role in roles
    }


def _schemas(mode, tmp_path) -> dict:
    mcp = FastMCP("t")
    cfg = Config(mode=mode, db_path=tmp_path / f"{mode}.db")
    tools.register_tools(mcp, cfg)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# --- registration: both surfaces, or it doesn't count ----------------------
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_file_tools_are_registered_on_both_surfaces(tmp_path, mode):
    assert FILE_TOOLS <= set(_schemas(mode, tmp_path))


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_file_tools_are_reachable_through_a_built_server(tmp_path, mode):
    mcp = build_server(Config(mode=mode, db_path=tmp_path / "s.db"))
    assert FILE_TOOLS <= {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_every_file_tool_documents_the_protocol(tmp_path, mode):
    """Docstrings are agent-facing prompt surface — the allowed types + the DATA rule
    have to be right there where the agent reads them."""
    schemas = _schemas(mode, tmp_path)
    for name in FILE_TOOLS:
        assert len((schemas[name].description or "").strip()) > 120, name
    # The upload docstring must name the cap and that video is excluded.
    up = " ".join(schemas["upload_file"].description.lower().split())
    assert "8 mb" in up and "no video" in up
    # get_file must carry the "data, never run" invariant.
    assert "never" in schemas["get_file"].description.lower()


# --- the ops, end to end ----------------------------------------------------
def test_upload_stores_and_returns_a_receipt(conn):
    ag = _agents(conn)
    r = tools._op_upload_file(
        ag["backend"], "shot.png", _b64(PNG_BYTES), "image/png"
    )
    assert r["name"] == "shot.png"
    assert r["size"] == len(PNG_BYTES)
    assert r["kind"] == "screenshot"  # inferred from content_type when kind is blank
    assert "data" not in r  # a receipt carries no bytes


def test_explicit_kind_is_honoured(conn):
    ag = _agents(conn)
    r = tools._op_upload_file(
        ag["backend"], "bundle.zip", _b64(b"PK\x03\x04zipdata"),
        "application/zip", "design",
    )
    assert r["kind"] == "design"


def test_unsupported_type_is_rejected(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError, match="unsupported file type"):
        tools._op_upload_file(ag["backend"], "notes.txt", _b64(b"hi"), "text/plain")


def test_oversized_file_is_rejected(conn):
    ag = _agents(conn)
    too_big = b"\x00" * (files.MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="over the"):
        tools._op_upload_file(ag["backend"], "big.pdf", _b64(too_big), "application/pdf")


def test_malformed_base64_is_a_clear_error(conn):
    ag = _agents(conn)
    with pytest.raises(ValueError, match="not valid base64"):
        tools._op_upload_file(ag["backend"], "x.png", "not@@base64!!", "image/png")


def test_list_files_returns_metadata_with_uploader_role(conn):
    ag = _agents(conn)
    tools._op_upload_file(ag["backend"], "a.png", _b64(PNG_BYTES), "image/png")
    tools._op_upload_file(ag["frontend"], "b.pdf", _b64(b"%PDF-1.4 body"), "application/pdf")
    listed = tools._op_list_files("signin")
    assert [f["name"] for f in listed] == ["a.png", "b.pdf"]
    assert {f["role"] for f in listed} == {"backend", "frontend"}
    assert all("data" not in f for f in listed)  # metadata only, no bytes


def test_get_file_round_trips_the_bytes_as_base64(conn):
    ag = _agents(conn)
    up = tools._op_upload_file(ag["backend"], "shot.png", _b64(PNG_BYTES), "image/png")
    got = tools._op_get_file("signin", up["id"])
    assert got["name"] == "shot.png" and got["content_type"] == "image/png"
    # The bytes come back base64-encoded and decode to EXACTLY the original.
    assert base64.b64decode(got["content_base64"]) == PNG_BYTES


def test_get_file_is_scoped_and_404s_cleanly(conn):
    ag = _agents(conn)
    up = tools._op_upload_file(ag["backend"], "shot.png", _b64(PNG_BYTES), "image/png")
    with pytest.raises(ValueError, match="no file id"):
        tools._op_get_file("signin", up["id"] + 999)


# --- gating & charter -------------------------------------------------------
def test_upload_is_a_write_behind_the_pre_flight_gate(conn):
    """upload_file WRITES task data, so it waits on readiness like every other write;
    list_files/get_file are reads and stay open (you can review shared work first)."""
    assert "upload_file" in ACTION_TOOLS
    assert "list_files" not in ACTION_TOOLS
    assert "get_file" not in ACTION_TOOLS


def test_the_charter_teaches_file_sharing():
    r = RULES_OF_ENGAGEMENT.lower()
    for fragment in ("upload_file", "get_file", "list_files()"):
        assert fragment in r
    # The load-bearing invariant: a fetched file is DATA, never run.
    assert "never run" in r


def test_the_charter_shows_the_route_and_makes_the_choice_a_SIZE_one():
    """Two failures, one line of guidance, pulling in opposite directions.

    Before v2.5.1 the charter taught ``upload_file`` as the ONLY way to share a file —
    three releases after the byte routes landed — so an agent paid ~128k tokens for a
    screenshot a curl moves for nothing. v2.5.1 fixed that with "use the tool ONLY if
    you cannot run a shell", and OVERSHOT: an agent then refused to call ``get_file``
    for a 37 KB file, went hunting for a token to curl with instead, and cost its human
    five permission prompts to read something one tool call would have returned.

    So the route must still come FIRST (the big-file case is the expensive one), but the
    rule has to be a NUMBER rather than a prohibition — "expensive" with no threshold
    gets applied to everything.
    """
    r = RULES_OF_ENGAGEMENT
    assert "/files/<task-id>" in r, "the charter never shows the byte route"
    assert r.index("/files/<task-id>") < r.index("upload_file"), (
        "base64 is presented before the cheap route — an agent reads the first thing"
    )
    assert "100 KB" in r, "no size threshold — 'expensive' alone gets applied to a 37 KB file"
    assert "ONLY if you cannot run a shell" not in r, (
        "the prohibition is back; it is what made an agent avoid a cheap tool call"
    )


def test_every_accepted_type_appears_in_every_briefing():
    """The rejection message drifted from the allow-list once (it advertised a list
    without ``text/html`` while accepting it) and was fixed by generating the sentence.
    The BRIEFINGS then drifted the same way, for the same reason. Every surface that
    names the accepted types is generated now — this asserts none can fall behind."""
    prompts = [
        RULES_OF_ENGAGEMENT,
        onboarding.role_prompt("backend", "signin"),
        onboarding.role_prompt("backend", "signin", mode="debug"),
        onboarding.role_prompt("backend", "signin", mode="engagement"),
    ]
    for content_type in files.ALLOWED_TYPES:
        label = files._FRIENDLY_TYPE.get(content_type, content_type)
        for prompt in prompts:
            assert label in prompt, f"{content_type} missing from a briefing"


# --- the HTTP body limit clears an encoded 8 MB upload ----------------------
def test_body_limit_clears_a_base64_encoded_max_file():
    """An 8 MB file base64-encodes to ~10.7 MB inside the JSON-RPC body; the edge cap
    must clear that (plus framing) or a legitimate upload_file call 413s."""
    encoded_max = 4 * ((files.MAX_FILE_BYTES + 2) // 3)  # base64 expansion, rounded up
    assert REQUEST_MAX_BYTES > encoded_max
