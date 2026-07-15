# Agent Bus — Multi-Agent Claude Code Setup

How we let multiple Claude Code agents (one per repo) coordinate with each other
automatically: negotiate API contracts, hand off work, run tests at the right moment,
and notify us on Slack when a feature is verified or they're stuck.

Before this, agents coordinated through a shared `CHANNEL.md` file that a human had to
tell them to read and write. The bus replaces that with an MCP server they use on
their own.

---

## Architecture

```
Terminal 1                    Terminal 3                    Terminal 2
Claude agent "backend"        agent-bus (FastMCP)           Claude agent "frontend"
repo-backend/          ◀────▶ localhost:8787         ◀────▶ repo-frontend/
CLAUDE.md = its rules         SQLite mailbox                CLAUDE.md = its rules
                                                            + Playwright MCP (tests)
```

- **agent-bus** is a ~130-line Python FastMCP server. It exposes 5 tools and stores
  messages in a SQLite file (`agent_bus.db`). One instance serves ALL projects.
- Each **agent** is a normal Claude Code session, one per repo, launched from that
  repo's folder. Its repo's `CLAUDE.md` tells it its agent name and when to use the
  bus — that's what makes coordination automatic.
- A message is a database row: `sender, recipient, content, read`. An agent only
  reads rows addressed to its own name, so one server cleanly serves many projects
  as long as agent names are unique (see Naming).

### The 5 tools

| Tool | What it does |
|---|---|
| `send_message(sender, recipient, content)` | Post a message to another agent |
| `check_messages(agent)` | Non-blocking: return my unread messages, mark them read |
| `wait_for_message(agent, timeout_seconds)` | **Long-poll**: block until a message arrives (or timeout). This is what makes agents react within ~2s of each other |
| `channel_history(limit)` | Recent traffic across all agents, for context |
| `notify_human(sender, message)` | Post to a Slack incoming webhook. Terminal events only: feature VERIFIED, or stuck after 3 fix cycles |

### Why long-polling matters

Claude pauses on every tool call until it returns. `wait_for_message` checks the
mailbox every 2s server-side and only responds when mail exists — so an agent
"parked" in it is effectively asleep-but-listening, and reacts the moment a sibling
posts. An idle session at the prompt, by contrast, notices nothing until prompted.
Timeouts are capped at ~9 min per call (Claude Code tool timeout); CLAUDE.md tells
agents to retry a few times, then give up gracefully. Messages persist in SQLite,
so nothing is ever lost while nobody is listening.

---

## Setup (one-time)

### 1. Server

```bash
# fastmcp can't install into Homebrew's Python (PEP 668) — use a venv:
python3 -m venv ~/agent-bus-venv
~/agent-bus-venv/bin/pip install fastmcp

mkdir -p ~/agent-bus
# put agent_bus.py in ~/agent-bus/ (file alongside this guide)

~/agent-bus-venv/bin/python ~/agent-bus/agent_bus.py
```

Success looks like a FastMCP banner + `Uvicorn running on http://127.0.0.1:8787`.
Leave the terminal open — the bus only works while this runs.

Convenience alias (add to `~/.zshrc`), including the Slack webhook (see Notifications):

```bash
alias agent-bus='SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ" ~/agent-bus-venv/bin/python ~/agent-bus/agent_bus.py'
```

Then starting the server is just `agent-bus`.

### 2. Register the MCP with Claude Code

```bash
claude mcp add --scope user --transport http agent-bus http://127.0.0.1:8787/mcp
```

- `--scope user` = every project under that Claude account; run it from any folder.
- **Must be HTTP transport, not stdio.** A stdio server spawns a private copy per
  session — agents would never see each other's messages. HTTP means one shared
  process, one shared mailbox.
- **Multiple Claude accounts gotcha:** registration lives in the account's config.
  If you launch sessions via an alias like
  `claude-personal='CLAUDE_CONFIG_DIR=~/.claude-personal claude'`, register with that
  same prefix: `CLAUDE_CONFIG_DIR=~/.claude-personal claude mcp add ...`.
  Verify inside a session with `/mcp` → look for `agent-bus ✔ connected · 5 tools`
  (also shows WHICH config file the session actually reads).

### 3. Slack notifications (optional but great)

1. api.slack.com/apps → Create App → From scratch → name it, pick workspace
2. Incoming Webhooks → Activate → Add New Webhook to Workspace → choose channel/DM
3. Copy the webhook URL into the alias above (or the env var at server start)

**The webhook URL is a secret** — keep it in `~/.zshrc`/env only. Never commit it or
put it in CLAUDE.md. If it leaks, regenerate it in the Slack app settings.

Without a webhook everything still works; agents just report outcomes in their final
response instead of Slack.

---

## Per-repo wiring: CLAUDE.md

The MCP gives agents tools; **CLAUDE.md gives them the habit**. Each repo's CLAUDE.md
(auto-loaded at session start) gets a section like this — swap names per repo:

```markdown
## Inter-agent communication (agent-bus MCP)

You are agent "myproj-frontend". Sibling agent: "myproj-backend".
Coordinate via the agent-bus MCP tools:

- START of every task: call check_messages(agent="myproj-frontend") and
  act on anything addressed to you.
- Before reporting any task complete: call check_messages again — a
  sibling may have replied mid-task.
- When you finish work a sibling depends on, or need something (missing
  field, new endpoint, different shape, auth), call
  send_message(sender="myproj-frontend", recipient="myproj-backend", ...).
  Be concrete: route, field, type, example payload.
- Batch related content into ONE message (4 questions = one message with
  4 bullets, not 4 sends).
- If blocked waiting on a sibling: wait_for_message(agent="myproj-frontend",
  timeout_seconds=120) instead of stopping; retry a few times before giving up.
- channel_history(limit=20) shows recent traffic for context.

### Integration testing rules  (client repos)
- NEVER run e2e tests (Playwright/etc.) during integration work.
- When the backend messages that something changed, integrate fully first;
  only after integration is complete, run the tests once as final verification.
- Report results via send_message: pass → confirm; backend-caused failure →
  include the failing request/response, then wait_for_message for the fix
  and re-test.

### Stop conditions
- Feature DONE = tests pass. Send the backend a final message starting with
  "VERIFIED:" and call notify_human(sender="myproj-frontend", ...) with a
  one-line summary. Then stop.
- Stuck = 3 fix cycles on the same failure. Stop and notify_human describing
  what was tried.
```

The backend variant mirrors this, plus: **an API change usually affects every client —
message each affected agent separately**; and "a client message starting with
VERIFIED: means that client is done — stop waiting on them."

### Naming (critical with multiple projects)

A name IS a mailbox. Two projects both using "frontend" would steal each other's
messages. Prefix per project: `moxie-frontend`, `moxie-backend`, `lightdey-api`,
`lightdey-tracker`, `lightdey-mobile`. Monorepo? Put per-agent CLAUDE.md files in
the subfolders (`frontend/CLAUDE.md`, `backend/CLAUDE.md`) and ALWAYS launch each
agent from its own subfolder. Separate repos? One CLAUDE.md each. Any number of
agents can share the bus — isolation is by name. (Note: it's convention, not
security — fine for one developer's local agents.)

---

## What a feature looks like

Login feature, two prompts from the human, everything else automatic:

1. **You → backend session:** "Build POST /api/auth/login taking {email, password} →
   {token, user}. Tests. Message frontend the contract when done, then wait for their
   results."
2. **You → frontend session:** "Backend is building login. Build the page now with the
   API call mocked behind a single function; when done, check messages for the contract
   (wait_for_message if not arrived), swap the mock, follow your testing rules."
3. Backend finishes → `send_message` with the contract → parks in `wait_for_message`.
4. Frontend (parked) wakes in ~2s, integrates, THEN runs Playwright once.
5. Test fails (contract says `token`, API returns `access_token`) → frontend messages
   the failing response to backend → parks.
6. Backend wakes, fixes, replies → frontend re-tests → green →
   `send_message("VERIFIED: ...")` + `notify_human` → **Slack: `[frontend] VERIFIED:
   login e2e green`**. Both stop.

If they can't converge, the 3-cycle cap stops the ping-pong and Slacks you instead.

### Day-to-day habits

- `ch` (shorthand we bake into CLAUDE.md) = "check your messages and act."
- An idle session can't wake itself. If nobody's parked in wait_for_message, a
  human nudge (`ch`, or any new task) delivers pending mail — works from the phone too.
- Long planning session? Don't park an agent on standby; timeouts will just cycle.
  Send the message when ready and nudge the recipient.
- Interrupting a parked agent (Esc) is always safe — mail persists in SQLite.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Agent says bus tools are missing | `/mcp` in the session: not listed → registered under the wrong account/config, re-add with the right `CLAUDE_CONFIG_DIR`; listed but failed → server not running |
| `command not found: pip` | Use `pip3`, or the venv's pip |
| `externally-managed-environment` | Homebrew Python refusing global installs — use the venv (step 1) |
| Registered but session doesn't see it | MCP connections are made at session START — restart the session |
| Two agents read each other's mail | Duplicate agent names across projects — prefix them |
| Message sent but nothing happens | Normal: the recipient is idle. Mail waits in SQLite until they check (task start, `ch`, or a parked wait) |
| Wipe history | Stop server, delete `~/agent-bus/agent_bus.db`, restart |

---

## Files

- `agent_bus.py` — the whole server (ships alongside this guide)
- `agent_bus.db` — SQLite mailbox, auto-created next to the script
- Per-repo `CLAUDE.md` — agent identity + habits (see template above)

Ideas for later: launchd auto-start at login; a Stop hook that blocks an agent from
ending its turn while unread messages exist; per-agent tokens if this ever leaves a
single developer's machine.
