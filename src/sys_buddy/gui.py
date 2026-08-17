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
    # SLACK_WEBHOOK_URL is picked up here so the desktop app honours it the same way the
    # CLI does — without this the app built a Config with no webhook and every ping went
    # nowhere, silently. The Host screen can also set it later via set_slack_webhook;
    # either way it lives on the process config only and is never written to the db.
    run_server(
        Config(
            mode="remote",
            host=BROKER_HOST,
            port=BROKER_PORT,
            public_url=None,
            slack_webhook=os.environ.get("SLACK_WEBHOOK_URL") or None,
        )
    )


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

    def check_version(self) -> dict:
        """Version-awareness banner data (see :mod:`sys_buddy.updates`).

        Takes NO argument, deliberately. It used to take the page's opt-in toggle and
        default to False, so a caller that simply asked "what's my version situation"
        got the local-only answer and the update banner never appeared. There is no flag
        to forget now: `updates.status` decides, and it checks.

        Always returns a dict; network failures degrade to ``None`` fields rather than
        raising across the bridge.
        """
        try:
            from . import updates

            return updates.status(BASE_URL)
        except Exception as exc:  # never raise across the bridge
            return {"error": str(exc)}

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

    def connect_clients(self, mcp_url: str, token: str) -> list | dict:
        """Every supported client's connect instructions for this seat.

        The page renders from this rather than spelling the commands/JSON out in JS —
        a second copy of those literals silently drifts, and the field names differ per
        client in ways that fail quietly (see :func:`onboarding.connect_clients`).
        ``start_host``/``join_flow`` already embed the same list, so this is only for a
        page that wants to re-render for a seat it already holds.
        """
        try:
            return onboarding.connect_clients(mcp_url, token)
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

    def staging_url(self, task_id: str, todo: int = 0) -> dict:
        """Read the deployment target in force for a task (or one deliverable)."""
        try:
            return onboarding.host_staging_url(task_id, todo or None)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_staging_url(
        self, task_id: str, url: str = "", todo: int = 0, clear: bool = False
    ) -> dict:
        """Change the deployment target — the ONE URL agents may fetch. HOST ONLY.

        Not a renegotiation: no contract, version or signature moves, because the target
        is configuration resolved live on every read. That is what makes a rotated ngrok
        URL a one-field fix rather than a re-signed shape, and it is also why no agent
        tool exists beside this one — nothing an agent says can reach this value.
        """
        try:
            return onboarding.host_set_staging_url(task_id, url, todo or None, clear)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def invite_link(self, task_id: str, role: str, base_url: str) -> str:
        """Mint the invite link a Buddy uses to pair into ``role`` on ``task_id``."""
        try:
            return onboarding.host_invite_link(task_id, role, base_url)
        except Exception as exc:
            return {"error": str(exc)}

    def start_host(self, title: str, roles: list, host_role: str = "", public_url: str = "",
                   mode: str = "contract", staging_url: str = "", dev_url: str = "") -> dict:
        """Host flow: start the in-process broker (once), create the task (id derived
        from ``title``), mint invite links for the buddy role(s), and — when
        ``host_role`` is given — seat the host's OWN agent on that role and auto-wire
        their Claude. ``public_url`` (optional) is the host's origin so a buddy on
        another machine can reach the broker — the invite links embed it. Blank =
        same machine (loopback). Both remote paths present an https origin: a public
        tunnel (``ngrok http 8787``) or a private network proxy (``tailscale serve
        8787``), so the GUI requires https for any remote origin.

        ``staging_url`` (optional) is the deployment target the human nominates on this
        screen — the URL their peers actually build and test against. It rides onto the
        task so the producer agent inherits it at ``propose_contract`` instead of
        inventing an aspirational one. Blank public_url means same-machine, and THAT is
        what lets the target be ``http://localhost:PORT``: the broker itself always runs
        in remote mode here (token auth needs it), so auth mode says nothing about who
        can reach the target. Returns the host_setup dict (with ``host_seat`` when
        host_role is set), or an error."""
        try:
            title = (title or "").strip()
            if not title:
                return {"ok": False, "error": "Give the task a title first."}
            base = (public_url or "").strip().rstrip("/")
            # ngrok and `tailscale serve` both hand you an https origin; raw-http private
            # overlays are a CLI-only power move (`serve --trusted-network`). So the GUI
            # simply requires https for anything beyond this machine — tokens never ride
            # a cleartext origin.
            if base and not base.lower().startswith("https://"):
                return {"ok": False, "error": "Public URL must be an https:// address — from `ngrok http 8787` or `tailscale serve 8787`. Leave it blank if your buddy is on this same computer."}
            if not _ensure_broker():
                return {"ok": False, "error": f"broker did not come up on {BASE_URL} — is port {BROKER_PORT} free?"}
            if base:
                # Exposed beyond this machine → default agent tokens to a 24h TTL so a
                # leaked token self-expires (agents refresh with rotate_token).
                from .config import get_config
                if get_config().agent_token_ttl is None:
                    get_config().agent_token_ttl = 24 * 3600
            host_role = (host_role or "").strip() or None
            res = onboarding.host_setup(
                None, list(roles), base or BASE_URL, title=title, mode=mode,
                host_role=host_role, public_url=base or None,
                staging_url=(staging_url or "").strip() or None,
                dev_url=(dev_url or "").strip() or None,
            )
            # The host is on the same box as the broker, so try to auto-register their
            # own seat's MCP (same as the buddy flow). The command is shown regardless,
            # since the GUI's Claude config/scope may differ from the host's terminal.
            seat = res.get("host_seat") if isinstance(res, dict) else None
            if seat:
                cfg = onboarding.configure_claude(seat["mcp_url"], seat["agent_token"])
                seat["config_ok"] = cfg["ok"]
                seat["config_detail"] = cfg["detail"]
                seat["config_command"] = cfg["command"]
            return res
        except Exception as exc:
            return {"error": str(exc)}

    def slack_status(self) -> dict:
        """Whether Slack is armed on THIS process. Returns a boolean and a source hint —
        never the webhook itself, which is a bearer credential (anyone holding it can post
        to the channel indefinitely). ``source`` lets the Host screen say "picked up from
        your environment" instead of showing an empty box over a working webhook."""
        from . import slack
        from .config import get_config

        return {
            "ok": True,
            "active": slack.is_configured(),
            "source": "env" if os.environ.get("SLACK_WEBHOOK_URL") else (
                "session" if get_config().slack_webhook else "none"
            ),
        }

    def set_slack_webhook(self, url: str) -> dict:
        """Arm (or clear) Slack for this session. NOT persisted — by design.

        Every credential in the db is a sha256 hash; a webhook must be replayed verbatim
        to work, so storing it would make it the first plaintext secret at rest. It lives
        on the process config and dies with the process. Pass "" to disarm.

        Safe to call after the broker is up: the config object is process-global, so the
        host can turn Slack on mid-session without restarting anything.
        """
        from . import slack
        from .config import get_config

        url = (url or "").strip()
        if not url:
            get_config().slack_webhook = None
            return {"ok": True, "active": False}
        err = slack.validate_webhook(url)
        if err:
            return {"ok": False, "error": err}
        get_config().slack_webhook = url
        return {"ok": True, "active": True}

    def test_slack(self) -> dict:
        """Send a real ping so the host gets PROOF before they rely on it.

        A webhook that 404s (revoked, wrong workspace, typo'd) is indistinguishable from
        a working one until something important fails to arrive — and `notify` is
        deliberately best-effort, so nothing else would ever surface the error.
        """
        from . import slack
        from .config import get_config

        if not get_config().slack_webhook:
            return {"ok": False, "error": "No webhook set yet."}
        result = slack.notify("sys-buddy is connected to this channel. 👋")
        return {"ok": result.startswith("Human notified"), "detail": result}

    def open_dashboard(self, url: str) -> dict:
        """Open the live read-only dashboard in its own native window (a separate
        top-level window, so the broker's frame-ancestors CSP doesn't block it).

        The window gets a MINIMAL bridge (``_DashApi``, only ``new_task``) so the
        host can jump back to the Start-a-task screen from the dashboard — the
        dashboard stays read-only for data (that all flows through the broker's
        read-only ``/api``); this bridge only navigates the local app UI."""
        try:
            webview.create_window(
                "sys-buddy · dashboard", url=url, js_api=_DashApi(), width=1200, height=860
            )
            return {"ok": True}
        except Exception as exc:
            return {"error": str(exc)}


class _DashApi:
    """Tiny bridge exposed to the dashboard window. Deliberately NOT ``GuiApi`` —
    the dashboard loads from the broker origin (possibly a remote tunnel), so it must
    not reach start_host/pairing. Its one method just deep-links the local app back to
    the host screen."""

    def new_task(self) -> dict:
        """Bring the main app window to the 'Start a task' screen (host add-task
        deep-link). Never raises across the bridge."""
        try:
            main = webview.windows[0]  # the GuiApi window, created first in run_gui
            main.evaluate_js("window.__sbGotoHost && window.__sbGotoHost()")
            try:
                main.restore()  # un-minimise / bring forward, best-effort per platform
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
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
    # debug=True enables the native web inspector (right-click → Inspect Element),
    # so a JS error that would otherwise make a click silently "do nothing" surfaces
    # in the console. Gate on SYS_BUDDY_GUI_DEBUG so shipping builds stay clean.
    webview.start(debug=bool(os.environ.get("SYS_BUDDY_GUI_DEBUG")))
