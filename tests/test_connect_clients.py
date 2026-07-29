"""Specs for the connect-your-agent client registry (``onboarding.connect_clients``).

Source of truth: ``docs/connect-your-agent.md`` — every instruction in it was verified
end-to-end against a real broker on 2026-07-28.

These tests pin LITERALS on purpose. The per-client field names look like they could
come from one template with substitutions, and they cannot: ``mcpServers`` vs
``servers`` vs ``context_servers``, ``url`` vs ``serverUrl`` vs ``httpUrl``, Claude
wanting ``"type": "http"`` where Cursor takes none. A shared template generates configs
that fail SILENTLY — which is the entire failure mode the doc exists to prevent — so
each client's exact keys are asserted here rather than its shape.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from sys_buddy import onboarding

SPEC = Path(__file__).resolve().parents[1] / "docs" / "connect-your-agent.md"

URL = "https://abc.ngrok.app/mcp"
TOK = "sbk_tok123"


def _clients(name: str = "sys-buddy") -> list[dict]:
    return onboarding.connect_clients(URL, TOK, name)


def _by_id(client_id: str) -> dict:
    return onboarding.connect_client(client_id, URL, TOK)


# --------------------------------------------------------------------------- #
# the registry, as a whole
# --------------------------------------------------------------------------- #
def test_registry_covers_exactly_the_shipped_clients():
    """Claude (terminal + desktop), Cursor and Gemini CLI are LOCKED; Other is the
    escape hatch. Anything we cannot RUN stays under Other — an untested per-client
    snippet would look as authoritative as the tested ones and be confidently wrong."""
    assert [c["id"] for c in _clients()] == list(onboarding.CLIENT_IDS)
    assert onboarding.CLIENT_IDS == (
        "claude-cli", "claude-desktop", "cursor", "gemini-cli", "other",
    )


def test_every_entry_renders_with_what_a_ui_needs_to_draw_it():
    """A UI must be able to render an entry WITHOUT knowing which client it is."""
    for c in _clients():
        assert c["id"] and c["label"]
        assert c["kind"] in (onboarding.CLIENT_CLI, onboarding.CLIENT_FILE,
                             onboarding.CLIENT_MANUAL)
        assert c["group"] and c["group_label"]
        assert c["mcp_url"] == URL
        assert c["steps"] and all(s["title"] for s in c["steps"])
        assert isinstance(c["notes"], list)
        # Principle #5 — every failure we found was silent, so every client carries a
        # verification step.
        assert c["verify"]


def test_every_step_copy_target_names_a_real_field():
    """``copy`` is how a generic renderer knows which payload a step's button copies —
    a name that doesn't resolve is a dead button."""
    for c in _clients():
        for step in c["steps"]:
            for field in step["copy"]:
                assert field in c, f"{c['id']}: step copies missing field {field!r}"
                assert c[field], f"{c['id']}: copy field {field!r} is empty"


def test_claude_is_the_default_and_is_the_only_grouped_client():
    """Claude is default-selected (zero clicks for today's audience) and is the ONLY
    one with a sub-step — the others have exactly one way in, so a second click would
    be friction with no information."""
    clients = _clients()
    defaults = [c["id"] for c in clients if c["default"]]
    assert defaults == [onboarding.DEFAULT_CLIENT_ID] == ["claude-cli"]

    grouped: dict[str, list[dict]] = {}
    for c in clients:
        grouped.setdefault(c["group"], []).append(c)
    assert [g for g, members in grouped.items() if len(members) > 1] == ["claude"]
    for c in clients:
        # A grouped client needs a label for its sub-step; a lone one must not have one.
        assert bool(c["variant_label"]) == (len(grouped[c["group"]]) > 1)


def test_every_entry_carries_the_two_facts_and_nothing_less():
    """Every client needs the same two things: the URL and the bearer header. However
    an entry is packaged, both must be somewhere in it."""
    for c in _clients():
        blob = json.dumps(c)
        assert URL in blob, c["id"]
        assert f"Bearer {TOK}" in blob, c["id"]


def test_unknown_client_id_is_a_value_error():
    with pytest.raises(ValueError):
        onboarding.connect_client("windsurf", URL, TOK)


# --------------------------------------------------------------------------- #
# kind: cli — a runnable argv, not just display text
# --------------------------------------------------------------------------- #
def _cli_clients() -> list[dict]:
    return [c for c in _clients() if c["kind"] == onboarding.CLIENT_CLI]


def test_cli_kinds_are_runnable_argvs_carrying_the_url_and_exact_header():
    """``argv`` is a list so a caller can both DISPLAY it and hand it straight to
    ``subprocess.run`` — no shell quoting anywhere near the bearer token."""
    assert {c["id"] for c in _cli_clients()} == {"claude-cli", "gemini-cli"}
    for c in _cli_clients():
        assert c["argv"] and all(isinstance(a, list) for a in c["argv"])
        assert all(isinstance(word, str) for argv in c["argv"] for word in argv)
        # The argv that does the registering carries both facts, and the header is the
        # EXACT string middleware.py reads — one space, "Bearer", one space.
        add = c["argv"][-1]
        assert URL in add, c["id"]
        assert f"Authorization: Bearer {TOK}" in add, c["id"]


def test_cli_command_text_is_the_argv_and_not_a_second_hand_written_copy():
    """The copy-paste string must be a rendering OF the argv. Two hand-written copies
    drift, and the one that drifts is always the one the human actually runs."""
    for c in _cli_clients():
        assert c["command"] == "\n".join(
            onboarding.display_command(argv) for argv in c["argv"]
        )


def test_the_copied_command_quotes_the_header_with_double_quotes():
    """DOUBLE quotes, as documented and verified. ``shlex.join`` would use SINGLE
    quotes, and cmd.exe does not treat ``'`` as a delimiter at all — the header would
    arrive as the literal word ``'Authorization:``. Double quotes work in bash, zsh,
    PowerShell and cmd alike, and the two-plain-lines format exists for exactly that
    same paste-anywhere reason."""
    for c in _cli_clients():
        assert f'"Authorization: Bearer {TOK}"' in c["command"], c["id"]
        assert "'" not in c["command"], c["id"]
    # And the exact lines the spec documents.
    assert _by_id("claude-cli")["command"].splitlines()[1] == (
        "claude mcp add --scope local --transport http sys-buddy "
        f'{URL} --header "Authorization: Bearer {TOK}"'
    )
    assert _by_id("gemini-cli")["command"] == (
        f'gemini mcp add -t http sys-buddy {URL} -H "Authorization: Bearer {TOK}"'
    )


def test_display_command_falls_back_to_shell_quoting_for_dangerous_words():
    """A word double-quoting would NOT neutralise must never be emitted bare-ish."""
    rendered = onboarding.display_command(["x", 'a "b" $c'])
    assert rendered.split(" ", 1)[1] == shlex.quote('a "b" $c')


def test_claude_terminal_is_remove_then_add():
    """``claude mcp add`` refuses to overwrite an existing entry, so a re-pair (new
    tunnel URL and/or new token) must remove the stale one first. Two plain lines, not
    a shell &&/; chain, so it pastes cleanly on any OS."""
    c = _by_id("claude-cli")
    assert [a[:3] for a in c["argv"]] == [
        ["claude", "mcp", "remove"], ["claude", "mcp", "add"],
    ]
    assert c["command"].splitlines()[0] == "claude mcp remove sys-buddy"


def test_claude_terminal_keeps_scope_local_on_add_and_no_scope_on_remove():
    """PINNED. The token is a seat on ONE task, so the connection belongs to the folder
    that task is worked from. User scope was tried and REVERTED: pairing again replaces
    the single global entry (capping you at one active task per machine) and leaves one
    task's bearer token in every project you open. The remove half passes no scope so it
    clears the entry wherever it lives — including entries the user-scope version made."""
    remove, add = _by_id("claude-cli")["argv"]
    assert "--scope" in add and add[add.index("--scope") + 1] == "local"
    assert "--transport" in add and add[add.index("--transport") + 1] == "http"
    assert "--scope" not in remove


def test_gemini_cli_uses_its_own_flags_not_claudes():
    """``gemini mcp add -t http <name> <url> -H "..."`` — verified against a real broker.
    Not a reskin of the Claude line: different subcommand flags entirely."""
    c = _by_id("gemini-cli")
    assert c["argv"] == [[
        "gemini", "mcp", "add", "-t", "http",
        "sys-buddy", URL, "-H", f"Authorization: Bearer {TOK}",
    ]]
    # Per-project by default is exactly what we want (same reason as --scope local), so
    # we must NOT pass -s user.
    assert "-s" not in c["argv"][0] and "user" not in c["argv"][0]
    # And there is no remove-first line: gemini mcp add overwrites.
    assert len(c["argv"]) == 1


def test_gemini_notes_record_the_two_things_that_look_wrong_and_are_not():
    """`(sse)` in `gemini mcp list`, and the CLI writing `url` not `httpUrl`. Both are
    recorded FINDINGS, not bugs — a future reader must not "fix" either."""
    notes = " ".join(n["text"] for n in _by_id("gemini-cli")["notes"])
    assert "(sse)" in notes
    assert "httpUrl" in notes


def test_gemini_settings_file_records_url_not_httpurl():
    """What ``gemini mcp add -t http`` actually WRITES. Third-party docs claim
    ``httpUrl`` selects streamable HTTP; that is not what the CLI produces, and what it
    produces works. The CLI's output is the source of truth over any doc."""
    entry = json.loads(onboarding.gemini_settings_config(URL, TOK))["mcpServers"]["sys-buddy"]
    assert entry["url"] == URL
    assert "httpUrl" not in entry
    assert entry["type"] == "http"


# --------------------------------------------------------------------------- #
# kind: file — the paste instruction, and the per-client JSON literals
# --------------------------------------------------------------------------- #
def _file_clients() -> list[dict]:
    return [c for c in _clients() if c["kind"] == onboarding.CLIENT_FILE]


def test_file_kinds_are_the_two_no_terminal_clients():
    assert {c["id"] for c in _file_clients()} == {"claude-desktop", "cursor"}


def test_file_kinds_ship_valid_json_containing_the_url_and_bearer_header():
    for c in _file_clients():
        entry = json.loads(c["config"])["mcpServers"]["sys-buddy"]
        assert entry["url"] == URL
        assert entry["headers"]["Authorization"] == f"Bearer {TOK}"


def test_claude_desktop_file_keys_are_exactly_claudes():
    """`.mcp.json` at the project ROOT, key ``mcpServers``, and ``"type": "http"``
    stated explicitly (VS Code requires it; being explicit costs nothing elsewhere)."""
    c = _by_id("claude-desktop")
    assert c["path"] == ".mcp.json"
    parsed = json.loads(c["config"])
    assert list(parsed) == ["mcpServers"]
    assert parsed["mcpServers"]["sys-buddy"]["type"] == "http"


def test_cursor_file_keys_are_exactly_cursors_and_differ_from_claudes():
    """`.cursor/mcp.json` INSIDE a `.cursor/` directory, not `.mcp.json` at the root —
    and NO ``type``: Cursor infers the transport from ``url``. The difference looks
    cosmetic, which is precisely why it gets its own test."""
    c = _by_id("cursor")
    assert c["path"] == ".cursor/mcp.json"
    entry = json.loads(c["config"])["mcpServers"]["sys-buddy"]
    assert "type" not in entry
    assert set(entry) == {"url", "headers"}
    # The two file clients must not be one renderer wearing two hats.
    assert c["config"] != _by_id("claude-desktop")["config"]


def test_the_paste_block_matches_the_json_it_asks_the_agent_to_write():
    """The instruction the agent READS and the file we'd write must agree — an entry
    the paste spells out differently is a config that fails in a way nobody can see."""
    for c in _file_clients():
        entry = json.loads(c["config"])["mcpServers"]["sys-buddy"]
        assert f'"url": "{URL}"' in c["paste"]
        assert f'"Bearer {TOK}"' in c["paste"]
        assert ('"type": "http"' in c["paste"]) == ("type" in entry)


def test_every_paste_instruction_carries_both_guard_lines():
    """PRINCIPLE #6, and each half was earned by a real failure.

    "STOP and tell me" turns a silent global write into a visible halt. "show me the
    resulting file" is the only reason a wrong-folder write is ever NOTICED — it is
    what caught the Cursor Home trap, because the agent printed the absolute path."""
    assert _file_clients(), "no paste instructions to guard"
    for c in _file_clients():
        assert onboarding.GUARD_STOP in c["paste"], c["id"]
        assert onboarding.GUARD_SHOW in c["paste"], c["id"]
        assert c["paste"].rstrip().endswith(onboarding.GUARD_SHOW), c["id"]


def test_every_paste_instruction_says_merge_not_replace():
    """"Save this as .mcp.json" would CLOBBER every server already there. Merging is
    the whole job of the paste."""
    for c in _file_clients():
        assert "KEEP every server already in it" in c["paste"], c["id"]
        assert "do not replace the file or remove anything" in c["paste"], c["id"]
        assert "THIS project's" in c["paste"], c["id"]


def test_cursor_guard_names_the_global_config_it_must_not_write():
    """Cursor's Home tab resolves "this project's .cursor/mcp.json" to the GLOBAL
    ~/.cursor/mcp.json. Naming the path is what makes the guard actionable."""
    c = _by_id("cursor")
    assert "~/.cursor/mcp.json" in c["paste"]
    assert any("~/.cursor/mcp.json" in n["text"] for n in c["notes"])


def test_claude_desktop_warns_about_the_home_folder_and_the_directory():
    """Two verified surprises: a file in the home folder yields no tools, and users
    look for us in the Directory — a catalogue of PUBLISHED connectors that never
    lists a self-hosted broker."""
    notes = " ".join(n["text"] for n in _by_id("claude-desktop")["notes"])
    assert "home folder" in notes
    assert "Directory" in notes


def test_file_clients_warn_not_to_commit_the_token():
    for c in _file_clients():
        assert any("commit" in n["text"] for n in c["notes"]), c["id"]


def test_desktop_step_says_new_conversation_not_a_full_restart():
    """Verified: a full quit is NOT needed, and "reconnect with /mcp" is CLI-only
    advice — the desktop app answers "MCP controls aren't available right now"."""
    steps = " ".join(s["title"] + " " + s["body"] for s in _by_id("claude-desktop")["steps"])
    assert "NEW conversation" in steps
    assert "/mcp" not in steps


# --------------------------------------------------------------------------- #
# kind: manual — the "Other" escape hatch
# --------------------------------------------------------------------------- #
def test_other_hands_over_the_raw_facts():
    c = _by_id("other")
    assert c["kind"] == onboarding.CLIENT_MANUAL
    assert c["url"] == URL
    assert c["header_name"] == "Authorization"
    assert c["header_value"] == f"Bearer {TOK}"
    assert c["header"] == f"Authorization: Bearer {TOK}"


def test_other_generic_block_is_valid_json():
    """It is laid out compactly because it is read as a hint about SHAPE, not pasted as
    a file — but it must still parse, or a reader who does paste it is stuck."""
    parsed = json.loads(_by_id("other")["config"])
    entry = parsed["mcpServers"]["sys-buddy"]
    assert entry["url"] == URL
    assert entry["headers"]["Authorization"] == f"Bearer {TOK}"


def test_other_names_the_field_names_that_differ():
    """The field names ARE the trap and they look cosmetic — an ``mcpServers`` block
    silently does nothing in Zed. Telling someone to check their client's exact key
    beats handing them a snippet that fails quietly."""
    notes = " ".join(n["text"] for n in _by_id("other")["notes"])
    for token in ("mcpServers", "servers", "context_servers",
                  "url", "serverUrl", "httpUrl"):
        assert token in notes
    assert "VS Code" in notes


def test_other_asks_which_client_it_was():
    """Load-bearing: we cannot verify Zed, Windsurf, Codex or Perplexity — we do not
    have them. Users do. This line is how a client moves out of Other into its own
    locked section, which is exactly how Claude, Cursor and Gemini got there."""
    notes = " ".join(n["text"] for n in _by_id("other")["notes"])
    assert "Tell us which client" in notes


def test_other_verification_is_client_agnostic():
    """Whatever file they found and whatever keys their client uses: if the agent lists
    our tools, it is connected. That one question removes the need for us to know their
    client at all."""
    c = _by_id("other")
    assert onboarding.VERIFY_QUESTION in c["verify"]
    assert "config is in the wrong place" in " ".join(s["body"] for s in c["steps"])


# --------------------------------------------------------------------------- #
# a custom server name reaches every literal
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("client_id", list(onboarding.CLIENT_IDS))
def test_a_custom_server_name_reaches_every_machine_readable_payload(client_id):
    """The default is "sys-buddy" everywhere, but nothing may hard-code it in the parts
    a machine consumes — a stale default there registers under the wrong name silently.

    Only the executable/parseable fields are checked: the prose deliberately keeps
    saying "sys-buddy" (the product's name, and the tools really are called that)."""
    entry = onboarding.connect_client(client_id, URL, TOK, "buddy-two")
    payloads = []
    payloads += [" ".join(a) for a in entry.get("argv", [])]
    payloads += [entry[f] for f in ("command", "config", "paste") if entry.get(f)]
    assert payloads, client_id
    for text in payloads:
        assert "buddy-two" in text, client_id
        assert "sys-buddy" not in text, client_id


# --------------------------------------------------------------------------- #
# the renderer against the doc it came from
# --------------------------------------------------------------------------- #
def _spec_blocks() -> list[str]:
    """Every fenced block in docs/connect-your-agent.md, with shell line-continuations
    folded back into one line so a wrapped command compares like the command it is."""
    raw = re.findall(r"```\n(.*?)```", SPEC.read_text(encoding="utf-8"), re.S)
    return [re.sub(r"\\\n\s+", "", b).strip() for b in raw]


@pytest.mark.parametrize("client_id", ["claude-desktop", "cursor"])
def test_the_paste_blocks_are_the_specs_blocks_verbatim(client_id):
    """Every word of these was verified end-to-end against a real broker on 2026-07-28.
    They are read by an AGENT, so a reworded "improvement" is a behaviour change with
    no test of its own — this is that test. If the doc and the renderer disagree, ONE
    of them is now shipping an untested instruction."""
    paste = onboarding.connect_client(client_id, "<url>", "<token>")["paste"].strip()
    assert paste in _spec_blocks(), f"{client_id} paste no longer matches the spec"


@pytest.mark.parametrize("client_id", ["claude-cli", "gemini-cli"])
def test_the_copy_commands_are_the_specs_commands_verbatim(client_id):
    command = onboarding.connect_client(client_id, "<url>", "<token>")["command"].strip()
    assert command in _spec_blocks(), f"{client_id} command no longer matches the spec"


# --------------------------------------------------------------------------- #
# the seam: the pages get this list from the broker, not from their own copy
# --------------------------------------------------------------------------- #
def test_pair_response_would_carry_the_rendered_clients(conn, monkeypatch):
    """``/pair`` hands the join page ``clients`` the same way it hands it ``prompt`` and
    ``rules``: rendered server-side, because a second copy of these literals in JS is
    the drift this codebase keeps hitting."""
    from sys_buddy import admin, pairing

    admin.create_task("signin", title="Sign-in", roles=["backend", "frontend"])
    code, _ = admin.mint_invite("signin", "frontend")
    res = pairing.redeem_invite(conn, code, "dave-frontend")

    clients = onboarding.connect_clients("http://127.0.0.1:9292/mcp", res["agent_token"])
    assert [c["id"] for c in clients] == list(onboarding.CLIENT_IDS)
    # JSON-serializable: it rides a JSONResponse.
    assert json.loads(json.dumps(clients))


def test_entries_echo_mcp_url_so_a_tunnelled_page_can_rebase_them():
    """The broker stamps its OWN base_url, which is loopback whenever it sits behind a
    tunnel it doesn't know about. The page reached it at a different origin, so it
    rebases every rendered string with one substitution — which needs the old value."""
    for c in _clients():
        assert c["mcp_url"] == URL
        rebased = json.dumps(c).replace(URL, "https://real.example/mcp")
        assert URL not in rebased
        assert "https://real.example/mcp" in rebased


def test_join_flow_and_host_seat_both_carry_the_clients(monkeypatch):
    """Both onboarding entry points hand the desktop app the same list, so gui_app.html
    never needs a client literal of its own."""
    monkeypatch.setattr(
        onboarding, "pair",
        lambda link, name: {
            "task_id": "signin", "role": "frontend",
            "mcp_url": URL, "agent_token": TOK, "rules": "R",
        },
    )
    monkeypatch.setattr(
        onboarding, "configure_claude",
        lambda *a, **k: {"ok": True, "detail": "registered", "command": "cmd"},
    )
    res = onboarding.join_flow("sb1_x", "dave")
    assert res["ok"] is True
    assert [c["id"] for c in res["clients"]] == list(onboarding.CLIENT_IDS)


def test_host_seat_carries_the_clients_too(conn):
    """The HOST may not be on Claude either. Their own seat is minted in-process (no
    /pair round-trip), so it has its own path to the registry — and gui_app.html renders
    both seats through one function, which needs both to carry the same field."""
    res = onboarding.host_setup(
        None, ["backend", "frontend"], "http://127.0.0.1:9292",
        title="Sign-in", host_role="backend",
    )
    assert res["ok"] is True
    seat = res["host_seat"]
    assert [c["id"] for c in seat["clients"]] == list(onboarding.CLIENT_IDS)
    # Rendered for THIS seat's url + token, not some other seat's.
    for c in seat["clients"]:
        assert c["mcp_url"] == seat["mcp_url"] == "http://127.0.0.1:9292/mcp"
        assert f"Bearer {seat['agent_token']}" in json.dumps(c)


# --------------------------------------------------------------------------- #
# the old Claude-specific names still work (gui.py and cli.py call them)
# --------------------------------------------------------------------------- #
def test_legacy_claude_helpers_agree_with_the_registry():
    """``claude_add_command`` / ``claude_setup_command`` / ``mcp_json_snippet`` predate
    the registry and still have callers. They must be the SAME strings the registry
    renders, not a parallel implementation."""
    cli = _by_id("claude-cli")
    assert onboarding.claude_add_command(URL, TOK) == cli["argv"][1]
    assert onboarding.claude_remove_command() == cli["argv"][0]
    assert onboarding.claude_setup_command(URL, TOK) == cli["command"]
    assert onboarding.mcp_json_snippet(URL, TOK) == _by_id("claude-desktop")["config"]
