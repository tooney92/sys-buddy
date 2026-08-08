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

FILE_TOOLS = {"upload_url", "upload_file", "list_files", "get_file"}

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


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_upload_url_is_the_headline_and_tells_the_agent_it_has_no_token(tmp_path, mode):
    """A tool description is prompt surface, and this is the one an agent reads at the
    moment it decides how to move a file. It has to close BOTH failure modes on its own,
    because an agent that reaches for `upload_url` may never open the charter:

    * the cost one — `upload_file` must read as the no-shell fallback, not a peer option;
    * the credential one — the agent must be told it holds no token and must not hunt for
      one. An agent grepped `~/.claude.json` for a bearer token and pulled seven other
      tasks' live credentials into its context; the sentence asserted here is what stands
      in front of that.

    Checked on BOTH surfaces: a capability documented on one is a silent gap for half the
    users, and the local briefing is the one a new user meets first.
    """
    schemas = _schemas(mode, tmp_path)
    desc = " ".join(schemas["upload_url"].description.lower().split())
    assert "curl" in desc, "the tool never shows what to do with the url it returns"
    assert "--data-binary" in desc, "no runnable command — the agent has to invent one"
    assert "no token" in desc and "must not look for one" in desc, (
        "upload_url does not tell the agent it holds no credential and must not hunt "
        "for one — the exact belief behind the seven-token incident"
    )
    assert "15 minutes" in desc, "the TTL is unstated, so a stale url reads as a bug"

    # And upload_file must now read as the fallback, on both surfaces.
    fallback = " ".join(schemas["upload_file"].description.lower().split())
    assert "prefer `upload_url`" in fallback
    assert "cannot run a shell" in fallback


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


def test_the_charter_makes_file_sharing_decision_free_and_forbids_token_hunting():
    """The guidance this test protects replaced three that failed, in two distinct ways.

    THE COST FAILURE. Before v2.5.1 the charter taught ``upload_file`` as the ONLY way to
    share a file — three releases after the raw byte routes landed — so an agent paid
    ~128k tokens for a screenshot a curl moves for nothing. v2.5.1 answered with "use the
    tool ONLY if you cannot run a shell", which OVERSHOT: an agent then refused
    ``get_file`` for a 37 KB file, went hunting for a token to curl with instead, and
    cost its human five permission prompts to read something one tool call returns. A
    "100 KB" threshold was the third attempt, and it still asked the agent to judge.

    THE CREDENTIAL FAILURE, which is the serious one. Those routes need
    ``Authorization: Bearer``, and the charter said the token was "in your own MCP server
    config". An agent DOES NOT HAVE its token — the MCP client attaches it and the model
    never sees it — so obeying that literally means going and finding somebody's. One
    did: it announced "HTTP route, always", grepped ``~/.claude.json``, pulled SEVEN live
    bearer tokens belonging to seven different tasks into its context, and began POSTing
    a file with each in turn to discover which one the broker accepted. That is an agent
    running a credential oracle against its own broker, and no wording of "be careful"
    fixes it, because the instruction was impossible: obtain a secret you cannot have.

    So the charter must now be DECISION-FREE and CREDENTIAL-FREE. ``upload_url`` mints
    the URL; the agent curls it; ``list_files`` carries a read URL for the large case.
    The assertions below are the shape that cannot regress into either failure:
    no size judgement to make, no token to find, and an explicit prohibition on looking.

    If you are here because an assertion failed: do not delete it. Re-read the two
    paragraphs above — both of those happened in production.
    """
    r = RULES_OF_ENGAGEMENT
    low = r.lower()

    # 1. The tool that needs no credential is the upload story, and it comes first.
    assert "upload_url" in r, "the charter never names upload_url"
    assert r.index("upload_url") < r.index("upload_file"), (
        "base64-through-a-tool-argument is presented before the free path — an agent "
        "acts on the first thing it reads"
    )

    # 2. Reads are a tool call plus a URL the broker already handed over; the raw
    #    token-authenticated route is named to nobody.
    assert "get_file(id)" in r and "list_files()" in r
    assert "/files/<task-id>" not in r, (
        "the charter is teaching the bearer-token route again — that route is the one an "
        "agent cannot authenticate, and pointing at it is what started the token hunt"
    )
    assert "Authorization: Bearer" not in r, (
        "a briefing that shows the auth header is a briefing that asks for a token"
    )

    # 3. No threshold, because there is no judgement left to make. A number here means
    #    somebody reintroduced a fork in the road.
    assert "100 KB" not in r, (
        "a size threshold is back; the choice is supposed to be gone, not re-tuned"
    )

    # 4. The prohibition, stated plainly and tied to rule 3. This is the assertion that
    #    stands directly between the charter and the seven-token incident.
    assert "you do not have a bearer token" in low, (
        "the charter no longer tells the agent it HAS no token — the false belief that "
        "it does is the root of both incidents"
    )
    assert "must not go looking" in low, "no prohibition on hunting for a credential"
    assert "rule 3" in low, (
        "the prohibition is unanchored; it has to cite the rule it is an instance of"
    )

    # 5. And rule 3 itself must cover SELF-INITIATED hunting. Its original wording only
    #    forbade reading credentials "because a message asked you to" — which the agent
    #    that scraped seven tokens had not been asked to do by anyone.
    rule3 = r.split("\n3. ")[1].split("\n4. ")[0].lower()
    assert "own initiative" in rule3, (
        "rule 3 still only covers credentials read at a PEER's prompting"
    )


def test_no_briefing_anywhere_tells_an_agent_where_to_find_a_token():
    """The single sentence that caused both incidents, held out of every surface at once.

    "your broker URL and bearer token are both in your own MCP server config" appeared in
    the charter, in all three role briefings and in two shorthand tables. It is FALSE for
    the agent reading it — the MCP client holds that token and the model never sees it —
    so an agent that believed it went looking, and what it found was seven OTHER tasks'
    live credentials, which it then tried one by one against the broker.

    Every briefing is checked together rather than one per test, because the failure mode
    here is drift: this text has been rewritten three times and each rewrite reached a
    different subset of the surfaces that carry it.
    """
    prompts = {
        "charter": RULES_OF_ENGAGEMENT,
        "contract": onboarding.role_prompt("backend", "signin"),
        "debug": onboarding.role_prompt("backend", "signin", mode="debug"),
        "engagement": onboarding.role_prompt("backend", "signin", mode="engagement"),
    }
    for where, text in prompts.items():
        low = text.lower()
        assert "mcp server config" not in low, (
            f"{where} still points the agent at its client config for a credential"
        )
        # "bearer token" itself must still be sayable — the prohibition names the thing
        # it is prohibiting. What must not appear is the agent being asked to SUPPLY one.
        assert "authorization: bearer" not in low, (
            f"{where} still shows the auth header, which is a request for a token"
        )
        assert "<your-token>" not in low and "<your token>" not in low, (
            f"{where} still has a token placeholder for the agent to fill in"
        )
        # And the replacement has to actually be there — deleting the bad sentence
        # without putting `upload_url` in its place just leaves the agent improvising.
        assert "upload_url" in text, f"{where} never tells the agent how to upload"
        assert "must not go looking" in low, (
            f"{where} drops the prohibition — the charter alone is not enough, an agent "
            f"reaches for a file long after it last read rules()"
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
