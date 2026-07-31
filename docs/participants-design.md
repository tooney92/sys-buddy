# Any cast: N participants per role type

**Status:** design, not yet implemented.
**Owner ruling:** "we support roles of mobile, backend, frontend, QA, project manager,
devops… the host selects from a list of roles, picks his own, there is a `+` to add other
roles and he selects the role type, so a session can be any combination." Three backends
and one frontend must work. The host may hold any role. When two frontends are both
parties to a todo, **both must sign** its contract.

## The problem

`role` was doing two jobs at once — *who you are* and *what kind of work you do* — and the
schema enforced the first:

```sql
-- Fixed cast: at most one *live* agent per role.
CREATE UNIQUE INDEX idx_agents_live_role
    ON agents(task_id, role) WHERE revoked_at IS NULL;
```

So a session with two frontend developers could not be described. One of them had to pair
as `mobile` — a lie that then propagates into every message, signature and event for the
life of the task.

Note what was NOT the problem: the role *vocabulary* was never closed. `admin.create_task`
takes arbitrary strings and `service.resolve_role` matches against whatever the task
declared, so `qa` / `devops` / `project manager` already worked. `ROLE_TAGS` (`BE/FE/MB/DE`)
is a typing shortcut, not a whitelist.

## The split

| | handle — WHO | role type — WHAT KIND |
|---|---|---|
| you | `backend` | backend |
| dev 2 | `frontend-1` | frontend |
| dev 3 | `frontend-2` | frontend |

- **handle** is the seat. Unique per task, stable forever, quoted in messages and
  signatures. This is what `parties_json`, quorum and provenance key on.
- **role type** is the kind of work. Many seats may share one. Drives the agent briefing,
  the pre-flight question set, the `@FE` tags and the dashboard colour.

## Why this is a SMALL change

Every quorum path already works on the strings in `tasks.roles_json`:

```python
signed_set = set(signed)
remaining = [r for r in required if r not in signed_set]
```

So if `roles_json` becomes the list of **handles**, and handles are unique per person,
then "both frontends must sign" falls out with **no change to the quorum logic at all**.
`parties_json` would read `["frontend-1", "frontend-2"]` and each must sign for itself.

That is the whole trick: keep `roles_json`'s SHAPE (a list of strings, one per seat) and
change only what the strings MEAN — from role type to handle. Existing tasks are already
correct under the new reading, because with one seat per role the handle and the role type
are the same string.

## Schema

```sql
ALTER TABLE agents ADD COLUMN handle TEXT;          -- backfill: handle = role
ALTER TABLE tasks  ADD COLUMN seat_roles_json TEXT; -- backfill: {r: r for r in roles_json}

DROP INDEX idx_agents_live_role;
CREATE UNIQUE INDEX idx_agents_live_handle
    ON agents(task_id, handle) WHERE revoked_at IS NULL;
```

- `tasks.roles_json` — unchanged shape, now the list of HANDLES (the declared cast).
- `tasks.seat_roles_json` — `{handle: role_type}`. Backfills to an identity map, so every
  existing task keeps behaving identically.
- `agents.handle` — backfills from `role`. Nullable at the column level only because
  ALTER TABLE cannot add NOT NULL without a default; fullness asserted explicitly in the
  migration, because SQLite treats NULLs as DISTINCT in a unique index and a half-backfilled
  table would sail past the constraint (this trap has bitten this codebase twice).

Nothing is deleted and no table needs a rebuild.

## Handles

- A role type with ONE seat: the handle IS the type — `frontend`. This is every task
  written before the split, and they are untouched.
- A role type with TWO OR MORE seats: **every one of them is numbered** — `frontend-1`,
  `frontend-2`. No seat is ever named after a type that several seats share, so
  `@frontend` can only ever mean the type. (Numbering only the second seat left
  `@frontend` meaning both "the type" and "the first seat", and an UNJOINED seat has no
  display name to be disambiguated by — so it could not be named in a party list at all.)
- Never reused, never renumbered — the same rule as todo `#N`, and for the same reason:
  a handle is quoted in message history, so re-pointing it silently rewrites the past.
- The host may name a seat at creation time, but not after a role type that another seat
  holds, or that several seats share — that puts the collision straight back.
- The one seat that CAN end up holding a bare shared type is one that was alone when it
  was minted and gained a sibling later, via `add_seat`. Its handle is immutable, so it
  gains a derived ADDRESS instead: `@frontend-1`, which exists exactly while 2+ seats of
  that type do and resolves to the stored handle. Nothing is written; nothing moves.

## Addressing

`to_role` on a message may hold a **handle** or a **role type**. Delivery matches either:

```
deliver if  msg.to_role == reader.handle  OR  msg.to_role == reader.role_type
```

- `@FE` → every frontend-type seat (fan-out).
- `@frontend-2` → exactly one.

This needs no storage change: `to_role` is already a plain string filtered at read time,
which is also why two frontends already receive broadcasts correctly today.

`service.resolve_role` must stop returning a single role and start returning a set, and
must accept handles as well as tags.

## Host

`create_task` grows a cast declaration instead of a bare role list — each entry a
(handle, role type) pair, with the host naming its own seat like any other. The GUI's host
screen becomes: pick your own role, `+` to add a seat, choose its role type, broker
suggests the handle.

## The roster

The cast must be DISCOVERABLE, not inferred. Today an agent learns a peer's name only when
that peer sends a message (`service.py:342` puts `from`/`role` on every inbound message),
and `get_todos` returns `parties` as bare strings. So before anyone has spoken, an agent
cannot know a second frontend seat exists — which makes "propose a todo with the second
frontend engineer" impossible to act on.

One roster, two renderers — the dashboard panel and an agent-facing tool read the SAME
source. (The shorthand list was hand-typed in two places in `ui.html` and had already
drifted — `pc [#N]` vs `pc #N`. Do not repeat that.)

Each row is seat · role type · who · readiness · presence:

```
@backend-1    backend    Tony     ✓ ready   listening
@backend-2    backend    Ade      ✓ ready   idle 12m
@frontend-1   frontend   Sarah    ✓ ready   listening
@frontend-2   frontend   Priya    · pending pre-flight
@qa           qa         —        invite not yet accepted
```

- **Unjoined seats are listed.** A roster of only joined agents cannot answer "who never
  accepted their invite?", which is precisely the state that silently stalls a task.
  The same fact rides on every todo (`todos.to_dict`'s `unjoined`, `state.next_step`'s
  `unjoined`, the rollup's `unjoined` count), because "awaiting Priya" and "awaiting a
  seat nobody ever accepted" need different actions — nudge a colleague, or chase an
  invite. There is **no timeout**: any number would be arbitrary, and a todo that
  silently expires is worse than one visibly waiting. `host_drop_todo` is the escape
  hatch.
- The seat renders as a monospace `@handle` chip because it is the token a human types
  into a command — same idiom as the existing shortcode chips.
- `readiness_status` and `listening_until`/`listening_since` are already columns on
  `agents`; the roster only has to render them.

## Resolving who someone means

`service.resolve_role` accepts, in priority order: **role type → handle → shadowed-seat
address → tag → agent name**. It returns a SET, because a role type may name several
seats.

- `@frontend` / `@FE` → every frontend-TYPE seat.
- `@frontend-2` → one seat.
- `@frontend-1` → one seat, on a task where the first seat of a type still holds the bare
  handle (see Handles). Elsewhere it is an ordinary handle or nobody.
- `@sarah` → the seat held by the agent named Sarah, case-insensitive.

**A role type always means the TYPE.** Where a seat is handled `frontend` — the same
string as the type — a handle-first order made `@frontend` mean "both frontends" in a
message and "Sarah's seat" in a party list. One token, two meanings: a
human will not hold that distinction, and the party-list reading can bind the wrong
person to a contract. On a one-seat-per-type task (every task written before the split)
the handle and the type are the same string, so type-first returns exactly what
handle-first returned — the reorder costs those tasks nothing.

Ambiguity is REFUSED, never guessed, and the refusal names the candidates:

- `@FE` / `@frontend` in a PARTY LIST → errors, because binding "both frontends" and
  binding "Sarah only" are different agreements and the broker must not choose.
- `@FE` / `@frontend` in a MESSAGE → fans out to both. Telling every frontend something
  is unambiguous: `to_role` is a plain string filtered at read time.
- A token that is both a role type and the handle of a seat NOT holding it (only
  reachable by a host overriding a derived handle) has two honest readings, so neither is
  taken — refused in both directions.

**Every seat has a token of its own, and there are no exceptions to "the type wins."**
Both were once untrue, and they were the same hole: with the first seat of a shared type
handled `frontend`, `@frontend` was refused as ambiguous and the only alternative was a
display NAME — which does not exist until someone joins. An unjoined seat was therefore
unnameable in a party list, and `admin.mint_invite` had to carry an exception (an exact
declared handle won there) or the first frontend seat could never be invited at all.

That is closed at the source — a declared cast numbers every seat of a shared type — and
patched in the one place it can still arise, a task that GREW its second seat of a type,
where the first seat's immutable handle gains the derived address `@frontend-1`. The
`mint_invite` exception is gone: `admin.mint_invite` now resolves through the ordinary
`service.resolve_seat` like everything else that names one seat.

`@frontend-1` is an ADDRESS, not a rename or a general alias mechanism: it is derived
from the cast (never stored), exists only while 2+ seats hold that type, canonicalises
back to the stored handle, and is minted for no other case. A seat that is the only one
of its type has no alias — `@frontend-1` there names nobody.

Refusals list candidates by the token that actually resolves (`@frontend-1 (Sarah) and
@frontend-2 (Priya)`), because a refusal that answers "`@frontend` is ambiguous" with
"did you mean `@frontend`?" is no answer.

### Names

Chosen by the buddy at join time (`pairing.join(..., name)`), **unique per TASK** —
case-insensitive, whitespace-trimmed. Not per `(task, role)`: that would allow
Sarah/frontend and Sarah/backend, and every `@sarah` after that would be ambiguous — a
permanent hazard on the most common action, traded for a one-time annoyance at join.

Still display-only and never a key. Handles remain the identifier, so a rename can never
move a signature.

Enforced in the application, inside the same `BEGIN IMMEDIATE` transaction as the INSERT
that claims the name (`seats.assert_name_free`, called from `pairing.redeem_invite`), NOT
by a unique index. SQLite has no `SELECT FOR UPDATE` and needs none — the immediate
transaction takes the write lock up front, so no second joiner fits between the check and
the write. An index was rejected because a legacy database may already hold duplicates,
and adding one would mean either failing the boot or renaming somebody retroactively;
letting history stand and constraining only new writes is the smaller lie. The live checks
on the join page and in the CLI are **advisory** and may race.

### The join surface

`POST /pair/preview` — read-only, burns nothing, and gated on the invite VALIDATING
first, because a surface that lists the cast for any code is a team directory for anyone
who guesses. It returns the task title, the SEAT this invite fills and its role type
(invites are per seat, so both are known before the joiner types anything), and the
people already here. Both `/join` in the browser (debounced live check) and `sys-buddy
join` in the terminal (print the cast, prompt, re-prompt) read it.

## Seats are not frozen at setup

Declaring the cast up front must not mean the cast is FIXED for the life of the task. A QA
seat added on day three is ordinary, and forcing a new task instead would split the history
of one piece of work in two.

So `admin.add_seat(task_id, role_type)` appends a seat and mints its invite, exactly as
creation does — creation is simply "add these N seats at once". What is immutable is a seat
that has already been USED: its handle is quoted in message history and signatures, so it is
never renamed or re-pointed. Revoking an agent frees the seat for re-pairing (this already
works — the live-role index is partial on `revoked_at IS NULL` precisely so that revoking
leaves the historical row intact).

A new seat does NOT retroactively join existing todos. `parties_json` names who agreed, and
a person who was not there did not agree.

## UI

### Roster panel (task detail)

Grows out of the existing `PRE-FLIGHT [backend ✓ passed] [frontend ✓ passed]` strip rather
than sitting beside it — two overlapping lists of people is how they drift apart.

```
┌─ Cast ────────────────────────────────────── 4 of 5 joined ─┐
│  SEAT           ROLE        WHO      PRE-FLIGHT   PRESENCE   │
│  ───────────────────────────────────────────────────────────│
│  @backend-1     backend     Tony     ✓ passed     listening  │
│    host                                                       │
│  @backend-2     backend     Ade      ✓ passed     idle 12m   │
│  @frontend-1    frontend    Sarah    ✓ passed     listening  │
│  @frontend-2    frontend    Priya    · pending    —          │
│  @qa            qa          —        —            invite sent│
└──────────────────────────────────────────────────────────────┘
```

`4 of 5 joined` is the headline number: an unjoined seat is the state that silently stalls
a task, and no roster built only from joined agents can show it.

### Task-list chips — COUNT form (decided)

One coloured letter per ROLE TYPE with a multiplier, never one chip per seat:

```
   (B×2)(F×2)(Q)          NOT  (B)(B)(F)(F)(Q)
```

Repeating identical letters reads as a rendering bug and stops scaling around six seats.
Per-person initials were considered and rejected: an unjoined seat has no initial, and the
letters would stop meaning role type. The list view answers "how big is this and is it
moving" — the roster answers "who exactly".

### Host creates the cast

```
│  Your role     ( backend        ▾ )   ← the host is a seat like any other
│
│  Who else is on this?
│    ( backend   ▾ )   seat: @backend-2         [ × ]
│    ( frontend  ▾ )   seat: @frontend-1        [ × ]
│    ( frontend  ▾ )   seat: @frontend-2        [ × ]
│    ( qa        ▾ )   seat: @qa                [ × ]
│    [ + add someone ]
│
│  Roles: backend · frontend · mobile · designer · QA ·
│         project manager · devops      (or type your own)
```

The seat name is DERIVED AND DISPLAYED, not typed: choosing "frontend" twice yields
`@frontend-1` and `@frontend-2` with no thought from the host. Editable, but never required.
The role list is a convenience, not a whitelist — the field stays open.

### What the agent sees

```
> todo Sign-in endpoint @frontend-2
  ✓ Proposed todo #1 "Sign-in endpoint"
    parties: @backend (Tony), @frontend-2 (Priya)

> todo Session refresh @FE
  ✗ @FE names two seats on this task — @frontend-1 (Sarah) and
    @frontend-2 (Priya). A todo binds specific people, so say which.
```

## Known follow-ups, deliberately not decided here

- `todo_decisions` / `todo_drop_consents` have `UNIQUE(todo_id, version, role)` and
  `UNIQUE(todo_id, role)`. Those columns become handles under the new reading, which is
  correct — but the column NAME then lies. Rename or comment.
- `readiness` picks its question set by role; with N seats sharing a type, several agents
  get the same questions. That is fine, but the "nobody got the propose question" guard in
  `readiness.py:45` should be re-read against a multi-seat cast.
- The dashboard's role chips assume one seat per letter (B/F/M). Two frontends need two
  distinguishable chips.
