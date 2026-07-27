# Session handoff — role tags, Slack wiring, todo-flow legibility (2026-07-27 → 28)

Long session. Broker repo `~/dev/sys-buddy`, plus the separate website repo
`~/dev/sysbuddy_website`. **581 tests green.** One PR open, some work still uncommitted.
This supersedes `2026-07-27-role-tags-and-slack-wiring.md` (written mid-session) — read
this one; the other is a subset.

---

## 1. STATE RIGHT NOW — read this first

**Branch:** `feat/role-tags-and-slack` · **PR #43** OPEN, not merged —
https://github.com/tooney92/sys-buddy/pull/43

7 commits on the branch:
```
ba505bf docs: detail the Playwright setup on the join page and at pre-flight
c9ef2e2 fix: todo stepper labelled the phase that just ended, not the one underway
61c126a docs: backfill releases/ v1.2.0-v1.4.0 and write v1.5.0
d9fed23 feat: role tags (@BE/@FE/@MB/@DE), designer role, and Slack that actually fires
4276ab2 fix: short needles made readiness checks unfalsifiable
93b0401 fix: look up local identity by name, not role — it duplicated agents
9c0d409 chore: pin dev/test brokers to :9292 and keep private dirs out of git
```

**UNCOMMITTED** (the next-step feature — an agent built it, it is green, it is NOT in the PR):
```
 M CLAUDE.md                      ← new "Concepts" section linking docs/todo-flow.md
 M src/sys_buddy/api.py           ← ships state.next_step() on every todo
 M src/sys_buddy/state.py         ← next_step() + _who_label/_next/_NEXT_TOOLS
 M src/sys_buddy/ui.html          ← nextStepHTML() under the mini-stepper
?? docs/todo-flow.md              ← the concept doc
?? tests/test_todo_next_step.py   ← 20 tests
?? .claude/                       ← still untracked, unrelated
```
Committing these adds them to PR #43. Merging #43 cuts **v1.5.0** automatically
(release-please reads the `feat:`).

**Website:** `0e35f7c` pushed to `main`, in sync (Netlify auto-deploys).
**demo/v1.5.0/:** 14 files (13 PNGs + README), gitignored, for Claude Design.

---

## 2. ⚠️ RULES LEARNED THE HARD WAY

- **NEVER bind or test on `:8787`.** The owner runs a live broker there. A second broker
  cannot bind it — it dies with `errno 48` **while your requests silently go to the LIVE
  broker**. This cost real time: it presented as a bogus "Access denied / 401" that was
  actually a dev-db token hitting the production broker. Test on **9292**
  (`SYS_BUDDY_DEV_PORT` / `config.DEV_PORT`), throwaway db `~/.sys-buddy-dev/*.db`.
  **Never `pkill` a broker you did not start** — match on your own port only.
- `ls` is aliased to **eza**, which reads `-t` as a time-FIELD flag and swallows the next
  argument, so `ls -t *.png` silently returns nothing. Use `command ls -t`.
- Playwright screenshots must be saved under `/Users/anthonynta/dev/sys-buddy` (MCP
  allowed-roots). The scratchpad is rejected.
- The dashboard strips `?v=` from the URL after load (by design). Load `/ui?v=<token>`
  FIRST, then click through; going straight to `/ui?v=…#/task/x` drops the token and 401s.
- `git add` everything then committing twice = the second commit is empty. Stage per
  commit. (Happened; fixed with a soft reset.)

## Environment changes made this session (outside the repo)

1. **`sysbuddy` now runs the DEV CHECKOUT.** `~/.zshrc` — the old alias is now a
   **function** routing through `uv run --directory "$SYSBUDDY_DIR"`. Bare `sysbuddy`
   opens the GUI; anything else passes through.
2. **Dev runs use a separate db**: `SYSBUDDY_DEV_DB=~/.sys-buddy-dev/sys_buddy.db`, set
   inline per invocation. The released `sys-buddy` keeps `~/.sys-buddy/sys_buddy.db`.
3. Released build upgraded 1.3.0 → **1.4.0**.

⚠️ `--version` does NOT distinguish dev from released (pyproject only bumps at release).
Verify with `uv run --directory "$SYSBUDDY_DIR" python -c "import sys_buddy; print(sys_buddy.__file__)"`.

---

## 3. WHAT SHIPPED

### Role tags — `@BE` / `@FE` / `@MB` / `@DE`
`service.ROLE_TAGS` + `resolve_role()`: exact → case-insensitive → tag. `post_message`
resolves BEFORE storing so `to_role` on the row is always canonical. Owner chose **both
layers** (prompt + broker) so a tag reaching the wire delivers instead of erroring. A real
role literally named `be` still wins over the tag.

### `designer` role
`--role-designer` (light + dark), `RL` entry, Tips legend. Previously rendered grey.

### Missing shortcodes
`upto` existed in the debug briefing but not the cheatsheet — and the **contract** briefing
never taught it at all. Added, plus `notify`. The cheatsheet now renders the tag list **from
`ROLE_TAGS`**, so it cannot drift again.

### Slack — was never wired on the paths people use
`slack_webhook` was read in ONE place, inside `cmd_serve`. Both `sys-buddy local` and
`sys-buddy gui` built a Config without it, so every ping silently returned "No Slack webhook
configured". Now read in `_cfg_from_args` (all CLI modes) and in `gui.py:_run_broker`.
Added `slack.validate_webhook()` / `is_configured()`, GUI bridge (`slack_status`,
`set_slack_webhook`, `test_slack`), a Host-screen password field with a **Send test message**
button, a header chip that renders only when armed, and agent training (charter paragraph,
both briefings, cheatsheet row, graded pre-flight question).

### Todo stepper relabel
`planning · building · ready · verified` (was `planning · locked · build · verified`). The
old labels were one step behind reality: `locked` sat current while people built, and
`build` only lit once the producer was already live. `contract_locked` now lights
**building**; `ready` matches the shorthand the producer types. Pure label change — the
numeric mapping was already correct.

### Playwright setup docs
`join.html` pointed at `@latest` while the repo pins `0.0.77`. Pinned it, added the
`.mcp.json` alternative, and a "what to expect" (slow first run, its own window on a
throwaway profile, no saved sessions). `readiness_check()` now returns a **`notes`** field
carrying the same tip — deliberately NOT a graded question.

### Contextual "next step" on the todo card (UNCOMMITTED — an agent built this)
`state.next_step(conn, task_id, todo_id)` returns `{stage, who, who_label, cmd, alt, tool,
text, human, done}`. **Derived from the same predicates the writes enforce**
(`_producer_role`, `_signatures_for`, `_current_locked`, `todos.status_of`, `MAX_STRIKES`)
— never a second copy of the rules. `api._todos_for` ships it; `ui.html` renders it under
the mini-stepper. Real output:

```
[both accepted]  waiting on: backend or frontend · type: pc #1
  "Everyone agreed on WHAT. Next is HOW: one of you proposes the contract — `pc #1`.
   Whoever proposes it becomes the PRODUCER for this deliverable, so there is no producer yet."

[locked]         waiting on: backend · type: ready #1
  "The contract is locked and nothing is building yet — backend proposed it, so backend is
   the producer and owes the build... Only they can: the broker refuses `ready` from anyone else."
```
It narrows as signatures arrive (`backend or frontend` → `frontend`). Two tests matter most:
one walks a todo start-to-finish doing **only** what the line says and asserts the broker
accepts every call; another asserts un-named parties are refused. That is the anti-drift
guarantee.

### Two latent bugs FIXED
- **`ensure_local_identity` duplicated agents** — looked up by `role` but inserts set
  `role = name`. Any role change → a second agent with the same name + a phantom role in
  `roles_json`. Produced 6 agents and 5 unclearable pre-flight chips on a 3-agent task.
  Now keyed on `name`. 3 regression tests.
- **`_contains` short-needle footgun** — plain substring matching meant grading on `"be"`
  also matched "because"/"before", making a readiness check unfalsifiable. Added
  `_contains_word`; `_contains_any` now auto-uses it for needles ≤ 4 chars. 3 tests.

### Docs & site
- `releases/` backfilled v1.2.0–v1.4.0 + `v1.5.0.md` written (dated 2026-07-27, phrased as
  upcoming — **bump the date if the merge slips**).
- `docs/todo-flow.md` (uncommitted) — the two-fields distinction, stage-by-stage walkthrough,
  permission gates, the two traps. Linked from `CLAUDE.md`.
- Website `4ecd1a9`: `#slack` + `#contributing` sections on `index.html`, `04 · Slack
  notifications` on `setup.html`.
- Website `0e35f7c`: a fourth "THE FLOW / Idea → verified" path in `learn.html`.

---

## 4. DECISIONS MADE (with the reasoning — do not re-litigate without it)

1. **Slack webhook is NEVER persisted.** Every credential in the db is a sha256 hash; a
   webhook must be replayed verbatim, so storing it would be the **first plaintext secret at
   rest**, and it is a bearer credential with no expiry. Two non-db paths: paste per session
   (GUI), or `SLACK_WEBHOOK_URL`. ⚠️ A hosted/VPS tier would REVERSE this — a managed deploy
   can't ask a customer to re-paste every restart.
2. **Role tags resolve at BOTH layers** (agent prompt + broker), so a tag on the wire
   delivers rather than erroring. Matches "the broker enforces; agents request".
3. **`designer` got its own colour** rather than the grey fallback.
4. **Playwright at pre-flight is a `notes` field, NOT a graded question.** Grading an
   optional tool makes it de-facto mandatory and punishes anyone testing another way.
5. **Todo stepper labels name what is HAPPENING, not the event that just fired.**
6. **Contributing is a SECTION on `index.html`, not a page.** Releases earned a page (a
   growing timeline); contributing is short, static, and hands off to GitHub.
7. **"Slack" was dropped from the website header nav** (footer keeps it) — two new nav items
   made the header wrap at 1440px. ⚠️ Owner has NOT confirmed this; revisit.
8. **New tool will be `decline_contract(reason, todo=N)`** — option A, consistent with its
   four scope-agnostic siblings (`propose_contract`/`lock_contract`/`get_contract`/
   `reopen_negotiations`, all taking `todo: int = 0`). Owner considered
   `decline_todo_contract` and chose consistency; renaming would have to be all five or none.
9. **NO `reject` that terminates a todo.** It would collapse the what/how layers and let one
   agent unilaterally destroy agreed work. `drop_todo` is already MUTUAL by design, and
   `host_drop_todo` is the human's unilateral escape hatch. Every case `reject` would cover
   already has a consent-requiring tool.
10. **No Claude footer / no Co-Authored-By** on any commit or PR, in either repo.

### Teaching principles the owner and I agreed (apply these to new work)
The author himself got stuck twice today — both times the system stayed silent, not because
docs were missing. So, in priority order:
1. **Errors teach the next move.** `state.py:817` is the model: it told the agent exactly
   what to do and the agent recovered. A refusal is read at peak attention.
2. **Screens say what they're waiting for.** Bar: *could someone who never read the docs
   finish a task using only what the screen says?*
3. **One vocabulary everywhere** — the word the human types must be the same word in the
   error, cheatsheet, stepper, next-step line, and docs. Every bug today was vocabulary
   drift. Render from ONE source (`ROLE_TAGS`, `next_step`), never a hand-maintained copy.
4. **Never make the human hold state in their head** — which is exactly the gap
   `decline_contract` closes.

---

## 5. PENDING — in the order the owner and I agreed

### A. Producer hardcode — DISCUSSED IN DETAIL, NOT STARTED (do this first)
The broker is **already correct**: `state.py:286 _producer_role()` derives the producer from
whoever proposed the *currently locked* contract, per-todo, newest version — *"Nothing is
hardcoded to 'backend'."* (called **model B**).

The agent-facing text never migrated. Two string checks:
```python
onboarding.py:161   is_backend = role.strip().lower() == "backend"
readiness.py:36-40  def _is_backend(role): return role.strip().lower() == "backend"
```
`onboarding` picks producer-vs-assessor briefing; `readiness` picks which pre-flight question.
**In a designer + frontend session neither role matches**, so BOTH get the assessor briefing,
BOTH are quizzed on assessing, and nobody is told they may propose — two agents each waiting
for the other, with the broker perfectly willing to proceed.

**Fix: delete the branch, don't make it smarter.** At briefing time there is no producer yet
(the contract doesn't exist) — and that's the answer, since the producer is whoever proposes.
One briefing teaching both halves; pre-flight asks every role about both.
`readiness.py:27` already concedes this in a comment.
Also carries the same assumption: `join.html:431` (hides Playwright for "non-backend" roles)
and `_contract_questions`' propose/assess split.
**Open sub-question:** ask both questions of everyone, or merge into one covering
propose-and-assess? I lean merge — the honest lesson is "either of you may propose; both must
assess."

### B. `decline_contract(reason, todo=N)` — agreed, not started
Today a contract has `lock` but no formal push-back, so an unsigned contract means EITHER "I
object" OR "I haven't looked" and nothing can tell them apart. Todos have `accept`+`decline`;
contracts are asymmetric. Note "I have a different approach" is *already* expressible
(`propose_contract` accepts a v2 from any party, any non-terminal state) — what's missing is
the **signal**, not the capability.

Build it well = these must all become true:
- the decline posts a message carrying the reason (into the thread)
- it lands in the event log
- `next_step` changes from *"waiting on frontend to sign"* to *"frontend declined v1 —
  someone proposes v2 with `pc #1`"*
- signing a declined version fails with an error naming the right next move

### C. Contract-flow stall — the `sign`-before-`pc` trap
Owner typed "lock the contract" 3× on a todo with nothing proposed. The agent was CORRECT
(nothing to sign) but took 3 rounds. Two fixes: (1) briefing — if the human says sign/lock
and nothing is proposed, propose first with your reading as a stated assumption, then sign,
don't stall (the agent did this unprompted on the 3rd reaffirm); (2) a `ship #N` shorthand
(propose-then-sign) since humans think in one move. The new next-step line already mitigates
much of this.

### D. Support non-Claude agents (Gemini, Cursor, …)
**The broker is already client-agnostic** — `server.py:61` runs `transport="http"`, standard
MCP at `/mcp` with `Authorization: Bearer <token>`. The coupling is entirely onboarding:
"claude" appears 24× in `onboarding.py`, 12× in `gui_app.html`, 9× in `join.html`, 2× in
`cli.py`; `configure_claude()` shells out to `claude mcp add`. Fix: show the raw endpoint +
token as the primary artifact, with per-client snippets layered on. ⚠️ VERIFY current MCP
client support per tool rather than asserting it — **Perplexity especially**, which may not
be an MCP client at all. Positioning win: "works with any MCP agent".

### E. Telegram as a second notification channel (contributor German's idea)
`state.py` calls `_slack()` at ~10 sites. Generalize to ONE notifier seam with Slack/Telegram
backends **before** a second channel is bolted alongside — cheap now (every call site was
just touched), expensive later. Telegram uses a bot token + chat ID, sidestepping the
credential-in-a-URL problem.

### F. Claude desktop app — blocked on an unverified fact
`middleware.py:69` reads the token ONLY from the `Authorization` header, so any client that
can't set one cannot connect at all. Unverified whether the desktop app can. If yes → docs +
GUI copy. If no → OAuth (substantial) or token-in-URL (a security tradeoff; mitigations
already exist: `agent_token_ttl` 24h on tunnels, `rotate_token`).

### G. Website loose ends
- Slack removed from header nav — owner never confirmed
- `releases.html` "latest" pulsing dot hardcoded to v1.4.0
- Docker listed under v1.2.0 — flagged twice across sessions, never actioned

### H. Housekeeping / ideas
- `.claude/` still untracked
- Capture German's **pricing tiers + VPS/cloud auto-deploy** idea in `ideas/` (gitignored),
  including that it reverses decision #1
- Consider whether the dashboard should show BOTH `status` and `state` — the two-field
  display is the root of the flow confusion

---

## 6. CONTEXT ON THE PROJECT

- Contributor **German Martinez** is engaged and proposing product direction (Telegram,
  pricing tiers). He still believes it's a "2 dev oriented tool" — the positioning gap is
  live in the wild. Roles are NOT capped: `--roles` takes arbitrary strings; what's bounded
  is that FOUR have first-class colour/tags (BE/FE/MB/DE), everything else falls back to grey.
- `designer` support is **v1.5.0**, not v1.4.0 (the owner said 1.4.0 in chat — it landed in
  `d9fed23`, still unmerged).
- The owner's next move: start a NEW session driving sys-buddy itself, pairing this agent
  with a frontend agent, to build a **new dashboard from a Claude Design**.
