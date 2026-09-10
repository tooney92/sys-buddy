# Resume a session — plan

## The problem, from a real morning

Picking a collaboration back up the next day cost an hour of archaeology. Four
independent things had rotted overnight, and **only one of them needed a re-invite**:

| What broke | What it actually needed |
| --- | --- |
| ngrok URL rotated | peer re-points config (keeps token) |
| agent tokens 4h from expiry | extend |
| staging target (Cloudflare) dead | `set_staging_url` |
| buddy's dashboard link unrecoverable | ← *only this one needs re-invite* |

The host had no way to see any of it. There is no "where do I even open my dashboard"
front door, no per-task health view, and no way to reissue a buddy's dashboard link —
which is why a healthy seat had to be revoked and re-paired to recover one lost URL.

**Both tunnels are ephemeral.** The broker's ngrok URL and the staging Cloudflare URL
rotate independently. Rotation is the normal case, not the exception.

## Design principle

**Resume is a diagnosis, not an action.** A single "Resume" button that revokes and
re-invites is a footgun: click it on a healthy session and you have kicked out a peer who
was working fine. So: probe everything, report per row, offer a *targeted* fix per row,
and keep revoke behind a confirm as the last resort.

**Probe, don't infer.** Ask the URL. Note the nuance learned the hard way: a Rails app
returns **404 at `/` and that is healthy**; a dead tunnel returns **000**. The check is
"did we get an HTTP response at all", not "was it a 200".

## Non-goals (for now)

- **Tier 3 — the app owning the tunnels.** Launching/supervising `ngrok` and `cloudflared`
  so staging self-heals. Real value, but it drags in process supervision, binary+auth
  management, and the ngrok-free **one-agent-session** limit. Earn it after detection ships.
- Any change to auth, contracts, or the state machine.

## Build order

Each step is independently useful and independently verifiable.

### 1. Buddy dashboard-link reissue — the missing primitive
Guests can have a link reissued (`admin.reissue_guest_link`); buddies cannot. That asymmetry
is the sole reason a working seat gets revoked. Fixing it makes Resume non-destructive.

- `admin.reissue_viewer_link(task, who)` — mint a fresh per-task viewer for an EXISTING
  live agent seat, matched by seat handle or display name. Same seat, new credential.
  Must refuse a guest (they have their own path) and refuse an unknown/revoked seat.
- CLI: `sys-buddy task viewer-link <task> <who> [--public-url] [--port]`
- Host route: `POST /host/viewer-link`, same `_host()` gate, 403 for anyone else.

### 2. Remember the broker's public URL
The enabling data for "changed since last session". Nothing records it today.

- Persist last-seen public origin (settings row or `tasks` column — pick the smaller change).
- Written on boot / when the origin is known.

### 3. Tunnel discovery + probes
- `tunnels.discover_ngrok()` — `GET 127.0.0.1:4040/api/tunnels`, return
  `{public_url, forwards_to}`. Works no matter who started ngrok. Also lets us verify the
  tunnel actually points at the broker's port.
- `tunnels.probe(url)` — reachable? `000`/timeout = dead; any HTTP status = alive.

### 4. Session health check — the engine
`health.session_report(task)` returning a list of rows:
`{key, label, status: ok|warn|dead|info, detail, fix: {label, cli, action}}`
covering: broker up · public URL changed · staging alive · token expiry · per-seat link state.
This is the substrate both surfaces render.

### 5. CLI surface
`sys-buddy task resume <task>` — prints the report with the CLI fix under each row.
Immediately useful, and proves the engine before any UI exists.

### 6. GUI surface
- A **"Resume session"** card on the desktop app home that opens the host dashboard.
- Per-task **Resume** → the health panel, one fix button per row, CLI shown alongside.
- Revoke + re-invite present but behind a confirm.

## Verification

- `uv run pytest -q` green at every step (baseline **1740**).
- Mutation-check new guards: if breaking a rule does not fail a test, it is not covered.
- Live proof on `:9292` against a throwaway db — **never `:8787`**.
- No push/publish without an explicit directive.
