"""The enforced state machine, contract lifecycle, and broker-counted strikes.

SPEC §5 (states), §6 (contract lock), §8 (strikes). This module is where the
guiding principle lives: **the broker enforces, agents request.** Every workflow
rule here is code or a DB fact, never a prompt. An agent asks to deploy; the
broker decides whether a locked contract exists and whether the caller is the
backend. An agent reports a failing test; the broker — not the agent — increments
a database column and pulls the stuck cord at three.

Enforcement runs in BOTH modes. SPEC §3 calls the local state machine "advisory",
but we enforce there too: a second, laxer code path is a second place for bugs and
security gaps to hide, and enforcement never *hurts* a well-behaved local agent. So
these functions are mode-agnostic — there is exactly one path, and it enforces.

States (SPEC §5):
    open → contract_proposed → contract_locked → backend_live → testing → verified
                                                       ↑             │
                                                       └── retry ─────┘ (or → stuck)
``verified`` and ``stuck`` are terminal: reopening requires a human.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import config, contracts, service, slack
from .identity import Identity

# --- states -----------------------------------------------------------------
OPEN = "open"
CONTRACT_PROPOSED = "contract_proposed"
CONTRACT_LOCKED = "contract_locked"
BACKEND_LIVE = "backend_live"
TESTING = "testing"
VERIFIED = "verified"
STUCK = "stuck"
RESOLVED = "resolved"  # debug tasks: terminal, reached from any non-terminal state

TERMINAL_STATES = frozenset({VERIFIED, STUCK, RESOLVED})

# --- report_status vocabulary -----------------------------------------------
# The status strings an agent may pass to report_status. Named for what the agent
# is asserting happened, so the broker can map each to a transition + typed message.
STATUS_DEPLOYED = "deployed"       # backend: the API is live on staging
STATUS_TEST_PASSED = "test_passed"  # client role: e2e suite went green
STATUS_TEST_FAILED = "test_failed"  # client role: e2e suite went red (a strike)
STATUS_VERIFIED = "verified"        # feature confirmed end-to-end (terminal)
STATUS_STUCK = "stuck"              # give up; humans needed (terminal)
STATUS_RESOLVED = "resolved"        # debug task: the issue is fixed (terminal)

TEST_STATUSES = frozenset({STATUS_TEST_PASSED, STATUS_TEST_FAILED})

# Task-agnostic vocabulary: the canonical words agents should reach for. Each is a
# pure ALIAS of an existing API/deploy-shaped status — same transition, same message,
# same strike behavior — so nothing downstream needs to know these words exist.
STATUS_READY = "ready"       # producer: my part is ready for the peer to build on
STATUS_CHECKED = "checked"    # consumer: it works against the producer's side
STATUS_BLOCKED = "blocked"    # consumer: it doesn't work (a strike)
_STATUS_ALIASES = {
    STATUS_READY: STATUS_DEPLOYED,
    STATUS_CHECKED: STATUS_TEST_PASSED,
    STATUS_BLOCKED: STATUS_TEST_FAILED,
}

MAX_STRIKES = 3  # SPEC §8: at 3 the broker force-transitions to stuck.

# The producer is NOT a hardcoded role (model B): it is whichever role proposed the
# current locked contract — see ``_producer_role``. Only the producer may report
# `ready`; only the OTHER (consuming) roles may report checks.


# --------------------------------------------------------------------------- #
# low-level helpers — the event-log convention (see step-4 brief) lives here
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.time()


def _state(conn, task_id: str) -> str:
    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    return row["state"]


def _task_mode(conn, task_id: str) -> str:
    """The task's workflow mode: 'contract' (full state machine) or 'debug'
    (simple open → resolved). Defaults to 'contract' when NULL or missing."""
    row = conn.execute("SELECT mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["mode"] is None:
        return "contract"
    return row["mode"]


def _event(conn, task_id: str, kind: str, detail: object) -> None:
    """Append an events row. ``detail`` is any JSON value; the API layer depends on
    the exact shapes documented in the step-4 brief (transition/lock/deploy/test)."""
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, kind, json.dumps(detail), _now()),
    )


def _transition(conn, task_id: str, to_state: str) -> str:
    """Move the task to ``to_state``, writing a ``transition`` event iff the state
    actually changes. Returns the resulting state. The transition event's
    ``created_at`` is what the API reads as ``times[to_state]`` — so we only emit
    one when there is a genuine change, never a no-op self-transition."""
    current = _state(conn, task_id)
    if current == to_state:
        return current
    conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (to_state, task_id))
    _event(conn, task_id, "transition", {"from": current, "to": to_state})
    return to_state


def _slack(conn, task_id: str, text: str) -> None:
    """Fire a best-effort Slack ping and record a ``slack`` event either way.

    The event is written regardless of whether a webhook is configured or the send
    succeeds, so the dashboard's event log shows that a human notification was
    triggered at this point. ``slack.notify`` never raises (SPEC §14)."""
    slack.notify(text)
    _event(conn, task_id, "slack", {"text": text})


def _reject_if_terminal(state: str) -> None:
    if state in TERMINAL_STATES:
        raise ValueError(
            f"task is in terminal state '{state}'; reopening requires a human"
        )


def _roles(conn, task_id: str) -> list[str]:
    row = conn.execute("SELECT roles_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return json.loads(row["roles_json"])


def _current_locked(conn, task_id: str) -> dict | None:
    """The highest-version locked contract for the task, or None.

    'Current' is the *newest* locked version: a v2 renegotiation supersedes v1
    the moment v2 locks, even though v1's row stays 'locked' for the audit trail.
    """
    row = conn.execute(
        "SELECT id, version, spec_json, locked_at FROM contracts "
        "WHERE task_id = ? AND status = 'locked' ORDER BY version DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "version": row["version"],
        "spec": json.loads(row["spec_json"]),
        "locked_at": row["locked_at"],
    }


def _producer_role(conn, task_id: str) -> str | None:
    """The PRODUCER role for a task — model B: whoever proposed the current locked
    contract. That role is the one others build against: it reports ``ready``, and it
    is the only role that may NOT report checks. ``None`` when no contract is locked
    yet (so there is no producer to speak of). Nothing is hardcoded to 'backend'.
    """
    row = conn.execute(
        "SELECT a.role AS role FROM contracts c JOIN agents a ON a.id = c.proposed_by "
        "WHERE c.task_id = ? AND c.status = 'locked' ORDER BY c.version DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["role"] if row else None


# --------------------------------------------------------------------------- #
# contract lifecycle
# --------------------------------------------------------------------------- #
def propose_contract(conn, identity: Identity, spec: dict) -> dict:
    """Validate and record a contract proposal, (re)opening negotiation.

    A proposal is valid from ``open`` or any later non-terminal state — a v2+
    proposal from, say, ``backend_live`` reopens negotiation and drops the task
    back to ``contract_proposed`` (SPEC §5 rule 1). Terminal tasks cannot be
    reopened without a human.
    """
    # staging_url strictness is mode-aware: remote peers are on another machine (real
    # https domain + SSRF guard); locally the frontend just hits the backend on
    # localhost, so any non-empty URL is fine. See contracts._validate_staging_url.
    errors = contracts.validate_spec(spec, is_remote=config.get_config().is_remote)
    if errors:
        # Raise with joined errors so the agent gets every fix in one shot.
        raise ValueError("invalid contract:\n- " + "\n- ".join(errors))

    current = _state(conn, identity.task_id)
    _reject_if_terminal(current)

    # Both parties must clear pre-flight before ANYONE can propose (owner rule): a
    # contract negotiated with an agent that never proved it understands the protocol
    # is worthless. Remote-only — local self-declared identities don't run pre-flight
    # (the middleware readiness gate is remote-only too), so gating there would brick
    # the whole local contract flow.
    if config.get_config().is_remote:
        not_ready = conn.execute(
            "SELECT role FROM agents WHERE task_id = ? AND revoked_at IS NULL AND ready = 0 "
            "ORDER BY role",
            (identity.task_id,),
        ).fetchall()
        if not_ready:
            waiting = ", ".join(r["role"] for r in not_ready)
            raise ValueError(
                "all parties must pass pre-flight before a contract can be proposed; "
                f"waiting on: {waiting}"
            )

    spec_json = json.dumps(spec)
    service.assert_content_size(spec_json, "contract spec")

    # Version is MAX+1; two concurrent proposals can compute the same value and
    # collide on UNIQUE(task_id, version). Retry on that collision (re-reading MAX)
    # so a racing proposer gets a clean higher version instead of an uncaught 500.
    for _attempt in range(6):
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM contracts WHERE task_id = ?",
            (identity.task_id,),
        ).fetchone()
        version = row["v"] + 1
        try:
            conn.execute(
                "INSERT INTO contracts (task_id, version, spec_json, status, proposed_by, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (identity.task_id, version, spec_json, "draft", identity.agent_id, _now()),
            )
            break
        except sqlite3.IntegrityError:
            conn.rollback()
    else:
        raise ValueError("could not allocate a contract version — please retry")
    state = _transition(conn, identity.task_id, CONTRACT_PROPOSED)
    conn.commit()
    # Tell the peer directly — a transition event alone is dashboard-only and would
    # never reach the other agent's wait_for_message queue. This is what makes the
    # negotiation actually flow: the peer hears "there's a proposal to assess."
    n_endpoints = len(spec.get("endpoints", []))
    service.post_message(
        conn,
        identity,
        "contract_proposal",
        f"Proposed contract v{version} ({n_endpoints} endpoint"
        f"{'' if n_endpoints == 1 else 's'}). Review it with get_contract, then sign with "
        f"lock_contract — or send a message to request changes before you sign.",
    )
    return {"version": version, "state": state}


def lock_contract(conn, identity: Identity, version: int) -> dict:
    """Record this agent's signature on a contract version; lock only when ALL
    declared roles have signed (SPEC §5 rule 2, §6).

    Not two signatures — *all of them*, per ``tasks.roles_json``. A locked contract
    is immutable (rule 6): re-signing or re-locking it is rejected, and changes must
    go through a fresh version → all roles re-sign.
    """
    _reject_if_terminal(_state(conn, identity.task_id))

    contract = conn.execute(
        "SELECT id, status FROM contracts WHERE task_id = ? AND version = ?",
        (identity.task_id, version),
    ).fetchone()
    if contract is None:
        raise ValueError(
            f"no contract version {version} on task '{identity.task_id}'"
        )
    if contract["status"] == "locked":
        raise ValueError(
            f"contract version {version} is already locked and immutable; "
            f"propose a new version to change it"
        )

    # Record this signature (idempotent — signing twice is a no-op, not an error).
    conn.execute(
        "INSERT OR IGNORE INTO contract_signatures (contract_id, agent_id, signed_at) "
        "VALUES (?,?,?)",
        (contract["id"], identity.agent_id, _now()),
    )

    required = _roles(conn, identity.task_id)
    signed = [
        r["role"]
        for r in conn.execute(
            "SELECT a.role FROM contract_signatures s "
            "JOIN agents a ON a.id = s.agent_id WHERE s.contract_id = ?",
            (contract["id"],),
        ).fetchall()
    ]
    signed_set = set(signed)
    remaining = [r for r in required if r not in signed_set]

    if remaining:
        # Partial signature is a normal, expected outcome — not an error.
        conn.commit()
        # Let the peer know a signature landed and the ball is in their court.
        service.post_message(
            conn,
            identity,
            "contract_lock",
            f"Signed contract v{version}. Waiting on {', '.join(remaining)} to sign before "
            f"it locks.",
        )
        return {
            "locked": False,
            "version": version,
            "signed": sorted(signed_set),
            "remaining": remaining,
        }

    # All roles have signed → the contract locks and the task advances. The UPDATE
    # is conditional on status='draft' so that if two roles sign the final signature
    # concurrently and both observe "all signed", exactly one wins the lock — the
    # loser's rowcount is 0 and it returns the locked result WITHOUT a duplicate lock
    # event or a second human Slack ping.
    cur = conn.execute(
        "UPDATE contracts SET status = 'locked', locked_at = ? WHERE id = ? AND status = 'draft'",
        (_now(), contract["id"]),
    )
    if cur.rowcount != 1:
        conn.commit()
        return {"locked": True, "version": version, "signed": sorted(signed_set)}
    state = _transition(conn, identity.task_id, CONTRACT_LOCKED)
    _event(conn, identity.task_id, "lock", {"version": version, "signed": sorted(signed_set)})
    _slack(
        conn,
        identity.task_id,
        f"[{identity.task_id}] Contract v{version} locked — signed by {', '.join(sorted(signed_set))}",
    )
    conn.commit()
    service.post_message(
        conn,
        identity,
        "contract_lock",
        f"Contract v{version} is LOCKED — signed by all parties. This is the blueprint to "
        f"build against; get the staging_url from get_contract.",
    )
    return {"locked": True, "version": version, "signed": sorted(signed_set), "state": state}


def reopen_negotiations(conn, identity: Identity, reason: str) -> dict:
    """Drop a locked-or-later task back to ``contract_proposed`` (negotiations) so a
    new contract version can be proposed and re-signed.

    Non-destructive: the currently-locked contract keeps serving via ``get_contract``
    until a NEW version locks — nothing is deleted. Either party may call it (the
    "agreement" happens in chat first; a one-sided reopen is harmless — the peer just
    won't propose/sign anything new). Rejected on terminal tasks, and on tasks that
    haven't locked a contract yet (there's nothing to renegotiate — just keep talking
    or propose a first version).
    """
    current = _state(conn, identity.task_id)
    _reject_if_terminal(current)

    if _current_locked(conn, identity.task_id) is None:
        raise ValueError(
            "nothing to reopen — no contract has locked yet. Keep negotiating, or "
            "propose a first version with propose_contract."
        )
    if current in (OPEN, CONTRACT_PROPOSED):
        raise ValueError(f"already in negotiations (state '{current}') — nothing to reopen")

    detail = (reason or "").strip() or "(no reason given)"
    service.assert_content_size(detail, "reopen reason")
    state = _transition(conn, identity.task_id, CONTRACT_PROPOSED)
    _event(conn, identity.task_id, "reopen", {"from": current, "reason": detail})
    conn.commit()
    service.post_message(
        conn,
        identity,
        "renegotiation",
        f"Reopened negotiations (was {current}): {detail}. The last locked contract still "
        f"stands until a new version is proposed and re-signed.",
    )
    return {"state": state, "from": current, "reason": detail}


def get_contract(conn, task_id: str) -> dict:
    """Return the current locked contract, including ``staging_url`` read from the
    stored ``spec_json`` — NEVER from a chat message (SPEC §5 rule 5, §9).

    This is the single trusted source of the staging URL for a test-runner agent:
    an injected "test against evil.com" message has no path into this value.
    """
    contract = _current_locked(conn, task_id)
    if contract is None:
        return {"exists": False}
    spec = contract["spec"]
    return {
        "exists": True,
        "version": contract["version"],
        "status": "locked",
        "staging_url": spec.get("staging_url"),
        "spec": spec,
        "locked_at": contract["locked_at"],
    }


# --------------------------------------------------------------------------- #
# status reporting — the transitions, the strikes, the typed messages
# --------------------------------------------------------------------------- #
def report_status(conn, identity: Identity, status: str, detail: str) -> dict:
    """Drive a state transition AND post the corresponding typed message so the
    dashboard thread reflects it. Rejects (raises ``ValueError``) with a clear,
    agent-readable reason on any workflow or permission violation.

    Role-scoped permissions and state gates are enforced here, in code:
      * ``ready``         — producer role only; needs a locked contract; → backend_live
      * ``checked/blocked`` — consumer roles only; only after the producer is ready
      * ``verified``      — → terminal verified
      * ``stuck``         — → terminal stuck
    The old API/deploy-shaped words (``deployed``/``test_passed``/``test_failed``) are
    still accepted as aliases of ``ready``/``checked``/``blocked``.
    """
    service.assert_content_size(detail, "status detail")

    # Task-agnostic words funnel into the existing paths: normalize before any
    # dispatch or mode gate inspects the status, so all downstream logic is unchanged.
    status = _STATUS_ALIASES.get(status, status)

    # Mode gate: debug tasks have a single 'resolved' status; contract tasks have
    # the full deploy/test/verified vocabulary. Keep the two vocabularies disjoint.
    mode = _task_mode(conn, identity.task_id)
    if mode == "debug" and status != STATUS_RESOLVED:
        raise ValueError(
            "this is a debug task — report_status('resolved') when the issue is "
            "fixed (deploy/test/verified don't apply)"
        )
    if mode != "debug" and status == STATUS_RESOLVED:
        raise ValueError(
            "'resolved' is only for debug tasks; contract tasks finish with 'verified'"
        )

    if status == STATUS_DEPLOYED:
        return _report_deployed(conn, identity, detail)
    if status in TEST_STATUSES:
        return _report_test(conn, identity, status, detail)
    if status == STATUS_VERIFIED:
        return _report_verified(conn, identity, detail)
    if status == STATUS_STUCK:
        return _report_stuck(conn, identity, detail)
    if status == STATUS_RESOLVED:
        return _report_resolved(conn, identity, detail)
    raise ValueError(
        f"unknown status {status!r}; expected one of: "
        f"{STATUS_READY}, {STATUS_CHECKED}, {STATUS_BLOCKED}, "
        f"{STATUS_VERIFIED}, {STATUS_STUCK}"
    )


def _report_deployed(conn, identity: Identity, detail: str) -> dict:
    """The producer signals its part is ready for the other side to build on. Gated
    on: caller IS the producer (model B: the role that proposed the locked contract),
    task not terminal, and a locked contract exists (SPEC §5 rule 3 — no contract, no
    ready). Resets strikes when this carries a *newer* locked contract version than
    the last one (SPEC §8 — a genuine new attempt, not the same loop).
    """
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)

    # A proposal in flight means an unsigned newer version exists. Advancing now would
    # move the task on a contract that hasn't been re-signed by all roles (SPEC §5
    # rule 6). Even though an older version is still 'locked', refuse until the pending
    # proposal is locked.
    if state == CONTRACT_PROPOSED:
        raise ValueError(
            "cannot report 'ready' while a contract proposal is awaiting signatures; "
            "lock the current version first"
        )

    contract = _current_locked(conn, identity.task_id)
    if contract is None:
        raise ValueError("cannot report 'ready': no locked contract exists yet")

    # Only the producer — the role that PROPOSED this locked contract — may report it.
    producer = _producer_role(conn, identity.task_id)
    if identity.role != producer:
        raise ValueError(
            f"only the role that proposed the contract may report 'ready' "
            f"(the producer is '{producer}', you are '{identity.role}')"
        )

    # Strike reset: if the current locked contract was locked *after* the previous
    # deploy, the backend is deploying a renegotiated version — a fresh attempt, so
    # the ping-pong counter starts over. Same contract redeployed = same loop, keep
    # the count. This needs no extra column: locked_at vs the last deploy event time.
    last_deploy = conn.execute(
        "SELECT created_at FROM events WHERE task_id = ? AND kind = 'deploy' "
        "ORDER BY id DESC LIMIT 1",
        (identity.task_id,),
    ).fetchone()
    if (
        last_deploy is not None
        and contract["locked_at"] is not None
        and contract["locked_at"] > last_deploy["created_at"]
    ):
        conn.execute("UPDATE tasks SET strikes = 0 WHERE id = ?", (identity.task_id,))

    state = _transition(conn, identity.task_id, BACKEND_LIVE)
    _event(conn, identity.task_id, "deploy", {"text": detail})
    service.post_message(conn, identity, "deploy_confirmed", detail)
    conn.commit()
    strikes = conn.execute(
        "SELECT strikes FROM tasks WHERE id = ?", (identity.task_id,)
    ).fetchone()["strikes"]
    return {"status": STATUS_DEPLOYED, "state": state, "strikes": strikes}


def _report_test(conn, identity: Identity, status: str, detail: str) -> dict:
    """A consuming role reports a check result. Gated on: caller is NOT the producer
    (model B: the role that proposed the locked contract runs no checks on its own
    work), and the producer is already ready (SPEC §5 rule 4 — no checks before
    backend_live). A failure is a broker-counted strike; the third pulls the stuck cord.
    """
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)
    if state not in (BACKEND_LIVE, TESTING):
        raise ValueError(
            f"cannot report a check before the producer is ready "
            f"(task is '{state}', need '{BACKEND_LIVE}')"
        )
    # The producer doesn't check its own work; the consuming role(s) do.
    producer = _producer_role(conn, identity.task_id)
    if identity.role == producer:
        raise ValueError(
            f"the producer ('{producer}') doesn't report checks on its own work; "
            f"the consuming role(s) do"
        )

    # First test after a deploy advances the task into the testing phase.
    if state == BACKEND_LIVE:
        state = _transition(conn, identity.task_id, TESTING)

    if status == STATUS_TEST_PASSED:
        _event(conn, identity.task_id, "test", {"pass": True, "strike": None})
        service.post_message(conn, identity, "test_result", detail)
        conn.commit()
        return {"status": status, "state": state, "strikes": _strikes(conn, identity.task_id)}

    # test_failed → the broker (not the agent) counts the strike.
    conn.execute("UPDATE tasks SET strikes = strikes + 1 WHERE id = ?", (identity.task_id,))
    strikes = _strikes(conn, identity.task_id)
    _event(conn, identity.task_id, "test", {"pass": False, "strike": strikes})
    service.post_message(conn, identity, "test_result", detail)

    if strikes >= MAX_STRIKES:
        # Three strikes: force stuck, refuse further cycles. The counter is a DB
        # column — an agent cannot talk it out of this (SPEC §8).
        state = _transition(conn, identity.task_id, STUCK)
        service.post_message(
            conn, identity, "stuck",
            f"{MAX_STRIKES} fix cycles reached — humans needed. Last failure: {detail}",
        )
        _slack(
            conn, identity.task_id,
            f"[{identity.task_id}] STUCK: {MAX_STRIKES} fix cycles reached — humans needed. "
            f"Last failure: {detail}",
        )
    conn.commit()
    return {"status": status, "state": state, "strikes": strikes}


def _report_verified(conn, identity: Identity, detail: str) -> dict:
    """The feature is confirmed end-to-end → terminal ``verified``. Per SPEC §5
    (state table): ``verified`` transitions from ``testing`` ONLY — a client must
    have reported at least one test result first (that is what moves the task
    backend_live → testing), so the terminal state can never be reached with zero
    tests run. Role is unrestricted (any party may confirm); a human owns any
    reopening after."""
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)
    if state != TESTING:
        raise ValueError(
            f"cannot report 'verified' before tests have run (task is '{state}', "
            f"need '{TESTING}'); a client must report a test result first"
        )
    state = _transition(conn, identity.task_id, VERIFIED)
    service.post_message(conn, identity, "verified", detail)
    _slack(conn, identity.task_id, f"[{identity.name}] VERIFIED: {detail}")
    conn.commit()
    return {"status": STATUS_VERIFIED, "state": state}


def _report_resolved(conn, identity: Identity, detail: str) -> dict:
    """Debug task: the issue is fixed → terminal ``resolved``. Either party may
    resolve, from any non-terminal state. Reopening after is a human's job."""
    state = _state(conn, identity.task_id)
    _reject_if_terminal(state)
    state = _transition(conn, identity.task_id, RESOLVED)
    service.post_message(conn, identity, "resolved", detail)
    _event(conn, identity.task_id, "resolved", {"text": detail})
    _slack(conn, identity.task_id, f"[{identity.name}] RESOLVED: {detail}")
    conn.commit()
    return {"status": STATUS_RESOLVED, "state": state}


def _report_stuck(conn, identity: Identity, detail: str) -> dict:
    """Give up and pull in the humans → terminal ``stuck``. Valid from any
    non-terminal state (SPEC §7)."""
    _reject_if_terminal(_state(conn, identity.task_id))
    state = _transition(conn, identity.task_id, STUCK)
    service.post_message(conn, identity, "stuck", detail)
    _slack(conn, identity.task_id, f"[{identity.task_id}] STUCK: {detail}")
    conn.commit()
    return {"status": STATUS_STUCK, "state": state}


def _strikes(conn, task_id: str) -> int:
    return conn.execute("SELECT strikes FROM tasks WHERE id = ?", (task_id,)).fetchone()["strikes"]
