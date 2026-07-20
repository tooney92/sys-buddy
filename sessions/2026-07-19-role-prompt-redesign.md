# Session handoff — role/prompt redesign + model B (producer = proposer)

**Date:** 2026-07-19
**Task:** Fix how roles/prompts work; generalize the status vocab; make the host a participant;
and the big one — the producer is now whoever proposes the contract (no hardcoded `backend`).

---

## Working agreement
- Not vibe coding. One step, explain, gate — unless "go auto mode" (this session ran auto).
- We test LOCALLY: `pytest` + Playwright MCP. Push to `main` only on owner's explicit say-so.
- Owner commands: **cfs**/**cfd** = read newest file in `~/dev/screen shots` / `~/dev/download`.
  **ccw** = this handoff. **pwr** = prove live in Playwright. Prefer concise replies.
- **sys-buddy is a laptop/desktop tool — NO mobile view** (GUI is pywebview; can't be on a phone).

## STATUS: all shipped to the WORKING TREE, NOT committed. 237 pytest green. HEAD still = `fb8f299`.
Owner has NOT authorized a commit/push yet. Everything below is on disk, uncommitted:
- Modified: `state.py · admin.py · onboarding.py · readiness.py · gui.py · gui_app.html · ui.html · tools.py`
  and tests `test_state/onboarding/pairing/readiness/debug.py`.
- New (untracked): `tests/test_admin.py`, `v2.md`, `sessions/role-prompt-redesign-checklist.md`.

## What shipped this session (all 9, pytest-green + proven live in Playwright, desktop only)
1. **Status vocab generalized** — `ready`/`checked`/`blocked` added as pure ALIASES of
   `deployed`/`test_passed`/`test_failed` (normalized at the top of `state.report_status`; nothing
   downstream changed). Agent-facing docstrings (tools.py x2, state.py) lead with the new words.
2. **Generic prompt** — deleted the hardcoded `POST /auth/login` demo in `onboarding.role_prompt`.
   Teaches ONLY the sys-buddy protocol, never what to build (humans decide in their own sessions).
3. **Host agent seat** — `host_setup(..., host_role=)` seats the host's OWN agent (mints an invite
   for host_role and redeems it IN-PROCESS via `pairing.redeem_invite`, no HTTP), returns `host_seat`
   {role,mcp_url,agent_token,prompt,config_command}; invite links minted only for the OTHER roles.
   `host_role=None` → old behavior (CLI). `gui.start_host` auto-runs `claude mcp add` for the seat.
4. **Auto task id** — human types only a Title; `admin.new_task_id(title)` = slug + `secrets` suffix
   (e.g. "New Login API" → `new-login-api-a3f2`). `create_task(id=None/"" , title=...)` derives it.
5. **GUI host screen** (`gui_app.html`): Title required (id auto), host picks their role, MERGED the
   "Roles" cast + "Which one are you?" into ONE section — the "which one" selector shows ONLY roles
   in the cast (hidden, not dimmed) and tracks it live. Session-type note reworded (no "deploy"/"backend").
6. **Connectivity selector** — 3-way: Same machine / Public tunnel (ngrok) / Private network
   (Tailscale via `tailscale serve 8787`). Both remote paths are https, so GUI requires https for
   any public_url (dropped the old "allows http" trusted toggle; CLI keeps `--trusted-network`).
7. **Dashboard "+ New task"** (`ui.html`) — host-only, in-app only (guarded on `window.pywebview`
   + `!buddy`), deep-links back to the app's Start-a-task via a MINIMAL bridge `gui._DashApi.new_task`
   (dashboard stays read-only; it never gets the full GuiApi). `window.__sbGotoHost` in gui_app.html.
8. **★ Model B — producer = whoever proposes the contract** (no hardcoded `backend`):
   - `state._producer_role(conn, task)` = role that PROPOSED the current locked contract.
   - `report ready` gate → "you ARE the producer"; `report checked/blocked` gate → "you are NOT".
   - `BACKEND_ROLE` constant removed. `admin.create_task` dropped "must include backend"; a contract
     now needs **≥2 roles** instead. `readiness.py` status question/grader generalized (no backend
     assumption). Prompt collapsed to ONE unified contract variant (producer unknown at onboarding).
   - Proof: `test_state.test_non_backend_producer_full_flow` — frontend proposes → is producer →
     reports ready; mobile checks → verified. ZERO backend role anywhere.
9. **Desktop-only** — no GUI mobile layout; dashboard shows a full-screen "Please switch to a
   desktop" gate when `state.isMobile` (width < 900). Verified: gate at 390px, normal at 1280px.

## Live-proof harness (Playwright MCP)
- GUI is pywebview → file:// blocked. Serve statically: `cd src/sys_buddy && .venv/bin/python -m
  http.server 8899`, open `http://127.0.0.1:8899/gui_app.html?v=N` (bump N to bust cache), and STUB
  `window.pywebview.api.start_host/open_dashboard` in `browser_evaluate` before clicking. Dashboard:
  `ui.html?v=sbv_x` (no broker → shows "connection issue"/no-token, fine; the mobile gate short-circuits first).
- Playwright drops screenshots in the REPO ROOT + `.playwright-mcp/` — `rm` the pngs and
  `rm -rf .playwright-mcp src/sys_buddy/.playwright-mcp` before committing. (zsh: an unmatched
  `*.png` glob aborts the whole `rm`; delete explicit filenames.)

## ⏭️ Next / open
- **Owner decision: commit + push?** Nothing is committed. Suggested commit split: (a) status vocab,
  (b) generic prompt + host seat + auto-id, (c) GUI host screen + connectivity + dashboard button,
  (d) model B. Or one squashed commit. Await owner.
- **Landing page** (owner is building one): drafted feed-to-Claude prompts in chat for "How it works
  (host→buddy)", an "Is it safe? (ngrok)" security section, and the ngrok/Tailscale walkthroughs.
  Honesty note baked in: ngrok TERMINATES TLS at its edge (trusted middleman) — Tailscale is true E2E.
- **README/manual still STALE** (no GUI/directed-msgs/debug/readiness/security/model-B; says "100+ tests").
- Two-session live dogfood (real Claude Code × 2 — needs a readiness step now the gate exists).

## v2.md backlog (logged, NOT built)
- Contract-lock PUSH (first signer polls `get_contract` today; make the broker notify instead).
- Stronger auth: mTLS / OAuth 2.1 (replace bearer-holder trust; complements Tailscale's E2E).
- (Producer-freedom entry is SHIPPED — marked done in v2.md.)

## Module map (src/sys_buddy/) — unchanged shape, key deltas this session
config · db · identity+middleware (auth + readiness gate) · service · **state** (producer=proposer,
`_producer_role`) · contracts · **admin** (create_task: ≥2 roles, `new_task_id`) · pairing ·
**onboarding** (unified prompt, `host_setup(host_role=)`, `_mint_host_seat`) · **readiness** (generic
status q) · api · server · tools · rules · **gui**+**gui_app.html** (host screen redesign, `_DashApi`) ·
**ui.html** (mobile gate + "+ New task"). Decisions log: `sessions/role-prompt-redesign-checklist.md`.
