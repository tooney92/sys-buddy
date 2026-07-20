"""``sys-buddy`` command-line interface.

Host-side commands (init, task create, invite, revoke-*, close) operate on the
local SQLite file directly — they run on the same machine as the broker. ``join``
is the one network client: it runs on the *buddy's* machine and POSTs to /pair.
``local`` and ``serve`` start the server.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_DB_PATH, DEFAULT_PORT, Config, set_config


def _cfg_from_args(args: argparse.Namespace, mode: str = "local") -> Config:
    from .db import init_db

    cfg = set_config(
        Config(
            mode=mode,
            db_path=Path(getattr(args, "db", None) or DEFAULT_DB_PATH),
        )
    )
    # Ensure the schema exists once per invocation (idempotent, cheap) so host-side
    # commands "just work" on a fresh machine without a separate `init` — matching
    # the predecessor's zero-setup behaviour, but without per-connection overhead.
    # (cmd_join never calls this; it's a network client with no local db.)
    init_db(cfg.db_path)
    return cfg


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    from .db import init_db

    _cfg_from_args(args)
    path = init_db()
    print(f"Initialised sys-buddy database at {path}")
    return 0


def cmd_task_create(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    if not roles:
        print("error: --roles must list at least one role", file=sys.stderr)
        return 2
    task = admin.create_task(args.id, title=args.title or args.id, roles=roles, mode=args.mode)
    print(f"Created task '{task['id']}'  ·  roles: {', '.join(roles)}  ·  state: {task['state']}")
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    code, expires = admin.mint_invite(args.task, args.role)
    print(f"Invite: {code}")
    print(f"  role:    {args.role}")
    print(f"  task:    {args.task}")
    print(f"  expires: {expires} (single use)")
    print("\nShare the broker URL + this code with your buddy over Slack/Signal.")
    return 0


def cmd_host_viewer(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    token = admin.issue_host_viewer(args.label)
    print(f"Host viewer token (all tasks): {token}")
    print("Open the dashboard at:  <broker-url>/ui?v=" + token)
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    from . import pairing

    # join is a network client; no local db/config needed.
    result = pairing.join(args.url, args.code, args.name, pubkey=args.pubkey)
    if result is None:
        return 1
    print("Paired successfully.\n")
    print(f"  task:          {result['task_id']}")
    print(f"  role:          {result['role']}")
    print(f"  mcp_url:       {result['mcp_url']}")
    print(f"  agent_token:   {result['agent_token']}")
    print(f"  dashboard_url: {result['dashboard_url']}")
    print("\nRegister the MCP with (the remove line is a no-op the first time,")
    print("and lets you re-pair later with a new URL/token without a collision):")
    print(f"  claude mcp remove sys-buddy")
    print(
        f"  claude mcp add --transport http sys-buddy {result['mcp_url']} "
        f'--header "Authorization: Bearer {result["agent_token"]}"'
    )
    if result.get("rules"):
        print("\n" + "-" * 68)
        print(result["rules"].rstrip())
        print("-" * 68)
    return 0


def cmd_revoke_agent(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    n = admin.revoke_agent(args.name, task=getattr(args, "task", None))
    print(f"Revoked agent '{args.name}'." if n else f"No active agent named '{args.name}'.")
    return 0 if n else 1


def cmd_revoke_viewer(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    n = admin.revoke_viewer(args.label, task=getattr(args, "task", None))
    print(f"Revoked viewer '{args.label}'." if n else f"No active viewer '{args.label}'.")
    return 0 if n else 1


def cmd_close(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    admin.close_task(args.task)
    print(f"Closed task '{args.task}' — all agent and viewer access revoked.")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    rows = admin.list_tasks()
    if not rows:
        print("No tasks yet. Create one with: sys-buddy task create <id> --roles ...")
        return 0
    for t in rows:
        print(f"  {t['id']:<14} {t['state']:<18} {t['title']}")
    return 0


def cmd_local(args: argparse.Namespace) -> int:
    from .server import run_server

    cfg = _cfg_from_args(args, mode="local")
    cfg.host = "127.0.0.1"
    cfg.port = args.port
    run_server(cfg)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    run_gui()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import os
    import sys

    from .server import run_server

    cfg = _cfg_from_args(args, mode="remote")
    cfg.host = args.host
    cfg.port = args.port
    cfg.public_url = args.public_url or os.environ.get("SYS_BUDDY_PUBLIC_URL")
    cfg.slack_webhook = os.environ.get("SLACK_WEBHOOK_URL") or None
    _ttl_env = os.environ.get("SYS_BUDDY_TOKEN_TTL")
    cfg.agent_token_ttl = (
        args.token_ttl if args.token_ttl is not None
        else (float(_ttl_env) if _ttl_env else None)
    )
    # Tunnel mode (a public_url is set) exposes the broker beyond this machine, so
    # default agent tokens to a 24h TTL unless the operator chose one — a leaked token
    # self-expires; agents refresh with rotate_token. Same-machine (no public_url)
    # keeps no-expiry so a long local session isn't cut off.
    if cfg.agent_token_ttl is None and cfg.public_url:
        cfg.agent_token_ttl = 24 * 3600

    # A private overlay (Tailscale/WireGuard) already encrypts the transport, so an
    # http:// origin over it is fine; --trusted-network says "this public_url rides an
    # encrypted private network" and lifts the https requirement for that case only.
    trusted = getattr(args, "trusted_network", False)

    # Remote mode ships bearer tokens (and the viewer/invite tokens in pairing links)
    # over this origin. Refuse a plaintext public_url; warn loudly if none is set.
    if cfg.public_url and not trusted and not cfg.public_url.lower().startswith("https://"):
        print(
            "error: --public-url must be an https:// origin — otherwise agent tokens "
            "and pairing links transit in cleartext. Point it at your TLS tunnel.",
            file=sys.stderr,
        )
        return 2
    if not cfg.public_url:
        print(
            "warning: no --public-url set — pairing links fall back to "
            f"http://{cfg.host}:{cfg.port} and tokens will transit in cleartext. "
            "Set --public-url (or $SYS_BUDDY_PUBLIC_URL) to your https tunnel origin.",
            file=sys.stderr,
        )
    run_server(cfg)
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sys-buddy", description="Broker for cross-human AI agent collaboration.")
    p.add_argument("--version", action="version", version=f"sys-buddy {__version__}")
    p.add_argument("--db", help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="Create the database schema")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("local", help="Run the broker in local mode (loopback, no auth)")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.set_defaults(func=cmd_local)

    sp = sub.add_parser("serve", help="Run the broker in remote mode (auth enforced)")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--public-url", help="Public base URL (e.g. the ngrok origin) for pairing links")
    sp.add_argument(
        "--token-ttl", type=float, default=None,
        help="Agent-token lifetime in seconds (default: no expiry; 24h when --public-url is set).",
    )
    sp.add_argument(
        "--trusted-network", action="store_true",
        help="The --public-url rides an encrypted private overlay (Tailscale/WireGuard); allow http.",
    )
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("gui", help="Launch the desktop app (host + buddy onboarding)")
    sp.set_defaults(func=cmd_gui)

    task = sub.add_parser("task", help="Task management")
    tsub = task.add_subparsers(dest="task_command", required=True)
    tc = tsub.add_parser("create", help="Create a task")
    tc.add_argument("id")
    tc.add_argument("--roles", required=True, help="Comma-separated roles, e.g. backend,frontend")
    tc.add_argument("--title", help="Human title (defaults to the id)")
    tc.add_argument(
        "--mode",
        choices=["contract", "debug"],
        default="contract",
        help="'contract' (full workflow) or 'debug' (collaborate then mark resolved)",
    )
    tc.set_defaults(func=cmd_task_create)

    sp = sub.add_parser("tasks", help="List tasks")
    sp.set_defaults(func=cmd_tasks)

    sp = sub.add_parser("invite", help="Mint a single-use invite for a role")
    sp.add_argument("--task", required=True)
    sp.add_argument("--role", required=True)
    sp.set_defaults(func=cmd_invite)

    sp = sub.add_parser("host-viewer", help="Issue an all-tasks host viewer token")
    sp.add_argument("--label", default="host")
    sp.set_defaults(func=cmd_host_viewer)

    sp = sub.add_parser("join", help="(buddy side) Redeem an invite for tokens")
    sp.add_argument("url", help="Broker base URL, e.g. https://abc123.ngrok.app")
    sp.add_argument("code", help="Invite code")
    sp.add_argument("--name", required=True, help="Your agent name, e.g. dave-frontend")
    sp.add_argument("--pubkey", help="(T2 only) client public key")
    sp.set_defaults(func=cmd_join)

    sp = sub.add_parser("revoke-agent", help="Revoke an agent's MCP access")
    sp.add_argument("name")
    sp.add_argument("--task", help="Scope revocation to this task (avoids hitting same-named agents on other tasks)")
    sp.set_defaults(func=cmd_revoke_agent)

    sp = sub.add_parser("revoke-viewer", help="Revoke a viewer's dashboard access")
    sp.add_argument("label")
    sp.add_argument("--task", help="Scope revocation to this task")
    sp.set_defaults(func=cmd_revoke_viewer)

    sp = sub.add_parser("close", help="Close a task and revoke all its access")
    sp.add_argument("task")
    sp.set_defaults(func=cmd_close)

    return p


def main(argv: list[str] | None = None) -> int:
    import os

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 — a CLI should fail with a clean line, not a traceback
        if os.environ.get("SYS_BUDDY_DEBUG"):
            raise
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
