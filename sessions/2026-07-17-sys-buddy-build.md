# Session handoff — sys-buddy build

**Date:** 2026-07-17
**Task:** Build sys-buddy from `SPEC.md` / `KICKOFF.md` (backend first, UI last).
**Supersedes:** 2026-07-16-sys-buddy-build.md

---

## Working agreement (important)
- **Not vibe coding.** Build ONE step, then explain with concrete examples, then STOP and wait for the user's approval before the next step.
- Say **"go auto mode"** to lift gating. Say **"ccw"** to refresh this file.
- Commit to git **per approved step**. Pushing to `main` is fine (solo repo; user asks explicitly).

## Environment
- `uv` at `~/.local/bin` → `export PATH="$HOME/.local/bin:$PATH"`.
- venv `.venv/`, Python 3.13, FastMCP 3.4.4, pytest 9.1.1 (dev group).
- Run CLI: `.venv/bin/sys-buddy ...`. Run tests: `.venv/bin/python -m pytest -q`.
- `ngrok` installed. `gh` authed as `tooney92`. Repo github.com/tooney92/sys-buddy (private).
- Default db is now **absolute**: `~/.sys-buddy/sys_buddy.db` (override `--db` / `$SYS_BUDDY_DB`).
- `$SYS_BUDDY_DEBUG=1` makes the CLI re-raise (traceback) instead of a clean error line.

## Build order (SPEC §14) & status
1. Schema + WAL + `sys-buddy init` — ✅ committed
2. Auth middleware (token→identity; no-op local; revocation) — ✅ committed
3. MCP messaging tools (agent_bus port + 3 bug fixes) — ✅ committed
4. **State machine (transitions, rejections, events rows, strikes) + contract/status tools** — ⬅ NEXT
5. Pairing (/pair, invite/join/revoke CLI, token issuance) — ⬜
6. API (/api/tasks, /api/task/{id}, server-side viewer scoping) — ⬜
7. UI (single-file vanilla HTML/JS, rebuild design pixel-for-pixel) — ⬜
8. Slack (webhook on contract_locked/verified/stuck; error-wrapped) — ⬜
9. Docs (README: 60s local quickstart first, remote second) — ⬜

## Git
- Latest commit `df5d385` "Backend foundation: schema, auth, messaging tools" pushed to `main`.
- Initial commit `a4acbe5` (spec/design/reference).

## Files written
- `pyproject.toml` — pkg `sys-buddy`, script `sys-buddy = sys_buddy.cli:main`, dep `fastmcp>=2.0`, dev `pytest`.
- `src/sys_buddy/config.py` — `Config` (mode/db/host/port/slack/public_url) + get/set singleton. Absolute default db path.
- `src/sys_buddy/db.py` — schema, WAL (set in init only), `connect()` (foreign_keys+busy_timeout), `init_db()`.
- `src/sys_buddy/identity.py` — sha256, token gen (`sbk_`/`sbv_`/invite), `Identity`/`ViewerIdentity`, contextvar, `resolve_agent_token`/`resolve_viewer_token` (revocation-aware).
- `src/sys_buddy/middleware.py` — `AuthMiddleware.on_call_tool`: remote resolves bearer→identity (rejects bad/revoked); local no-op.
- `src/sys_buddy/service.py` — messaging core: `ensure_local_identity`, `post_message`, `_fetch`/`fetch_unacked`/`fetch_new`, `ack` (task-scoped), `channel_history`, `_wrap` (HTML-escaped envelope).
- `src/sys_buddy/tools.py` — `register_tools(mcp,cfg)`; shared `_op_*` helpers; remote (no sender param) + local (task/agent) registrations.
- `src/sys_buddy/cli.py` — full argparse; `init` implemented; other cmds lazy-import not-yet-built modules (`admin`, `pairing`, `server`).
- `src/sys_buddy/ui.html` — placeholder (real UI step 7).
- `tests/conftest.py` + `test_identity.py` + `test_messaging.py` — **25 specs, all green**.
- `DECISIONS.md` — D1 (per-recipient deliveries table).

## Key behaviors to preserve (learned the hard way)
- **Envelope must stay escaped** (`service._wrap`): raw interpolation = prompt-injection breakout. Critical.
- **wait_for_message = `fetch_new`** (undelivered only); **check_messages = `fetch_unacked`** (crash-safe recovery). Don't merge them.
- **`ack` only touches own-task, other-sender messages** — ignores unknown/foreign/self ids.
- Remote tool signatures **never** take a sender/agent param — identity from token only.

## Code-review note
High-effort review flagged missing `server.py`/`admin.py`/`pairing.py` and `connect()` self-init. These are **future steps (5, 8)**, not bugs — do NOT build ahead of the gate. `connect()` self-init will be covered by server startup calling `init_db()` in step 5.

## Next actions (Step 4 — state machine)
- `contracts.py`: validate structured contract JSON (SPEC §6) — required keys, HTTP verbs, https `staging_url`, field types; return actionable errors.
- `state.py`: state constants + allowed transitions (SPEC §5), `apply_action`, rejection reasons, write `events` rows, **broker-counted strikes** (test_result fail → +1; >=3 → force `stuck`; new deploy+new contract version resets to 0). Terminal `verified`/`stuck`.
- Contract/status MCP tools: `propose_contract`, `lock_contract` (all roles must sign), `get_contract`, `report_status`. Wire role-scoped permissions (only backend → deploy_confirmed; non-backend → test_result). staging_url read from locked contract, never chat.
- Add specs for: transition rejections, all-roles-must-sign lock, deploy-requires-locked-contract, test-before-live rejected, 3-strikes→stuck, strike reset on new version.
