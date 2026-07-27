# Session handoff — CLI URL fix, planning rename, live remote debugging (2026-07-21)

Continues the SSE work in `sessions/2026-07-21-dashboard-sse-live-updates.md`.
Everything below is **merged to `main`** (owner is sole merger; merged via admin bypass).

## Shipped this session (all on main)
`main` chain: #2 OSS → #3 SSE → #4 CLI/README → #5 planning rename. Suite **267 passed**.

### PR #3 — dashboard live updates over SSE (merged)
Replaced the 3s dashboard poll with one long-lived `GET /api/stream` (SSE) + client
pause/resume. Fixes the ngrok request-count exhaustion (polling was ~90% of tunnel
traffic). Details in the SSE handoff. Change detection is server-side LOCAL polling
(off-tunnel); possible future upgrade = push-from-write via an asyncio event, no wire
change needed.

### PR #4 — `fix/cli-public-url-and-readme` (merged)
Real bug found by actually running the README's remote flow: `invite` / `host-viewer`
printed **loopback** `http://127.0.0.1:8787/...` links because `_cfg_from_args` (their
config path) never read `public_url` — only `serve` did. A host would hand a buddy a dead
`127.0.0.1/join` link.
- `_cfg_from_args` now sets `public_url` from the command's `--public-url` or
  `$SYS_BUDDY_PUBLIC_URL`; `invite` gained a `--public-url` flag; `host-viewer` prints the
  real dashboard URL (was a `<broker-url>` placeholder).
- README: remote flow now `export SYS_BUDDY_PUBLIC_URL=…` once (serve + invite agree);
  documents the browser `/join` path + shell alias (`alias sys-buddy="uv run sys-buddy"`)
  + the `--name` agent alias; dropped "two agents" framing; status/test-count refreshed.
- Tests: `tests/test_cli.py` (env path, flag override, placeholder gone).

### PR #5 — `rename/planning-vocab` (merged)
"negotiation" → "planning" **everywhere a human/agent reads it**: dashboard state pill +
stepper (`SLAB`/`STEPS`), event-log label → **"replanning"**, empty-state copy, the
`role_prompt`, readiness questions, tool docstrings, readiness-pass "next" hint, GUI mode
hint. Also rebuilt the dashboard **"Short commands"** card: one-line gloss per command,
grouped Messaging/Contract/Status/Meta, and added the previously-missing `reopen`.
- LEFT UNDER THE HOOD (no API/behaviour change): state key `contract_proposed`, tool name
  `reopen_negotiations`, event-type key `renegotiation`, readiness id `renegotiate` /
  `_grade_renegotiate`. So DB/protocol/MCP surface unchanged — purely wording.
- Proven live in Playwright (planning pill + stepper active step + grouped command card
  + readiness Q10 wording): `pwr-planning-stepper.png`, `pwr-planning-commands.png`.

## Live remote session with a buddy (rhema-demo-6389 / signin etc.)
Owner ran a real remote session (GUI broker on `:8787`, `ngrok http 8787` →
`https://carmelo-convertive-neta.ngrok-free.dev`). Two issues debugged:
1. **Buddy "broker offline"** — the buddy's Claude was registered against
   `http://127.0.0.1:8787/mcp` (their OWN localhost), not the host tunnel. This is exactly
   the loopback bug PR #4 fixes. Fix relayed: buddy re-adds MCP with the tunnel URL + their
   token (`claude mcp get sys-buddy` to recover it; GUI-mode tokens don't expire). The
   buddy's agent had also wrongly suggested starting a local broker — in remote mode the
   broker runs ONLY on the host.
2. **Tools connected but not loaded** — `claude mcp list` = Connected but MCP tools absent
   in the session. Cause: MCP tools bind at `claude` process boot; adding/changing the
   server mid-session needs a full quit + relaunch (not a prompt re-paste). Escalation if
   still empty: `MCP_TIMEOUT=30000 claude` (ngrok handshake latency).
- Logs also showed the backend agent's `propose_contract` correctly REJECTED for bad spec
  shape (`request` must be a list of field objects) — validator doing its job, agent
  iterating. Not our bug.

## STILL OPEN / not done
- **`.gitignore`** for `pwr-*.png` + `.playwright-mcp/` — offered several times, never done.
  These keep showing up as untracked noise (and there's a stray `sessions/` + `v2.md` etc.).
- Session `sessions/*.md` handoffs are untracked by convention (not committed).
- The live GUI broker still runs the PRE-SSE code (PID from this session) — to actually get
  SSE live for the owner's real session, restart the `gui`/`serve` broker so the new
  `ui.html` + `/api/stream` load. (Merging to main doesn't restart the running process.)

## Working notes
- Commits authored as **Tony**, no Claude attribution (standing preference).
- Local-only testing; spare ports (8790/8792) + throwaway dbs for pwr — never touch the
  owner's live `:8787`.
