"""Slack notifications (SPEC §8/§14, and the third ported bug fix).

The predecessor's ``notify_human`` had no error handling — a Slack timeout raised
straight out of the tool and derailed the agent's turn. Here every network failure
is swallowed and turned into a *soft* status string the caller can log or relay.
Notifying a human is best-effort; it must never break the workflow.

Uses stdlib ``urllib`` only (no new dependency). The webhook URL is a secret and is
read from config (populated from ``$SLACK_WEBHOOK_URL`` in remote mode) — never
stored in the database or echoed back.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlparse

from .config import get_config

# Agent-supplied status text flows into the *other* operator's Slack channel. Slack
# renders `<`, `>`, `&` as mrkdwn control chars (links `<url|label>`, specials like
# `<!channel>`), so a buddy could otherwise inject a clickable phishing link or a
# channel-wide mention. Escape them per Slack's own rules and cap the length.
MAX_SLACK_CHARS = 3000


def _mrkdwn_safe(text: str) -> str:
    safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return safe if len(safe) <= MAX_SLACK_CHARS else safe[:MAX_SLACK_CHARS] + "…"


def validate_webhook(url: str) -> str | None:
    """Return an error string for a bad webhook URL, or None when it's usable.

    Shared by every entry point (Host screen, CLI env var, notify) so they cannot
    disagree about what's acceptable. https is required for the same reason as in
    ``notify``: task content must never go out in cleartext or to a non-web scheme.
    """
    url = (url or "").strip()
    if not url:
        return "Paste a Slack webhook URL."
    parsed = urlparse(url)
    if (parsed.scheme or "").lower() != "https":
        return "The webhook must be an https:// URL."
    if not parsed.netloc:
        return "That doesn't look like a URL."
    # Deliberately NOT pinned to hooks.slack.com: self-hosted relays and test doubles are
    # legitimate, and baking a vendor hostname into a validator ages badly. https + a real
    # host is the security boundary; the rest is the operator's call.
    return None


def is_configured() -> bool:
    """Whether a usable webhook is set on the CURRENT process config.

    The dashboard surfaces this as a boolean and nothing more — the URL itself is a
    bearer credential and must never reach the browser.
    """
    webhook = get_config().slack_webhook
    return bool(webhook) and validate_webhook(webhook) is None


def notify(text: str) -> str:
    """Post ``text`` to the configured Slack webhook. Never raises.

    Returns a human-readable status: notified, not-configured, rejected (bad URL),
    or a soft failure the caller should surface in its final response instead.
    """
    webhook = get_config().slack_webhook
    if not webhook:
        return "No Slack webhook configured — tell the human in your final response instead."
    # Require https so a misconfigured/typo'd webhook can't ship task content over
    # cleartext http or to a non-web scheme (file://, gopher://, …).
    if (urlparse(webhook).scheme or "").lower() != "https":
        return "Slack webhook must be an https URL — not sending; tell the human in your final response instead."
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"text": _mrkdwn_safe(text)}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return "Human notified on Slack."
    except Exception as e:  # noqa: BLE001 — a Slack failure must never derail the turn
        return f"Slack notification failed ({type(e).__name__}); tell the human in your final response instead."
