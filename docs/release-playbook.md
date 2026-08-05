# Release playbook — how we ship, and how to set it up

Two parts. **Part 1** explains the system and why each piece exists — read it yourself.
**Part 2** is a prompt to paste into your coding agent; it will set your repo up the same
way, adapted to your stack.

The whole thing rests on one idea: **the version number, the changelog, and the release
notes should be consequences of how you already write commits — not a separate chore you
remember to do.** Every chore you have to remember is a chore that eventually gets
skipped, and a changelog that gets skipped once is never trusted again.

---

# Part 1 — the system

## 1. The PR title is the release instruction

We **squash-merge, and only squash-merge**. That single setting is what makes the rest
work: when you squash, your **PR title becomes the one commit on `main`**. A release bot
reads those commits to compute the next version and write the changelog.

So the title isn't cosmetic. It's an instruction to the release machinery.

Format: `<type>: <imperative summary>`

| type | version effect | use for |
|---|---|---|
| `feat:` | **minor** — 1.2.0 → 1.3.0 | a new, backwards-compatible capability |
| `fix:` | **patch** — 1.2.0 → 1.2.1 | a bug fix |
| `feat!:` (or a `BREAKING CHANGE:` footer) | **major** — 1.2.0 → 2.0.0 | an incompatible change to a contract users depend on |
| `docs:` | none | documentation only |
| `chore:` | none | tooling, deps, housekeeping |
| `refactor:` | none | internal change, no behaviour change |
| `test:` | none | tests only |
| `ci:` | none | pipeline / workflow changes |

```
feat: containerize the broker for deployment
fix: sort dashboard thread by sub-second timestamp
feat!: rename the report_status verbs   ← the ! marks a breaking change
```

Lower-case after the colon, imperative mood, no trailing period. A CI check enforces the
format on the PR title, so a malformed one can't reach `main` in the first place — you
find out while you're still in the PR, not when the release comes out wrong.

**The version never over-counts.** A release bundles every PR since the last one and bumps
**once**, by the largest change in the batch. Five `feat:` PRs ship as a single minor
release, not five. This is the thing people get wrong when they bump versions by hand.

## 2. What the bot does, so you don't

[release-please](https://github.com/googleapis/release-please) watches `main`. When it sees
releasable commits it opens — and keeps updating — a single **Release PR** titled
`chore(main): release X.Y.Z`. That PR contains exactly three things:

1. the version bumped in your project file (`pyproject.toml`, `package.json`, …)
2. the `CHANGELOG.md` entry, generated from the commit titles
3. the manifest bump

You review it like any other PR. **Merging it is the act of releasing**: it tags the
commit, cuts a GitHub Release, and — gated on that same run — publishes your artifacts
(package registry, container registry, whatever you've wired up).

Nothing is published because a human remembered to publish it. Publishing is what merging
means.

## 3. The two artifacts every version leaves behind

The generated `CHANGELOG.md` is **thin on purpose** — squash-merging collapses a whole
release to one line per PR. That's the right level of detail for "what changed", and
useless for "why, and what do I do about it". So each version leaves two hand-written
artifacts behind.

### `releases/vX.Y.Z.md` — the prose note

One file per version, written by hand after the release cuts. This is where the detail
lives. Ours run to a page or two and cover:

- a one-paragraph headline: what this release *is*, and how big a deal it is
- **Added / Fixed / Changed** — in prose, explaining the reasoning, not restating the diff
- **Migration** — what a user has to do. Say **"None."** explicitly when it's none; a
  missing section reads as "the author forgot", and the reader has to go and check.
- **Tests** — the count and what the new ones actually prove

Write it for the person deciding whether to upgrade. The changelog says *what* changed;
this says *whether you care*.

### `features/vX.Y.Z/` — the design bundle

This is the piece most teams don't have, and it's the one that pays off repeatedly.

```
features/v1.1.0/
├── README.md          the feature explained + a "Handoff for Design" section
├── screens/           numbered PNGs, real data, light AND dark
│   ├── 01-task-list-rollup-light.png
│   ├── 05-todo-task-dark.png
│   └── …
└── seed_demo.py       creates the exact data state those screens show
```

**Why the screenshots are committed to git.** Our UI doesn't exist as a static file — it
builds its DOM at runtime from live API calls, so you cannot see it without a running
server, a seeded database and an auth token. That means the visual design of v1.1.0 is,
by default, *unrecoverable* the moment the demo database is deleted.

Checked-in screens fix that, and they earn their disk space three ways:

- **An agent can see your product.** Point Claude at `features/v2.4.0/screens/` and it can
  match your actual visual language — spacing, badge shapes, how dark mode differs — instead
  of inventing a design system that doesn't look like yours. This is the biggest win and the
  reason the folder exists.
- **Marketing and demo assets** have a source that isn't a Slack thread from four months ago.
- **Visual regression by eye.** "Did the stepper always look like that?" has an answer.

Rules that keep the folder honest:

- **Real seeded data, never mockups.** A screenshot of a mockup is a screenshot of a lie.
- **Numbered filenames, with the theme in the name.** `03-todo-task-list-light.png`. The
  number is the narrative order; the name is the answer to "which one do I use".
- **Both themes**, if you have both. Dark mode captured as an afterthought looks like an
  afterthought.
- **A fixed capture size** — we use 1440×1000 — so the set is comparable across versions.
- **`seed_demo.py` beside them**, so any *other* screen can be captured later. The screens
  are the cache; the seed is the source. Have it create a deliberate spread of states
  (something pending, something done, something empty) — the empty case is what proves you
  didn't break the old view.
- A **README table** listing each file, what it shows, and which is the hero shot.

Not every release needs one. A `fix:`-only patch doesn't. Anything with a visible surface
does.

## 4. The gotcha that will bite you exactly once

**The Release PR will arrive blocked, with an empty checks list, and nothing red to explain
why.** This is not a failure. GitHub refuses to trigger workflows for anything its own
built-in bot token did — a loop guard — so a Release PR opened by the bot gets *no*
`pull_request` runs at all. If your required checks are `tests` and `conventional PR
title`, they never run, and the PR sits unmergeable.

Unblock it:

```bash
gh pr close <n> && gh pr reopen <n>   # reopening is attributed to you, so checks run
```

Fix it permanently: give the bot a fine-grained PAT (`Contents: read+write`,
`Pull requests: read+write`) as a `RELEASE_PLEASE_TOKEN` secret. Then its PRs trigger CI
like anyone else's.

Leave the fallback in place — a fresh clone or a fork has no such secret and must still be
able to release.

This one cost us the same twenty minutes across four separate releases before it got
written down. Which is the argument for writing things down.

## 5. The sequence, end to end

1. Branch. Do the work. Open a PR with a **conventional title**.
2. CI green → **squash-merge**. The title lands on `main`.
3. release-please opens/updates `chore(main): release X.Y.Z`.
4. Merge the Release PR → tag + GitHub Release + automatic publish.
5. **Hand-write `releases/vX.Y.Z.md`.**
6. **If it has a visible surface: capture `features/vX.Y.Z/`** — seed, screenshot light and
   dark, write the README.
7. If you have a public releases page, update it **in the same sitting**. Ours drifted four
   releases behind while the app's update banner linked straight to it — so a stale page was
   exactly what a user saw at the moment they were being told to upgrade.

One thing to internalise: **a published package version can never be withdrawn or reused,
only superseded.** So a dry run belongs before step 2, not after step 4.

---

# Part 2 — the setup prompt

Paste everything in the block below into your coding agent, at the root of the repo you
want to set up. It's written to be run against an existing repo of any stack.

````markdown
Set this repository up with a conventional-commits release pipeline. Work through the
steps in order and tell me what you changed at the end. Ask me before anything
destructive; everything here should be additive.

## 0. Survey first — don't assume

Report back before writing anything:
- Language/stack and the file holding the version (`pyproject.toml`, `package.json`,
  `Cargo.toml`, `build.gradle`, …).
- Current version, and whether git tags already exist.
- Existing CI workflows, test command, and whether tests currently pass.
- Whether there's an existing CHANGELOG, and whether commit history already looks
  conventional.
- Where the project publishes to, if anywhere (npm, PyPI, ghcr.io, a deploy target).

If tags/history exist, we are **adopting mid-stream**: seed the manifest with the current
version rather than starting at 0.1.0, and don't rewrite history.

## 1. Repo settings

Print the exact commands or click-path for these — I'll confirm before you run any:
- **Allow squash merging ONLY.** Disable merge commits and rebase merging. This is
  load-bearing: it's what makes the PR title become the commit on `main`.
- Set the squash-commit default to "pull request title".
- Enable auto-delete of head branches.

## 2. release-please

Create `release-please-config.json` and `.release-please-manifest.json`. Pick the correct
`release-type` for the detected stack (`python`, `node`, `rust`, `simple`, …). Seed the
manifest with the CURRENT version from step 0.

Config the changelog sections so noise stays hidden: `feat` → "Added", `fix` → "Fixed",
`refactor` → "Changed", and `docs`/`chore`/`test`/`ci` present but `"hidden": true`.

Add `.github/workflows/release-please.yml`:
- triggers on push to `main`; permissions `contents: write`, `pull-requests: write`
- uses `googleapis/release-please-action@v4` with
  `token: ${{ secrets.RELEASE_PLEASE_TOKEN || secrets.GITHUB_TOKEN }}` — the fallback
  matters so a fresh clone can still release
- publish jobs in the SAME workflow, gated on
  `if: ${{ needs.release-please.outputs.release_created }}`. They must be in this
  workflow, not a separate one triggered by the release event — a release created with
  the built-in token does not trigger other workflows.
- Put a comment block at the top of the file explaining the token/checks gotcha (see §4
  of the playbook I'm giving you) and, ideally, a step that writes the "close and reopen
  the PR" instruction to `$GITHUB_STEP_SUMMARY` when running on the fallback token.
  Whoever hits this at 11pm should find the answer in the run, not in a search engine.

## 3. Enforce the PR title

Add `.github/workflows/pr-title.yml` using `amannn/action-semantic-pull-request@v5`,
triggered on `pull_request: [opened, edited, synchronize, reopened]`.

Allowed types: `feat fix docs chore refactor test ci`.
Set `subjectPattern: ^(?![A-Z]).+$` with a helpful `subjectPatternError`, so titles stay
lower-case and imperative.

Then tell me to mark this check and the test job as **required** in branch protection on
`main`, with the exact steps.

## 4. Folders and templates

Create, with a `.gitkeep` where empty:
- `releases/` — one hand-written prose note per version, `vX.Y.Z.md`
- `features/` — one design bundle per visible release: `vX.Y.Z/README.md`,
  `vX.Y.Z/screens/*.png`, and a seed script that produces the data those screens show

Write `releases/TEMPLATE.md` with sections: headline paragraph, Added, Fixed, Changed,
**Migration** (with a note to write "None." explicitly rather than omitting it), Tests.

Write `features/TEMPLATE/README.md` covering: what the feature is and why, a table of
every screen (file, what it shows, theme, which is the hero), the suggested narrative
order, constraints someone capturing more shots needs to know, and the exact commands to
boot and seed the app locally.

Make sure screenshots are NOT gitignored — check the ignore file for a blanket `*.png`.
These are committed deliberately: they are how an AI agent sees what the product actually
looks like, and how the design survives the demo database being deleted.

## 5. Document it

Add a "Commit & PR title — Conventional Commits" section to `CONTRIBUTING.md` (create it
if absent) with the type→version-bump table, three or four real examples from this
repo's own history, and the note that a release bundles every PR since the last one and
bumps ONCE by the largest change in the batch.

If the repo has a `CLAUDE.md` / `AGENTS.md` / equivalent, add a short "Releasing" section:
the numbered sequence, the close-and-reopen unblock, and — explicitly — the two
hand-written steps that come AFTER the merge, since those are the ones that get skipped.

## 6. Prove it works

Do not declare done until:
- both workflows pass `actionlint` (or are otherwise syntax-checked)
- the existing test suite still passes
- you've opened a trivial `chore:` PR to confirm the title check fires, and a
  deliberately bad title to confirm it FAILS — a guard nobody has seen fail is not a
  guard

Report what you set up, what you couldn't (and why), and the exact list of things I need
to click in GitHub settings myself.
````

---

## A note on adopting this

Don't backfill. Start the discipline from the next PR; `releases/` and `features/` fill in
going forward. A half-invented history is worth less than an honest short one.

The part that actually takes discipline is steps 5 and 6 of the sequence — the two
hand-written artifacts, after the merge, when the feature already feels finished. Everything
before them is automated and will happen whether you're paying attention or not. Those two
only happen if you do them in the same sitting.
