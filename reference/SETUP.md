# Agent Bus — Setup

Replaces your manual `channel.md` workflow. One shared server, both agents connect to it.

## 1. Run the server (once, any folder)

```bash
pip install fastmcp
python agent_bus.py
```

Keep it running in a terminal (or add it to launchd/pm2). It listens on `http://127.0.0.1:8787/mcp` and stores messages in `agent_bus.db`.

## 2. Register the MCP in each repo

```bash
cd /path/to/repo-a
claude mcp add --transport http agent-bus http://127.0.0.1:8787/mcp

cd /path/to/repo-b
claude mcp add --transport http agent-bus http://127.0.0.1:8787/mcp
```

Important: it must be HTTP, not stdio. A stdio server would spawn a separate copy per Claude session and the agents would never see each other's messages.

## 3. Make it automatic — CLAUDE.md

The MCP gives agents the tools; CLAUDE.md tells them when to use them without you asking. Add to each repo's `CLAUDE.md` (swap names per repo):

```markdown
## Inter-agent communication

You are agent "repo-a". Agent "repo-b" works in a sibling repo.
Use the agent-bus MCP tools to coordinate:

- At the START of every task, call `check_messages(agent="repo-a")`.
- When you finish work the other agent depends on, or need something
  from them, call `send_message(sender="repo-a", recipient="repo-b", ...)`.
- If you are blocked waiting on repo-b, call
  `wait_for_message(agent="repo-a", timeout_seconds=120)` instead of
  stopping — it returns as soon as they reply. Retry a few times before
  giving up.
```

## 4. Tools available to the agents

| Tool | Purpose |
|---|---|
| `send_message(sender, recipient, content)` | Post to the other agent |
| `check_messages(agent)` | Non-blocking read of unread messages |
| `wait_for_message(agent, timeout_seconds)` | Blocks until a reply arrives — this is what makes it automatic |
| `channel_history(limit)` | Recent conversation for context |

## Notes

- `wait_for_message` is capped at ~9 min per call so it stays under Claude Code's MCP tool timeout; the CLAUDE.md instruction tells agents to re-call it.
- Even stronger enforcement: add a Claude Code hook (e.g. a `Stop` hook that runs a script checking `agent_bus.db` for unread messages and blocks the stop with a "you have a message" reason). Ask me if you want that.
- Messages persist in SQLite, so nothing is lost if a session restarts.
