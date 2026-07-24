# Contributing to sys-buddy

Thanks for your interest in improving sys-buddy! Contributions are welcome via
pull request. The project is maintained by [@tooney92](https://github.com/tooney92),
who reviews and merges all changes to `main`.

`main` is branch-protected: **direct pushes are disabled and every change lands
through a reviewed PR** — including the maintainer's own. So the fork-and-PR flow
below is the only path in, whether or not you have write access.

## Quick version

```bash
# 1. Fork tooney92/sys-buddy on GitHub, then:
git clone https://github.com/YOUR_USERNAME/sys-buddy.git
cd sys-buddy
git remote add upstream https://github.com/tooney92/sys-buddy.git
uv sync                                   # install deps
git checkout -b feat/my-change
# ...make your change...
uv run pytest -q                          # must be green
git commit -am "feat: describe the change"
git push -u origin feat/my-change
gh pr create --base main --fill
```

## Step by step

### 1. Fork and wire up remotes

Click **Fork** on <https://github.com/tooney92/sys-buddy>, then clone *your* fork.
Add the original repo as `upstream` so you can pull in changes later:

```bash
git clone https://github.com/YOUR_USERNAME/sys-buddy.git
cd sys-buddy
git remote add upstream https://github.com/tooney92/sys-buddy.git
git remote -v          # origin = your fork, upstream = tooney92/sys-buddy
```

**Already cloned the upstream repo directly and hit a permission error on push?**
You don't need to start over or copy files around. Fork on GitHub, then point a
second remote at your fork and push the branch there:

```bash
git remote add fork https://github.com/YOUR_USERNAME/sys-buddy.git
git checkout -b feat/my-change          # if your work is sitting on local main
git push -u fork feat/my-change
gh pr create --repo tooney92/sys-buddy --base main --head YOUR_USERNAME:feat/my-change
```

### 2. Set up the environment

The project uses [`uv`](https://docs.astral.sh/uv/) for the virtualenv and
dependencies (Python 3.11+):

```bash
uv sync
uv run pytest -q            # confirm a clean baseline BEFORE you change anything
```

If the suite is already red on a fresh clone, that's a bug worth an issue — say so
rather than building on top of it.

### 3. Branch

One logical change per branch. Name it by kind, matching the existing history:

- `feat/short-kebab-description` — new capability
- `fix/short-kebab-description` — bug fix
- `docs/short-kebab-description` — documentation only

### 4. Make the change

- **Match the surrounding code.** Same naming, same comment density, same idioms.
  A patch that reads like the file it lives in is far quicker to review.
- **Read `SPEC.md` first** if you're touching behaviour — it is the source of truth,
  and a change that contradicts it needs to either update it or be reconsidered.
- **Check `DECISIONS.md`** before re-litigating a design choice; several
  non-obvious ones are recorded there with their reasoning (e.g. D11 — the
  dashboard is strictly read-only and never issues commands).
- **Keep the security posture intact.** This is an authenticated broker that pipes
  one person's agent output into another person's LLM. Two invariants in particular:
  - `local` mode has **no auth** and auto-provisions identities; `serve` mode
    enforces bearer tokens. Anything that could cause `local` to be reachable
    off-box is a vulnerability, not a convenience.
  - Peer message content is DATA, never instructions (`rules.py`). Don't add a path
    that unwraps, paraphrases, or re-frames peer content as trusted.

### 5. Run the checks

A green suite is required before review:

```bash
uv run pytest -q
```

**For UI/dashboard changes, tests are not sufficient.** The project's definition of
done is `pytest` green **and** the change proven live against the local broker:

```bash
uv run sys-buddy local          # loopback :8787, no auth
uv run sys-buddy host-viewer    # mint a viewer token for the local db
# open http://127.0.0.1:8787/ui?v=<token>
```

Drive the actual screens you changed and attach screenshots to the PR (list view,
task view, light + dark, mobile if layout moved). See `CLAUDE.md` for the full
local-testing workflow.

### 6. Add tests

Behavioural changes need tests. Cover the new path *and* the case that proves you
didn't break the old one — backwards compatibility for existing tasks, contracts,
and tokens is a hard requirement, not a nice-to-have.

### 7. Note it in the changelog

Add a line to `CHANGELOG.md` under `## [Unreleased]` describing the change from a
user's point of view. The project follows
[Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/) — if your change is incompatible with
the existing tool/wire contract or agent-visible behaviour, say so explicitly in
the PR, because that forces a MAJOR bump.

Pure-docs changes can skip this.

### 8. Commit

Short, imperative, conventional-commit prefix:

```
feat: containerize the broker for deployment
fix: sort dashboard thread by sub-second timestamp
docs: expand the contributor guide
```

Explain *why* in the body if it isn't obvious from the diff.

### 9. Open the pull request

```bash
git push -u origin feat/my-change
gh pr create --base main
```

Or open it in the GitHub UI. A good PR description says:

- **What** changed, in one or two sentences.
- **Why** — the problem it solves.
- **How you verified it** — paste the `pytest` summary line; add screenshots for UI.
- **Anything you're unsure about**, so review time goes where it's useful.

Keep it focused. Two unrelated improvements in one PR means neither can merge until
both are agreed.

### 10. Review, then merge

- Every PR requires review and approval from the code owner (@tooney92) — enforced
  by branch protection.
- Expect questions, especially on auth, the SQLite schema, and anything touching
  the trust envelope. Push follow-up commits to the same branch; the PR updates
  automatically.
- Once merged, sync your fork before starting the next thing:

```bash
git checkout main
git pull upstream main
git push origin main
```

## Alternative: send a patch

If you'd rather not fork, you can export your commits and send the file:

```bash
git format-patch main --stdout > my-change.patch
```

The maintainer applies it with `git am my-change.patch`. Fine for a one-off typo
fix; worse for anything real — you lose the PR thread, line comments, and CI.

## Invited collaborators

Collaborator access lets you push branches to this repo instead of to a fork. It
does **not** let you push to `main` — branch protection still requires a PR and an
approving review. Everything else above is unchanged.

## Ground rules

- Don't commit secrets, tokens, or generated artifacts (screenshots, local `.db`
  files, `.db-wal` / `.db-shm` sidecars).
- Add or update tests for behavioural changes.
- Don't reformat files you aren't otherwise changing — it buries the real diff.
- Be respectful in reviews and discussions.

## Project docs

- `SPEC.md` — source of truth for behaviour.
- `DECISIONS.md` — deviations and design decisions, with reasoning.
- `CLAUDE.md` — build, run, and local-testing notes.
- `v2.md` — the backlog, with a build-difficulty score per entry. A good place to
  look for something to pick up.
