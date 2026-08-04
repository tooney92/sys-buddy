# sys-buddy — working notes for Claude

sys-buddy is an authenticated, contract-enforcing MCP broker that lets two developers'
AI coding agents collaborate across the internet. **The broker enforces; agents request.**
Source of truth: `SPEC.md`. Build brief: `KICKOFF.md`. Deviations/decisions: `DECISIONS.md`.

## Concepts
- **[`docs/todo-flow.md`](docs/todo-flow.md)** — how a todo goes from idea to verified: the two
  state fields (`status` = the agreement, `state` = the march) and why the dashboard shows both,
  the start-to-end walkthrough with the human shorthand (`todo` → `yes #N` → `pc #N` → `sign #N`
  → `ready #N` → `ok #N` → `done #N`), who may act at each gate, why `#N` is mandatory, and the
  two traps — `sign` before anything is proposed does nothing, and **nothing auto-advances**
  (every arrow is a person deciding). Read it before touching `todos.py` or the todo paths in
  `state.py`.

## Stack
- Python 3.11+, FastMCP (HTTP transport), SQLite (WAL). Env & deps via **`uv`**.
- One process, three surfaces: `/mcp` (MCP tools) · `/pair` (pairing REST) · `/ui` + `/api/*` (read-only dashboard).
- Dashboard is a single self-contained file at `src/sys_buddy/ui.html`, served at `/ui`.
- Run the broker: `uv run sys-buddy local` (loopback `:8787`, no auth) or `uv run sys-buddy serve` (remote, auth enforced).
- Tests: `uv run pytest -q`.

## Local testing & deploy workflow (owner-directed)
- **NEVER test on `:8787`.** That is the owner's real broker and they are often mid-session on
  it. A second broker cannot bind it — it dies with `errno 48` while your requests silently go
  to the LIVE one, which then looks like an auth or data bug. Never `pkill` a broker you did
  not start.
- **Test brokers bind `:9292`** (`SYS_BUDDY_DEV_PORT`, `config.DEV_PORT`) against a throwaway
  db, never the default one:
  ```
  SYS_BUDDY_DB=~/.sys-buddy-dev/sys_buddy.db uv run sys-buddy local --port 9292
  ```
  Confirm the port is free first (`lsof -nP -iTCP:9292 -sTCP:LISTEN`) and check the boot log
  actually says `9292` before driving it.
- **We test LOCALLY.** A `git push` / publish is NEVER a prerequisite for testing. E2E runs
  against the **local** broker on `:9292`, driving the dashboard at
  `http://127.0.0.1:9292/ui` with **Playwright**. Backend behaviour is covered by `pytest`.
- **Push/publish happens ONLY on the owner's explicit directive, and ONLY AFTER** local pytest +
  Playwright are green — never to unblock a test.

## Releasing — the sequence, including the step everyone forgets
Merging to `main` is what starts a release; **squash is the only merge method enabled**, so the
PR title becomes the changelog entry (`feat:` → minor, `fix:` → patch).

1. Merge the feature PR. release-please then opens a `chore(main): release X.Y.Z` PR.
2. **That PR arrives `BLOCKED` with an empty checks list. This is not a failure.** GitHub will
   not trigger workflows for anything its own bot token did, so the required `pytest` and
   `conventional PR title` checks never fire. `gh pr close <n> && gh pr reopen <n>` — the
   reopen is attributed to a real account and the checks run. This has bitten v2.0.0, v2.0.1,
   v2.2.0 and v2.3.0; the permanent fix is the `RELEASE_PLEASE_TOKEN` secret described in
   `.github/workflows/release-please.yml`.
3. Merge the release PR → tag + automatic publish to **PyPI and ghcr.io**.
4. Hand-write `releases/vX.Y.Z.md`. `CHANGELOG.md` is generated and thin (squash collapses a
   whole release to one line), so the prose note is where the detail lives.
5. **Update the website's releases page and push it** — repo `~/dev/sysbuddy_website`,
   `releases.html`. Paste the new `<li class="rel">` at the top of the list and stop: the
   "latest" pill and pulsing dot are derived from `:first-child` in CSS, so they move on their
   own. The owner's shorthand for this step is **"uswr"**.
   **This is not optional.** The page had silently drifted four releases behind, and the
   desktop app's update banner links straight to it — so a stale page is what a user sees at
   the exact moment they are being told to upgrade.

A published PyPI version can never be withdrawn or reused, only superseded. So a dry run
belongs **before** step 1, not after step 3.
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
