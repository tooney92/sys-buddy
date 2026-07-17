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
