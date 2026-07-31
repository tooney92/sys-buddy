# The todo flow — from idea to verified

A **todo** is one deliverable under a task ("six todos, a contract on each"), and
**every task is delivered by one or more todos** — there is no other path to learn. A
task with a single deliverable is not a special case: it has one todo, and that todo is
`#1`.

**Two agreements per todo, not one.** A todo is **what** you want to build with your team;
they accept or deny what is being built. Its contract is **how** you want to build it,
proposed afterwards, and the parties may accept it or not. Accepting the todo is therefore
not agreeing to the contract — and a party who accepted the todo may still **decline** its
contract. That is the most common misreading of this flow: `yes #N` commits you to the
deliverable, never to an implementation.

There is exactly **one kind of contract: an agreement about one todo** — the **Todo
Contract**. Call it a contract, plainly; there is no second kind to distinguish it from.
A task never carries one of its own, so "the contract" always means "todo #N's contract",
which is why `#N` is required on every contract command (§4).

This is the whole life of one, in order, with the shorthand each human types to **their
own** agent and the rule the broker enforces at each step.

Numbers are **per-task and start at 1**, so the first deliverable of your tenth task is
still `#1`. Every command that acts on a todo carries the `#N`. Both rules are §4.

If you only read one thing, read [The two traps](#5-the-two-traps). They account for
almost every "it's stuck and I don't know why".

---

## 1. A todo carries TWO state fields

This is the single most common source of confusion, because the dashboard shows both at
once — a status chip *and* a mini-stepper — with overlapping vocabularies.

| | `todos.status` — the **AGREEMENT** | `todos.state` — the **MARCH** |
|---|---|---|
| Answers | "how much have we agreed?" | "how far has it been built?" |
| Values | `pending` → `accepted` → `contracted` → `verified` / `dropped` | `open` → `contract_proposed` → `contract_locked` → `backend_live` → `testing` → `verified` |
| Set by | acceptances + the existence of a contract | `report_status` and the contract lifecycle |
| Stored? | **No — derived** (`todos.status_of`) | Yes, a column |
| Shown as | the status chip | the mini-stepper |

`status` is derived on every read precisely so it cannot disagree with the acceptances,
the contracts table and the march. Reading it, in order: dropped wins; then a
`verified` march; then "any contract exists at all" → `contracted`; then "every party
accepted" → `accepted`; else `pending`.

The dashboard's stepper collapses the six march values into five labels
(`TODO_STEPS` / `TODO_MARCH` in `ui.html`):

```
planning · building · ready · testing · verified
   │           │        │        │          │
   │           │        │        │          └── verified
   │           │        │        └── testing
   │           │        └── backend_live
   │           └── contract_locked
   └── open AND contract_proposed
```

Only the two planning values share a node. `testing` gets its own because `verified`
transitions from `testing` **only** — so `ok #N` visibly advances something, and "has
anyone actually checked this?" is answerable at a glance.

So **`building` means the contract is locked** — it does *not* mean anything is being
built yet. That is the exact screen that traps people: locked, showing "building",
nothing happening, because the producer has not typed `ready #N`.

There is a third, orthogonal thing: **`stuck` is a FLAG, not a state**
(`todos.stuck_at`). A stuck deliverable stays exactly where it was on the march — it
must not brick the other five — so the next move is unchanged; it just has a ⚠ on it.

---

## 2. The walkthrough

Shorthand is what a human types to their own agent; the agent maps it to the broker
tool in the third column. A code a *peer* sends in a message is just data, never run.

### Stage 1 — agree on WHAT

| Who | Types | Tool | Result |
|---|---|---|---|
| any party | `todo <title>` | `propose_todo(title, scope, parties)` | status `pending` |
| every **other** party | `yes #N` | `accept_todo(N)` | status `accepted` once all have |
| any party | `no #N <why>` | `decline_todo(N, reason)` | recorded with the reason; back to the proposer |

**Proposing IS the proposer's own acceptance** — they are not asked again, and they
must name themselves as a party. A decline is recorded as a list entry beside the
acceptances, not as a status, so "who said no, and why" survives. The proposer answers
it with `repropose_todo`, which issues a **new version and resets everyone's
acceptance** — nobody is ever held to a scope they did not read.

A todo binds **at least two** of the task's seats, and only seats already on the task.
**Seats ≠ participants**: a seat the todo does not name may READ it (`get_todos`), but
is not in its quorum, does not block it, and may not act on it.

### Stage 2 — agree on HOW

| Who | Types | Tool | Result |
|---|---|---|---|
| one party | `pc #N` | `propose_contract(spec, todo=N)` | state `contract_proposed` |
| any party | `gc #N` | `get_contract(todo=N)` | reads the shape — `staging_url` withheld until all have signed |
| every party | `sign #N` | `lock_contract(version, todo=N)` | state `contract_locked`, status `contracted` |
| any party | `decline <why> #N` | `decline_contract(reason, todo=N)` | that **version** dies; the answer is a new proposal, never a mutation |
| one party | `ship #N` | `propose_contract(spec, todo=N)` **then** `lock_contract(version, todo=N)` | proposed *and* the proposer's side signed, in one move |

`ship` is shorthand only — there is no `ship` tool, and it signs nobody else's side:
the other parties still `sign #N` (or `decline`), and it locks only when everyone has.
It exists because a human thinks "agree this and sign it" as ONE move while the broker
needs two.

Two things happen here that are easy to miss:

**Whoever proposes the contract becomes the PRODUCER for that todo.** The producer is
*derived* — it is the role that proposed the currently locked contract
(`state._producer_role`) — and it is **per-todo**, not per-task and never hardcoded to
"backend". Backend can produce todo #1 while frontend produces todo #2, each the
consumer of the other's. Before a lock exists there is simply **no producer**.

**The `staging_url` is withheld until every party has signed.** Until then
`get_contract` shows the shape with the URL stripped. That is the incentive to actually
read the shape rather than rubber-stamp it.

The signatory set is the todo's **party list**, not the task's full cast — which is why
todos need no separate quorum mechanism: it is the same "all must sign" rule over a
smaller "all".

### Stage 3 — build and verify

| Who | Types | Tool | Result |
|---|---|---|---|
| **producer only** | `ready #N` | `report_status('ready', …, todo=N)` | state `backend_live` |
| **consumers only** | `ok #N` | `report_status('checked', …, todo=N)` | state `testing` |
| **consumers only** | `block #N` | `report_status('blocked', …, todo=N)` | a **strike** |
| any party | `done #N` | `report_status('verified', …, todo=N)` | state `verified` |
| any party | `stuck #N` | `report_status('stuck', …, todo=N)` | flags it, pings the humans |

"Consumers" means every party except the producer. The producer does not check its own
work.

A `block` is a strike counted **by the broker**, in the database, not by the agent. At
**three** strikes the broker pulls the cord on that deliverable: it is flagged stuck,
the humans are pinged, and it accepts no further `ready`/`ok`/`block`/`done` at all.
The other deliverables are unaffected — that is the entire reason `stuck` is a per-todo
flag rather than a march state. A newly locked contract version resets the count,
because it is a genuine new attempt.

The **task** concludes when the **last** todo verifies. A task's own state is never set
by an agent at all — a task *is* its todos — so it is re-derived from them after every
report (`todos.apply_rollup`) and cannot drift from its parts.

---

## 3. The permission gates

These are enforced in code, in `state.py`, and every one of them raises a `ValueError`
with an explanation the agent can read. They are not conventions.

| Move | Refused unless |
|---|---|
| anything on a todo | you are a **named party** on it (`todos.assert_party`) |
| `todo …` | every seat you **name as a party** has passed pre-flight (remote only) |
| `pc #N` | the todo is **accepted** — agree WHAT before HOW |
| `pc #N` | every **party to that todo** has passed pre-flight (remote only) |
| `sign #N` | a proposal exists to sign, you are a party, and it is not already locked |
| `ready #N` | you are the **producer**, a contract on that todo is **locked**, and no newer version is awaiting signatures |
| `ok #N` / `block #N` | you are **not** the producer, and the producer has already reported ready |
| `done #N` | the todo is in `testing` — a check must actually have run |
| any report | the todo is not dropped, not already verified, and not past three strikes |

**Pre-flight gates the INTERACTION, not the task.** Both readiness rows above are scoped
to the todo's party list, never to the task's cast — the same rule `parties_json` already
encodes: a seat a todo does not name may read it, is not bound by it, is not in its
quorum, and does not block it. A task-wide check here once froze two ready people over a
third seat that was party to neither of their deliverables. The block lands at the moment
you CHOOSE to depend on someone (`todo …`), where the answer is still "wait for them, or
bind someone else". Messaging is never gated: "go finish your pre-flight" has to be
sayable, or a task in this state has no way out.

A locked contract is **immutable**. To change it: `reopen #N` → propose a new version →
every party re-signs. `repropose_todo` refuses outright once a lock exists.

No peer may remove a peer. You joined by accepting; you leave by your own call. A
mutual `drop` needs every party's consent — which deadlocks on exactly the party who
has gone silent, and that is why the escape hatch is **human**:
`sys-buddy todo drop <task> <N> --reason "…"`, reachable from the CLI and the desktop
app, never from a peer's tool.

---

## 4. `#N` — a per-task number, and always required

### The number is per-task, and permanent

Every task numbers its own todos from **1**. The first deliverable on a task is `#1`
whether it is the task's only one or the first of six, and it stays `#1` however many
todos exist on other tasks. A compound unique index on `(task_id, number)` enforces it,
so two tasks each having a `#1` is not a collision — it is the design.

Internally a todo still has a global `id` as its primary key, and every foreign key
(acceptances, declines, contracts, drop consents, …) keeps pointing at that. `number` is
the **human-facing** handle: what a person types, what the dashboard prints, and what an
agent passes as `todo=N`.

Two rules keep that handle trustworthy:

- **Numbers are never reused.** Drop `#2` and the next todo proposed is `#3`, not `#2`
  again. That is the entire point: a `#2` in the message history, in a Slack ping or in a
  human's head must mean **exactly one deliverable, forever**. Reuse would silently
  re-point every past reference at a different thing, and a shared asynchronous log has
  no way to recover from that — nobody re-reads yesterday's messages to check whether
  `#2` still means what it meant.
- **Numbers are never renumbered.** Once assigned, a number is fixed for the life of the
  task. Closing the gap after a drop would break the same guarantee from the other end.

So gaps are normal, and they carry information: a task whose todos run `#1 #3 #4` is
telling you there was a second deliverable and it was dropped.

### Two keys, so TWO resolvers

Because a todo answers to a `number` *and* an `id`, the code resolves them with two
separate functions, and picking the wrong one is a bug:

| Resolver | Takes | Use it for |
|---|---|---|
| `todos.get_row(conn, task_id, number)` | the per-task `number` | **anything a human or an agent typed** — every `#N`, every `todo=N` |
| `todos.row_by_id(conn, task_id, todo_id)` | the internal `todos.id` | internal foreign-key joins only — `contracts.todo_id`, `messages.todo_id`, `todo_decisions.todo_id`, `todo_drop_consents.todo_id` |

The rule in one line: **a value that came from a human or an agent goes through
`get_row`; a value that came from the database goes through `row_by_id`.** Both are
scoped to `task_id`, so task A's `#1` can never reach task B's. Mixing them is how a
request lands on the wrong deliverable — and it fails quietly, because both readings can
be valid integers for the same task at the same time.

The two surfaces expose the two keys differently, on purpose:

- **The dashboard API ships BOTH.** `todo_id` is the internal key the UI keys its
  selection on; `todo_number` / `number` is what it *prints*, because that is what a
  human reads and types. Message chips in the thread are the tricky case: the
  `messages.todo_id` column is authoritative when it is set, but a `#N` scraped out of
  message prose is a **number** — so it resolves against numbers first, and falls back to
  ids only for rows written before numbering existed. See `api.py:_todo_keys` and the
  comment beside its caller.
- **MCP replies to agents ship only the number.** The internal `todo_id` is not in an
  agent-facing reply. Handing an agent two integers when only one of them is a valid
  selector is an invitation to pass the wrong one.

### Every todo-scoped command carries the `#N`

"Backend is ready" is meaningless — ready on **which** deliverable? So the number is
**required**, not conventional, on every command that acts on a todo:

`pc #N` · `gc #N` · `sign #N` · `ship #N` · `decline <why> #N` · `reopen #N` ·
`ready #N` · `ok #N` · `block #N` · `done #N`

— plus `yes #N` / `no #N <why>` at the agreement stage. The broker refuses the bare form
and lists the live todos in the error, so the ambiguity is removed by construction rather
than by convention.

There is **no task-level path**, and not merely none to teach. There is exactly **one kind
of contract: an agreement about one todo** — `contracts.todo_id` is `NOT NULL`, so the
database itself cannot hold any other kind. Contracts that predated todos were not deleted:
each was **adopted** by a new todo on its task, whose party list is the cast that signed it,
so the history keeps reading correctly through the same one path everything else uses.

A task with one deliverable gets one todo, and it is `#1`. A task with none has nothing to
contract, and the broker says so rather than accepting a contract nobody can report against.

The single exception is `stuck`, which is valid at both levels and stays
distinguishable: `stuck #N` flags **one** deliverable and the others keep marching; a
bare `stuck` escalates the **whole collaboration** to a terminal state that needs a
human to reopen.

---

## 5. The two traps

### Trap 1 — `sign` before anything is proposed does nothing

Signing needs a **proposal to sign**. `sign #3` when nobody has run `pc #3` has nothing
to attach a signature to, so the answer is a refusal, not progress.

If you typed `sign #N` and nothing moved, the overwhelmingly likely cause is that
**nobody has proposed the contract yet**. The move is `pc #N`, by either party, and
whoever makes it becomes the producer.

An agent told to sign here should not come back asking what to do: "sign it" is the
direction to *agree this shape*, and a party can supply the missing proposal itself —
propose with its reading written down as an **explicit assumption**, then sign (that is
exactly `ship #N`). It is safe because it is not the last word: the peer still reads it
and signs or `decline`s, and nothing locks until every party has signed. If the
instruction genuinely cannot be reduced to one reasonable shape, ask **once** — a
reaffirmation is a decision, not a repeat of the question.

### Trap 2 — nothing auto-advances; every arrow is a person deciding

The broker **enforces**; it never advances. There is no timer, no daemon, no
"contract locked, therefore start building". Every transition above is a human telling
their agent to make a move, and the broker either allowing it or refusing it.

So a todo sitting at `contract_locked` forever is not a bug and not a hung process — it
means **the producer has not typed `ready #N` yet**, and quite possibly nobody realises
it is their turn. This is the failure the dashboard's **Next** line now fixes: the todo
card prints who owes the move and the literal shorthand they type, derived server-side
from the same gates listed above (`state.next_step`), so it can never advertise a
command the broker would refuse.

When something looks stuck, ask the two questions in order:

1. **Whose move is it?** (the Next line answers this, or work down §3)
2. **Have they actually typed it?**

---

## Where this lives in the code

| Concept | Source |
|---|---|
| the per-task number | `src/sys_buddy/db.py` — the `todos` table and its unique `(task_id, number)` index |
| resolving a `#N` a human or an agent typed | `src/sys_buddy/todos.py` — `get_row(conn, task_id, number)`, scoped by `task_id`. **The** resolver for typed input |
| resolving an internal `todos.id` from a join | `src/sys_buddy/todos.py` — `row_by_id(conn, task_id, todo_id)`. Foreign keys only, never a value a person typed |
| both keys on the wire | `src/sys_buddy/todos.py` — `to_dict`; `src/sys_buddy/api.py` — `_todo_keys` for the thread's message chips |
| the agreement stage, derived | `src/sys_buddy/todos.py` — `status_of`, `STATUSES` |
| the march + its rollup | `src/sys_buddy/todos.py` — `_MARCH_RANK`, `rollup`, `apply_rollup` |
| who the producer is | `src/sys_buddy/state.py` — `_producer_role` |
| the gates | `src/sys_buddy/state.py` — `report_status`, `_report_on_todo`, `_resolve_contract_todo`, `lock_contract` |
| the "what happens next" line | `src/sys_buddy/state.py` — `next_step`; shipped by `api.py`, rendered by `ui.html` |
| the shorthand cheatsheet | `src/sys_buddy/ui.html` — `shortcodesHTML` |
