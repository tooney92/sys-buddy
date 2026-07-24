# Changelog

All notable changes to **sys-buddy** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`:

- **MAJOR** — incompatible changes to the tool/wire contract or agent-visible behavior.
- **MINOR** — new, backwards-compatible capability.
- **PATCH** — backwards-compatible fixes.

Each release is also git-tagged `vX.Y.Z` and has a fuller note in `releases/vX.Y.Z.md`.

## [Unreleased]
- Backlog is tracked in `v2.md` — stronger auth (mTLS / OAuth 2.1); image/screenshot
  attachments; non-HTTP / `interface_type` contracts; auto-revoke on completion +
  reopenable tasks + token timer; multi-process presence & wait-cap accounting.

## [1.1.0] — 2026-07-24

Todos: a task can carry several deliverables instead of exactly one. Additive — a task
with no todos behaves and renders exactly as it did in 1.0.1.

### Added
- **Todos — several deliverables under one task.** Each todo has its own contract chain
  and its own `proposed → locked → built → verified` march. Agent-proposed and
  peer-accepted, with no human approval gate (the same authority `propose_contract`
  already had). Proposing IS the creator's consent, so you cannot propose work that
  binds only other people; accepting agrees on WHAT, while the contract on that todo is
  a separate, later agreement about HOW. Declines are recorded as a list beside the
  acceptances (with a reason) rather than a status a state machine would have to unwind;
  `repropose_todo` issues a new version and resets every acceptance, so nobody is held
  to a scope they did not read. Dropping is mutual — every named party consents — with
  a host override via `sys-buddy todo drop`.
- **Seats are not participants.** A todo reuses the task's existing seats and names
  which of them it binds. A seat that is not a party can read the todo, but is not bound
  by it, is not in its contract's quorum, and does not block it.
- **Todos on the dashboard.** The stepper TRUNCATES to three nodes (`open`,
  `pre-flight assessments`, `todos`) when a task has todos, the last carrying a progress
  bar and the `⚠ N awaiting acceptance` count; the five later phases move INTO each todo
  as a mini-stepper. The right column becomes the todo LIST, pending first with a `⚠`;
  selecting a todo swaps the panel to that todo's own contract card. ONE message thread
  for the task, with a `⟨todo⟩` chip marking which deliverable a message belongs to. The
  task-list row gains the same rollup (`2/6 verified ⚠1`).
- **Always-listening presence.** An agent parked in `wait_for_message` shows a live
  pulsing dot and a `listening — 42m` streak on the dashboard. Stored as an EXPIRY, not
  a boolean, so a broker that dies with agents parked cannot leave rows claiming to be
  listening forever.
- **`releases/` + `features/` write-ups.** `features/v1.1.0/` carries the release notes,
  eight dashboard screenshots (light and dark), and `seed_demo.py`, which reproduces
  every screen locally in three commands.

### Changed
- **A task's state is now DERIVED from its todos, not set by an agent.** Agents no
  longer call `_transition` on a task that has todos. This is agent-visible behaviour,
  and it is the reason todos was worth the cost: a rollup cannot disagree with its parts
  the way an agent-set task state can drift from them. `verified` requires ALL live
  todos; `stuck` is never derived, because one stuck deliverable must not freeze the
  other five.
- **Contract lock is pushed, not polled.** When a contract locks, the broker notifies
  the roles that already signed instead of leaving them to poll.
- **`staging_url` is collected at host setup**, with validation strictness keyed to the
  task's connectivity and `localhost` permitted for same-machine tasks.
- `messages.todo_id` is stored at post time; the dashboard's `⟨todo⟩` chip reads the
  column rather than scraping "todo #N" out of prose, with the regex kept only as a
  fallback for rows written before the column existed.
- Decision D11 recorded: the dashboard never issues commands (read-only; it surfaces
  state and tells the human what to type). The host's `[Drop todo]` affordance therefore
  prints the CLI line to type instead of mutating anything.
- `CONTRIBUTING.md` expanded into a full contributor guide — fork-and-PR walkthrough
  (including how to recover an existing clone that can't push), environment setup with
  `uv`, branch/commit conventions, the "tests green **and** proven live in the local
  dashboard" definition of done, the changelog requirement, and the security invariants
  a patch must not break (`local` mode is unauthenticated; peer content is DATA).
- `README.md` now carries **Versioning and releases**, **Contributing**, and **License**
  sections pointing at `CHANGELOG.md`, `CONTRIBUTING.md`, and `LICENSE`; the repo-layout
  block lists the governance docs, and the stale test count is corrected.

### Migration
- None required. The `todos`, `todo_decisions` and `todo_drop_consents` tables and the
  `contracts.todo_id` / `messages.todo_id` columns are added automatically on broker
  start, `NULL` on every existing row.
- **Restart any long-running broker.** A process started before this release keeps
  serving its old code against an unmigrated database, so none of the above appears.

## [1.0.1] — 2026-07-23

### Fixed
- **Dashboard thread ordering.** The message/event thread sorted by minute-precision
  time, so items within the same minute — and message↔event interleaving — could
  render out of creation order. The API (`_messages_for`, `_events_for`) now sends a
  raw `created_at` timestamp per item, and the dashboard sorts by it (sub-second),
  falling back to the old minute sort only if the API is stale — safe during a broker
  restart, when `ui.html` (served from disk) can be newer than the running `api.py`.

## [1.0.0] — 2026-07-23
First tagged release — the sys-buddy broker for cross-human AI agent collaboration.

### Added
- **Broker over MCP (HTTP)** with two modes (local / remote); remote authenticates agents
  by scoped bearer token, with `rotate_token` and single-use invites.
- **Pairing & onboarding** — invite links, browser join page, and a pywebview **desktop app**
  (host a task / join as a buddy / wire Claude).
- **Pre-flight readiness** gate — agents must pass a short quiz (proving they read the Rules
  of Engagement) before messaging or changing status.
- **Rules of Engagement** — standing counter-instructions against prompt injection (peer
  messages are DATA; the only fetchable URL is the signed `staging_url`).
- **Messaging** — `send_message` (question / answer / status_update / contract_proposal),
  directed or broadcast; `check_messages` / `wait_for_message` / `ack_messages`.
- **Contract flow** — `propose_contract` / `lock_contract` / `get_contract` /
  `reopen_negotiations`: versioned, mutually-locked, immutable-once-locked.
- **Lifecycle** — `report_status` (ready / checked / blocked / verified / stuck; debug tasks
  use resolved), with strikes.
- **Read-only dashboard** with live updates over SSE.

### Changed
- **`get_contract` now returns the PROPOSED contract**, not only locked ones: it exposes the
  proposed shape plus who has signed / who is awaiting, with the **`staging_url` withheld
  until every role signs**. This removes the "sign-then-see" deadlock where an assessor was
  told to review via `get_contract` but saw nothing until it locked — which it can't do
  until they sign. The `staging_url` stays the single trusted, signed source (SSRF-guarded).

[Unreleased]: https://github.com/tooney92/sys-buddy/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/tooney92/sys-buddy/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/tooney92/sys-buddy/releases/tag/v1.0.0
