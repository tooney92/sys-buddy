# Session handoff — sys-buddy build

**Date:** 2026-07-17 (updated after all 9 steps)
**Task:** Build sys-buddy from `SPEC.md` / `KICKOFF.md`. **Backend is complete.**

---

## Working agreement (important)
- **Not vibe coding.** Build one step, explain with concrete examples, STOP and wait for approval. Say **"go auto mode"** to lift gating, **"ccw"** to refresh this file.
- Commit **per approved step**; pushing to `main` is fine (solo repo, user asks explicitly).

## STATUS: all 9 build steps DONE, committed, pushed to `main`
Commits: `a4acbe5` (spec/design) → `df5d385` (steps 1-3) → `5694aa5` (steps 4-6 + review fixes) → `78f462e` (steps 7-9).
- 1 schema+WAL+init · 2 auth middleware · 3 messaging tools · 4 state machine+strikes · 5 pairing · 6 dashboard API · 7 dashboard UI · 8 Slack · 9 docs — ALL ✅.
- **115 pytest specs green.** Live remote e2e passing. UI verified structurally.

## ⏭️ WHAT'S PENDING (resume here)
1. **Visual UI verification via Playwright MCP** — just registered `playwright` MCP (local scope, `npx @playwright/mcp@latest`). It needs a **session restart** to load its browser tools. AFTER RESTART, do this:
   - Boot a seeded broker and drive the dashboard in a browser, screenshot each screen (task list, task view w/ stepper+thread+contract, **light + dark**, **mobile** breakpoint), compare to `design/project/Sys-Buddy Dashboard.dc.html`, fix any drift.
   - Seed script for rich content already exists: `scratchpad/ui_verify.py` (seeds signin task through propose→lock→deploy→test_pass→verified + issues a host viewer token). The scratchpad dir: `/private/tmp/claude-501/-Users-anthonynta-dev-sys-buddy/1efa790f-ab5f-47d6-9c1b-523c696d6e58/scratchpad/`.
   - Quick manual view: `uv run sys-buddy local &` then `uv run sys-buddy host-viewer` → open the printed `/ui?v=...`.
2. **Final pre-ship code review** — NOT yet run. Must cover the 6 fixes to steps 4-6 AND all of steps 7-9 (esp. the agent-written `ui.html`). Prior reviews: `/code-review` skill → `Workflow({name:"code-review", args:"high"})`.
3. **Full two-session dogfood** (SPEC §16 definition of done) — register sys-buddy in two real Claude Code sessions, ship a feature through it.

## Environment
- `uv` at `~/.local/bin` → `export PATH="$HOME/.local/bin:$PATH"`. venv `.venv/`, Python 3.13, FastMCP 3.4.4, pytest 9.1.1.
- Run: `uv run sys-buddy ...` or `.venv/bin/sys-buddy ...`. Tests: `.venv/bin/python -m pytest -q`.
- `ngrok` installed. `gh` authed as `tooney92`. Repo github.com/tooney92/sys-buddy (private).
- Default db: `~/.sys-buddy/sys_buddy.db` (absolute; override `--db`/`$SYS_BUDDY_DB`). `$SYS_BUDDY_DEBUG=1` → CLI shows tracebacks.
- `SLACK_WEBHOOK_URL` env enables Slack in `serve` mode.
- Playwright MCP: local scope; remove with `claude mcp remove playwright -s local`.

## Module map (`src/sys_buddy/`)
config · db (schema/WAL) · identity + middleware (auth) · service (messaging) · contracts + state (enforced workflow, strikes, Slack triggers) · pairing + admin (invites/tokens/host ops) · slack (error-wrapped webhook) · api (dashboard JSON, viewer scoping) · server (assembly: init_db+middleware+tools+routes) · tools (10 MCP tools, remote/local) · cli (argparse) · ui.html (single-file dashboard).
Tests: test_identity, test_messaging, test_contracts, test_state, test_pairing, test_api, test_server, test_slack.

## Invariants NOT to break (learned via reviews)
- **`service._wrap` stays HTML-escaped** — else prompt-injection breakout.
- **wait_for_message = `fetch_new` (undelivered); check_messages = `fetch_unacked` (crash recovery)** — keep separate.
- **`ack` only own-task, other-sender** ids.
- **Remote tool signatures NEVER take sender/agent** — identity from token.
- **Fixed cast = partial unique index** (`WHERE revoked_at IS NULL`); don't restore blanket UNIQUE.
- **Lifecycle types (deploy_confirmed/test_result/verified/stuck) are report_status-only**; `send_message` rejects them (keeps strike count in sync).
- **`close_task` burns outstanding invites; closed tasks reject pairing + messaging.**
- **A task must include a `backend` role** (deployer) — enforced in `admin.create_task`.
- Decisions D1-D10 documented in `DECISIONS.md`.

## Scratchpad artifacts (handy for resume)
- `scratchpad/e2e.py` — live remote dogfood (invite→join→scoped api→authed tool→forged-token reject).
- `scratchpad/ui_verify.py` — seed lifecycle + boot + structural UI checks.
