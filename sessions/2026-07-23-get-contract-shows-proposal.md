# Session handoff — get_contract shows the proposed contract (2026-07-23)

## The problem (recurring, hit live twice)
Agents kept getting stuck at the same wall: the backend proposes a contract and signs
its side, then the consumer (frontend/mobile) tries to "review it with `get_contract`"
before signing — and gets `exists: false`. Because `get_contract` returned **only the
LOCKED contract**, a proposed-but-unlocked contract was invisible to everyone until it
locked… which it can't do until the consumer signs. Chicken-and-egg. Agents burned turns
re-proposing / re-pulling, thinking it was a race or a registration bug. It is neither —
it was the documented behavior fighting the natural review-then-sign instinct.

## Decision
Make `get_contract` the single source of truth at BOTH stages (proposed and locked)
instead of teaching agents to work around a locked-only tool. Preserve the security
reason the tool was locked-only in the first place — the `staging_url` must be signed
before it's fetchable (rule 2 / SPEC §5 rule 5) — by **withholding `staging_url` until
lock**. The shape is reviewable pre-lock; the URL is not.

## What shipped (branch `fix/get-contract-shows-proposal`, off `main`)
Three commits, not pushed yet (main is PR-protected — needs `tooney92` account + PR):

- `f2a80cc` **fix(contract)** — the core change:
  - `src/sys_buddy/state.py`
    - New helpers `_newest_contract(conn, task_id)` (highest-version row, any status)
      and `_signatures_for(conn, contract_id)`.
    - Rewrote `get_contract`: returns the newest contract. If `status == 'locked'` →
      full contract incl. `staging_url` (as before) + `signatures`. If draft/proposed →
      `status: "proposed"`, `locked: False`, `spec` = shape **with `staging_url` stripped**,
      `staging_url: None`, plus `signatures` + `awaiting` (roles not yet signed) + a `note`.
      If a v2 draft is in flight while an older v1 is still locked, adds
      `locked_version_in_force`.
    - Updated the `contract_proposal` message text propose posts (it now truthfully says
      "review with get_contract … staging_url appears once every role signs").
  - `src/sys_buddy/tools.py` — both `get_contract` tool docstrings (remote + local)
    updated to describe proposed-or-locked behavior.
  - `src/sys_buddy/rules.py` — contract-flow section rewritten to the "review in
    get_contract, then lock_contract" model (removed the old sign-then-see/locked-only
    wording from earlier in the day).
  - `src/sys_buddy/onboarding.py` — backend + consumer planning blocks reconciled to the
    new behavior.
  - `src/sys_buddy/readiness.py` — the `visibility` question + `_grade_visibility` now
    test "review via get_contract + staging_url withheld until lock" (was "locked-only +
    see it in the message").
  - Tests: `tests/test_state.py` (+3: proposal visible pre-lock, staging_url stripped,
    partial-signature status; renamed the old absent-before-lock test) and
    `tests/test_readiness.py` (visibility fixture + grading updated).
- `95cf7c0` **docs(v2)** — `v2.md` entry: non-HTTP / `interface_type` contracts
  (discussion #16 — BLE/serial/mqtt/file/custom, drop endpoint+URL requirement for
  non-HTTP media; + a lightweight contract-less mode alt).
- `02a4963` **chore(gui)** — `SYS_BUDDY_GUI_DEBUG=1` enables the pywebview web inspector
  in `run_gui` (so silent dead-clicks surface in a console). Off by default.

## get_contract return shape (the new contract — don't drift)
- No contract at all → `{"exists": false}`
- Proposed → `{exists, version, status:"proposed", locked:false, staging_url:null,
  spec:<shape, no staging_url>, signatures:[...], awaiting:[...], note, [locked_version_in_force]}`
- Locked → `{exists, version, status:"locked", locked:true, staging_url:"…", spec, signatures, locked_at}`

## Proof
`uv run pytest -q` → **271 passed**. Full suite, not just the touched files.

## What's NOT done / next steps
- **Three roles / scoped-parties (discussion #9) — NOT started.** `lock_contract` STILL
  requires ALL declared roles to sign (`required = _roles(conn, task_id)`), so a
  frontend↔backend contract on a 3-role task can't lock without mobile. Two sizes on the
  table: (a) incremental — a contract declares its `parties`; `lock_contract` needs only
  those; `get_contract` per-caller; DB contract↔party table or `parties_json`. (b) full —
  #9's TODO layer (a contract per TODO between its parties). #9 is not yet in `v2.md`.
- **Push + PR**: branch is local only. Push under `tooney92`, open PR into `main`, then
  switch active gh account back to `anthugny`.
- **Discussion #16 screenshots** and **#16 non-HTTP** are logged in `v2.md`, not built.

## Gotchas
- Pushing this repo needs the **`tooney92`** gh account (`gh auth switch --user tooney92
  && gh auth setup-git`); `anthugny` gets 403. Switch back to `anthugny` after. `main` is
  PR-protected (owner can bypass, prints "Bypassed rule violations").
- Earlier in the day an interim "sign-then-see" prompt edit (teaching that get_contract is
  locked-only) was made and then **superseded** by this fix — if you see any locked-only
  wording anywhere, it's stale; the model is now review-in-get_contract.
