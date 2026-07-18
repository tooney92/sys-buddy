from __future__ import annotations

"""Native desktop shell for sys-buddy (pywebview).

WHY this exists:
  The core sys-buddy experience is browser-based (the read-only dashboard at
  ``/ui``), but the *onboarding* flow — a Host spinning up a task, a Buddy
  pairing their agent, wiring Claude to the broker — is a first-run, local,
  credential-touching experience. Shipping that as a plain web page would mean
  the user juggling a terminal, a browser tab, and copy/pasted tokens.

  Instead we wrap it in a native window over the operating system's own webview
  (WebKit on macOS, WebView2 on Windows, WebKitGTK on Linux). This buys us:

    * A real application window the user launches like any other app — no
      "open this localhost URL" dance, no stray server to remember to kill.
    * A JS<->Python bridge (``js_api``): the HTML calls ``pywebview.api.<fn>()``
      and we run trusted Python (the ``onboarding`` engine) on this side, so
      secrets and local config writes never leave the machine or ride over HTTP.
    * Reuse of our existing HTML/CSS design language — the page is just another
      self-contained single-page app (``gui_app.html``), same fonts/palette as ``ui.html``.

  This module is the SKELETON (milestones M0/M1): it opens the home screen and
  proves the bridge is live. The full Host/Buddy flows are layered on later by
  fleshing out ``GuiApi`` and adding more screens; the engine those methods call
  lives in the sibling :mod:`sys_buddy.onboarding` module.
"""

import os
import threading
import time
import urllib.request

import webview

from . import onboarding

# The host runs the broker in-process on loopback. REMOTE mode is required (not
# local): only then do agents authenticate by bearer token and get the parameter-free
# tool surface the buddy's Claude expects. http is fine on 127.0.0.1; a two-machine
# host exposes it via an https tunnel (M5). Kept in a daemon thread so it dies with
# the app — no stray server to remember to kill.
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 8787
BASE_URL = f"http://{BROKER_HOST}:{BROKER_PORT}"

_broker_thread: threading.Thread | None = None


def _broker_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/ui", timeout=1):
            return True
    except Exception:
        return False


def _run_broker() -> None:
    from .config import Config
    from .server import run_server

    # build_server sets the global config + inits the db, then serves (blocking).
    run_server(Config(mode="remote", host=BROKER_HOST, port=BROKER_PORT, public_url=None))


def _ensure_broker(timeout: float = 30.0) -> bool:
    """Start the in-process broker once and wait until it answers. Idempotent."""
    global _broker_thread
    if _broker_is_up():
        return True
    if _broker_thread is None or not _broker_thread.is_alive():
        _broker_thread = threading.Thread(target=_run_broker, daemon=True)
        _broker_thread.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _broker_is_up():
            return True
        time.sleep(0.4)
    return False


class GuiApi:
    """JS<->Python bridge exposed to the webview page as ``pywebview.api``.

    Every method is reachable from the page via ``pywebview.api.<name>(...)``
    and returns a JSON-serializable value (dict or string). These are thin
    wrappers over :mod:`sys_buddy.onboarding`; the bridge must NEVER raise
    across into JavaScript, so each method catches everything and returns an
    ``{"error": <str>}`` dict instead of propagating the exception.
    """

    def ping(self) -> str:
        """Liveness probe used by the page to prove the bridge is wired up."""
        return "pong"

    def pair(self, link: str, name: str) -> dict:
        """Pair this agent against an invite ``link`` under ``name`` (Buddy flow)."""
        try:
            return onboarding.pair(link, name)
        except Exception as exc:  # never raise across the bridge
            return {"error": str(exc)}

    def configure_claude(self, mcp_url: str, token: str) -> dict:
        """Wire the local Claude CLI to the broker MCP endpoint with ``token``."""
        try:
            return onboarding.configure_claude(mcp_url, token)
        except Exception as exc:
            return {"error": str(exc)}

    def role_prompt(self, role: str, task_id: str) -> str:
        """Return the kickoff prompt an agent pastes for ``role`` on ``task_id``."""
        try:
            return onboarding.role_prompt(role, task_id)
        except Exception as exc:
            return {"error": str(exc)}

    def join_flow(self, link: str, name: str) -> dict:
        """One-call Buddy onboarding: pair via ``link`` then wire Claude to the broker."""
        try:
            return onboarding.join_flow(link, name)
        except Exception as exc:  # never raise across the bridge
            return {"error": str(exc)}

    def create_task(self, task_id: str, roles: list, title: str = "") -> dict:
        """Create a host-side task with the given ``roles`` (Host flow)."""
        try:
            return onboarding.host_create_task(task_id, roles, title=title or None)
        except Exception as exc:
            return {"error": str(exc)}

    def invite_link(self, task_id: str, role: str, base_url: str) -> str:
        """Mint the invite link a Buddy uses to pair into ``role`` on ``task_id``."""
        try:
            return onboarding.host_invite_link(task_id, role, base_url)
        except Exception as exc:
            return {"error": str(exc)}

    def start_host(self, task_id: str, roles: list, title: str = "", public_url: str = "",
                   trusted: bool = False) -> dict:
        """Host flow: start the in-process broker (once), create the task, and mint an
        invite link per role. ``public_url`` (optional) is the host's tunnel origin
        (e.g. ngrok) so a buddy on another machine can reach the broker — the invite
        links embed it. Blank = same-machine (loopback). ``trusted`` marks it a private
        overlay (Tailscale/WireGuard) so an http origin is allowed (the network already
        encrypts). Returns the host_setup dict, or an error the UI shows."""
        try:
            base = (public_url or "").strip().rstrip("/")
            if base and not trusted and not base.lower().startswith("https://"):
                return {"ok": False, "error": "Public URL must be https:// — or tick 'private network' if it's a Tailscale/WireGuard overlay. Leave blank for same-machine."}
            if not _ensure_broker():
                return {"ok": False, "error": f"broker did not come up on {BASE_URL} — is port {BROKER_PORT} free?"}
            if base:
                # Exposed beyond this machine → default agent tokens to a 24h TTL so a
                # leaked token self-expires (agents refresh with rotate_token).
                from .config import get_config
                if get_config().agent_token_ttl is None:
                    get_config().agent_token_ttl = 24 * 3600
            return onboarding.host_setup(task_id, list(roles), base or BASE_URL, title=title or None)
        except Exception as exc:
            return {"error": str(exc)}

    def open_dashboard(self, url: str) -> dict:
        """Open the live read-only dashboard in its own native window (a separate
        top-level window, so the broker's frame-ancestors CSP doesn't block it)."""
        try:
            webview.create_window("sys-buddy · dashboard", url=url, width=1200, height=860)
            return {"ok": True}
        except Exception as exc:
            return {"error": str(exc)}


def _home_path() -> str:
    """Absolute path to the bundled home screen, resolved next to this file."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_app.html")


def run_gui() -> None:
    """Open the native sys-buddy window on the home screen and start the loop.

    Blocks until the user closes the window (``webview.start()`` runs the GUI
    event loop on the main thread).
    """
    webview.create_window(
        "sys-buddy",
        url=_home_path(),
        js_api=GuiApi(),
        width=1000,
        height=720,
        min_size=(720, 560),
    )
    webview.start()
