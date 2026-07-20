# Session handoff — pre-flight stepper node, negotiations phase, contract UX

**Date:** 2026-07-20
**Task:** Make pre-flight a first-class step in the task stepper (with per-agent host/buddy
pills), rename the "proposed" phase to **negotiations**, persist a real pending/passed/**failed**
readiness state so humans can coach, gate proposing on both parties passing pre-flight, let
either party reopen a locked contract, make `staging_url` local-friendly + visible, and reword
the onboarding prompt to teach all of it.

---

## Working agreement
- Not vibe coding. One step, explain, gate. This session was heavily gated — many design
  decisions were made interactively before any code (see "Decisions locked" below).
- We test LOCALLY: `pytest` + Playwright MCP. Push to `main` only on owner's explicit say-so
  (this session: owner authorized fix → ccw → commit → push at the end).
- Owner commands: **cfs**/**cfd** = read newest file in `~/dev/screen shots` / `~/dev/download`.
  **ccw** = this handoff. **pwr** = prove live in Playwright. Concise replies. Desktop only.

## STATUS: shipped to working tree; committed + pushed at end of session. 244 pytest green.
Proven live in Playwright (light + dark, desktop).

---

## Decisions locked this session (the "why", so future changes don't undo them)
- **Pre-flight is a stepper NODE**, inserted between `open` and `negotiations`. It has no matching
  task `state` — it's a sub-phase of `open`, driven purely by per-agent readiness.
- **Producer convention (pinned):** the role literally named `backend` is the producer/proposer.
  Every other role assesses + signs. (This replaces the old "model B: producer = whoever proposes";
  pre-flight questions are now role-aware on this convention.)
- **Both parties must pass pre-flight before ANYONE can propose** — enforced in `propose_contract`,
  **remote-only** (local self-declared identities never run pre-flight; the middleware readiness
  gate is remote-only too, so gating locally would brick the whole local flow).
- **"proposed" → "negotiations" is a UI-label rename only.** The backend state key stays
  `contract_proposed` (no DB/state-machine churn). What "negotiations" means: agents talk + pull
  scope from their humans; the **human** tells one to `propose_contract`; the other assesses/pushes
  back, then both `lock_contract`. Only after lock can they build.
- **Reopen is either-party, chat-first.** New `reopen_negotiations(reason)` tool drops a locked-or-
  later task back to `contract_proposed`. Non-destructive (old locked contract still serves via
  `get_contract` until a new version locks). No broker-enforced handshake — a one-sided reopen is
  harmless (peer just won't propose/sign). Ad-hoc changes/bugs after lock = **just messages**, no
  relock; reopen is only for an expressly-wanted re-signed contract.
- **`staging_url` is mode-aware.** Remote = strict https + SSRF guard (peer's test-runner is on
  another machine). Local = any non-empty URL (localhost/http fine — same box, no SSRF surface).
- **"failed" needed backend state.** `agents.ready` (0/1) couldn't tell failed from never-attempted;
  added `readiness_status` + `readiness_report`.
- **Playwright-MCP onboarding is optional, never a gate** — only in the consumer's prompt.

## What shipped (all pytest-green + proven live)
1. **Schema** (`db.py`): `agents.readiness_status TEXT DEFAULT 'pending'` (pending/passed/failed) +
   `agents.readiness_report TEXT` (JSON of last attempt's per-question results), with migrations.
2. **Mode-aware staging_url** (`contracts.py`): `validate_spec(spec, is_remote=True)` →
   `_validate_staging_url(url, is_remote)`; local returns `[]` for any non-empty URL.
3. **Role-aware pre-flight** (`readiness.py`): `_is_backend` convention; `_contract_questions`
   appends `propose` (backend) or `assess` (others) + shared `renegotiate`; graders
   `_grade_propose/_grade_assess/_grade_renegotiate`; preview_questions shows both halves.
4. **Persist + guide + reopen tool** (`tools.py`): `_op_submit_readiness` writes status+report on
   pass/fail and returns negotiations guidance (`result["next"]`) on pass; new `reopen_negotiations`
   tool in BOTH remote + local tool sets (`_op_reopen`). Added `import json`.
5. **State machine** (`state.py`): `import config`; `propose_contract` passes `is_remote` to
   validate + gates on all-agents-ready (remote) + emits a `contract_proposal` peer message;
   `lock_contract` emits `contract_lock` peer messages (partial-sign + full-lock); new
   `reopen_negotiations(conn, identity, reason)` → transition to `CONTRACT_PROPOSED`, `reopen`
   event, `renegotiation` peer message.
6. **API** (`api.py`): `_agents_for` returns `readiness_status`/`readiness_report`; `_contract_for`
   adds `staging_url` per version.
7. **Onboarding prompt** (`onboarding.py`): role-aware contract-flow prompt — phase model
   (pre-flight → negotiations → locked → build → test → verified), backend-proposes vs
   consumer-assesses/pushes-back, post-lock ad-hoc-via-messages + `reopen_negotiations`, optional
   Playwright-MCP setup line for the consumer, `reopen` shorthand.
8. **Dashboard** (`ui.html`): STEPS gained a `preflight` node + `contract_proposed` relabeled
   `negotiations` (also in `SLAB`); `genSteps(state, times, agents)` rewritten to map real→display
   indices around the inserted node and special-case pre-flight done/current from `preflightPassed`;
   `preflightPills` (passed/failed/pending, under the node) + `preflightRow` (debug) +
   `readinessFailPanel` (coaching: per-agent missed questions + hints); contract panel shows a
   "CONNECT TO <staging_url>" line (`IC.link` added); `renegotiation` message type in `typeChip`.
   Removed the earlier misplaced `partiesHTML` embeds.
9. **pyproject.toml**: added `[tool.pytest.ini_options]` `pythonpath=["."]` + `testpaths=["tests"]`
   so the documented `uv run pytest -q` collects (was failing with `No module named 'tests'`;
   only `python -m pytest` worked before).

## Tests (244 green)
- `test_readiness.py`: `_correct_answers` now includes role-aware `propose`/`assess`/`renegotiate`;
  new `test_submit_readiness_persists_status_and_report` (pending→failed+report→passed via
  `tools._op_submit_readiness` + `api._agents_for`).
- `test_state.py`: https test switched to remote mode (+ clears ready gate first); new
  `test_propose_allows_localhost_url_locally`, `test_propose_blocked_until_all_pass_preflight_remote`,
  `test_reopen_negotiations_drops_locked_task_back`, `test_reopen_negotiations_rejected_before_any_lock`.
- `test_onboarding.py`: replaced "same for every role" with role-aware assertions +
  negotiations/reopen + optional-Playwright tests.

## How it was proven (pwr)
Isolated demo db via `SYS_BUDDY_DB=<scratch>/pwr.db`; seed script created two tasks —
`signin-flow` (backend passed / frontend FAILED → pre-flight node current, B✓ green + F✗ red pills,
coaching panel) and `checkout-api` (both passed, contract locked with `staging_url=http://localhost:4000`
→ pre-flight ✓, negotiations ✓, locked current, "CONNECT TO" preview, peer messages in thread).
Boot: `SYS_BUDDY_DB=... uv run sys-buddy local`; token: `... uv run sys-buddy host-viewer`.
NOTE: `--db` is a GLOBAL flag (before the subcommand); prefer the env var.

## Not done / possible follow-ups
- The all-ready propose gate is remote-only by design; no local enforcement (intentional).
- `reopen_negotiations` has no broker-enforced two-party handshake (intentional — chat-first).
- Stray untracked `pwr-*.png` + `.playwright-mcp/` in repo root (screenshots/debug); not committed.
- `v2.md` and `sessions/role-prompt-redesign-checklist.md` still untracked from prior session.
