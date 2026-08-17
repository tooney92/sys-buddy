"""``sys-buddy`` command-line interface.

Host-side commands (init, task create, invite, revoke-*, close, todo) operate on the
local SQLite file directly — they run on the same machine as the broker. ``join``
is the one network client: it runs on the *buddy's* machine and POSTs to /pair.
``local`` and ``serve`` start the server.

This is also where the HOST's own writes live, and deliberately not on the dashboard
(D11): ``todo drop`` is the escape hatch for a todo hanging on a party whose human
went offline, ``todo drop-party`` the smaller one that removes only that party and
lets the deliverable carry on, and a host running ``sys-buddy serve`` headless has no
GUI to click. Both are host-only because no AGENT may remove a peer from an agreement
(D14); the self-service half is the ``leave_todo`` tool, which cannot name anyone else.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
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
    task = admin.create_task(
        args.id, title=args.title or args.id, roles=roles, mode=args.mode,
        dev_url=getattr(args, "dev_url", None),
    )
    # Print the SEATS, not the input: repeating a role type is how you say "two frontend
    # developers", and the host has to see what they were named — a repeated type numbers
    # every one of its seats (`frontend-1`, `frontend-2`), so neither is spelled like the
    # type they share and both can be addressed before anyone has joined.
    print(
        f"Created task '{task['id']}'  ·  seats: {', '.join(task['roles'])}  ·  "
        f"state: {task['state']}"
    )
    return 0


def cmd_task_dev_url(args: argparse.Namespace) -> int:
    """Show or set the LOCAL dev URL (localhost/http fine). Task-level, host-owned.

    A read with no `url` and no `--clear`, a write otherwise. Deliberately lenient — it
    names where the app runs during development, not the SSRF-checked deployment target.
    """
    from . import admin

    _cfg_from_args(args)
    if not args.url and not args.clear:
        print(f"Local dev URL for {args.task}: {admin.get_dev_url(args.task) or '— (none set)'}")
        return 0
    res = admin.set_dev_url(args.task, None if args.clear else args.url)
    print(f"Local dev URL for '{args.task}': {res['dev_url'] or 'cleared'}")
    return 0


def cmd_task_extend_tokens(args: argparse.Namespace) -> int:
    from . import admin

    _cfg_from_args(args)
    try:
        touched = admin.extend_agent_tokens(
            args.task, hours=args.hours, never=args.never
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not touched:
        print(f"No live agents on '{args.task}' — nothing to extend.")
        return 0
    when = "no expiry" if args.never else _fmt_local(touched[0]["now"])
    for t in touched:
        # Flag the ones that were ALREADY dead: that is the seat whose agent is locked out
        # right now, and the reason the host is running this command.
        was = " (was EXPIRED)" if t["was_expired"] else ""
        print(f"  @{t['seat']:<16} {t['name']}{was}")
    print(f"Extended {len(touched)} token(s) on '{args.task}' → {when}")
    if not args.never:
        print("  Agents need no action: the broker re-reads the token on every call, so")
        print("  they are live again immediately — no re-pair, no session restart.")
    return 0


def _fmt_local(ts: float | None) -> str:
    if ts is None:
        return "no expiry"
    return time.strftime("%a %d %b %H:%M", time.localtime(ts))


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


def cmd_task_add_guest(args: argparse.Namespace) -> int:
    from . import admin
    from .config import get_config

    _cfg_from_args(args)
    guest = admin.add_guest(args.task, args.name, args.seat)
    origin = get_config().base_url
    print(f"Added guest '{guest['name']}' as seat '{guest['seat']}' on task '{guest['task']}'")
    print("\nSend them this link — they open it in a browser and just type. No install, no terminal:")
    print(f"  {origin}/ui?v={guest['viewer_token']}")
    print(
        "\nThat link is a WRITE credential for this one guest seat (the only write the "
        "dashboard grants) — share it like a password, and revoke it with `revoke-viewer`."
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
        # WHO IS NO LONGER ON IT. Printed separately from the party list rather than
        # struck through inside it: the list answers "who is bound", this answers "who
        # used to be, and did they choose to go or did a human cut them loose" — and a
        # host reading this screen is usually deciding whether to do exactly that.
        latest = {d["seat"]: d for d in t.get("departed") or []}
        gone = [d for seat, d in latest.items() if seat not in t["parties"]]
        if gone:
            print(
                "      left: "
                + ", ".join(f"{d['seat']} ({d['mode']}: {d['reason']})" for d in gone)
            )
    # TWO host moves, and the smaller one is listed first because it is almost always the
    # right one: the usual situation is that ONE party went dark and the others still want
    # the deliverable. Dropping the whole todo throws away work nobody asked to abandon.
    print(
        "\nA todo hanging on a party whose human went offline is the host's to unblock:\n"
        f"  sys-buddy todo drop-party {args.task} <N> --seat <handle> --reason \"...\"\n"
        "      removes just that party; the todo lives on and its quorum recomputes\n"
        f"  sys-buddy todo drop {args.task} <N> --reason \"...\"\n"
        "      abandons the whole deliverable"
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


def cmd_todo_drop_party(args: argparse.Namespace) -> int:
    """Remove one party from a todo, and PRINT WHAT IT UNBLOCKED.

    The last line matters as much as the removal. The host runs this because a todo is
    frozen; "removed mobile" alone leaves them to go and check whether that actually
    achieved anything, so the rollup and the todo's own status are printed back — a
    contract that just locked or an issue that just resolved shows up as a changed
    status right here rather than being discovered later on the dashboard.
    """
    from . import admin

    _cfg_from_args(args)
    number = args.todo if args.todo is not None else args.todo_flag
    if number is None:
        print(
            "which todo? Give its number: "
            f"sys-buddy todo drop-party {args.task} <N> --seat <handle> --reason \"...\"",
            file=sys.stderr,
        )
        return 2
    if args.todo is not None and args.todo_flag is not None:
        print(
            "todo number given twice (positionally and with --todo) — supply it once.",
            file=sys.stderr,
        )
        return 2

    before = {t["number"]: t for t in admin.list_todos(args.task)[0]}
    t, roll = admin.host_remove_party(args.task, number, args.seat, args.reason)
    was = before.get(t["number"], {})
    gone = [d for d in t.get("departed") or [] if d["mode"] == "removed"]
    seat = gone[-1]["seat"] if gone else args.seat
    print(f"Removed {seat} from todo #{t['number']} '{t['title']}' on task '{args.task}'.")
    print(f"  still bound: {', '.join(t['parties']) or '(nobody)'}")
    print(f"  reason:      {args.reason}")
    print(
        "\nPosted to the task thread as 'broker' (a human decision, not a peer's), so "
        f"the\nremoved party's agent finds an explanation rather than vanished work. "
        f"{seat} is\nstill on the TASK — it is this one deliverable it is no longer bound "
        "by."
    )
    # What the removal actually achieved. `status` is derived from the party list, the
    # acceptances and the contracts, so a change here IS the recomputed quorum.
    if was.get("status") and was["status"] != t["status"]:
        print(f"\nTodo #{t['number']} moved {was['status']} → {t['status']}.")
    else:
        print(f"\nTodo #{t['number']} is {t['status']} (state {t['state']}).")
    if t["awaiting"]:
        print(f"  still awaiting: {', '.join(t['awaiting'])}")
    if roll:
        print(
            f"Task rollup is now {roll['verified']}/{roll['total']} verified · "
            f"state {roll['state']}."
        )
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
    # The one module-level need for `admin` in this file: `--mode`'s choices come from
    # `admin.MODES` rather than being retyped here, which is how the CLI stopped being a
    # third place that could disagree about which workflows exist. Imported inside the
    # function like every other use, so `--help` stays cheap to reach.
    from . import admin

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
        # From admin.MODES so a new workflow cannot ship with no way to select it.
        choices=list(admin.MODES),
        default="contract",
        help="'contract' (full workflow), 'debug' (collaborate then mark resolved), or "
             "'engagement' (contract flow plus a client — needs an 'owner' seat in the cast)",
    )
    tc.add_argument(
        "--dev-url",
        help="Local dev URL where the app runs, e.g. http://localhost:3000. Unlike the "
             "staging target this is lenient — localhost/http/a bare host are all fine.",
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

    tg = tsub.add_parser(
        "add-guest",
        help="Add a non-technical GUEST (browser message box, no AI) and print their link",
    )
    tg.add_argument("task")
    tg.add_argument("--name", required=True, help="Display name shown in the thread, e.g. Ada")
    tg.add_argument("--seat", help="Seat handle (default: derived, e.g. guest or guest-2)")
    tg.add_argument(
        "--public-url",
        help="Tunnel origin for the guest's link (e.g. https://abc123.ngrok.app). "
        "Defaults to $SYS_BUDDY_PUBLIC_URL, else loopback.",
    )
    tg.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port your broker is on, for the loopback link (default: {DEFAULT_PORT}). "
             "Ignored when --public-url is set.",
    )
    tg.set_defaults(func=cmd_task_add_guest)

    te = tsub.add_parser(
        "extend-tokens",
        help="Push back the expiry on a task's agent tokens (or lift it entirely)",
    )
    te.add_argument("task")
    te.add_argument(
        "--hours", type=float, default=24.0,
        help="How much longer the tokens should live, from now (default: 24)",
    )
    te.add_argument(
        "--never", action="store_true",
        help="Remove the expiry altogether — what you want for a long session or a demo",
    )
    te.set_defaults(func=cmd_task_extend_tokens)

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

    tdv = tsub.add_parser(
        "dev-url",
        help="Show or set the LOCAL dev URL (localhost/http ok) — where the app runs in dev",
        description=(
            "The local dev URL is host-owned configuration, like the staging target, but "
            "deliberately lenient: localhost, http and bare hosts are all fine, because it "
            "names where the app runs while you build rather than a fetchable deployment "
            "target. No agent tool can set it."
        ),
    )
    tdv.add_argument("task")
    tdv.add_argument(
        "url", nargs="?",
        help="The dev URL, e.g. http://localhost:3000. Omit to SHOW; --clear to unset.",
    )
    tdv.add_argument("--clear", action="store_true", help="Unset it.")
    tdv.set_defaults(func=cmd_task_dev_url)

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

    # `drop-party` is `drop`'s smaller sibling and belongs beside it for the same reason:
    # it is the answer to a party who went dark, and the agents cannot supply it (a
    # silent agent cannot call `leave_todo`, and no agent may ever remove a peer).
    #
    # It takes the todo positionally, exactly like `drop` — the two sit next to each other
    # and a host who has typed one should not have to re-learn the shape for the other.
    # `--todo N` is accepted as an alias because that is how it reads in prose and in the
    # dashboard's own hint; supplying both, or neither, is an error rather than a guess.
    dp = tosub.add_parser(
        "drop-party",
        help="Remove ONE unresponsive party from a todo, leaving the todo alive",
    )
    dp.add_argument("task")
    dp.add_argument(
        "todo", nargs="?",
        help="Todo NUMBER (#N per task), as shown by `sys-buddy todo list <task>`",
    )
    dp.add_argument("--todo", dest="todo_flag", help="Same thing, named.")
    dp.add_argument(
        "--seat", required=True,
        help="The seat to remove (@handle, role, or that person's display name).",
    )
    dp.add_argument(
        "--reason", required=True,
        help="Why — posted to the task thread, and the only thing the agents will see.",
    )
    dp.set_defaults(func=cmd_todo_drop_party)

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


# --------------------------------------------------------------------------- #
# command catalog — the HOST CLI, READ OUT OF THE PARSER ABOVE
# --------------------------------------------------------------------------- #
# The dashboard's Commands panel lists these, and it must never carry a second,
# hand-typed copy of them. Every hand-maintained list in this codebase has drifted —
# the shortcode cheatsheet lost `upto`, the file-types list drifted twice, the website's
# releases page sat four versions stale — and a stale list of commands is worse than
# those, because the reader concludes the capability does not exist. That is exactly
# what happened with `task staging-url --todo N`, which had shipped in v2.0.0 and cost a
# working session because nothing on screen said it was there.
#
# So the panel is GENERATED. `ui.html` is a static file and cannot import Python, so the
# seam is the API: `api._cli_catalog` serves this, the page renders it. Nothing here is
# written down twice — walk `build_parser()` and you have, by construction, exactly what
# the CLI accepts.
def _arg_entry(action: argparse.Action) -> dict:
    """One argument, as the panel needs to read it: what to type, and why.

    ``usage`` carries its own brackets — ``<task>`` is required, ``[--todo N]`` is not —
    because the bracket convention IS the required/optional fact and splitting them
    across two fields invites a renderer that disagrees with itself.
    """
    if action.option_strings:
        flag = action.option_strings[0]
        # nargs == 0 is argparse's spelling of a flag that takes no value (store_true).
        takes_value = action.nargs != 0
        metavar = action.metavar or action.dest.upper()
        spelling = f"{flag} <{metavar}>" if takes_value else flag
        return {
            "name": flag,
            "kind": "option",
            "required": bool(action.required),
            "usage": spelling if action.required else f"[{spelling}]",
            "help": action.help or "",
        }
    name = action.metavar or action.dest
    optional = action.nargs in ("?", "*")
    return {
        "name": name,
        "kind": "positional",
        "required": not optional,
        "usage": f"[{name}]" if optional else f"<{name}>",
        "help": action.help or "",
    }


def _visible_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Everything a human types, minus argparse's own plumbing.

    ``--help`` and ``--version`` are argparse furniture on every parser; listing them
    once per command would bury the arguments that differ.
    """
    return [
        a
        for a in parser._actions
        if not isinstance(a, (argparse._HelpAction, argparse._VersionAction,
                              argparse._SubParsersAction))
    ]


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _walk(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> list[dict]:
    """Every LEAF command under ``parser``, in registration order.

    A parser that itself has subcommands (``task``, ``todo``) is a grouping, not
    something you can run, so it contributes its name to its children's path and no row
    of its own. Registration order is preserved (argparse's ``choices`` is insertion
    ordered), which is why the rows cluster by group without anything sorting them.
    """
    sub = _subparsers(parser)
    if sub is None:
        return []
    # Subcommand help lives on the pseudo-actions argparse builds for the listing, not
    # on the child parser — read it from there rather than restating it.
    helps = {a.dest: (a.help or "") for a in sub._choices_actions}
    out: list[dict] = []
    for name, child in sub.choices.items():
        child_path = path + (name,)
        if _subparsers(child) is not None:
            out.extend(_walk(child, child_path))
            continue
        args = [_arg_entry(a) for a in _visible_actions(child)]
        out.append(
            {
                "name": " ".join(child_path),
                # "" for a top-level command, else the parent it hangs under, so the
                # panel can head `task create` / `task roster` with one `task` label
                # without a second table saying which commands are nested.
                "group": " ".join(child_path[:-1]),
                "help": helps.get(name, "") or (child.description or ""),
                "usage": " ".join(["sys-buddy", *child_path] + [a["usage"] for a in args]),
                "args": args,
            }
        )
    return out


def command_catalog() -> dict:
    """The host's whole CLI, derived from :func:`build_parser`.

    ``{"commands": [...], "global_options": [...]}``. Pure and cheap; the API memoises
    it because a parser is identical for the life of a process.
    """
    p = build_parser()
    return {
        "commands": _walk(p),
        "global_options": [_arg_entry(a) for a in _visible_actions(p)],
    }


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
