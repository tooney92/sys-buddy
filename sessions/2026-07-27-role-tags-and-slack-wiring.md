# Session handoff — role tags, `upto`/`notify` shortcodes, Slack wiring (2026-07-27)

Broker repo (`~/dev/sys-buddy`). **Nothing is committed** — 17 modified files in the working
tree, `pytest` green at **555 passed**, all changes proven live via Playwright. Targets
**v1.5.0** (release-please: `feat:` under a 1.x package → minor bump).

## Environment changes made this session (outside the repo)

1. **`sysbuddy` now runs the DEV CHECKOUT, not the PyPI release.** `~/.zshrc` — the old
   `alias sysbuddy='sys-buddy gui'` is now a **function** routing through
   `uv run --directory "$SYSBUDDY_DIR"`, so edits in `src/` take effect immediately with no
   reinstall. Bare `sysbuddy` opens the GUI; anything else passes through (`sysbuddy tasks`,
   `sysbuddy --version` — the old alias broke on flags because it appended `gui`).
2. **Dev runs use a separate db**: `SYSBUDDY_DEV_DB=~/.sys-buddy-dev/sys_buddy.db`, set inline
   per invocation. The released `sys-buddy` keeps `~/.sys-buddy/sys_buddy.db` (real data, 659k).
   Previously both shared one file, so dev schema changes ran against real task history.
3. **The released build was upgraded 1.3.0 → 1.4.0** (`uv tool upgrade sys-buddy`).

⚠️ `--version` does NOT distinguish dev from released — `pyproject.toml` only bumps at release,
so both read 1.4.0 while the code diverges. Verify with the import path:
`uv run --directory "$SYSBUDDY_DIR" python -c "import sys_buddy; print(sys_buddy.__file__)"`.

## ⚠️ Test-port rule (learned the hard way)

**Never test on `:8787`.** The owner is often mid-session there (`sys-buddy gui`). A second
broker cannot bind it — it dies with `errno 48` **while your requests silently go to the LIVE
broker**. This burned time this session: it surfaced as a bogus "Access denied / 401", which was
really a dev-db viewer token being presented to the owner's production broker.

Test brokers bind **9292** — now `SYS_BUDDY_DEV_PORT` / `config.DEV_PORT`, documented in
`CLAUDE.md` and `demo/README.md` (which previously said 8799 — reconciled to one value).

```
SYS_BUDDY_DB=~/.sys-buddy-dev/sys_buddy.db uv run sys-buddy local --port 9292
```

Check the port is free first, confirm the boot log prints the port you asked for (a failed bind
still prints the banner), and never `pkill` a broker you did not start.

## What changed

### 1. Role tags — BE / FE / MB / DE

A human types `sm @BE <text>` to their own agent instead of spelling the role out.

- **`service.py`** — new `ROLE_TAGS` + `resolve_role(value, roles)`. Resolution order: exact
  match → case-insensitive → tag expansion. Returns `None` so the caller owns the error.
  A task declaring a real role literally named `be` still wins over the tag.
- **`post_message`** resolves BEFORE storing, so `to_role` on the row is always canonical —
  delivery fan-out and the dashboard never see `"BE"`.
- Owner chose **both layers** (prompt + broker) via AskUserQuestion: agents are briefed to
  expand, and the broker resolves too, so a tag reaching the wire delivers instead of erroring.
- Taught in `rules.py`, **both** `onboarding.py` briefings, and the pre-flight `direct`
  question now asks what `sm @BE` means.

### 2. `designer` role in the dashboard

`RL` at `ui.html:72` only had backend/frontend/mobile/broker, so DE fell back to grey `D`.
Added `--role-designer` (`#B8823C` light / `#D6A05E` dark), an `RL` entry, and the Tips legend
now lists four roles.

### 3. Missing shortcodes

`upto` (share_activity) was in `onboarding.py` but **not** in the dashboard cheatsheet — same
drift class as commit `39250e0` ("cheatsheet missing todos"). Added, plus `notify`. The
cheatsheet now renders the tag list **from `ROLE_TAGS`** rather than hardcoding it, so it can't
drift again. Also found: the **contract** briefing never taught `upto` at all (only debug did).

### 4. Slack — was never wired up on the paths people use

**The bug:** `slack_webhook` was read in exactly one place, inside `cmd_serve`. Both
`sys-buddy local` and `sys-buddy gui` built a `Config` without it, so `notify_human` returned
"No Slack webhook configured" silently on both real on-ramps.

- **`cli.py`** — moved the `SLACK_WEBHOOK_URL` read into `_cfg_from_args`, so every mode gets it.
- **`gui.py:_run_broker`** — passes `slack_webhook` into its `Config`.
- **`slack.py`** — new `validate_webhook()` (https-only; deliberately NOT pinned to
  `hooks.slack.com`, since relays/test doubles are legitimate) and `is_configured()`.
- **GUI bridge** — `slack_status()`, `set_slack_webhook(url)`, `test_slack()`. Config is
  process-global, so Slack can be armed mid-session without restarting the broker.
- **Host screen** (`gui_app.html`) — `type="password"` field + **Send test message** button. A
  revoked/typo'd webhook is otherwise indistinguishable from a working one until something
  important fails to arrive. If `SLACK_WEBHOOK_URL` is exported the field says
  "Active from SLACK_WEBHOOK_URL" so an empty box doesn't read as "off".
- **Dashboard** — `viewer_block` gained `slack_active` (a BOOLEAN; the URL never crosses to the
  browser). Header chip renders **only** when armed; absent = no Slack.
- **Training** — charter paragraph in `rules.py`, `notify` in both briefings + cheatsheet, and a
  graded pre-flight question requiring evidence the agent knows it's terminal-events-only.

**Storage decision (owner's, and correct):** the webhook is **never persisted**. Every
credential in the db is a sha256 hash; a webhook must be replayed verbatim, so storing it would
be the first plaintext secret at rest — and it's a bearer credential with no expiry. Two
non-db paths: paste per session, or `SLACK_WEBHOOK_URL`.

## Files touched

`src/`: `api.py` `cli.py` `config.py` `gui.py` `gui_app.html` `onboarding.py` `readiness.py`
`rules.py` `service.py` `slack.py` `ui.html`
`tests/`: `test_api.py` `test_directed.py` `test_readiness.py` `test_slack.py`
Also: `CLAUDE.md`, `demo/README.md`, `.gitignore`, `uv.lock` (stale `1.1.1` → `1.4.0`, correct).

## Two latent bugs found and FIXED (not just documented)

### `ensure_local_identity` duplicated agents — `service.py`

The lookup matched on `role`, but inserts set `role = name`. The docstring says "make sure
the task and **this agent** exist" — identity is the NAME. So as soon as an agent's role
differed from its name (reassigned on the task, or seeded then corrected), the lookup missed
and inserted a **second agent with the same name**, appending that name to `roles_json` as a
phantom role. Result: duplicate agents and pre-flight chips no flow could ever clear. This is
what produced the five bogus chips in today's screenshots — it was a real bug, not just a
seeding artifact.

**Fixed:** look up by `name`; register the name-as-role only when genuinely inserting, so
re-seeing an existing agent can never grow `roles_json`. Returns the agent's CURRENT role.
Three regression tests in `tests/test_identity.py`.

### `_contains` short-needle footgun — `readiness.py`

Plain substring matching means a short needle makes a check unfalsifiable: grading on `"be"`
also matches "because"/"before", so every agent passes. Hit while writing the tag question.

**Fixed:** added `_contains_word` (regex word boundaries) and made `_contains_any`
automatically use it for needles ≤ 4 chars, so one weak entry in a mixed list can no longer
silently disable the whole check. Both helpers now carry docstrings saying which to use when,
and the module docstring states the floor. Three tests in `tests/test_readiness.py`, including
one that pins the "because" trap.

## Environment gotchas (not code — these are shell/tooling facts)

- **`ls` is aliased to `eza`**; `eza -t` reads `-t` as a *time field* and swallows the next
  argument, so `ls -t *.png` silently returns nothing. Use `command ls -t`.
- **Playwright screenshots must land under `/Users/anthonynta/dev/sys-buddy`** — the scratchpad
  is outside the MCP's allowed roots.
- **The `?v=` token is stripped from the URL after load** (by design — it keeps the token out of
  the address bar). Load `/ui?v=<token>` FIRST, then navigate to a hash route; going straight to
  `/ui?v=…#/task/x` drops the token and 401s.

## Open / next

1. **`demo/v1.5.0/`** — not created yet. Convention in `demo/README.md`: boot on 9292 + throwaway
   db, seed via the real flow, Playwright light+dark, write a README with concrete per-shot
   captions (Claude Design reads it for the demo videos). Shots needed: Slack field on the Host
   screen, the Slack header chip, a `slack` event in the Event log, role-tag `→ designer` chip,
   the `upto` activity strip, the extended cheatsheet. **Re-shoot** — today's captures predate
   the duplicate-agent fix and show five phantom pre-flight chips. `demo/` is gitignored.
2. **`releases/`** — stale at **v1.1.1** (four releases behind). Owner was asked whether to write
   `v1.5.0.md` and whether to backfill 1.2–1.4; **not answered yet**.
3. **Website** (`~/dev/sysbuddy_website`, separate repo, Netlify) — owner wants Slack
   notifications on the landing page + a "how to set up Slack to get notifications" walkthrough
   on `setup.html`. Explicitly queued for AFTER the broker work.
4. **Commit + PR** — nothing committed. Land the tags/`upto`/designer/Slack work as `feat:` for
   the 1.5.0 bump; `SYS_BUDDY_DEV_PORT` + `CLAUDE.md` are `chore:`. No Claude footer on commits
   or PRs (owner preference).
5. `releases.html` on the site is built from `CHANGELOG.md` and has a hardcoded "latest" pulsing
   dot on v1.4.0 — needs updating when 1.5.0 ships.
