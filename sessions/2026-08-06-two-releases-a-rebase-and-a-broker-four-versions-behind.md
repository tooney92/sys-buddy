# Two releases, a rebase that could have reverted one of them, and a broker four versions behind

**Shipped:** `v2.5.1` and `v2.6.0` (PyPI + ghcr + release notes + website) · **1476 tests** on main
**Closed:** PR #65 (rebased and merged) · **Live broker upgraded** 2.2.0 → 2.6.0
**Parked design:** `ideas/lessons.md`, `ideas/jira-and-issue-trackers.md` (gitignored, local only)

---

## Where it started — "did we fix the file upload issue?"

The honest answer was **the latest release contained the fix, but the agents were never told**.
v2.2.0 gave files their own door — `POST /files/{task_id}`, `GET /files/{task_id}/{file_id}` —
precisely so bytes stop going through a model. Only **two of the four** surfaces that teach file
sharing were updated.

Still teaching `upload_file(name, content_base64, content_type)` as the *only* way:

* `RULES_OF_ENGAGEMENT` — issued at pairing and served by the `rules` tool
* the contract/engagement block in `onboarding.role_prompt`

Which is to say: the two surfaces an agent is most certain to read were the two still sending it
down the path the release was written to replace. ~128k tokens for a 328 KB screenshot.

Both also spelled the accepted types by hand — omitting `text/html`, accepted since v2.2.0. **The
identical defect v2.2.0 fixed in the rejection message and nowhere else, so it grew back.** Every
surface now renders `files.types_sentence()`, generated from `ALLOWED_TYPES`.

`RULES_OF_ENGAGEMENT` became a `.replace()` on a template rather than an f-string — the document
carries a literal JSON example, and doubling those braces would tax every future edit of what is
mostly prose.

## The bug the tests could not see

`SHORTCODES` had no Files group at all — no `files` / `upload` / `getfile` — though
`onboarding.py` had taught all three shorthands, in both modes, for three releases.

Adding them surfaced the interesting failure. `cmd()` interpolates a shortcode's `desc` as **raw
HTML** (it cannot `esc()` it, because the role-tags row builds `<b>` into its own desc), so a
literal `<task-id>` was **eaten by the parser** and the panel rendered `/files/`.

```
MCP_TOOLS   esc()s BOTH fields      → literal <task-id> is correct
SHORTCODES  esc()s code, NOT desc   → must be written &lt;task-id&gt;
```

Opposite conventions, one file. **pytest was green through all of it** — the tests assert the
table's contents, not what a browser makes of them. Only the screenshot caught it. Documented on
the field itself so the next person does not rediscover it.

## The rebase worth reading twice — PR #65

Open since Aug 4, green CI **from Aug 4**, against a main two releases behind. That green told us
nothing. Three conflicts, and two of them were traps:

* **`onboarding.py`** — their branch carried the **old hardcoded type list**. Taking their side,
  the obvious resolution, would have silently reverted v2.5.1 two hours after it shipped. Kept
  their ISSUES block **plus** the generated line.
* **`ui.html` SHORTCODES** — theirs superseded ours on status (`fixed #N`, `stuck-debug` folded
  into a both-modes `stuck`); ours was additive (Files). Took **both**, preserving render order.
* **`ui.html` comment** — both added a distinct still-true sentence. Merged rather than picked.

`1476 passing` locally — 1434 + their 42, green **together**, which is the thing a stale CI run
cannot tell you. Then driven in a browser on a real debug task: **Messaging · Issues · Status ·
Files · Meta**, both features coexisting, no duplicate `stuck`.

## The guard that caught its own release

#65 shipped `.github/scripts/check_lockfile.py`. It **failed the 2.6.0 release PR immediately** —
release-please bumps `pyproject.toml` and not `uv.lock`. That is the check working, on the first
try, against the exact drift it was written for (three prior occurrences: 2.2.0/2.3.0 per v2.4.0's
note, and again at 2.5.1, fixed this session in #71).

**Known consequence, recorded in `releases/v2.6.0.md`:** every future release PR fails this check
until release-please is taught to bump the lockfile. For now it is a manual commit on the
release branch.

## The release trap, twice more

Both release PRs arrived **`BLOCKED` with zero checks** — GitHub will not trigger workflows for
its own bot. `gh pr close && gh pr reopen` both times. That is now **six** occurrences (v2.0.0,
v2.0.1, v2.2.0, v2.3.0, 2.5.1, 2.6.0). The `RELEASE_PLEASE_TOKEN` secret remains the real fix and
remains unset.

Also: main is **branch-protected now**. Release notes used to go straight to main (`e91a517`,
`6789843` carry no PR number); that path is closed. Step 4 of the playbook costs a PR.

## The frontend that could not rejoin

A closed Claude session, and the MCP had been registered in the **wrong folder** — `claude mcp add
--scope local` binds the entry to the directory it is run from, and telling the agent to `chdir`
mid-session does not move it.

Forensics, all read-only: every bearer token in `~/.claude.json` (7, all matching real seats) and
every `.mcp.json` under `$HOME`. **The frontend's token was on this machine nowhere.** The seat row
was alive — `ready=1`, `passed`, `revoked_at` NULL — but `agents.token_hash` is sha256, one-way by
design (`identity.py`: *"Never store a raw token or invite code"*). Across all 23 tables the only
credential columns are `agents.token_hash`, `viewers.token_hash`, `invites.code_hash`.

So the seat being alive did **not** help — you need the token to prove you are that seat.
`redeem_invite` refuses a seat that is already paired, so: `revoke-agent` → `invite` → `join` **from
the right folder**. The name freed up (collision checks only live agents), so `T-frontend` was
reused. Cost: a new agent row starts `ready=0 / pending`, so pre-flight is redone. Everything
task-scoped survived.

**The `join` CLI prints a hint that does not work** — a positional `<name>` where the parser wants
`--name`. Small, real, unfixed.

## The broker was four releases behind, not two

Assumed 2.5.0. It was **2.2.0** — `sys-buddy serve --host 127.0.0.1 --port 8787`, started by hand,
installed via `uv tool`. Killed on the owner's explicit instruction (the standing rule is never to
kill a broker you did not start), upgraded **2.2.0 → 2.6.0**, and deliberately **not restarted from
the session** — starting it here would parent the production broker to a shell that dies.

Every agent on that broker still holds a briefing written by 2.2.0: no byte routes, no issue
vocabulary. Re-brief after restart; `rules()` now returns the corrected charter.

## Parked, not built

* **`ideas/lessons.md`** — the broker houses lessons per feature (the PCC folder story), and debug
  mode briefs the agent with them. Take: **start without RAG.** The design already retrieves — a
  human picks PCC. FTS5 is confirmed available here (SQLite 3.53.4), no dependency, when volume
  bites. The blocking decision is **scoping**: lessons cross task boundaries, and nothing else in
  sys-buddy does — `/files` 404s another task's ids rather than 403ing so an agent cannot even
  probe. Likely shape: host-owned, agent-visible, never agent-writable, like `staging_url`. Check
  first whether the `guidelines` table is already this mechanism under another name.
* **`ideas/jira-and-issue-trackers.md`** — `notify.py` is already a channel registry ("write a
  module with exactly two functions") and already keeps channel credentials process-local and
  never persisted, so a Jira token changes no promise. But **commenting is a notification and
  transitioning is not**: transitions need ticket identity, idempotency, and a failure contract the
  registry deliberately refuses to give (*best-effort, never raises*). Two features. Jira gotchas
  recorded: status moves only via a discovered **transition**, and v3 comment bodies are **ADF**.

## Still open

1. `RELEASE_PLEASE_TOKEN` — six occurrences of the close/reopen dance.
2. release-please does not bump `uv.lock`; the new guard will fail every release PR until it does.
3. `sys-buddy join`'s printed hint uses a positional name the parser rejects.
4. `releases.html` has a **pre-existing** unclosed `<li>` (69 opens / 68 closes before this
   session's edits; the four added are balanced).
5. The live broker needs restarting, and its three agents re-briefing.
