"""Tunnel discovery and reachability — finding out what is actually true right now.

A collaboration runs behind TWO ephemeral addresses that rotate independently:

* the **broker's public URL** (how a peer reaches ``/mcp`` and ``/ui``), and
* the **staging URL** (where the built app lives, the one URL agents may fetch).

Free-tier tunnels hand out a fresh random hostname on every restart, so rotation is the
NORMAL case, not an incident. A morning spent picking a collaboration back up found both
had moved overnight: peers' MCP configs pointed at a dead broker, and six locked contracts
pointed at a dead app. Neither was visible anywhere — the host had to curl things by hand
to find out.

The rule this module exists to enforce: **probe, don't infer.** Ask the address whether it
answers, rather than reasoning about whether it ought to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# ngrok's local agent API. Present whenever ngrok runs, no matter WHO started it — a
# terminal, a script, or the desktop app — which is exactly why we can read the live URL
# without owning the process.
NGROK_AGENT_API = "http://127.0.0.1:4040/api/tunnels"


def probe(url: str, timeout: float = 8.0) -> dict:
    """Is there anything answering at ``url``?

    THE NUANCE, learned the hard way: a healthy Rails app returns **404 at `/`**, and a
    dead tunnel returns nothing at all. So "alive" means WE GOT AN HTTP RESPONSE — any
    status — and "dead" means the connection failed or timed out. Checking for 200 would
    flag a perfectly good backend as broken every single session.

    Never raises: a probe that blows up is a probe that reports ``dead``, because the
    caller is a health check and an exception there is just a worse way to say "no".
    """
    if not url:
        return {"url": url, "alive": False, "status": None, "detail": "no URL set"}
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "sys-buddy"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "alive": True, "status": resp.status, "detail": "answered"}
    except urllib.error.HTTPError as e:
        # A 4xx/5xx IS an answer — something is listening and speaking HTTP.
        return {"url": url, "alive": True, "status": e.code, "detail": f"answered {e.code}"}
    except Exception as e:  # noqa: BLE001 — every failure mode means the same thing here
        return {"url": url, "alive": False, "status": None, "detail": _reason(e)}


def _reason(exc: Exception) -> str:
    """A short human reason, so a health row can say WHY rather than just 'dead'."""
    if isinstance(exc, urllib.error.URLError):
        return f"unreachable ({getattr(exc, 'reason', exc)})"
    return f"unreachable ({type(exc).__name__})"


def discover_ngrok(timeout: float = 3.0) -> list[dict]:
    """Every tunnel ngrok currently serves — ``[{public_url, forwards_to, name}]``.

    Read from the local agent API rather than asked of the human, because the human is the
    part that goes stale: the paste-the-URL-into-the-app step is exactly where a rotated
    tunnel stops being noticed. This works whoever started ngrok, so it costs no process
    supervision — the app simply asks what is true.

    ``forwards_to`` is the quiet prize: it lets a caller check the tunnel points at the
    BROKER's port. A tunnel aimed at the wrong port otherwise surfaces as an inexplicable
    auth error on the peer's machine.

    Returns ``[]`` when ngrok is not running — not an error, just an absence.
    """
    try:
        with urllib.request.urlopen(NGROK_AGENT_API, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — no agent API simply means "no ngrok here"
        return []
    out: list[dict] = []
    for t in data.get("tunnels") or []:
        public = t.get("public_url") or ""
        # ngrok publishes http and https for the same tunnel; the token rides this, so
        # only the https one is ever the right answer to hand a peer.
        if public.startswith("http://"):
            continue
        out.append({
            "name": t.get("name"),
            "public_url": public,
            "forwards_to": (t.get("config") or {}).get("addr") or "",
        })
    return out


def ngrok_for_port(port: int, timeout: float = 3.0) -> dict | None:
    """The ngrok tunnel forwarding to ``port`` on this machine, or None.

    Matching on the port is what turns "a tunnel exists" into "a tunnel to THE BROKER
    exists" — the difference between a working invite link and one that quietly points a
    peer somewhere else entirely.
    """
    for t in discover_ngrok(timeout=timeout):
        addr = t.get("forwards_to") or ""
        if addr.rsplit(":", 1)[-1] == str(port):
            return t
    return None
