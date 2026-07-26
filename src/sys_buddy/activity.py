"""Activity notes — ambient "what we're up to" presence.

A human has their agent post a brief one-liner ("researching the OAuth refresh flow") so the
buddy has ambient awareness while work is in flight. This is a THIRD channel, deliberately
separate from the other two:

    report_status  → lifecycle STATES   (ready / verified / stuck)
    send_message   → the CONVERSATION   (question / answer / proposal)   — peer DATA
    activity note  → "what we're up to" (planning / researching X)       — ambient presence

It carries no lifecycle meaning and is NEVER delivered as mail — it must not consume the
new-flag or wake a parked ``wait_for_message`` (it's ambient, not addressed). The dashboard
surfaces it as pills over SSE. Content is DATA like a message: the peer's agent shows it to
its human, never acts on it.
"""

from __future__ import annotations

import time

from .service import Identity

# Twitter-ish: brief by construction. Two sentences fit comfortably; the cap is what the
# tool enforces (the "2 sentences" is guidance in the prompt, the char cap is the teeth).
MAX_ACTIVITY_CHARS = 200


def share_activity(conn, identity: Identity, text: str) -> dict:
    """Post an activity note on ``identity``'s task. Rejects empty or over-long text with a
    clear, agent-readable ValueError (keep it to a line or two)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("an activity note needs some text")
    if len(text) > MAX_ACTIVITY_CHARS:
        raise ValueError(
            f"activity note is {len(text)} chars — keep it under {MAX_ACTIVITY_CHARS} "
            f"(a line or two, max 2 sentences)"
        )

    task = conn.execute(
        "SELECT closed_at FROM tasks WHERE id = ?", (identity.task_id,)
    ).fetchone()
    if task is None:
        raise ValueError(f"unknown task '{identity.task_id}'")
    if task["closed_at"] is not None:
        raise ValueError(f"task '{identity.task_id}' is closed")

    now = time.time()
    cur = conn.execute(
        "INSERT INTO activity (task_id, from_agent_id, text, created_at) VALUES (?,?,?,?)",
        (identity.task_id, identity.agent_id, text, now),
    )
    conn.commit()
    return {"id": cur.lastrowid, "text": text, "created_at": now}


def list_activity(conn, task_id: str) -> list[dict]:
    """Activity notes on the task, oldest-first, with the poster's role. The dashboard
    shows them newest-first (a scrollable pill strip); returning oldest-first keeps this
    consistent with the message/event queries — the UI reverses for display."""
    rows = conn.execute(
        "SELECT a.id, a.text, a.created_at, ag.role AS role "
        "FROM activity a JOIN agents ag ON ag.id = a.from_agent_id "
        "WHERE a.task_id = ? ORDER BY a.id",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]
