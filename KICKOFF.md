# KICKOFF — read this first, then start building

You are building **sys-buddy** from scratch in this repo. Everything you need is here.

## Read these, in this order

1. **`SPEC.md`** — the complete specification. Architecture, schema, state machine, security model, API contract, build order. This is the source of truth. Read it top to bottom before writing a line of code.
2. **`design/README.md`** then **`design/project/Sys-Buddy Dashboard.dc.html`** — the visual source of truth for the dashboard. Read the HTML in full; every colour, size, and state is in there.
3. **`reference/agent_bus.py`** — a *working* local-only predecessor. The long-poll loop, SQLite mailbox, and FastMCP HTTP setup all work today. Port them; don't rewrite blind.
4. **`reference/AGENT_BUS_GUIDE.md`** — hard-won operational knowledge (PEP 668, HTTP-vs-stdio transport, multi-account MCP registration, naming collisions). Read the Troubleshooting table — those are real bugs someone already paid for.

## The one principle

> **The broker enforces. Agents request.**

If a rule could live in an agent's prompt *or* in broker code, it lives in the broker. Prompts get ignored, injected, and forgotten. Database constraints don't. When you hit a design decision the spec doesn't cover, resolve it with that sentence.

## Build order (SPEC §14)

1. Schema + WAL + `sys-buddy init`
2. Auth middleware (token → identity; no-op in local mode; revocation checks)
3. MCP tools — messaging first (port from `reference/agent_bus.py`), then contracts, then status
4. State machine — transitions, rejections, `events` rows, strike counting
5. Pairing — `/pair`, invite/join/revoke CLI, token issuance
6. API — `/api/tasks`, `/api/task/{id}`, **server-side** viewer scoping
7. UI — rebuild the design as single-file vanilla HTML/JS against the real API
8. Slack — webhook on contract_locked, verified, stuck
9. Docs — README with the 60-second local quickstart *first*, remote pairing second

Ship each step working before starting the next. Commit per step.

## Non-negotiables

- **Single-file vanilla HTML/CSS/JS for the UI.** No React, no bundler, no build step. FastMCP serves one `ui.html`. One process, one port, one tunnel — that's the product's whole promise.
- **`messages.from_agent_id` is stamped by middleware from the bearer token.** Never accept identity as a tool parameter in remote mode. Remote tool signatures have no `sender` param at all.
- **Never store raw tokens or invite codes.** sha256 only.
- **The staging URL comes from the locked contract.** Never from a message body. This is not a style preference; it kills an injection class.
- **The Host/Buddy toggle in the design prototype is NOT clickable in production.** It's a static badge reflecting the token's scope. A buddy clicking "Host" would be privilege escalation. (SPEC §12)
- **Buddy task scoping is server-side.** The prototype filters client-side; that's prototype-only. `/api/tasks` returns only what the token permits.
- **Local mode must stay zero-friction.** No auth, no pairing, `127.0.0.1`. It's the adoption on-ramp. Same code path, auth middleware is just a no-op.

## Three known bugs to fix while porting `agent_bus.py`

1. No WAL mode — the long-poll loop opens a SQLite connection every 2s. Add `PRAGMA journal_mode=WAL`.
2. `notify_human` has no error handling — a Slack timeout raises and derails the agent's turn. Wrap it; return a soft failure string.
3. `_fetch_unread` marks messages read on fetch — a crashed session loses them. Split `delivered_at` / `acked_at` (SPEC §4) and add an `ack_messages` tool.

## Design fidelity

The `.dc.html` is a **prototype**, not production code. It uses a custom `<x-dc>`/`<sc-if>`/`<sc-for>` runtime from `support.js` with hardcoded mock data in `TASKS`/`DETAIL`.

**Match the visual output pixel-for-pixel. Do not copy the prototype's internal structure.** Design tokens are listed in SPEC §12 — use those exact hex values. Keep the flourishes: pulsing ring on the current stepper node, falling confetti on `verified`, dashed-avatar empty states, required-field coral asterisks. They're what make it feel like sys-buddy and not a status table.

Don't render the prototype in a browser or screenshot it — everything is in the source.

## Stack

- Python 3.11+, FastMCP (Starlette/uvicorn underneath — that's what lets one process serve `/mcp`, `/pair`, `/ui`, `/api`)
- SQLite (stdlib), WAL mode
- No frontend framework
- `pyproject.toml`, installable, `sys-buddy` console entrypoint

## When you're done

SPEC §16 defines done. The real test: register sys-buddy as an MCP in two Claude Code sessions and have them ship a feature through it. If you can't dogfood it, it isn't finished.

## If anything is ambiguous

Ask before implementing. Cheaper to clarify scope up front than to build the wrong thing.
