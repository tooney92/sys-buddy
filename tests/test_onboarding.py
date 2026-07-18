"""Specs for the onboarding helpers (invite links, role prompts, client wiring).

These test the *contract* of ``sys_buddy.onboarding`` — the implementation is built
in parallel. The host-side helpers (``host_create_task``/``host_invite_link``) call
``admin`` under the hood, which opens its own connection off ``get_config().db_path``;
the ``conn`` fixture has already pointed that at an isolated temp db, so they share
the same database as the fixture connection.
"""

from __future__ import annotations

import base64
import json

import pytest

from sys_buddy import onboarding


# --- invite link round-trip -------------------------------------------------
@pytest.mark.parametrize(
    "base_url",
    [
        "https://abc.ngrok.app",
        "http://127.0.0.1:8787",
        "https://example.com/mcp",
    ],
)
def test_invite_link_round_trips(base_url):
    code = "signin-abc123XYZ"
    link = onboarding.make_invite_link(base_url, code)
    assert link.startswith("sb1_")
    assert onboarding.parse_invite_link(link) == (base_url, code)


def test_make_invite_link_has_no_padding_or_whitespace():
    link = onboarding.make_invite_link("https://abc.ngrok.app", "signin-abc123")
    assert "=" not in link
    assert not any(ch.isspace() for ch in link)


# --- parse_invite_link error handling ---------------------------------------
def _link_missing_c_key():
    """A structurally valid sb1_ link whose payload is missing the required 'c' key."""
    payload = base64.urlsafe_b64encode(json.dumps({"u": "https://x"}).encode()).rstrip(b"=")
    return onboarding.INVITE_PREFIX + payload.decode()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-link",
        "sb1_@@@notb64@@@",
    ],
)
def test_parse_invite_link_rejects_garbage(bad):
    with pytest.raises(ValueError):
        onboarding.parse_invite_link(bad)


def test_parse_invite_link_rejects_missing_key():
    with pytest.raises(ValueError):
        onboarding.parse_invite_link(_link_missing_c_key())


# --- role_prompt ------------------------------------------------------------
@pytest.mark.parametrize("role", ["backend", "frontend"])
def test_role_prompt_common_contract(role):
    text = onboarding.role_prompt(role, "signin")
    assert "signin" in text
    assert "rules" in text
    # Peer messages must be framed as DATA, not instructions to obey.
    low = text.lower()
    assert "data" in low and "instruction" in low


def test_role_prompt_backend_mentions_propose_and_lock():
    text = onboarding.role_prompt("backend", "signin").lower()
    assert "propose" in text and "lock" in text


def test_role_prompt_frontend_mentions_verified():
    text = onboarding.role_prompt("frontend", "signin").lower()
    assert "verified" in text


# --- claude_add_command -----------------------------------------------------
def test_claude_add_command_shape():
    cmd = onboarding.claude_add_command("https://abc.ngrok.app/mcp", "sbk_tok", name="sys-buddy")
    assert isinstance(cmd, list)
    assert cmd[:3] == ["claude", "mcp", "add"]
    assert "https://abc.ngrok.app/mcp" in cmd
    assert "sys-buddy" in cmd
    assert "Authorization: Bearer sbk_tok" in cmd


# --- configure_claude -------------------------------------------------------
class _FakeCompleted:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_configure_claude_success(monkeypatch):
    monkeypatch.setattr(
        onboarding.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0)
    )
    result = onboarding.configure_claude("https://abc.ngrok.app/mcp", "sbk_tok")
    assert result["ok"] is True
    assert "claude mcp add" in result["command"]


def test_configure_claude_binary_missing(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(onboarding.subprocess, "run", _raise)
    result = onboarding.configure_claude("https://abc.ngrok.app/mcp", "sbk_tok")
    assert result["ok"] is False
    low = result["detail"].lower()
    assert "not found" in low or "install" in low


def test_configure_claude_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        onboarding.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr="boom"),
    )
    result = onboarding.configure_claude("https://abc.ngrok.app/mcp", "sbk_tok")
    assert result["ok"] is False
    assert "boom" in result["detail"]


# --- pair -------------------------------------------------------------------
def test_pair_returns_join_result(monkeypatch):
    fake = {"task_id": "signin", "role": "frontend", "agent_token": "sbk_x"}
    monkeypatch.setattr(onboarding.pairing, "join", lambda *a, **k: fake)
    link = onboarding.make_invite_link("http://127.0.0.1:8787", "signin-abc")
    assert onboarding.pair(link, "dave-frontend") is fake


def test_pair_raises_when_join_returns_none(monkeypatch):
    monkeypatch.setattr(onboarding.pairing, "join", lambda *a, **k: None)
    link = onboarding.make_invite_link("http://127.0.0.1:8787", "signin-abc")
    with pytest.raises(ValueError):
        onboarding.pair(link, "dave-frontend")


# --- host_create_task + host_invite_link ------------------------------------
def test_host_create_task_and_invite_link(conn):
    onboarding.host_create_task("signin", ["backend", "frontend"])
    link = onboarding.host_invite_link("signin", "frontend", "http://127.0.0.1:8787")

    base_url, code = onboarding.parse_invite_link(link)
    assert base_url == "http://127.0.0.1:8787"
    assert code  # a non-empty invite code


# --- join_flow --------------------------------------------------------------
def _fake_join():
    """A representative successful ``pair`` result."""
    return {
        "task_id": "signin",
        "role": "frontend",
        "agent_token": "sbk_x",
        "mcp_url": "http://h/mcp",
        "dashboard_url": "http://h/ui?v=sbv_y",
        "expires_at": None,
        "rules": "RULES",
    }


def test_join_flow_success(monkeypatch):
    monkeypatch.setattr(onboarding, "pair", lambda *a, **k: _fake_join())
    monkeypatch.setattr(
        onboarding,
        "configure_claude",
        lambda *a, **k: {"ok": True, "detail": "registered", "command": "claude mcp add ..."},
    )
    result = onboarding.join_flow("sb1_link", "dave-frontend")
    assert result["ok"] is True
    assert result["role"] == "frontend"
    assert result["task_id"] == "signin"
    assert result["config_ok"] is True
    assert isinstance(result["prompt"], str) and result["prompt"]
    assert "signin" in result["prompt"]
    assert result["dashboard_url"]
    assert result["mcp_url"]


def test_join_flow_pair_failure(monkeypatch):
    def _raise(*a, **k):
        raise ValueError("bad link")

    monkeypatch.setattr(onboarding, "pair", _raise)
    result = onboarding.join_flow("sb1_link", "dave-frontend")
    assert result["ok"] is False
    assert "bad link" in result["error"]


def test_join_flow_surfaces_config_failure(monkeypatch):
    monkeypatch.setattr(onboarding, "pair", lambda *a, **k: _fake_join())
    monkeypatch.setattr(
        onboarding,
        "configure_claude",
        lambda *a, **k: {
            "ok": False,
            "detail": "Claude Code CLI not found",
            "command": "claude mcp add ...",
        },
    )
    result = onboarding.join_flow("sb1_link", "dave-frontend")
    assert result["ok"] is True  # pairing still worked
    assert result["config_ok"] is False
    assert "not found" in result["config_detail"]


def test_join_flow_never_raises_on_unexpected(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(onboarding, "pair", _raise)
    result = onboarding.join_flow("sb1_link", "dave-frontend")
    assert isinstance(result, dict)
    assert result["ok"] is False


# --- host_setup -------------------------------------------------------------
def test_host_setup_success(conn):
    r = onboarding.host_setup("signin", ["backend", "frontend"], "http://127.0.0.1:8787")

    assert r["ok"] is True
    assert r["task_id"] == "signin"
    assert len(r["invites"]) == 2
    for invite in r["invites"]:
        assert invite["role"] in {"backend", "frontend"}
        assert invite["link"].startswith("sb1_")
    assert onboarding.parse_invite_link(r["invites"][0]["link"])[0] == "http://127.0.0.1:8787"
    assert r["viewer_token"]
    assert "/ui?v=" in r["dashboard_url"]


def test_host_setup_rejects_missing_backend(conn):
    r = onboarding.host_setup("x", ["frontend"], "http://h")

    assert r["ok"] is False
    assert isinstance(r["error"], str) and r["error"]


def test_host_setup_rejects_duplicate_task(conn):
    first = onboarding.host_setup("dup", ["backend"], "http://h")
    assert first["ok"] is True

    r = onboarding.host_setup("dup", ["backend"], "http://h")
    assert r["ok"] is False
    assert "already exists" in r["error"]


def test_gui_start_host_rejects_http_public_url_without_trusted():
    """Host GUI must refuse a cleartext public_url unless it's a trusted overlay."""
    from sys_buddy import gui
    r = gui.GuiApi().start_host("t", ["backend"], "", "http://insecure.example", False)
    assert r.get("ok") is False and "https" in r["error"].lower()
