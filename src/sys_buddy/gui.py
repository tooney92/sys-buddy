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
      self-contained file (``gui_home.html``), same fonts and palette as ``ui.html``.

  This module is the SKELETON (milestones M0/M1): it opens the home screen and
  proves the bridge is live. The full Host/Buddy flows are layered on later by
  fleshing out ``GuiApi`` and adding more screens; the engine those methods call
  lives in the sibling :mod:`sys_buddy.onboarding` module.
"""

import os

import webview

from . import onboarding


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


def _home_path() -> str:
    """Absolute path to the bundled home screen, resolved next to this file."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_home.html")


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
