"""Short-lived, single-purpose signed URLs for the raw-byte ``/files`` routes.

WHY THIS EXISTS — and it is not a performance story, it is an incident report.

The ``/files`` routes need a credential. The MCP tools do not: the client attaches the
bearer token to every ``/mcp`` call, so the MODEL never sees it. That asymmetry was
documented to agents as "your broker URL and bearer token are both in your own MCP server
config", and agents obeyed it literally. Twice, in production:

* one refused ``get_file`` for a 37 KB file, went hunting through config files for a token
  it structurally cannot have, and cost its human five permission prompts;
* one announced "HTTP route, always", grepped ``~/.claude.json``, pulled **seven live
  bearer tokens belonging to seven different tasks** into its context, and started POSTing
  a file with each candidate in turn to find out which one the broker would accept — a
  credential oracle, run by an agent, against its own broker.

The guidance was rewritten three times (under-warning, then over-warning, then a size
threshold) and agents still got it wrong, because the ask was impossible: make a judgement
call, and obtain a secret you were never handed. So the fix is not a fourth warning. The
broker HANDS OUT the credential instead, already scoped, already expiring:
``upload_url`` mints a POST URL for the calling agent's own task, and ``list_files``
carries a read URL per entry. An agent never needs a token, so it is never asked to look
for one — and "you must not go looking" becomes an absolute rule instead of a trade-off.

THE SCHEME. HMAC-SHA256 over a canonical JSON claim set, signature in the query string.

* The key is generated at BOOT (``secrets.token_bytes(32)``) and never persisted. No new
  table, no migration, no secret on disk to leak or rotate. A restart invalidates every
  outstanding URL, which at this TTL is indistinguishable from them expiring.
* TTL is 15 minutes: long enough that an agent which stops to ask its human comes back to
  a live URL, short enough that a leaked one is worthless by the time anyone reads the log
  it leaked into.
* THE ACTION, THE TASK AND THE FILE ID ARE NOT IN THE QUERY STRING. They are recovered by
  the route from the HTTP METHOD and the PATH and fed into the claim set at verification
  time. That is what makes the scoping structural rather than a check somebody has to
  remember to write: replay an upload URL on the read route and the claims are rebuilt
  with ``act="read"``, so the signature simply does not match. Move a URL to another
  task's path, or another file id, and the same thing happens. There is no code path where
  a valid signature can be checked against the wrong target, because the target IS part of
  what was signed.
* Canonical JSON (``sort_keys``, no whitespace) rather than a delimiter-joined string: a
  filename may contain any byte, and ``|`` or ``\\n`` as a separator is a claim-injection
  bug waiting for the first file called ``a|b.png``.
* ``hmac.compare_digest`` for the comparison.

A verified upload URL also carries the NAME, CONTENT TYPE and KIND it was minted for, so
the stored file is exactly what the tool call described and the agent's command line has
nothing in it to get wrong — no ``-H "Content-Type: ..."``, no ``?name=``, just
``curl -X POST "<url>" --data-binary @file``.

LOCAL MODE — the honest version. Local mode binds loopback and has no auth at all: any
process on the machine can already POST to ``/files/<task>?agent=me``, which is the
documented local path and stays open. So a signature is NOT a security boundary there, and
this module does not pretend otherwise. What it buys locally is UNIFORMITY: ``upload_url``
returns the same shape of URL, verified by the same code, so the briefings carry one
instruction instead of a mode-dependent branch for an agent to get wrong — and that branch
is precisely the kind of judgement call that caused both incidents above.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
from hashlib import sha256
from urllib.parse import urlencode

# 15 minutes. See the module docstring: this is the window in which an agent can stop,
# ask its human a question, and come back — not a session length.
SIGNED_URL_TTL = 900

# The two actions a URL may be scoped to. An "upload" URL is only ever checked against
# POST /files/{task}; a "read" URL only against GET /files/{task}/{file}.
UPLOAD = "upload"
READ = "read"

# Query parameter names, namespaced so they can never collide with the route's own
# ``name`` / ``kind`` / ``agent`` params (which the token path still uses).
SIG_PARAM = "sb_sig"
_PARAMS = {
    "aid": "sb_aid",      # the agent the URL was minted for
    "name": "sb_name",    # upload only
    "ct": "sb_ct",        # upload only — the content type the bytes will be stored as
    "kind": "sb_kind",    # upload only, may be ""
    "exp": "sb_exp",      # unix seconds
}

_KEY: bytes | None = None


def key() -> bytes:
    """The process's signing key, minted on first use.

    Lazy rather than module-import-time only so importing this module is free; the
    practical effect is the same, since the first signature is minted after boot.
    """
    global _KEY
    if _KEY is None:
        _KEY = secrets.token_bytes(32)
    return _KEY


def rotate_key() -> bytes:
    """Throw the key away and mint a new one — every outstanding URL dies.

    This is what a broker RESTART does implicitly. It is exposed as a function so a test
    can prove the property without spawning a process, and so an operator who believes a
    URL leaked has a way to kill it that does not involve waiting out the TTL.
    """
    global _KEY
    _KEY = secrets.token_bytes(32)
    return _KEY


def _canonical(claims: dict) -> bytes:
    """The exact bytes that get signed.

    Canonical JSON, so the encoding is total: every value round-trips unambiguously no
    matter what characters a filename contains. A delimiter-joined string would not —
    the first file named ``a|b.png`` would let its own name forge a claim boundary.
    """
    return json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()


def _mac(claims: dict) -> str:
    raw = hmac.new(key(), _canonical(claims), sha256).digest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _claims(
    *, act: str, task_id: str, file_id: str, agent_id: int,
    name: str, content_type: str, kind: str, exp: int,
) -> dict:
    """One claim set, built the SAME way whether we are signing or verifying.

    Signing and verification must never build this differently — the whole guarantee is
    that the string checked is the string minted — so there is one constructor and both
    sides call it.
    """
    return {
        "v": 1,
        "act": act,
        "task": task_id,
        "fid": str(file_id or ""),
        "aid": int(agent_id),
        "name": name or "",
        "ct": content_type or "",
        "kind": kind or "",
        "exp": int(exp),
    }


def _query(claims: dict) -> str:
    """The signed claims as a query string — WITHOUT act/task/fid.

    Those three are deliberately absent: the route recovers them from the method and the
    path, so they cannot be tampered with in transit without breaking the signature. See
    the module docstring.

    EMPTY CLAIMS ARE OMITTED rather than sent as ``sb_kind=``. A read URL has no name, type
    or kind, and a blank query parameter is the kind of thing an intermediary is entitled
    to drop — after which the URL would fail verification for reasons nobody could see.
    ``verify`` reconstructs a missing parameter as the same empty value it was minted with,
    so the two sides still agree. The test that first caught this was using
    ``parse_qs`` (which drops blanks by default) rather than Starlette's ``QueryParams``
    (which keeps them); the fix is not to depend on the difference at all.

    ``aid`` is dropped only when it is 0 — the "no seat" value a read URL carries — while a
    file genuinely NAMED ``"0"`` keeps its ``sb_name=0``, because the test is emptiness,
    not falsiness.
    """
    q = {
        v: claims[k] for k, v in _PARAMS.items()
        if not (claims[k] == "" or (k == "aid" and claims[k] == 0))
    }
    q[SIG_PARAM] = _mac(claims)
    return urlencode(q)


def upload_url(
    base_url: str, *, task_id: str, agent_id: int, name: str, content_type: str,
    kind: str = "", ttl: int = SIGNED_URL_TTL, now: float | None = None,
) -> tuple[str, int]:
    """A signed ``POST /files/{task_id}`` URL. Returns ``(url, expires_at)``.

    Scoped to ONE task, ONE action, and one (name, content_type, kind) — it cannot read
    anything, cannot reach another task, and cannot store the bytes under a different
    type than the one the agent asked for.
    """
    exp = int((time.time() if now is None else now) + ttl)
    claims = _claims(
        act=UPLOAD, task_id=task_id, file_id="", agent_id=agent_id,
        name=name, content_type=content_type, kind=kind, exp=exp,
    )
    return f"{base_url.rstrip('/')}/files/{task_id}?{_query(claims)}", exp


def read_url(
    base_url: str, *, task_id: str, file_id: int,
    ttl: int = SIGNED_URL_TTL, now: float | None = None,
) -> tuple[str, int]:
    """A signed ``GET /files/{task_id}/{file_id}`` URL. Returns ``(url, expires_at)``.

    Scoped to ONE file. It cannot upload, and it cannot be walked to the file next door:
    the id is in the path and the path is signed.

    NOT scoped to a seat (``aid`` is 0), unlike an upload URL — a decision, not an
    omission. Every agent on a task may already read every file on it; that is what
    ``get_file`` does. So a read URL grants exactly what its holder's task membership
    already grants, narrowed to one file and 15 minutes. Binding a seat as well would buy
    nothing, and would cost the local surface its ``list_files(task)`` signature, which has
    no agent argument because local mode has no identity to check.
    """
    exp = int((time.time() if now is None else now) + ttl)
    claims = _claims(
        act=READ, task_id=task_id, file_id=str(file_id), agent_id=0,
        name="", content_type="", kind="", exp=exp,
    )
    return f"{base_url.rstrip('/')}/files/{task_id}/{file_id}?{_query(claims)}", exp


def present(query) -> bool:
    """Is this request even claiming to carry a signature?

    Used by the routes to choose a lane. A request with no signature falls through to the
    bearer-token / ``?agent=`` path exactly as before — signed URLs ADD a way in, they do
    not replace one.
    """
    return bool(query.get(SIG_PARAM))


def verify(query, *, act: str, task_id: str, file_id: str = "", now: float | None = None) -> dict | None:
    """Verify a signed URL, returning its claims — or ``None`` for ANY failure.

    ``act``, ``task_id`` and ``file_id`` come from the ROUTE (its method and its path
    params), never from the query. That is the scoping: a read URL replayed on the upload
    route is verified against ``act="upload"`` and fails, and the same holds for a URL
    dragged onto another task's path or another file's id.

    One return value for expired, tampered, wrong-action, wrong-task, wrong-file and
    signed-before-a-key-rotation, because the CALLER must not be able to tell them apart
    — see the route handlers, where a read failure is the same 404 a missing file gets so
    ids stay unprobeable.
    """
    sig = (query.get(SIG_PARAM) or "").strip()
    if not sig:
        return None
    try:
        claims = _claims(
            act=act,
            task_id=task_id,
            file_id=file_id,
            agent_id=int(query.get(_PARAMS["aid"]) or 0),
            name=query.get(_PARAMS["name"]) or "",
            content_type=query.get(_PARAMS["ct"]) or "",
            kind=query.get(_PARAMS["kind"]) or "",
            exp=int(query.get(_PARAMS["exp"]) or 0),
        )
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(_mac(claims), sig):
        return None
    # Checked AFTER the MAC, so an expiry is only ever read off a claim set we have
    # already proved we wrote. Checking it first would be answering a question about
    # attacker-controlled input.
    if claims["exp"] <= (time.time() if now is None else now):
        return None
    return claims
