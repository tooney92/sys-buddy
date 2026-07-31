"""``sys-buddy`` command-line interface.

Host-side commands (init, task create, invite, revoke-*, close, todo) operate on the
local SQLite file directly — they run on the same machine as the broker. ``join``
is the one network client: it runs on the *buddy's* machine and POSTs to /pair.
``local`` and ``serve`` start the server.

This is also where the HOST's own writes live, and deliberately not on the dashboard
(D11): ``todo drop`` is the escape hatch for a todo hanging on a party whose human
went offline, and a host running ``sys-buddy serve`` headless has no GUI to click.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_DB_PATH, DEFAULT_PORT, Config, set_config


def _cfg_from_args(args: argparse.Namespace, mode: str = "local") -> Config:
    from .db import init_db

    # public_url makes every host-side command (invite, host-viewer, …) emit links
    # that point at the tunnel origin, not loopback. It's a process-local setting —
    # `serve --public-url` only configures the serving process, so the *other*
    # commands learn it from the --public-url flag (where they have one) or the
    # SYS_BUDDY_PUBLIC_URL env var. Export the env var once and they all agree.
    # The Slack webhook is read here — for EVERY mode — rather than only in `serve`,
    # where it used to live. Both `local` and the desktop app build their config through
    # this path, so pinging a human silently did nothing on the two on-ramps people
    # actually use. It is never persisted: the DB holds only sha256 token hashes and this
    # is a replayable bearer credential, so it stays process-local (env var or the Host
    # screen) and dies with the process.
    # The port is read here, not just in `local`/`serve`, because the host-side commands
    # PRINT URLS. `host-viewer` and `invite` are separate invocations that never touch a
    # socket, so without this they build a Config at DEFAULT_PORT and confidently tell you
    # to open :8787 while your broker is on :9292 — a link that either 404s or, worse,
    # lands on a *different* broker that doesn't know the token. Both flows also hand the
    # URL to another human. Falls back to $SYS_BUDDY_PORT, then 8787.
    cfg = set_config(
        Config(
            mode=mode,
            db_path=Path(getattr(args, "db", None) or DEFAULT_DB_PATH),
            port=getattr(args, "port", None) or DEFAULT_PORT,
            public_url=getattr(args, "public_url", None) or os.environ.get("SYS_BUDDY_PUBLIC_URL"),
            slack_webhook=os.environ.get("SLACK_WEBHOOK_URL") or None,
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
    # Print the SEATS, not the input: repeating a role type is how you say "two frontend
    # developers", and the host has to see what they were named — a repeated type numbers
    # every one of its seats (`frontend-1`, `frontend-2`), so neither is spelled like the
    # type they share and both can be addressed before anyone has joined.
    print(
        f"Created task '{task['id']}'  ·  seats: {', '.join(task['roles'])}  ·  "
        f"state: {task['state']}"
    )
    return 0


def cmd_task_add_seat(args: argparse.Namespace) -> int:
    from . import admin
    from .config import get_config
    from .onboarding import make_invite_link, make_join_url

    _cfg_from_args(args)
    seat = admin.add_seat(args.task, args.role, args.seat)
    origin = get_config().base_url
    print(f"Added seat '{seat['seat']}' ({seat['role']}) to task '{seat['task']}'")
    print(f"Invite: {seat['code']}")
    print(f"  expires: {seat['expires']} (single use)")
    print("\nSend them this link — they open it in a browser to set up:")
    print(f"  {make_join_url(origin, seat['code'])}")
    print("\nOr for the desktop app / CLI join:")
    print(f"  link: {make_invite_link(origin, seat['code'])}")
    print(f"  cli:  sys-buddy join {origin} {seat['code']} <name>")
    print(
        "\nThis seat is NOT retroactively a party to existing todos — a party list "
        "names who agreed, and they were not there."
    )
    return 0


def cmd_task_roster(args: argparse.Namespace) -> int:
    """The cast, from the SAME source the dashboard panel and the agents' `roster` tool
    read. Unjoined seats are listed: that is the state that silently stalls a task."""
    from . import admin

    _cfg_from_args(args)
    r = admin.roster(args.task)
    print(f"Cast of '{args.task}'  ·  {r['joined']} of {r['seats']} joined\n")
    print(f"  {'SEAT':<16} {'ROLE':<14} {'WHO':<16} {'PRE-FLIGHT':<12} PRESENCE")
    for row in r["rows"]:
        if not row["joined"]:
            who, pre = "—", "—"
            presence = "invite sent" if row["invite_pending"] else "not invited yet"
        else:
            who = row["name"] or "—"
            pre = row["readiness_status"] or "pending"
            presence = "listening" if row["listening"] else "idle"
        # `address`, not `seat`: they differ only for a seat whose stored handle is a
        # bare role type that a later-added sibling turned into the TYPE, and printing
        # the handle there would show the one token on this task that is ambiguous.
        # The roster exists to hand a human something they can type.
        token = row.get("address") or row["seat"]
        print(f"  @{token:<15} {row['role']:<14} {who:<16} {pre:<12} {presence}")
    return 0


def cmd_task_staging_url(args: argparse.Namespace) -> int:
    """Show or change the deployment target — the HOST's, and the only fetchable URL.

    A read with no `url` and no `--clear`, a write otherwise. The write is not a
    renegotiation: it touches no contract, no version and no signature, which is the
    entire point of the target living on the task rather than inside a signed spec.
    """
    from . import admin

    _cfg_from_args(args)
    if not args.url and not args.clear:
        cur = admin.get_staging_url(args.task, args.todo)
        scope = f"todo #{args.todo}" if args.todo is not None else "task"
        print(f"Deployment target for {args.task} ({scope}):")
        print(f"  task:      {cur['task'] or '—'}")
        if args.todo is not None:
            print(f"  override:  {cur['override'] or '— (inherits the task)'}")
        print(f"  effective: {cur['effective'] or '— (none set)'}")
        return 0
    res = admin.set_staging_url(args.task, None if args.clear else args.url, args.todo)
    where = f"todo #{res['todo']}" if res["todo"] is not None else "the task"
    print(f"Deployment target for {where} on '{args.task}': {res['staging_url'] or 'cleared'}")
    if res["previous"]:
        print(f"  was: {res['previous']}")
    print(f"  agents now resolve: {res['effective'] or '— (none set)'}")
    print(
        "\nNo contract, version or signature changed — the target is configuration, "
        "resolved live on every read."
    )
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    from . import admin
    from .config import get_config
    from .onboarding import make_invite_link, make_join_url

    _cfg_from_args(args)
    # Print the SEAT the invite opens, not the token the host typed: `@frontend-1` may
    # be the address of a seat stored as `frontend`, and a person's name is not a seat
    # at all. Echoing the input would name the wrong thing on the one line that matters.
    seat = admin.seat_for(args.task, args.role)
    code, expires = admin.mint_invite(args.task, args.role)
    origin = get_config().base_url
    print(f"Invite: {code}")
    print(f"  seat:    @{seat}")
    print(f"  task:    {args.task}")
    print(f"  expires: {expires} (single use)")
    print("\nSend your buddy this link — they open it in a browser to set up:")
    print(f"  {make_join_url(origin, code)}")
    print("\nOr for the desktop app / CLI join:")
    print(f"  link: {make_invite_link(origin, code)}")
    print(f"  cli:  sys-buddy join {origin} {code} <name>")
    return 0


def cmd_host_viewer(args: argparse.Namespace) -> int:
    from . import admin
    from .config import get_config

    _cfg_from_args(args)
    token = admin.issue_host_viewer(args.label)
    print(f"Host viewer token (all tasks): {token}")
    print(f"Open the dashboard at:  {get_config().base_url}/ui?v={token}")
    return 0


def _print_preview(pv: dict) -> None:
    """What you are joining, which SEAT you take, and who is already here.

    Printed only AFTER the invite validated server-side — a join surface that listed
    the cast for any code would turn a random guess into a team directory. The seat and
    its role type come off the invite, so the joiner knows what they are being
    onboarded as before they type anything.
    """
    print(f"You're joining: {pv.get('title') or pv.get('task_id')}")
    print(f"Your seat:      @{pv.get('seat')}  ·  {pv.get('role_type')}")
    cast = pv.get("cast") or []
    if cast:
        who = ", ".join(f"{c['name']} ({c['role']})" for c in cast)
        print(f"Already here:   {who}")
    else:
        print("Already here:   nobody yet — you're first")
    print()


def _ask_name(pv: dict | None, given: str | None) -> str | None:
    """The joiner's display name: unique per TASK, so ``@sarah`` is safe to type.

    The CLI checks the preview's list and RE-PROMPTS rather than watching every
    keystroke — a terminal has no debounce and no room for a live tick. That check is
    advisory either way: the guarantee is the insert-time one inside ``redeem_invite``'s
    immediate transaction, and this loop only spares the joiner a burnt round trip.

    ``given`` (``--name``) is used as-is when it is free, so scripted joins keep
    working. Returns ``None`` if there is nobody to ask (no tty, no ``--name``).
    """
    taken = set((pv or {}).get("taken_names") or [])
    fold = lambda s: " ".join(str(s or "").split()).lower()  # noqa: E731 — mirrors seats.fold_name

    name = (given or "").strip()
    while True:
        if name and fold(name) not in taken:
            return name
        if not sys.stdin.isatty():
            # Nobody to re-prompt. Hand the name over anyway and let the broker's
            # refusal be the ONE message — repeating an advisory guess beside the
            # authoritative answer just prints the same sentence twice.
            return name or None
        if name:
            print(
                f'error: "{name}" is already on this task — pick another name '
                f'(e.g. "{name} K")',
                file=sys.stderr,
            )
        try:
            name = input("Your name: ").strip()
        except EOFError:
            return None


def cmd_join(args: argparse.Namespace) -> int:
    from . import onboarding, pairing

    # join is a network client; no local db/config needed.
    pv = pairing.preview(args.url, args.code)
    if pv and pv.get("error"):
        # A dead invite is the invite's problem, not the name's — say so before asking
        # anyone to think of one.
        print(f"error: {pv['error']}", file=sys.stderr)
        return 1
    if pv:
        _print_preview(pv)

    name = _ask_name(pv, args.name)
    if not name:
        print(
            "error: a name is required — pass --name, or run this where you can type.",
            file=sys.stderr,
        )
        return 2

    result = pairing.join(args.url, args.code, name, pubkey=args.pubkey)
    if result is None:
        return 1
    print("Paired successfully.\n")
    print(f"  task:          {result['task_id']}")
    print(f"  role:          {result['role']}")
    print(f"  mcp_url:       {result['mcp_url']}")
    print(f"  agent_token:   {result['agent_token']}")
    print(f"  dashboard_url: {result['dashboard_url']}")
    # Rendered from the client registry, never spelled out here: a hand-written copy of
    # the `claude mcp add` line is exactly the kind of literal that drifts out of sync
    # with onboarding.py and then fails silently.
    print("\nRegister the MCP with (the remove line is a no-op the first time,")
    print("and lets you re-pair later with a new URL/token without a collision):")
    for line in onboarding.claude_setup_command(
        result["mcp_url"], result["agent_token"]
    ).splitlines():
        print(f"  {line}")
    # Every other client we support (Claude desktop, Cursor, Gemini CLI, Other) is in
    # `result["clients"]` — same renderer, same two facts, different packaging.
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


def cmd_todo_list(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    rows, roll = admin.list_todos(args.task)
    if not rows:
        print(f"No todos on task '{args.task}'. Agents propose them with propose_todo().")
        return 0
    if roll:
        # `unjoined` is a SUBSET of `pending`, and it is called out because the fix is
        # different: pending is a colleague to nudge, this is an invite to chase.
        never = f"  ·  {roll['unjoined']} on a seat nobody joined" if roll.get("unjoined") else ""
        print(
            f"  {roll['verified']}/{roll['total']} verified"
            f"  ·  {roll['pending']} awaiting acceptance"
            f"{never}"
            f"  ·  {roll['stuck']} stuck"
            f"  ·  task state: {roll['state']}"
        )
    for t in rows:
        # `⚠` marks the only rows a HUMAN can unblock: a pending todo is waiting on
        # someone's acceptance, a stuck one on someone's help.
        flag = "⚠" if t["status"] == "pending" or t["stuck"] else " "
        # One note, most-final first: a dropped todo's stale "awaiting mobile" would read
        # as an outstanding ask when the ask is what was abandoned.
        note = ""
        if t["status"] == "dropped":
            note = f"dropped by {t['dropped_by']}: {t['drop_reason']}"
        elif t["stuck"]:
            note = f"stuck: {t['stuck_reason']}"
        elif t["declined_by"]:
            note = f"declined by: {', '.join(t['declined_by'])}"
        elif t["awaiting"]:
            # "awaiting priya" and "awaiting a seat nobody ever accepted" look identical
            # without the tag, and they need different actions.
            never = set(t.get("unjoined") or [])
            note = "awaiting: " + ", ".join(
                f"{p} (never joined)" if p in never else p for p in t["awaiting"]
            )
        print(
            f"  {flag} #{t['number']:<4} {t['status']:<11} {','.join(t['parties']):<24} "
            f"{t['title']}"
        )
        if note:
            print(f"      {note}")
    print(
        "\nA todo hanging on a party whose human went offline is the host's to drop:\n"
        f"  sys-buddy todo drop {args.task} <N> --reason \"...\""
    )
    return 0


def cmd_todo_drop(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    t, roll = admin.host_drop_todo(args.task, args.todo, args.reason)
    print(f"Dropped todo #{t['number']} '{t['title']}' on task '{args.task}'.")
    print(f"  bound:  {', '.join(t['parties'])}")
    print(f"  reason: {t['drop_reason']}")
    print(
        "\nPosted to the task thread as 'broker' (a human decision, not a peer's), so "
        "the\nabsent party's agent finds an explanation rather than vanished work."
    )
    if roll:
        print(f"\nTask rollup is now {roll['verified']}/{roll['total']} verified · state {roll['state']}.")
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
    tc.add_argument(
        "--roles", required=True,
        help="Comma-separated ROLE TYPES, e.g. backend,frontend. Repeat one for two "
             "people doing the same job — backend,backend,frontend seats @backend-1, "
             "@backend-2 and @frontend (a repeated type numbers ALL of its seats, so no "
             "seat is named after a role type that several of them share)",
    )
    tc.add_argument("--title", help="Human title (defaults to the id)")
    tc.add_argument(
        "--mode",
        choices=["contract", "debug"],
        default="contract",
        help="'contract' (full workflow) or 'debug' (collaborate then mark resolved)",
    )
    tc.set_defaults(func=cmd_task_create)

    ts = tsub.add_parser(
        "add-seat",
        help="Add a seat to a live task and mint its invite (the cast is not frozen at setup)",
    )
    ts.add_argument("task")
    ts.add_argument("--role", required=True, help="Role type, e.g. qa")
    ts.add_argument("--seat", help="Seat handle (default: derived, e.g. frontend-2)")
    ts.set_defaults(func=cmd_task_add_seat)

    tr = tsub.add_parser("roster", help="Show a task's cast, including seats nobody has taken")
    tr.add_argument("task")
    tr.set_defaults(func=cmd_task_roster)

    tu = tsub.add_parser(
        "staging-url",
        help="Show or change the deployment target — the ONE URL agents may fetch",
        description=(
            "The deployment target is host-owned CONFIGURATION, not part of any signed "
            "contract. Changing it here re-points every contract on the task at once, "
            "with no version, no re-signature and no renegotiation — which is what makes "
            "a rotated ngrok URL a one-line fix instead of a re-signed shape. It is also "
            "why no agent tool can set it: there is nothing for an injected 'test against "
            "evil.com' to write to."
        ),
    )
    tu.add_argument("task")
    tu.add_argument(
        "url",
        nargs="?",
        help="The new target (absolute https, or anything on a same-machine task). "
             "Omit to SHOW the current one; pass --clear to unset it.",
    )
    tu.add_argument(
        "--todo",
        type=int,
        help="Set the override for ONE deliverable (#N) instead of the whole task",
    )
    tu.add_argument(
        "--clear", action="store_true",
        help="Unset it. On a todo that means 'inherit the task's again'.",
    )
    tu.set_defaults(func=cmd_task_staging_url)

    sp = sub.add_parser("tasks", help="List tasks")
    sp.set_defaults(func=cmd_tasks)

    # The host's todo surface. `drop` is a WRITE and deliberately lives here rather than
    # on the dashboard (D11): it is the escape hatch for a party whose human went offline
    # and will never accept, and many hosts run `sys-buddy serve` with no GUI at all.
    todo = sub.add_parser("todo", help="Todos on a task (host view, and the host's drop)")
    tosub = todo.add_subparsers(dest="todo_command", required=True)
    tl = tosub.add_parser("list", help="List a task's todos, with the rollup")
    tl.add_argument("task")
    tl.set_defaults(func=cmd_todo_list)
    td = tosub.add_parser(
        "drop", help="Drop a todo unilaterally (for a party who went offline)"
    )
    td.add_argument("task")
    td.add_argument("todo", help="Todo NUMBER (#N per task), as shown by `sys-buddy todo list <task>`")
    td.add_argument(
        "--reason", required=True,
        help="Why — posted to the task thread, and the only thing the agents will see.",
    )
    td.set_defaults(func=cmd_todo_drop)

    sp = sub.add_parser("invite", help="Mint a single-use invite for a role")
    sp.add_argument("--task", required=True)
    sp.add_argument("--role", required=True)
    sp.add_argument(
        "--public-url",
        help="Tunnel origin for the links (e.g. https://abc123.ngrok.app). "
        "Defaults to $SYS_BUDDY_PUBLIC_URL, else loopback.",
    )
    sp.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port your broker is on, for the loopback links (default: {DEFAULT_PORT}). "
             "Ignored when --public-url is set.",
    )
    sp.set_defaults(func=cmd_invite)

    sp = sub.add_parser("host-viewer", help="Issue an all-tasks host viewer token")
    sp.add_argument("--label", default="host")
    sp.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port your broker is on, for the printed dashboard link (default: {DEFAULT_PORT}).",
    )
    sp.set_defaults(func=cmd_host_viewer)

    sp = sub.add_parser("join", help="(buddy side) Redeem an invite for tokens")
    sp.add_argument("url", help="Broker base URL, e.g. https://abc123.ngrok.app")
    sp.add_argument("code", help="Invite code")
    sp.add_argument(
        "--name",
        help="Your display name, e.g. Sarah. Unique per TASK (so `@sarah` is safe to "
             "type). Omit it and you'll be shown the cast and asked.",
    )
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
