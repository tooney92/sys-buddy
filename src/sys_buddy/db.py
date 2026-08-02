"""SQLite storage, WAL mode, schema (SPEC §4).

WAL matters here: the dashboard polls read queries every ~3s while agents write
messages and the long-poll loop opens a connection every ~2s. WAL lets readers
never block the writer (and vice versa) — a known bug in the ``agent_bus.py``
predecessor was the absence of it.

Deviation from SPEC §4: delivery state is tracked per-recipient in ``deliveries``
rather than as ``delivered_at``/``acked_at`` columns on ``messages``. A task can
have 3+ roles (the spec's own ``signin`` example has backend+frontend+mobile) and
one message is read by every other agent on the task, so a single pair of columns
on the message row cannot represent "delivered to frontend but not mobile". The
per-recipient table preserves the crash-safety intent — split delivered/acked,
``ack_messages(ids)`` — while supporting N agents. See DECISIONS.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .config import get_config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    state       TEXT NOT NULL,
    -- 'contract' | 'debug' | 'engagement'.
    -- `engagement` is commissioned work: the cast gains an `owner` seat, the owner
    -- sets DELIVERABLES, and nothing may be built until the team has agreed them.
    -- Peer sessions are completely unaffected — every engagement rule is gated on
    -- this column, so `contract` and `debug` behave exactly as before.
    mode        TEXT NOT NULL DEFAULT 'contract',
    -- The declared cast, as a list of SEAT HANDLES. The SHAPE is unchanged — a JSON
    -- list of strings, one per seat — and that is the whole trick behind N-per-role:
    -- every quorum path already does `[r for r in required if r not in signed_set]`
    -- over these strings, so once they mean HANDLES ("frontend", "frontend-2") rather
    -- than role types, "both frontends must sign" falls out with no change to quorum.
    -- Existing rows are already correct under the new reading, because with one seat
    -- per role the handle and the role type are the same string.
    roles_json  TEXT NOT NULL,
    -- `{handle: role_type}` — the OTHER half of what `role` used to conflate. The
    -- handle says WHO (the seat: unique per task, stable forever, quoted in messages
    -- and signatures); the role type says WHAT KIND of work (many seats may share
    -- one; it drives briefing, the pre-flight question set, @FE tags and the
    -- dashboard colour). Backfills to an identity map, so every pre-existing task
    -- keeps behaving identically. Nullable only because ALTER TABLE cannot add a
    -- NOT NULL column without a default; readers fall back to the identity map for
    -- any handle the map does not mention (see seats.seat_roles).
    seat_roles_json TEXT,
    strikes     INTEGER NOT NULL DEFAULT 0,
    -- 1 only when the HOST declared at setup that everything lives on ONE box
    -- (loopback broker origin, no public/tunnel URL). Defaults to 0 so anything
    -- created by another path is validated with the strict remote rules.
    same_machine INTEGER NOT NULL DEFAULT 0,
    -- The deployment target for this task, and the ONLY fetchable URL the broker ever
    -- hands out. HOST-OWNED CONFIGURATION, not part of any signed document: no agent
    -- tool writes it, which is why an injected "test against evil.com" has nothing to
    -- aim at — the attack needs a field an agent can reach and there isn't one.
    -- Resolved LIVE at read time (todos.staging_url overrides it per deliverable), so
    -- restarting an ngrok tunnel re-points every contract on the task and re-negotiates
    -- none of them. See state.resolve_staging_url.
    staging_url TEXT,
    created_at  REAL NOT NULL,
    closed_at   REAL
);

CREATE TABLE IF NOT EXISTS contracts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    version     INTEGER NOT NULL,
    spec_json   TEXT NOT NULL,
    status      TEXT NOT NULL,            -- 'draft' | 'locked' | 'declined'
    proposed_by INTEGER REFERENCES agents(id),
    -- The deliverable this contract is about. NOT NULL, and that is the whole point:
    -- a contract is an agreement on ONE todo, so there is exactly one kind of contract
    -- and no "task contract" vs "todo contract" to disambiguate. The todo's party list —
    -- not the task's full cast — is the signatory set (see todos.py).
    --
    -- It was nullable before todos existed. Legacy rows are adopted by a todo on the
    -- same task rather than deleted (`_adopt_task_contracts_onto_todos`); nothing in the
    -- broker writes a NULL any more.
    todo_id     INTEGER NOT NULL REFERENCES todos(id),
    locked_at   REAL,
    -- The deployment target that was LIVE at the moment this contract locked, recorded
    -- so "what did we agree to?" still has an answer after the host re-points a tunnel.
    --
    -- Deliberately a COLUMN and not a key in `spec_json`: the spec is the signed shape,
    -- and the target is host-owned configuration that no signatory negotiated. Writing
    -- it into the signed document is what made an ngrok restart a full renegotiation —
    -- versions identical but for one string, which trains people to sign without
    -- reading and devalues every signature that does catch something. This is the
    -- historical RECORD; `state.resolve_staging_url` is the live value, and they are
    -- allowed to differ. NULL on every contract locked before this existed and on every
    -- draft — a contract nobody has signed has agreed to no target.
    staging_url_at_lock TEXT,
    created_at  REAL NOT NULL
    -- NO `UNIQUE(task_id, version)` here, and that absence is load-bearing.
    --
    -- `version` is numbered PER CONTRACT CHAIN — 1, 2, 3… within EACH todo, allocated
    -- MAX(version)+1 for that todo — because each todo owns its own chain and a
    -- deliverable's FIRST proposal must read as v1. A task-wide sequence made todo #6's
    -- first proposal "v9", which looks like eight renegotiations that never happened.
    -- So `(task_id, version)` no longer names one row and a table-level UNIQUE on it is
    -- simply wrong.
    --
    -- Uniqueness is enforced instead by the `contracts_todo_version` index created in
    -- init_db, over `(todo_id, version)` — see `_migrate_contract_versions` for why an
    -- existing database needs a full table rebuild to shed the old constraint.
);

-- TODOS (v2): several deliverables under one task, each with its own contract chain
-- and its own proposed→locked→built→verified march. The TASK's state becomes a
-- ROLLUP of these (see todos.rollup) rather than something an agent sets.
--
-- `parties_json` is the decision the feature rests on: SEATS ≠ PARTICIPANTS. A todo
-- reuses the task's seats (you pair ONCE, at the task) and names WHICH of them it
-- binds. A seat not named here may READ the todo, is not bound by it, is not in its
-- contract's quorum, and does not block it.
CREATE TABLE IF NOT EXISTS todos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    -- The HUMAN's `#N`: per-task, starts at 1, assigned MAX(number)+1 on insert. The
    -- GitHub pattern — a global `id` for every internal join (contracts.todo_id,
    -- messages.todo_id, todo_decisions.todo_id, todo_drop_consents.todo_id all point
    -- at `id`, never at this) and a per-parent `number` for the people typing it. A
    -- global AUTOINCREMENT is unusable as a human handle: three tasks with one todo
    -- each read `#1`, `#2`, `#3`, so on the third task the only deliverable is "#3".
    --
    -- NEVER reused (drop #2 and the next todo is #3 — MAX, not COUNT) and NEVER
    -- renumbered, because `#N` is quoted in messages, in the event log and in whatever
    -- the two humans said to each other yesterday.
    --
    -- Nullable at the column level ONLY because an existing db acquires it via
    -- ALTER TABLE ADD COLUMN, which cannot add a NOT NULL column without a default.
    -- Uniqueness is enforced by the `todos_task_number` index created in init_db, and
    -- fullness by the explicit NULL check in the migration — SQLite treats NULLs as
    -- DISTINCT in a unique index, so a half-backfilled table sails past the constraint
    -- and the index alone cannot catch it.
    number        INTEGER,
    title         TEXT NOT NULL,
    scope         TEXT NOT NULL,
    parties_json  TEXT NOT NULL,            -- roles bound by THIS todo (⊆ task roles)
    version       INTEGER NOT NULL DEFAULT 1,  -- bumped by repropose_todo; resets acceptances
    -- The per-todo march. Deliberately NOT the agreement stage: `pending/accepted/
    -- contracted/verified/dropped` is DERIVED from acceptances + contracts + this
    -- column (todos.status_of), so a rollup can never disagree with its parts.
    state         TEXT NOT NULL DEFAULT 'open',
    strikes       INTEGER NOT NULL DEFAULT 0,  -- per-todo ping-pong counter
    proposed_by   INTEGER REFERENCES agents(id),
    proposed_role TEXT NOT NULL,
    created_at    REAL NOT NULL,
    verified_at   REAL,
    -- `stuck` is an orthogonal FLAG, not a state: a stuck deliverable must not brick
    -- the whole task the way task-level `stuck` does, and the rollup still needs to
    -- know how far this todo actually got.
    stuck_at      REAL,
    stuck_reason  TEXT,
    dropped_at    REAL,
    dropped_by    TEXT,                     -- role that finalised the drop, or 'host'
    drop_reason   TEXT,
    -- HOST-SET override of `tasks.staging_url` for THIS deliverable, and nothing else
    -- may write it. Real tasks deploy their pieces to different places (one todo on a
    -- tunnel, another on a staging box), and a single task-level value forced them to
    -- share one. NULL = "no override", i.e. inherit the task's — which is what every
    -- existing todo carries, so nothing changes for them. Like the task-level value it
    -- is configuration, never part of a signed spec, and is resolved LIVE at read time.
    staging_url   TEXT
);

-- Acceptances/declines as LISTS (mirroring contract_signatures) rather than a
-- `declined` status a state machine would then have to unwind. Keyed by VERSION so a
-- repropose resets consent without deleting the audit trail of who accepted v1.
CREATE TABLE IF NOT EXISTS todo_decisions (
    todo_id    INTEGER NOT NULL REFERENCES todos(id),
    version    INTEGER NOT NULL,
    -- NAME LIES, VALUE IS RIGHT: this column holds a SEAT HANDLE, not a role type —
    -- the same reading as `todos.parties_json`, which is where the values come from.
    -- The `UNIQUE(todo_id, version, role)` below is therefore exactly the constraint
    -- we want ("each SEAT decides once per version"): two frontends each record their
    -- own row. Deliberately NOT renamed: the rename costs a full table rebuild on a
    -- table three other code paths join, and buys only a nicer identifier.
    role       TEXT NOT NULL,
    agent_id   INTEGER REFERENCES agents(id),
    decision   TEXT NOT NULL,               -- 'accepted' | 'declined'
    reason     TEXT,
    created_at REAL NOT NULL,
    UNIQUE(todo_id, version, role)
);

-- Dropping is MUTUAL: every named party consents. Version-independent — you are
-- abandoning the deliverable, not a revision of it.
CREATE TABLE IF NOT EXISTS todo_drop_consents (
    todo_id    INTEGER NOT NULL REFERENCES todos(id),
    -- A SEAT HANDLE, exactly like `todo_decisions.role` above — see the note there
    -- for why the column keeps its old name. `UNIQUE(todo_id, role)` = "each seat
    -- consents once", which is what a mutual drop across N seats needs.
    role       TEXT NOT NULL,
    agent_id   INTEGER REFERENCES agents(id),
    reason     TEXT,
    created_at REAL NOT NULL,
    UNIQUE(todo_id, role)
);

CREATE TABLE IF NOT EXISTS contract_signatures (
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    signed_at   REAL NOT NULL,
    UNIQUE(contract_id, agent_id)
);

CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    -- The human's chosen DISPLAY name, supplied at join (pairing.join(..., name)).
    -- Display only: never a key, and two seats may legitimately share one.
    name        TEXT NOT NULL,
    -- The kind of work this seat does: backend / frontend / qa / devops / … Many
    -- seats on one task may share it. NOT the seat itself — that is `handle`.
    role        TEXT NOT NULL,
    -- WHO: the seat. Unique per task among live agents, stable forever, quoted in
    -- message history and signatures — which is exactly why it is never renamed or
    -- re-pointed once used. `parties_json`, quorum and provenance all key on this.
    --
    -- Nullable at the column level ONLY because an existing db acquires it via ALTER
    -- TABLE, which cannot add a NOT NULL column without a default. Fullness is
    -- asserted explicitly in `_migrate_agent_handles`: SQLite treats NULLs as
    -- DISTINCT in a unique index, so a half-backfilled column would sail straight
    -- past `idx_agents_live_handle` and leave seats silently unreachable. The index
    -- CANNOT catch this; only a count can.
    handle      TEXT,
    token_hash  TEXT,                     -- sha256; NULL for implicit local identities
    pubkey      TEXT,
    created_at  REAL NOT NULL,
    revoked_at  REAL,
    expires_at  REAL,                     -- optional TTL; NULL = never expires
    ready       INTEGER NOT NULL DEFAULT 0, -- 0 = not yet passed the pre-flight
    readiness_status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'passed' | 'failed'
    readiness_report TEXT,             -- JSON of the last attempt's per-question results
    listening_until  REAL,             -- presence EXPIRY: parked in wait_for_message while > now
    listening_since  REAL              -- start of the current listening streak (for "listening — 42m")
);

-- NOTE: the unique index over agents(task_id, handle) is created in init_db AFTER
-- `_migrate_agent_handles`, not here — on an existing db `handle` arrives by ALTER,
-- which runs long after this executescript, so indexing it here would fail with
-- "no such column: handle". Its predecessor `idx_agents_live_role` (one live agent
-- per ROLE — the constraint that made two frontends undescribable) is dropped by
-- that same migration and is never recreated.

CREATE TABLE IF NOT EXISTS viewers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT REFERENCES tasks(id),  -- NULL = host (all tasks)
    label       TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    revoked_at  REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    from_agent_id INTEGER NOT NULL REFERENCES agents(id),
    type          TEXT NOT NULL,
    body_json     TEXT NOT NULL,
    state_at_send TEXT NOT NULL,
    created_at    REAL NOT NULL,
    to_role       TEXT,                     -- optional directed recipient; NULL = broadcast to all roles
    -- Which deliverable this message belongs to, or NULL for a task-level message
    -- (everything before todos existed, and every message on a task with none). This
    -- is what the dashboard's ⟨todo⟩ chip keys on — an authoritative id rather than a
    -- string scraped from the body.
    todo_id       INTEGER REFERENCES todos(id)
);

-- Per-recipient delivery tracking (see module docstring).
CREATE TABLE IF NOT EXISTS deliveries (
    message_id   INTEGER NOT NULL REFERENCES messages(id),
    agent_id     INTEGER NOT NULL REFERENCES agents(id),
    delivered_at REAL,
    acked_at     REAL,
    UNIQUE(message_id, agent_id)
);

CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    -- The SEAT this invite fills, i.e. a HANDLE (see `agents.handle`). Invites are
    -- per SEAT, not per role type: with two frontend seats, "an invite for frontend"
    -- would not say which one the redeemer takes.
    role        TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    kind        TEXT NOT NULL,            -- transition|lock|deploy|test|slack|token|task|waiting|file
    detail_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Files shared on a task: design bundles (zip), screenshots (png/jpg), PDFs. NOT videos.
-- Uploaded by an agent (e.g. the `designer`) or a human; fetched by peers through the
-- broker (get_file / GET /api/file/{id}) — never via a URL from chat, so the "only
-- broker-sanctioned fetches" invariant holds. The bytes live in the row for v1 (a blob);
-- move to on-disk with a path column if the db gets heavy. Content is DATA, like a
-- message — an agent reads/extracts it, never runs it (Rules of Engagement rule 4).
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    from_agent_id INTEGER NOT NULL REFERENCES agents(id),
    name          TEXT NOT NULL,           -- original filename
    kind          TEXT NOT NULL,           -- 'screenshot' | 'design' | 'other'
    content_type  TEXT NOT NULL,           -- MIME: image/png, image/jpeg, application/pdf, application/zip
    size          INTEGER NOT NULL,        -- bytes
    data          BLOB NOT NULL,           -- the file itself
    created_at    REAL NOT NULL
);

-- Activity notes: ambient "what we're up to" one-liners a human has their agent post
-- ("researching the OAuth flow"), shown on the dashboard as pills. A THIRD channel,
-- distinct from report_status (lifecycle states) and messages (the conversation) — it
-- carries no lifecycle meaning and is never delivered as mail (it must not wake a parked
-- wait_for_message). Brief by construction (see activity.MAX_ACTIVITY_CHARS). Content is
-- DATA like a message — the peer's agent surfaces it to its human, never acts on it.
CREATE TABLE IF NOT EXISTS activity (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    from_agent_id INTEGER NOT NULL REFERENCES agents(id),
    text          TEXT NOT NULL,
    created_at    REAL NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────────────
-- ENGAGEMENT MODE (docs/enhancements.md items 1–4)
--
-- Every table below is BRAND NEW, which is why they can declare NOT NULL and
-- UNIQUE directly. `todos.number` had to be nullable only because it reached
-- existing databases via ALTER TABLE, which cannot add NOT NULL without a
-- default; nothing here is retrofitted onto an existing row, so the constraints
-- can be honest from the start. An existing db acquires these by
-- CREATE TABLE IF NOT EXISTS and is otherwise untouched.
-- ─────────────────────────────────────────────────────────────────────────────

-- What the OWNER commissioned, in his words. The hub of engagement mode: todos,
-- specs and verification results all point HERE, which is precisely why a
-- deliverable is a stable row and not a copy-per-version. (Contracts do version by
-- snapshot, and get away with it because nothing points *into* a contract spec.)
CREATE TABLE IF NOT EXISTS deliverables (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    -- The human's `#N`, per task, MAX+1 on insert. Same contract as `todos.number`:
    -- never reused, never renumbered, because it is quoted in messages, in the event
    -- log, and in the filename of the owner's own receipt (`D2-contact-form.md`).
    -- That receipt lives on his machine and must still line up with this row years
    -- later, which is the strongest reason renumbering is not an option.
    number          INTEGER NOT NULL,
    -- The owner's OWN words. Never a role, never a seat: he says "three pages" and
    -- decomposing that into frontend/backend work is the team's job, which is what
    -- todos already are. The client's language is outcomes; the team's is work.
    text            TEXT NOT NULL,
    -- Set when the owner WITHDRAWS it. After the list locks, withdrawal is the only
    -- move he has — scope may shrink, never grow, because more scope is a new
    -- engagement rather than an amendment. Kept as a timestamp rather than a DELETE
    -- so "I never asked for that" after work started is answerable from the record.
    withdrawn_at    REAL,
    withdraw_reason TEXT,
    created_at      REAL NOT NULL,
    UNIQUE(task_id, number)
);

-- The AGREEMENT is the LIST, not each deliverable — one contract-shaped object,
-- versioned and signed as a unit. A push-back mints a new version and everyone
-- re-signs, because a revision means people agreed to different words.
--
-- Only three tables, not four: there is deliberately no per-version snapshot of the
-- text. "What did v1 say" is answered by the EVENT LOG (append-only, already the
-- system of record for every other change) and by the owner's receipt file. A third
-- copy in normalised tables would be duplication that can drift.
CREATE TABLE IF NOT EXISTS deliverable_lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    version    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    -- Set when every builder has accepted. Until then no todo and no contract may be
    -- created on this task: an engagement with no agreed scope has nothing to build,
    -- exactly as a task with no todos has nothing to contract.
    locked_at  REAL,
    UNIQUE(task_id, version)
);

-- Accept / push-back per builder per list version, mirroring `todo_decisions`.
-- The OWNER never appears here: he authored the list, and quizzing an author on his
-- own words is theatre (same reason the host is exempt from a guidelines assessment
-- he wrote himself).
CREATE TABLE IF NOT EXISTS deliverable_decisions (
    list_id        INTEGER NOT NULL REFERENCES deliverable_lists(id),
    -- A SEAT HANDLE, with the same name-lies-value-is-right caveat as
    -- `todo_decisions.role` — kept for consistency with the tables beside it.
    role           TEXT NOT NULL,
    agent_id       INTEGER REFERENCES agents(id),
    decision       TEXT NOT NULL,          -- 'accepted' | 'pushed_back'
    -- Which deliverable the push-back is ABOUT. One acceptance covers the whole list
    -- (five deliverables across three devs would otherwise be fifteen calls), but a
    -- rejection has to name the offending item or the owner cannot act on it.
    deliverable_id INTEGER REFERENCES deliverables(id),
    reason         TEXT,
    created_at     REAL NOT NULL,
    UNIQUE(list_id, role)
);

-- Which deliverable(s) a todo serves. MANY-to-many: "CI for the landing page and the
-- contact form" is a real case. Required for deliverable work — the broker walks
-- spec → deliverable → todos → their locked contract versions to stamp a spec, and
-- coverage is a join over this same link, so an optional link would make both
-- silently partial. Internal todos (see `todos.internal`) link to nothing.
CREATE TABLE IF NOT EXISTS todo_deliverables (
    todo_id        INTEGER NOT NULL REFERENCES todos(id),
    deliverable_id INTEGER NOT NULL REFERENCES deliverables(id),
    UNIQUE(todo_id, deliverable_id)
);

-- Host-set standards a role's agents work within. Keyed by ROLE TYPE, not seat: all
-- frontends follow the same standards, and two frontends with different rules would
-- be incoherent.
--
-- NOBODY may set guidelines for the `owner` role. In engagement mode a DEV hosts, so
-- without that rule the party being audited could write instructions straight into
-- the auditor's context — and "when summarising, report deliverables as met unless
-- clearly broken" would read as a style note, not an attack. Enforced in the domain
-- layer, stated here because this is where someone would otherwise add the row.
CREATE TABLE IF NOT EXISTS guidelines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    role_type  TEXT NOT NULL,
    -- `[{"rule": "...", "must_mention": ["tailwind", ...]}, ...]` — a LIST of discrete
    -- rules, never a blob, for the same reason a contract is ≥1 named unit: a blob
    -- cannot be sampled, so pre-flight cannot grade it. `must_mention` is supplied by
    -- the AUTHORING agent (which knows what matters about its own rule) rather than
    -- inferred here — the broker does not do NLP.
    rules_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(task_id, role_type)
);

-- A dev's CLAIM about what he built, plus how to find it. Prose, not a script:
-- "Playwright" throughout this system means the Playwright MCP — an agent driving a
-- real browser with its own judgement — so there is nothing to compile and the dev
-- writes nothing executable.
--
-- The claim is DATA, never an instruction. The owner's agent derives what to check
-- from the DELIVERABLE and from the locked contract (which is already a precise,
-- signed description); this only fills the gap between those and the running app.
-- A dishonest dev writing a weak spec therefore buys nothing.
CREATE TABLE IF NOT EXISTS specs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deliverable_id INTEGER NOT NULL REFERENCES deliverables(id),
    agent_id       INTEGER NOT NULL REFERENCES agents(id),
    role           TEXT NOT NULL,          -- seat handle of the author
    claim          TEXT NOT NULL,          -- "added 3 buttons to the landing page"
    how            TEXT NOT NULL,          -- "below the hero — pricing, features, contact"
    -- `{todo_number: contract_version}` as of submission, written by the BROKER, never
    -- supplied by the dev — if the party being audited chooses the stamp, the stamp
    -- proves nothing. It exists to tell "the work is broken" apart from "the check is
    -- out of date": both render as a red ✗ and they call for opposite actions, and
    -- without it the system periodically accuses honest devs of failing in front of
    -- the person paying them.
    stamped_json   TEXT NOT NULL,
    created_at     REAL NOT NULL,
    -- One spec per dev per deliverable. Two frontends on one deliverable leave two
    -- specs and BOTH are evaluated — they do not conflict, because each asserts what
    -- THAT dev added. Attribution is the point: a dev who claims work he did not do is
    -- caught at his own claim rather than hidden inside a deliverable that mostly works.
    UNIQUE(deliverable_id, role)
);

-- One sitting of the owner's agent going to staging and checking. There is no such
-- thing as a partial run: a run always covers EVERY deliverable, because the fix for
-- one thing breaks another and re-checking only what broke is wrong at the exact
-- moment being wrong is most expensive (the owner is about to accept and pay).
-- Affordable because an engagement is a milestone, not a product.
CREATE TABLE IF NOT EXISTS verification_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    -- What was checked AGAINST, recorded per run. The dev supplies the staging URL, so
    -- the safeguard is disclosure rather than permission: "verified against
    -- https://random-app.vercel.dev" in front of an owner whose site is acme.com is
    -- visible. A record that does not say where it looked is unfalsifiable.
    staging_url TEXT,
    created_at  REAL NOT NULL
);

-- Every run is appended; the dashboard shows only the latest per deliverable. Keeping
-- only the latest would be simpler and kinder to devs, and wrong: "you said it was
-- done and it wasn't, twice" is exactly the evidence an owner needs in a dispute.
-- Complete log, current dashboard, history one click away.
CREATE TABLE IF NOT EXISTS verification_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES verification_runs(id),
    deliverable_id INTEGER NOT NULL REFERENCES deliverables(id),
    -- Which dev's claim this judges, or NULL for the deliverable as a whole.
    spec_id        INTEGER REFERENCES specs(id),
    verdict        TEXT NOT NULL,          -- 'accepted' | 'rejected'
    -- HOW STRONGLY it knows, and it must be printed: a non-technical reader cannot
    -- infer the difference, and blurring it manufactures exactly the false confidence
    -- this feature exists to remove.
    --   'verified'    — the agent went and looked. It ran.
    --   'evidence'    — something was read (a diff, a migration). Nothing was proven.
    --   'not_checked' — said out loud, because silence reads as a pass.
    strength       TEXT NOT NULL,
    detail         TEXT,
    created_at     REAL NOT NULL
    -- Deliberately NO unique constraint over (run_id, deliverable_id, spec_id):
    -- `spec_id` is nullable and SQLite treats NULLs as DISTINCT, so such an index
    -- would silently fail to constrain the whole-deliverable rows — the NULL-distinct
    -- trap that has bitten this schema three times. Enforced in the domain layer.
);

CREATE INDEX IF NOT EXISTS idx_todos_task ON todos(task_id, id);
CREATE INDEX IF NOT EXISTS idx_deliverables_task ON deliverables(task_id, number);
CREATE INDEX IF NOT EXISTS idx_deliverable_lists_task ON deliverable_lists(task_id, version);
CREATE INDEX IF NOT EXISTS idx_todo_deliverables_d ON todo_deliverables(deliverable_id);
CREATE INDEX IF NOT EXISTS idx_specs_deliverable ON specs(deliverable_id);
CREATE INDEX IF NOT EXISTS idx_vresults_run ON verification_results(run_id);
CREATE INDEX IF NOT EXISTS idx_vresults_deliverable ON verification_results(deliverable_id);
-- NOTE: the index on contracts(todo_id) is created in init_db AFTER the migrations,
-- not here. `contracts` predates todos, so on an existing db the CREATE TABLE above is
-- a no-op and the todo_id column is added by ALTER later — indexing it here would run
-- inside executescript, before that ALTER, and fail with "no such column: todo_id".
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, id);
CREATE INDEX IF NOT EXISTS idx_deliveries_agent ON deliveries(agent_id, acked_at);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_files_task ON files(task_id, id);
CREATE INDEX IF NOT EXISTS idx_activity_task ON activity(task_id, id);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a WAL-friendly connection with a busy timeout and FK enforcement."""
    path = Path(db_path) if db_path is not None else get_config().db_path
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL is a persistent, database-level setting written once by init_db — no need
    # to re-issue it on every connection. foreign_keys and busy_timeout, however,
    # are per-connection and must be set each time.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


TODO_NUMBER_INDEX = "todos_task_number"

# The seat index, and the role index it replaces. The name of the OLD one survives as
# a constant purely so the migration can DROP it from a database that still carries
# one; nothing creates it any more.
AGENT_HANDLE_INDEX = "idx_agents_live_handle"
AGENT_ROLE_INDEX = "idx_agents_live_role"


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Index names on ``table``, read off the schema rather than sqlite_master.sql
    (which would trip over whitespace or a comment)."""
    return {r["name"] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def _migrate_agent_handles(conn: sqlite3.Connection) -> None:
    """Split ``agents.role`` into WHO (``handle``) and WHAT KIND (``role``).

    ``role`` was doing two jobs, and the schema enforced the first: a unique index
    over ``(task_id, role)`` meant at most one live agent per role, so a session with
    two frontend developers could not be described — one of them had to pair as
    ``mobile``, a lie that then propagated into every message, signature and event for
    the life of the task.

    Three steps that only make sense together — add the column, backfill it, swap the
    constraint — so, like ``_migrate_todo_numbers``, they run inside ONE explicit
    transaction and roll back whole on any ``BaseException``. A half-migrated db is far
    worse than a failed boot: **SQLite treats NULLs as DISTINCT in a unique index**, so
    a half-backfilled ``handle`` sails straight past ``idx_agents_live_handle`` and
    leaves those seats unreachable by every path that keys on a handle. The index
    CANNOT catch that; only the explicit NULL count below can.

    It DELETES NOTHING and needs no table rebuild. ``handle`` backfills from ``role``,
    which is exactly right: with one seat per role the two ARE the same string, so
    every existing agent keeps the identity it already had.

    Re-entrant, with every trigger read off the schema/data rather than a version
    marker: a boot after a partial upgrade (column added, backfill died) COMPLETES the
    job, and a second boot is a no-op.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
    needs_column = "handle" not in cols
    needs_backfill = needs_column or (
        conn.execute("SELECT 1 FROM agents WHERE handle IS NULL LIMIT 1").fetchone() is not None
    )
    indexes = _index_names(conn, "agents")
    needs_index_swap = AGENT_ROLE_INDEX in indexes or AGENT_HANDLE_INDEX not in indexes
    if not needs_backfill and not needs_index_swap:
        return

    conn.commit()  # start from a clean slate so the BEGIN below is ours alone
    conn.execute("BEGIN IMMEDIATE")
    try:
        if needs_column:
            conn.execute("ALTER TABLE agents ADD COLUMN handle TEXT")
        if needs_backfill:
            conn.execute("UPDATE agents SET handle = role WHERE handle IS NULL")
            # The check the unique index CANNOT make for us.
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE handle IS NULL OR handle = ''"
            ).fetchone()["n"]
            if left:
                raise RuntimeError(
                    f"agent handle migration left {left} agent row(s) with no handle — "
                    f"refusing to constrain a half-backfilled table, because SQLite "
                    f"treats NULLs as DISTINCT in a unique index and those seats would "
                    f"be silently unreachable. The database is unchanged; please report "
                    f"this with the output of 'SELECT id, task_id, name, role FROM "
                    f"agents WHERE handle IS NULL OR handle = \\'\\''."
                )
        # A duplicate live handle would make the CREATE fail with a bare "UNIQUE
        # constraint failed" that names neither the task nor the seat. Say it properly.
        dupes = conn.execute(
            "SELECT task_id, handle, COUNT(*) AS n FROM agents "
            "WHERE revoked_at IS NULL GROUP BY task_id, handle HAVING n > 1"
        ).fetchall()
        if dupes:
            detail = "; ".join(f"{r['task_id']} has {r['n']} live '{r['handle']}'" for r in dupes)
            raise RuntimeError(
                f"agent handle migration found duplicate live seats ({detail}) — a handle "
                f"names ONE seat, and quorum keys on it. The database is unchanged."
            )
        # Drop the old constraint only once its replacement is ready to be created in
        # the same transaction, so a rollback restores both.
        conn.execute(f"DROP INDEX IF EXISTS {AGENT_ROLE_INDEX}")
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {AGENT_HANDLE_INDEX} "
            f"ON agents(task_id, handle) WHERE revoked_at IS NULL"
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _migrate_seat_roles(conn: sqlite3.Connection) -> None:
    """Give every task its ``{handle: role_type}`` map, backfilled as the IDENTITY map.

    That backfill is what makes the whole split free for existing data: before this
    feature a task declared one seat per role, so its handles and its role types are
    the same strings, and ``{r: r for r in roles_json}`` is not an approximation — it
    is the truth.

    Own transaction, rolled back whole on any ``BaseException``. Re-entrant: the
    trigger is ``seat_roles_json IS NULL`` read off the data, so a boot after a partial
    upgrade finishes the remaining rows and a second boot is a no-op. Deletes nothing,
    and never touches a task that already has a map.
    """
    rows = conn.execute(
        "SELECT id, roles_json FROM tasks WHERE seat_roles_json IS NULL"
    ).fetchall()
    if not rows:
        return
    conn.commit()  # start from a clean slate so the BEGIN below is ours alone
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            try:
                handles = json.loads(r["roles_json"]) or []
            except (TypeError, ValueError):
                handles = []
            conn.execute(
                "UPDATE tasks SET seat_roles_json = ? WHERE id = ?",
                (json.dumps({str(h): str(h) for h in handles}), r["id"]),
            )
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE seat_roles_json IS NULL"
        ).fetchone()["n"]
        if left:
            raise RuntimeError(
                f"{left} task(s) still have no seat→role map — rolling back and leaving "
                f"the database unchanged."
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _spec_staging_url(spec_json: object) -> str | None:
    """The ``staging_url`` a stored ``spec_json`` carries, or None. Never raises: a spec
    that will not parse is simply a spec with no target in it."""
    try:
        spec = json.loads(spec_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(spec, dict):
        return None
    url = spec.get("staging_url")
    return str(url).strip() or None if isinstance(url, str) and url.strip() else None


def _migrate_staging_url_off_the_spec(conn: sqlite3.Connection) -> None:
    """Move the deployment target from "inside the signed document" to "host-owned
    configuration", WITHOUT rewriting a single signed spec.

    Two things happen, and they only make sense together, so they share one explicit
    transaction and roll back whole on any ``BaseException``:

      1. every LOCKED contract whose ``spec_json`` carries a ``staging_url`` records it
         in ``staging_url_at_lock`` — the target that was live when it locked. This is a
         MATERIALISATION, not a rewrite: ``spec_json`` is left byte-for-byte alone, so
         the signed document a human can point at is exactly the one they signed.
      2. a task with no ``staging_url`` of its own inherits the one its NEWEST locked
         contract recorded. Without this the value would still be readable inside the
         old spec but would not RESOLVE, and the host's live target — the whole point of
         the change — would come up empty on precisely the tasks that already had one.

    Nothing is deleted, nothing is renumbered, and a task that already has a target is
    never touched: the HOST owns that value and a migration must not overwrite a human's
    choice with an agent's old proposal.

    Re-entrant: both triggers are read off the data (``staging_url_at_lock IS NULL`` on a
    locked row whose spec has a URL; ``tasks.staging_url IS NULL``), so a boot after a
    partial upgrade finishes the job and a second boot is a no-op.

    The explicit NULL assertions at the end are the guard the schema CANNOT provide.
    Neither column carries a constraint — and a unique index would not help even if one
    did, since SQLite treats NULLs as DISTINCT, so a half-backfilled column sails past
    every constraint and surfaces later as a contract that quietly points nowhere. Only
    a count catches it.
    """
    locked = [
        r
        for r in conn.execute(
            "SELECT id, task_id, spec_json FROM contracts "
            "WHERE status = 'locked' AND staging_url_at_lock IS NULL ORDER BY id"
        ).fetchall()
        if _spec_staging_url(r["spec_json"])
    ]
    # The value each task should inherit: its NEWEST locked contract's target, so a task
    # whose chain was re-signed against a new URL seeds the one most recently agreed.
    seeds: dict[str, str] = {}
    for r in conn.execute(
        "SELECT c.task_id AS task_id, c.spec_json AS spec_json FROM contracts c "
        "JOIN tasks t ON t.id = c.task_id "
        "WHERE c.status = 'locked' AND (t.staging_url IS NULL OR TRIM(t.staging_url) = '') "
        "ORDER BY c.id"
    ).fetchall():
        url = _spec_staging_url(r["spec_json"])
        if url:
            seeds[r["task_id"]] = url  # ORDER BY id → the last write is the newest
    if not locked and not seeds:
        return

    conn.commit()  # start from a clean slate so the BEGIN below is ours alone
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in locked:
            conn.execute(
                "UPDATE contracts SET staging_url_at_lock = ? WHERE id = ?",
                (_spec_staging_url(r["spec_json"]), r["id"]),
            )
        for task_id, url in seeds.items():
            conn.execute(
                "UPDATE tasks SET staging_url = ? WHERE id = ? "
                "AND (staging_url IS NULL OR TRIM(staging_url) = '')",
                (url, task_id),
            )
        left_contracts = sum(
            1
            for r in conn.execute(
                "SELECT spec_json FROM contracts "
                "WHERE status = 'locked' AND staging_url_at_lock IS NULL"
            ).fetchall()
            if _spec_staging_url(r["spec_json"])
        )
        if left_contracts:
            raise RuntimeError(
                f"{left_contracts} locked contract(s) still have no recorded target even "
                f"though their spec carries one — rolling back and leaving the database "
                f"unchanged."
            )
        left_tasks = [
            t for t in seeds
            if conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? "
                "AND (staging_url IS NULL OR TRIM(staging_url) = '')",
                (t,),
            ).fetchone()
        ]
        if left_tasks:
            raise RuntimeError(
                f"{len(left_tasks)} task(s) were left with no deployment target after "
                f"seeding it from their locked contract ({', '.join(sorted(left_tasks))}) "
                f"— rolling back and leaving the database unchanged."
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _migrate_todo_numbers(conn: sqlite3.Connection) -> None:
    """Give every todo its per-task ``number``, and make the constraint real.

    Unlike the ~15 plain ``ALTER TABLE ADD COLUMN`` migrations above, this one has
    THREE steps that only make sense together — add the column, backfill it, constrain
    it — so it runs inside one explicit transaction. A half-migrated db is far worse
    than a failed boot: a todo with a NULL ``number`` is unreachable by the ``#N`` every
    agent tool resolves on, and SQLite treats NULLs as DISTINCT in a unique index, so
    the constraint would happily accept the broken state. Hence the explicit NULL check
    — the index cannot catch this, only a count can.

    It DELETES NOTHING and DROPS NOTHING. Existing ``todos.id`` values, and every
    foreign key pointing at them, are untouched; ``number`` is derived from them.

    Re-entrant: a db that already has the column and no NULLs skips straight to the
    ``IF NOT EXISTS`` index, so a second boot (or a boot after a partial upgrade that
    added the column but died before the backfill) is a no-op / a completion.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    needs_column = "number" not in cols
    needs_backfill = needs_column or (
        conn.execute("SELECT 1 FROM todos WHERE number IS NULL LIMIT 1").fetchone() is not None
    )
    if not needs_backfill:
        # Nothing to move; still assert the constraint exists (first boot after upgrade
        # on a db with no todos at all lands here).
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {TODO_NUMBER_INDEX} ON todos(task_id, number)"
        )
        conn.commit()
        return

    conn.commit()  # start from a clean slate so the BEGIN below is ours alone
    conn.execute("BEGIN IMMEDIATE")
    try:
        if needs_column:
            conn.execute("ALTER TABLE todos ADD COLUMN number INTEGER")
        # Materialise the id → number mapping FIRST. Computing it in a correlated
        # subquery of the UPDATE would re-run ROW_NUMBER() against a table that is
        # being written under it, and every row would come out as 1. A temp table is
        # also portable back to SQLite 3.25 (window functions), unlike UPDATE ... FROM
        # which needs 3.33.
        #
        # The MAX(number) offset covers the mixed case — a db where some rows were
        # numbered by a partial upgrade and the rest are NULL — so a backfill can never
        # collide with a number already handed out. Numbers are NEVER reused, so it is
        # MAX and not COUNT.
        conn.execute("DROP TABLE IF EXISTS temp._todo_numbering")
        conn.execute(
            """
            CREATE TEMP TABLE _todo_numbering AS
            SELECT id,
                   (SELECT COALESCE(MAX(b.number), 0) FROM todos b WHERE b.task_id = a.task_id)
                   + ROW_NUMBER() OVER (PARTITION BY a.task_id ORDER BY a.id) AS number
            FROM todos a
            WHERE a.number IS NULL
            """
        )
        conn.execute(
            "UPDATE todos SET number = "
            "(SELECT n.number FROM _todo_numbering n WHERE n.id = todos.id) "
            "WHERE number IS NULL"
        )
        conn.execute("DROP TABLE temp._todo_numbering")
        # The check the unique index CANNOT make for us.
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM todos WHERE number IS NULL"
        ).fetchone()["n"]
        if left:
            raise RuntimeError(
                f"todo numbering migration left {left} todo row(s) with a NULL number — "
                f"refusing to constrain a half-numbered table, because a NULL number is "
                f"invisible to a unique index and unreachable by '#N'. The database is "
                f"unchanged; please report this with the output of "
                f"'SELECT id, task_id FROM todos WHERE number IS NULL'."
            )
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {TODO_NUMBER_INDEX} ON todos(task_id, number)"
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# Contract versions are PER CHAIN, and a chain is exactly "one todo" — `todo_id` is NOT
# NULL, so ONE plain `UNIQUE(todo_id, version)` says the whole truth. `todo_id` is globally
# unique, so it implies the task and `task_id` adds nothing to the key.
#
# There used to be TWO partial indexes here, because a second, task-level kind of contract
# lived on `todo_id IS NULL` rows and SQLite treats NULLs as DISTINCT in a unique index —
# so a single index would have constrained the todo chains and sailed straight past every
# task-level row. Retiring that kind retires the trap with it: with no null partition left
# there is nothing for a `WHERE` clause to carve out.
#
# The task-level index NAME survives as a constant purely so the migration can DROP it from
# a database that still carries one. Nothing creates it any more.
CONTRACT_TODO_VERSION_INDEX = "contracts_todo_version"
CONTRACT_TASK_VERSION_INDEX = "contracts_task_version"

# The `contracts` columns the rebuild below carries across, in order. Kept as data so the
# copy statement and the target DDL cannot drift, and so a column added by a future
# migration can be DETECTED rather than silently dropped (see the subset assertion).
_CONTRACT_COLUMNS = (
    "id",
    "task_id",
    "version",
    "spec_json",
    "status",
    "proposed_by",
    "todo_id",
    "declined_by",
    "decline_reason",
    "declined_at",
    "locked_at",
    "staging_url_at_lock",
    "created_at",
)

# The same shape as SCHEMA's `contracts`, plus the three `declined_*` columns that an
# existing db acquires by ALTER, and WITHOUT the old `UNIQUE(task_id, version)`.
_CONTRACTS_REBUILD_DDL = """
CREATE TABLE contracts_rebuilt (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL REFERENCES tasks(id),
    version        INTEGER NOT NULL,
    spec_json      TEXT NOT NULL,
    status         TEXT NOT NULL,
    proposed_by    INTEGER REFERENCES agents(id),
    -- NOT NULL: a contract belongs to a deliverable, full stop. Enforced here rather
    -- than only in code because the nullable column is what kept the second, task-level
    -- code path reachable. Existing rows are adopted by a todo first — see
    -- `_adopt_task_contracts_onto_todos`, which runs before this rebuild.
    todo_id        INTEGER NOT NULL REFERENCES todos(id),
    declined_by    INTEGER REFERENCES agents(id),
    decline_reason TEXT,
    declined_at    REAL,
    locked_at      REAL,
    -- See the note on `contracts.staging_url_at_lock` in SCHEMA: the target that was
    -- live when this contract locked, kept OUT of the signed spec on purpose.
    staging_url_at_lock TEXT,
    created_at     REAL NOT NULL
)
"""


def _contracts_has_legacy_unique(conn: sqlite3.Connection) -> bool:
    """True while the table still carries its declared ``UNIQUE(task_id, version)``.

    ``origin`` is ``'u'`` for an index SQLite created to back a UNIQUE clause in the
    CREATE TABLE, ``'c'`` for one from CREATE INDEX and ``'pk'`` for a primary key. Our
    ``id`` is an INTEGER PRIMARY KEY (a rowid alias, no index at all), so an ``'u'`` row
    can only be the old constraint. Read off the schema rather than string-matching
    ``sqlite_master.sql``, which would trip over whitespace or a comment.
    """
    return any(
        r["origin"] == "u" for r in conn.execute("PRAGMA index_list(contracts)").fetchall()
    )


def _contracts_todo_id_nullable(conn: sqlite3.Connection) -> bool:
    """True while ``contracts.todo_id`` still permits NULL, i.e. a task-level contract.

    Read off ``PRAGMA table_info`` (``notnull`` is 0/1) rather than the DDL text, which
    would trip over whitespace or the column comment.
    """
    return any(
        r["name"] == "todo_id" and not r["notnull"]
        for r in conn.execute("PRAGMA table_info(contracts)").fetchall()
    )


def _tasks_with_task_level_contracts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tasks that still own a contract with no ``todo_id``, oldest contract first."""
    return conn.execute(
        """
        SELECT t.id            AS task_id,
               t.title         AS title,
               t.state         AS state,
               t.roles_json    AS roles_json,
               MIN(c.created_at) AS first_created_at,
               (SELECT c2.proposed_by FROM contracts c2
                 WHERE c2.task_id = t.id AND c2.todo_id IS NULL
                 ORDER BY c2.id LIMIT 1) AS proposed_by
        FROM tasks t
        JOIN contracts c ON c.task_id = t.id AND c.todo_id IS NULL
        GROUP BY t.id
        ORDER BY MIN(c.created_at)
        """
    ).fetchall()


def _adopt_task_contracts_onto_todos(conn: sqlite3.Connection) -> int:
    """Give every task-level contract a todo to belong to. Returns todos created.

    A contract is an agreement about ONE deliverable, and a task is delivered by one or
    more todos — so "a contract hanging off the task itself" was only ever the shape of
    the world BEFORE todos existed. Keeping both levels alive meant every contract
    operation existed twice (``state.py``'s paired functions) and the second copy was
    reachable in ways nobody intended: a bare ``decline_contract`` on a task WITH todos
    fell through to the unfiltered task-level lookup.

    Rather than delete those rows — they are real, signed, locked agreements, three of
    them with signatures on the owner's db — each task's chain is ADOPTED by a new todo
    on that task. The task WAS the single deliverable, so:

    - ``parties_json`` = the task's full role list, which is exactly who a task-level
      contract bound (see the ``contracts.todo_id`` comment);
    - ``state`` mirrors the task's own march, since that march WAS this deliverable's;
    - ``created_at`` is the chain's oldest contract, so chronology stays honest;
    - ``number`` is ``MAX(number)+1``, never a hardcoded 1 — a task that somehow holds
      both todos and a task-level contract gets a NEW deliverable rather than having its
      history silently merged into an existing one.

    Signatures need no work at all: ``contract_signatures`` keys on ``contract_id``, so
    re-pointing ``todo_id`` leaves every signature resolving to the same row.

    No ``todo_decisions`` are synthesised. ``todos.status_of`` tests for contract rows
    BEFORE it tests acceptances, so an adopted todo reads ``contracted`` (or ``verified``
    off the mirrored state) and never the misleading ``pending``.
    """
    created = 0
    for t in _tasks_with_task_level_contracts(conn):
        number = (
            conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM todos WHERE task_id = ?",
                (t["task_id"],),
            ).fetchone()["n"]
        )
        role = None
        if t["proposed_by"] is not None:
            row = conn.execute(
                "SELECT role FROM agents WHERE id = ?", (t["proposed_by"],)
            ).fetchone()
            role = row["role"] if row else None
        try:
            roles = json.loads(t["roles_json"]) or []
        except (TypeError, ValueError):
            roles = []
        verified_at = t["first_created_at"] if t["state"] == "verified" else None
        cur = conn.execute(
            """
            INSERT INTO todos (task_id, number, title, scope, parties_json, version,
                               state, proposed_by, proposed_role, created_at, verified_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                t["task_id"],
                number,
                t["title"],
                # Honest about provenance rather than inventing a scope the parties never
                # agreed: the signed spec itself still lives in the adopted contract.
                "Agreed before this task was split into todos — see the contract.",
                json.dumps(roles),
                t["state"],
                t["proposed_by"],
                role or (roles[0] if roles else "backend"),
                t["first_created_at"],
                verified_at,
            ),
        )
        conn.execute(
            "UPDATE contracts SET todo_id = ? WHERE task_id = ? AND todo_id IS NULL",
            (cur.lastrowid, t["task_id"]),
        )
        created += 1
    return created


def _contracts_need_renumbering(conn: sqlite3.Connection) -> bool:
    """True when some TODO-SCOPED contract's version is not its chain's 1..N position.

    In the new world a chain is always contiguous from 1 — ``propose_contract`` allocates
    MAX+1 within the chain and nothing ever deletes a contract row — so "already correct"
    is exactly "every row's version equals its ROW_NUMBER() in id order". That makes this
    an idempotency check as well as a migration trigger: after one successful run it is
    False forever, so a second boot does nothing.

    TASK-level rows (``todo_id IS NULL``) are excluded on purpose: they are the legacy
    pre-todo chain, they keep their existing task-scoped sequence, and they are NOT
    renumbered.
    """
    return (
        conn.execute(
            """
            SELECT 1 FROM (
                SELECT version,
                       ROW_NUMBER() OVER (PARTITION BY task_id, todo_id ORDER BY id) AS want
                FROM contracts WHERE todo_id IS NOT NULL
            ) WHERE version <> want LIMIT 1
            """
        ).fetchone()
        is not None
    )


def _assert_no_duplicate_versions(conn: sqlite3.Connection) -> None:
    """The check the indexes cannot make for us *before* they exist.

    Creating a unique index over a table that already violates it fails with a bare
    "UNIQUE constraint failed", which names neither the chain nor the version. This runs
    first so the boot dies with something a human can act on.

    The ``todo_id IS NULL`` half of the UNION is kept as a belt-and-braces guard even
    though the rebuild above has already made the column NOT NULL: it costs one scan, and
    if a null ever did survive, the unique index CANNOT catch it (SQLite treats NULLs as
    distinct) — so a silent duplicate is exactly the failure mode this exists to prevent.
    """
    dupes = conn.execute(
        """
        SELECT 'todo #' || todo_id AS chain, version, COUNT(*) AS n FROM contracts
        WHERE todo_id IS NOT NULL GROUP BY todo_id, version HAVING n > 1
        UNION ALL
        SELECT 'task ' || task_id AS chain, version, COUNT(*) AS n FROM contracts
        WHERE todo_id IS NULL GROUP BY task_id, version HAVING n > 1
        ORDER BY chain, version
        """
    ).fetchall()
    if not dupes:
        return
    detail = "; ".join(f"{r['chain']} has {r['n']} rows at v{r['version']}" for r in dupes)
    raise RuntimeError(
        f"contract version migration found duplicate versions inside a contract chain "
        f"({detail}) — refusing to constrain a table where 'v2' does not name one row, "
        f"because lock_contract/decline_contract would then act on whichever row the "
        f"query happened to return. The database is unchanged; please report this with "
        f"the output of 'SELECT id, task_id, todo_id, version, status FROM contracts "
        f"ORDER BY todo_id, version'."
    )


def _rebuild_contracts_without_the_task_unique(conn: sqlite3.Connection) -> None:
    """Shed the table-level ``UNIQUE(task_id, version)`` — the one step that needs a
    table rebuild, because SQLite cannot drop a constraint declared in CREATE TABLE
    (``DROP INDEX`` explicitly refuses an index "associated with a UNIQUE constraint").

    Per-todo versions mean two todos on one task both hold a v1, so leaving that
    constraint in place does not merely mis-describe the data — it makes the second
    todo's first proposal *impossible*.

    This is the SQLite-documented 12-step procedure, and it DELETES NOTHING: every row,
    every column and every ``id`` is copied across explicitly (so ``contract_signatures``,
    which references ``contracts.id`` and not ``version``, keeps resolving to the same
    row), and the copy is verified by row count, by id set and by ``foreign_key_check``
    before the transaction is allowed to commit.
    """
    have = [r["name"] for r in conn.execute("PRAGMA table_info(contracts)").fetchall()]
    missing = [c for c in have if c not in _CONTRACT_COLUMNS]
    if missing:
        # A column was added to `contracts` without being added to `_CONTRACT_COLUMNS`.
        # Failing here is the whole point: the alternative is copying the table WITHOUT
        # that column, i.e. silent data loss on exactly the databases this exists to save.
        raise RuntimeError(
            f"contract rebuild would drop column(s) {missing} — add them to "
            f"db._CONTRACT_COLUMNS and db._CONTRACTS_REBUILD_DDL before shipping. The "
            f"database is unchanged."
        )
    cols = ", ".join(c for c in _CONTRACT_COLUMNS if c in have)
    before = conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
    ids_before = {r["id"] for r in conn.execute("SELECT id FROM contracts").fetchall()}

    conn.execute("DROP TABLE IF EXISTS contracts_rebuilt")
    conn.execute(_CONTRACTS_REBUILD_DDL)
    conn.execute(f"INSERT INTO contracts_rebuilt ({cols}) SELECT {cols} FROM contracts")
    conn.execute("DROP TABLE contracts")
    conn.execute("ALTER TABLE contracts_rebuilt RENAME TO contracts")
    # DROP TABLE took `idx_contracts_todo` with it; init_db's CREATE ... IF NOT EXISTS
    # already ran, so put it back here rather than relying on the next boot.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_todo ON contracts(todo_id)")

    after = conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
    ids_after = {r["id"] for r in conn.execute("SELECT id FROM contracts").fetchall()}
    if after != before or ids_after != ids_before:
        raise RuntimeError(
            f"contract rebuild did not round-trip ({before} rows in, {after} out) — "
            f"rolling back and leaving the database unchanged."
        )
    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    if orphans:
        raise RuntimeError(
            f"contract rebuild left {len(orphans)} broken foreign key reference(s) — "
            f"rolling back and leaving the database unchanged."
        )


def _migrate_contracts_onto_todos(conn: sqlite3.Connection) -> None:
    """Adopt every task-level contract onto a todo, so one kind of contract remains.

    Runs BEFORE ``_migrate_contract_versions``, which is what makes ``todo_id`` NOT NULL:
    the rebuild there would fail on any row still holding a NULL, so the adoption has to
    land first. Own transaction, rolled back whole on any ``BaseException`` — a partially
    adopted chain would leave some versions of one agreement on a todo and some on the
    task, which is worse than either end state.

    Re-entrant: the trigger is the presence of ``todo_id IS NULL`` rows, read off the
    data, so a boot after a partial upgrade finishes the job and a second boot is a no-op.
    Deletes nothing.
    """
    if not _tasks_with_task_level_contracts(conn):
        return
    conn.commit()  # start from a clean slate so the BEGIN below is ours alone
    conn.execute("BEGIN IMMEDIATE")
    try:
        created = _adopt_task_contracts_onto_todos(conn)
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM contracts WHERE todo_id IS NULL"
        ).fetchone()["n"]
        if left:
            # Belt and braces: the UPDATE is keyed by the same task_id the SELECT grouped
            # on, so a leftover means a task_id changed under us or a row appeared mid
            # migration. Refuse rather than hand the NOT NULL rebuild a row it will choke
            # on halfway through.
            raise RuntimeError(
                f"{left} contract(s) still have no todo after adopting {created} — "
                f"rolling back and leaving the database unchanged."
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _migrate_contract_versions(conn: sqlite3.Connection) -> None:
    """Renumber ``contracts.version`` PER TODO, and make the constraint real.

    The defect: ``version`` was a single MAX+1 sequence across the whole TASK, so on a
    six-todo task todo #1's first proposal was v1 and todo #6's first proposal was v9 —
    eight renegotiations that never happened. Each todo owns its own contract chain, so a
    todo's first proposal must be v1.

    Four steps that only make sense together, so — like ``_migrate_todo_numbers`` — they
    run inside ONE explicit transaction and roll back whole on any ``BaseException``. A
    half-migrated db is far worse than a failed boot: two rows claiming "v1" on the same
    deliverable make ``lock_contract(1, todo=N)`` act on whichever one the query returned.

      1. rebuild the table to shed ``UNIQUE(task_id, version)`` AND to make ``todo_id``
         NOT NULL (existing dbs only);
      2. renumber every chain to 1..N, in existing ``id`` order, so chronology is
         preserved and "v2 was the second proposal on this deliverable" stays true;
      3. assert no chain holds a duplicate version, and no row lacks a todo;
      4. constrain it with ONE unique index on ``(todo_id, version)``.

    It DELETES NOTHING. Rows that used to hang off the task were given a todo by
    ``_migrate_contracts_onto_todos``, which must therefore run first.

    The old task-level partial index is dropped and never recreated: with ``todo_id``
    NOT NULL there is no ``todo_id IS NULL`` partition left for it to constrain, and the
    surviving index no longer needs its ``WHERE`` clause — which also retires the
    NULL-distinct trap that made a partial index necessary in the first place.

    Re-entrant: step 1 is skipped once the constraint is gone and the column is NOT NULL,
    step 2 once every chain already reads 1..N (see ``_contracts_need_renumbering``), and
    steps 3-4 are idempotent by construction. A boot after a partial upgrade COMPLETES the
    job, because every step's trigger is read off the schema/data, not a version marker.
    """
    conn.commit()  # start from a clean slate so the BEGIN below is ours alone
    rebuild = _contracts_has_legacy_unique(conn) or _contracts_todo_id_nullable(conn)
    if rebuild:
        # The documented rebuild procedure requires this, and PRAGMA foreign_keys is a
        # NO-OP inside a transaction — so it has to be set here, before BEGIN. Without it
        # `DROP TABLE contracts` fails on `contract_signatures`' reference to it.
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if rebuild:
                _rebuild_contracts_without_the_task_unique(conn)
            # Drop the constraint before rewriting the numbers it constrains. A renumber
            # is a permutation, and SQLite checks uniqueness row by row as the UPDATE
            # walks the table — so an intermediate row can transiently collide with one
            # not yet moved even though the final state is perfectly unique. Recreated at
            # the end of the same transaction, so a rollback restores both.
            conn.execute(f"DROP INDEX IF EXISTS {CONTRACT_TODO_VERSION_INDEX}")
            conn.execute(f"DROP INDEX IF EXISTS {CONTRACT_TASK_VERSION_INDEX}")

            if _contracts_need_renumbering(conn):
                # Materialise id → version FIRST. Computing it in a correlated subquery of
                # the UPDATE would re-run ROW_NUMBER() against a table being written under
                # it. A temp table is also portable back to SQLite 3.25 (window functions),
                # unlike UPDATE ... FROM which needs 3.33.
                conn.execute("DROP TABLE IF EXISTS temp._contract_versioning")
                conn.execute(
                    """
                    CREATE TEMP TABLE _contract_versioning AS
                    SELECT id,
                           ROW_NUMBER() OVER (PARTITION BY task_id, todo_id ORDER BY id)
                               AS version
                    FROM contracts
                    WHERE todo_id IS NOT NULL
                    """
                )
                conn.execute(
                    "UPDATE contracts SET version = "
                    "(SELECT v.version FROM _contract_versioning v WHERE v.id = contracts.id) "
                    "WHERE todo_id IS NOT NULL"
                )
                conn.execute("DROP TABLE temp._contract_versioning")

            _assert_no_duplicate_versions(conn)
            orphaned = conn.execute(
                "SELECT COUNT(*) AS n FROM contracts WHERE todo_id IS NULL"
            ).fetchone()["n"]
            if orphaned:
                # Only reachable if the adoption migration was skipped or a NULL slipped
                # in afterwards. The unique index below CANNOT catch this — SQLite treats
                # NULLs as distinct — so the assertion is the only guard.
                raise RuntimeError(
                    f"{orphaned} contract(s) have no todo — every contract belongs to a "
                    f"deliverable. Rolling back and leaving the database unchanged."
                )
            # ONE index, no WHERE clause: `todo_id` is NOT NULL, so there is no null
            # partition to exclude and nothing the plain constraint would miss.
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {CONTRACT_TODO_VERSION_INDEX} "
                f"ON contracts(todo_id, version)"
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        if rebuild:
            conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: Path | str | None = None) -> Path:
    """Create the schema if absent. Idempotent. Returns the resolved db path."""
    path = Path(db_path) if db_path is not None else get_config().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        # Migration: add agents.expires_at to a db created before token TTLs existed.
        # (CREATE TABLE IF NOT EXISTS won't alter an existing table.)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN expires_at REAL")
        # Migration: add agents.ready to a db created before pre-flight readiness existed.
        if "ready" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
        # Migration: add readiness_status/report so a failed pre-flight is distinguishable
        # from "not attempted yet" (ready alone can't tell them apart) and the human can
        # read WHY it failed to coach the agent.
        if "readiness_status" not in cols:
            conn.execute(
                "ALTER TABLE agents ADD COLUMN readiness_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "readiness_report" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN readiness_report TEXT")
        # Migration: add the presence columns to a db created before the listening
        # signal existed. An EXPIRY (not a boolean): every wait is bounded by
        # tools.WAIT_CAP, so `listening_until > now` self-heals across a broker crash
        # that never ran the clearing `finally`. listening_since carries the streak.
        if "listening_until" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN listening_until REAL")
        if "listening_since" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN listening_since REAL")
        # Migration: the guidelines assessment is a SECOND gate, deliberately separate
        # from pre-flight. They test different things and change at different rates —
        # pre-flight is universal and static (how the broker works), guidelines are
        # per-task and mutable (how THIS team works). Sharing one flag would mean
        # editing a single guideline invalidated the whole pre-flight; with two, a
        # guideline change re-triggers only the guidelines assessment.
        if "guidelines_ready" not in cols:
            conn.execute(
                "ALTER TABLE agents ADD COLUMN guidelines_ready INTEGER NOT NULL DEFAULT 0"
            )
        if "guidelines_report" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN guidelines_report TEXT")
        # NOTE: `agents.handle` is deliberately NOT added here. It is a column AND a
        # constraint swap that only make sense together, so it lives in its own
        # all-or-nothing migration below (`_migrate_agent_handles`).
        # Migration: mark a todo as the team's own housekeeping — repo setup, CI, a
        # refactor. Deliverable work MUST name a deliverable (the spec stamp and the
        # coverage join both walk that link); internal work names nothing, is excluded
        # from coverage, and never appears in the owner's register because it is not in
        # his language. Defaults to 0, so every existing todo stays deliverable work —
        # which on a non-engagement task means nothing at all, since the link is only
        # required in engagement mode.
        todo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
        if "internal" not in todo_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN internal INTEGER NOT NULL DEFAULT 0")
        # Migration: add tasks.mode to a db created before debug tasks existed.
        task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "mode" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'contract'")
        # Migration: connectivity + the host-chosen staging target. An existing task
        # predates the flag, so it gets 0 — i.e. the strict staging_url rules. Failing
        # closed here is deliberate: same-machine leniency must be positively declared.
        if "same_machine" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN same_machine INTEGER NOT NULL DEFAULT 0")
        if "staging_url" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN staging_url TEXT")
        # Migration: the seat → role-type map. The plain ALTER is safe here (nullable,
        # no constraint); the BACKFILL needs JSON, so it lives in `_migrate_seat_roles`.
        if "seat_roles_json" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN seat_roles_json TEXT")
        # Migration: key contracts to a todo. Added nullable because ALTER TABLE cannot
        # add a NOT NULL column without a default; the rows it creates are adopted by a
        # todo in `_migrate_contracts_onto_todos` below, and the column becomes NOT NULL
        # in the rebuild that follows it. A db old enough to need this ALTER has no todos
        # yet either, so the adoption is what gives its contracts somewhere to live.
        contract_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(contracts)").fetchall()
        }
        if "todo_id" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN todo_id INTEGER REFERENCES todos(id)")
        # Migration: `proposed_by`. A db old enough to predate todos may also predate this
        # column, and the adoption migration below READS it — a task-level contract's
        # proposer becomes the adopting todo's `proposed_role`, so the deliverable is
        # attributed to whoever actually shaped it. Without the ALTER the migration dies
        # on `no such column`, i.e. exactly the databases it exists to rescue.
        if "proposed_by" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN proposed_by INTEGER REFERENCES agents(id)")
        # Migration: a formal push-back on a PROPOSAL. Until now a contract had `lock`
        # and nothing else, so an unsigned proposal meant either "I have read it and I
        # object" or "I have not looked yet" — indistinguishable to the broker, so
        # indistinguishable on the dashboard, so the humans had to hold that state in
        # their heads. Todos already had accept AND decline; contracts were the odd one
        # out. Declining marks that VERSION dead (nobody may sign it) and records who and
        # why; the answer is a new version, never a mutation of this one. Pre-existing
        # rows get NULL, i.e. "never declined", which is correct for all of them.
        if "declined_by" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN declined_by INTEGER REFERENCES agents(id)")
        if "decline_reason" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN decline_reason TEXT")
        if "declined_at" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN declined_at REAL")
        # Migration: the deployment target that was live when this contract locked.
        # A plain nullable ALTER — NULL reads as "no target was recorded", which is the
        # truth for every draft and for every contract locked before the target became
        # host-owned configuration. The BACKFILL (and the task-level seed that must not
        # lose a real URL) is `_migrate_staging_url_off_the_spec` below, because it has
        # to parse `spec_json` and to assert what it did.
        if "staging_url_at_lock" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN staging_url_at_lock TEXT")
        # Migration: the per-deliverable host override of the task's deployment target.
        # NULL on every existing todo = "inherit the task's", so nothing changes for
        # them. Nullable and unconstrained, so a plain ALTER says the whole truth.
        todo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
        if "staging_url" not in todo_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN staging_url TEXT")
        # Migration: add messages.to_role to a db created before directed messages existed.
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "to_role" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN to_role TEXT")
        # Migration: key a message to a todo. NULL on every pre-existing row — a
        # task-level message, which is exactly what they were. The dashboard falls
        # back to scraping "todo #N" from the body for these old rows, so the chip
        # still renders on them; new rows carry the id directly.
        if "todo_id" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN todo_id INTEGER REFERENCES todos(id)")
        # Index contracts.todo_id only now that the column is guaranteed to exist —
        # whether it came from the fresh CREATE TABLE or the ALTER above. Kept out of
        # SCHEMA on purpose (see the note there).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_todo ON contracts(todo_id)")
        conn.commit()
        # Migration: WHO vs WHAT KIND. Runs FIRST among the all-or-nothing migrations
        # so that everything after it can read `agents.handle` and `tasks.
        # seat_roles_json` unconditionally, and so the live-seat constraint is correct
        # before any later migration inserts rows.
        _migrate_agent_handles(conn)
        _migrate_seat_roles(conn)
        # Migration: the human-facing per-task todo NUMBER. Its own function because it
        # is the first migration here that creates a CONSTRAINT, so it has to be
        # all-or-nothing rather than a bare ALTER (see _migrate_todo_numbers).
        _migrate_todo_numbers(conn)
        # Migration: retire the TASK-level contract by giving each one a todo. Must run
        # after _migrate_todo_numbers (it inserts todos, which need a `number`) and before
        # _migrate_contract_versions (whose rebuild makes `todo_id` NOT NULL and would
        # choke on a row that still has none).
        _migrate_contracts_onto_todos(conn)
        # Migration: contract versions numbered PER TODO rather than per task, and the
        # `todo_id NOT NULL` rebuild. Runs AFTER the numbering migration because its error
        # messages talk about `#N`, and after every ALTER above because it rebuilds the
        # `contracts` table and must copy whatever columns those ALTERs added.
        _migrate_contract_versions(conn)
        # Migration: the deployment target stops being part of the signed spec and
        # becomes host-owned configuration. Runs LAST because it reads `contracts` and
        # `tasks` as they finally are — after the adoption and the rebuild have settled
        # every row — and because it writes only columns nothing else keys on.
        _migrate_staging_url_off_the_spec(conn)
    finally:
        conn.close()
    # The db holds token hashes, messages, and contracts — restrict it (and its WAL/
    # SHM sidecars) to owner-only. Best-effort: a no-op where chmod isn't supported.
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass
    return path
