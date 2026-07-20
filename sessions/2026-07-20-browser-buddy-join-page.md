# Session handoff — browser-based buddy onboarding (/join) + pairing URL fix

**Date:** 2026-07-20
**Task:** Let a remote buddy set up with nothing but a browser + Claude Code. Add a
broker-served `/join` page reached by the invite link; fix pairing so buddies get the
remote (tunnel) URL, not loopback.

---

## Working agreement
- One task, explain, gate. Test LOCALLY: `pytest` + Playwright MCP. Push to `main` only
  on owner's explicit say-so (this session: authorized ccw → commit → push at the end).
- Owner commands: **cfs/cfd** = newest file in `~/dev/screen shots` / `~/dev/download`.
  **ccw** = this handoff. **pwr** = prove live in Playwright. Concise. Desktop-only.
- This session USED PARALLEL AGENTS (owner asked "fire up agents so we're faster"): one
  built backend plumbing, one built join.html, against a pinned shared contract; owner
  integrated + pwr'd.

## STATUS: shipped to working tree; committed + pushed at end of session. 251 pytest green.
Proven live in Playwright (pair success light+dark, incomplete-link guard, used-invite error).

---

## What prompted it
Owner's friend (remote, Windows) needs to use the app. The desktop GUI is host-only;
the buddy path was clone-repo + CLI. Also: the buddy's generated `claude mcp add` command
used `http://127.0.0.1:8787` instead of the ngrok URL (screenshot of the Join screen).

## Root-cause fix (pairing URL) — done earlier this session, folded into this commit
- `pairing.py` `/pair` route stamps `mcp_url`/`dashboard_url` from `cfg.base_url`, which is
  loopback because the GUI starts the in-process broker with `public_url=None` (`gui.py:63`,
  remote mode). The broker sits behind a tunnel it doesn't know about.
- Fix: `pairing.join` now `_rebase()`s the broker-returned URLs onto the origin the buddy
  actually reached (the invite link's origin) — authoritative from the buddy's side, and
  independent of broker config. Keeps the server's path+query (dashboard `?v=` token rides
  along). Covered by `test_rebase_*` and `test_join_rebases_loopback_mcp_url_to_tunnel_origin`.
- NOTE: the NEW web /join page sidesteps this entirely — it's served FROM the real origin,
  so `location.origin` is already correct. The rebase remains for the CLI/desktop path.

## Decisions locked this session
- **Buddy onboarding is a broker-served web page at `/join`** — buddy needs only a browser
  (no clone). Invite link = `https://<origin>/join#c=<code>`; code in the FRAGMENT so it
  never hits server logs (mirrors `/ui`'s care with `?v=`).
- **Redeem on CLICK, never on load** — invites are single-use; a prefetch/preview must not
  burn them.
- **Page builds command + dashboard from `location.origin`** (not server-echoed URLs), so
  the public URL is always correct with zero broker config.
- **Keep the `sb1_` blob too** (owner's pick) — web link is primary in the host GUI; `sb1_`
  is the secondary "Desktop app / CLI" option.
- **Scope = buddy page only** — host keeps the desktop GUI/CLI (no host web-setup page).

## What shipped (all pytest-green + proven live)
Backend (agent):
1. `api.py`: `GET /join` serves `src/sys_buddy/join.html` (unauth, `Referrer-Policy: no-referrer`), lazy file read so it registers before the file exists.
2. `pairing.py`: `/pair` response adds `prompt` (= `role_prompt(role, task_id, mode)`, mode read from the task row, default "contract") and surfaces `viewer_token` top-level. Lazy `from .onboarding import role_prompt` to dodge the circular import.
3. `onboarding.py`: `make_join_url(origin, code)` → `{origin}/join#c={code}`.
Frontend (agent):
4. `src/sys_buddy/join.html` (NEW): self-contained, theme-aware (light/dark via prefers-color-scheme + data-theme). Views: incomplete-link / intro (agent-name + Pair) / result. Reads `#c=<code>`; POST `/pair` on click; renders Step 1 command (2-line remove/add from `location.origin`), Step 2 prompt textarea, Step 3 dashboard link (`origin + /ui?v=<viewer_token>`); optional Playwright note for non-backend roles; privacy note; graceful error/retry.
Integration (owner):
5. `onboarding.py host_setup`: each invite entry now `{role, join_url, link}` — ONE mint per role, both links off the same code (minting twice would burn two codes).
6. `gui_app.html`: invite rows show the web `join_url` (primary, "Copy link") + a secondary `.invite-alt` line with the `sb1_` blob ("Desktop app / CLI"). Label reworded. New CSS `.invite-alt*`.
7. `cli.py cmd_invite`: prints the web join link + the `sb1_`/`sys-buddy join` fallback (origin from `get_config().base_url`).

## Contract the two agents shared (kept them from diverging)
`/pair` JSON: `{agent_token, viewer_token, task_id, role, mcp_url, dashboard_url, expires_at, prompt, rules}`.
Invite web URL: `{origin}/join#c=<code>`. Page builds command/dashboard from `location.origin`.

## Tests (251 green; new this session)
- `test_pairing.py`: `test_pair_response_carries_viewer_token_and_prompt`; `test_rebase_swaps_origin_keeps_path_and_query`; `test_join_rebases_loopback_mcp_url_to_tunnel_origin`.
- `test_onboarding.py`: `test_make_join_url_shape_and_fragment`; `test_make_join_url_trims_trailing_slash`.

## How it was proven (pwr)
Port 8787 was busy (owner's live broker), so booted on `--port 8790` with an isolated db
(`SYS_BUDDY_DB=<scratch>/join.db`), seeded task `join-demo` + a frontend invite, and drove
`/join#c=<code>` in Playwright: paired → command showed `http://127.0.0.1:8790/mcp` + real
token, dashboard `…/ui?v=<viewer>`, 3370-char prompt, Playwright note (frontend); light+dark;
`/join` with no code → incomplete guard (no /pair call); re-pair used code → "invite code has
already been used" + button re-enabled. NOTE: fragment-only nav (`/join` → `/join#c=`) is a
same-document nav and does NOT re-run the script — a real buddy opens the link fresh, so fine;
in tests, force `location.reload()`.

## Not done / follow-ups
- No host web-setup page (intentional; host uses GUI/CLI).
- The GUI in-process broker still starts with `public_url=None` — the web /join page doesn't
  care (origin-based), and the CLI/desktop path is covered by the rebase. Could later set the
  broker's public_url from the GUI for a fully server-authoritative setup.
- Stray untracked `pwr-*.png` + `.playwright-mcp/` in repo root (not committed).
- A "How to set up" walkthrough prompt for the marketing/business page was drafted for the
  owner (host + buddy tracks) — lives in chat, not the repo.
