# Session handoff — marketing site refresh for current features (2026-07-27)

Work was in the **website** repo, not the broker: `~/dev/sysbuddy_website`
(GitHub `tooney92/sys-buddy-website`, separate from `tooney92/sys-buddy`). Static HTML,
**no build step**, deployed via **Netlify** on push to `main`. Everything below is committed
and pushed (commit `0917acf`); Netlify auto-deploys.

## Why
The site was **stale** — a keyword sweep found zero mentions of anything shipped since it was
written: todos, activity notes, file/screenshot sharing, listener presence, `waiting`, designer
role. The dashboard section was a hand-built HTML mock predating all of it. Goal: make the site
reflect where the broker actually is (v1.4.0), add a proper install walkthrough, and add a
releases page.

## What changed (files in `~/dev/sysbuddy_website`)

1. **Homepage Get started (`index.html#start`)** — was a bare 2-command dump. Expanded into a
   step-by-step **PyPI + Docker** install walkthrough (reuses the `.install` two-track grid, each
   a numbered `.steplist`).

2. **Extended the dashboard mock (`index.html#dashboard`)** — kept it a *vector HTML mock*
   (decision below), added the three missing surfaces:
   - **Todos roll-up** (`.dash__strip` → `.todos`/`.todo`): `3 / 5 verified`, per-todo state dots.
   - **Activity strip** (`.acts`/`.act`): role-colored pills (be/fe/de), newest-first, count badge.
   - **Screenshots & files card** (`.share*`) in the side column: inline thumbnail + zip download row.
   - Added two icons to the sprite (`assets/js/icons.js`): `sb-image`, `sb-download`.

3. **Reworked `setup.html`** into a numbered flow (kept the role-tab UX; `setup.js` only toggles
   `.setup-tab`/`.setup-track`, so new sections around `.setup-tracks` are safe):
   - **01 · Install** — new top section: PyPI + Docker walkthroughs (`.install` grid).
   - **02 · Host or join** — the existing tabs. Removed the install steps that were duplicated
     inside each track and **renumbered**: host track now 1–4 (was 1–5), buddy Way A 1–7,
     Way B 1–2 (was 1–3).
   - **03 · Once you're in** — new section: 3 cards (`.uses`/`.use`) for todos / activity /
     screenshots+files.

4. **New `releases.html`** — a version-by-version **timeline** (`.rel*` classes) built from
   `sys-buddy/CHANGELOG.md`, newest first (v1.4.0 → v1.0.0). Semver legend in the header, pulsing
   "latest" dot on v1.4.0, minor/patch/first chips, dates, Added/Fixed groups, links to each
   GitHub tag + the full changelog. **Linked from `index.html` header nav AND footer.**

5. **Surfaced the pre-flight readiness gate** (owner asked; it was only in the Security card):
   - `index.html#how` — new `.gate` row (`pre-flight → read Rules of Engagement → pass a readiness
     check → tools unlock`) sitting **above** the contract-mode `.machine` row, framed as a
     precondition for *both* modes. Tightened the `.then` prose to name the charter.
   - `setup.html` buddy Way A — new **step 6 "Your agent clears pre-flight"** (between paste-prompt
     and open-dashboard). Wording is accurate: agents are *briefed* + *pass a readiness check*,
     NOT "pretrained".

CSS all in `assets/css/styles.css`: `.install .steplist` fixes, dashboard strips, `.setup-section`,
`.uses`/`.use`, the `.rel*` timeline, `.gate`. Everything is **theme-aware** (light + dark tokens
already existed) and responsive.

## Current state
- Committed `0917acf` on `main`, pushed to `origin/main`. Netlify should have auto-deployed —
  **verify on the live domain** (sys-buddy.com) that the build went green.
- Verified locally via **Playwright MCP**: desktop + mobile, light + dark, all three pages.
- A local preview server is (was) running: `python3 -m http.server 8791 --directory
  ~/dev/sysbuddy_website` (pid ~67297). Throwaway — kill it when done (`pkill -f "http.server 8791"`).

## Gotchas / open items
- **Two repos.** Website = `tooney92/sys-buddy-website`; broker = `tooney92/sys-buddy`. This
  handoff lives in the broker repo's `sessions/` (where the convention lives) but describes
  website work.
- **Docker under v1.2.0 on the releases page** is a judgment call: the ghcr image is real and
  shipped around then, but release-please's auto-CHANGELOG only lists *version awareness* for
  1.2.0 (Docker was in the hand-written `[Unreleased]` block). Flagged to the owner; they did NOT
  ask to change it. If they want strict tag-accuracy, move/drop that bullet.
- **`learn.html` did NOT get a "Releases" nav link** — it uses the minimal back-to-site nav, so it
  was left alone. Only `index.html` (header + footer) links Releases.
- **copy.js copies `<code>` text only**; the `$` prompt lives in a `.cmd__prompt` span *outside*
  `<code>` — preserved in every new `.cmd` block so Copy never grabs the `$`.
- Browser caching bit us during testing (stale DOM after edits) — had to cache-bust with `?v=`.
  Not a site issue; just when re-testing over the local server.

## Key decisions (owner chose via AskUserQuestion)
- **Dashboard: extend the HTML mock, not embed real screenshots** — stays crisp/vector,
  theme-aware, zero image weight (tradeoff: it's a stylized mock, not the real UI). A real-
  screenshot pipeline exists in `sys-buddy/demo/` if they ever want proof shots later.
- **Install: rework `setup.html`, not a new `install.html`** — reuse the working page.
- **Pre-flight: surface in BOTH the join flow and How it works.**
- **Commit authorship: no Claude footer** (per `[[pr-authorship-no-claude-footer]]` — the site is
  public and reads as the owner's own).

## Possible follow-ups
- Confirm the Netlify production deploy succeeded.
- Optionally shoot real v1.4.0 dashboard screenshots (todos/activity/files) for the site later.
- If the owner wants, mirror the "Once you're in" feature list onto the homepage too (currently
  only the extended dashboard mock carries todos/activity/files on `index.html`).
