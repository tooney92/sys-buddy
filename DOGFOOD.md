# Dogfooding sys-buddy

Two developers' AI agents ship a feature together, under a broker-enforced contract.
The fastest path is the **desktop app**; a manual CLI path is below it.

## Requirements
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). Clone + `uv sync`.
- Each side needs **Claude Code** (`claude`) on their PATH.
- Two machines over the internet also need an https tunnel (e.g. [ngrok](https://ngrok.com)).

---

## A. Desktop app (recommended)

```bash
git clone <repo> && cd sys-buddy && uv sync
uv run sys-buddy gui
```

A native window opens. Then:

### Host
1. **I'm the Host** → name the task (e.g. `signin`), pick roles (backend is always on), **Create & start broker**.
   - The broker runs *inside the app* on `127.0.0.1:8787` — keep the window open.
   - For a buddy on **another machine**: first run `ngrok http 8787`, paste the `https://…ngrok.app` URL into **Public URL**.
2. Copy each role's **invite link** and send it to the right buddy (Slack/Signal).
3. **Open dashboard** → a live window (stepper · thread · contract · event log) that fills in as the agents work.

### Buddy
1. **I'm the Buddy** → paste your **invite link**, name your agent, **Join**.
   - It pairs you and auto-runs `claude mcp add`. (If `claude` isn't found — common when the app is launched from Finder — it shows the exact command to run yourself.)
2. Copy the **role prompt** it shows, start `claude`, and paste it. Click **✓ I've briefed my agent**.

The two agents negotiate a contract, sign it, deploy, test, and verify — the broker enforces every step. Watch it on the dashboard.

---

## B. Manual CLI (no app)

**Host** (one terminal per line; leave `serve` running):
```bash
uv run sys-buddy init
uv run sys-buddy task create signin --roles backend,frontend --title "Sign-in & session API"
uv run sys-buddy invite --task signin --role backend      # copy the code
uv run sys-buddy invite --task signin --role frontend     # copy the code
uv run sys-buddy host-viewer                              # dashboard viewer token
uv run sys-buddy serve --host 127.0.0.1 --port 8787       # add --public-url https://… for a remote buddy
```

**Each buddy** (own directory):
```bash
uv run sys-buddy join http://127.0.0.1:8787 <CODE> --name alice-backend
# run the printed `claude mcp add …` line, then `claude`, then paste the printed role prompt.
```

**Watch:** open `http://127.0.0.1:8787/ui?v=<VIEWER_TOKEN>` (or the printed dashboard link).

**Reset:** stop `serve`, `rm ~/.sys-buddy/sys_buddy.db`, start again.

---

## Security modes (going over the internet)
- **Default — ngrok (buddy installs nothing):** the host exposes the broker via an https tunnel. Protections with **zero buddy install**: every MCP request (even the `initialize` handshake) requires a valid token — nothing, not even the tool list, is reachable unauthenticated; agent tokens **auto-expire in 24h** in tunnel mode (agents refresh via the `rotate_token` tool); `/pair` is rate-limited + invite-gated; auth failures are audit-logged.
- **Max — Tailscale/WireGuard private overlay:** both sides run Tailscale (free, ~2 min). The broker is reachable **only** to authorized devices, encrypted peer-to-peer, **no public surface**. Tick **"Private network"** in the host screen (or `serve --trusted-network`) — it allows an `http://100.x` / `*.ts.net` origin since the overlay already encrypts. This is the strongest posture.

## Notes
- **Windows buddy:** needs only Claude Code + the invite link/token — Python optional (the host can run `join` and send the `claude mcp add` line). The broker also runs on Windows (the `0600` db-permission lockdown is a no-op there).
- **The broker enforces**, so a misbehaving agent can't skip a step, fake a status, verify without tests, or be talked into fetching a rogue URL (it follows the Rules of Engagement and only fetches the signed `staging_url`).
