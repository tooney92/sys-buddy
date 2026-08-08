# Onboarding trust, a demo scaffold, and two corrections of work shipped hours earlier

**Shipped:** `v2.7.0` and `v2.7.1` (PyPI + ghcr + release notes + website **live**) · **1519 tests** on main
**Built, not shipped:** `~/dev/sys-buddy-demo` (frontend + backend scaffold) · engagement demo parked on `:9293`
**New skill:** `.claude/skills/ship-release/SKILL.md` (tracked — `.gitignore` changed to allow it)
**Parked design:** `ideas/lessons.md`, `ideas/jira-and-issue-trackers.md` (gitignored, local only)

Continues `2026-08-06-two-releases-a-rebase-and-a-broker-four-versions-behind.md`.

---

## v2.7.0 — three things a human hits before the product works

**Messages render with shape.** The thread escaped every body into one blob, so a
structured update arrived as a wall. `scopeHTML` had already solved this for todo scopes
with a **markup-free** parser, so the parse is now shared (`proseBlocks`) and bubbles
render via `bodyHTML`. Still no markdown — and the reasoning is *stronger* for messages
than scopes: a scope comes from your own agent, a message is peer content from another
company's, and a renderer would return links and image loads inside a page holding a
viewer token. **A one-line message is byte-identical to before**, verified in the DOM at
zero child elements.

**And the broker says when one won't read.** `send_message`'s receipt gains a note above
400 chars with no line break. Deliberately **not a refusal** — an agent that hits a
formatting gate optimises for the gate, truncating to get past it. Live: a well-shaped
**495-char** message gets a clean receipt while a **487-char** wall is nudged.

**Your own Claude command.** `claude-personal`/`claude-work` users were hand-editing every
generated command. Now on both the join page and the Host screen, with a **line-anchored**
substitution — a blind replace of `claude` would also rewrite an occurrence inside the
bearer token and yield a command that looks right and authenticates as nobody. Validated
to the same character set on both sides, since the string is pasted into a shell.

**"What we're asking".** A panel above the prompt: gains / sends / never / seen by. The
`never` list is **parsed from `RULES_OF_ENGAGEMENT`** — every rule, as its own opening
sentence. A drifted security summary is a false assurance, not a stale line. The page was
already receiving the full charter and **discarding** it; it now expands inline.

## v2.7.1 — two of these are corrections of work from the same night

### The font bug nobody could have tested

The stylesheet requested Geist Mono at **400 and 500**. The dashboard uses it at **600 in
fourteen places and 700 in six**. Missing weights get **synthesised** — the browser smears
the 500 — which is the entire "not sharp" complaint.

Worth recording the shape: nothing was broken, no test could have caught it, and it
degraded the surface a customer looks at for the life of the product. **It was found by
looking at a screenshot.**

### "Two agents. One contract." was stale in BOTH halves

The owner caught the second half, which I had missed: casts have held >2 seats since
v2.5.0, **and contracts have been per-todo since v2.0.0** — five todos, five contracts.
Now *"Their agents and yours. Agreed before built. Shipped."*

Same assumption in three more places, one of them a real defect: the board said a debug
issue needs **"both parties"** when **v2.6.0 requires every party** — a description of a
rule the broker does not enforce.

Kept singular where still true: you invite one person at a time.

### The file rule — I over-corrected v2.5.1 and it cost the owner five prompts

v2.5.1 (shipped earlier the same night) fixed briefings that taught base64 as the only
path. The fix said *"use the tool ONLY if you cannot run a shell."*

**That overshot, and the overshoot cost more than the original bug.** An agent asked to
read a **37 KB** screenshot refused `get_file`, went hunting through config for a bearer
token to curl with, then looped over the tokens it found — **five permission prompts** to
read something one tool call returns.

The warning was sized for 328 KB (~128k tokens); the agent applied it to 37 KB (~15k).
**"Expensive" without a threshold is applied to everything.** The rule is a number now, in
the charter, all three briefings, both tool docstrings on both surfaces, and the
cheatsheet. `list_files` already reports size, so the choice is **checkable** rather than
interpretive.

The test that pinned the old prohibition now asserts it is **gone**, with both failure
directions in its docstring — under-corrected before v2.5.1, over-corrected in it.

## Demo prep (demo is today)

**`~/dev/sys-buddy-demo`** — backend (FastAPI, `:8000`) + frontend (static, `:5500`).
Sign-in deliberately absent from both: that is what the agents contract about. **CORS
pre-wired** (both `localhost` and `127.0.0.1` — different origins to a browser), deps
pre-installed so venue wifi cannot break it, health check that says plainly if the backend
is down, form/input/button pre-styled. Dockerfile present and **not** what to run —
`--reload` puts an agent's edit live in under a second, which was verified by adding an
endpoint and watching it appear.

**Engagement demo** on `:9293`, detached, parked in a browser: *Send Tony to Dubai for
Christmas*, owner **Amal** + two builders, five deliverables in mixed states. Finding worth
keeping: **the 8-node `confirmed` stepper cannot render on a populated engagement** —
`taskHTML` truncates to three nodes once todos exist — so a second task (`Q1 offsite in
Lisbon`) sits at the pre-todo moment to show it.

## Two mistakes of mine, recorded because they cost real time

**I ran `git reset --hard origin/main` on a branch holding ~8 files of uncommitted work
and destroyed it.** Rebuilt every edit from context and re-verified, but that is why the
font/wording/file-rule work landed on its own `fix/` branch rather than riding with
something earlier. **Commit before any reset, always.**

**I told the owner there was no recent screenshot when there was one from four minutes
earlier.** `find -newermt` is GNU syntax that **BSD find rejects**, and I had sent stderr
to `/dev/null`. A silent-failing check reported as a fact. Use `stat -f '%Sm'` + `sort`.

## Notable, carried forward

* **The relock push fires the release PR's checks by itself** — no close/reopen needed.
  release-please never bumps `uv.lock`, so that commit is required anyway, and it comes
  from a real account. **If release-please is ever fixed, the close/reopen trap returns.**
* `gh run list --limit 1` after a release merge returns the **previous** run, whose
  publish jobs correctly read `skipped`. Look it up by title.
* `uv tool upgrade --no-cache` still says "Nothing to upgrade" while the **simple** index
  lags the JSON API; `uv tool install --force --no-cache` is the follow-up.
* `.claude/` was ignored wholesale. Now `.claude/*` + `!.claude/skills/` — note the `/*`,
  since **git will not descend into an ignored directory** and the obvious spelling
  silently does nothing.

## Still open

1. **The broker on `:8787` is running 2.6.0** — two releases behind. Restart before the demo.
2. `RELEASE_PLEASE_TOKEN` still unset (see the note above about it re-arming the trap).
3. release-please still does not bump `uv.lock`.
4. `sys-buddy join`'s printed hint uses a positional name the parser rejects (`--name`).
5. `releases.html` has a **pre-existing** unclosed `<li>`; additions since are balanced.
6. `ideas/lessons.md` is blocked on one decision: **lessons cross task boundaries and
   nothing else in sys-buddy does.** Likely shape is host-owned / agent-visible /
   never-agent-writable, like `staging_url`. Check first whether the `guidelines` table is
   already that mechanism under another name.
