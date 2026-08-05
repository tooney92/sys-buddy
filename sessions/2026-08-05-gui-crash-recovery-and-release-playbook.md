# The GUI took the broker down with it, a release playbook for a second dev, and a token audit

**Shipped:** nothing · **New file:** `docs/release-playbook.md` (untracked) · **Branch:** `docs/session-2026-08-05`
**Product finding worth acting on:** the desktop app's crash is a broker outage
**Deployment finding:** `8787` on an unfirewalled VPS is the broker in cleartext, TLS bypassed

---

## The crash — and the thing it exposed

`sys-buddy gui` aborted mid-session with an uncaught `NSException`:

```
'OC_PythonException', reason: '<class 'AttributeError'>: 'NoneType' object has no attribute 'frame''
  -[NSWindow _setFrameCommon:display:fromServer:]
  -[_NSFullScreenContentController reshapeContentForTileContentFrame:fromFrame:]
```

A PyObjC/pywebview bug on the **fullscreen transition** — AppKit posts a window-frame
notification, the Python observer gets a `None` window and raises across the ObjC boundary,
which is fatal. Not ours, not fixable from our code, and it will happen again to anyone who
fullscreens the app.

**The part that is ours:** the broker went down with it. `gui.py:44` runs the broker
in-process on 8787 as a *daemon thread* — the comment says "kept in a daemon thread so it
dies with the app — no stray server to remember to kill." That tradeoff was chosen for
tidiness, and its real cost showed up here: **a window-manager bug in a UI toolkit is a
full outage for every connected agent.** The buddy's agents on a live task lost their MCP
endpoint because a window was resized.

Worth reconsidering. Options, cheapest first:

1. **Wrap the pywebview window callbacks** so an exception in an ObjC observer can't abort
   the process. Narrow, doesn't change the architecture.
2. **Supervise instead of embed** — the app spawns the broker as a child process and
   restarts it if it dies. Keeps "no stray server" (kill it on clean exit) without coupling
   its lifetime to AppKit's.
3. Leave it, and **document the recovery** so the outage is a two-minute one.

We got (3) for free this session; (1) is small and I'd do it next.

## Recovery, verified

Nothing was lost. `~/.sys-buddy/sys_buddy.db` was intact — 2.1 MB, and **no leftover
`-wal`/`-shm` files**, so the last write had checkpointed cleanly. The live task
`light-dey-live-phase-2-ad18` still held all three seats (`backend`/host, `T-frontend`,
`T-mobile`, joined 2026-08-04 22:32–22:33) with their token hashes valid. No todos had
been proposed yet; 9 messages on the thread.

The recovery is to run the broker **outside** the app:

```bash
sys-buddy serve --host 127.0.0.1 --port 8787
```

That reproduces exactly what the GUI hosted — same default db, same `mode="remote"`, same
port — so **every issued seat token and MCP URL keeps working** and agents reconnect on
their next call. No re-pairing, no new task.

**And the two coexist safely.** `_ensure_broker` probes `GET /ui` before starting its
thread, and `/ui` is unauthenticated (the viewer token gates `/api/*`, not the page), so a
GUI launched afterwards *detects and reuses* the CLI broker instead of trying to bind 8787
twice. This is already the documented behaviour in the v2.4.0 restart banner — it's
correct, and it's the argument for making the standalone broker the normal way to run.

The broker was back on 8787 (PID 6129) before the session ended.

**Housekeeping:** two dev brokers have been running since Monday — `:9292` (from 01:00) and
`:9696` against `~/.sys-buddy-dev-merged/`. Harmless, but they're not from this session and
nobody is using them.

## `docs/release-playbook.md` — teaching the pipeline to a second dev

Written to hand to another developer so his own agent can set his repo up the same way.
Two parts by design: **prose that explains the reasoning**, then **a fenced prompt** he
pastes into his agent.

What it documents, all drawn from what this repo actually does:

* **Squash-merge-only is load-bearing** — it's what makes the PR title the commit on
  `main`, which is what release-please reads. Plus the type→bump table from
  `CONTRIBUTING.md` §8 and the thing people get wrong when bumping by hand: a release
  bundles every PR since the last one and bumps **once**, by the largest change.
* **Merging the Release PR *is* the release.** Nothing is published because someone
  remembered to publish it.
* **The two hand-written artifacts**, because the generated `CHANGELOG.md` is thin by
  design — `releases/vX.Y.Z.md` prose, and the `features/vX.Y.Z/` bundle.
* **Why screenshots are committed**, argued from `features/v1.1.0/README.md`: the dashboard
  builds its DOM at runtime from `/api/*`, so a version's visual design is *unrecoverable*
  once the demo db is deleted. Checked-in screens are how an agent sees the real visual
  language instead of inventing one. Rules: real seeded data never mockups, numbered
  filenames with the theme in the name, both themes, fixed 1440×1000, seed script beside
  them (screens are the cache, the seed is the source).
* **The close-and-reopen gotcha**, framed as "this will bite you exactly once" — with the
  honest note that it cost us the same twenty minutes across four releases before it got
  written down.
* The sequence, with steps 5–7 flagged as the ones that get skipped, and: a published
  version can never be withdrawn, only superseded, so a dry run belongs *before* the merge.

The embedded prompt surveys before writing, adopts mid-stream (seeds the manifest with the
current version rather than restarting at 0.1.0), keeps the
`RELEASE_PLEASE_TOKEN || GITHUB_TOKEN` fallback, keeps publish jobs in the same workflow,
checks that screenshots aren't caught by a blanket `*.png` ignore, and won't declare done
until it has watched the title check **fail** on a deliberately bad title.

**Not committed.** It's untracked on this branch. It belongs in the repo — the type table
and the release sequence are ours and drift if they live only in a Slack DM.

**Slack tip, learned the hard way:** the workspace rejects `.md` uploads by extension
("Try uploading a .zip version"). `.txt` was also refused; the zip went through.

## Token audit — can an agent ask the broker for another agent's token?

Asked, and checked rather than assumed. **No**, blocked three independent ways:

1. **The broker doesn't have it to give.** `agents` stores only `sha256(token)`; raw tokens
   are never persisted (`identity.py:9`). Same for invite codes. Full database read access
   yields no usable credential.
2. **No tool takes an "other agent" parameter.** Identity is not asserted by an argument —
   middleware resolves the bearer token to one row into a ContextVar, read via
   `require_current()` (`identity.py:101`). `rotate_token` is the only tool returning a
   token, and it writes `WHERE id = ident.agent_id` (`tools.py:418`) with no way to
   redirect it.
3. **Issuance isn't on the MCP surface** — tokens are minted only by redeeming an invite on
   `POST /pair`. `get_roster` knows the most about peers and its SELECT is
   `id, name, role, handle, ready, readiness_status, listening_*` — no `token_hash`
   (`seats.py:567`). An agent sees `invite_pending: true`, never the code.

Three caveats recorded honestly:

* **`local` mode has no auth at all.** `register_tools` branches on `cfg.is_remote`, and the
  local path uses `_local_identity(task, agent)` (`tools.py:91`) — the caller *declares* who
  it is. So the answer is "no" for `serve`/GUI and "not applicable" for `local`.
* **An invite code is a bearer credential for a seat.** Single-use, 15 minutes, rate-limited
  — but anyone holding one becomes that seat. The MCP surface never emits one, so the only
  realistic leak is a human pasting one into a message or a shared file.
* The **host** can mint anything via the CLI. That's a human with disk access, not an agent.

### One naming fix worth making

`seats.roster()`'s docstring describes the `address` field as *"the token a human should
actually TYPE."* That's an **addressing string** (`@frontend-1`), not a credential — and in
a codebase where `token` otherwise always means a bearer secret, the overloading reads like
the roster hands out tokens. Rename it in the prose (`the handle to type` / `the address`).
Cheap, and it removes a genuine misreading.

## Deploying to a public box — what the standard hardening list is actually for

Owner is new to deployments and asked what `unattended-upgrades`, a 22/80/443 firewall and
`fail2ban` are for. Explained; two points from it are about **this repo**, not general
sysadmin, and are the reason it's recorded here.

**The broker binds 8787, and that is a hole in a public deployment.** On a VPS with no
firewall, `sys-buddy serve` is reachable at `http://<ip>:8787` by anyone — bearer tokens in
cleartext, going *around* whatever TLS terminator was carefully put in front of it. The
`--public-url` https check refuses to hand out cleartext pairing links, but it cannot stop
someone connecting to the raw port directly. **A default-deny firewall is what makes that
check meaningful.** So the deployment shape is: 8787 closed at the firewall, reachable only
via nginx/Caddy on 443 or a tunnel.

Worth considering as a product change: `serve` currently defaults its bind host from
`--host`. A warning when it binds a non-loopback address without a `public_url` would catch
this at the moment someone makes the mistake, which is the same "say it in the run" pattern
already used for the release-token gap.

**Backups of `sys_buddy.db` cannot be a `cp`.** It's SQLite in WAL mode; copying the file
while the broker is writing gives you a torn database, and — as this same session showed —
the `-wal` may hold committed data the main file doesn't. Any backup story has to use
`.backup` or `sqlite3 .dump`. Nothing in the docs says this yet.

The rest was general and stands on its own: patch automation closes the
CVE-published-to-patched window; the firewall's value is as a backstop for the port you
bind by accident later; and fail2ban is a noise reducer, not a boundary — `PasswordAuthentication no`
plus `PermitRootLogin no` is the change that actually removes SSH brute force.

---

## Next

1. **Commit `docs/release-playbook.md`** — `docs:` PR, no version bump.
2. **Guard the pywebview window callbacks** (option 1 above) so an ObjC observer exception
   can't take the broker down. Small, and it fixes a real outage class.
3. **Reword the `address`/"token" docstring** in `seats.py`. Trivial, ship it with anything.
4. Open question from the audit, not chased: can an invite code reach agent-visible content
   through the message or file paths? Worth a grep before it matters.
5. **Warn when `serve` binds a non-loopback host with no `public_url`** — catches the
   exposed-8787 mistake at the moment it's made. Same "say it in the run" pattern as the
   release-token gap.
6. **Document backing up `sys_buddy.db`** — `.backup`/`.dump`, never `cp`, because WAL.
   Belongs wherever the deployment instructions end up.
7. PR #65 (issues on debug tasks) is still open and unmerged from the previous session —
   merging it cuts 2.6.0.
