# Session handoff — v1 release setup + dashboard thread ordering (2026-07-23)

Continues from `2026-07-23-get-contract-shows-proposal.md`. That fix was merged, then we
stood up release/versioning and shipped two tags.

## What happened
1. **Merged** `fix/get-contract-shows-proposal` into `main` (`36dfa68`) — get_contract now
   returns the proposed contract (shape + signatures), staging_url withheld until lock.
2. **Set up release/versioning** and cut **v1.0.0** (`af7779e`, tag `v1.0.0`):
   - Semantic versioning (MAJOR.MINOR.PATCH). Version lives in BOTH `pyproject.toml` and
     `src/sys_buddy/__init__.py` — keep them in sync.
   - `CHANGELOG.md` (Keep a Changelog; `[Unreleased]` on top, move to a version section on
     release).
   - `releases/vX.Y.Z.md` per-version notes (filename = version).
   - Annotated git tag `vX.Y.Z` per release.
3. **Fixed dashboard thread ordering** and cut **v1.0.1** (`4c7b84c` fix, `d917e4b` release,
   tag `v1.0.1`).

## The thread-ordering fix (what & why)
- **Symptom:** dashboard message thread not in creation order (reported from a live session).
- **Cause:** `ui.html buildThread` merged messages + broker event-dividers (transition/lock/
  slack) and sorted by **minute precision** (`_hhmm` → `minutes()`), so same-minute items and
  message↔event interleaving fell out of order. (Agent-facing order was already fine —
  `check_messages`/`channel_history` sort by `id`.)
- **Fix:**
  - `api._messages_for` adds `"ts": created_at`; `api._events_for` appends `created_at` as a
    4th tuple element (existing consumers use `[0:3]`).
  - `ui.html` sorts by `ts` (sub-second) with an `ord` (id-order) tiebreak, **falling back to
    the legacy minute sort if any item lacks `ts`**.
  - Test updated: `test_api.py` event-shape assertion `len(row)==3` → `==4` (+ ts checks).
- **Why the fallback matters:** `ui.html` is served **fresh from disk per `/ui` request**
  (`api.py:632`), but `api.py` is loaded in memory — so during the window between editing and
  a broker restart, the live dashboard can run NEW js against the OLD api (no `ts`). The
  fallback keeps it behaving exactly as before in that window instead of breaking.

## Current state
- `main` pushed; tags `v1.0.0` and `v1.0.1` pushed to `github.com/tooney92/sys-buddy`.
- **271 tests pass.** No DB migration in either release.
- Active gh account: **`tooney92`** (left as-is per the owner; do NOT auto-restore to
  `anthugny` — switch back only when returning to OGTL ERP work).
- **The live broker was NOT restarted.** The ordering fix takes full effect only after the
  broker/app is restarted; until then the running broker serves the old behavior.

## Release flow (repeat every release)
1. Bump version in `pyproject.toml` + `__init__.py`.
2. Move `CHANGELOG.md` `[Unreleased]` → the new version section.
3. Write `releases/vX.Y.Z.md`.
4. Commit (`release: vX.Y.Z`), `git tag -a vX.Y.Z`.
5. Push `main` AND the tag (as `tooney92`). Optional: `gh release create vX.Y.Z --notes-file releases/vX.Y.Z.md`.

## Not done / backlog (`v2.md`)
- Three-role / scoped-parties contracts (`lock_contract` still needs ALL roles) — discussion #9.
- Non-HTTP / `interface_type` contracts — discussion #16.
- Image/screenshot attachments between agents.
- staging_url at host setup + allow localhost for same-machine tasks (GUI is always remote-mode,
  so it forces https + rejects loopback even for same-machine tasks).
- A GitHub Release page for v1.0.0 / v1.0.1 was offered but not created.

## Gotchas
- Push needs `tooney92`; `anthugny` 403s. `main` is PR-protected — owner bypass prints
  "Bypassed rule violations".
- `ui.html` is disk-served per request → any UI change must tolerate a stale in-memory `api.py`.
