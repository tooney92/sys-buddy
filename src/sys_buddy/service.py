"""Messaging core — the agent_bus.py port, with its three known bugs fixed.

Pure-ish functions that take an open connection and a resolved ``Identity``. The
FastMCP tool wrappers in ``tools.py`` are thin shells over these; keeping the logic
here means we can spec it without a running server.

Bugs fixed vs the predecessor (SPEC §14 / reference/NOTE.md):
1. WAL — handled in ``db.connect`` (poll loop no longer contends).
2. ``notify_human`` error handling — see ``slack.py`` (built in step 8).
3. delivered vs acked — the predecessor marked messages read *on fetch*, so a
   crashed session lost them. Here a fetch stamps ``delivered_at`` only; the agent
   must ``ack_messages(ids)`` after processing. Until acked, a message keeps coming
   back — a dropped tunnel mid-fetch can't silently eat it.
"""

from __future__ import annotations

import html
import json
import time

from .identity import Identity


# --------------------------------------------------------------------------- #
# local-mode identity (self-declared, auto-provisioned on loopback)
# --------------------------------------------------------------------------- #
def ensure_local_identity(conn, task_id: str, agent_name: str) -> Identity:
    """Local mode only: make sure the task and this agent exist, then return it.

    Zero-friction: on loopback an agent just names itself and starts talking. The
    name doubles as the role (fine on a single developer's machine). Remote mode
    never calls this — identity is stamped from the token by the middleware.
    """
    row = conn.execute("SELECT roles_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    now = time.time()
    if row is None:
        conn.execute(
            "INSERT INTO tasks (id, title, state, roles_json, created_at) VALUES (?,?,?,?,?)",
            (task_id, task_id, "open", json.dumps([agent_name]), now),
        )
    else:
        roles = json.loads(row["roles_json"])
        if agent_name not in roles:
            roles.append(agent_name)
            conn.execute("UPDATE tasks SET roles_json = ? WHERE id = ?", (json.dumps(roles), task_id))

    agent = conn.execute(
        "SELECT id, task_id, name, role FROM agents WHERE task_id = ? AND role = ?",
        (task_id, agent_name),
    ).fetchone()
    if agent is None:
        cur = conn.execute(
            "INSERT INTO agents (task_id, name, role, token_hash, created_at) VALUES (?,?,?,?,?)",
            (task_id, agent_name, agent_name, None, now),
        )
        conn.commit()
        return Identity(agent_id=cur.lastrowid, task_id=task_id, name=agent_name, role=agent_name)
    conn.commit()
    return Identity(agent_id=agent["id"], task_id=agent["task_id"], name=agent["name"], role=agent["role"])


# --------------------------------------------------------------------------- #
# messaging
# --------------------------------------------------------------------------- #
# Message types the broker produces as a side effect of report_status — they carry
# lifecycle meaning (a deploy, a counted test result, a terminal state). Agents must
# NOT post them via send_message: a free-form test_result would desync the dashboard's
# broker-counted strike total, and a free-form verified/deploy_confirmed would forge a
# lifecycle event that never happened. They go through report_status only.
RESERVED_TYPES = frozenset({"deploy_confirmed", "test_result", "verified", "stuck"})

# The conversational types an agent may send via send_message. A positive
# allow-list (not just blocking RESERVED_TYPES) also stops an agent forging
# broker-authoritative chips like 'contract_lock' in the human dashboard thread.
ALLOWED_SEND_TYPES = frozenset({"question", "answer", "status_update", "contract_proposal"})

# Types the BROKER itself authors — not peer content at all. They are pushed onto the
# message queue (the only channel a parked wait_for_message reads) so an agent is woken
# by a broker fact instead of having to poll for it. Two consequences, both deliberate:
#   * they are wrapped in the BROKER envelope, not the peer <msg trust="external"> one
#     (see _wrap_broker) — the framing must never claim a peer said this;
#   * no agent can ever send one (assert_sendable rejects them, and they're outside
#     ALLOWED_SEND_TYPES), so a lock notification cannot be forged.
BROKER_TYPES = frozenset({"contract_locked"})
BROKER_NAME = "sys-buddy"   # the author shown for a broker-authored notification
BROKER_ROLE = "broker"      # …and its role, in both the agent view and the dashboard

# An unbounded body is a DoS AND a prompt-injection amplifier: it is persisted and
# redelivered to the peer on every poll until acked, stuffing the peer LLM's context
# and token budget (SPEC §7). Cap all agent-supplied content at the broker.
MAX_CONTENT_BYTES = 64 * 1024


def assert_content_size(text: str, label: str = "message") -> None:
    """Reject agent-supplied content larger than MAX_CONTENT_BYTES."""
    if text is not None and len(str(text).encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError(
            f"{label} exceeds the {MAX_CONTENT_BYTES // 1024} KB limit — shorten it "
            f"or split it across messages"
        )


def assert_sendable(mtype: str) -> None:
    """Gate the send_message path: only conversational types, never lifecycle ones
    or forged broker chips (SPEC §7/§8)."""
    if mtype in RESERVED_TYPES:
        raise ValueError(
            f"'{mtype}' is a lifecycle event — report it via report_status(...), "
            f"not send_message"
        )
    if mtype in BROKER_TYPES:
        raise ValueError(
            f"'{mtype}' is authored by the broker itself — you cannot send one; "
            f"the broker emits it when the underlying fact actually happens"
        )
    if mtype not in ALLOWED_SEND_TYPES:
        raise ValueError(
            f"'{mtype}' is not a valid message type; use one of "
            f"{sorted(ALLOWED_SEND_TYPES)}"
        )


def _count_other_agents(conn, task_id: str, exclude_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM agents WHERE task_id = ? AND id != ? AND revoked_at IS NULL",
        (task_id, exclude_id),
    ).fetchone()["n"]


def post_message(conn, identity: Identity, mtype: str, body: str, to_role: str | None = None) -> dict:
    """Store a message from ``identity`` on its task. Returns a small receipt.

    Delivery rows are created lazily on fetch, so an agent that pairs later still
    picks up anything it hasn't acked.

    ``to_role`` directs the message at a single role; None/empty broadcasts to all
    other agents on the task (the unchanged default). A non-empty ``to_role`` must
    name a role declared on the task.
    """
    task = conn.execute(
        "SELECT state, closed_at, roles_json FROM tasks WHERE id = ?", (identity.task_id,)
    ).fetchone()
    if task is None:
        raise ValueError(f"unknown task '{identity.task_id}'")
    if task["closed_at"] is not None:
        raise ValueError(f"task '{identity.task_id}' is closed")
    # Empty string means broadcast, same as None.
    if not to_role:
        to_role = None
    elif to_role not in json.loads(task["roles_json"]):
        raise ValueError(
            f"cannot address '{to_role}' — not a role on task '{identity.task_id}'"
        )
    assert_content_size(body, "message body")
    state_at_send = task["state"]
    now = time.time()
    cur = conn.execute(
        "INSERT INTO messages (task_id, from_agent_id, type, body_json, to_role, state_at_send, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (identity.task_id, identity.agent_id, mtype, json.dumps(body), to_role, state_at_send, now),
    )
    conn.commit()
    recipients = _count_other_agents(conn, identity.task_id, identity.agent_id)
    return {"id": cur.lastrowid, "recipients": recipients, "type": mtype, "to_role": to_role}


def _wrap(from_name: str, role: str, task_id: str, body: str, to_role: str | None = None) -> str:
    """The untrusted-content envelope (SPEC §7). Content is DATA, not instructions.

    Every interpolated value is HTML-escaped so attacker-controlled content can't
    break out of the envelope. Without this, a body containing ``</msg>`` could
    close the wrapper early and inject a forged ``trust="internal"`` block that the
    receiving agent would read as trusted in-band instructions — the exact hijack
    the envelope exists to prevent. Agent name/role are chosen by the buddy at
    pairing time, so the attributes are escaped too (``quote=True``).
    """
    attr = lambda v: html.escape(str(v), quote=True)
    to_attr = f' to="{attr(to_role)}"' if to_role else ""
    return (
        f'<msg from="{attr(from_name)}" role="{attr(role)}"{to_attr} trust="external" task="{attr(task_id)}">\n'
        f"{html.escape(str(body), quote=False)}\n"
        f"</msg>"
    )


def _wrap_broker(task_id: str, body: str) -> str:
    """The BROKER envelope — for the notifications the broker authors itself.

    ``_wrap`` frames *peer* content as external DATA. A ``contract_locked`` push is
    not peer content: it is the enforcing broker stating a fact about the task's own
    state (a fact it just wrote to the contracts table). Reusing the peer envelope
    would be dishonest in BOTH directions — it would attribute broker words to an
    agent, and it would tell the reader to treat the broker's own statement as
    untrusted external chatter.

    It still cannot be forged. Message bodies are HTML-escaped by both wrappers, so
    peer content can never emit a real ``<broker>`` tag, and ``assert_sendable``
    refuses ``BROKER_TYPES`` on the public send path.
    """
    attr = lambda v: html.escape(str(v), quote=True)
    return (
        f'<broker from="{attr(BROKER_NAME)}" trust="broker" task="{attr(task_id)}">\n'
        f"{html.escape(str(body), quote=False)}\n"
        f"</broker>"
    )


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fetch(conn, identity: Identity, only_new: bool, mark_delivered: bool = True) -> list[dict]:
    """Return messages on my task, from other agents.

    Two modes, deliberately different (this split is what keeps long-poll working
    while staying crash-safe):

    - ``only_new=False`` (check_messages): everything I haven't *acked*. This is the
      recovery path — a message keeps coming back until I ack it, so a crash mid-
      fetch can't lose it.
    - ``only_new=True`` (wait_for_message): only messages not yet *delivered* to me.
      A parked agent wakes on genuinely new mail and then goes back to sleep,
      instead of busy-spinning on backlog it has already seen but not acked.

    Both stamp ``delivered_at`` (first-seen) on return; neither sets ``acked_at``.
    """
    unseen = "d.delivered_at IS NULL" if only_new else "d.acked_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT m.id, m.from_agent_id, m.type, m.body_json, m.created_at, m.to_role,
               a.name AS from_name, a.role AS from_role
        FROM messages m
        JOIN agents a ON a.id = m.from_agent_id
        LEFT JOIN deliveries d ON d.message_id = m.id AND d.agent_id = ?
        WHERE m.task_id = ?
          AND m.from_agent_id != ?
          AND (m.to_role IS NULL OR m.to_role = ?)
          AND ({unseen})
        ORDER BY m.id
        """,
        (identity.agent_id, identity.task_id, identity.agent_id, identity.role),
    ).fetchall()

    now = time.time()
    out = []
    for r in rows:
        if mark_delivered:
            # Upsert a delivery row; set delivered_at on first sight, keep acked_at NULL.
            conn.execute(
                "INSERT INTO deliveries (message_id, agent_id, delivered_at) VALUES (?,?,?) "
                "ON CONFLICT(message_id, agent_id) DO UPDATE SET "
                "delivered_at = COALESCE(deliveries.delivered_at, excluded.delivered_at)",
                (r["id"], identity.agent_id, now),
            )
        body = json.loads(r["body_json"])
        if r["type"] in BROKER_TYPES:
            # Broker-authored: the row carries the agent whose call TRIGGERED it (the
            # finalising signer — useful provenance, and it keeps the recipient set
            # right: `from_agent_id != me` means the trigger doesn't get told about
            # the thing it just did, everyone else does). But the words are the
            # broker's, so that is how they are attributed and framed.
            from_name, from_role = BROKER_NAME, BROKER_ROLE
            content = _wrap_broker(identity.task_id, body)
        else:
            from_name, from_role = r["from_name"], r["from_role"]
            content = _wrap(from_name, from_role, identity.task_id, body, r["to_role"])
        out.append(
            {
                "id": r["id"],
                "from": from_name,
                "role": from_role,
                "type": r["type"],
                "sent_at": _fmt_time(r["created_at"]),
                "content": content,
            }
        )
    if mark_delivered:
        conn.commit()
    return out


def fetch_unacked(conn, identity: Identity, mark_delivered: bool = True) -> list[dict]:
    """check_messages: everything I haven't acked yet (crash-safe recovery)."""
    return _fetch(conn, identity, only_new=False, mark_delivered=mark_delivered)


def fetch_new(conn, identity: Identity, mark_delivered: bool = True) -> list[dict]:
    """wait_for_message: only mail not yet delivered to me (wake on new traffic)."""
    return _fetch(conn, identity, only_new=True, mark_delivered=mark_delivered)


def ack(conn, identity: Identity, ids: list[int]) -> int:
    """Mark messages processed. Returns how many were acked.

    Only ids that are real messages on the agent's *own* task, sent by *another*
    agent, are acked. Unknown, foreign-task, or self-sent ids are ignored — so a
    stale or wrong id can't crash the call (no FK error) or write across tasks.
    """
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    valid = [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM messages "
            f"WHERE task_id = ? AND from_agent_id != ? AND id IN ({placeholders})",
            (identity.task_id, identity.agent_id, *ids),
        ).fetchall()
    ]
    now = time.time()
    for mid in valid:
        conn.execute(
            "INSERT INTO deliveries (message_id, agent_id, delivered_at, acked_at) VALUES (?,?,?,?) "
            "ON CONFLICT(message_id, agent_id) DO UPDATE SET "
            "acked_at = excluded.acked_at, "
            "delivered_at = COALESCE(deliveries.delivered_at, excluded.delivered_at)",
            (mid, identity.agent_id, now, now),
        )
    conn.commit()
    return len(valid)


# --------------------------------------------------------------------------- #
# presence — "this seat is parked in wait_for_message"
# --------------------------------------------------------------------------- #
# An agent that keeps a listener parked respawns it every ~WAIT_CAP seconds, so
# there is always a small gap between one wait returning and the next starting.
# Treat gaps under this as the SAME streak, otherwise the dashboard's "listening —
# 42m" would reset to 0 on every message cycle.
LISTEN_STREAK_GAP = 120.0


def mark_listening(conn, identity: Identity, timeout_seconds: float, cap: float) -> float:
    """Stamp this seat as listening until ``now + min(timeout, cap)``.

    Deliberately an EXPIRY, not a boolean: a boolean would persist a LIE if the
    broker dies with agents parked (the clearing ``finally`` never runs), and the
    rows would claim "listening" forever. Every wait is bounded by the cap, so
    ``listening_until > now`` is self-healing with no cleanup job.

    ``listening_since`` is the streak start: kept when the previous stamp expired
    less than ``LISTEN_STREAK_GAP`` ago (the respawn gap between consecutive
    listener waits), reset to now otherwise. Returns the new ``listening_until``.
    """
    now = time.time()
    until = now + max(0.0, min(float(timeout_seconds), float(cap)))
    row = conn.execute(
        "SELECT listening_until, listening_since FROM agents WHERE id = ?",
        (identity.agent_id,),
    ).fetchone()
    since = now
    if row is not None:
        prev_until, prev_since = row["listening_until"], row["listening_since"]
        if prev_since and prev_until and (now - prev_until) < LISTEN_STREAK_GAP:
            since = prev_since
    conn.execute(
        "UPDATE agents SET listening_until = ?, listening_since = ? WHERE id = ?",
        (until, since, identity.agent_id),
    )
    conn.commit()
    return until


def clear_listening(conn, identity: Identity) -> None:
    """Mark this seat's listening window as ended — NOT NULL, "expired as of now".

    Writing ``now`` (rather than NULL) keeps the dot going out immediately while
    leaving ``listening_until`` readable as the moment the seat stopped listening,
    so a respawned listener inside LISTEN_STREAK_GAP still counts as one streak.
    """
    conn.execute(
        "UPDATE agents SET listening_until = ? WHERE id = ?", (time.time(), identity.agent_id)
    )
    conn.commit()


def is_listening(until: float | None, now: float | None = None) -> bool:
    """Read the stored expiry as a live boolean."""
    if not until:
        return False
    return float(until) > (time.time() if now is None else now)


MAX_HISTORY = 200  # cap the agent-supplied `limit` (OWASP API4: records per page)


def channel_history(conn, task_id: str, limit: int = 20) -> list[dict]:
    """Recent traffic on a task (read or unread), oldest-first, for context."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, MAX_HISTORY))
    rows = conn.execute(
        """
        SELECT m.id, m.type, m.body_json, m.created_at, m.to_role, a.name AS from_name, a.role AS from_role
        FROM messages m
        JOIN agents a ON a.id = m.from_agent_id
        WHERE m.task_id = ?
        ORDER BY m.id DESC
        LIMIT ?
        """,
        (task_id, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            # Broker-authored notifications are attributed to the broker here too, so
            # the recap an agent reads matches the envelope it was delivered in.
            "from": BROKER_NAME if r["type"] in BROKER_TYPES else r["from_name"],
            "role": BROKER_ROLE if r["type"] in BROKER_TYPES else r["from_role"],
            "type": r["type"],
            "to_role": r["to_role"],
            "sent_at": _fmt_time(r["created_at"]),
            "body": json.loads(r["body_json"]),
        }
        for r in reversed(rows)
    ]
