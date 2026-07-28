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
from sys_buddy.rules import RULES_OF_ENGAGEMENT


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


# --- make_join_url ----------------------------------------------------------
def test_make_join_url_shape_and_fragment():
    url = onboarding.make_join_url("https://abc.ngrok.app", "signin-abc123")
    # /join with the code in the FRAGMENT (after #), so it never reaches the server.
    assert url == "https://abc.ngrok.app/join#c=signin-abc123"
    path, _, fragment = url.partition("#")
    assert path.endswith("/join")
    assert fragment == "c=signin-abc123"


def test_make_join_url_trims_trailing_slash():
    assert (
        onboarding.make_join_url("https://abc.ngrok.app/", "code123")
        == "https://abc.ngrok.app/join#c=code123"
    )


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


@pytest.mark.parametrize("role", ["backend", "frontend"])
@pytest.mark.parametrize("mode", ["contract", "debug"])
def test_role_prompt_is_task_agnostic(role, mode):
    """The prompt teaches the sys-buddy protocol only — never a concrete build task.

    The old prompt hardcoded a `POST /auth/login` demo; a prompt that leaks WHAT to
    build (the humans decide that) is a regression.
    """
    low = onboarding.role_prompt(role, "signin", mode).lower()
    assert "login" not in low
    assert "/auth/" not in low
    # Still names the task and front-loads the pre-flight, whatever the mode/role.
    assert "signin" in low
    assert "readiness_check" in low and "rules" in low


def test_role_prompt_is_identical_for_every_role():
    """Model B (pinned): the producer is whoever PROPOSES the contract — decided per
    deliverable, hardcoded to no role name (state._producer_role). So the briefing must be
    the same for everyone bar the role it addresses.

    This previously branched on `role == "backend"`, which silently broke any task without
    a role by that name: in a designer+frontend session NEITHER matched, so both agents were
    briefed as assessors, neither was told it could propose, and they waited on each other
    while the broker was perfectly willing to proceed."""
    for role in ("backend", "frontend", "mobile", "designer"):
        rendered = onboarding.role_prompt(role, "signin")
        assert rendered == onboarding.role_prompt("backend", "signin").replace(
            "`backend`", f"`{role}`"
        ), f"{role} got a different briefing — the producer must not be hardcoded"


@pytest.mark.parametrize("role", ["backend", "frontend", "mobile", "designer"])
def test_role_prompt_teaches_both_halves_to_every_role(role):
    """Every role learns to propose AND to assess, because either may end up doing either."""
    low = onboarding.role_prompt(role, "signin").lower()
    assert "propose_contract" in low
    assert "assess" in low or "push back" in low
    assert "not forced to sign" in low


@pytest.mark.parametrize("role", ["backend", "frontend", "mobile", "designer"])
def test_role_prompt_never_claims_a_fixed_producer_role(role):
    """The prompt must not tell anyone they ARE the producer up front — nobody is, until
    someone proposes. Naming a role here is the exact drift this test exists to catch."""
    low = onboarding.role_prompt(role, "signin").lower()
    assert "you are the backend" not in low
    assert "the backend proposes" not in low
    # ...and it must say what actually decides it.
    assert "whoever proposes" in low


def test_role_prompt_teaches_planning_and_reopen():
    """Both roles learn the phase name, the post-lock messaging rule, and reopen."""
    for role in ("backend", "frontend"):
        low = onboarding.role_prompt(role, "signin").lower()
        assert "planning" in low
        assert "reopen_negotiations" in low
        # after lock: keep working via messages, no re-lock needed for ad-hoc changes
        assert "no re-lock" in low or "without" in low


@pytest.mark.parametrize("role", ["backend", "frontend", "mobile", "designer"])
def test_role_prompt_mentions_optional_playwright_for_every_role(role):
    """The Playwright nudge used to be consumer-only, which assumed we knew at briefing
    time who would consume. We don't — any role may end up building against a peer's
    contract, so everyone gets it, and it stays explicitly optional (never a gate)."""
    low = onboarding.role_prompt(role, "signin").lower()
    assert "playwright" in low
    assert "optional" in low


@pytest.mark.parametrize("mode", ["contract", "debug"])
def test_role_prompt_teaches_the_agent_owned_wait_loop_not_a_subagent(mode):
    """BOTH variants tell the agent to stay responsive by parking on wait_for_message in
    its OWN turn — NOT by spawning a listener subagent (that reloads its whole context
    per spawn and is expensive). And they carry the give-up rule: a silent peer escalates
    via report_status("stuck") rather than parking forever."""
    text = onboarding.role_prompt("backend", "signin", mode=mode)
    low = text.lower()
    assert "staying in the loop" in low
    assert "wait_for_message" in text
    # The whole point of the change: no subagent-listener guidance anymore.
    assert "subagent" not in low or "don't spawn a separate 'listener' subagent" in text
    assert "listener parked" not in low
    assert "wait_for_message(timeout_seconds=500)" not in text  # the old spawn recipe is gone
    # Give-up → escalate, not hang.
    assert 'report_status("stuck"' in text
    # Explicit turn-taking: the floor-passing signals so neither side stalls or over-waits.
    assert "over to you" in low and "i'll follow up" in low and "done for now" in low


@pytest.mark.parametrize("role", ["backend", "frontend", "mobile", "designer"])
def test_role_prompt_teaches_the_ship_shorthand(role):
    """`ship [#N]` = propose-then-sign in ONE move, because that is how a human thinks
    about it ("agree this and sign my side") while the broker needs two calls.

    It is PROMPT-side only — it maps to propose_contract then lock_contract; there is no
    `ship` tool (pinned in test_server.py). And it must never read as signing FOR the
    peer: it signs one side, and the lock still waits for everyone.
    """
    text = onboarding.role_prompt(role, "signin")
    low = text.lower()
    assert "`ship [#n]`" in low
    assert "propose_contract" in text and "lock_contract" in text
    assert "signs your side only" in low
    assert "every party has" in low or "everyone has" in low
    # ...and it is the answer to a `sign` with nothing proposed, instead of stalling.
    assert "nothing has been proposed" in low
    assert "assumption" in low


def test_ship_is_taught_to_every_role_identically():
    """Same invariant as the rest of the briefing: no role-specific contract vocabulary
    (the producer is whoever proposes — see test_role_prompt_is_identical_for_every_role)."""
    ships = {
        role: [ln for ln in onboarding.role_prompt(role, "signin").splitlines() if "`ship" in ln]
        for role in ("backend", "frontend", "mobile", "designer")
    }
    assert all(v and v == ships["backend"] for v in ships.values())


def test_charter_says_a_sign_with_nothing_proposed_means_propose_it_yourself():
    """The stall this fixes: told to "lock the contract" with nothing proposed, the agent
    explained the problem and asked — three times. The charter (re-readable mid-session via
    the `rules` tool) now says the missing step is the PROPOSAL, that a party may supply it
    under a stated assumption, and that a reaffirmation is a decision, not a re-ask.
    """
    r = RULES_OF_ENGAGEMENT.lower()
    assert "the missing step is the proposal" in r
    assert "explicit assumption" in r
    assert "ask once" in r and "reaffirmation is a decision" in r
    # Must not contradict decline_contract: the peer still gets to object, and the
    # proposal is safe *because* nothing locks until everyone signs.
    assert "decline_contract" in r
    assert "until every party has signed" in r


def test_role_prompt_debug_has_no_contract():
    text = onboarding.role_prompt("backend", "signin", mode="debug")
    assert "debug task" in text
    assert "no contract to plan" in text
    assert "propose_contract" not in text


# --- claude_add_command -----------------------------------------------------
def test_claude_add_command_shape():
    cmd = onboarding.claude_add_command("https://abc.ngrok.app/mcp", "sbk_tok", name="sys-buddy")
    assert isinstance(cmd, list)
    assert cmd[:3] == ["claude", "mcp", "add"]
    assert "https://abc.ngrok.app/mcp" in cmd
    assert "sys-buddy" in cmd
    assert "Authorization: Bearer sbk_tok" in cmd


# --- claude_setup_command (re-pair-safe: remove then add) -------------------
def test_claude_setup_command_removes_before_adding():
    cmd = onboarding.claude_setup_command("https://abc.ngrok.app/mcp", "sbk_tok")
    lines = cmd.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("claude mcp remove sys-buddy")
    assert lines[1].startswith("claude mcp add --scope local")
    assert "sbk_tok" in lines[1]


def test_connect_command_is_scoped_to_the_project_directory():
    """The token is a seat on ONE task, so the connection belongs to the folder that task
    is worked from. Global (user) scope was tried and reverted: it caps you at one active
    task per machine, because pairing again replaces the single global entry — and it
    leaves one task's bearer token in every project you open."""
    add = onboarding.claude_add_command("https://abc.ngrok.app/mcp", "sbk_tok")
    assert "--scope" in add and add[add.index("--scope") + 1] == "local"
    # Remove passes NO scope on purpose: it then clears the entry wherever it lives,
    # including the user-scope entries a short-lived earlier version created.
    assert "--scope" not in onboarding.claude_remove_command()


def test_configure_claude_runs_remove_before_add(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboarding.subprocess, "run",
        lambda argv, *a, **k: (calls.append(list(argv)), _FakeCompleted(returncode=0))[1],
    )
    onboarding.configure_claude("https://abc.ngrok.app/mcp", "sbk_tok")
    assert calls[0][:3] == ["claude", "mcp", "remove"]
    assert calls[1][:3] == ["claude", "mcp", "add"]


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


def test_host_setup_rejects_single_role_contract(conn):
    # Model B: no 'backend' requirement, but a contract still needs >= 2 roles.
    r = onboarding.host_setup("x", ["frontend"], "http://h")
    assert r["ok"] is False
    assert isinstance(r["error"], str) and r["error"]


def test_host_setup_contract_without_backend_succeeds(conn):
    # A 2-role contract with NO 'backend' role is fine now — producer = whoever proposes.
    r = onboarding.host_setup("noback", ["frontend", "mobile"], "http://h")
    assert r["ok"] is True
    assert {i["role"] for i in r["invites"]} == {"frontend", "mobile"}


def test_host_setup_rejects_duplicate_task(conn):
    first = onboarding.host_setup("dup", ["backend", "frontend"], "http://h")
    assert first["ok"] is True

    r = onboarding.host_setup("dup", ["backend", "frontend"], "http://h")
    assert r["ok"] is False
    assert "already exists" in r["error"]


def test_host_setup_seats_host_role(conn):
    """With host_role set, the host gets its OWN agent seat and invite links go only
    to the other roles."""
    r = onboarding.host_setup(
        "signin", ["backend", "frontend"], "http://127.0.0.1:8787", host_role="backend"
    )
    assert r["ok"] is True

    # Invite links exclude the host's own role.
    invite_roles = {i["role"] for i in r["invites"]}
    assert invite_roles == {"frontend"}

    seat = r["host_seat"]
    assert set(seat) == {
        "role", "mcp_url", "agent_token", "prompt", "config_command", "clients",
    }
    assert seat["role"] == "backend"
    assert seat["mcp_url"] == "http://127.0.0.1:8787/mcp"
    assert seat["agent_token"]
    assert "signin" in seat["prompt"]
    # config_command is the ready-to-run claude mcp add line carrying the token.
    assert "claude mcp add" in seat["config_command"]
    assert seat["agent_token"] in seat["config_command"]
    # The host may not be on Claude: every supported client comes with the seat, so
    # the desktop app never has to spell one of these out itself.
    assert [c["id"] for c in seat["clients"]] == list(onboarding.CLIENT_IDS)


def test_host_setup_without_host_role_seats_nobody(conn):
    """Back-compat: no host_role → invite link per role and no host_seat key."""
    r = onboarding.host_setup("plain", ["backend", "frontend"], "http://h")
    assert r["ok"] is True
    assert {i["role"] for i in r["invites"]} == {"backend", "frontend"}
    assert "host_seat" not in r


def test_host_setup_derives_task_id_from_title(conn):
    """No task_id → id derived from the title; the derived id is returned."""
    r = onboarding.host_setup(None, ["backend", "frontend"], "http://h", title="New Login API")
    assert r["ok"] is True
    assert r["task_id"].startswith("new-login-api-")


def test_gui_start_host_rejects_http_public_url():
    """Host GUI must refuse a cleartext public_url — both remote paths (ngrok /
    `tailscale serve`) present https, so the GUI requires it."""
    from sys_buddy import gui
    r = gui.GuiApi().start_host(
        "My Task", ["backend", "frontend"], host_role="", public_url="http://insecure.example"
    )
    assert r.get("ok") is False and "https" in r["error"].lower()


# --- host_setup: connectivity + the human-owned staging target --------------
def _task_row(conn, task_id):
    return conn.execute(
        "SELECT same_machine, staging_url FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


def test_host_setup_records_same_machine_for_a_loopback_origin(conn):
    """Blank public URL + loopback broker origin = one box: recorded on the task so
    the contract may name http://localhost:PORT."""
    r = onboarding.host_setup(
        "local1", ["backend", "frontend"], "http://127.0.0.1:8787",
        public_url=None, staging_url="http://localhost:3000",
    )
    assert r["ok"] is True
    assert r["same_machine"] is True
    assert r["staging_url"] == "http://localhost:3000"
    row = _task_row(conn, "local1")
    assert row["same_machine"] == 1
    assert row["staging_url"] == "http://localhost:3000"


@pytest.mark.parametrize("public_url", [
    "https://abc-123.ngrok-free.app",
    "https://my-box.tailnet.ts.net",
])
def test_host_setup_marks_a_tunnel_task_remote(conn, public_url):
    r = onboarding.host_setup(
        "remote1", ["backend", "frontend"], public_url,
        public_url=public_url, staging_url="https://api-staging.example.com",
    )
    assert r["ok"] is True
    assert r["same_machine"] is False
    assert _task_row(conn, "remote1")["same_machine"] == 0


@pytest.mark.parametrize("staging_url", [
    "http://localhost:3000",
    "http://api-staging.example.com",
    "https://127.0.0.1/admin",
    "https://169.254.169.254/latest/meta-data/",
    "https://db.internal/x",
])
def test_host_setup_rejects_an_unreachable_target_on_a_tunnel_task(conn, staging_url):
    """REGRESSION: the strict https + SSRF rules apply to any task with a real
    origin, and the human is told at setup rather than the agent failing later."""
    r = onboarding.host_setup(
        "remote2", ["backend", "frontend"], "https://abc.ngrok-free.app",
        public_url="https://abc.ngrok-free.app", staging_url=staging_url,
    )
    assert r["ok"] is False
    assert "staging_url" in r["error"]
    assert conn.execute("SELECT 1 FROM tasks WHERE id = 'remote2'").fetchone() is None


def test_host_setup_without_a_staging_url_is_unchanged(conn):
    """Back-compat: the field is optional — omit it and the agents agree one."""
    r = onboarding.host_setup("nourl", ["backend", "frontend"], "http://127.0.0.1:8787")
    assert r["ok"] is True
    assert r["staging_url"] is None
    assert _task_row(conn, "nourl")["staging_url"] is None


def test_host_seat_prompt_names_the_chosen_target(conn):
    r = onboarding.host_setup(
        "seated", ["backend", "frontend"], "http://127.0.0.1:8787",
        host_role="backend", staging_url="http://localhost:4321",
    )
    assert r["ok"] is True
    assert "http://localhost:4321" in r["host_seat"]["prompt"]


def test_role_prompt_omits_the_target_line_when_unset():
    prompt = onboarding.role_prompt("backend", "t1")
    assert "already agreed this task's target" not in prompt


# --- gui.start_host plumbing ------------------------------------------------
def _stub_broker(monkeypatch):
    """Neutralise the two side effects of start_host: booting the in-process broker
    and shelling out to the Claude CLI."""
    from sys_buddy import gui
    monkeypatch.setattr(gui, "_ensure_broker", lambda *a, **k: True)
    monkeypatch.setattr(
        onboarding, "configure_claude",
        lambda *a, **k: {"ok": True, "detail": "stub", "command": "claude mcp add …"},
    )
    return gui


def test_gui_start_host_threads_the_staging_url_onto_a_same_machine_task(conn, monkeypatch):
    gui = _stub_broker(monkeypatch)
    r = gui.GuiApi().start_host(
        "Local Task", ["backend", "frontend"], host_role="backend",
        public_url="", mode="contract", staging_url="http://localhost:3000",
    )
    assert r["ok"] is True
    assert r["same_machine"] is True and r["staging_url"] == "http://localhost:3000"
    row = _task_row(conn, r["task_id"])
    assert row["same_machine"] == 1 and row["staging_url"] == "http://localhost:3000"
    assert "http://localhost:3000" in r["host_seat"]["prompt"]


def test_gui_start_host_rejects_localhost_target_when_exposed(conn, monkeypatch):
    """REGRESSION: choosing a tunnel means the buddy is elsewhere — a localhost
    target is refused even though the same GUI allows it on a same-machine task."""
    gui = _stub_broker(monkeypatch)
    r = gui.GuiApi().start_host(
        "Tunnelled", ["backend", "frontend"], host_role="backend",
        public_url="https://abc.ngrok-free.app", mode="contract",
        staging_url="http://localhost:3000",
    )
    assert r["ok"] is False and "staging_url" in r["error"]


def test_gui_start_host_without_a_staging_url_still_works(conn, monkeypatch):
    gui = _stub_broker(monkeypatch)
    r = gui.GuiApi().start_host("No Target", ["backend", "frontend"], host_role="backend")
    assert r["ok"] is True and r["staging_url"] is None


# --------------------------------------------------------------------------- #
# the no-terminal connect path
# --------------------------------------------------------------------------- #
# The CLI path assumes the `claude` binary. Someone driving Claude Code inside the
# desktop app has no terminal and may not know the CLI exists — observed in the wild,
# and it ended with the buddy installing a tool he had never heard of just to join.
def test_mcp_json_snippet_is_valid_json_with_the_bearer_header():
    import json as _json

    snippet = onboarding.mcp_json_snippet("https://abc.ngrok.app/mcp", "sbk_tok")
    parsed = _json.loads(snippet)
    entry = parsed["mcpServers"]["sys-buddy"]
    assert entry["url"] == "https://abc.ngrok.app/mcp"
    assert entry["headers"]["Authorization"] == "Bearer sbk_tok"


def test_mcp_json_states_the_transport_explicitly():
    """VS Code REQUIRES `type`; the others infer it. Stating it costs nothing and stops
    one snippet silently selecting the wrong transport in a client that needs it."""
    import json as _json

    parsed = _json.loads(onboarding.mcp_json_snippet("https://x/mcp", "t"))
    assert parsed["mcpServers"]["sys-buddy"]["type"] == "http"


def test_both_connect_paths_describe_the_same_connection():
    """A file that disagrees with the command is worse than having only one of them."""
    import json as _json

    url, tok = "https://abc.ngrok.app/mcp", "sbk_tok"
    argv = onboarding.claude_add_command(url, tok)
    parsed = _json.loads(onboarding.mcp_json_snippet(url, tok))["mcpServers"]["sys-buddy"]

    assert url in argv and parsed["url"] == url
    assert f"Authorization: Bearer {tok}" in argv
    assert parsed["headers"]["Authorization"] == f"Bearer {tok}"
