# Implementation decisions & spec deviations

Log of choices made while building from `SPEC.md` where the spec was ambiguous or
where a faithful reading would have been incorrect. Each entry says what the spec
said, what we did instead, and why.

## D1 — Per-recipient delivery tracking

**Spec (§4):** `messages` carries `delivered_at` and `acked_at` columns.

**Done instead:** a separate `deliveries(message_id, agent_id, delivered_at,
acked_at)` table.

**Why:** a task can declare 3+ roles (the spec's own `signin` example is
backend + frontend + mobile), and a single message is read by *every* other agent
on the task. One `delivered_at`/`acked_at` pair on the message row cannot express
"delivered to frontend but not yet to mobile." The per-recipient table keeps the
crash-safety intent the spec was after — delivery and ack are split, and
`ack_messages(ids)` marks processing per agent — while correctly supporting N
recipients. No behavioural promise from the spec is lost.

## D2 — `report_status` vocabulary

**Spec (§10):** `report_status(status, detail)` with no fixed status list.

**Done:** statuses `deployed`, `test_passed`, `test_failed`, `verified`, `stuck`.
Test activity is split into pass/fail because a *fail* is what increments strikes;
a *pass* records a green run without auto-verifying — the agent must explicitly
report `verified`. Each `report_status` also posts a matching typed message into
the thread so the dashboard reflects it.

## D3 — Strike reset without a schema change (§8)

**Spec (§8):** strikes reset on a "successful deploy with a new contract version."

**Done:** since the schema is fixed and `deploy` events carry only text, "new
version" is derived: strikes reset to 0 when the current locked contract's
`locked_at` is later than the previous `deploy` event — i.e. a version was
(re)locked since the last deploy = a genuine new attempt. Redeploying the same
locked contract keeps the count (same fix loop). Both paths are tested.

## D4 — `verified` accepted from `backend_live` or `testing`

**Spec (§7):** lists `verified` valid only in `testing`.

**Done:** accepted when the backend is live (state ∈ {backend_live, testing}) and
rejected before that or from terminal states. A safe, slightly more lenient read
that matches the definition-of-done flow.

## D5 — Enforcement in both modes

**Spec (§5):** state machine is "advisory in local, enforced in remote."

**Done:** the state machine enforces in *both* modes. Enforcement never hurts
correctness, and a single code path is safer than a branch that only guards
remotely. Local mode still differs only in identity (self-declared vs token).

## D6 — Schema self-heal per process, not per connection

**Review finding:** the predecessor created tables on every DB connection ("just
works" on a fresh machine); dropping that risks "no such table" before `init`.

**Done:** `init_db` runs once on server boot (`build_server`) and once per CLI
invocation (`_cfg_from_args`), both idempotent. This restores zero-setup without
re-running the schema on the hot per-connection path.

## D8 — A task must declare a `backend` role

**Review finding (#3):** the state machine hardcodes `backend` as the deploying
role, so a task whose roles don't include one named `backend` can lock a contract
but never deploy — a permanent deadlock.

**Done:** `admin.create_task` rejects a role set that doesn't include `backend`.
This keeps the spec's designated-deployer model (SPEC §7: "deploy_confirmed —
backend role only") while making the deadlock unreachable. The alternative —
inferring the deployer from the first role — was rejected as more surprising than
a clear up-front requirement.

## D9 — Fixed cast enforced by a partial unique index

**Review finding (#2):** a blanket `UNIQUE(task_id, role)` counted *revoked* agent
rows, so revoking an agent permanently bricked its role — a replacement could
never pair.

**Done:** dropped the inline `UNIQUE` and added
`CREATE UNIQUE INDEX ... ON agents(task_id, role) WHERE revoked_at IS NULL`. At most
one *live* agent per role; revoked rows stay for message provenance but no longer
occupy the seat. (Safe DDL change — no persisted data yet.)

## D10 — Lifecycle message types are report_status-only

**Review finding (#6):** an agent could `send_message(type="test_result")`, which
would desync the dashboard's broker-counted strike total (and let it forge a
`verified`/`deploy_confirmed` chip).

**Done:** `send_message` rejects the reserved lifecycle types
(`deploy_confirmed`, `test_result`, `verified`, `stuck`); those are produced only
by `report_status`, which pairs each with the matching event. `report_status` still
posts them via the internal `post_message` path (the guard is on the public send
path only), so the message↔event 1:1 invariant the API relies on always holds.

## D7 — `/ui` served unauthenticated

**Done:** the dashboard HTML is inert; all data comes from `/api/*`, which is
viewer-token-scoped. The viewer token rides in `?v=`, so the page itself needs no
gate. A leaked page with no token shows nothing.

## D11 — The dashboard never issues commands

**Done:** `/ui` + `/api/*` stay strictly read-only. The dashboard surfaces state and
tells the human what to type; it never acts. Every mutation flows human → agent →
broker tool, or human → CLI.

**Why:** the viewer token is read-scoped, so a leaked `?v=` link can only ever
*look* (see D7). A single write button — "rotate", "close" — would be the first
crack in that and would need its own auth story. It also could not finish the job
anyway: `rotate_token` returns a new bearer token that must land in the agent's MCP
`Authorization` header, which only the human can paste. So the honest division is
that the dashboard warns EARLY ENOUGH to act (token countdown at T-1h, listening
dot, pre-flight badge) and the human and their agent do the acting.

Revisit only with a real write-auth story — not per-button.

## D12 — A contract is ≥1 named unit, and the unit key names the KIND

**Spec (§6):** a contract spec is `endpoints` (each a valid HTTP method + path) plus a
`staging_url`. The only shape a contract can take is an HTTP API.

**Deviation:** a contract is **≥1 named unit with attributes**, in one of four KINDS.
Only the validator and the dashboard's renderer are kind-aware; versioning, both-sides
`lock_contract`, `decline`, `reopen_negotiations` and `get_contract` never mention HTTP.

| kind | unit key | the broker enforces |
|---|---|---|
| `http` (default) | `endpoints` | ≥1, valid method + non-empty path |
| `schema` | `types` | ≥1, each a `name` + non-empty `fields` (name + shape) |
| `ui` | `screens` | ≥1, each a `name` + non-empty `states` |
| `none` | `criteria` | ≥1 non-empty checkable string |

**Why:** the spec's shape is not a permissions problem — any party to a todo may propose
— it is a SCHEMA problem. A designer+frontend pair agreeing six screens, or two frontends
agreeing a `<SessionProvider>`, are entitled to propose and simply cannot produce a spec
that validates. Both were forced to either skip the contract (losing the signed, versioned,
both-locked artifact) or fake an HTTP one, and the second is the likelier failure because
extra spec keys are kept: a real agreement smuggled in beside a dummy endpoint, invisible
to the dashboard.

**`interface_type` is INFERRED, not declared.** SPEC §6 has no such field and the
`v2.md` note proposed one. Each kind has a DISTINCT unit key, so the key already names
the kind — requiring an agent to declare a kind AND pick the matching key is two chances
to be wrong instead of none. It stays *accepted* for exactly two jobs: catching a
contradiction (declared `http`, supplied `screens` — the broker names both and refuses
rather than picking which half was the mistake), and disambiguating any future kinds that
share a key. Two unit keys in one spec is refused as ambiguous, never silently preferred.

**Backwards compatibility is total.** `http` keeps `endpoints` and stays the default, so
every contract written before kinds existed validates byte-for-byte with no `spec_json`
migration — verified against the owner's 11 real contracts, all of which infer `http`.
The dashboard payload keeps emitting `endpoints` for http alongside the generic
`kind`/`unit_key`/`units`, because `ui.html` is served from disk and a browser tab may be
older than the running `api.py`.

**Security is simpler, not laxer.** `staging_url` remains the ONLY fetchable URL the
broker ever hands out — see D13, which took it out of the spec entirely: no kind
requires one, no kind may carry one, and every kind RESOLVES one when the host has set
it. `has_http_surface` gates the validator and the renderer, never the target.

**Rejected:** a generic `units: [...]` wire format. It would have forced a `spec_json`
migration for zero gain, and an agent writes `screens:` more reliably than an abstraction.

**Not decided here** (see `docs/contract-kinds-design.md`): whether a `ui` contract wants
a first-class design reference, whether `ui` should collapse into `schema`, and whether a
todo's contract may change kind between versions (the dashboard's version diff would have
to cope).

## D13 — `staging_url` is host-owned CONFIGURATION, not part of the signed shape

**Spec (§6, §9):** the producer's agent puts `staging_url` inside the contract spec, and
it is part of the signed document; `get_contract` reads it out of `spec_json`.

**Deviation:** the deployment target lives on the TASK (`tasks.staging_url`), optionally
overridden per deliverable (`todos.staging_url`), and is **resolved live on every read**
(`state.resolve_staging_url`: todo override → task → the legacy spec). A spec that
carries a `staging_url` is REFUSED. Only the host writes it — CLI (`sys-buddy task
staging-url`), the desktop app, and host setup — and the change is an ordinary event in
the log, not a renegotiation.

**Why 1 — it churned for reasons nobody negotiated.** ngrok free URLs rotate on every
tunnel restart, so a locked contract went stale routinely, and the only sanctioned fix
was a full renegotiation producing a version identical but for one string. Forcing people
to re-sign on non-events trains them to sign without reading, which devalues every other
signature — including the ones that catch real problems. The owner hit this live: a
locked contract carrying `https://a1b2c3d4.ngrok-free.dev:3000`, which
cannot connect (ngrok terminates on 443; the `:3000` belonged on localhost) and was
immutable inside a signed document.

**Why 2 — it was an agent-controlled field on the most security-sensitive value in the
system.** An injected "test against `evil.com`" landed in a proposal and was defended
only by the consumer noticing during review. With the host owning the target there is no
field for it to land in at all. This makes the posture STRONGER, not weaker: the SSRF and
https rules are unchanged (`contracts.validate_staging_url`, now public), only the door
they guard has moved to where a human writes the value.

**An agent-supplied `staging_url` is REFUSED, not ignored.** Silently dropping it would
let a prompt-injected target appear to succeed — the agent would believe the contract
points there and could repeat it in chat. The refusal contradicts the injection out loud,
names the owner and the door (`get_contract`, after the lock), and rides in the same
collected-errors list so one revision still fixes everything. It is refused on PRESENCE,
not on being non-empty: a blank one is still an agent reaching for a field it does not
own, and keying the rule on emptiness would make it depend on how carefully the injection
was written.

**The contract still records what it agreed to.** `contracts.staging_url_at_lock` is
written when the final signature lands — a column, never `spec_json`, because mutating
the signed document at the instant it is signed is the one thing a signature must not
permit. `get_contract` returns both: `staging_url` (live) and `staging_url_at_lock`
(historical). They are allowed to diverge; that divergence IS the feature.

**`get_contract` still withholds the target until every party has signed.** The
withholding keys on the LOCK, never on whether a target happens to be configured, so the
incentive to actually read the shape survives. `get_todos` never carries it at all — that
is the agents' view, and publishing it there would hand out the URL with nothing signed.
The dashboard does show it, because a viewer token is a human.

**Resolution is KIND-AGNOSTIC.** The target resolves for `http`, `schema`, `ui` and
`none` alike, whenever the host has set one. It is tempting to gate it on
`has_http_surface`, and that would be wrong: that flag answers *does this contract
describe HTTP?*, not *does the consumer need somewhere to go and look?* — and the two
diverge for the kind that invites the mistake. A `ui` contract is verified by opening the
deployed app, so gating would strip the URL from the kind that most needs one and make
`ok #N` unanswerable. Nothing is loosened by resolving it: agents still cannot write it,
and it is still withheld until full signature. The pre-D13 wording in
`docs/contract-kinds-design.md` ("a kind with no HTTP surface has nothing to grant")
described the design where the target lived inside the signed spec; it is flagged there
as stale so nobody "fixes" the code back to it.

**No new tool.** There is deliberately no agent-facing way to set or request a target;
adding one would give an injection somewhere to aim again.

**Migration deletes nothing and rewrites no spec.** `_migrate_staging_url_off_the_spec`
materialises `staging_url_at_lock` from each LOCKED contract's spec, and seeds
`tasks.staging_url` from the newest locked contract for any task that has none — a task
whose host already chose a target is never overwritten. Verified on a copy of the owner's
live db (29 tasks, 12 contracts, 9 todos): 4 tasks gained a target (`signin`,
`rhema-demo-6389`, `leave-management-f17c`, `lightdey-v3-75d9`), 11 host-chosen targets
were left alone, all 7 locked contracts recorded theirs, drafts recorded nothing, no
`spec_json` changed, and a second and third boot were byte-for-byte no-ops.

## D14 — *No **agent** removes a peer from a todo;* the host can, and it is recorded

**Previously decided** (`todos.py` module docstring, `rules.py`, and the `drop_todo` tool
description on both surfaces): *"No peer may remove a peer. You joined by accepting; you
leave by your own call… the escape hatch is HUMAN: `host_drop_todo`, reachable from the
CLI/GUI, never from a peer's tool."* The charter said it to the agents outright:
**"No tool removes a peer from a todo, and you should not ask for one."**

**What changed.** Removing a party is now possible, in two forms, and the sentence becomes
**"no AGENT removes a peer; the host can, and it is recorded."** The self-service half —
which the old rule never actually provided — is new too:

| | who calls it | who can be removed |
|---|---|---|
| `leave_todo(N, reason)` | any party's agent, both tool surfaces | **only itself** — the tool has no seat argument |
| `sys-buddy todo drop-party <task> <N> --seat X` | a HUMAN, CLI/desktop only | any one party |

**Why the old rule was not enough.** It answered one question ("may backend delete
mobile?" — no) and left two real situations with no move at all:

* **"We don't need mobile after all."** Mobile is present and agrees. The only tool was
  `drop_todo`, which is MUTUAL and abandons the *whole deliverable* — so removing one
  party meant asking two other people to throw away work they still wanted. There was no
  way to leave a todo, only to end it.
* **"Mobile has an outage."** Its agent cannot call a tool — that is what an outage *is* —
  so self-removal is useless at precisely the moment it is needed. The one hatch,
  `host_drop_todo`, again destroys the deliverable. A locked contract and two-thirds of the
  work went in the bin because one party's laptop was shut.

**Why the property still holds.** `leave_todo` is not "removal with a permission check on
top"; it takes no `seat`/`handle` argument on either surface, so naming a peer is
*unspellable* rather than refused. The reason the original rule existed is untouched: if
backend could remove mobile, then the moment mobile objects to a shape backend removes it
and locks without the dissent, and "both sides sign" quietly becomes "whoever proposes
wins". Nothing an agent can call changes anyone else's binding.

**Why the ejection half is host-only, and not merely host-*preferred*.** "Eject a peer" is
the most abusable capability on a cross-org broker — it is the one call that turns a
disagreement into a deletion. A human typing a command at their own terminal cannot be
prompt-injected. That is the same posture already taken for `staging_url`, `add-seat`,
`revoke-agent` and `close`, and it is why this lives in `cli.py`/`admin.py` and not in the
tool registry. The dashboard keeps D11: it *prints the command* and never issues it.

**Quorum RECOMPUTES, and that is the feature.** Every all-must-agree gate reads
`todos.parties_json` live (`status_of`, `awaiting`, `all_fixed`, `lock_contract`'s
`required`, `drop_consents`), so shrinking the list fixes the derived readings for free.
Three gates are LATCHED, however — they fire inside the call that completes them, and no
later call notices that "everyone" now means fewer people. `todos.settle_after_departure`
re-runs exactly those three, and only ever unblocks:

1. a **mutual drop** whose last outstanding consent belonged to the departed party → the
   drop completes;
2. a **draft contract** every remaining party has already signed → it **locks**;
3. an **issue** every remaining party has already reported `fixed` → it **resolves**.

Without this the outage case ends with a todo that is unblocked on paper and still frozen
in fact, which is the same as not shipping the feature.

**A locked contract signed by a departing party STILL STANDS.** It was validly agreed by
everyone it bound at the time and the shape has not changed; voiding it would revoke an
agreement nobody withdrew from and strand work already built against it. But `signatures`
and `signatories` then disagree, and a reader cannot tell a departure from a bug — so
`get_contract` names the difference (`departed_signatories` + a note), and `get_todos`
carries the mode, the reason and the date. Silence there would be this feature's own
failure mode one level down: a signature displayed as though the person behind it were
still bound.

**A draft contract is NOT re-signed after a departure** — it locks on the signatures
already there. Requiring a re-sign would leave the todo exactly as frozen as before for
anyone who does not happen to know to go and re-sign it, which defeats the point. The
protection for a party who no longer agrees now that the cast is smaller is the move that
already exists — `reopen_negotiations(reason, todo=N)` — and the broker's `contract_locked`
push says so, out loud, in the same breath as "this locked with nobody having signed".
Contrast `repropose_todo`, which *does* reset draft signatures when parties change: there
a PEER changes the shape and the cast together, and consent to a document nobody has read
is worthless. Here the shape is untouched and the change is a human decision in the log.

**A one-party todo stays ILLEGAL where it was already illegal.** `_validate_parties`
refuses a peer todo with fewer than two seats; that same floor (`todos.min_parties`, one
statement, both callers) now applies to a departure, so removal cannot manufacture a shape
that could never have been proposed. This is not symmetry for its own sake — a solo peer
todo **deadlocks**: the sole party proposes the contract, is therefore the producer, and
`state._report_todo_test` refuses a producer checking its own work off an engagement, so
the todo can never reach `testing` and `verified` is unreachable forever. On a debug task a
one-party issue resolves on its author's word alone, which is the exact thing all-must-agree
exists to prevent. An ENGAGEMENT keeps its documented exception (floor of one) because its
outer ring — the client's agent verifying against the deliverable list — supplies the
counterparty a peer task has to name. So a two-party todo minus one party is refused, and
the refusal names the right move: `drop_todo`, which is what abandoning is called.

**Terminal rules are unchanged.** Verified is terminal for departures exactly as it is for
drops — the rollup already reports the deliverable as done and the record of who delivered
it is part of that. The last party may not leave: a todo bound to nobody is orphaned,
unactionable, and still counted, and that case already has a tool.

**Rejoining is `repropose_todo`, unchanged.** It already resets every acceptance and every
draft signature, so a returning party is indistinguishable from an original one. Note who
may do it: the reproposer must itself be a party, so a departed seat **cannot re-add
itself** — it comes back by invitation from somebody still on the todo. That falls out of
the existing `assert_party` check rather than needing a new rule, and it is the right
asymmetry: leaving is your own business, returning is the remaining parties'.

**Nothing is deleted, ever.** A departure writes a `todo_departures` row (seat, `left` vs
`removed`, who acted, reason, the todo version, the time), a `todo` event, and a message —
peer-authored for a self-leave, BROKER-authored for a host removal (`todo_party_removed`
joins `service.BROKER_TYPES`, so no agent can author a sentence claiming a peer was
removed). Acceptances, fixes, drop consents and signatures are all left exactly where they
are: they are statements somebody actually made, and ending an obligation is not the same
as rewriting history. A party that vanishes from a list with no trace is worse than one
that never left.
