"""Verification specs and runs — engagement mode (``docs/enhancements.md`` items 3–4).

A **spec** is a dev's CLAIM about what he built, plus how to find it. Prose, not a
script: "Playwright" throughout this system means the Playwright MCP — an agent
driving a real browser with its own judgement — so there is nothing to compile and
the dev writes nothing executable.

A **run** is one sitting of the owner's agent going to staging and checking. There
are no partial runs: a run covers every deliverable, because the fix for one thing
breaks another and re-checking only what broke is wrong at the exact moment being
wrong is most expensive (the owner is about to accept and pay). Every run is
appended; the dashboard shows the latest.

Four decisions carry this module.

**The broker stamps, the dev never does.** On :func:`submit_spec` the broker walks
deliverable → linked todos → their locked contract versions and stores that map
itself. If the party being audited could choose the stamp, the stamp proves nothing.

**The stamp exists to tell two different failures apart.** "The work is broken" and
"the check is out of date" both render as a red ✗ and they call for opposite
actions: John's note expects 3 buttons and the run finds 4 — either he never built
them, or the owner later asked for a fourth, the contract moved to v2 and the note
was never updated. Without :func:`is_stale` the system periodically accuses honest
devs of failing in front of the person paying them, which is the fastest way to make
devs refuse to use it.

**Absolute URLs are refused, by regex and not by judgement.** That is the entire
mechanical safety story and it is enough, because the testing agent's only base URL
is the one the HOST provided (``state.resolve_staging_url``). If the dev's text can
only contain paths, "go to evil.com" is not expressible — there is no field for the
injection to land in. Everything else about the dev's text is already carried:
it is DATA, never an instruction, and the owner's agent is pre-flighted on not
taking a dev's word for anything.

**The broker looks things up and compares them; the agents do the judging.** Nothing
here reads a claim, scores it, or decides whether work is done. :func:`coverage`
counts rows over a join — it measures PRESENCE, not quality, and a spec with one
lazy assertion still "covers" its deliverable, so "3 of 4 covered" must never be
rendered as "3 of 4 are properly tested".

Everything in this module is engagement-mode only; ``contract``/``debug`` tasks
never reach it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from .identity import Identity

# --- how strongly a result knows what it claims ------------------------------
# It MUST be printed. A non-technical reader cannot infer the difference, and
# blurring it manufactures exactly the false confidence this feature exists to
# remove. Reading a migration proves the migration EXISTS — not that it ran, not
# that it works — so code review stays 'evidence', never 'verified'.
VERIFIED = "verified"        # the agent went and looked. It ran.
EVIDENCE = "evidence"        # something was read (a diff, a migration). Nothing proven.
NOT_CHECKED = "not_checked"  # said out loud, because silence reads as a pass.

STRENGTHS = (VERIFIED, EVIDENCE, NOT_CHECKED)

# The words a surface should print for each, so the dashboard, the CLI and the
# agent's own summary cannot each invent a gentler phrasing of 'not_checked'.
STRENGTH_LABELS = {
    VERIFIED: "Verified — this ran",
    EVIDENCE: "Evidence reviewed — nothing proven",
    NOT_CHECKED: "Not checked",
}

# A verdict reuses the deliverable's own vocabulary (item 1) rather than inventing a
# parallel one for "this isn't done".
ACCEPTED = "accepted"
REJECTED = "rejected"
VERDICTS = (ACCEPTED, REJECTED)

# Staleness is three-valued, and the third value is the honest one: with no linked
# todo carrying a locked contract there is nothing to compare against, and reporting
# that as "current" would imply a freshness check that never happened.
CURRENT = "current"
STALE = "stale"
UNKNOWN = "unknown"

ENGAGEMENT = "engagement"

# Caps, for the same reason contracts bounds its unit lists: one agent must not be
# able to push an unbounded blob into a DB row and into every dashboard payload.
MAX_CLAIM = 300
MAX_HOW = 2000
MAX_DETAIL = 4000
MAX_STAGING_URL = 500

# --- "paths, never URLs" -----------------------------------------------------
# Mechanical, deliberately. Each pattern is a shape a navigable absolute target can
# take; none of them requires reading the sentence around it.
#
#   "they're at /pricing, below the hero"   ✓
#   "they're at https://evil.com/pricing"   ✗
_ABSOLUTE_PATTERNS = (
    # scheme://host — https, http, ftp, ws, anything
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://"),
    # protocol-relative //host, which a browser resolves against the current scheme
    re.compile(r"(?:^|\s)//[a-zA-Z0-9]"),
    # schemes that navigate or execute without a '//' authority
    re.compile(r"\b(?:javascript|data|mailto|file|vbscript|blob):", re.I),
    # a bare www. host
    re.compile(r"(?<![\w.])www\.[a-zA-Z0-9-]", re.I),
    # a bare host CARRYING A PATH — "evil.com/pricing". The trailing slash is
    # required on purpose: it is what separates a navigable target from a filename
    # ("vite.config.dev", "server.io"), and without it the check would refuse
    # ordinary prose about ordinary files.
    re.compile(
        r"(?<![\w.@/\-])[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9\-]+)*"
        r"\.(?:com|net|org|io|dev|app|co|ai|sh|xyz|cc|me|site|online|cloud|link|to)"
        r"(?::\d+)?/",
        re.I,
    ),
)

_URL_REFUSAL = (
    "{field} contains an absolute URL, and a spec may only contain PATHS — write "
    "'/pricing', not 'https://example.com/pricing'. The agent that checks your work "
    "is given exactly one base URL, by the host, and it will not go anywhere else; a "
    "URL here cannot send it somewhere and is refused rather than silently dropped, "
    "so you know the target was never changed."
)


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# validation — pure, and every error in one pass
# --------------------------------------------------------------------------- #
def validate_claim(claim: object, how: object) -> list[str]:
    """Return a list of human-fixable error strings; empty list means valid.

    Pure (no DB, no raising), exactly like ``contracts.validate_spec``: this module
    owns "is this text acceptable?" and the caller owns what to do about it. Every
    error is collected in one pass so a dev fixes a spec in a single revision rather
    than round-tripping through the broker once per mistake.
    """
    return _validate_text(claim, "claim", MAX_CLAIM) + _validate_text(how, "how", MAX_HOW)


def _validate_text(value: object, field: str, cap: int) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [
            f"'{field}' must be a non-empty string — "
            + (
                "say what YOU added, e.g. 'added 3 buttons to the landing page'"
                if field == "claim"
                else "say how to find it, e.g. 'below the hero on /, "
                "pricing / features / contact'"
            )
        ]
    errors: list[str] = []
    if len(value) > cap:
        errors.append(f"'{field}' must be at most {cap} characters (got {len(value)})")
    if has_absolute_url(value):
        errors.append(_URL_REFUSAL.format(field=f"'{field}'"))
    return errors


def has_absolute_url(text: str) -> bool:
    """True if ``text`` contains anything shaped like an absolute, navigable target.

    Public because the same question is asked of anything a dev writes that an agent
    might later act on. A regex and nothing else — no judgement, no model, no
    allow-list of "good" hosts, because an allow-list is a decision and this must be
    a lookup.
    """
    return any(pattern.search(text) for pattern in _ABSOLUTE_PATTERNS)


# --------------------------------------------------------------------------- #
# the stamp — written by the broker, never by the dev
# --------------------------------------------------------------------------- #
def stamp_for(conn, deliverable_id: int) -> dict[str, int]:
    """``{todo_number: locked_contract_version}`` for a deliverable, right now.

    ``spec says deliverable #1 → which todos link to #1? → #2 and #5 → their locked
    versions? → {"2": 1, "5": 2}``. Only possible because todo → deliverable links
    are REQUIRED in engagement mode; an optional link would make both this and
    :func:`coverage` silently partial.

    Keyed by the human's ``#N`` and stringified, because that is what survives a
    round trip through JSON and what a person reads in the log.

    A todo with no LOCKED contract contributes nothing: there is no agreed version to
    snapshot yet. A dropped todo contributes nothing either — it is no longer part of
    what was agreed. An empty map is therefore "nothing to compare", which
    :func:`staleness` reports as ``unknown`` rather than as ``current``.
    """
    rows = conn.execute(
        "SELECT t.number AS number, "
        "       (SELECT MAX(c.version) FROM contracts c "
        "         WHERE c.todo_id = t.id AND c.status = 'locked') AS version "
        "  FROM todo_deliverables td "
        "  JOIN todos t ON t.id = td.todo_id "
        " WHERE td.deliverable_id = ? AND t.dropped_at IS NULL",
        (deliverable_id,),
    ).fetchall()
    return {
        str(row["number"]): int(row["version"])
        for row in rows
        if row["number"] is not None and row["version"] is not None
    }


def staleness(conn, spec_id: int) -> str:
    """``'current'`` | ``'stale'`` | ``'unknown'`` for one spec.

    ``unknown`` is not a failure to compute — it is the truthful answer when the
    stamp is empty (no linked todo has a locked contract, or nothing is linked at
    all). Surfaces must SAY unknown; rendering it as current claims a freshness check
    that was never possible.
    """
    row = _spec_row(conn, spec_id)
    stamped = json.loads(row["stamped_json"])
    if not stamped:
        return UNKNOWN
    return CURRENT if stamped == stamp_for(conn, row["deliverable_id"]) else STALE


def is_stale(conn, spec_id: int) -> bool:
    """Has a contract moved under this spec since it was written?

    Recomputes the stamp and compares. ``unknown`` answers False: the broker will not
    assert a spec is out of date when it has nothing to compare it against. Callers
    that need to distinguish "known current" from "cannot tell" must use
    :func:`staleness` — and any surface showing a red ✗ should, because the two mean
    opposite things to whoever has to act.
    """
    return staleness(conn, spec_id) == STALE


# --------------------------------------------------------------------------- #
# specs
# --------------------------------------------------------------------------- #
def submit_spec(
    conn, identity: Identity, deliverable_number: int, claim: str, how: str
) -> dict:
    """A dev leaves his claim on a deliverable, plus how to find it.

    ONE spec per dev per deliverable. Two frontends on one deliverable leave two
    specs and BOTH are evaluated — they do not conflict, because each asserts what
    THAT dev added. Attribution is the point: a dev who claims work he did not do is
    caught at his own claim rather than hidden inside a deliverable that mostly works.

    The dev supplies exactly one binding — the deliverable. The version stamp is
    DERIVED from it by the broker (see :func:`stamp_for`) and is not a parameter,
    deliberately: the party being audited must not be able to choose the evidence
    that says whether his check is current.
    """
    _require_engagement(conn, identity.task_id)
    deliverable = _deliverable_by_number(conn, identity.task_id, deliverable_number)

    errors = validate_claim(claim, how)
    if errors:
        # Joined, so the agent gets every fix in one shot rather than one per call.
        raise ValueError("cannot submit this spec:\n- " + "\n- ".join(errors))

    stamped = stamp_for(conn, deliverable["id"])
    try:
        cur = conn.execute(
            "INSERT INTO specs (deliverable_id, agent_id, role, claim, how, "
            "stamped_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                deliverable["id"],
                identity.agent_id,
                identity.role,
                claim.strip(),
                how.strip(),
                json.dumps(stamped),
                _now(),
            ),
        )
    except sqlite3.IntegrityError:
        raise ValueError(
            f"{identity.role} already left a spec on deliverable "
            f"#{deliverable['number']}, and there is one per dev per deliverable — a "
            f"spec is your single CLAIM about what you built, not a running list of "
            f"notes. If the claim has changed, say so on the channel; if another "
            f"deliverable is the one you meant, submit against that number."
        ) from None

    spec_id = cur.lastrowid
    _event(
        conn,
        identity.task_id,
        "spec_submitted",
        {
            "spec_id": spec_id,
            "deliverable_id": deliverable["id"],
            "deliverable_number": deliverable["number"],
            "role": identity.role,
            "stamped": stamped,
        },
    )
    conn.commit()
    return _spec_dict(conn, _spec_row(conn, spec_id))


def specs_for(conn, deliverable_id: int) -> list[dict]:
    """Every dev's claim on one deliverable, oldest first.

    Contradictory claims are SURFACED, never merged: the broker cannot know which is
    right, and "the two people who built this disagree about what is there" is
    exactly what the owner should see.
    """
    rows = conn.execute(
        "SELECT * FROM specs WHERE deliverable_id = ? ORDER BY id", (deliverable_id,)
    ).fetchall()
    return [_spec_dict(conn, row) for row in rows]


def _spec_dict(conn, row) -> dict:
    name = conn.execute(
        "SELECT name FROM agents WHERE id = ?", (row["agent_id"],)
    ).fetchone()
    stamped = json.loads(row["stamped_json"])
    state = (
        UNKNOWN
        if not stamped
        else (CURRENT if stamped == stamp_for(conn, row["deliverable_id"]) else STALE)
    )
    return {
        "id": row["id"],
        "deliverable_id": row["deliverable_id"],
        "agent_id": row["agent_id"],
        "role": row["role"],
        "name": name["name"] if name is not None else row["role"],
        "claim": row["claim"],
        "how": row["how"],
        "stamped": stamped,
        "staleness": state,
        "stale": state == STALE,
        "created_at": row["created_at"],
    }


def _spec_row(conn, spec_id: int):
    row = conn.execute("SELECT * FROM specs WHERE id = ?", (spec_id,)).fetchone()
    if row is None:
        raise ValueError(f"no spec with id {spec_id}")
    return row


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
def _assert_the_work_is_finished(conn, task_id: str) -> None:
    """Refuse a run while the team is still building.

    THERE ARE TWO LEVELS OF DONE, and conflating them is what this guards.

    * A **todo** reaches ``verified`` when the BUILDERS agree it is finished — one dev
      builds, another checks, peer to peer. That is the team saying "we're done".
    * A **deliverable** is verified when the OWNER's agent goes and looks. That is the
      client saying "this is what I asked for".

    The second must follow the first. `docs/enhancements.md` item 4 puts it as *devs
    finish → owner is notified → owner decides when to look*, and starting a run before
    the devs have finished inverts it: the owner tests half-built work, gets failures
    that are nobody's fault, and either loses confidence in the team or in the checking.
    Both are worse than waiting.

    A deliverable that NO todo claims also blocks, and that is the less obvious half.
    It looks like there is nothing to wait for — but no todo means no contract, so
    nothing was ever agreed about HOW it gets built. The invariant is "we said we will
    build like this, and this is how we have built"; an unclaimed deliverable skips the
    first half and then gets checked as though it had not. Observed live: the owner's
    agent verified a mobile-layout deliverable that no todo named, purely because the
    landing-page work happened to satisfy it. It passed, and it should never have been
    checkable — the team had not agreed to build it at all.

    The fix is a person's, and the refusal says which: raise a todo for it, or withdraw
    it. Withdrawn deliverables are out of scope entirely.
    """
    unclaimed = conn.execute(
        """
        SELECT d.number AS number, d.text AS text
          FROM deliverables d
         WHERE d.task_id = ?
           AND d.withdrawn_at IS NULL
           AND NOT EXISTS (
                 SELECT 1 FROM todo_deliverables td
                   JOIN todos t ON t.id = td.todo_id
                  WHERE td.deliverable_id = d.id
                    AND t.dropped_at IS NULL
               )
      ORDER BY d.number
        """,
        (task_id,),
    ).fetchall()
    if unclaimed:
        named = ", ".join(f"#{r['number']} '{r['text']}'" for r in unclaimed)
        raise ValueError(
            f"nobody has taken this on yet: {named}. No todo names it, so no contract "
            f"covers it and nothing was agreed about HOW it gets built — checking it "
            f"would confirm work the team never agreed to do. Raise a todo naming the "
            f"deliverable, or withdraw it if it is no longer wanted."
        )

    rows = conn.execute(
        """
        SELECT d.number AS number, t.number AS todo_number, t.title AS title, t.state AS state
          FROM deliverables d
          JOIN todo_deliverables td ON td.deliverable_id = d.id
          JOIN todos t              ON t.id = td.todo_id
         WHERE d.task_id = ?
           AND d.withdrawn_at IS NULL
           AND t.dropped_at IS NULL
           AND t.state != 'verified'
      ORDER BY d.number, t.number
        """,
        (task_id,),
    ).fetchall()
    if not rows:
        return
    unfinished = ", ".join(
        f"#{r['todo_number']} '{r['title']}' ({r['state']}) on deliverable #{r['number']}"
        for r in rows
    )
    raise ValueError(
        f"the team has not finished yet, so there is nothing to check: {unfinished}. "
        f"A todo reaches 'verified' when the BUILDERS agree it is done — that is what "
        f"tells you there is something to look at. Wait for them to say so, then start "
        f"the run. Checking half-built work produces failures that are nobody's fault."
    )


def start_run(conn, identity: Identity, staging_url: str | None) -> int:
    """Open one sitting of checking, and return its id.

    The OWNER triggers it, after being told there is something to check. Not
    automatic: *Talk anywhere, act here* applied to verification — the run is a
    person deciding to look, not a reflex the system fires on ``report_status``, and
    it means the owner is present for the result rather than finding a verdict he
    never asked for.

    ``staging_url`` is recorded per run because the DEV supplies the target, so the
    safeguard is DISCLOSURE rather than permission: "verified against
    https://random-app.vercel.dev" in front of an owner whose site is acme.com is
    visible. A record that does not say where it looked is unfalsifiable — which is
    why the value is stored verbatim and not normalised away.
    """
    _require_engagement(conn, identity.task_id)
    if identity.kind != "owner":
        raise ValueError(
            "only the owner starts a verification run — it is the owner's agent going "
            "and looking, and a run started by the party being audited proves nothing. "
            "Report what you built (report_status) and leave a spec (submit_spec); the "
            "owner decides when to check."
        )

    _assert_the_work_is_finished(conn, identity.task_id)

    url = staging_url.strip() if isinstance(staging_url, str) else None
    if url is not None and len(url) > MAX_STAGING_URL:
        raise ValueError(
            f"'staging_url' must be at most {MAX_STAGING_URL} characters "
            f"(got {len(url)})"
        )

    cur = conn.execute(
        "INSERT INTO verification_runs (task_id, agent_id, staging_url, created_at) "
        "VALUES (?,?,?,?)",
        (identity.task_id, identity.agent_id, url or None, _now()),
    )
    run_id = cur.lastrowid
    _event(
        conn,
        identity.task_id,
        "run_started",
        {"run_id": run_id, "staging_url": url or None, "by": identity.role},
    )
    conn.commit()
    return run_id


def record_result(
    conn,
    run_id: int,
    deliverable_id: int,
    verdict: str,
    strength: str,
    detail: str | None,
    spec_id: int | None = None,
) -> dict:
    """File one verdict in a run — for a whole deliverable, or for one dev's claim.

    ``spec_id`` is what makes the report per-person, which is sharper than
    deliverable-level pass/fail::

        DELIVERABLE #1 · Landing page
          James  "added the shell"    ✓ verified
          John   "added 3 buttons"    ✗ rejected — found 2

    A dev who claims work he did not do is caught at his own claim; the honest dev is
    individually credited, which is the other half of *both sides represented*.

    Uniqueness over ``(run_id, deliverable_id, spec_id)`` is enforced HERE and not by
    an index: ``spec_id`` is nullable and SQLite treats NULLs as DISTINCT, so a
    unique index would silently fail to constrain exactly the whole-deliverable rows
    it looks like it covers — the NULL-distinct trap this schema has been bitten by
    before.
    """
    run = conn.execute(
        "SELECT * FROM verification_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise ValueError(f"no verification run with id {run_id}")
    _require_engagement(conn, run["task_id"])

    errors: list[str] = []
    if verdict not in VERDICTS:
        errors.append(
            f"'verdict' {verdict!r} is not a verdict (expected one of {list(VERDICTS)})"
        )
    if strength not in STRENGTHS:
        errors.append(
            f"'strength' {strength!r} is not a strength (expected one of "
            f"{list(STRENGTHS)}): {VERIFIED!r} means you went and looked and it ran, "
            f"{EVIDENCE!r} means something was read and nothing was proven — reading a "
            f"migration proves it exists, not that it ran — and {NOT_CHECKED!r} is said "
            f"out loud, because silence reads as a pass"
        )
    if detail is not None and not isinstance(detail, str):
        errors.append("'detail' must be a string or omitted")
    elif isinstance(detail, str) and len(detail) > MAX_DETAIL:
        errors.append(
            f"'detail' must be at most {MAX_DETAIL} characters (got {len(detail)})"
        )

    deliverable = conn.execute(
        "SELECT * FROM deliverables WHERE id = ?", (deliverable_id,)
    ).fetchone()
    if deliverable is None:
        errors.append(f"no deliverable with id {deliverable_id}")
    elif deliverable["task_id"] != run["task_id"]:
        errors.append(
            f"deliverable {deliverable_id} belongs to task "
            f"'{deliverable['task_id']}', not to this run's task '{run['task_id']}'"
        )

    if spec_id is not None:
        spec = conn.execute("SELECT * FROM specs WHERE id = ?", (spec_id,)).fetchone()
        if spec is None:
            errors.append(f"no spec with id {spec_id}")
        elif spec["deliverable_id"] != deliverable_id:
            errors.append(
                f"spec {spec_id} is a claim about deliverable "
                f"{spec['deliverable_id']}, so it cannot be judged under deliverable "
                f"{deliverable_id} — a result judges the claim where it was made"
            )

    if not errors and _already_recorded(conn, run_id, deliverable_id, spec_id):
        subject = f"spec {spec_id}" if spec_id is not None else "the deliverable itself"
        errors.append(
            f"this run already recorded a result for {subject} on deliverable "
            f"{deliverable_id} — one verdict per claim per run, or the log stops "
            f"saying what a run concluded. Start a new run to check it again."
        )

    if errors:
        raise ValueError("cannot record this result:\n- " + "\n- ".join(errors))

    cur = conn.execute(
        "INSERT INTO verification_results (run_id, deliverable_id, spec_id, verdict, "
        "strength, detail, created_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, deliverable_id, spec_id, verdict, strength, detail, _now()),
    )
    _event(
        conn,
        run["task_id"],
        "result_recorded",
        {
            "run_id": run_id,
            "result_id": cur.lastrowid,
            "deliverable_id": deliverable_id,
            "deliverable_number": deliverable["number"],
            "spec_id": spec_id,
            "verdict": verdict,
            "strength": strength,
        },
    )
    conn.commit()
    return _result_dict(
        conn,
        conn.execute(
            "SELECT * FROM verification_results WHERE id = ?", (cur.lastrowid,)
        ).fetchone(),
    )


def _already_recorded(conn, run_id: int, deliverable_id: int, spec_id: int | None):
    if spec_id is None:
        return conn.execute(
            "SELECT 1 FROM verification_results WHERE run_id = ? AND deliverable_id = ? "
            "AND spec_id IS NULL LIMIT 1",
            (run_id, deliverable_id),
        ).fetchone()
    return conn.execute(
        "SELECT 1 FROM verification_results WHERE run_id = ? AND deliverable_id = ? "
        "AND spec_id = ? LIMIT 1",
        (run_id, deliverable_id, spec_id),
    ).fetchone()


def latest_run(conn, task_id: str) -> dict | None:
    """The most recent run on a task, or None if nobody has checked yet."""
    row = conn.execute(
        "SELECT * FROM verification_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    by = conn.execute(
        "SELECT name, handle, role FROM agents WHERE id = ?", (row["agent_id"],)
    ).fetchone()
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "agent_id": row["agent_id"],
        "by": (by["handle"] or by["role"]) if by is not None else None,
        "by_name": by["name"] if by is not None else None,
        "staging_url": row["staging_url"],
        "created_at": row["created_at"],
    }


def latest_results(conn, task_id: str) -> dict:
    """The LATEST run's results, grouped by deliverable id.

    The latest run IS the answer, and that is not a shortcut — a run always covers
    every deliverable, so every tick on screen came from the same sitting. That
    single rule deletes stale ticks, "latest result per deliverable" bookkeeping, and
    any need to label a result with its age.

    Shape::

        {"run": {...} | None, "by_deliverable": {deliverable_id: [result, ...]}}

    Older runs are NOT returned and are NOT deleted: the log is complete, the
    dashboard is current, and the history is one click away. "You said it was done
    and it wasn't, twice" is exactly the evidence an owner needs in a dispute.
    """
    run = latest_run(conn, task_id)
    if run is None:
        return {"run": None, "by_deliverable": {}}
    rows = conn.execute(
        "SELECT * FROM verification_results WHERE run_id = ? ORDER BY id", (run["id"],)
    ).fetchall()
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["deliverable_id"], []).append(_result_dict(conn, row))
    return {"run": run, "by_deliverable": grouped}


def _result_dict(conn, row) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "deliverable_id": row["deliverable_id"],
        "spec_id": row["spec_id"],
        "verdict": row["verdict"],
        "strength": row["strength"],
        "strength_label": STRENGTH_LABELS.get(row["strength"], row["strength"]),
        "detail": row["detail"],
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #
def coverage(conn, task_id: str) -> dict:
    """``{"deliverables": n, "with_spec": n, "with_result": n}`` — three counts.

    What it buys: the owner learns how much of what he asked for was actually
    checked, so "everything passed" cannot hide a deliverable nobody looked at. And
    it is not something his agent asserts in a summary he has to trust — it is the
    broker comparing two lists it already holds, so a summary claiming "all good"
    while #4 has no result is contradicted by the dashboard.

    ``with_result`` counts the LATEST run only, for the same reason
    :func:`latest_results` does: a run covers everything, so results from an earlier
    run describe a different build.

    Withdrawn deliverables are excluded from all three — scope may shrink, and what
    was withdrawn is no longer something to check.

    **It measures presence, not quality.** A spec with one lazy assertion still
    "covers" its deliverable. "3 of 4 covered" must never be rendered so a
    non-technical owner reads it as "3 of 4 are properly tested" — this is exactly
    the place that misreading would happen.
    """
    live = conn.execute(
        "SELECT id FROM deliverables WHERE task_id = ? AND withdrawn_at IS NULL",
        (task_id,),
    ).fetchall()
    ids = {row["id"] for row in live}
    if not ids:
        return {"deliverables": 0, "with_spec": 0, "with_result": 0}

    placeholders = ",".join("?" for _ in ids)
    with_spec = {
        row["deliverable_id"]
        for row in conn.execute(
            f"SELECT DISTINCT deliverable_id FROM specs "
            f"WHERE deliverable_id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    }
    run = latest_run(conn, task_id)
    with_result: set[int] = set()
    if run is not None:
        with_result = {
            row["deliverable_id"]
            for row in conn.execute(
                f"SELECT DISTINCT deliverable_id FROM verification_results "
                f"WHERE run_id = ? AND deliverable_id IN ({placeholders})",
                (run["id"], *ids),
            ).fetchall()
        }
    return {
        "deliverables": len(ids),
        "with_spec": len(with_spec & ids),
        "with_result": len(with_result & ids),
    }


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _require_engagement(conn, task_id: str) -> None:
    """Every rule in this module is gated on the task's mode, so ``contract`` and
    ``debug`` tasks behave exactly as they did before engagement mode existed."""
    row = conn.execute("SELECT mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    if row["mode"] != ENGAGEMENT:
        raise ValueError(
            f"task '{task_id}' is a '{row['mode']}' task, and verification specs and "
            f"runs belong to an engagement — they check what an OWNER commissioned "
            f"against deliverables he agreed to, and a peer task has neither"
        )


def _deliverable_by_number(conn, task_id: str, number: object) -> dict:
    """Resolve the human's ``#N`` on this task. Numbers are never reused and never
    renumbered, so ``#2`` in a message is ``#2`` here forever."""
    try:
        n = int(number)
    except (TypeError, ValueError):
        raise ValueError(
            f"deliverable number must be a number, got {number!r}"
        ) from None
    row = conn.execute(
        "SELECT * FROM deliverables WHERE task_id = ? AND number = ?", (task_id, n)
    ).fetchone()
    if row is None:
        raise ValueError(f"no deliverable #{n} on task '{task_id}'")
    if row["withdrawn_at"] is not None:
        raise ValueError(
            f"deliverable #{n} was withdrawn"
            + (f" ({row['withdraw_reason']})" if row["withdraw_reason"] else "")
            + " — there is nothing left to claim against it"
        )
    return {"id": row["id"], "number": row["number"], "text": row["text"]}


def _event(conn, task_id: str, action: str, detail: dict) -> None:
    """Append a ``verification`` event.

    One kind in the log with the specific action inside, mirroring ``todos._event``,
    so the dashboard's kind filter keeps a fixed vocabulary. Every run lands here and
    nothing is ever rewritten: the event log is append-only and already the system of
    record, which is why "log every run, show the latest" needs no new storage.
    """
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, "verification", json.dumps({"action": action, **detail}), _now()),
    )
