# Contributing to sys-buddy

Thanks for your interest in improving sys-buddy! Contributions are welcome via
pull request. The project is maintained by [@tooney92](https://github.com/tooney92),
who reviews and merges all changes to `main`.

## How to contribute

1. **Fork** the repository (or, if you're an invited collaborator, create a branch).
2. **Create a branch** for your change: `git checkout -b my-change`.
3. **Make your change**, keeping the style of the surrounding code.
4. **Run the checks locally** — a green suite is required before review:
   ```bash
   uv run pytest -q
   ```
   UI/dashboard changes should also be sanity-checked against the local broker
   (`uv run sys-buddy local`) — see `CLAUDE.md` for the local testing workflow.
5. **Open a pull request** against `main` with a clear description of what and why.

## What to expect

- Every PR requires review and approval from the code owner (@tooney92) before
  it can merge — this is enforced by branch protection on `main`.
- Direct pushes to `main` are disabled; all changes land through PRs.
- Keep PRs focused: one logical change per PR is easier to review and merge.

## Ground rules

- Don't commit secrets, tokens, or generated artifacts (screenshots, local dbs).
- Add or update tests for behavioural changes.
- Be respectful in reviews and discussions.

## Project docs

- `SPEC.md` — source of truth for behaviour.
- `DECISIONS.md` — deviations and design decisions.
- `CLAUDE.md` — build, run, and local-testing notes.
