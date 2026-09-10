"""Session health — what is actually wrong before you pick a collaboration back up.

WHY THIS IS A REPORT AND NOT A BUTTON. Resuming a day-old session went wrong in four
independent ways at once: the broker's tunnel had rotated, the agent tokens were hours from
expiry, the staging target was dead, and a peer had mislaid his dashboard link. Only the
LAST of those needed a re-invite — yet "resume" as a single action would have revoked a
healthy seat to fix any of them. A button whose ordinary behaviour destroys a working
credential is one people learn to fear.

So resume is a DIAGNOSIS. Every row says what it checked, what it found, and the one
targeted fix for that row — with the CLI beside it, because a host who can see the command
can also run it when the app is not in front of them. Revoke-and-re-invite still exists, at
the bottom, behind a confirm, where a last resort belongs.

Every row is PROBED, never inferred (see :mod:`tunnels`).
"""

from __future__ import annotations

import time

from . import admin, seats, tunnels
from .db import connect

OK, WARN, DEAD, INFO = "ok", "warn", "dead", "info"


def _row(key, label, status, detail, fix=None, cli=None, action=None, args=None):
    """One probed fact.

    ``fix`` is what the button SAYS, ``cli`` the same move at a terminal, and ``action``
    the machine key a surface dispatches on — kept separate from ``key`` so the UI never
    has to infer intent by string-matching a row id. ``needs`` names a value the host must
    supply before the fix can run (only the staging target does).
    """
    return {"key": key, "label": label, "status": status, "detail": detail,
            "fix": fix, "cli": cli, "action": action, "args": args or {}}


def session_report(task: str, *, port: int = 8787, known_public_url: str | None = None) -> dict:
    """Everything a host needs to see before resuming ``task``.

    ``known_public_url`` is what the broker last believed its public origin to be; passing
    it turns "here is the tunnel" into "the tunnel MOVED", which is the fact that actually
    matters to a peer whose config is now stale.
    """
    conn = connect()
    try:
        admin._assert_task(conn, task)
        trow = conn.execute(
            "SELECT title, state, staging_url, dev_url FROM tasks WHERE id=?", (task,)
        ).fetchone()
        roster = seats.roster(conn, task)
        agents = conn.execute(
            "SELECT COALESCE(handle, role) AS seat, name, expires_at, role "
            "FROM agents WHERE task_id=? AND revoked_at IS NULL AND token_hash IS NOT NULL "
            "ORDER BY id",
            (task,),
        ).fetchall()
    finally:
        conn.close()

    rows = [
        _broker_row(port),
        _public_url_row(port, known_public_url),
        _staging_row(task, trow["staging_url"]),
        _tokens_row(task, agents),
    ]
    rows += _seat_rows(task, roster)

    worst = DEAD if any(r["status"] == DEAD for r in rows) else (
        WARN if any(r["status"] == WARN for r in rows) else OK)
    return {"task": task, "title": trow["title"], "state": trow["state"],
            "overall": worst, "rows": rows}


def _broker_row(port: int) -> dict:
    p = tunnels.probe(f"http://127.0.0.1:{port}/ui", timeout=4.0)
    if p["alive"]:
        return _row("broker", "Broker", OK, f"up on 127.0.0.1:{port}")
    return _row("broker", "Broker", DEAD, f"nothing answering on 127.0.0.1:{port}",
                fix="Start the broker", cli="sys-buddy serve", action="start_broker")


def _public_url_row(port: int, known: str | None) -> dict:
    """Read the live tunnel rather than trusting what anyone last typed in.

    The paste-the-URL step is precisely where a rotated tunnel stops being noticed, so the
    answer comes from ngrok itself. Matching on the forwarded PORT is what distinguishes
    "a tunnel exists" from "a tunnel to the broker exists"; one aimed elsewhere shows up on
    a peer's machine as an inexplicable auth failure.
    """
    t = tunnels.ngrok_for_port(port)
    if t is None:
        others = tunnels.discover_ngrok()
        if others:
            return _row("public_url", "Public URL", WARN,
                        f"ngrok is running but forwards to {others[0]['forwards_to']}, "
                        f"not the broker on :{port}",
                        fix="Point the tunnel at the broker",
                        cli=f"ngrok http {port}")
        return _row("public_url", "Public URL", INFO,
                    "no ngrok tunnel found — peers off this machine cannot reach the broker",
                    fix="Start a tunnel", cli=f"ngrok http {port}")
    live = t["public_url"]
    if known and known.rstrip("/") != live.rstrip("/"):
        return _row("public_url", "Public URL", WARN,
                    f"CHANGED since last session — now {live} (was {known}). "
                    f"Every peer's MCP config still points at the old one.",
                    fix="Copy the re-point message for peers", cli=None,
                    action="repoint", args={"url": live})
    return _row("public_url", "Public URL", OK, live)


def _staging_row(task: str, staging: str | None) -> dict:
    if not staging:
        return _row("staging", "Staging target", INFO, "not set",
                    fix="Set the deployment target",
                    cli=f"sys-buddy task staging-url {task} <url>",
                    action="set_staging", args={"task": task, "needs": "url"})
    p = tunnels.probe(staging, timeout=10.0)
    if p["alive"]:
        # Any HTTP answer means something is listening. A 404 at / is a healthy API.
        return _row("staging", "Staging target", OK, f"{staging} ({p['detail']})")
    return _row("staging", "Staging target", DEAD,
                f"{staging} is {p['detail']} — the one URL your agents may fetch",
                fix="Set the new tunnel URL",
                cli=f"sys-buddy task staging-url {task} <new-url>",
                action="set_staging", args={"task": task, "needs": "url"})


def _tokens_row(task: str, agents) -> dict:
    """Agent tokens on a tunnelled broker carry a 24h TTL, and an agent whose token lapses
    cannot rotate it or even report `stuck` — it is locked out of the tools that would let
    it say so. Warn while there is still time to act."""
    now = time.time()
    live = [a for a in agents if a["expires_at"] is not None]
    if not agents:
        return _row("tokens", "Agent tokens", INFO, "no joined agents yet")
    if not live:
        return _row("tokens", "Agent tokens", OK, "no expiry set")
    soonest = min(a["expires_at"] for a in live)
    hours = (soonest - now) / 3600.0
    if hours <= 0:
        return _row("tokens", "Agent tokens", DEAD, "EXPIRED — agents are locked out",
                    fix="Extend them", cli=f"sys-buddy task extend-tokens {task} --never",
                    action="extend_tokens", args={"task": task})
    if hours < 8:
        return _row("tokens", "Agent tokens", WARN,
                    f"soonest expires in {hours:.1f}h",
                    fix="Extend them", cli=f"sys-buddy task extend-tokens {task} --hours 24",
                    action="extend_tokens", args={"task": task})
    return _row("tokens", "Agent tokens", OK, f"soonest expires in {hours:.0f}h")


def _seat_rows(task: str, roster: list[dict]) -> list[dict]:
    """One row per seat, because "who can actually get back in" is the other half of
    resuming — and a lost dashboard link is now recoverable without touching the seat."""
    out = []
    for r in roster:
        seat = r["seat"]
        if not r["joined"]:
            if r.get("invite_pending"):
                out.append(_row(f"seat:{seat}", f"@{seat}", WARN, "invited, not joined yet",
                                fix="Reissue the invite",
                                cli=f"sys-buddy invite --task {task} --role {seat}",
                                action="invite", args={"task": task, "role": seat}))
            else:
                out.append(_row(f"seat:{seat}", f"@{seat}", INFO, "seat unfilled",
                                fix="Invite someone",
                                cli=f"sys-buddy invite --task {task} --role {seat}",
                                action="invite", args={"task": task, "role": seat}))
            continue
        who = r.get("name") or seat
        is_guest = r.get("role") == seats.GUEST_ROLE
        verb = "guest-link" if is_guest else "viewer-link"
        out.append(_row(f"seat:{seat}", f"@{seat}", OK, f"joined · {who}",
                        fix="Copy their dashboard link",
                        cli=f"sys-buddy task {verb} {task} {who}",
                        action="viewer_link", args={"task": task, "who": who}))
    return out
