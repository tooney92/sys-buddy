"""Deliverables — what the OWNER commissioned, in his words, and the one agreement
that gates an engagement.

Engagement mode is commissioned work: a client pays, and cannot check what he got.
sys-buddy already makes two devs accountable **to each other**; this module extends
that to the person paying, without making him learn the vocabulary.

Four decisions carry the whole module:

**THE CLIENT'S LANGUAGE IS OUTCOMES; THE TEAM'S IS WORK.** A deliverable is the
owner's own sentence — *"a contact form that emails me"* — and carries NO role, NO
seat and NO decomposition. Asking him which half is frontend would be modelling his
domain in the team's vocabulary, and it buys nothing at the only moment it could pay
off: verification asks *"does the contact form work"*, never who built which half.
Decomposing an outcome into work is what todos already are, and ``todo_deliverables``
is the translation between the two registers (:func:`link_todo`).

**THE AGREEMENT IS THE LIST, NOT EACH DELIVERABLE.** One contract-shaped object,
versioned and signed as a unit: every builder accepts the whole list in ONE call, and
a push-back names the offending item. Per-deliverable versioning would only earn its
keep if deliverables could proceed independently — they cannot, because the gate is
all-or-nothing — so it would be five objects doing one object's job. Per-deliverable
acceptance would be worse: five deliverables across three devs is fifteen calls to
agree one list.

**THE OWNER AUTHORS; HE DOES NOT ACCEPT.** He wrote the words, and quizzing an author
on his own words is theatre (the same reason a host is exempt from a guidelines
assessment he wrote himself). Only builders accept, and ALL of them must, because the
argument *"three pages with bespoke components isn't feasible"* is only worth having
BEFORE anyone builds. Blocking is the feature.

**AFTER THE LOCK: WITHDRAW ONLY, NEVER ADD.** Scope may shrink — reducing it asks
nothing new of anyone, so a withdrawal does not even reopen the agreement — but it may
not grow. More scope is a NEW ENGAGEMENT, not an amendment, which is how statements of
work behave in the real world, and the refusal says so rather than merely refusing.
That is what makes this fair in BOTH directions: the owner is protected from *"we
built it"* when nothing was built, and the dev from *"can you also just add…"*.

A consequence worth stating: **an engagement is a milestone, not a product.** Since
scope cannot grow after the lock, a twenty-deliverable engagement is a trap. That is
guidance and not a limit — the broker cannot know how much scope is too much — so it
lives in the docs and the pitch, never in a MAX here.

**EVERYTHING IS GATED ON ``tasks.mode == 'engagement'``.** A ``contract`` or ``debug``
task has no deliverables and is completely unaffected: every write here refuses on
one, and :func:`deliverables_locked` — the gate the todo/contract paths call — answers
True for them, because a peer session has no gate to open.

**What is deliberately NOT enforced here: observability.** *"Set up the database"* is
a task, not a deliverable; *"the app stores and retrieves users on staging"* is. That
rule is real and it matters (verification has to have something to go and check), but
it is judged by the OWNER'S AGENT, which interviews him and drafts the words — the
broker does not do NLP, and the refusal must land on the agent rather than on a
non-technical human reading ``ERROR: deliverable not observable``.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import seats as _seats
from . import service
from .identity import Identity

# The role TYPE that makes a seat the client rather than a builder. A TYPE and not a
# handle: the owner's seat may be called `@client` or `@acme`, and what marks it is
# the kind of participant it is, exactly as `frontend` marks the frontends.
OWNER_ROLE = _seats.OWNER_ROLE   # re-exported; seats.py owns the cast

ACCEPTED = "accepted"
PUSHED_BACK = "pushed_back"

# One sentence in the owner's words. Long enough for a real outcome, short enough that
# a spec-sized paragraph is visibly the wrong shape for this field — that belongs in
# the todo's scope, which is the team's register.
MAX_TEXT = 500

# The refusal for "more scope after the lock", verbatim from the design. It says what
# to do instead, because a bare refusal here reads as the broker being obstructive
# when it is actually describing how commissioned work already behaves.
LOCKED_ADD = "scope is locked; start a new engagement for additional work"


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# the cast: who owns, who builds
# --------------------------------------------------------------------------- #
def is_engagement(conn, task_id: str) -> bool:
    """Is this commissioned work? The switch every rule in this module hangs off."""
    row = conn.execute("SELECT mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task '{task_id}'")
    return (row["mode"] or "contract") == "engagement"


def owner_seats(conn, task_id: str) -> list[str]:
    """Every declared seat whose ROLE TYPE is ``owner`` — normally exactly one.

    Delegates to :mod:`seats`, which owns the cast. Three modules needed this answer
    and three copies is how they drift apart.
    """
    return _seats.owner_handles(conn, task_id)


def owner_seat(conn, task_id: str) -> str | None:
    """The owner's seat handle, or ``None`` if the cast declares no owner."""
    found = owner_seats(conn, task_id)
    return found[0] if found else None


def builder_seats(conn, task_id: str) -> list[str]:
    """Every DECLARED seat that is not the owner — the signatory set for the list.

    Delegates to :func:`seats.builder_handles`, where the reasoning for *declared*
    rather than *joined* lives.
    """
    return _seats.builder_handles(conn, task_id)


def _assert_engagement(conn, task_id: str, action: str) -> None:
    """Refuse every write in this module on a peer task.

    Deliverables are commissioned work. A `contract` or `debug` task has no client, no
    scope to agree and no gate — and must stay behaviourally identical to before this
    feature existed, which is why every write starts here rather than trusting callers
    to check the mode themselves.
    """
    if not is_engagement(conn, task_id):
        row = conn.execute("SELECT mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
        mode = (row["mode"] or "contract") if row else "contract"
        raise ValueError(
            f"deliverables are ENGAGEMENT mode only, and '{task_id}' is a '{mode}' task "
            f"— so there is nobody to {action} for. Deliverables are what a client "
            f"commissioned; a peer task agrees its work through todos and the contracts "
            f"on them instead."
        )


def _assert_owner(conn, identity: Identity, action: str) -> None:
    """Only the OWNER writes the list. It is his scope and his words."""
    owners = owner_seats(conn, identity.task_id)
    if not owners:
        raise ValueError(
            f"this engagement declares no owner seat, so nobody can {action}. An "
            f"engagement's cast needs a seat with the role type '{OWNER_ROLE}' — the "
            f"client, joined by an ordinary invite like everyone else."
        )
    if identity.role not in owners:
        raise ValueError(
            f"only the owner ({', '.join('@' + o for o in owners)}) can {action} — "
            f"deliverables are what the CLIENT commissioned, in his words. You "
            f"('{identity.role}') are building against them: accept the list with "
            f"accept_deliverables(), or push_back(#N, reason) if one of them cannot "
            f"be built or cannot be checked."
        )


def _assert_builder(conn, identity: Identity, action: str) -> None:
    """Only a BUILDER accepts or pushes back — never the owner, who wrote the words."""
    if identity.role in owner_seats(conn, identity.task_id):
        raise ValueError(
            f"you wrote this list, so you do not {action} it — the builders do. "
            f"Quizzing an author on his own words proves nothing. What you can still "
            f"do is reword one (revise_deliverable) or drop one "
            f"(withdraw_deliverable)."
        )
    builders = builder_seats(conn, identity.task_id)
    if identity.role not in builders:
        raise ValueError(
            f"you ('{identity.role}') are not a seat on this engagement, so you cannot "
            f"{action} its deliverables. It is built by "
            f"{', '.join('@' + b for b in builders) or 'nobody yet'}."
        )


def _assert_text(text: object) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError(
            "a deliverable needs text — one outcome, in the OWNER's words "
            "(\"a contact form on the landing page that emails me\"). Not a task, and "
            "not a role: what should a person be able to do when this is finished?"
        )
    if len(text) > MAX_TEXT:
        raise ValueError(
            f"a deliverable must be at most {MAX_TEXT} characters — it is one outcome "
            f"in the owner's words, not a spec. The detail belongs in the todos that "
            f"serve it, which are the team's register."
        )
    return text


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def _rows(conn, task_id: str) -> list:
    return conn.execute(
        "SELECT * FROM deliverables WHERE task_id = ? ORDER BY number", (task_id,)
    ).fetchall()


def get_row(conn, task_id: str, number: int):
    """One deliverable by the HUMAN's ``#N``, SCOPED to ``task_id``.

    The same split todos make: ``deliverables.number`` is what a person types (per
    task, from 1), ``deliverables.id`` is what every foreign key in the schema points
    at. Nothing a human typed ever reaches an id — and the owner's own receipt file is
    named after the NUMBER (``D2-contact-form.md``), so it must still name this row
    years later.
    """
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise ValueError(f"deliverable number must be a number, got {number!r}") from None
    row = conn.execute(
        "SELECT * FROM deliverables WHERE task_id = ? AND number = ?", (task_id, number)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"no deliverable #{number} on task '{task_id}'. Deliverables are numbered "
            f"PER TASK from #1 — call get_deliverables() for the list."
        )
    return row


def next_number(conn, task_id: str) -> int:
    """``MAX(number) + 1``. MAX and never COUNT: numbers are NEVER REUSED.

    Withdraw #2 and the next deliverable is #4, because ``#2`` is already quoted in
    the thread, in the event log and in the filename of the owner's receipt.
    """
    return conn.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM deliverables WHERE task_id = ?",
        (task_id,),
    ).fetchone()["n"]


def current_list(conn, task_id: str):
    """The live ``deliverable_lists`` row — the highest version — or ``None``.

    Highest is current because a version is only ever minted BEFORE the lock: once
    locked, nothing may mint another (adds and rewordings are refused), so the top of
    the pile is both the newest and the one that locked.
    """
    return conn.execute(
        "SELECT * FROM deliverable_lists WHERE task_id = ? ORDER BY version DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def decisions(conn, list_id: int) -> dict[str, dict]:
    """``{seat: {decision, deliverable, reason, at}}`` for ONE list version.

    Keyed by list_id, which is why a revision "resets" everything for free: a new
    version is a new row, and a new row has no decisions on it. Nothing is deleted,
    so v1's acceptances are still there to read — people DID agree to those words.
    """
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT d.role, d.decision, d.reason, d.created_at, t.number AS number "
        "FROM deliverable_decisions d "
        "LEFT JOIN deliverables t ON t.id = d.deliverable_id "
        "WHERE d.list_id = ?",
        (list_id,),
    ).fetchall():
        out[r["role"]] = {
            "decision": r["decision"],
            "deliverable": r["number"],
            "reason": r["reason"],
            "at": r["created_at"],
        }
    return out


def to_dict(conn, row) -> dict:
    """The wire shape of ONE deliverable, in the owner's register.

    Carries BOTH ids for the same reason a todo does: ``number`` is what humans and
    agents type, ``id`` is what ``todo_deliverables``, ``specs`` and
    ``verification_results`` join on.

    ``todos`` is the translation back into the team's register — the work that serves
    this outcome — and is what makes coverage answerable without a second query.
    """
    return {
        "id": row["id"],
        "number": row["number"],
        "text": row["text"],
        "withdrawn": row["withdrawn_at"] is not None,
        "withdrawn_at": row["withdrawn_at"],
        "withdraw_reason": row["withdraw_reason"],
        "created_at": row["created_at"],
        "todos": [
            r["number"]
            for r in conn.execute(
                "SELECT t.number FROM todo_deliverables td JOIN todos t ON t.id = td.todo_id "
                "WHERE td.deliverable_id = ? ORDER BY t.number",
                (row["id"],),
            ).fetchall()
        ],
    }


def get_deliverables(conn, task_id: str) -> list[dict]:
    """Every deliverable on the task, withdrawn ones INCLUDED and flagged.

    Withdrawn rows are never hidden: *"I never asked for that"* after work has started
    is precisely the dispute this exists to settle, and a list that quietly forgets an
    item cannot settle it.
    """
    return [to_dict(conn, r) for r in _rows(conn, task_id)]


def record(conn, task_id: str) -> dict:
    """The whole engagement's scope — the ARCHIVABLE RECORD.

    Shaped so the owner's agent TRANSCRIBES rather than composes: the exact accepted
    text, who accepted it, when, and the broker's own ids. His receipt folder (one
    markdown file per deliverable, on HIS machine) is then uniform and mechanically
    comparable later, instead of prose that has to be diffed by eye.

    The rule that stops the two records rotting: the BROKER is authoritative for what
    is true NOW; the folder is authoritative for what was agreed THEN. Backwards, and
    the owner's agent reads its own stale files and quietly drifts.
    """
    lst = current_list(conn, task_id)
    builders = builder_seats(conn, task_id)
    d = decisions(conn, lst["id"]) if lst is not None else {}
    return {
        "task_id": task_id,
        "engagement": is_engagement(conn, task_id),
        "owner": owner_seat(conn, task_id),
        "builders": builders,
        "list_id": (lst["id"] if lst is not None else None),
        "version": (lst["version"] if lst is not None else None),
        "locked": (lst is not None and lst["locked_at"] is not None),
        "locked_at": (lst["locked_at"] if lst is not None else None),
        "accepted_by": sorted(b for b in builders if d.get(b, {}).get("decision") == ACCEPTED),
        "pushed_back_by": {
            b: {"deliverable": d[b]["deliverable"], "reason": d[b]["reason"]}
            for b in builders
            if d.get(b, {}).get("decision") == PUSHED_BACK
        },
        "awaiting": [b for b in builders if d.get(b, {}).get("decision") != ACCEPTED],
        "deliverables": get_deliverables(conn, task_id),
    }


# --------------------------------------------------------------------------- #
# THE GATE
# --------------------------------------------------------------------------- #
def deliverables_locked(conn, task_id: str) -> bool:
    """Is this task allowed to BUILD? The one helper the todo/contract paths call.

    **True for a ``contract`` or ``debug`` task, always.** Read the name as "is the
    gate open", not "does a locked row exist": a peer session has no deliverables and
    no gate, and every engagement rule is gated on ``tasks.mode`` precisely so that
    those sessions behave exactly as they did before this module existed. Returning
    False for them would brick every peer task on the broker.

    For an engagement it is True only once EVERY builder has accepted the current
    list. Until then: no todos, no contracts — but messaging stays open, and that is
    load-bearing rather than a softening. Pushing back is a CONVERSATION; gate the
    talking and a dev who thinks deliverable 2 is unbuildable cannot say so, and the
    session deadlocks on the exact discussion it exists to have.
    """
    if not is_engagement(conn, task_id):
        return True
    lst = current_list(conn, task_id)
    return lst is not None and lst["locked_at"] is not None


def assert_can_build(conn, task_id: str, action: str = "do this") -> None:
    """Raise the gate's refusal, naming the fix. No-op on a non-engagement task."""
    if deliverables_locked(conn, task_id):
        return
    lst = current_list(conn, task_id)
    if lst is None:
        raise ValueError(
            f"this engagement has no deliverables yet, so you cannot {action}. The "
            f"OWNER sets them first — what he commissioned, in his own words — and "
            f"the team accepts the list before any work starts. Nothing is blocked "
            f"about talking: use send_message() while that is being agreed."
        )
    rec = record(conn, task_id)
    waiting = ", ".join("@" + b for b in rec["awaiting"])
    raise ValueError(
        f"the deliverable list (v{rec['version']}) is not agreed yet, so you cannot "
        f"{action}. Every builder accepts it before any work starts — still waiting "
        f"on {waiting}. Accept it with accept_deliverables(), or push_back(#N, reason) "
        f"if something cannot be built or cannot be checked. Messaging stays open."
    )


# --------------------------------------------------------------------------- #
# writes — the owner authors
# --------------------------------------------------------------------------- #
def propose_deliverables(conn, identity: Identity, texts: list[str]) -> dict:
    """The owner sets the scope: N deliverables and the list's v1.

    His agent interviews him and drafts these; he approves in his own words. He never
    types one into a form, which is why strictness here is safe — the refusal lands on
    the agent, which translates it, not on a non-technical human.

    Deliberately the INITIAL set only. A second call is refused rather than quietly
    appending, because "propose the scope" and "add one more" are different moves with
    different consequences after the lock, and one tool doing both would make the
    dangerous one easy to reach by accident.
    """
    _assert_engagement(conn, identity.task_id, "propose deliverables")
    _assert_owner(conn, identity, "set the deliverables")

    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, (list, tuple)):
        raise ValueError("texts must be a list of deliverables, one outcome per entry")
    cleaned = [_assert_text(t) for t in texts if str(t or "").strip()]
    if not cleaned:
        raise ValueError(
            "an engagement needs at least one deliverable — it is the whole agreement, "
            "and nothing can be built or verified without it"
        )

    if _rows(conn, identity.task_id):
        rec = record(conn, identity.task_id)
        raise ValueError(
            f"this engagement already has deliverables (list v{rec['version']}). Add "
            f"one with add_deliverable(text) or reword one with "
            f"revise_deliverable(#N, text) — propose_deliverables sets the INITIAL "
            f"scope, once."
        )

    now = _now()
    numbers = []
    for text in cleaned:
        numbers.append(_insert(conn, identity.task_id, text, now))
    _mint_version(conn, identity.task_id)
    _event(conn, identity.task_id, "deliverables_proposed", None,
           {"by": identity.role, "numbers": numbers, "count": len(numbers)})
    conn.commit()

    listing = "; ".join(f"#{n} {t}" for n, t in zip(numbers, cleaned))
    builders = builder_seats(conn, identity.task_id)
    service.post_message(
        conn, identity, "deliverable_proposal",
        f"Here is what I'm commissioning, in my words — {len(numbers)} deliverable(s): "
        f"{listing}. This LIST is the agreement, and it binds "
        f"{', '.join(builders)}: read it with get_deliverables(), then "
        f"accept_deliverables() once — that accepts the whole list — or "
        f"push_back(#N, reason) naming the one that is wrong. Nothing can be built "
        f"until every builder has accepted. Talking is not blocked: if a deliverable "
        f"is unbuildable or impossible to check, say so now, before anyone starts.",
    )
    return record(conn, identity.task_id)


def add_deliverable(conn, identity: Identity, text: str) -> dict:
    """One more outcome — BEFORE the lock only.

    After the lock this is the refusal the whole feature turns on: more scope is a NEW
    ENGAGEMENT, not an amendment. That protects the devs from *"can you also just
    add…"* exactly as the locked list protects the owner from *"we built it"*.

    Before the lock it mints a NEW VERSION and clears every acceptance, because a
    builder who accepted three deliverables did not accept four. That costs nothing
    here — nobody is building yet, which is the entire point of gating first.
    """
    _assert_engagement(conn, identity.task_id, "add a deliverable")
    _assert_owner(conn, identity, "add a deliverable")
    text = _assert_text(text)
    _assert_unlocked(conn, identity.task_id, add=True)

    number = _insert(conn, identity.task_id, text, _now())
    version = _mint_version(conn, identity.task_id)
    _event(conn, identity.task_id, "deliverable_added", number,
           {"by": identity.role, "text": text, "version": version})
    conn.commit()
    service.post_message(
        conn, identity, "deliverable_proposal",
        f"Added deliverable #{number}: {text}. That makes it list v{version}, and "
        f"everyone's earlier acceptance is cleared — you agreed to a shorter list. "
        f"Read it again with get_deliverables() and accept_deliverables() if v{version} "
        f"is right.",
    )
    return record(conn, identity.task_id)


def revise_deliverable(conn, identity: Identity, number: int, text: str) -> dict:
    """Reword one deliverable — usually the answer to a push-back.

    Mints a NEW LIST VERSION and every builder accepts again. Earlier acceptances do
    NOT carry over: a revision means people agreed to different words, and carrying a
    signature across a change of wording is how signatures come to mean nothing.

    Before the lock only, like every other change to the words. Afterwards the team
    agreed to THIS text and is building against it — reduce the scope
    (:func:`withdraw_deliverable`) or start a new engagement.
    """
    _assert_engagement(conn, identity.task_id, "revise a deliverable")
    _assert_owner(conn, identity, "revise a deliverable")
    row = get_row(conn, identity.task_id, number)
    text = _assert_text(text)
    _assert_unlocked(conn, identity.task_id, add=False, number=row["number"])
    if row["withdrawn_at"] is not None:
        raise ValueError(
            f"deliverable #{row['number']} was withdrawn — rewording something you "
            f"took out of scope would put it back in by the side door. Add it again "
            f"with add_deliverable(text) if you want it after all."
        )
    if text == row["text"]:
        raise ValueError(
            f"deliverable #{row['number']} already reads exactly like that. A revision "
            f"mints a new version and clears every acceptance, so an identical one "
            f"would make the team re-accept for nothing."
        )

    was = row["text"]
    conn.execute("UPDATE deliverables SET text = ? WHERE id = ?", (text, row["id"]))
    version = _mint_version(conn, identity.task_id)
    _event(conn, identity.task_id, "deliverable_revised", row["number"],
           {"by": identity.role, "was": was, "text": text, "version": version})
    conn.commit()
    service.post_message(
        conn, identity, "deliverable_proposal",
        f"Revised deliverable #{row['number']} — it now reads: {text} (was: {was}). "
        f"That is list v{version}, and every earlier acceptance is cleared, because "
        f"you agreed to different words. accept_deliverables() again if v{version} is "
        f"right, or push_back(#N, reason) if it still isn't.",
    )
    return record(conn, identity.task_id)


def withdraw_deliverable(conn, identity: Identity, number: int, reason: str) -> dict:
    """Take one out of scope. The owner's ONLY move after the lock.

    Scope may shrink because reducing it asks nothing new of anyone — so this
    deliberately does NOT mint a version and does NOT reopen the agreement. A locked
    list stays locked and the team keeps working; unlocking on a withdrawal would stop
    every other deliverable dead over an item nobody has to build any more.

    Kept as a timestamp and a reason rather than a DELETE, and it stays visible in
    :func:`get_deliverables`, because *"I never asked for that"* after work has started
    is exactly the dispute this record exists to settle.
    """
    _assert_engagement(conn, identity.task_id, "withdraw a deliverable")
    _assert_owner(conn, identity, "withdraw a deliverable")
    row = get_row(conn, identity.task_id, number)
    if row["withdrawn_at"] is not None:
        raise ValueError(f"deliverable #{row['number']} is already withdrawn")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(
            "withdrawing needs a reason — someone may already be building it, and "
            "they will read this instead of finding the work gone"
        )
    service.assert_content_size(reason, "withdraw reason")

    conn.execute(
        "UPDATE deliverables SET withdrawn_at = ?, withdraw_reason = ? WHERE id = ?",
        (_now(), reason, row["id"]),
    )
    _event(conn, identity.task_id, "deliverable_withdrawn", row["number"],
           {"by": identity.role, "reason": reason, "text": row["text"]})
    conn.commit()
    service.post_message(
        conn, identity, "deliverable_withdraw",
        f"Withdrawing deliverable #{row['number']} ({row['text']}): {reason}. It is out "
        f"of scope from now on and nothing further is expected against it — if you are "
        f"mid-work on a todo that serves it, stop and check get_deliverables(). The "
        f"rest of the list is unchanged and still agreed.",
    )
    return record(conn, identity.task_id)


# --------------------------------------------------------------------------- #
# writes — the builders agree
# --------------------------------------------------------------------------- #
def accept_deliverables(conn, identity: Identity) -> dict:
    """A builder accepts the WHOLE current list, in one call. Locks it when all have.

    One signature per builder per VERSION — never one per deliverable per person,
    which for five deliverables across three devs is fifteen calls to agree one list.

    The owner is refused here, and that is not an oversight: he authored the words.
    """
    _assert_engagement(conn, identity.task_id, "accept deliverables")
    _assert_builder(conn, identity, "accept")

    lst = current_list(conn, identity.task_id)
    if lst is None:
        raise ValueError(
            "there are no deliverables to accept yet — the owner sets the scope first, "
            "in his own words. Ask him with send_message('question', ...); messaging is "
            "never gated."
        )
    if lst["locked_at"] is not None:
        raise ValueError(
            f"the deliverable list (v{lst['version']}) is already LOCKED — every "
            f"builder accepted it and work has begun. There is nothing left to accept."
        )
    if not _live_rows(conn, identity.task_id):
        raise ValueError(
            "every deliverable on this engagement has been withdrawn, so there is no "
            "scope to accept. The owner adds one with add_deliverable(text)."
        )

    _decide(conn, lst["id"], identity, ACCEPTED, None, None)
    _event(conn, identity.task_id, "deliverables_accepted", None,
           {"by": identity.role, "version": lst["version"]})
    conn.commit()

    rec = record(conn, identity.task_id)
    if rec["awaiting"]:
        service.post_message(
            conn, identity, "deliverable_accept",
            f"Accepted the deliverable list v{lst['version']} — all "
            f"{len(rec['deliverables'])} of them, as written. Still waiting on "
            f"{', '.join(rec['awaiting'])} before anything can be built.",
        )
        return rec

    conn.execute(
        "UPDATE deliverable_lists SET locked_at = ? WHERE id = ?", (_now(), lst["id"])
    )
    _event(conn, identity.task_id, "deliverables_locked", None,
           {"version": lst["version"], "builders": rec["builders"]})
    conn.commit()
    service.post_message(
        conn, identity, "deliverable_accept",
        f"Accepted the deliverable list v{lst['version']} — every builder "
        f"({', '.join(rec['builders'])}) has now agreed it, so the list is LOCKED and "
        f"work may begin. From here the scope can only SHRINK: the owner may withdraw "
        f"a deliverable, but nothing can be added — more scope is a new engagement. "
        f"Next is the team's own register: propose todos, each naming the "
        f"deliverable(s) it serves, and contract them as usual.",
    )
    return record(conn, identity.task_id)


def push_back(conn, identity: Identity, number: int, reason: str) -> dict:
    """A builder refuses the list, NAMING the one deliverable that is wrong.

    Acceptance covers the whole list; a rejection must be specific, or the owner has
    nothing to act on. *"Too vague to check"* against #2 is answerable; *"no"* is not.

    This is the conversation the gate exists to force. *"Three pages with bespoke
    components isn't feasible"* is only worth saying BEFORE anyone builds, and a single
    push-back holds the whole list, because deliverables cannot proceed independently.
    """
    _assert_engagement(conn, identity.task_id, "push back on a deliverable")
    _assert_builder(conn, identity, "push back on")
    row = get_row(conn, identity.task_id, number)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(
            "push_back needs a reason — the owner has to know what to change, and he "
            "does not speak your register. Say what cannot be built, or what cannot be "
            "checked from outside."
        )
    service.assert_content_size(reason, "push-back reason")

    lst = current_list(conn, identity.task_id)
    if lst is None:
        raise ValueError("there are no deliverables to push back on yet")
    if lst["locked_at"] is not None:
        raise ValueError(
            f"the deliverable list (v{lst['version']}) is already LOCKED — you accepted "
            f"it and work has begun, so it is too late to push back. Say so with "
            f"send_message(...): the owner can WITHDRAW #{row['number']} if it turns out "
            f"to be wrong, and that is the only change left to the scope."
        )

    _decide(conn, lst["id"], identity, PUSHED_BACK, row["id"], reason)
    _event(conn, identity.task_id, "deliverable_pushed_back", row["number"],
           {"by": identity.role, "reason": reason, "version": lst["version"]})
    conn.commit()
    service.post_message(
        conn, identity, "deliverable_pushback",
        f"Pushing back on deliverable #{row['number']} ({row['text']}): {reason}. The "
        f"list v{lst['version']} is not agreed and nothing can be built until it is. "
        f"revise_deliverable({row['number']}, text) — that issues a new version and "
        f"everyone accepts again — or withdraw it if it isn't needed. Happy to talk it "
        f"through first.",
        to_role=owner_seat(conn, identity.task_id),
    )
    return record(conn, identity.task_id)


# --------------------------------------------------------------------------- #
# the two registers: which work serves which outcome
# --------------------------------------------------------------------------- #
def link_todo(conn, todo_id: int, numbers: list[int]) -> list[int]:
    """Bind a todo to the deliverable(s) it serves. Returns the numbers linked.

    MANY-to-many, because *"CI for the landing page and the contact form"* is a real
    case and needs no exception. Takes the internal ``todos.id`` (its caller is holding
    a row it just wrote) and the HUMAN's deliverable numbers (its caller was given
    those by a person).

    This link is what makes the two registers one record: the owner reads outcomes,
    the devs read work, and coverage plus a spec's contract-version stamp are both
    walks over this table. An optional link would make both silently partial — which
    is why it is REQUIRED for deliverable work, enforced where todos are created.
    Internal work (repo setup, CI, a refactor) links to nothing, on purpose: it serves
    no outcome the client asked for, and inventing a standing "groundwork" deliverable
    would put something in the owner's list that he never wrote.

    Idempotent: re-linking an existing pair is a no-op, never an error.
    """
    todo = conn.execute(
        "SELECT id, task_id, number FROM todos WHERE id = ?", (int(todo_id),)
    ).fetchone()
    if todo is None:
        raise ValueError(f"no todo with id {todo_id}")
    _assert_engagement(conn, todo["task_id"], "link a todo to a deliverable")

    if isinstance(numbers, (int, str)):
        numbers = [numbers]
    if not isinstance(numbers, (list, tuple)):
        raise ValueError("numbers must be a list of deliverable numbers, e.g. [1, 3]")
    linked: list[int] = []
    for number in numbers:
        row = get_row(conn, todo["task_id"], number)
        if row["withdrawn_at"] is not None:
            raise ValueError(
                f"deliverable #{row['number']} was withdrawn — it is out of scope, so no "
                f"todo should serve it. Link this work to a live deliverable, or mark "
                f"the todo internal if it is the team's own housekeeping."
            )
        conn.execute(
            "INSERT OR IGNORE INTO todo_deliverables (todo_id, deliverable_id) VALUES (?,?)",
            (todo["id"], row["id"]),
        )
        linked.append(row["number"])
    conn.commit()
    return linked


def deliverables_of_todo(conn, todo_id: int) -> list[dict]:
    """The outcome(s) one todo serves — empty for INTERNAL work, and that is correct.

    An internal todo carries no verification spec (nobody verifies a refactor by
    opening a browser) and is excluded from coverage, visibly: *"3 deliverables · 5
    todos · 2 internal"*, so a clean coverage number never implies that work outside
    the count does not exist.
    """
    rows = conn.execute(
        "SELECT d.* FROM todo_deliverables td JOIN deliverables d ON d.id = td.deliverable_id "
        "WHERE td.todo_id = ? ORDER BY d.number",
        (int(todo_id),),
    ).fetchall()
    return [to_dict(conn, r) for r in rows]


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _live_rows(conn, task_id: str) -> list:
    return [r for r in _rows(conn, task_id) if r["withdrawn_at"] is None]


def _assert_unlocked(conn, task_id: str, *, add: bool, number: int | None = None) -> None:
    """The post-lock refusal — the one that has to EXPLAIN rather than merely refuse."""
    lst = current_list(conn, task_id)
    if lst is None or lst["locked_at"] is None:
        return
    if add:
        raise ValueError(
            f"the deliverable list (v{lst['version']}) is locked — {LOCKED_ADD}. Every "
            f"builder agreed to exactly this scope and is working against it; an "
            f"amendment would be a scope change nobody signed. You can still WITHDRAW a "
            f"deliverable — scope may shrink, because that asks nothing new of anyone."
        )
    raise ValueError(
        f"the deliverable list (v{lst['version']}) is locked, so deliverable "
        f"#{number} cannot be reworded — the team accepted those exact words and is "
        f"building against them. Withdraw it with "
        f"withdraw_deliverable({number}, reason) if it is wrong, or {LOCKED_ADD}."
    )


def _insert(conn, task_id: str, text: str, now: float) -> int:
    """Insert one deliverable at ``MAX(number)+1`` and return its NUMBER.

    The number is allocated in the same transaction as the INSERT that consumes it;
    the ``UNIQUE(task_id, number)`` constraint is the backstop if two proposals race,
    and the retry re-reads the maximum.
    """
    for _attempt in range(6):
        number = next_number(conn, task_id)
        try:
            conn.execute(
                "INSERT INTO deliverables (task_id, number, text, created_at) "
                "VALUES (?,?,?,?)",
                (task_id, number, text, now),
            )
            return number
        except sqlite3.IntegrityError:
            conn.rollback()
    raise ValueError("could not allocate a deliverable number — please retry")


def _mint_version(conn, task_id: str) -> int:
    """A NEW list version — which is the whole of "acceptances are reset".

    Decisions hang off ``list_id``, so a fresh row starts with none and the earlier
    version's acceptances stay on it, unedited: people DID agree to those words, and
    the event log plus the owner's receipt are what say so afterwards.
    """
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM deliverable_lists WHERE task_id = ?",
        (task_id,),
    ).fetchone()["v"]
    conn.execute(
        "INSERT INTO deliverable_lists (task_id, version, created_at) VALUES (?,?,?)",
        (task_id, version, _now()),
    )
    return version


def _decide(conn, list_id: int, identity: Identity, decision: str,
            deliverable_id: int | None, reason: str | None) -> None:
    """Upsert this builder's decision on this VERSION — accept after push-back wins."""
    conn.execute(
        "INSERT INTO deliverable_decisions (list_id, role, agent_id, decision, "
        "deliverable_id, reason, created_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(list_id, role) DO UPDATE SET decision = excluded.decision, "
        "deliverable_id = excluded.deliverable_id, reason = excluded.reason, "
        "agent_id = excluded.agent_id, created_at = excluded.created_at",
        (list_id, identity.role, identity.agent_id, decision, deliverable_id,
         reason, _now()),
    )


def _event(conn, task_id: str, action: str, number: int | None, detail: dict) -> None:
    """Append a ``deliverable`` event — one kind, the specific action inside it, the
    same shape ``todos._event`` uses so the dashboard's kind filter keeps a fixed
    vocabulary.

    The NUMBER is recorded and not the id, because the log PRINTS "#2" and it has to
    be the same "#2" the owner typed, quoted in his receipt and in whatever the humans
    said to each other about it.
    """
    conn.execute(
        "INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
        (task_id, "deliverable",
         json.dumps({"action": action, "deliverable_number": number, **detail}),
         _now()),
    )
