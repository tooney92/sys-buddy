# Session handoff — per-task todo numbering, per-todo contract versions, Workflows panel (2026-07-30)

Branch `feat/todos-are-the-story`, on top of `1b1392c wip`. **Nothing is committed or pushed.**
22 files dirty, 2 untracked (`tests/test_shortcodes.py`, `tests/test_todo_numbers.py`).
`uv run pytest -q` → **764 passed** (session started at 669).

Playwright/`pwr` was **in progress when the session ended** — see "Where it stopped" below.

## Why — the owner's ruling

> "we use Todos so even one task is a single todo. no need to talk about the original
> contract to users. hide its references on the frontend. our story is this: all tasks are
> delivered by one or more Todos."

Three consequences drove everything:

1. Task-level contracts stop being **taught**. They stay in the DB and the broker still
   accepts them for pre-todo rows; no user-facing surface mentions them.
2. `#N` is **required** on every todo-scoped command. The single exception is `stuck`
   (`stuck #N` = one deliverable, bare `stuck` = escalate the task).
3. The word **"contract" does not appear in frontend prose.** The concept is **"the shape."**
   Only survivors are the literal tool names `propose_contract` / `lock_contract` in the
   steps table, which are the real functions the agent calls.

## 1 · Per-task todo numbering (the foundation)

**The bug, live on the owner's DB:** `todos.id` was `INTEGER PRIMARY KEY AUTOINCREMENT`, so
three tasks with one todo each had ids 1, 2, 3 — meaning one task's *only* deliverable was
`#3`. The panel copy we were about to write ("the first thing you agree is always #1") would
have been false the day it shipped.

**The fix — the GitHub pattern.** Global `id` stays the internal PK with every FK pointing at
it; a new per-task `number` is the human handle. Compound unique index
`todos_task_number ON todos(task_id, number)`.

- Numbers **never reused** (drop `#2` → next is `#3`). Why: a `#2` in message history must
  mean exactly one deliverable forever, and reuse silently re-points every past reference.
  Gaps are information — `#1 #3 #4` says a deliverable was dropped.
- Numbers **never renumbered** after assignment.
- **Two resolvers, and this is the load-bearing idea:**
  - `todos.get_row(conn, task_id, number)` — anything a human or agent typed.
  - `todos.row_by_id(conn, task_id, todo_id)` — internal FK joins only.
  - Rule: a value from a person goes through `get_row`; a value from the DB goes through
    `row_by_id`. Mixing them lands a request on the wrong deliverable, **quietly**, because
    both readings can be valid integers for the same task.

**Migration** (`db.py:_migrate_todo_numbers`): explicit `BEGIN IMMEDIATE` → ADD COLUMN →
backfill → NULL assertion → `CREATE UNIQUE INDEX IF NOT EXISTS` → commit, with `rollback()`
on any `BaseException`. Deletes nothing. Re-entrant: runs if the column is missing **or** any
`number IS NULL`, so a boot after a partial upgrade finishes rather than skips.

Backfill materialises `ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id)` into a **temp
table** first — a correlated window subquery re-runs against a table being written under it,
and `UPDATE ... FROM` needs SQLite 3.33 vs 3.25 for window functions.

**⚠ The NULL-distinct trap (bit us twice this session).** SQLite treats NULLs as **distinct**
in a unique index, so a half-backfilled table sails past the constraint and leaves todos
unreachable by `#N`. The index cannot catch it; only an explicit NULL assertion can.

Verified against a **copy** of `~/.sys-buddy/sys_buddy.db` (original never opened for write):
all three tasks now read `#1`, ids unchanged, 2 contracts + 17 messages + 5 decisions still
resolving, zero orphans, second boot a no-op.

## 2 · Agents no longer see the internal id

Dropped `todo_id` from all 7 agent-facing reply dicts in `state.py`, and added
`tools._agent_view()` / `_agent_views()` at the MCP boundary for the six todo ops — because
`todos.to_dict` is the **shared** wire shape (dashboard *and* `get_todos`) and its `"id"` key
was the same footgun under another name. `api.py` untouched; the dashboard still gets both.

Reason: handing an LLM two integers where only one is a valid selector invites it to pass the
wrong one. `get_row` would read an id as a *number* — usually a clean error, but a silently
wrong todo when a number of that value exists.

**Real leak found:** `reopen` is not in `api._EVENT_KINDS`, so `_render_detail` fell through
to its generic branch and dumped raw JSON to humans — literally
`{"from": "backend_live", "reason": "...", "todo_id": 7}`. Fixed by giving the event a `text`.

## 3 · Message chips: `#N` in prose changed meaning

`api._messages_for` scraped `#N` from message bodies and looked it up as an **id**. Correct
before (a `#N` in a body *was* the global id), wrong after. Reachable, not legacy-only:
`service.post_message` declares `todo_id: int | None = None`.

Now `_todo_keys()` returns both maps and resolves **number first, id as fallback** for
pre-numbering rows. Dropping the scrape entirely would have silently un-chipped 17 real
messages on the owner's DB.

Pinned by a fixture where **ids and numbers diverge** (task A takes id 1, so task B holds ids
2/3 as numbers 1/2) — an aligned `id == number` fixture proves nothing. Discrimination was
confirmed by temporarily reverting to id-first and watching the test fail `assert 2 == 3`.

## 4 · Per-todo contract versions

Found while seeding: `contracts.version` incremented across the **whole task**, so todo `#2`'s
*first* proposal was `v2`. On a six-todo task, todo `#6`'s first proposal read `v9` — eight
renegotiations that never happened. Owner: *"correct it we need consistency!"*

**This forced a full table rebuild.** The old `contracts` table declared
`UNIQUE(task_id, version)` in `CREATE TABLE`, and SQLite **cannot drop a constraint declared
that way** (`DROP INDEX` refuses an index backing a UNIQUE clause). With per-todo versions two
todos both hold a v1, so the constraint didn't merely mis-describe the data — it made the
second todo's first proposal **impossible**. Hence the documented
`foreign_keys=OFF` → create → copy → drop → rename dance (PRAGMA set *before* `BEGIN`; it is a
no-op inside a transaction), verified by row count, id set and `PRAGMA foreign_key_check`
before commit.

**Index choice: two partial indexes**, not one and not `COALESCE`:
- `contracts_todo_version ON (todo_id, version) WHERE todo_id IS NOT NULL`
- `contracts_task_version ON (task_id, version) WHERE todo_id IS NULL`

A naive `UNIQUE(task_id, todo_id, version)` would not constrain the legacy `todo_id IS NULL`
rows at all — the NULL-distinct trap again. `COALESCE(todo_id, -1)` was rejected: it invents a
sentinel that breaks if a real `todos.id` ever collides with it.

Task-level chains (`todo_id IS NULL`) are **not** renumbered. Unique indexes are dropped at
the top of the transaction and recreated at the end, because a renumber is a permutation and
SQLite checks uniqueness row-by-row — an intermediate row transiently collides with one not
yet moved.

Verified on copies of four real DBs. A 7-todo task whose chains read v1…v5 now reads v1 in
every chain, 9 signatures intact (they reference `contract_id`, not `version`).

## 5 · The diagnostics bug this exposed

`state.lock_contract` fetched `WHERE task_id = ? AND version = ?` — **the `todo` argument was
not in the lookup**. The cross-check that would catch a mismatch sat *after* the `locked` /
`declined` status checks.

So `lock_contract(todo=2, version=1)` fetched **todo #1's** contract, saw it locked, and said
*"contract version 1 is already locked and immutable; propose a new version to change it"* —
about a different deliverable, never saying so, with advice that was wrong (todo #2 had a
signable draft). Not a correctness bug (the cross-check still ran when status allowed, so you
could never sign another todo's contract) but the broker blamed the wrong thing.

Audit of every version lookup found two more:
- **`decline_contract` bare form** on a task with todos fell through to an unfiltered
  `_newest_contract` and could kill a proposal on a deliverable the caller never named. Now
  refused with the live-todo list.
- **`_newest_contract` / `_current_locked` / `_producer_role`** ordered by `version DESC`.
  Across chains that compares numbers from unrelated chains — todo #1's third revision would
  outrank todo #6's live v1. Switched to `id DESC` (identical within a chain, correct across).

## 6 · `SHORTCODES` — one vocabulary, two panels

The shorthand list was hand-typed in **two** places in `ui.html` (`shortcodesHTML`'s `cmd()`
rows and `workflowsPanelBody`'s ~20 `shChip()` calls) and had already drifted: Commands said
`pc [#N]`, Workflows said `pc #N`. The panel rebuild would have made a third copy.

Now one 27-entry `SHORTCODES` array + `SHORTCODE_BY_ID` + `SH(id, variant)`. Notable: **`scope`
is a field** (`required` | `optional` | `none`), so the notation ruling is **data, not prose**.
`stuck` carries `"bare"` and `"scoped"` so copy can name either half.

`tests/test_shortcodes.py` (21 tests) parses `SHORTCODES` out of `ui.html` as strict JSON and
asserts every named tool is actually registered via `build_server()`; that `stuck` is the only
`scope: optional`; that no `[#N]` survives on any line not about `stuck`; and that neither
panel hand-types a command. Behaviour-preservation was checked by **executing ui.html's real JS
under node** and diffing rendered output.

**The rot bug it killed:** the panel hand-typed *"whose four labels fold six march values:
planning · building · ready · verified"* while `TODO_STEPS` has **five** — `testing` was added
to the stepper in `19d0ba2` and this sentence never caught up. It now *counts*
(`TODO_STEPS.length`, `Object.keys(TODO_MARCH).length`) and derives the label list. **No count
is written in prose anywhere in the file.** `testing` earns its own node because `verified`
transitions from `testing` only, so `ok #N` visibly advances something.

## 7 · The Workflows panel (rebuilt)

Was 8 flat sections, all always expanded, most of them undifferentiated grey prose. Now three
collapsible sections, everything shut on open:

1. **How todos work** — *Agreeing what to build, then building it*
2. **Pre-flight** — *Why your agent's tools are locked*
3. **Debug sessions** — *Nothing to agree — just fix the thing*

"How contracts work" is **not** a peer section — a contract only ever lives on a todo, so it's
steps 3–4 of the story. Nothing from the old panel was dropped; the reference material
(`gates`, `traps`, disagreements, the two state fields) nests one level down, closed by
default, so you meet it when you've hit it.

- **Mode-aware.** `workflowsPanelBody(d)` now takes `state.detail` (it took no argument, which
  is why both flows were stacked unconditionally). `wfIsOpen()`: an explicit toggle wins, an
  untouched key falls back to the mode default, so a debug task opens on its own section
  without anyone having clicked.
- **Story before vocabulary.** Nobody meets *party*, *producer* or *consumer* until the story
  has shown what they do.
- **Cast: Tom 🐱 produces, Jerry 🐭 consumes, Spike 🐶** is on the task but not on this todo.
  The owner's insight, which is the actual thesis: *they don't get along, and sys-buddy doesn't
  ask them to — it just makes sure neither can skip a step.* `block` = Jerry finding the hole.
  Alternating icons show the producer/consumer turn-taking faster than the sentences do.
  Every emoji is `aria-hidden="true"`.
- **Humour is football-*vocabulary*, never player ranking** — a World Cup joke was cut for
  exactly that reason. Avoid permanently: age/retirement (dates badly), club allegiance.
- **Emoji only, never character artwork.** Names-as-examples in docs is low-risk IP use;
  illustrations are a different conversation.
- **`[2 agents]` / `[3+ agents]` toggle** re-tells the same seven steps with Spike added.
- Generated, never hand-typed: the seven-step table and every chip (`SHORTCODES`), the stepper
  labels (`TODO_STEPS`).
- New state `state.wf = {sec:{}, sub:{}, cast:'2'}`, **added to `sig()`** — a key missing from
  `sig()` never repaints. Handlers: `[data-wf-sec]`, `[data-wf-sub]`, `[data-wf-cast]`.
  Sections toggle independently; an accordion that shut its siblings would hide the thing you
  were comparing against.
- Dead `flowRow()` removed. Full copy deck lives in the scratchpad (see below).

Verified headlessly by executing the real JS: 3 rows closed, story hidden when shut, debug
self-opens with todos shut, 7 tool names present, stepper generated, chip says **signed** not
`contracted`, Spike gated to 3+, 21 `aria-hidden` spans, and **zero "contract" in prose** —
the only two hits are the tool names.

## Where it stopped

`pwr` was mid-flight. Already done:
- Throwaway DB at `~/.sys-buddy-dev-pwr/sys_buddy.db` seeded **through the real op path**
  (`propose_todo` → `accept_todo` → `propose_contract` → `lock_contract` → `report_status`),
  not a fixture dump. Seed script: `scratchpad/seed_pwr.py`.
  - `signin-flow` (contract, backend/frontend/mobile): `#1` verified, `#2` signed/building,
    `#3` ready awaiting the consumer, `#4` pending Spike's acceptance.
  - `cache-bug` (debug) with a two-message thread.
  - Every contract chain reads **v1** — under the old scheme these were v1, v2, v3.
- Broker running on **`:9292`** (confirmed in the boot log, `lsof` shows PID on 9292, never
  8787). Viewer token minted via `host-viewer`.
- Browser resized to 1440×1000. **No screenshots taken yet.**

To resume: navigate `http://127.0.0.1:9292/ui?v=<token>`, open the Workflows panel, screenshot
closed / How-todos-open / 3+ agents / a nested entry / debug-task default, light **and** dark.
Desktop only — sys-buddy is a laptop tool, skip mobile.

## Open items for the owner

1. **`SLAB` still says "contract locked"** (`ui.html:67`) — the *task* state chip label, so a
   user-facing "contract" reference survives outside the panel. Not touched: relabelling task
   states is broader than the panel and would ripple into tests. Needs a decision.
2. **`get_contract()` with no `todo`** on a task with todos is still permissive — returns
   whichever chain was written to last, self-identifying via its `todo` field. `docs/todo-flow.md`
   teaches `gc #N`, so it may want to be required too.
3. **`docs/todo-flow.md` has no per-chain version paragraph.** It never claimed versions were
   task-wide (so nothing is wrong), but "v1 means nothing without a `#N`" isn't stated.
4. Nothing committed. Per the OSS workflow this wants a branch + PR, owner merges.

## Traps worth remembering

- **SQLite NULLs are DISTINCT in unique indexes.** Cost us a near-miss twice. A partial
  backfill passes the constraint and leaves silently unreachable rows.
- **SQLite cannot drop a `UNIQUE` declared in `CREATE TABLE`.** Only a table rebuild sheds it.
- **A fixture where `id == number` proves nothing.** Two tests were self-confirming for exactly
  this reason (`test_decline_contract.py`, `test_todos_cli.py`) and are now real.
- **`'UNIQUE' in sql` is not a constraint check** — it matches the comment saying there is no
  UNIQUE. Use `PRAGMA index_list` and look at `origin`.
- **The `<script>` in `ui.html` is wrapped in an IIFE**, so nothing is hoisted into a
  `new Function(js)` scope. Strip the wrapper to test its internals under node.
