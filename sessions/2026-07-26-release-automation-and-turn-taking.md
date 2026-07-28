# Session handoff — automated releases (PyPI + ghcr) + turn-taking/presence rework (2026-07-26)

Long session. Shipped **v1.1.1 → v1.2.0 → v1.3.0**, stood up fully-automated releases,
reworked the presence design, and moved the broker to a PyPI install. Everything below is
merged and released unless marked otherwise.

## What happened (in order)

1. **v1.1.1 — startup crash fix.** The broker aborted on boot against any pre-1.1.0 db:
   `sqlite3.OperationalError: no such column: todo_id`. Cause: the index on
   `contracts(todo_id)` lived in `SCHEMA` (runs via `executescript` BEFORE the migration
   that ADDs the column). Fresh dbs worked; existing dbs didn't — and every test used a
   fresh db, so it shipped in 1.1.0. Fix: create that index in `init_db` AFTER the
   migrations. Added a regression test that builds a pre-todos `contracts` table
   (`test_security_hardening.py`). Released v1.1.1 by hand (last manual release).

2. **Docker (PR #18 → integrated as #22, in v1.2.0).** Friend @glmartinez01's Dockerfile +
   compose + Makefile. Integrated onto main, dropping the version bump (conflicted with the
   single-sourcing), fixing `make url` (targeted a renamed `token` target → `viewer`), and
   removing a phantom `[1.0.2]` CHANGELOG entry. Verified by building + running the container.

3. **Version awareness (in v1.2.0).** `GET /api/version` reports the version the RUNNING
   broker booted with. `__version__` now derives from `importlib.metadata` (single source =
   `pyproject.toml`; `test_version.py` fails the build if they drift). GUI banner in
   `gui_app.html` compares installed / running / published(GitHub, opt-in) and nags a restart
   or an update. `updates.py` holds the comparison logic.

4. **Automated releases (release-please → PyPI + ghcr).**
   - `release-please-config.json` + `.release-please-manifest.json`, `release-type: python`.
   - `.github/workflows/release-please.yml`: on push to main, keeps a "chore(main): release
     X.Y.Z" PR; merging it tags `vX.Y.Z` and — same workflow, gated on `release_created` —
     publishes to **PyPI** (Trusted Publishing, OIDC, no stored token) and **ghcr** (built-in
     token). Publish is chained in-workflow because a GITHUB_TOKEN-created release doesn't
     trigger separate workflows.
   - CI gates: `.github/workflows/ci.yml` (pytest + a version-guard job that fails a PR that
     hand-bumps the version, skipping release-please's own branch) and `pr-title.yml`
     (Conventional-Commit PR title, since we squash-merge).

5. **Turn-taking / presence rework (in v1.3.0).** Dogfooding showed the always-listening
   SUBAGENT costs ~21k tokens/spawn — ruinous per-message. Superseded it:
   - `v2.md` entry revised (the design).
   - Prompt fix (`onboarding.py` `STAY_LISTENING` → `STAY_IN_THE_LOOP`, `rules.py`): the agent
     parks on `wait_for_message` in its OWN turn (one context), passes the floor explicitly
     ("over to you" / "I'll follow up" / "done for now"), and on a silent peer escalates via
     `report_status("stuck")` instead of hanging.
   - New soft **`waiting`** status (`state.py`): pings Slack + posts to the thread WITHOUT
     changing state/strikes or ending anything — the rung below `stuck`. Task-level only;
     works in contract AND debug mode.

6. **Docs (v1.3.0-era, PR #35).** `SETUP.md` (install via PyPI/Docker/source, run, pair,
   remote, dashboard, verify) + README install section now leads with `uv tool install
   sys-buddy` and `docker pull ghcr.io/tooney92/sys-buddy`. For the website / frontend.

7. **Ops: moved the broker to a PyPI install.** `uv tool install sys-buddy` (1.3.0).
   Repointed the `~/.zshrc` alias `sysbuddy` from `uv run --directory "$SYSBUDDY_DIR" …` to
   just `sys-buddy gui` (and `sysbuddy_ui` likewise). `SYSBUDDY_DIR` kept for source hacking.
   This decouples the running broker from the dev checkout (the branch you're on no longer
   changes what `sysbuddy` runs).

8. **Branch-protection lockdown.** `main` now requires status checks **`pytest`** +
   **`conventional PR title`**, `enforce_admins: true`, and **no required reviews** (solo
   config — a red build can't merge, not even via `--admin`, but you don't need a reviewer).
   The version-guard check is deliberately NOT required (it skips on release-please's PR and a
   skipped required check would deadlock that PR).

## Current state
- Released: **v1.3.0**, tagged and on GitHub, **PyPI** (`pip install sys-buddy`), and **ghcr**
  (`docker pull ghcr.io/tooney92/sys-buddy:1.3.0` / `:latest`). ghcr package is public.
- Suite: **~494 tests**, green.
- Releases are automated: merge feat/fix PRs → release-please opens a Release PR → merge it →
  tag + PyPI + ghcr. Version = biggest change since last tag, computed once (never over-counts).
- gh account: `tooney92`.

## Gotchas / open items (important for whoever resumes)
- **The live broker the owner is running is still 1.2.0** (started 14:25 from the old repo
  branch, before the new alias). It won't pick up 1.3.0 (and the prompt fix) until the owner
  restarts via the new alias (`sysbuddy` → installed `sys-buddy gui`). `/api/version` will read
  1.2.0 until then. DO NOT restart it for them — they had an active build session.
- **Already-onboarded frontend/mobile agents have FROZEN prompts** with the old listener
  block (generated by the 1.2.0 broker). A broker restart fixes only NEW prompts. For existing
  sessions, the human tells them: "stop the listener subagent; park on wait_for_message yourself."
- **`~/dev/sys-buddy` is shared** — the owner's `sysbuddy` used to run from it. Now it runs from
  the PyPI install, so branch-switching here is safe again. Keep this dir on `main` between tasks.
- **v2 gap discovered live:** there is NO "add a role to an existing task" command. Adding
  `frontend` to `light-dey-mobile-issue-f657` needed a manual `UPDATE tasks SET roles_json` +
  `sys-buddy invite`. Candidate feature: `sys-buddy task add-role <task> <role>` (open task only).
- Cosmetic: the 1.3.0 CHANGELOG has the `waiting` line twice (squash subject + PR-title ref).
  Harmless; dedupe if it bugs you.
- The `waiting` status is backend-only so far — the prompt doesn't yet mention it alongside
  `stuck` (deferred to avoid colliding with the prompt PR), and there's no dashboard badge for
  it (the thread bubble + event already render). Both are small follow-ups.

## Key decisions / mental model
- **Merging a PR ≠ releasing.** PRs pile on main; a release is merging the bot's Release PR.
- **The bump is computed once per release**, sized by the biggest change (feat! > feat > fix;
  docs/chore/etc. = no bump). Five feat PRs from 1.1.0 = one 1.2.0, not 1.6.0.
- **Tags are immutable checkpoints.** A release tags the *release commit* (the version-bump
  commit), which sits on top of all the feature commits since the last tag.
- **Run the broker from the PyPI install, hack from the source checkout.** Pinned version for
  running; local checkout only for dev.
