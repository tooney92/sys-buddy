# Session handoff — one kind of contract, any cast, contract kinds, host-owned targets (2026-07-31)

Branch `feat/todos-are-the-story`. **Committed** — working tree clean, three commits on top
of `1b1392c wip`:

```
6083737 fix: four dashboard copy and affordance fixes from a UI review
7a99127 docs: session handoffs for the todos and v2 work
6b9be1f feat!: one kind of contract, any cast, contract kinds, host-owned targets
```

`uv run pytest -q` → **986 passed** (session started at 669). Not pushed. No Claude
attribution in the commit messages, per the owner's standing preference that the public
record read as their own work.

**Why one feature commit and not seven.** The seven features were built ON TOP OF EACH OTHER
in the same files — `state.py`, `db.py` and `ui.html` were each touched by nearly all of
them. Splitting post-hoc would have needed `git add -p` surgery and produced commits that
did not individually pass tests, which defeats the only real benefit of splitting
(bisectability). Session notes went separately because they are genuinely independent.

A broker is **still running on :9292** against `~/.sys-buddy-dev-pwr3/sys_buddy.db`, seeded
with a three-seat session — see "Where it stopped".

The owner's live db at `~/.sys-buddy/sys_buddy.db` was **never opened for write** by any
agent. One deliberate manual write is documented under "The live unblock".

---

## Why — the owner's rulings

> "we originally designed contracts at the task level but realised a task can have multiple
> todos. Now I don't want to confuse users with contract and then todo contract."

> "a contract is a list of things with possible attributes which we agree on, and it doesn't
> matter if it is an API, UI, frontend components etc." … "we said we will build like this
> and this is how we have built."

> "the host selects from a list of roles, picks his own, there is a `+` to add other roles…
> so a session can be any combination." Three backends and one frontend must work; when two
> frontends are both parties to a todo, **both must sign**.

**A correction that shaped everything:** an earlier session read "no need to talk about the
original contract to users" as *the word* "contract" and replaced it with the euphemism
"the shape". The owner meant the **task-level** contract. The word is good and stays; what
was removed is the second KIND. All "shape" copy was reverted.

---

## 1 · The task-level contract is gone

Every contract belongs to a todo. `contracts.todo_id` is **NOT NULL**, enforced by SQLite,
and the four write ops (`propose` / `lock` / `decline` / `reopen`) require `todo=N`.

- **Migration adopts, never deletes.** The owner's db held **6 task-level contracts across
  4 tasks**, three with signatures. Each task's chain is adopted by a new `#1` todo, whose
  `parties_json` is the task's full role list (exactly who a task-level contract bound) and
  whose `state` mirrors the task's own march. Signatures need no work — `contract_signatures`
  keys on `contract_id`.
- No task had **both** todos and a task-level contract, so the adoption is unambiguous. The
  code still uses `MAX(number)+1` rather than a hardcoded 1, so a task that somehow held both
  gets a NEW deliverable rather than silently merging history.
- `status_of` tests for contract rows BEFORE acceptances, so an adopted todo reads
  `contracted`/`verified`, never a misleading `pending`. No synthetic `todo_decisions`.
- The two partial unique indexes collapsed to one plain `UNIQUE(todo_id, version)` — which
  also retires the NULL-distinct trap that made partial indexes necessary.

`report_status` and `get_contract` keep an OPTIONAL selector, deliberately: bare `stuck`
escalates the whole collaboration, and bare `get_contract` is how an agent recovers a `#N`
it lost.

**Latent bug found on the way:** `propose_contract`'s version-allocation retry caught *every*
`IntegrityError`, so a NOT NULL violation burned all six attempts and surfaced as "could not
allocate a contract version — please retry", advice that could never work.

## 2 · Any cast — seats, not roles

`role` was doing two jobs. Split:

| | handle — WHO | role type — WHAT KIND |
|---|---|---|
| Tony | `backend` | backend |
| Sarah | `frontend-1` | frontend |
| Priya | `frontend-2` | frontend |

`agents.handle` (unique per task, stable, what signatures/party lists/provenance key on)
plus `tasks.seat_roles_json` (`{handle: role_type}`). `idx_agents_live_role` →
`idx_agents_live_handle`. Two ALTERs, no table rebuild.

**Why it was small:** every quorum path already iterated the strings in `tasks.roles_json`.
Keep that field's SHAPE and change only what the strings MEAN — role type → handle — and
"both frontends must sign" falls out with **no change to quorum logic**. Existing tasks are
already correct under the new reading, because with one seat per role the two strings are
identical.

**Handle numbering (the second pass).** Declaring 2+ seats of a type numbers them ALL —
`frontend-1`, `frontend-2` — so no seat ever holds the bare type string. A type declared
once keeps the bare handle, so every pre-existing task is byte-identical. On `add_seat`,
where the first seat's handle is already fixed, `seats.seat_aliases` DERIVES `<type>-1` as
an address for the shadowed seat; the stored handle never moves. This removed the
`mint_invite` exception entirely, so **"the role type always wins" is now total**.

`resolve_role` order is **role type → handle → alias → tag → name**, returning a SET.
`@frontend` fans out for MESSAGES and is REFUSED in a party list — binding "both frontends"
and binding "Sarah only" are different agreements, so the broker names the candidates rather
than picking.

## 3 · Names are unique per task

Enforced at join, case-insensitive, whitespace-collapsed. **(task, name, role) was tried and
rejected**: it permits Sarah/frontend and Sarah/backend, so `@sarah` becomes ambiguous — a
permanent hazard on the most common action, traded for a one-time annoyance. The owner
caught this.

- **Application-level check inside the same immediate transaction**, not a DB index: a legacy
  db may already hold duplicates, and an index would either fail the boot or force a
  retroactive rename. Legacy duplicates are left standing and refused as ambiguous at
  resolution time.
- Names stay **display-only and never a key** — a rename must never move a signature.
- Bonus from the transaction: a refused name no longer burns the single-use invite.
- `/join` and `sys-buddy join` show the cast and the joiner's seat BEFORE they type (only
  after the code validates — otherwise a random code enumerates the team). `POST
  /pair/preview`, 300 ms debounce, own rate-limit bucket. The live check is advisory; the
  insert-time check is the guarantee.

## 4 · Contract kinds — a contract is ≥1 named unit

`contracts.validate_spec` hard-required `endpoints` + `staging_url`, so an HTTP API was the
only shape a contract could take. A designer↔frontend pair could not produce a valid one.
**Not a permissions problem** — nothing is hardcoded to backend (`state.py` says so) — a
SCHEMA problem.

| kind | unit key | enforced |
|---|---|---|
| `http` (default) | `endpoints` | ≥1, method + path |
| `schema` | `types` | ≥1, name + fields with shapes |
| `ui` | `screens` | ≥1, name + states |
| `none` | `criteria` | ≥1 checkable statement |

- **The unit key NAMES the kind**, so `interface_type` is INFERRED, not declared — requiring
  both is two chances to be wrong instead of none. It stays accepted only to catch
  contradictions (declared `http` + supplied `screens` → refused, naming both).
- **Natural keys kept per kind** (`endpoints`, not a generic `units`), so every existing
  contract stays valid byte-for-byte and no `spec_json` migrates. Verified: **all of the
  owner's real contracts still validate and infer `http`**, plus a differential run of the
  old vs new validator over 7,920 generated legacy specs with zero behavioural difference.
- **`none` still requires criteria.** The invariant is "this is how we have built", so the
  second half must be CHECKABLE. The kind that sounds like "no structure" is really "the
  structure is a checklist".
- Errors name the kind back at the agent and end with *"Do not invent an endpoint to satisfy
  this check"* — the exact failure `v2.md` predicted.

## 5 · `staging_url` is host-owned configuration

It used to live inside the signed spec. Two problems, both real:

1. **It churned for reasons nobody negotiated.** ngrok free URLs rotate on every restart, so
   a locked contract went stale routinely — forcing a renegotiation per restart and producing
   versions identical but for one string. Re-signing on non-events trains people to sign
   without reading, which devalues every other signature.
2. **It was an agent-controlled field on the most security-sensitive value in the system.**
   Host-owned means an injected "test against evil.com" has nothing to write to, rather than
   something the reviewer must notice. **Stronger, not weaker.**

- `todos.staging_url` (per-todo host override) → `tasks.staging_url` → legacy spec. Resolved
  LIVE, so a tunnel restart touches no contract.
- `contracts.staging_url_at_lock` records what was live at signing, so "what did we agree to?"
  still has an answer. `get_contract` returns both, still withheld until every party signs.
- Host action only (`sys-buddy task staging-url`, GUI), event-logged. **No agent tool** —
  asserted by a test.
- **An agent-supplied `staging_url` is REFUSED, not ignored**, and refused on PRESENCE not
  emptiness. Silently dropping it would let a prompt injection appear to succeed, after which
  the agent believes the contract points there and repeats it in chat.
- Migration seeded 4 tasks' targets from their newest locked contract, left 11 host-chosen
  values alone, rewrote no `spec_json`, and re-boots byte-identically.

**⚠ The ruling that matters here:** the resolved target is **kind-agnostic**. An agent
proposed gating it on `has_http_surface` because `contract-kinds-design.md` said a kind with
no HTTP surface "has nothing to grant". That sentence describes the PRE-D13 design and is now
stale — the doc was fixed, not the code. `has_http_surface` conflates *does the contract
describe HTTP?* with *does the consumer need somewhere to look?* A `ui` contract is verified
by **opening the deployed app**, so gating would have removed the URL from the kind that most
needs it. The field now carries a comment saying outright: do not repurpose this.

## 6 · Pre-flight gates the INTERACTION, not the task

Found live on the owner's own session. The gate was task-wide:

```sql
SELECT ... FROM agents WHERE task_id = ? AND revoked_at IS NULL AND ready = 0
```

A `mobile` seat that never passed pre-flight froze contracts on two todos that bound only
backend+frontend. Now scoped to the todo's parties, via one shared helper
`seats.unready(conn, task_id, handles)` so no caller can reintroduce a task-wide query.
`propose_todo` refuses to BIND an unready seat (you are about to depend on them).
**Messaging is deliberately NOT refused** — that is how you tell someone to go do their
pre-flight, and the refusals point at `send_message` as the fix.

## 7 · Smaller things

- **`--port` on `host-viewer` and `invite`.** `_cfg_from_args` never read `args.port`, so the
  two commands that PRINT URLS always emitted `:8787` — a link that 404s on a dev broker or,
  worse, resolves to a *different* broker that has never seen the token, which reads as an
  auth bug.
- **Two agreements, promoted.** The panel already said "agrees twice" — as an info note BELOW
  the story and the steps table. A real user still concluded that accepting a todo meant
  agreeing HOW. Placement fix: the section now opens with it, in the owner's words, and
  `docs/todo-flow.md` gained the clause that makes it stick — *a party who accepted the todo
  may still decline its contract*.
- **Stalled todos are legible.** `awaiting @designer — never joined` vs `awaiting Priya`.
  Different problems, different fixes (chase the invite vs nudge a colleague). **No timeout**
  — any number is arbitrary and a silently expiring todo is worse than a visibly waiting one.

---

## The live unblock (the one manual write)

The owner's active session was blocked by the pre-flight bug above. With explicit
instruction, `ready = 1` was set on agent 53 (`najiu`, mobile) by raw SQL —
`readiness_status` left `pending` so the dashboard stays truthful. Backup:
`~/.sys-buddy/sys_buddy.db.bak-20260730-180533`. Raw SQL on purpose: running any current-code
CLI against that db would have triggered the schema migration mid-session with a live broker
holding it open.

Once the scoped-pre-flight fix ships that row can honestly return to 0. The owner has since
said najiu is not needed.

**A lesson worth keeping:** their agent then reported *"the blocker resolved itself… it
cleared between my two attempts."* It did not — the timestamps show every successful proposal
landed after the manual edit. An out-of-band db write leaves agents narrating a world that
did not happen, and that false belief propagates into session summaries.

---

## 8 · The UI review (owner-driven, on the seeded three-seat task)

Four fixes, all in `6083737`. Each came from the owner looking at a real screen — worth
noting because none of them would have been found by a test:

- **The Drop panel repeated the NEXT card.** Almost verbatim: *"@qa has never joined —
  nobody accepted that invite, so no agent is there to act."* appeared twice on one screen.
  The NEXT card says what is blocking and what to do; the Drop panel now says only what
  dropping DOES. A two-line situation was reading as a wall of text.
- **The `…` on a party pill was not truncation** — it was the *awaiting* status glyph,
  sitting immediately after a name that IS ellipsis-truncated. `[qa] · not joined …` was
  unreadable: nothing distinguished "awaiting acceptance" from "text got cut". Now a hollow
  ring `○`, which cannot be mistaken for truncation and pairs with the ✓/✕ beside it.
- **The task switcher was a dropdown with one entry** — the task you were already on. Now
  plain text below two tasks; the menu returns the moment a second exists.
- **The tasks blurb was hand-typed twice**, said "One task" on a page that could show
  thirty, and described a task as having "the contract" — pre-todo framing for the model
  removed today. One `TASKS_BLURB` constant now. (Two hand-typed copies of one sentence is
  exactly how the shortcode list drifted.)

Also dropped the "Briefly, allies." closer from the debug section, at the owner's request.

## Where it stopped

`pwr` for a **three-seat UI review** — four fixes landed (above), the remaining screens not
yet walked. The seeded task is still live for whoever picks it up.

- Broker **UP on :9292** against `~/.sys-buddy-dev-pwr3/sys_buddy.db`.
- Seed script: `scratchpad/seed_three.py` — drives the REAL ops, not a fixture dump.
  Task `payments-dashboard`, cast `backend` (Tony) + `frontend-1` (Sarah) + `frontend-2`
  (Priya) + an **unjoined `@qa`**; four todos covering an `http` contract verified end to
  end, a `ui` contract locked by both frontends and mid-build, a `schema` contract awaiting
  one signature, and a todo blocked on the seat nobody accepted; plus a short thread.
- Viewer token printed by the seed script; open `http://127.0.0.1:9292/ui?v=<token>`.

To resume: mint a fresh token with `SYS_BUDDY_DB=~/.sys-buddy-dev-pwr3/sys_buddy.db uv run
sys-buddy host-viewer --port 9292` if the old one is lost, then drive the dashboard.

---

## Open for the owner

1. **Not pushed, and no PR.** Three commits sit on the branch. Worth noting for the next
   session: now that the work is committed, future parallel agents CAN use real worktree
   isolation — the uncommitted tree is the only reason they had to be serialised, and two
   agents editing `ui.html` at once was a live hazard all session.
2. **Version.** `pyproject.toml` is still `1.4.0` — deliberately not bumped, since the
   number is the owner's call. By the project's own CHANGELOG rule
   (MAJOR = "incompatible changes to the tool/wire contract or agent-visible behavior") this
   is **2.0.0**, not 1.5.0: `todo=N` is now required on four tools, and a spec carrying
   `staging_url` is refused. `releases/v1.5.0.md` is a committed 07-28 draft that says "No
   schema change and no migration" — now false; it wants rewriting, not extending.
3. **The owner's live db is still UNMIGRATED** and will migrate on next boot of this code.
   Verified safe on copies repeatedly. One consequence on `employee-form-feature-e287`, which
   is mid-negotiation: contracts currently quoted as "v3 on todo #4" and "v2 on todo #5"
   become "v2 on #1" and "v1 on #2". Messages are immutable, so the thread will reference
   numbers that no longer resolve. **Get those two signatures in before upgrading.** The
   owner has said the task will not be revisited afterwards.
4. **`…ngrok-free.dev:3000` is unreachable** (ngrok terminates on 443; the port belonged on
   localhost) and is frozen into 4 contracts on that task. `curl` confirmed: `:3000` fails to
   connect, the bare host answers. After the staging_url work, one
   `sys-buddy task staging-url` fixes all four with no renegotiation.
5. **Unjoined seat + shared role type** — a seat whose handle equals a role type held by 2+
   seats, that nobody has joined, still cannot be named in a party list on a GROWN task. It
   has no display name yet. Every other seat works. Narrow; flagged, not solved.
6. Still open in `docs/contract-kinds-design.md`: a first-class design reference for `ui`,
   whether `ui` should collapse into `schema`, and whether a contract may change kind between
   versions.

---

## Traps worth remembering

- **A display name must never become an identifier.** Names duplicate, change, and are chosen
  by the person. Show the friendly one, key on the stable one.
- **Uniqueness scoped to (name, role) does not make `@sarah` safe** — people address each
  other by name, not by name-within-role.
- **SQLite has no `SELECT FOR UPDATE`, and none is needed.** A unique constraint plus
  `BEGIN IMMEDIATE` serialises writers. Never build row locking here.
- **SQLite treats NULLs as DISTINCT in a unique index** — a half-backfilled column sails past
  the constraint. Only an explicit NULL assertion catches it. This has now bitten three times.
- **A test can pass for the wrong reason.** The contract-kinds surfaces test passed three
  times while two kinds were untaught: a fixed-size window matched a neighbouring example, an
  unbounded last block matched stray prose, and a line-bound regex missed a wrapped example.
  Each was found by mutating the source and confirming red. The failure modes are written into
  that test's docstring.
- **Refuse, do not guess.** `@FE` in a party list, two unit keys in one spec, an ambiguous
  `@sarah` — every one names the candidates back rather than picking. It is the same posture
  as "the broker enforces; agents request".
- **The broker checks shape, never truth.** A todo scope only has to be non-empty. An invented
  endpoint dies the first time the peer calls it; an invented screen state validates, locks,
  and verifies against itself. The only guard is the peer actually reading — which is why
  `_assert_text` says "The other parties accept the SCOPE, not the title."
- **`ui.html`'s `<script>` is IIFE-wrapped**, so nothing hoists into a `new Function(js)`
  scope. Strip the wrapper to test its internals under node.
