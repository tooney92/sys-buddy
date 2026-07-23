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
