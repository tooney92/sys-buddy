---
name: ship-release
description: Run the sys-buddy release sequence end to end — merge, unblock the release PR, publish to PyPI and ghcr, then write the release note and update the website. Use when asked to "ship", "release", "push to PyPI", "cut a release", or after a feature PR is ready to merge. Also use for the post-release half alone ("uswr", "write the release note", "update the website") when a version is already published.
---

# Ship a sys-buddy release

Merging to `main` is what starts a release. **Squash is the only merge method enabled**,
so the PR title becomes the changelog entry: `feat:` → minor, `fix:`/`chore:` → patch.

**A published PyPI version can never be withdrawn or reused, only superseded.** The dry
run belongs before step 2, not after step 5.

## Before anything

```
uv run pytest -q          # must be green; the current baseline is in the last release note
uv build && ls dist/      # dry run. Then check the artifact actually carries the change:
```

```python
import zipfile; z = zipfile.ZipFile('dist/sys_buddy-<version>-py3-none-any.whl')
'<a string from your change>' in z.read('sys_buddy/ui.html').decode()
```

`ui.html`, `join.html` and `gui_app.html` are package data — a change to them ships only
if it is in the wheel. `rm -rf dist` afterwards.

## 1. Merge the feature PR

```
gh pr checks <n>        # all green
gh pr merge <n> --squash --delete-branch
```

## 2. The release PR arrives BLOCKED with zero checks — this is not a failure

GitHub will not trigger workflows for anything its own bot token did, so the required
`pytest` and `conventional PR title` checks never fire.

**Do not reach for `gh pr close && gh pr reopen` first.** Step 3 fixes this as a side
effect, because a push from a real account triggers the workflows. Only if you have
somehow ended up with a green lockfile and still-empty checks does close/reopen apply.

The permanent fix is the `RELEASE_PLEASE_TOKEN` secret described in
`.github/workflows/release-please.yml`. It is still unset.

## 3. Relock — required every time, and it also fires the checks

release-please bumps `pyproject.toml` and **not** `uv.lock`, so the release PR always
arrives with the two disagreeing and the `lockfile matches the project version` guard
fails. That guard shipped in v2.6.0 and has fired on every release since.

```
git fetch origin release-please--branches--main
git checkout -B rp origin/release-please--branches--main
uv lock                                    # bumps sys-buddy to the new version
git add uv.lock && git commit -m "chore: relock uv.lock to X.Y.Z"
git push origin HEAD:release-please--branches--main
git checkout main
```

## 4. Merge the release PR → tag + publish

```
gh pr checks <release-pr>          # wait for green
gh pr merge <release-pr> --squash
```

Then find the run **by title**, not by `--limit 1` — the previous run is often still the
newest for a few seconds and you will inspect the wrong one:

```
gh run list --workflow=release-please.yml --limit 3 --json databaseId,displayTitle
gh run view <id> --json conclusion,jobs -q '.jobs[] | "\(.name)\t\(.conclusion)"'
```

Expect three jobs: `release PR / tag`, `publish to PyPI`, `publish image to ghcr.io`.
`skipped` means you are looking at a run where no release was created — wrong run.

Verify it actually landed rather than trusting the job:

```
curl -sS https://pypi.org/pypi/sys-buddy/json | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])"
```

PyPI's CDN lags a minute or two. Poll; do not conclude failure.

## 5. Upgrade the local install (if asked)

```
uv tool upgrade sys-buddy --no-cache
```

If it says **"Nothing to upgrade"** the index is still cached — uv reads the *simple*
index, which lags behind the JSON API. Then:

```
uv tool install sys-buddy --force --no-cache
```

The running broker keeps serving the OLD code from memory. It needs a restart, and the
dashboard's own banner will say so (`restart_needed` compares installed vs running).
**Do not start the owner's broker from an agent session** — it would die with the session.

## 6. Write `releases/vX.Y.Z.md` — NOT optional

`CHANGELOG.md` is generated and thin (squash collapses a whole release to one line), so
the prose note is where the detail lives. Read the two or three most recent notes first
and match them: they explain **why**, name the failure a fix prevents, and state what the
tests actually pin. Include `## Migration` and `## Tests` with the passing count.

`main` is **branch-protected** — this needs its own PR titled `docs: …` (no version bump).

## 7. Update the website — the step everyone forgets ("uswr")

Repo `~/dev/sysbuddy_website`, file `releases.html`.

Paste the new `<li class="rel">` **at the top of the list and stop**. The "latest" pill
and the pulsing dot are derived from `.releases > .rel:first-child` in CSS — they move on
their own. Do not add a badge, do not touch the entry below it.

Use `rel__type--minor` or `rel__type--patch` to match the bump.

**Write it for a reader, not a changelog.** "Sharing a screenshot stops costing your
agent its memory", not "proseBlocks is now shared". Then commit and push directly — that
repo is not branch-protected.

Render it before pushing:

```
cd ~/dev/sysbuddy_website && python3 -m http.server 8099 &
```

Load `http://127.0.0.1:8099/releases.html` in Playwright and confirm the new entry is
first, carries the LATEST pill, and the one below it dropped to a plain badge.

**This is not optional.** The page silently drifted four releases behind once, and the
desktop app's update banner links straight at it — so a stale page is what a user sees at
the exact moment they are told to upgrade.

## Known state to carry forward

- `releases.html` has a **pre-existing** unclosed `<li>` (one more open than close).
  Not yours; do not chase it, but do not let your own additions add to the delta.
- Every release since v2.6.0 has needed the step-3 relock. If release-please is ever
  taught to bump the lockfile, step 3 disappears — **and the step-2 close/reopen trap
  comes back**, because nothing else will push to that branch from a real account.
