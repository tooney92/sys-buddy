<div align="center">

# sys-buddy

**Let your AI coding agent talk to your teammate's AI coding agent.**

*Two buddies, one task. Their agents negotiate the contract, build, and ship — you just watch.*

</div>

---

> ⚠️ **Status: pre-implementation.** This repo currently contains the spec, the design, and a working local-only predecessor. The build hasn't started. See `KICKOFF.md`.

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

## Building it

```bash
cd sys-buddy
claude
> Read KICKOFF.md and build this.
```

## Credits

Grew out of `agent-bus`, a ~130-line FastMCP message bus for coordinating Claude Code agents across repos on one machine (see `reference/`). sys-buddy is that idea taken across the internet, between people.
