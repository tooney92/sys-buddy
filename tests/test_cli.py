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

from sys_buddy import cli


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
