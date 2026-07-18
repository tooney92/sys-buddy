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

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import audit
from .config import Config
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
    creates the agent row for the invite's ``{task, role}`` — the DB's
    ``UNIQUE(task_id, role)`` enforces the fixed-cast rule, so a second buddy trying
    to claim a taken role is rejected clearly — plus a task-scoped viewer, then burns
    the invite (``used_at``). Returns RAW tokens; only their hashes are persisted.
    """
    now = time.time()
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
    role = invite["role"]

    # A closed task grants no new access, even to an invite minted before it closed
    # (SPEC §9: "close kills everything for that task"). close_task also burns
    # outstanding invites; this is the belt-and-suspenders check.
    closed = conn.execute("SELECT closed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if closed is None or closed["closed_at"] is not None:
        raise ValueError(f"task '{task_id}' is closed")

    # Guard the fixed-cast rule with a clear message before hitting the DB constraint.
    taken = conn.execute(
        "SELECT 1 FROM agents WHERE task_id = ? AND role = ? AND revoked_at IS NULL",
        (task_id, role),
    ).fetchone()
    if taken is not None:
        raise ValueError(f"role '{role}' on task '{task_id}' is already paired")

    agent_token = new_agent_token()
    viewer_token = new_viewer_token()

    try:
        # The closed-task check above is a fast-path message, but it is a read that
        # races close_task (a concurrent close could commit between it and this
        # write). Make the INSERT itself conditional on the task still being open, in
        # one statement: SQLite serializes writers, so either close_task committed
        # first (0 rows insert here → we abort) or we commit first (close_task's sweep
        # then revokes this row). No live agent can survive on a closed task.
        cur = conn.execute(
            "INSERT INTO agents (task_id, name, role, token_hash, pubkey, created_at) "
            "SELECT ?,?,?,?,?,? WHERE EXISTS "
            "(SELECT 1 FROM tasks WHERE id = ? AND closed_at IS NULL)",
            (task_id, agent_name, role, sha256_hex(agent_token), pubkey, now, task_id),
        )
    except sqlite3.IntegrityError as e:
        # The partial unique index (one LIVE agent per role) — safety net against a
        # race between the pre-check and the INSERT. Revoked rows no longer collide,
        # so a re-pair after revocation succeeds.
        raise ValueError(f"role '{role}' on task '{task_id}' is already taken") from e
    if cur.rowcount != 1:
        # Task was closed between the pre-check and this write — close_task won.
        conn.rollback()
        raise ValueError(f"task '{task_id}' is closed")
    agent_id = cur.lastrowid

    conn.execute(
        "INSERT INTO viewers (task_id, label, token_hash, created_at) VALUES (?,?,?,?)",
        (task_id, agent_name, sha256_hex(viewer_token), now),
    )
    conn.execute("UPDATE invites SET used_at = ? WHERE id = ?", (now, invite["id"]))
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, "token", json.dumps({"text": f"Paired '{agent_name}' as {role}"}), now),
    )
    conn.commit()

    return {
        "agent_token": agent_token,
        "viewer_token": viewer_token,
        "task_id": task_id,
        "role": role,
    }


# --- /pair abuse controls ---------------------------------------------------
# The endpoint is unauthenticated (it hands out the first token), so it gets a
# lightweight fixed-window per-IP cap: a backstop against invite-guessing / a free
# DB-write DoS, and a signal when someone is hammering it. Invite entropy already
# makes brute force impractical; this bounds attempts and abuse.
PAIR_RATE_MAX = 20
PAIR_RATE_WINDOW = 60.0
_PAIR_HITS: dict[str, list[float]] = {}

# agent_name is buddy-controlled and surfaces in the message envelope and Slack;
# constrain charset/length (defense-in-depth on top of the escaping in _wrap/notify).
_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")


def _rate_limited(ip: str, now: float) -> bool:
    hits = [t for t in _PAIR_HITS.get(ip, ()) if now - t < PAIR_RATE_WINDOW]
    hits.append(now)
    _PAIR_HITS[ip] = hits
    return len(hits) > PAIR_RATE_MAX


def _valid_agent_name(name) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def register_pairing_routes(mcp: FastMCP, cfg: Config) -> None:
    """Mount ``POST /pair`` on the FastMCP app (SPEC §2).

    Unauthenticated by design — it is protected by the single-use invite code, not a
    bearer token (it is where the first token comes from).
    """

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
        except ValueError as e:
            # Invalid/expired/used invite, or a taken role — a client error, not a 500.
            audit.event("pair_fail", ip=ip, name=agent_name)
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()
        audit.event("pair_ok", ip=ip, task=result["task_id"], role=result["role"], name=agent_name)

        viewer_token = result["viewer_token"]
        return JSONResponse(
            {
                "agent_token": result["agent_token"],
                "viewer_token": viewer_token,
                "task_id": result["task_id"],
                "role": result["role"],
                "mcp_url": f"{cfg.base_url}/mcp",
                "dashboard_url": f"{cfg.base_url}/ui?v={viewer_token}",
                # The broker's non-negotiable charter, handed to the agent at setup.
                "rules": RULES_OF_ENGAGEMENT,
            }
        )


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

    return {
        "task_id": data.get("task_id"),
        "role": data.get("role"),
        "mcp_url": data.get("mcp_url"),
        "agent_token": data.get("agent_token"),
        "dashboard_url": data.get("dashboard_url"),
    }
