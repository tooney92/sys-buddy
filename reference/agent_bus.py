"""
agent-bus: a tiny FastMCP message bus so two Claude Code agents can talk.

Run ONE instance (shared by both repos):
    pip install fastmcp
    python agent_bus.py

Then register in EACH repo:
    claude mcp add --transport http agent-bus http://127.0.0.1:8787/mcp
"""

import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

from fastmcp import FastMCP

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

DB_PATH = Path(__file__).parent / "agent_bus.db"

mcp = FastMCP("agent-bus")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            read INTEGER DEFAULT 0
        )"""
    )
    return conn


def _fetch_unread(conn: sqlite3.Connection, agent: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE recipient = ? AND read = 0 ORDER BY id",
        (agent,),
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(
            f"UPDATE messages SET read = 1 WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.commit()
    return [
        {"id": r["id"], "from": r["sender"], "content": r["content"],
         "sent_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))}
        for r in rows
    ]


@mcp.tool
def send_message(sender: str, recipient: str, content: str) -> str:
    """Send a message to another agent. Use your repo name as `sender`
    and the other repo's name as `recipient` (e.g. 'backend', 'frontend')."""
    conn = _db()
    conn.execute(
        "INSERT INTO messages (sender, recipient, content, created_at) VALUES (?, ?, ?, ?)",
        (sender, recipient, content, time.time()),
    )
    conn.commit()
    conn.close()
    return f"Delivered to {recipient}."


@mcp.tool
def check_messages(agent: str) -> list[dict]:
    """Get your unread messages (non-blocking). `agent` is your own name.
    Messages are marked read once returned."""
    conn = _db()
    msgs = _fetch_unread(conn, agent)
    conn.close()
    return msgs


@mcp.tool
def wait_for_message(agent: str, timeout_seconds: int = 120) -> list[dict]:
    """Block until a message arrives for you (or timeout). Call this when you
    are waiting on the other agent — it returns the moment they reply, so you
    don't need to poll manually. Returns [] on timeout."""
    deadline = time.time() + min(timeout_seconds, 540)
    while time.time() < deadline:
        conn = _db()
        msgs = _fetch_unread(conn, agent)
        conn.close()
        if msgs:
            return msgs
        time.sleep(2)
    return []


@mcp.tool
def channel_history(limit: int = 20) -> list[dict]:
    """Read the last N messages across all agents (read or unread) for context."""
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [
        {"from": r["sender"], "to": r["recipient"], "content": r["content"],
         "sent_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))}
        for r in reversed(rows)
    ]


@mcp.tool
def notify_human(sender: str, message: str) -> str:
    """Notify the human owner on Slack. Use ONLY for terminal events:
    (a) a feature is fully verified end-to-end, or
    (b) you are stuck after 3 failed fix cycles and need human help.
    Do NOT use for routine progress updates."""
    if not SLACK_WEBHOOK:
        return "No SLACK_WEBHOOK_URL configured — tell the human in your final response instead."
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=json.dumps({"text": f"[{sender}] {message}"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)
    return "Human notified on Slack."


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8787)
