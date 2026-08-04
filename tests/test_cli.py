"""CLI-level checks for host-side commands.

Focus: host commands other than ``serve`` (invite, host-viewer) must emit links
that point at the tunnel origin, not loopback. They learn the origin from the
``--public-url`` flag (where present) or the ``SYS_BUDDY_PUBLIC_URL`` env var —
``serve --public-url`` only configures the serving process, so a separately-run
``invite`` would otherwise print a dead ``127.0.0.1`` link. Regression guard for
that bug.
"""

from __future__ import annotations

from types import SimpleNamespace

from sys_buddy import cli, onboarding


def _make_task(db: str, task_id: str = "signin") -> None:
    cli.cmd_task_create(
        SimpleNamespace(db=db, id=task_id, roles="backend,frontend", title=None, mode="contract")
    )


def test_cfg_from_args_reads_public_url_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SYS_BUDDY_PUBLIC_URL", "https://abc123.ngrok.app")
    cfg = cli._cfg_from_args(SimpleNamespace(db=str(tmp_path / "t.db")))
    assert cfg.base_url == "https://abc123.ngrok.app"


def test_cfg_from_args_defaults_to_loopback_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SYS_BUDDY_PUBLIC_URL", raising=False)
    cfg = cli._cfg_from_args(SimpleNamespace(db=str(tmp_path / "t.db")))
    assert cfg.base_url.startswith("http://127.0.0.1")


def test_invite_link_uses_public_url_env(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("SYS_BUDDY_PUBLIC_URL", "https://abc123.ngrok.app")
    _make_task(db)
    cli.cmd_invite(SimpleNamespace(db=db, task="signin", role="frontend", public_url=None))
    out = capsys.readouterr().out
    assert "https://abc123.ngrok.app/join" in out
    assert "127.0.0.1" not in out  # the bug: a loopback link the buddy can't reach


def test_invite_public_url_flag_overrides_env(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("SYS_BUDDY_PUBLIC_URL", "https://env.example")
    _make_task(db)
    cli.cmd_invite(
        SimpleNamespace(db=db, task="signin", role="frontend", public_url="https://flag.example")
    )
    out = capsys.readouterr().out
    assert "https://flag.example/join" in out
    assert "env.example" not in out


def test_host_viewer_prints_real_url_not_placeholder(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("SYS_BUDDY_PUBLIC_URL", "https://abc123.ngrok.app")
    cli.cmd_host_viewer(SimpleNamespace(db=db, label="host"))
    out = capsys.readouterr().out
    assert "https://abc123.ngrok.app/ui?v=sbv_" in out
    assert "<broker-url>" not in out


# --------------------------------------------------------------------------- #
# --port on the link-printing commands
#
# `host-viewer` and `invite` never bind a socket, so before this they built a Config
# at DEFAULT_PORT and printed a :8787 link no matter which port the broker was on.
# On a dev broker (:9292) that link 404s; worse, if the owner's real broker IS on
# :8787 it resolves to a DIFFERENT broker that has never seen the token — which
# reads as an auth bug, not a wrong URL.
# --------------------------------------------------------------------------- #
def _parse(argv: list[str]) -> SimpleNamespace:
    """Parse real argv, so these tests prove the FLAG exists — not just that the
    handler would honour a hand-built namespace."""
    return cli.build_parser().parse_args(argv)


def test_host_viewer_link_honours_port_flag(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    monkeypatch.delenv("SYS_BUDDY_PUBLIC_URL", raising=False)
    args = _parse(["--db", db, "host-viewer", "--port", "9292"])
    args.func(args)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:9292/ui?v=sbv_" in out
    assert "8787" not in out  # the bug: pointed at a broker that isn't serving this token


def test_invite_links_honour_port_flag(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    monkeypatch.delenv("SYS_BUDDY_PUBLIC_URL", raising=False)
    _make_task(db)
    args = _parse(["--db", db, "invite", "--task", "signin", "--role", "frontend", "--port", "9292"])
    args.func(args)
    out = capsys.readouterr().out
    # Every printed origin must agree — the browser link, the desktop link and the
    # `sys-buddy join` line are all handed to another human. Two are plaintext; the
    # desktop `sb1_` link packs the origin as base64, so it gets decoded rather than
    # grepped — a substring check would pass while the embedded origin stayed :8787.
    assert "8787" not in out
    assert "http://127.0.0.1:9292/join" in out
    assert "cli:  sys-buddy join http://127.0.0.1:9292 " in out

    link = next(w for w in out.split() if w.startswith(onboarding.INVITE_PREFIX))
    embedded_origin, _code = onboarding.parse_invite_link(link)
    assert embedded_origin == "http://127.0.0.1:9292"


def test_public_url_still_beats_port_flag(tmp_path, monkeypatch, capsys):
    """A tunnel origin carries its own port; --port must not leak into it."""
    db = str(tmp_path / "t.db")
    monkeypatch.delenv("SYS_BUDDY_PUBLIC_URL", raising=False)
    _make_task(db)
    args = _parse([
        "--db", db, "invite", "--task", "signin", "--role", "frontend",
        "--port", "9292", "--public-url", "https://abc123.ngrok.app",
    ])
    args.func(args)
    out = capsys.readouterr().out
    assert "https://abc123.ngrok.app/join" in out
    assert "9292" not in out
    assert "127.0.0.1" not in out


def test_port_defaults_when_flag_absent(tmp_path, monkeypatch, capsys):
    """Omitting --port keeps the documented default, so nothing changes for the
    normal single-broker user."""
    db = str(tmp_path / "t.db")
    monkeypatch.delenv("SYS_BUDDY_PUBLIC_URL", raising=False)
    args = _parse(["--db", db, "host-viewer"])
    args.func(args)
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{cli.DEFAULT_PORT}/ui?v=sbv_" in out


def test_link_printing_commands_all_accept_port():
    """Guard against a future link-printing command forgetting the flag."""
    for argv in (["host-viewer"], ["invite", "--task", "t", "--role", "backend"]):
        args = _parse([*argv, "--port", "1234"])
        assert args.port == 1234, f"{argv[0]} dropped --port"


# --- the workflows a host can actually pick ---------------------------------
def test_cli_mode_choices_are_exactly_admin_modes():
    """Three surfaces used to spell the mode list independently — `admin.create_task`, this
    parser, and the desktop app's radio group. `engagement` shipped in v2.1.0 and only the
    domain layer learned about it, so no host could create one for two releases. The CLI now
    reads `admin.MODES`; this asserts it and fails if anyone retypes the list."""
    from sys_buddy import admin

    action = next(
        a for a in cli.build_parser()._subparsers._group_actions[0]
        .choices["task"]._subparsers._group_actions[0]
        .choices["create"]._actions
        if a.dest == "mode"
    )
    assert tuple(action.choices) == admin.MODES


def test_the_desktop_app_offers_a_radio_for_every_mode():
    """The third surface, checked at the source. The desktop app is the route most hosts
    take, and it is where `engagement` was invisible — offering two of the three workflows
    while the broker supported all three. A missing radio is a feature nobody can reach."""
    from pathlib import Path

    from sys_buddy import admin, gui

    html = (Path(gui.__file__).parent / "gui_app.html").read_text(encoding="utf-8")
    for mode in admin.MODES:
        assert f'name="session-mode"' in html
        assert f'value="{mode}"' in html, f"the desktop app cannot create a {mode!r} task"
