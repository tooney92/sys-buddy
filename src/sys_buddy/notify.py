"""Human notification — one seam, many channels.

Everything that pings a human goes through here: the ~11 lifecycle fire points in
``state.py`` and the agent-facing ``notify_human`` tool. Callers name no channel; they
say "tell the humans this" and the registry fans out to whatever is configured.

Why a registry rather than a second import. Before this, ``state.py`` called
``slack.notify`` directly at every fire point. Adding Telegram that way would have meant
a parallel call beside each one — eleven places to keep in step, and a twelfth channel
later would mean touching all of them again. The channel list belongs in one place, and
the call sites should not know it exists.

The contract every channel keeps, inherited from ``slack``: **best-effort, never
raises**. A notification is a courtesy — a dead webhook or a network blip must never
break an agent's turn or a state transition (SPEC §14).

Credentials for every channel are process-local and NEVER persisted. The db holds only
sha256 hashes; a webhook URL and a bot token must both be replayed verbatim to work, so
storing either would put the first plaintext secret at rest. They come from the
environment or the Host screen and die with the process.
"""

from __future__ import annotations

from . import slack

# Name → module. TO ADD A CHANNEL: write a module with exactly two functions —
#   is_configured() -> bool     # True only when it can actually deliver. Half-configured
#                               # is NOT configured: advertising a channel that will
#                               # silently drop the message is worse than showing nothing.
#   notify(text) -> str         # a human-readable status. MUST NOT raise, ever — a
#                               # notification is a courtesy and cannot break a turn or a
#                               # state transition (SPEC §14). Swallow your own errors and
#                               # return them as text, the way `slack` does.
# — then add it here. Nothing else changes: the ~11 fire points in state.py and the
# notify_human tool already call through this registry and name no channel.
#
# Credentials stay PROCESS-LOCAL and are never persisted, whatever the channel: the db
# holds only sha256 hashes, and anything replayable verbatim (a webhook URL, a bot token)
# would be the first plaintext secret at rest.
#
# Order is the order results come back in and carries no priority meaning — every
# configured channel gets the message.
CHANNELS: dict[str, object] = {
    "slack": slack,
}


def active() -> list[str]:
    """Names of the channels configured on THIS process, in registry order.

    This is what the dashboard is told — names only, never a credential. Callers should
    treat an empty list as "nobody will be paged", which is a real state worth showing:
    an unconfigured channel looks exactly like a working one until something important
    fails to arrive.
    """
    return [name for name, mod in CHANNELS.items() if mod.is_configured()]


def is_configured() -> bool:
    """True if AT LEAST ONE channel is configured."""
    return bool(active())


def send(text: str) -> dict[str, str]:
    """Send ``text`` to every configured channel. Never raises.

    Returns ``{channel_name: status_string}`` for the channels that were configured —
    an unconfigured channel is absent rather than reported as failed, because "you never
    set up Telegram" is not a failure the agent should relay.

    When NOTHING is configured the dict is empty; :func:`summarize` turns that into the
    "tell your human directly" line an agent can put in its final response.
    """
    results: dict[str, str] = {}
    for name in active():
        try:
            results[name] = CHANNELS[name].notify(text)
        except Exception as e:  # noqa: BLE001 — a channel must never derail the caller
            # Defensive: every channel already swallows its own errors, so reaching here
            # means a channel broke its contract. Still not the caller's problem.
            results[name] = f"{name} notification failed ({type(e).__name__})."
    return results


def summarize(results: dict[str, str]) -> str:
    """One line an agent can hand to its human, from :func:`send`'s result.

    Kept separate from ``send`` so the state machine can fire and forget while the
    agent-facing tool gets something to say.
    """
    if not results:
        return (
            "No notification channel is configured — tell the human in your final "
            "response instead."
        )
    return " ".join(f"[{name}] {status}" for name, status in results.items())
