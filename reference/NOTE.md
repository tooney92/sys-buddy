# Reference — the working predecessor

`agent_bus.py` is a ~130-line FastMCP message bus that **works today**. It coordinates
multiple Claude Code agents across repos on a single machine. sys-buddy is this idea
taken remote, between different humans.

**Port from it; don't rewrite blind.** These parts work and are load-bearing:

- **The long-poll loop** (`wait_for_message`) — Claude pauses on every tool call until
  it returns, so an agent parked in this is *asleep-but-listening* and wakes within ~2s
  of a sibling posting. This is the mechanism that makes coordination feel automatic
  instead of nagged. Keep the 540s cap (under Claude Code's ~9min MCP tool timeout).
- **SQLite as the mailbox** — messages persist, so nothing is lost while nobody listens.
- **FastMCP over HTTP transport** — stdio would spawn a private server per session and
  the agents would never see each other's messages. This is not a preference; it's the
  difference between working and not.

## Three known bugs — fix while porting (SPEC §14)

1. No WAL mode. The poll loop opens a SQLite connection every 2 seconds.
2. `notify_human` has no error handling — a Slack timeout raises and derails the
   agent's turn.
3. `_fetch_unread` marks messages read on fetch, so a crashed session loses them.
   Split `delivered_at` / `acked_at`.

## Also read AGENT_BUS_GUIDE.md

Especially the Troubleshooting table and the Naming section. Those are real bugs
someone already paid for: PEP 668 / Homebrew Python, multi-account
`CLAUDE_CONFIG_DIR` registration, mailbox collisions from duplicate agent names, and
why "message sent but nothing happens" is usually correct behaviour.

The guide's core insight, worth carrying into sys-buddy verbatim:
**the MCP gives agents tools; CLAUDE.md gives them the habit.**
