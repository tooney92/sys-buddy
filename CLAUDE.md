# sys-buddy — working notes for Claude

sys-buddy is an authenticated, contract-enforcing MCP broker that lets two developers'
AI coding agents collaborate across the internet. **The broker enforces; agents request.**
Source of truth: `SPEC.md`. Build brief: `KICKOFF.md`. Deviations/decisions: `DECISIONS.md`.

## Stack
- Python 3.11+, FastMCP (HTTP transport), SQLite (WAL). Env & deps via **`uv`**.
- One process, three surfaces: `/mcp` (MCP tools) · `/pair` (pairing REST) · `/ui` + `/api/*` (read-only dashboard).
- Dashboard is a single self-contained file at `src/sys_buddy/ui.html`, served at `/ui`.
- Run the broker: `uv run sys-buddy local` (loopback `:8787`, no auth) or `uv run sys-buddy serve` (remote, auth enforced).
- Tests: `uv run pytest -q`.

## Local testing & deploy workflow (owner-directed)
- **We test LOCALLY.** A `git push` / publish is NEVER a prerequisite for testing. E2E runs
  against the **local** broker: `uv run sys-buddy local` on `:8787`, driving the dashboard at
  `http://127.0.0.1:8787/ui` with **Playwright**. Backend behaviour is covered by `pytest`.
- **Push/publish happens ONLY on the owner's explicit directive, and ONLY AFTER** local pytest +
  Playwright are green — never to unblock a test.
- The dashboard needs a viewer token. Mint one against the **local** db with
  `uv run sys-buddy host-viewer`, then open `/ui?v=<token>`. Ask for seeds against the LOCAL db —
  never "deploy + seed on a remote."
- If a task needs data, **CREATE it via the real flow first** (`task create` → `propose_contract`
  → `lock_contract` → `report_status`) rather than waiting on a seed.

## Feature DONE
= `pytest` green **AND** the change proven live in Playwright against the local dashboard.

## "pwr" — prove it live
When the owner says **"pwr"**, drive the just-finished change in a real browser via the Playwright
MCP: boot a seeded local broker, navigate the dashboard, screenshot the relevant screens
(list, task view, light + dark, mobile), and confirm it actually works before reporting done.

## Playwright MCP
Declared in `.mcp.json` (tracked) and auto-approved via `enabledMcpjsonServers` in
`.claude/settings.local.json`. MCP tools bind at session start — a freshly added server needs a
session restart to load.
