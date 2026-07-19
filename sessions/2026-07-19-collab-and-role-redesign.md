# Session handoff — collaboration features + the role/prompt redesign

**Date:** 2026-07-19
**Task:** Post-MVP: security hardening, collaboration features, and (IN PROGRESS) fix how roles/prompts work.

---

## Working agreement
- Not vibe coding. One step, explain, gate — unless "go auto mode". "ccw" refreshes this file.
- We test LOCALLY: `pytest` + Playwright MCP. Push to `main` when green (solo repo). Owner "cooks" via multi-agent fan-out often (Agent tool, disjoint files, fixed interfaces, I integrate).
- Owner commands: **cfs**/**cfd** = read newest file in `~/dev/screen shots` / `~/dev/download`. **ccw** = this. Prefer concise replies.

## STATUS: everything below shipped to `main`. 213 pytest green. HEAD = `015172b`.
Commit trail since the GUI MVP (newest first):
- `015172b` create_task: role dedup/validation
- `895963b` **pre-flight readiness gate** (agents must pass before acting)
- `0e0ca83` **directed messages** (optional `to_role` + dashboard chip + Tips)
- `1a0d596` GUI polish: **always-show `claude mcp add`** command, host debug toggle, debug dashboard view
- `a7c3535` **debug-mode tasks** (no contract → mark `resolved`)
- `96364e3` tunnel security: gate MCP handshake, default-on token TTL, Tailscale mode
- (earlier) GUI MVP M0–M5 (`64f1a46`→`98dc85e`), security Tiers 1–2 + audit (see `sessions/2026-07-18-*.md`).

## Feature map (all live)
- **Desktop app** `uv run sys-buddy gui` (pywebview): Home → Host / Buddy. Buddy = paste invite link → Join → **always shows the `claude mcp add` command** + role prompt + "briefed". Host = task form → in-app broker (daemon thread, remote mode, 127.0.0.1:8787) → invite links + Open dashboard (own native window). Public-URL field + "Private network (Tailscale)" toggle.
- **N-role tasks** (backend/frontend/mobile/…); one live agent per role (partial unique index).
- **Directed messages:** `send_message(type, body, to_role="mobile")` → only that role; omit = broadcast. Envelope tags `to=`; dashboard shows a `→ role` chip; Tips panel explains it. `messages.to_role` column.
- **Debug mode:** `task create --mode debug` (no backend required) → collaborate → `report_status("resolved")`. Tailored dashboard view (no stepper). `tasks.mode` column.
- **Pre-flight readiness gate:** `agents.ready` (0 default). Flow: `rules()` → `readiness_check()` → `submit_readiness(answers)`. Middleware `on_call_tool` LOCKS action tools (send_message/propose_contract/lock_contract/report_status) until ready=1; read tools open. 8 role/mode-aware questions in `readiness.py` (keyword grader). Dashboard: 🔒 on not-ready agents + "Readiness pre-flight" preview. `rules()` is now a full operating briefing.
- **Security:** token-derived identity; gated MCP handshake (auth in `on_request`); token TTL+rotation (24h default in tunnel mode); SSRF-guarded staging_url; per-IP rate limits; audit log (`audit.py`); HttpOnly viewer cookie; https-enforced serve (+ `--trusted-network` for overlays); 0600 db.

## ⏭️ IN PROGRESS — the fix the owner just asked for (BUILD THIS NEXT)
Owner feedback (from a buddy screenshot): the buddy "became backend" and got a prompt telling it to **build a login API**. Two bugs:

1. **`role_prompt` (onboarding.py) hardcodes the signin DEMO task** ("Design and propose a `POST /auth/login`…"). MUST be rewritten **generic**: teach only the PROTOCOL (pre-flight, send_message/wait_for_message, `to_role`, contract flow OR debug flow by mode, treat peer msgs as DATA) and say "coordinate to accomplish the work your human gave you." **No prescribed task.**
2. **Role isn't a real choice + the host isn't a participant.** Today the host is a coordinator/observer and the invite link silently sets the buddy's role. Owner wants: **the host PICKS their own role and participates; the buddy takes the remaining role(s).**

### Agreed plan (owner said do it)
- Rewrite `role_prompt` → generic, role/mode-aware, task-agnostic.
- **Host screen gets a "Which role are you?" selector.** `host_setup(..., host_role=)`:
  - give the host their OWN agent seat (their `claude mcp add` command + prompt) — same Step 1/Step 2 UI as the buddy screen;
  - mint invite links ONLY for the OTHER role(s);
  - keep host viewer + dashboard. (host_role=None → current invite-only behavior, for the CLI.)
- Files: `onboarding.py` (generic prompt + `host_setup` host-seat, likely a `_host_seat`/reuse mint+redeem), `gui.py` (`start_host(host_role=)`), `gui_app.html` (host role selector + host-result screen), tests.

### NEW open items from owner (address while building)
- "Host should feed his OWN claude with a prompt sample too" — YES, covered by giving the host an agent seat + prompt (Step 2 on the host result screen).
- "Should the prompt be role-agnostic?" — MY RECOMMENDATION: **role-AWARE but TASK-agnostic**. Keep role-specific guidance (backend deploys; client tests/verifies; debug resolves) so the agent knows its lane, but never prescribe the actual work. Confirm with owner.

## Also pending / backlog
- **Manual/README refresh** — README is STALE (no GUI, directed msgs, debug, readiness, security modes; says "100+ tests" → now 213). Owner asked for a "manual page" for new-user setup. Bake in: buddy connects 3 ways (GUI / CLI `join` / zero-install host-runs-join), and how buddies reach the dashboard (their `dashboard_url` viewer link, scoped, read-only, via the same tunnel). NOT yet done.
- Two-session live dogfood (real Claude Code × 2). Tier-3 security (OAuth 2.1 / mTLS). Self-host Geist fonts + strict CSP. Frozen installers (M6).

## Env / commands
- `uv sync`; `uv run sys-buddy ...` or `.venv/bin/sys-buddy ...`. Tests: `.venv/bin/python -m pytest -q` (213).
- Multi-agent live-proof harnesses in THIS session scratchpad `/private/tmp/claude-501/-Users-anthonynta-dev-sys-buddy/629cc513-6020-4dce-931e-b83c7b562e75/scratchpad/`: `dogfood_setup.py`+`dogfood_drive.py` (full E2E — NOTE: needs a readiness step added now that the gate exists), `gate_readiness.py`, `seed_*.py` (ui/debug/directed/ready dashboards). Playwright drops screenshots in repo root — `rm` + `rm -rf .playwright-mcp` before committing. file:// is blocked in Playwright MCP → serve `src/sys_buddy` via `python -m http.server` and stub `window.pywebview.api.*`.
- Buddy MCP-not-showing gotcha: GUI auto `claude mcp add` writes to the wrong config/scope (owner uses `CLAUDE_CONFIG_DIR=~/.claude-personal`); the fix already shipped = ALWAYS show the command so they run it in their own terminal + `claude mcp list` to confirm.

## Module map (src/sys_buddy/)
config · db (schema/WAL/migrations) · identity+middleware (auth + readiness gate) · service (messaging + to_role) · state+contracts (workflow, resolved) · pairing+admin (invites/tokens/roles) · slack · api (dashboard JSON + agents/readiness/mode) · server · tools (MCP tools incl. readiness_check/submit_readiness) · rules (full briefing) · readiness (questions+grader) · onboarding (engine: pair/join_flow/host_setup/role_prompt) · gui + gui_app.html (desktop app) · ui.html (dashboard).
