"""Specs for signed file URLs — the credential the broker hands out so an agent never
has to go and find one.

WHY THIS EXISTS. The raw ``/files`` routes need ``Authorization: Bearer <token>``, and an
agent does not have a token: its MCP client attaches one the model never sees. The
briefings told agents otherwise ("your bearer token is in your own MCP server config"),
and two production incidents followed — one agent burning five permission prompts hunting
for a credential to read a 37 KB file, and one grepping ``~/.claude.json``, surfacing
SEVEN live bearer tokens belonging to seven different tasks, and POSTing a file with each
in turn to work out which the broker would accept. ``upload_url`` and the read ``url`` on
each ``list_files`` entry remove the need for a credential rather than warning about it.

The tests below are the security half: a signed URL must be worth NOTHING outside the one
task, the one action, the one file and the fifteen minutes it was minted for. Everything
here is a property of ``signing.py`` plus the two route handlers in ``files.py``; the
guidance half lives in ``test_files_tools.py``.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import parse_qs, urlparse

import pytest

from sys_buddy import files, service, signing, tools
from sys_buddy.config import Config
from tests.conftest import seed_agent, seed_task

BASE = "https://broker.example"
PNG = b"\x89PNG\r\n\x1a\n" + b"round-trip-payload" * 8


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


def _query(url: str) -> dict:
    """A URL's query as the flat ``{k: v}`` mapping a Starlette route sees.

    ``keep_blank_values=True`` because that is what Starlette's ``QueryParams`` does; a
    helper that quietly dropped blanks would test a request shape no route ever receives.
    """
    return {
        k: v[0]
        for k, v in parse_qs(urlparse(url).query, keep_blank_values=True).items()
    }


def _stored(conn, task="signin"):
    return files.list_files(conn, task)


# --- the happy path, end to end --------------------------------------------
def test_a_signed_upload_url_stores_the_bytes_under_the_signed_name_and_type(conn):
    ag = _agents(conn)
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="shot.png", content_type="image/png", kind="screenshot",
    )
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=_query(url), data=PNG
    )
    assert status == 201
    assert body["name"] == "shot.png" and body["content_type"] == "image/png"
    assert body["size"] == len(PNG)
    # Stored against the SEAT the url was minted for, not "whoever POSTed".
    assert _stored(conn)[0]["role"] == "backend"


def test_the_name_and_type_come_from_the_signature_not_the_request(conn):
    """The whole point of the two-line workflow is that the curl carries nothing but
    bytes. So a request that also sets ``?name=`` or a ``Content-Type`` header must not be
    able to steer where those bytes land — the signed claims win, and the route never even
    reads the header on this lane."""
    ag = _agents(conn)
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="real.png", content_type="image/png", kind="screenshot",
    )
    q = _query(url)
    q["name"] = "attacker.html"       # the token lane's own parameter
    q["kind"] = "design"
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=q, data=PNG
    )
    assert status == 201
    assert body["name"] == "real.png"
    assert body["content_type"] == "image/png"
    assert body["kind"] == "screenshot"


def test_a_signed_read_url_returns_exactly_the_bytes_that_were_uploaded(conn):
    ag = _agents(conn)
    up = tools._op_upload_file(
        ag["backend"], "shot.png", base64.b64encode(PNG).decode(), "image/png"
    )
    url, _ = signing.read_url(BASE, task_id="signin", file_id=up["id"])
    rec = files.handle_signed_download(
        conn, task_id="signin", file_id=str(up["id"]), query=_query(url)
    )
    assert not isinstance(rec, tuple), "a freshly minted read url was refused"
    assert rec["data"] == PNG


# --- expiry -----------------------------------------------------------------
def test_an_expired_url_is_refused_on_both_routes(conn):
    """15 minutes is the window in which an agent can stop and ask its human a question.
    Past it the URL is worth nothing, so a copy left in a log or a chat transcript is not
    a standing grant of write access to somebody's task."""
    ag = _agents(conn)
    long_ago = time.time() - signing.SIGNED_URL_TTL - 1
    up_url, exp = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="shot.png", content_type="image/png", now=long_ago,
    )
    assert exp < time.time()
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=_query(up_url), data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})

    stored = tools._op_upload_file(
        ag["backend"], "shot.png", base64.b64encode(PNG).decode(), "image/png"
    )
    rd_url, _ = signing.read_url(
        BASE, task_id="signin", file_id=stored["id"], now=long_ago
    )
    assert files.handle_signed_download(
        conn, task_id="signin", file_id=str(stored["id"]), query=_query(rd_url)
    ) == ({"error": "not found"}, 404)


def test_the_expiry_cannot_be_pushed_out_by_editing_the_query(conn):
    """``sb_exp`` is IN the signed claim set, so extending it invalidates the signature.
    An expiry that could be edited would be a comment, not a limit."""
    ag = _agents(conn)
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="shot.png", content_type="image/png",
        now=time.time() - signing.SIGNED_URL_TTL - 1,
    )
    q = _query(url)
    q["sb_exp"] = str(int(time.time() + 3600))
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=q, data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})


# --- tampering --------------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("sb_sig", "AAAA" * 11),        # a plausible-looking forgery
    ("sb_sig", ""),                 # stripped entirely
    ("sb_name", "evil.html"),       # rename the file after minting
    ("sb_ct", "text/html"),         # store a PNG upload as renderable HTML
    ("sb_kind", "design"),          # re-bucket it
    ("sb_aid", "999999"),           # attribute it to another seat
])
def test_a_tampered_upload_url_is_refused(conn, field, value):
    """Every claim is covered by the MAC, so editing ANY of them breaks it. ``sb_ct`` is
    the one with teeth: ``text/html`` is an accepted upload type, and being able to change
    a signed image upload into an HTML one after the fact would be a way to plant a
    document the dashboard has to keep neutering (see ``files.file_response``)."""
    ag = _agents(conn)
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="shot.png", content_type="image/png",
    )
    q = _query(url)
    q[field] = value
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=q, data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})
    assert _stored(conn) == [], "a tampered url still landed a file"


def test_a_tampered_read_url_is_refused(conn):
    ag = _agents(conn)
    up = tools._op_upload_file(
        ag["backend"], "shot.png", base64.b64encode(PNG).decode(), "image/png"
    )
    url, _ = signing.read_url(BASE, task_id="signin", file_id=up["id"])
    q = _query(url)
    q["sb_sig"] = q["sb_sig"][:-1] + ("A" if q["sb_sig"][-1] != "A" else "B")
    assert files.handle_signed_download(
        conn, task_id="signin", file_id=str(up["id"]), query=q
    ) == ({"error": "not found"}, 404)


# --- action scoping ---------------------------------------------------------
def test_an_upload_url_cannot_read(conn):
    """The action is signed, and the route supplies it — so replaying a write URL against
    the read handler rebuilds the claims with ``act="read"`` and the MAC simply does not
    match. A URL that could do both would make "this only uploads" a comment."""
    ag = _agents(conn)
    up = tools._op_upload_file(
        ag["backend"], "shot.png", base64.b64encode(PNG).decode(), "image/png"
    )
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="shot.png", content_type="image/png",
    )
    assert files.handle_signed_download(
        conn, task_id="signin", file_id=str(up["id"]), query=_query(url)
    ) == ({"error": "not found"}, 404)


def test_a_read_url_cannot_upload(conn):
    ag = _agents(conn)
    up = tools._op_upload_file(
        ag["backend"], "shot.png", base64.b64encode(PNG).decode(), "image/png"
    )
    url, _ = signing.read_url(BASE, task_id="signin", file_id=up["id"])
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=_query(url), data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})
    assert len(_stored(conn)) == 1, "a read url wrote a second file"


def test_a_read_url_cannot_be_walked_to_the_file_next_door(conn):
    """The file id lives in the PATH and the path is signed, so incrementing it is the
    same class of failure as forging the signature."""
    ag = _agents(conn)
    b64 = base64.b64encode(PNG).decode()
    first = tools._op_upload_file(ag["backend"], "a.png", b64, "image/png")
    second = tools._op_upload_file(ag["backend"], "b.png", b64, "image/png")
    url, _ = signing.read_url(BASE, task_id="signin", file_id=first["id"])
    assert files.handle_signed_download(
        conn, task_id="signin", file_id=str(second["id"]), query=_query(url)
    ) == ({"error": "not found"}, 404)


# --- task scoping -----------------------------------------------------------
def test_a_url_for_task_A_is_refused_on_task_B(conn):
    """The task is in the path and in the claims, so a URL cannot be dragged sideways. This
    is the property the ORIGINAL design bought with "your token must match the path" — it
    has to survive the move to signatures, because it is what stops a misconfigured or
    coerced agent landing a file on somebody else's task."""
    a = _agents(conn, "signin")
    _agents(conn, "billing", roles=("backend",))
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=a["backend"].agent_id,
        name="shot.png", content_type="image/png",
    )
    body, status = files.handle_signed_upload(
        conn, task_id="billing", query=_query(url), data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})
    assert files.list_files(conn, "billing") == []

    stored = tools._op_upload_file(a["backend"], "shot.png",
                                   base64.b64encode(PNG).decode(), "image/png")
    rd, _ = signing.read_url(BASE, task_id="signin", file_id=stored["id"])
    assert files.handle_signed_download(
        conn, task_id="billing", file_id=str(stored["id"]), query=_query(rd)
    ) == ({"error": "not found"}, 404)


def test_an_upload_url_for_a_seat_on_another_task_is_refused(conn):
    """Belt to the path's braces: even a correctly signed URL is re-checked against the
    seat at USE time, so a claim set naming an agent that does not belong to this task
    cannot store anything."""
    _agents(conn, "signin")
    other = _agents(conn, "billing", roles=("backend",))
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=other["backend"].agent_id,
        name="shot.png", content_type="image/png",
    )
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=_query(url), data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})


def test_a_revoked_seats_upload_url_stops_working(conn):
    """A URL outlives the tool call that minted it, so liveness is judged at USE time —
    the same way ``identity.resolve_agent_token`` judges a token. Revoking a seat has to
    actually revoke it, not leave a 15-minute write window open behind it."""
    ag = _agents(conn)
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="shot.png", content_type="image/png",
    )
    conn.execute("UPDATE agents SET revoked_at = ? WHERE id = ?",
                 (time.time(), ag["backend"].agent_id))
    conn.commit()
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=_query(url), data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})


# --- key rotation / restart -------------------------------------------------
def test_a_signature_from_before_a_key_rotation_is_refused(conn):
    """The key is minted at BOOT and never persisted, which is the whole reason there is
    no migration and no secret on disk. The cost is that a restart invalidates outstanding
    URLs — harmless at a 15-minute TTL, and worth asserting so nobody "fixes" it by
    writing the key to the database, which would turn a process secret into a durable one
    that can leak.

    It is also the operator's kill switch: if a URL leaks, rotating beats waiting.
    """
    ag = _agents(conn)
    url, _ = signing.upload_url(
        BASE, task_id="signin", agent_id=ag["backend"].agent_id,
        name="shot.png", content_type="image/png",
    )
    stored = tools._op_upload_file(
        ag["backend"], "old.png", base64.b64encode(PNG).decode(), "image/png"
    )
    rd, _ = signing.read_url(BASE, task_id="signin", file_id=stored["id"])

    old = signing.key()
    assert signing.rotate_key() != old       # a simulated restart

    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=_query(url), data=PNG
    )
    assert (status, body) == (401, {"error": "unauthorized"})
    assert files.handle_signed_download(
        conn, task_id="signin", file_id=str(stored["id"]), query=_query(rd)
    ) == ({"error": "not found"}, 404)


def test_the_key_is_never_written_to_the_database(conn):
    """Stated as a test because "no schema change, no persisted secret" is the design claim
    that makes boot-time key generation acceptable in the first place. A key on disk is a
    key that can be read off a backup, and a key in a table is a migration plus a rotation
    story nobody asked for."""
    schema = "\n".join(
        r[0] or "" for r in conn.execute("SELECT sql FROM sqlite_master").fetchall()
    )
    key_hex = signing.key().hex()
    assert "signing" not in schema.lower() and "hmac" not in schema.lower(), schema
    assert key_hex not in schema
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%key%'"
    ).fetchall()
    assert rows == [], rows


# --- the failure is opaque --------------------------------------------------
def test_every_read_refusal_is_the_same_404_a_missing_file_gets(conn):
    """Deliberate indistinguishability, the same rule ``handle_download`` and
    ``api._file_for`` follow. A 403 meaning "valid signature, wrong file" would tell a
    caller that the file exists, which is exactly the probe the 404 exists to defeat."""
    ag = _agents(conn)
    up = tools._op_upload_file(
        ag["backend"], "shot.png", base64.b64encode(PNG).decode(), "image/png"
    )
    good = _query(signing.read_url(BASE, task_id="signin", file_id=up["id"])[0])
    expired = _query(signing.read_url(
        BASE, task_id="signin", file_id=up["id"],
        now=time.time() - signing.SIGNED_URL_TTL - 1)[0])
    forged = dict(good, sb_sig="AAAA" * 11)

    missing = files.handle_signed_download(
        conn, task_id="signin", file_id="999999",
        query=_query(signing.read_url(BASE, task_id="signin", file_id=999999)[0]),
    )
    for label, q, fid in [
        ("expired", expired, str(up["id"])),
        ("forged", forged, str(up["id"])),
        ("wrong action", _query(signing.upload_url(
            BASE, task_id="signin", agent_id=ag["backend"].agent_id,
            name="x.png", content_type="image/png")[0]), str(up["id"])),
    ]:
        assert files.handle_signed_download(
            conn, task_id="signin", file_id=fid, query=q
        ) == missing, f"{label} is distinguishable from a file that does not exist"


# --- the signed lane does not close the old one -----------------------------
def test_an_unsigned_request_still_takes_the_token_lane(conn):
    """Signed URLs ADD a door. Anything already wired to the bearer-token route keeps
    working — it is simply no longer named in any briefing."""
    ag = _agents(conn)
    assert not signing.present({})
    body, status = files.handle_upload(
        conn, is_remote=True, task_id="signin", token="sbk_backend",
        name="shot.png", content_type="image/png", data=PNG,
    )
    assert status == 201 and body["name"] == "shot.png"


# --- what the tools actually hand back --------------------------------------
def test_upload_url_hands_back_a_runnable_command_and_no_credential(conn):
    ag = _agents(conn)
    r = tools._op_upload_url(ag["backend"], "shot.png", "image/png", None, BASE)
    assert r["url"].startswith(f"{BASE}/files/signin?")
    assert r["command"] == f'curl -sS -X POST "{r["url"]}" --data-binary @shot.png'
    assert r["kind"] == "screenshot"          # inferred, as upload_file infers it
    assert r["expires_in_seconds"] == signing.SIGNED_URL_TTL == 900
    assert r["max_bytes"] == files.MAX_FILE_BYTES
    # Nothing token-shaped anywhere in the reply.
    assert "sbk_" not in str(r) and "Authorization" not in str(r)
    # And it is really usable.
    body, status = files.handle_signed_upload(
        conn, task_id="signin", query=_query(r["url"]), data=PNG
    )
    assert status == 201


def test_upload_url_refuses_a_bad_type_before_the_agent_shells_out(conn):
    """Validated at MINT time through the same ``files.check_uploadable`` the store path
    runs, so the agent hears "unsupported file type" from the tool call rather than from a
    curl it has already spent 8 MB on."""
    ag = _agents(conn)
    with pytest.raises(ValueError, match="unsupported file type"):
        tools._op_upload_url(ag["backend"], "clip.mp4", "video/mp4", None, BASE)
    with pytest.raises(ValueError, match="needs a name"):
        tools._op_upload_url(ag["backend"], "   ", "image/png", None, BASE)


def test_upload_url_refuses_a_closed_task(conn):
    ag = _agents(conn)
    conn.execute("UPDATE tasks SET closed_at = ? WHERE id = ?", (time.time(), "signin"))
    conn.commit()
    with pytest.raises(ValueError, match="closed"):
        tools._op_upload_url(ag["backend"], "shot.png", "image/png", None, BASE)


def test_list_files_carries_a_working_read_url_per_entry(conn):
    """This is what makes "downloads need no new tool" true: the listing an agent already
    calls is the listing that hands it the URL."""
    ag = _agents(conn)
    b64 = base64.b64encode(PNG).decode()
    tools._op_upload_file(ag["backend"], "a.png", b64, "image/png")
    tools._op_upload_file(ag["frontend"], "b.pdf",
                          base64.b64encode(b"%PDF-1.4 body").decode(), "application/pdf")

    listed = tools._op_list_files("signin", BASE)
    assert [f["name"] for f in listed] == ["a.png", "b.pdf"]
    for f in listed:
        # The metadata contract is unchanged — the url is additive.
        assert {"id", "name", "kind", "content_type", "size", "role"} <= set(f)
        assert "data" not in f
        assert f["url"].startswith(f"{BASE}/files/signin/{f['id']}?")
        assert f["url_expires_at"] > time.time()

    rec = files.handle_signed_download(
        conn, task_id="signin", file_id=str(listed[0]["id"]),
        query=_query(listed[0]["url"]),
    )
    assert rec["data"] == PNG


def test_the_dashboards_file_listing_gets_no_signed_urls(conn):
    """Signing lives in the TOOL layer, not in ``files.list_files``, because the dashboard
    calls that function directly under a VIEWER token. A viewer handed an agent-lane URL
    would be handed a capability its own credential does not carry."""
    ag = _agents(conn)
    tools._op_upload_file(ag["backend"], "a.png",
                          base64.b64encode(PNG).decode(), "image/png")
    for f in files.list_files(conn, "signin"):
        assert "url" not in f


def test_both_surfaces_mint_the_same_kind_of_url(tmp_path):
    """Local mode has no auth, so a signature is not a security boundary there and this
    module does not pretend it is: ``?agent=`` stays open and is still the documented local
    escape hatch. What signing buys locally is UNIFORMITY — the same tool, the same URL
    shape, the same briefing sentence — because a mode-dependent branch is exactly the kind
    of judgement call that produced both incidents in the first place."""
    for mode in ("local", "remote"):
        cfg = Config(mode=mode, db_path=tmp_path / f"{mode}.db")
        url, _ = signing.upload_url(
            cfg.base_url, task_id="signin", agent_id=1,
            name="shot.png", content_type="image/png",
        )
        q = _query(url)
        assert set(q) == {"sb_aid", "sb_name", "sb_ct", "sb_exp", "sb_sig"}
        assert signing.verify(q, act=signing.UPLOAD, task_id="signin") is not None

    # A read url carries no name/type/kind and no seat, so those params are absent
    # entirely rather than sent blank — see ``signing._query``.
    rd = _query(signing.read_url(BASE, task_id="signin", file_id=1)[0])
    assert set(rd) == {"sb_exp", "sb_sig"}
    assert signing.verify(rd, act=signing.READ, task_id="signin", file_id="1") is not None
