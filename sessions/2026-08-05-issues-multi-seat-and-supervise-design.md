# Issues on debug tasks, more than one of a role, and `supervise` designed but not built

**Shipped:** `v2.5.0` (PyPI + ghcr + website) · **Open:** PR #65 · **1432 tests** on main
**Parked design:** `ideas/supervisor-mode.md` (gitignored, local only)

---

## What shipped — v2.5.0

**The desktop app can put more than one person on a role.** The broker has accepted "two
frontend developers" since v2.0.0 — `normalise_cast` numbers a repeated role into
`@frontend-1`/`@frontend-2`, and invites are minted per **seat**. The app could not say it:

```js
var cast = { backend:true };   // roles on the task; user adds more
```

`cast[role]` is on or off; there is no *how many*. So a two-frontend task was unreachable from
the surface almost every host uses, and that comment promised something the code could not do —
"user adds more" means more role **types**, which is not how anyone reads it.

The cast is a count now: a `+` on each chip, `×N` shown only once there is more than one, and
`castRoles()` expands the count into the repeated array `create_task` already reads. `owner`
stays capped at 1.

**The two-agent minimum now counts seats, not role types** — deliberately. Two frontends are two
agents who can hold a contract with each other, so "just FEs" is a legitimate cast.

Verified by stubbing the pywebview bridge in a browser and reading what the form *would* submit:
2 BE + 3 FE produced exactly `['backend','backend','frontend','frontend','frontend']`. And the
regression that matters — a one-of-each cast is **byte-identical**, no counts rendered, same
`['backend','frontend']` as before.

## What is finished and NOT merged — PR #65

**Issues on debug tasks.** A debug task was one problem you fix and then
`report_status("resolved")`; it can now carry multiple **issues** — what todos are to a contract
task, minus the contract.

```
issue "Login 500s"   raising IS the raiser's own accept   → pending
yes #2               every OTHER named party accepts      → accepted
fixed #2             each party says so independently
fixed #2             the last one lands it                → resolved
```

The task **auto-resolves** when every live issue is fixed and **un-resolves** when a new one is
raised, no human step either way — using the distinction already in `_assert_task_usable`: a
rollup-derived `resolved` reopens, a **human-escalated** one is terminal.

Design decisions, all deliberate:

* **Reuses the `todos` table.** Numbering, parties, accept/decline, drop, stuck and the rollup
  already exist there; an issue is a todo with the contract half removed.
* **Mode derived from `tasks.mode`, never a parameter.** A `debug=True` argument would be a
  second source of truth for a fact the broker already holds — the exact class of bug that left
  engagement mode with no way to create it.
* **Additive and opt-in per task.** A debug task with no issues behaves exactly as before. No
  migration. Existing debug tasks are **not** auto-converted into a synthetic "original problem"
  issue.
* **One new table, `todo_fixes`** — not an `issues` table. The per-party fix cannot ride on
  `todo_decisions`, which is `UNIQUE(todo_id, version, role)` with a single `decision` column, so
  a fix there would overwrite that seat's `accepted` and un-accept the issue.

1470 tests on that branch. **Merging it cuts 2.6.0.**

## `supervise` mode — designed, itemised, paused

Full record in `ideas/supervisor-mode.md`, including a 40-item list marked ✅/❓/🔨. Settled:

1. **The mode is called `supervise`.** A fourth mode, sharing engagement's
   deliverable/guidelines machinery rather than forking it.
2. **Most of it already exists.** Guidelines per role with `must_mention` grading, deliverables
   numbered `#N`, the accept-the-list gate — all shipped in v2.1.0.
3. **Deliverables assign SEATS, not role types** — because two devs really do get put on one
   deliverable, and you may want `@frontend-1` on #1 and `@frontend-2` on #2. New table
   `deliverable_parties`, named for seats because `todo_decisions.role` already carries a comment
   admitting "NAME LIES, VALUE IS RIGHT: this column holds a SEAT HANDLE".
4. **Success criteria attach to (deliverable, role type)**, hidden until the agent unlocks them
   before `verified`.

**My unresolved objection, item 24.** A self-assessment against a criterion the agent has just
been shown is a **checklist, not a check** — a self-graded exam handed out with the answer key.
It has real value (it forces a code read that would not otherwise happen, and the claim is
recorded) but it is not verification, and this project already has the vocabulary for that
distinction: engagement mode's *"Verified — this ran"* / *"Evidence reviewed"* / *"Not checked"*,
with the note that a reader **cannot infer** which they are looking at. Unless it is labelled as
a claim, a self-graded pass reads as verification — the exact failure engagement mode exists to
prevent.

**Still open and gating the build:** item 28 (one joint todo the assigned seats co-sign, or one
each), item 20 (may the supervisor set criteria for their own role, if they build), items 22/23
(is a self-assessment graded, and must it cite evidence).

## Things I got wrong

* **I claimed `propose_contract` said "debug tasks don't carry todos".** It did not — that string
  was in `todos` and `report_status`; `propose_contract` fell through to *"propose the todo
  first"*, which would have become actively wrong once debug tasks had todos.
* **I briefed "add no new table" and was wrong to.** `todo_fixes` is necessary for the reason
  above. A justified deviation, verified rather than accepted.
* **I wrote a CI check that could not run.** The lockfile guard was first an inline `run:` block
  needing nested quotes and a heredoc inside an indented YAML scalar. The YAML **parsed**, so it
  looked right — I extracted the run block from the parsed YAML, executed it, and it died on line
  1. It is a script file now. Lesson: a workflow that parses is not a workflow that runs.
* **I guessed a button id** (`btn-host-start`) instead of reading it, and the browser drive threw.
* **My first `castRoles()` fallback** used the expanded seat list where the distinct-type list was
  meant; harmless but wrong, fixed to `castTypes()`.

## Fixed along the way

* The event log rendered the **`todo` filter button twice** on any task with todos — it sat in the
  base list *and* was appended by a conditional whose comment says it should be the only source.
* On a debug task that button said `todo` above rows reading "Issue #N". The value stays `todo`
  (it is the stored event kind); only the label follows the task.
* **A CI guard for `uv.lock` drift.** release-please bumps `pyproject.toml` and never the
  lockfile, so after every release the lock names the previous version and every `uv run`
  rewrites it. Hand-relocked twice before this. The check deliberately does **not** exempt
  release-please's own PR — that PR is where the drift comes from.

## Process

**The release PR arrived `BLOCKED` with no checks for the sixth consecutive release.** GitHub
will not trigger workflows for anything its own bot token did. `gh pr close <n> && gh pr reopen
<n>` every time. Six occurrences is enough evidence: setting `RELEASE_PLEASE_TOKEN` is a one-time
`gh secret set` and ends it permanently — see the header of `.github/workflows/release-please.yml`.

**"uswr" is now a written rule**, in `CLAUDE.md` and in memory: updating the website's releases
page is part of a release, not an afterthought. Done for 2.5.0 the same day it shipped, which is
the first time that has happened without being asked.

## Open

* **PR #65** — issues on debug tasks. Green, unmerged, would be 2.6.0.
* **`supervise`** — four questions in `ideas/supervisor-mode.md` before any code.
* **Two follow-ups deliberately excluded from 2.5.0:** "YOU" still means a role, so with three
  frontends the host gets `@frontend-1`; and the app does not ask for seat **names**, so a
  dashboard shows `@frontend-2` where `@sarah` would read far better. The domain supports names
  already (`{'role':'frontend','handle':'sarah'}`) — only the app does not ask.
* **`RELEASE_PLEASE_TOKEN`** — ten minutes, ends a recurring tax.
* **Two stale brokers** on the owner's machine: `:9292` and `:9696` (`kill 64147 17986`).
