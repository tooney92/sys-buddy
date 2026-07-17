# Session handoff — sys-buddy build

**Date:** 2026-07-16
**Task:** Build sys-buddy from scratch per `SPEC.md` / `KICKOFF.md` (backend first, UI last).

---

## Working agreement (important)
- **Not vibe coding.** Build ONE step at a time, then explain what was built **with concrete examples**, then STOP and wait for the user's approval before the next step.
- Say **"go auto mode"** to lift the gating.
- Say **"ccw"** to refresh this session file.
- Commit to git **per step, only after the user approves** that step.

## Environment
- `uv` installed at `~/.local/bin` (add to PATH: `export PATH="$HOME/.local/bin:$PATH"`).
- venv at `.venv/` (created by `uv sync`). Python 3.13, FastMCP 3.4.4.
- Run the CLI: `.venv/bin/sys-buddy ...` (or `uv run sys-buddy ...`).
- `ngrok` is installed. `gh` is authed as `tooney92`. Repo: github.com/tooney92/sys-buddy (private).

## Build order (SPEC §14) & status
1. **Schema + WAL + `sys-buddy init`** — ✅ DONE (awaiting user approval to commit)
2. Auth middleware (token → identity; no-op local; revocation) — ⬜ NEXT
3. MCP tools (messaging → contracts → status) — ⬜
4. State machine (transitions, rejections, events rows, strikes) — ⬜
5. Pairing (/pair, invite/join/revoke CLI, token issuance) — ⬜
6. API (/api/tasks, /api/task/{id}, server-side viewer scoping) — ⬜
7. UI (single-file vanilla HTML/JS, rebuild design pixel-for-pixel) — ⬜
8. Slack (webhook on contract_locked, verified, stuck; error-wrapped) — ⬜
9. Docs (README: 60s local quickstart first, remote second) — ⬜

## Files written so far
- `pyproject.toml` — package `sys-buddy`, console script `sys-buddy = sys_buddy.cli:main`, dep `fastmcp>=2.0`, hatchling src-layout.
- `src/sys_buddy/config.py` — `Config` dataclass (mode/db/host/port/slack/public_url) + `get_config`/`set_config` singleton.
- `src/sys_buddy/db.py` — full SQLite schema, WAL, `connect()`, `init_db()`.
- `src/sys_buddy/cli.py` — full argparse surface; only `init` implemented. Other commands lazy-import modules not yet written (`admin`, `pairing`, `server`) — they'll fail until those land.
- `src/sys_buddy/ui.html` — placeholder (real UI is step 7).
- `DECISIONS.md` — D1: per-recipient `deliveries` table instead of `delivered_at`/`acked_at` columns on `messages` (needed for 3+ role tasks).

## Verified
`sys-buddy --db /tmp/x.db init` creates all 8 tables and confirms `journal_mode: wal`. Idempotent.

## Next actions (Step 2 — auth middleware)
- `identity.py`: token hashing (sha256), token prefixes (`sbk_` agent, `sbv_` viewer, invite `task-xxxx`), a contextvar holding the resolved identity, resolve `token → agents row` (revoked check).
- `middleware.py`: FastMCP `Middleware.on_call_tool` that reads the bearer token via `get_http_request()`, resolves identity, rejects revoked/invalid in remote mode; **no-op in local mode** (identity self-declared via tool params).
- FastMCP API confirmed available: `custom_route`, `http_app`, `add_middleware`, `Middleware.on_call_tool`, `get_http_request()`, `mcp.run(transport="http", host, port)`.

## Open questions / watch-outs
- Local-mode identity: tools take `task`/`agent` params and auto-create an implicit `agents` row (token_hash NULL). Confirm approach when building tools (step 3).
- Nothing committed to git yet.
