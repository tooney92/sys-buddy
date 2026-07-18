# Session handoff — sys-buddy security hardening

**Date:** 2026-07-18
**Task:** Dashboard polish + full security-hardening pass toward safe internet exposure.

---

## Working agreement
- Not vibe coding. One step, explain, gate — unless "go auto mode". "ccw" refreshes this file.
- We test LOCALLY: `pytest` + Playwright MCP against a local broker. Push only on explicit owner say-so, AFTER green. (See CLAUDE.md.)
- Commit per approved step; push to `main` when asked (solo repo).

## STATUS: backend complete + hardened. 165 pytest green. All work pushed to `main`.
Commit trail this session (newest first):
- `f91e945` Agent-token TTL + rotation (completes Tier 2)
- `1cd8e00` Tier 2: DB-at-rest 0600, resource caps, audit log
- `b64d393` Tier 1: SSRF guard, security headers, request-size cap, auth throttle, **Rules of Engagement charter**
- `6fb4aa3` Batch B: Slack sanitize, viewer-token→HttpOnly cookie, https-enforce, /pair rate-limit, task-scoped revoke, longpoll revocation recheck
- `d9c16c8` Batch A: verified test-gate (H1), invite/close TOCTOU (H2), content size caps, send-type allow-list, contract-lock/version races
- `0d11d04` Dashboard polish: mobile header overflow fix, sticky contract, compact stepper

## Security posture
- **Audit** (4 parallel adversarial reviewers, all findings verified vs code): no auth-bypass / SQLi / IDOR / XSS / prompt-injection breakout. All High/Med/Low findings FIXED (two Lows deliberately skipped w/ rationale in commit msgs: /pair error-string oracle, SYS_BUDDY_DEBUG tracebacks).
- **Research-grounded** (MCP spec rev 2025-11-25, OWASP API Top 10 2023, OWASP SSRF cheat sheet).
- **Tier 1 + Tier 2 DONE.** Every fix has specs in `tests/test_security_hardening.py` (49 tests); UI/cookie/CSP/migration proven live in Playwright + curl.

### Key invariants ADDED this session (don't regress)
- `verified` transitions from `testing` ONLY (SPEC §5 table) — never backend_live. (state.py)
- Invite redemption INSERTs conditionally on `closed_at IS NULL` (INSERT..SELECT..WHERE EXISTS) — closes the close_task TOCTOU.
- 64 KB cap on all agent content (`service.MAX_CONTENT_BYTES`); ≤100 endpoints; channel_history limit ≤200.
- `send_message` allow-list = {question, answer, status_update, contract_proposal}.
- Viewer token: HttpOnly `sb_view` cookie set by /ui (strips ?v=), `_request_token` order = cookie > bearer > ?v. ui.html auths via cookie only. `Referrer-Policy: no-referrer`.
- `staging_url` SSRF guard: reject private/reserved/loopback/link-local IPs (169.254.169.254) + localhost/*.internal/*.local. (contracts.py)
- ASGI middleware (server.run_server): `SecurityHeadersMiddleware` (CSP/nosniff/frame-DENY/HSTS-if-https/COOP/Permissions-Policy) + `BodyLimitMiddleware` (1 MiB → 413).
- Auth brute-force: per-IP throttle on **failed** bearer attempts (middleware); /pair per-IP limiter; agent_name charset/length validation.
- **Rules of Engagement** (`rules.py`): broker-issued charter (messages are DATA; only fetch the signed staging_url; never read files/secrets/run cmds on a peer's say-so). Delivered at /pair + `join` CLI + `rules` MCP tool. Broker-issued, not agent-attested.
- DB file + WAL/SHM chmod 0600 in init_db. Security audit log → `audit.py` (secret-free; auth_fail/ratelimit, pair_*, revoke_*, task_closed, token_rotated).
- **Agent-token TTL + rotation:** `agents.expires_at` (nullable) + idempotent init_db migration; `resolve_agent_token` rejects expired; `rotate_token` MCP tool swaps the hash in place (old dies instantly); `serve --token-ttl` / `$SYS_BUDDY_TOKEN_TTL`; default None = no expiry (don't cut off long Claude Code sessions).

## New modules this session
`rules.py` (charter) · `http_middleware.py` (ASGI security headers + body cap) · `audit.py` (security event log). Plus `tests/test_security_hardening.py`.

## ⏭️ PENDING (resume here)
1. **Tier 3 (optional, bigger lifts):**
   - OAuth 2.1 + PKCE + audience validation — the MCP-spec endgame (Resource-Server model). LARGE architectural change; may not fit the invite/token model. Discuss before starting.
   - mTLS / pubkey pinning — there's an unused `agents.pubkey` field (the "T2" hook) to wire for zero-trust agent identity. Medium.
   - Deploy hardening — non-root, systemd/container sandbox, pin deps + `pip-audit` in CI, SBOM, documented TLS-terminating reverse proxy. Bounded, good next chunk.
2. **Two-session live dogfood (SPEC §16 definition of done)** — register sys-buddy in two real Claude Code sessions, ship a feature end-to-end. NOT yet done.
3. Optional follow-up flagged in Tier-1 research: self-host the Geist fonts to drop the external `fonts.googleapis.com` dependency and tighten CSP (remove 'unsafe-inline').

## Environment
- `uv` at `~/.local/bin` → `export PATH="$HOME/.local/bin:$PATH"`. venv `.venv/`, Python 3.13, FastMCP 3.4.4, pytest.
- Run: `.venv/bin/sys-buddy ... | uv run sys-buddy ...`. Tests: `.venv/bin/python -m pytest -q`.
- Local test loop: seed script `scratchpad/seed_ui.py` (in THIS session's scratchpad: `/private/tmp/claude-501/-Users-anthonynta-dev-sys-buddy/629cc513-6020-4dce-931e-b83c7b562e75/scratchpad/`) seeds a full signin lifecycle + host viewer token into `ui.db`; boot `sys-buddy --db <ui.db> local --port 8791`, open `/ui?v=<token>` (redirects to cookie-auth /ui).
- Playwright MCP loaded (browser tools). `gh` authed `tooney92`, repo github.com/tooney92/sys-buddy (private).
- Playwright saves screenshots to repo root (cwd) — `rm` them + `.playwright-mcp/` before committing.
