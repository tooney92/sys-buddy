# Session handoff — Dashboard live updates over SSE (2026-07-21)

## The problem
Owner exhausted the ngrok free tier (~21k requests). Server logs were dominated by
`GET /api/tasks` + `GET /api/task/{id}` — the dashboard was polling every 3s per open
tab (`ui.html`, old `setInterval(poll, 3000)`), ~1,200–2,400 req/hr each. ngrok's free
tier caps on **HTTP request count**, so polling drained the quota in about a day. The
`/mcp` agent traffic was a small minority — the dashboard poll was ~90% of the volume.

## Decision: SSE, not WebSockets
The dashboard is a **read-only viewer** — data only flows server→browser. SSE fits that
exactly and rides the existing viewer cookie + CSP, has built-in auto-reconnect, and is
already proven over this tunnel (`GET /mcp` is an SSE stream). WebSockets would add a
duplex channel we'd never talk back on, plus manual auth/reconnect. Key mechanism: ngrok
counts **requests**, and a streaming connection is **one** request that stays open —
messages over it don't tick the meter. So polling (O(time) requests) collapses to one
connection + O(changes) pushes.

## What shipped (branch `feat/dashboard-sse`, PR against main)
Built by two parallel agents against a pinned contract (`scratchpad/sse-contract.md`):
disjoint files, so no clobbering.

- **`src/sys_buddy/api.py`**
  - `_change_tokens(conn, viewer)` — pure helper; reuses `_list_tasks_for`,
    `_last_activity`, `_task_detail`, `_messages_for`, `_events_for`, `_agents_for`,
    `_contract_for` so scoping matches `/api/tasks`. Returns `(list_token, {task_id: token})`.
  - `_sse_events(request, viewer, *, poll=1.0, ping_every=15.0, idle_timeout=1800.0)` —
    module-level async generator (kept module-level so it's testable). Baseline tokens on
    entry (no synthetic first event), then each tick reads a fresh local `connect()`,
    diffs, yields only changed channels, pings when due, `asyncio.sleep(poll)`. Exits on
    `request.is_disconnected()` or the ~30min idle backstop.
  - `GET /api/stream` route in `register_api_routes` — `_resolve` viewer, unauth →
    identical `JSONResponse({"error":"unauthorized"}, 401)` (no stream). Else
    `StreamingResponse(..., media_type="text/event-stream")` with `Cache-Control: no-cache`,
    `X-Accel-Buffering: no`.
- **`src/sys_buddy/ui.html`**
  - Removed the 3s poll timer/`poll()`; KEPT `setInterval(patchVolatile, 1000)` (local).
  - `openStream()` (always starts with a catch-up fetch) → `EventSource('/api/stream')`;
    `tasks` → `fetchTasks()`, `task` → `fetchDetail(id)` only if `id === state.taskId`.
    Relies on EventSource auto-reconnect for tunnel blips.
  - `pauseStream()` = `es.close()` (client close is the ONLY true pause — server close
    just gets retried by the browser) + a bottom-center banner appended to `<body>` (so it
    survives `render()`'s innerHTML rewrites), themed with existing tokens.
  - Visibility gate: hidden → pause; visible → resume + catch-up.
  - Inactivity: passive listeners (mousemove/keydown/scroll/click/touchstart) reset a
    ~15min idle timer; idle → pause + banner; any activity/click → resume.

## Wire contract (v1) — do not drift
- `event: tasks` / `data: {"token": "<opaque>"}` → client `fetchTasks()`
- `event: task`  / `data: {"id": "<id>", "token": "<opaque>"}` → client `fetchDetail(id)` if open
- `: ping` comment ~15s. Tokens are opaque to the client.

## Proof
- `uv run pytest -q` → **262 passed** (+11 in `test_api.py`: token purity + generator framing/exit).
- Playwright (throwaway broker on **:8790**, throwaway db, seeded via real `task create` —
  owner's :8787 never touched): stream connects (200) / unauth 401; live push updated the
  DOM with **no reload**; polling dropped to **3** total `/api/tasks` hits over ~3 min (vs
  ~60 under the old poll); task created *while paused* did NOT appear (true pause, no
  reconnect); resume cleared the banner, caught up the missed task, reopened the stream
  (2nd 200). Screenshots: `pwr-sse-paused.png`, `pwr-sse-resumed.png`.

## Notes / possible follow-ups
- Change detection is server-side **local** polling (off-tunnel, cheap: ~0.3ms read then
  asleep ~1s per connection) rather than push-from-write. Deliberate low-risk first cut;
  can later signal an asyncio event from mutating tools without changing the wire contract.
- Untracked pwr artifacts + `.playwright-mcp/` in the repo root are NOT committed; consider
  a `.gitignore` entry (`pwr-*.png`, `.playwright-mcp/`) — offered, not yet done.
- Commit authored as **Tony**, no Claude attribution (per standing preference).
