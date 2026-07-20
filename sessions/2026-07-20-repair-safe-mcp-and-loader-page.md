# Session handoff — re-pair-safe MCP setup + full-page start loader

**Date:** 2026-07-20
**Task:** Fix the "MCP server sys-buddy already exists" collision on re-pair, and stop the
GUI's post-start invites screen from hiding below the fold (move it to its own page with a
loader).

---

## Working agreement
- One task, explain, gate. We test LOCALLY: `pytest` + Playwright MCP. Push to `main` only on
  owner's explicit say-so (this session: owner authorized ccw → commit → push at the end).
- Owner commands: **cfs**/**cfd** = read newest in `~/dev/screen shots` / `~/dev/download`.
  **ccw** = this handoff. **pwr** = prove live in Playwright. Concise replies. Desktop only.

## STATUS: shipped to working tree; committed + pushed at end of session. 246 pytest green.
Proven live in Playwright (running page + loader + command wrap, dark theme).

---

## What prompted it
Owner re-paired with a new ngrok URL + new token and ran the printed `claude mcp add …`, got
`MCP server sys-buddy already exists in local config` — `add` won't overwrite. Separately: after
"Create & start broker" the invites/connect screen rendered BELOW the form, so a new user who
doesn't scroll misses it.

## Decisions locked this session
- **Re-pair = remove then add.** `claude mcp add` refuses to overwrite, so every place we emit the
  setup command now runs/prints `claude mcp remove sys-buddy` FIRST. On a first-time setup the
  remove line is a harmless "not found" no-op.
- **Two plain lines, not a shell chain.** The copy-paste command is `remove\nadd` (newline), NOT
  `remove && add` / `remove; add`, so it pastes cleanly on bash, zsh, PowerShell, and cmd — the
  buddy may be on Windows.
- **Running screen gets its own page.** New `#view-hostrun` view so there's nothing above it to
  scroll past. (The buddy join flow already had its result inline; only the host start needed this.)
- **Loader is honest, not a fixed 2s.** Owner floated a fun 2s loader. Chose: show a full-page
  loader WHILE `start_host` really runs, with a MINIMUM visible time of 1.2s so it never flickers
  but never wastes a full 2s. Rotating lines ("Starting broker… → Minting invite links… →
  Almost ready…") give the effect without faking the wait.
- **Command `<pre>` wraps.** `white-space:pre-wrap` + `overflow-wrap:anywhere` so the full mcp URL
  and bearer token are readable instead of clipped off the right edge (this was pre-existing).

## What shipped (all pytest-green + proven live)
1. **`onboarding.py`** — single source of truth for the setup command:
   - `claude_remove_command(name)` → `["claude","mcp","remove",name]`.
   - `claude_setup_command(mcp_url, token, name)` → two-line copy-paste string (`remove\nadd`).
   - `configure_claude(...)` runs remove (best-effort, errors swallowed) BEFORE add, and returns
     the two-line string as `command`.
   - `_mint_host_seat(...)` `config_command` now uses `claude_setup_command` (was single-line add).
2. **`cli.py cmd_join`** — printed registration block now emits the `claude mcp remove sys-buddy`
   line above the `add`, with a one-line explanation of why.
3. **`gui_app.html`**:
   - New `#view-loading` (full-page centered `.spinner-lg` + rotating `#load-msg` + subtitle) and
     `#view-hostrun` (the moved `#host-result`, with its own "← Home" back button + header).
   - Router `views` map gained `loading` + `hostrun`.
   - Start handler: `startLoader()` (rotates lines every 500ms, `finish(cb)` clears the timer and
     waits out the 1.2s floor) → on success `handleHostResult` + `showView('hostrun')`; on error
     `showView('host')` and paint `hostErr` in the form. Removed the old inline `#host-starting`
     spinner + its two `.classList` toggles.
   - `pre.code` → `white-space:pre-wrap` + `overflow-wrap:anywhere`.

## Tests (246 green; +2 this session)
- `test_onboarding.py`:
  - `test_claude_setup_command_removes_before_adding` — two lines, remove first, token in add line.
  - `test_configure_claude_runs_remove_before_add` — records subprocess argv order (remove, add).
  - Existing `configure_claude` success/missing/nonzero tests still pass unchanged (remove call is
    swallowed; add is the source of truth).

## How it was proven (pwr)
GUI is pywebview, so drove `gui_app.html` over a throwaway `python -m http.server 8899` in
Playwright with a STUBBED `window.pywebview.api.start_host`. Verified: click "Create & start
broker" → full-page loader ("Minting invite links…") → auto-advances to the "Broker running"
page (no scroll) showing the two-line `remove`/`add` command fully wrapped (URL + token visible).
Confirmed the ">2s spin" the owner saw was only because I'd manually pinned the loading view for
an isolation screenshot — the real flow exits at max(work, 1.2s).

## Not done / possible follow-ups
- `configure_claude`'s remove step runs `claude mcp remove` even on first setup (prints "not found"
  to captured stderr, ignored) — intentional, keeps one code path.
- Loader min is a JS constant `LOAD_MIN_MS = 1200`; rotate lines in `LOAD_STEPS`.
- Buddy join result is still inline in `#view-buddy` (fits without scroll today); if it grows,
  give it the same dedicated-page treatment as `#view-hostrun`.
