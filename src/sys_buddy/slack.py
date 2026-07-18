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
