"""Pairing: invite → join → tokens (SPEC §9).

The chicken-and-egg solver. You cannot call MCP tools without a bearer token, and
you get one here. Three pieces:

- ``redeem_invite`` — the server-side core, given an open connection. Testable
  without a running server.
- ``register_pairing_routes`` — mounts ``POST /pair`` on the FastMCP app. This route
  is UNAUTHENTICATED by design (SPEC §2): it is what hands out the first token, so it
  cannot require one. Its protection is the single-use, 15-minute invite code.
- ``join`` — the buddy-side network client (stdlib ``urllib`` only). It runs on the
  buddy's machine, which has no broker database — it just POSTs and parses JSON.

Dual tokens are issued deliberately (SPEC §9): an ``agent_token`` for MCP (scoped to
one ``{task, role}``) and a separate ``viewer_token`` for the read-only dashboard
(scoped to one task, independently revocable). A leaked dashboard link cannot send
messages or reach other tasks.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import audit, seats
from .config import Config, get_config
from .db import connect
from .identity import new_agent_token, new_viewer_token, sha256_hex
from .rules import RULES_OF_ENGAGEMENT


def redeem_invite(
    conn: sqlite3.Connection,
    code: str,
    agent_name: str,
    pubkey: str | None = None,
) -> dict:
    """Redeem a single-use invite: mint an agent + viewer, burn the invite.

    The invite is looked up by the sha256 of the presented code (raw codes are never
    stored). It is rejected if unknown, expired, or already used. On success this
    creates the agent row for the invite's SEAT — ``invites.role`` holds a handle, so
    the DB's ``UNIQUE(task_id, handle) WHERE revoked_at IS NULL`` is what rejects a
    second buddy claiming a taken seat — plus a task-scoped viewer, then burns the
    invite (``used_at``). Returns RAW tokens; only their hashes are persisted.

    ``agent_name`` is the human's chosen DISPLAY name, and it must be free on this
    TASK — one Sarah per task, case-insensitively — because the whole point of a name
    is that ``@sarah`` is safe to type. It is still not a key: handles remain the
    identifier, so nothing about a name can move a signature.

    **The check on insert is the guarantee.** Everything from the invite lookup to the
    commit runs inside one ``BEGIN IMMEDIATE`` transaction, which takes the write lock
    up front — so a second joiner cannot slip between "is this name free?" and the
    INSERT that claims it. SQLite has no ``SELECT FOR UPDATE`` and needs none. Any
    availability check outside this transaction (the join page's live tick, the CLI's
    pre-prompt) is advisory and may race.
    """
    now = time.time()
    conn.commit()  # start from a clean slate so the BEGIN below is ours alone
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _redeem(conn, code, agent_name, pubkey, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return result


def _redeem(conn, code, agent_name, pubkey, now) -> dict:
    """The body of :func:`redeem_invite`, inside its immediate transaction."""
    invite = conn.execute(
        "SELECT id, task_id, role, expires_at, used_at FROM invites WHERE code_hash = ?",
        (sha256_hex(code),),
    ).fetchone()

    if invite is None:
        raise ValueError("invalid invite code")
    if invite["used_at"] is not None:
        raise ValueError("invite code has already been used")
    if invite["expires_at"] < now:
        raise ValueError("invite code has expired")

    task_id = invite["task_id"]
    handle = invite["role"]

    # A closed task grants no new access, even to an invite minted before it closed
    # (SPEC §9: "close kills everything for that task"). close_task also burns
    # outstanding invites; this is the belt-and-suspenders check.
    closed = conn.execute("SELECT closed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if closed is None or closed["closed_at"] is not None:
        raise ValueError(f"task '{task_id}' is closed")

    # Guard the one-live-agent-per-SEAT rule with a clear message before hitting the
    # DB constraint. Note it is per seat, not per role type: a second frontend seat is
    # a different handle and pairs perfectly happily alongside the first.
    taken = conn.execute(
        "SELECT 1 FROM agents WHERE task_id = ? AND handle = ? AND revoked_at IS NULL",
        (task_id, handle),
    ).fetchone()
    if taken is not None:
        raise ValueError(f"role '{handle}' on task '{task_id}' is already paired")

    # One Sarah per task. This read and the INSERT below are in the same immediate
    # transaction, which is what makes it a guarantee rather than a hint.
    agent_name = seats.assert_name_free(conn, task_id, agent_name)

    # The seat's ROLE TYPE — what kind of work it does — drives the briefing and the
    # pre-flight question set. Falls back to the handle for a task written before the
    # split, where the two were the same string.
    role = seats.seat_roles_of(conn, task_id).get(handle, handle)

    agent_token = new_agent_token()
    viewer_token = new_viewer_token()
    ttl = get_config().agent_token_ttl
    expires_at = (now + ttl) if ttl else None

    try:
        # The closed-task check above is a fast-path message, but it is a read that
        # races close_task (a concurrent close could commit between it and this
        # write). Make the INSERT itself conditional on the task still being open, in
        # one statement: SQLite serializes writers, so either close_task committed
        # first (0 rows insert here → we abort) or we commit first (close_task's sweep
        # then revokes this row). No live agent can survive on a closed task.
        cur = conn.execute(
            "INSERT INTO agents (task_id, name, role, handle, token_hash, pubkey, "
            "created_at, expires_at) "
            "SELECT ?,?,?,?,?,?,?,? WHERE EXISTS "
            "(SELECT 1 FROM tasks WHERE id = ? AND closed_at IS NULL)",
            (task_id, agent_name, role, handle, sha256_hex(agent_token), pubkey, now,
             expires_at, task_id),
        )
    except sqlite3.IntegrityError as e:
        # The partial unique index (one LIVE agent per SEAT) — safety net against a
        # race between the pre-check and the INSERT. Revoked rows no longer collide,
        # so a re-pair after revocation succeeds.
        raise ValueError(f"role '{handle}' on task '{task_id}' is already taken") from e
    if cur.rowcount != 1:
        # Task was closed between the pre-check and this write — close_task won.
        raise ValueError(f"task '{task_id}' is closed")
    agent_id = cur.lastrowid

    conn.execute(
        "INSERT INTO viewers (task_id, label, token_hash, created_at) VALUES (?,?,?,?)",
        (task_id, agent_name, sha256_hex(viewer_token), now),
    )
    conn.execute("UPDATE invites SET used_at = ? WHERE id = ?", (now, invite["id"]))
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, "token", json.dumps({"text": f"Paired '{agent_name}' as {handle}"}), now),
    )

    return {
        "agent_token": agent_token,
        "viewer_token": viewer_token,
        "task_id": task_id,
        # `role` stays the SEAT — it is what the agent is addressed by, what its token
        # stamps onto every call, and what every existing caller already prints. The
        # kind of work rides alongside as `role_type`.
        "role": handle,
        "handle": handle,
        "role_type": role,
        "expires_at": expires_at,
    }


def invite_preview(conn: sqlite3.Connection, code: str, name: str | None = None) -> dict:
    """What the joiner is walking into — the task, their SEAT, and who is already here.

    Gated on the invite VALIDATING first, and deliberately so: a join surface that
    listed the cast before checking the code would turn a random guess into a team
    directory. The failure modes are the same three ``redeem_invite`` raises (unknown /
    used / expired), with the same wording, so a joiner never sees two vocabularies for
    one problem.

    Reads only, and it does NOT burn the invite — the code is redeemed on the click,
    never on the load. The seat and its role type come off the invite, so both are known
    before the joiner types anything: invites are per SEAT.

    ``name``, when given, is checked for availability. That answer is **advisory** — it
    is not inside ``redeem_invite``'s transaction and may race a simultaneous joiner.
    The insert-time check in :func:`seats.assert_name_free` is the guarantee; this only
    saves a round trip and a retyped form.
    """
    now = time.time()
    invite = conn.execute(
        "SELECT task_id, role, expires_at, used_at FROM invites WHERE code_hash = ?",
        (sha256_hex(code),),
    ).fetchone()
    if invite is None:
        raise ValueError("invalid invite code")
    if invite["used_at"] is not None:
        raise ValueError("invite code has already been used")
    if invite["expires_at"] < now:
        raise ValueError("invite code has expired")

    task_id, handle = invite["task_id"], invite["role"]
    task = conn.execute(
        "SELECT title, closed_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task is None or task["closed_at"] is not None:
        raise ValueError(f"task '{task_id}' is closed")

    # Only the seats somebody has actually joined: "already here" is a list of people
    # to avoid colliding with, and an empty seat has no name to collide with. The
    # unjoined ones are the ROSTER's job, and the host's.
    cast = [
        {"seat": r["seat"], "role": r["role"], "name": r["name"]}
        for r in seats.roster(conn, task_id)
        if r["joined"] and r["name"]
    ]
    out = {
        "task_id": task_id,
        "title": task["title"] or task_id,
        "seat": handle,
        "role_type": seats.seat_roles_of(conn, task_id).get(handle, handle),
        "cast": cast,
        "taken_names": sorted({seats.fold_name(c["name"]) for c in cast}),
    }
    if name is not None:
        try:
            seats.assert_name_free(conn, task_id, name)
        except ValueError as e:
            out["name_available"] = False
            out["name_error"] = str(e)
        else:
            out["name_available"] = True
            out["name_error"] = None
    return out


# --- /pair abuse controls ---------------------------------------------------
# The endpoint is unauthenticated (it hands out the first token), so it gets a
# lightweight fixed-window per-IP cap: a backstop against invite-guessing / a free
# DB-write DoS, and a signal when someone is hammering it. Invite entropy already
# makes brute force impractical; this bounds attempts and abuse.
PAIR_RATE_MAX = 20
PAIR_RATE_WINDOW = 60.0
_PAIR_HITS: dict[str, list[float]] = {}

# /pair/preview gets its own, much higher cap on the same window. It writes nothing and
# still demands a valid invite code, and it is called on every debounced keystroke of
# the name field — sharing the pairing cap would lock a joiner out of the form for
# typing their own name.
PREVIEW_RATE_MAX = 240
_PREVIEW_HITS: dict[str, list[float]] = {}

# agent_name is buddy-controlled and surfaces in the message envelope and Slack;
# constrain charset/length (defense-in-depth on top of the escaping in _wrap/notify).
_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")


def _rate_limited(
    ip: str, now: float, hits_by_ip: dict | None = None, cap: int = PAIR_RATE_MAX
) -> bool:
    bucket = _PAIR_HITS if hits_by_ip is None else hits_by_ip
    hits = [t for t in bucket.get(ip, ()) if now - t < PAIR_RATE_WINDOW]
    hits.append(now)
    bucket[ip] = hits
    return len(hits) > cap


def _valid_agent_name(name) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def register_pairing_routes(mcp: FastMCP, cfg: Config) -> None:
    """Mount ``POST /pair`` and ``POST /pair/preview`` on the FastMCP app (SPEC §2).

    Unauthenticated by design — they are protected by the single-use invite code, not a
    bearer token (this is where the first token comes from).
    """

    @mcp.custom_route("/pair/preview", methods=["POST"])
    async def pair_preview(request: Request) -> Response:
        """Read-only: the task, the seat this invite fills, and who is already here.

        Same protection as ``/pair`` — a valid invite code — because that is exactly
        what stops a random code from enumerating the team. It burns nothing, so the
        joiner can look before they leap, and the page can tell them their chosen name
        is taken BEFORE they spend the single-use invite on finding out.
        """
        ip = request.client.host if request.client else "?"
        if _rate_limited(ip, time.time(), _PREVIEW_HITS, PREVIEW_RATE_MAX):
            audit.event("preview_ratelimit", ip=ip)
            return JSONResponse(
                {"error": "too many attempts; slow down and retry shortly"},
                status_code=429,
            )
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        code = payload.get("code")
        name = payload.get("name")
        if not code:
            return JSONResponse({"error": "code is required"}, status_code=400)
        # An unusable name is a form-validation answer, not a 400: the page wants to
        # render the same ✗ it renders for a taken one, beside a still-usable preview.
        name_error = None
        if name is not None and not _valid_agent_name(name):
            name_error = "a name is 1-64 chars: letters, digits, space, . _ -"
            name = None

        conn = connect()
        try:
            out = invite_preview(conn, code, name)
        except ValueError as e:
            audit.event("preview_fail", ip=ip)
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()
        if name_error:
            out["name_available"] = False
            out["name_error"] = name_error
        return JSONResponse(out)

    @mcp.custom_route("/pair", methods=["POST"])
    async def pair(request: Request) -> Response:
        ip = request.client.host if request.client else "?"
        if _rate_limited(ip, time.time()):
            audit.event("pair_ratelimit", ip=ip)
            return JSONResponse(
                {"error": "too many pairing attempts; slow down and retry shortly"},
                status_code=429,
            )
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        code = payload.get("code")
        agent_name = payload.get("agent_name")
        pubkey = payload.get("pubkey")
        if not code or not agent_name:
            return JSONResponse({"error": "code and agent_name are required"}, status_code=400)
        if not _valid_agent_name(agent_name):
            return JSONResponse(
                {"error": "agent_name must be 1-64 chars: letters, digits, space, . _ -"},
                status_code=400,
            )

        conn = connect()
        try:
            result = redeem_invite(conn, code, agent_name, pubkey=pubkey)
            # The briefing prompt is mode-aware (contract vs debug); read the task's
            # mode off the same connection before it closes, defaulting to "contract"
            # if the row/column is somehow missing.
            row = conn.execute(
                "SELECT mode FROM tasks WHERE id = ?", (result["task_id"],)
            ).fetchone()
            mode = (row["mode"] if row and row["mode"] else "contract")
        except ValueError as e:
            # Invalid/expired/used invite, or a taken role — a client error, not a 500.
            audit.event("pair_fail", ip=ip, name=agent_name)
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()
        audit.event("pair_ok", ip=ip, task=result["task_id"], role=result["role"], name=agent_name)

        # Lazy import: onboarding imports pairing at module top, so importing it here
        # (inside the handler) avoids a circular import.
        from .onboarding import connect_clients, role_prompt

        viewer_token = result["viewer_token"]
        mcp_url = f"{cfg.base_url}/mcp"
        return JSONResponse(
            {
                "agent_token": result["agent_token"],
                "viewer_token": viewer_token,
                "task_id": result["task_id"],
                "role": result["role"],
                "handle": result["handle"],
                "role_type": result["role_type"],
                "mcp_url": mcp_url,
                "dashboard_url": f"{cfg.base_url}/ui?v={viewer_token}",
                "expires_at": result["expires_at"],  # None = token never expires
                # The briefing the operator pastes into their Claude agent.
                "prompt": role_prompt(
                    result["role_type"], result["task_id"], mode, handle=result["handle"]
                ),
                # The broker's non-negotiable charter, handed to the agent at setup.
                "rules": RULES_OF_ENGAGEMENT,
                # How to connect, for EVERY client we support — rendered here rather
                # than in the page, for the same reason `prompt` and `rules` are: these
                # literals fail silently when they drift, so there is exactly one copy.
                # Stamped with cfg.base_url like mcp_url above, so a client that reached
                # us through a tunnel rebases them the same way (see connect_clients).
                "clients": connect_clients(mcp_url, result["agent_token"]),
            }
        )


def _rebase(server_url: str | None, connected_base: str) -> str | None:
    """Rewrite a broker-returned URL onto the origin the buddy actually reached.

    The ``/pair`` route stamps ``mcp_url``/``dashboard_url`` from the broker's OWN
    config (``cfg.base_url``), which is loopback whenever the broker sits behind a
    tunnel it doesn't know about (``ngrok http 8787`` / ``tailscale serve 8787``).
    But the buddy reached the broker at ``connected_base`` — the invite link's origin
    — so that origin, not the broker's self-view, is the one the buddy must use. We
    keep the server's path + query (the dashboard's ``?v=<token>`` rides along) and
    swap only the scheme + host.
    """
    if not server_url:
        return server_url
    b = urlsplit(connected_base.rstrip("/"))
    s = urlsplit(server_url)
    return urlunsplit((b.scheme, b.netloc, s.path, s.query, s.fragment))


def preview(url: str, code: str, name: str | None = None) -> dict | None:
    """Buddy-side client for ``POST {url}/pair/preview``.

    Returns the preview dict, or ``None`` on ANY failure — including a broker too old
    to serve the route. Callers treat it as an enhancement: without it the CLI still
    pairs exactly as it always did, it just cannot show the cast first. It never prints
    to stderr for that reason; a missing preview is not an error the joiner caused.
    """
    endpoint = f"{url.rstrip('/')}/pair/preview"
    payload: dict = {"code": code}
    if name is not None:
        payload["name"] = name
    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # A 4xx here is a real answer about the INVITE (unknown / used / expired);
        # surface it so the joiner is not told to pick a name for a dead code.
        try:
            detail = json.loads(e.read().decode()).get("error")
        except Exception:
            detail = None
        return {"error": detail or str(e.reason)} if detail else None
    except Exception:  # noqa: BLE001 — an old broker 404s; that is not the joiner's problem
        return None
    return data if isinstance(data, dict) else None


def join(url: str, code: str, name: str, pubkey: str | None = None) -> dict | None:
    """Buddy-side client: POST the invite to ``{url}/pair`` and return the tokens.

    Runs on the buddy's machine with no local database — a pure network client, so it
    uses stdlib ``urllib`` (no ``requests`` dependency). Returns the fields the CLI
    prints, or ``None`` with a clear stderr message on any failure.
    """
    endpoint = f"{url.rstrip('/')}/pair"
    body = json.dumps({"code": code, "agent_name": name, "pubkey": pubkey}).encode()
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # The broker returns {"error": "..."} with a 4xx — surface that reason.
        try:
            detail = json.loads(e.read().decode()).get("error", e.reason)
        except Exception:
            detail = e.reason
        print(f"error: pairing failed ({e.code}): {detail}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"error: could not reach broker at {endpoint}: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001 — a bad/garbled response must not traceback
        print(f"error: unexpected pairing failure: {e}", file=sys.stderr)
        return None

    # Rebase the broker's self-stamped URLs onto the origin we actually reached, so a
    # broker behind a tunnel it doesn't know about still hands back a reachable mcp_url.
    mcp_url = _rebase(data.get("mcp_url"), url) or f"{url.rstrip('/')}/mcp"
    agent_token = data.get("agent_token")

    # Connect instructions: re-RENDER them locally against the rebased mcp_url rather
    # than rebasing the server's copy by string surgery. We're in Python here with the
    # same renderer the broker used, so a local render is exact and cannot go stale —
    # and it still works against an older broker that doesn't send `clients` at all.
    from .onboarding import connect_clients

    return {
        "task_id": data.get("task_id"),
        # `role` is the SEAT the buddy took. `role_type` is what kind of work it does —
        # absent from an older broker's reply, in which case they are the same thing.
        "role": data.get("role"),
        "handle": data.get("handle") or data.get("role"),
        "role_type": data.get("role_type") or data.get("role"),
        "mcp_url": mcp_url,
        "agent_token": agent_token,
        "dashboard_url": _rebase(data.get("dashboard_url"), url),
        "expires_at": data.get("expires_at"),
        "rules": data.get("rules"),  # surface the charter to the buddy (CLI prints it)
        "clients": connect_clients(mcp_url, agent_token or ""),
    }
