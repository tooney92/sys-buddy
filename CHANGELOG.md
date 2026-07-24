# Changelog

All notable changes to **sys-buddy** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`:

- **MAJOR** — incompatible changes to the tool/wire contract or agent-visible behavior.
- **MINOR** — new, backwards-compatible capability.
- **PATCH** — backwards-compatible fixes.

Each release is also git-tagged `vX.Y.Z` and has a fuller note in `releases/vX.Y.Z.md`.

## [Unreleased]
- Backlog is tracked in `v2.md` (image/screenshot attachments; non-HTTP / `interface_type`
  contracts; staging_url at host setup + localhost for same-machine tasks; three-role /
  scoped-parties contracts).

## [1.0.2] — 2026-07-23

### Infrastructure
No agent-visible or tool-contract changes; packaging/dev-tooling only.
- **Docker packaging.** A multi-stage root `Dockerfile` (built with `uv`) and
  `docker-compose.yml` run the broker as a single container with a persisted
  `sys-buddy-data` volume for the SQLite DB.

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

[Unreleased]: https://github.com/tooney92/sys-buddy/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/tooney92/sys-buddy/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/tooney92/sys-buddy/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/tooney92/sys-buddy/releases/tag/v1.0.0
