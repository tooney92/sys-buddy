<div align="center">

# sys-buddy

**Let your AI coding agent talk to your teammates' agents.**

*One task, your agents on it — they negotiate the contract, build, and ship while you just watch.*

</div>

---

> **Status: built and dogfooding.** The broker, MCP tools, enforced state
> machine, pairing (CLI + browser onboarding), dashboard API, live-updating
> dashboard UI, and Slack are implemented and covered by 260+ tests plus a live
> end-to-end. See the [Quickstart](#quickstart) to run it. Design/spec live in
> `SPEC.md`, `KICKOFF.md`, and `DECISIONS.md`.

---

## The problem

You're the backend engineer. Your teammate is the frontend engineer. You both use Claude Code.

Every API contract, every field rename, every "ok it's deployed now" gets manually relayed by *you*, copy-pasting between two agent sessions. You're a message bus made of meat, sitting between two systems that could coordinate at machine speed.

Existing tools all assume **one developer, one machine, one trust domain**. sys-buddy is for agents belonging to **different humans**, coordinating **over the internet**, with authenticated identity, an enforced workflow, and an audit trail both people can watch.

## The principle

> **The broker enforces. Agents request.**

Rules that live in prompts get ignored, injected, and forgotten. Rules that live in database constraints don't. So the broker owns the workflow: it validates contracts, rejects out-of-order actions, counts test failures, and stamps every message with a cryptographically-verified identity the agent cannot forge.

## How it works

```
                    ┌──────────────────────────────────┐
                    │       sys-buddy (FastMCP)        │
   your agent ─MCP─▶ │  /mcp        MCP tools           │
 buddy's agent ─MCP─▶ │  /pair       pairing REST        │ ─▶ Slack
     browser ─HTTP─▶ │  /ui + /api  dashboard           │
                    │  SQLite (WAL)                    │
                    └──────────────────────────────────┘
```

One Python process. One port. One tunnel.

1. You create a task and mint a single-use invite for your buddy's role
2. They run `sys-buddy join <url> <code>` — their agent gets a scoped token, they get a read-only dashboard link
3. Both agents propose and **lock a structured API contract** — Slack pings both humans
4. Backend builds, deploys, reports live. **Only then** can the frontend agent run its tests — the broker refuses earlier
5. Tests fail? Frontend reports it, backend fixes, retry. **The broker counts.** Three strikes → task marked stuck, humans pinged
6. Tests pass → `VERIFIED` → both agents stop → Slack says so

Nobody relayed a message.

## Quickstart

### Install

```bash
git clone https://github.com/tooney92/sys-buddy && cd sys-buddy
uv sync                      # creates .venv with everything
```

The CLI runs as `uv run sys-buddy ...`. The examples below drop that prefix — so
**alias it once** (or activate the venv) and the commands work as written:

```bash
alias sys-buddy="uv run sys-buddy"     # add to ~/.zshrc / ~/.bashrc to keep it
```

### Local — 60 seconds, no auth (solo dev, many repos on one machine)

```bash
# 1. start the broker (loopback, zero auth)
sys-buddy local                                    # → http://127.0.0.1:8787

# 2. register it with Claude Code in each repo
#    (re-pairing later? run `claude mcp remove sys-buddy` first — a name can't be overwritten)
claude mcp add --transport http sys-buddy http://127.0.0.1:8787/mcp

# 3. watch it happen (optional)
sys-buddy host-viewer                              # prints a dashboard link → /ui?v=...
```

That's it. Your agents call `send_message` / `check_messages` / `propose_contract`
/ `report_status` with a `task` and `agent` name — the broker auto-creates the task
on first use. Drop the CLAUDE.md snippet from `SPEC.md` §13 into each repo to make
coordination automatic.

### Remote — two humans, two machines (the real thing)

```bash
# ── HOST ─────────────────────────────────────────────
ngrok http 8787                                    # or Tailscale / real infra

# tell every command the tunnel origin — serve AND invite/host-viewer read this,
# so the links they print point at the tunnel, not loopback:
export SYS_BUDDY_PUBLIC_URL=https://abc123.ngrok.app

sys-buddy serve                                    # binds 0.0.0.0, auth enforced
sys-buddy task create signin --roles backend,frontend
sys-buddy invite --task signin --role frontend     # → prints the buddy's https://…/join link + code
# send your buddy that /join link over Slack/Signal (or the sb1_ blob for CLI/desktop)

# ── BUDDY ────────────────────────────────────────────
sys-buddy join https://abc123.ngrok.app signin-J7fK2mQx --name dave-frontend
# → prints the agent token + the exact `claude mcp add ... --header "Authorization: Bearer sbk_..."`
#   command to run, plus a read-only dashboard link
```

`--name` is your agent's **alias** — the label that stamps every message and Slack
ping (e.g. `dave-frontend`). Pick something recognizable; it's how the other humans
tell whose agent said what.

**No CLI required for the buddy.** The invite doubles as a browser link — the host can
send it straight over Slack/Signal. Opening it lands on `/join`, which walks the buddy
through the Claude setup command, the briefing prompt, and their dashboard link. Cloning
the repo is optional (only needed if they want to run their own broker).

Slack pings (optional): set `SLACK_WEBHOOK_URL` before `sys-buddy serve` and both
humans get a message on contract-lock, verified, and stuck.

Revoke anytime: `sys-buddy revoke-agent dave-frontend`, `sys-buddy revoke-viewer
dave`, or `sys-buddy close signin` (kills everything for that task).

## Two modes

| `sys-buddy local` | `sys-buddy serve` |
|---|---|
| loopback, no auth, zero friction | invite-pairing, scoped tokens, enforced state machine |
| your repos, your machine | two humans, two machines, two orgs |

Same tools, same schema, same dashboard. One flag.

## Security, honestly

The full model is in `SPEC.md` §9. The short version:

- **An ngrok URL is not a secret.** They get scanned within minutes, leak via link previews, and appear in certificate transparency logs. All security lives in authentication, never in obscurity.
- **You cannot filter prompt injection to zero.** So the model doesn't try. Assume injection sometimes succeeds and make success worthless: agents can't request file reads or shell commands, staging URLs come only from signed contracts (never from chat), tokens are role-scoped to one task, irreversible steps need a human tap in Slack, and an injected loop still dies at three strikes.
- **Agent access and dashboard access are separate credentials.** A leaked viewer link reads one task's transcript until you revoke it. It can't send anything.

## Repo layout

```
SPEC.md      ← the complete specification. Start here.
KICKOFF.md   ← build instructions for a coding agent
design/      ← Claude Design handoff: the dashboard prototype (visual source of truth)
reference/   ← agent_bus.py: working local-only predecessor + its ops guide
```

## Development

```bash
uv sync                        # install deps into .venv
uv run pytest -q               # the full spec suite (260+ tests)
uv run sys-buddy --help        # the CLI surface
```

Source lives in `src/sys_buddy/`: `db` (schema/WAL) · `identity` + `middleware`
(auth) · `service` (messaging) · `state` + `contracts` (the enforced workflow) ·
`pairing` + `admin` (invites/tokens) · `api` (dashboard JSON) · `server` (assembly)
· `ui.html` (single-file dashboard). Implementation decisions and spec deviations
are logged in `DECISIONS.md`.

## Credits

Grew out of `agent-bus`, a ~130-line FastMCP message bus for coordinating Claude Code agents across repos on one machine (see `reference/`). sys-buddy is that idea taken across the internet, between people.
