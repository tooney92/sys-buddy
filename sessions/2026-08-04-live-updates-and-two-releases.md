# Live updates through a tunnel — and two releases out the door

**Shipped:** `v2.3.0` and `v2.4.0`, both on PyPI + ghcr · **1428 tests** (from 1390)
**Website:** live — setup tunnel caveat + releases page caught up
**Trigger:** an investor demo, and a dashboard that had silently stopped updating

---

## The headline

The dashboard stopped showing new messages when watched through a Cloudflare quick tunnel.
Locally the same version was fine, which sent us looking at the wrong thing for a while.

**It is the transport, and it cannot be fixed at our layer.** Measured against a real
`cloudflared` quick tunnel: subscribed for 90 seconds, then 45 with a message posted at
+6s — **zero bytes both times.** Not slow, nothing. The response head only appeared when
the client tore the connection down. `X-Accel-Buffering: no` is present on the loopback
response and **stripped** by Cloudflare.

Probing with a padded stream put the threshold at roughly an edge buffer: 2, 8, 32, 48, 64
and 96 KB produced nothing in 25s; **128 KB flushed in 0.56s.** Our real frames are a few
dozen bytes every 10 seconds, so they would take days to accumulate. The padding trick is
written up as a `DO NOT` in `_sse_ping` rather than shipped.

**ngrok, Tailscale and named Cloudflare tunnels all carry it fine.** The owner had been on
ngrok in the v1 era — his own contract history shows the ngrok URL on an early locked
contract and `trycloudflare.com` on later ones. So "it was fine in v1" was true and had
nothing to do with v1. **The transport changed, not the code.**

## What was actually broken on our side

The stream dying was the trigger. The defect was that the page could not tell:

* nothing was emitted until the first real change, first keepalive 15s out — so through a
  buffering proxy the connection might never visibly establish;
* the keepalive was a bare `: ping` **comment**, and SSE comments are never surfaced to
  JavaScript — the liveness signal was on the wire and unobservable;
* `onerror` unhandled, relying on the browser's own reconnect, which never fires for a
  connection that stays open and silent;
* **no fallback poll** — the stream was not an optimisation, it was the only refresh path;
* the "paused" banner only appeared on a *deliberate* pause, so a dead stream rendered as
  healthy while the header counted "refreshed 3s ago".

Fixed: opening frame on connect (loopback time-to-first-byte **15.1s → 0.02s**), an
observable `event: ping` every 10s, a watchdog that reopens a stream silent for 45s, and a
15s poll — **opt-in**.

### The poll is opt-in, and that was the owner's correction

The first pass polled unconditionally. That is ~480 requests/hour through **the viewer's**
tunnel, and a free ngrok allowance is tens of thousands of requests — we were spending
somebody's quota because we had decided it was worth it. It also polled while the stream
was working perfectly.

Now: a switch in the header that **only exists while degraded**, plus a one-time modal
naming the likely cause (free Cloudflare quick tunnels) and the transports that work.
Default is **not** polling. The answer is remembered per broker origin. A backdrop click
dismisses without recording — a stray click must not decide how someone's quota is spent.

`onopen` is deliberately **not** liveness: through a quick tunnel it fires in 214ms and
then nothing arrives for 30s with `readyState` still OPEN.

## Everything else in v2.3.0

* **A dead viewer cookie locked the dashboard with no way out.** `sb_view` outranked an
  explicit `?v=`, lived 7 days, and was never cleared — so a fresh link could not override
  it while the error screen told you to get one. Unrecoverable in the desktop dashboard
  window (no address bar, no devtools, no private mode). Also: `secure` was keyed on the
  configured public URL rather than the request scheme, so any host with an https tunnel
  marked every cookie `Secure` — including the one set on the `127.0.0.1` link
  `host-viewer` prints, which browsers then refuse to send.
* **An expired agent token was a dead end that lied.** `rotate_token` authenticates with
  the token it would replace, so an expired agent cannot recover *or* escalate. The error
  said "invalid or revoked" for all three causes, sending people hunting a revocation
  nobody performed. Added `sys-buddy task extend-tokens <task>` (`--hours`, `--never`) and
  made the error name which of the three it was.
* **Engagement mode had no door.** Shipped in 2.1.0 — owner seat, deliverables, guidelines,
  verification — and `admin.create_task` validated against `("contract","debug")`, so it
  raised on the mode the release was named for. The CLI and the app's radios agreed with
  it. One `admin.MODES` now, read by all three, with tests pinning both surfaces.
* **A todo opens over the board** instead of replacing the list — modal, status band across
  the top, prose left, contract right. The dead in-column renderer was removed.
* **The version check is unconditional.** It sat behind a footer checkbox defaulting to
  off, so it told nobody anything — four releases shipped while the owner ran 1.4.0 and hit
  two bugs live that were already fixed upstream.

## v2.4.0

The banner told you a release existed and never how to install it. It now shows the right
command with a copy button — `uv tool upgrade` / `pipx upgrade` / `pip install -U` /
`docker pull` — detected from where the running interpreter lives. The container check runs
**first**, because the Docker image installs into a venv and a venv-shaped test would tell
a container user to run pip.

"Restart needed" now says to quit the app and reopen it, plus a sentence for the case where
the app attached to a broker somebody started in a terminal — then quitting the app does
**not** restart it. That is the owner's own workflow.

## Things that were wrong, and how they were found

* **"Emit a frame immediately is the highest-value line"** — wrong. Real (15.1s → 0.02s)
  but it does not save a tunnelled demo; the fallback poll does.
* **"The refreshed-Ns-ago label lies"** — wrong. `state.lastRefresh` only updates on
  successful fetches. It was true and useless. The *pulsing green dot* was the liar.
* **A `NameError` nearly shipped.** `explain_agent_token` was used in a request handler and
  not imported — `import middleware` and all 1398 tests still passed. Now pinned by a test
  asserting the name is in the module namespace.
* **`git add -A` conflated two features** into one commit under a message describing only
  one. Split into two green commits.
* **Recovery had to become a refetch**, not just a cleared flag: after the stream came back
  the switch correctly vanished while the header still read "connection issue". Found by
  watching it, not reasoning.
* **A superseded *draft* was labelled "Signed against"** — a draft was never signed. My own
  bug, caught driving it.

## Process, now written down

`CLAUDE.md` gained the release sequence, because two things kept costing time:

1. **The release PR always arrives `BLOCKED` with no checks.** GitHub will not trigger
   workflows for anything its own bot token did, so the required checks never fire. Close
   and reopen. Has bitten v2.0.0, v2.0.1, v2.2.0, v2.3.0 and v2.4.0. The permanent fix is
   the `RELEASE_PLEASE_TOKEN` secret described in the workflow header.
2. **Updating the website's releases page is part of a release.** It had drifted four
   releases behind, and the app's update banner links straight to it — so a stale page is
   what a user sees at the moment they are told to upgrade. The owner's shorthand is
   **"uswr"**.

## Verified against artifacts, not the working tree

Both releases were installed from PyPI into clean venvs and checked there: 2.3.0 emits the
opening frame immediately, `engagement` is selectable, `extend-tokens` exists; 2.4.0 carries
the `upgrade_command` field. Detection was also checked against the **real installed tool's
interpreter** (`~/.local/share/uv/tools/sys-buddy/bin/python`), which reports `uv` — the
tests all monkeypatch the paths, so they would pass even if real detection were wrong.

Note: `uv` served a **stale index cache** immediately after publishing and claimed 2.4.0 did
not exist. `--refresh` fixes it. Worth knowing before concluding a publish failed.

## Open

* **The owner's dry run** — two machines, a real tunnel. The only untested link. Over
  **ngrok**, not a quick tunnel, or the demo is poll-paced with a visible degraded switch.
* **His machine is still on 2.2.0** — both the installed tool and the `:8787` broker.
  `uv tool upgrade sys-buddy`, then quit and reopen.
* **Rhema needs 2.4.0 too** — either of them may host, and the dashboard is served by
  whoever does.
* **Two stale brokers**: `:9292` and `:9696` (`kill 64147 17986`).
* **grey-boulders tokens expire 22:22 today**; `extend-tokens` now exists for it.
* **Supervisor mode** — `ideas/supervisor-mode.md`, four questions before any code. Most of
  it already exists as engagement mode; the genuinely new part is roles on deliverables,
  which reverses a decision written into `db.py`.
