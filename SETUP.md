# Setting up sys-buddy

sys-buddy is an authenticated, contract-enforcing broker that lets two developers' AI
coding agents collaborate over the internet. This guide gets you from zero to a running
broker your agents can talk to.

There are three ways to run it — pick one:

| You want to… | Use |
|---|---|
| Just run it (recommended) | **PyPI** — `uv tool install sys-buddy` |
| Deploy it on a server / keep it running | **Docker** — `docker pull ghcr.io/tooney92/sys-buddy` |
| Modify the source | **From source** — `git clone` + `uv sync` |

---

## Option 1 — Install from PyPI (recommended)

You get a single, pinned, released version of the `sys-buddy` command, isolated from any
source checkout.

```bash
uv tool install sys-buddy          # installs the `sys-buddy` command on your PATH
#   or:  pipx install sys-buddy
#   or:  pip install sys-buddy     (into a virtualenv)
```

Verify:

```bash
sys-buddy --version                # → sys-buddy 1.3.0
```

**Upgrading** — releases are versioned, so upgrading is explicit and reversible:

```bash
uv tool upgrade sys-buddy          # move to the latest release
uv tool install sys-buddy==1.3.0   # or pin / roll back to an exact version
```

---

## Option 2 — Run with Docker (from GitHub Container Registry)

A prebuilt image, good for a server or for keeping the broker running across reboots.

```bash
docker pull ghcr.io/tooney92/sys-buddy:latest        # or a pinned tag, e.g. :1.3.0

docker run -d --name sys-buddy \
  -p 127.0.0.1:8787:8787 \
  -v sysbuddy:/data \
  ghcr.io/tooney92/sys-buddy:latest
```

- The image **defaults to `serve`** (authentication enforced) — never `local`, which is
  unauthenticated and must not be reachable off-box.
- `-v sysbuddy:/data` persists the SQLite database (and its WAL files) across restarts.
- Binding `127.0.0.1:8787:8787` keeps it on loopback; put an https tunnel in front to
  expose it (see *Remote* below).

---

## Option 3 — Run from source (for contributors)

Only if you're modifying sys-buddy itself.

```bash
git clone https://github.com/tooney92/sys-buddy && cd sys-buddy
uv sync                            # create the venv
uv run sys-buddy --version         # runs your local checkout
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor workflow.

---

## Running the broker

Two modes, same tools and dashboard:

```bash
# LOCAL — loopback, NO auth. One developer, many repos on one machine.
sys-buddy local                    # → http://127.0.0.1:8787   (never expose this off-box)

# SERVE — authentication enforced. Two people, two machines. The real thing.
sys-buddy serve

# Desktop app (host/join wizard + dashboard), if you installed the GUI extra:
sys-buddy gui
```

> **local vs serve — the one rule that matters.** `local` has no authentication and
> auto-provisions identities; it is safe *only* because it stays on loopback. `serve`
> enforces bearer tokens and is the only mode you should ever make reachable from another
> machine (including inside Docker).

---

## Wiring an agent to the broker

1. **Host a task** and mint an invite for each role:
   ```bash
   sys-buddy task create signin --roles backend,frontend
   sys-buddy invite --task signin --role frontend      # prints a /join link + code
   ```
2. **The buddy redeems it** for a scoped token:
   ```bash
   sys-buddy join <broker-url> <code> --name dave-frontend
   # → prints the exact `claude mcp add … --header "Authorization: Bearer sbk_…"` line
   ```
3. Run that `claude mcp add` line in the buddy's repo — their agent is now on the task.

The invite doubles as a browser link (`/join#c=…`), so a buddy can onboard without the CLI.

---

## Remote (two humans, two machines)

`serve` binds locally; a tunnel makes it reachable, and you **must** tell the broker its
public origin or the invite links it prints will point at `127.0.0.1`:

```bash
ngrok http 8787                                        # or Tailscale / real infra
export SYS_BUDDY_PUBLIC_URL=https://abc123.ngrok.app   # serve + invites read this
sys-buddy serve
```

The origin is baked into the invite token, so setting `SYS_BUDDY_PUBLIC_URL` is required,
not cosmetic — without it the buddy's link is unusable.

---

## The dashboard

Read-only, live-updating. Mint a viewer token and open it:

```bash
sys-buddy host-viewer                                  # prints a /ui?v=… link
```

The viewer token is read-scoped — a leaked link can only *watch* one task, never act.

---

## Verify it's up

```bash
curl -s http://127.0.0.1:8787/api/version              # → {"version":"1.3.0"}
```

`/api/version` reports the version the **running** broker actually booted with — handy for
confirming an upgrade took effect (restart the broker after upgrading, or it keeps serving
the old code).
