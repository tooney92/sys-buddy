# The todo flow — from idea to verified

A **todo** is one deliverable under a task ("six todos, a contract on each"). This is
the whole life of one, in order, with the shorthand each human types to **their own**
agent and the rule the broker enforces at each step.

If you only read one thing, read [The two traps](#the-two-traps). They account for
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

The dashboard's stepper collapses the six march values into four labels:

```
planning · building · ready · verified
   │           │        │        │
   │           │        │        └── verified
   │           │        └── backend_live AND testing
   │           └── contract_locked
   └── open AND contract_proposed
```

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
| every party | `sign #N` | `lock_contract(version, todo=N)` | state `contract_locked`, status `contracted` |

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

The **task** concludes when the **last** todo verifies. Once a task runs on todos its
own state is never set by an agent at all: it is re-derived from the todos after every
report (`todos.apply_rollup`), so it cannot drift from its parts.

---

## 3. The permission gates

These are enforced in code, in `state.py`, and every one of them raises a `ValueError`
with an explanation the agent can read. They are not conventions.

| Move | Refused unless |
|---|---|
| anything on a todo | you are a **named party** on it (`todos.assert_party`) |
| `pc #N` | the todo is **accepted** — agree WHAT before HOW |
| `sign #N` | a proposal exists to sign, you are a party, and it is not already locked |
| `ready #N` | you are the **producer**, a contract on that todo is **locked**, and no newer version is awaiting signatures |
| `ok #N` / `block #N` | you are **not** the producer, and the producer has already reported ready |
| `done #N` | the todo is in `testing` — a check must actually have run |
| any report | the todo is not dropped, not already verified, and not past three strikes |

A locked contract is **immutable**. To change it: `reopen` → propose a new version →
every party re-signs. `repropose_todo` refuses outright once a lock exists.

No peer may remove a peer. You joined by accepting; you leave by your own call. A
mutual `drop` needs every party's consent — which deadlocks on exactly the party who
has gone silent, and that is why the escape hatch is **human**:
`sys-buddy todo drop <task> <N> --reason "…"`, reachable from the CLI and the desktop
app, never from a peer's tool.

---

## 4. Why `#N` is mandatory

Once a task has todos, "backend is ready" is meaningless — ready on **which** of the
six deliverables? So `ready` / `ok` / `block` / `done` are todo-scoped and the id is
**required**; the ambiguity is removed by construction rather than by convention. The
broker refuses the bare form and lists the live todos in the error.

The one exception is `stuck`, which is valid at both levels and stays distinguishable:
`stuck #N` flags **one** deliverable and the other five keep marching; a bare `stuck`
escalates the **whole collaboration** to a terminal state that needs a human to reopen.

---

## 5. The two traps

### Trap 1 — `sign` before anything is proposed does nothing

Signing needs a **proposal to sign**. `sign #3` when nobody has run `pc #3` has nothing
to attach a signature to, so the answer is a refusal, not progress.

If you typed `sign #N` and nothing moved, the overwhelmingly likely cause is that
**nobody has proposed the contract yet**. The move is `pc #N`, by either party, and
whoever makes it becomes the producer.

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
| the agreement stage, derived | `src/sys_buddy/todos.py` — `status_of`, `STATUSES` |
| the march + its rollup | `src/sys_buddy/todos.py` — `_MARCH_RANK`, `rollup`, `apply_rollup` |
| who the producer is | `src/sys_buddy/state.py` — `_producer_role` |
| the gates | `src/sys_buddy/state.py` — `report_status`, `_report_on_todo`, `_resolve_contract_todo`, `lock_contract` |
| the "what happens next" line | `src/sys_buddy/state.py` — `next_step`; shipped by `api.py`, rendered by `ui.html` |
| the shorthand cheatsheet | `src/sys_buddy/ui.html` — `shortcodesHTML` |
