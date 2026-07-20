# Role/prompt redesign — decision checklist

Living record of what the owner has agreed to for the role + prompt redesign.
`[x]` = agreed. `[ ]` = still open / not built. Build only after all relevant boxes are agreed.

## Decisions (agreed)
- [x] **Kill the hardcoded login-API demo** in `role_prompt` — no prescribed task, ever.
- [x] **Host picks their own role** and gets their own agent seat (Step 1 `claude mcp add` + Step 2 prompt), same as the buddy. Invite links minted only for the *other* role(s). `host_role=None` keeps CLI invite-only behavior.
- [x] **B — generalize the status vocabulary** (not just prompt wording). Additive:
      - add `ready` as the canonical producer status; keep `deployed` as a silent alias (nothing breaks).
      - soften client-side `test_passed`/`test_failed` → generic *checked* / *blocked* (aliases kept).
      - terminal stays `verified`; `stuck` stays. Contract negotiate→lock half stays (universal).
- [x] **Prompt = "how to use sys-buddy," not "what to build."** The human, in their own session, tells the agent what to build and when to lock the contract. Once locked, both agents know to start building.
- [x] **Both host AND buddy get a copy-able prompt.** Both should copy it into their Claude session.

## Decisions (agreed) — prompt semantics
- [x] **Role-aware, task-agnostic**: name the role + its protocol lane, never the work.
- [x] **Producer proposes** the contract. **The human** tells their agent when to sign.
- [x] **Signing = calling `lock_contract`** (it IS the signature). Contract locks automatically
      once EVERY declared role has signed. No unilateral lock. Locked = immutable = build signal.
- [x] Prompt ends with "don't build yet — pass pre-flight, read rules(), wait for your human."

## Decisions (agreed) — lock detection
- [x] **v1 = poll `get_contract`.** Last signer sees `locked: true` synchronously; first signer
      polls `get_contract()` until `status == "locked"`. Prompt documents this. No broker push.
- [x] Push-on-lock (broker notifies the first signer) → deferred to `v2.md`.

## Decisions (agreed) — task identity
- [x] **Auto-generate `task_id`; require a Title instead.** Human types only a Title.
      id = slug(title) + short random suffix (titles may collide). id stays internal
      (URLs/invites/identity); humans only ever see the title. Flip the Start-a-task form.

## Decisions (agreed) — dashboard add-task
- [x] **Option A** — dashboard "+ New task" deep-links to the GUI Start-a-task screen.
      Dashboard stays 100% read-only; no new write endpoint. (B logged to v2.md if ever wanted.)

## Decisions (agreed) — prompt wording
- [x] Producer + consumer prompt variants APPROVED verbatim (incl. `get_contract` lock-poll line).
      SUPERSEDED by model B: contract prompt is now ONE unified prompt for every role.

## DONE — Model B: producer = whoever proposes the contract (no hardcoded backend)
- [x] `state.py`: `_producer_role()` = role that proposed the locked contract; `ready` gate =
      "you are the producer", checks gate = "you are NOT the producer". BACKEND_ROLE removed.
- [x] `admin.py`: dropped "must include backend"; contract now needs ≥2 roles instead.
- [x] `onboarding.py`: ONE unified contract prompt (producer is dynamic, unknown at onboarding).
- [x] `readiness.py`: status question/grader generalized (no backend-deploys assumption).
- [x] `gui_app.html`: no forced/disabled backend; "a contract needs at least two" note.
- [x] GUI: MERGED "Roles" + "Which role are you?" into one section — "Which one are you?" lists
      only the roles actually in the cast (hidden, not dimmed). Tracks cast dynamically.
- [x] Tests updated; NEW `test_non_backend_producer_full_flow` proves frontend-proposes E2E. 237 green.
- [x] `v2.md`: (obsolete — producer-freedom now shipped; entry can be pruned).

## Decisions (agreed) — host form copy
- [x] **Explain the "Private network" toggle in plain language.** It has NO hint today
      (`gui_app.html:484`), just jargon. Essence: default forces HTTPS for remote buddies;
      toggle = "we're both on a private VPN (Tailscale/WireGuard), allow plain http, no tunnel."
      Add a hint line + soften label to "My buddy and I share a private network (…)". Copy only,
      no logic change. Lands in the host-screen rework (step 4).

## DECIDED (Tailscale) — fix in THIS build via `tailscale serve`
- [x] **`tailscale serve 8787`** proxies tailnet HTTPS → loopback broker. NO bind change; broker
      stays on 127.0.0.1. Reuses the ngrok path (https URL, trusted=False, broker on loopback).
- [x] GUI: reframe connectivity as a 3-way mode selector (Same machine / Public tunnel (ngrok) /
      Private network (Tailscale)), each with tailored instructions:
        ngrok      → run `ngrok http 8787`, paste https ngrok URL
        tailscale  → run `tailscale serve 8787`, paste https `host.tailnet.ts.net` URL
- [x] Drop the "allows http" boolean from the GUI (serve gives https, so normal https check passes).
      Keep CLI `--trusted-network` for power users doing raw http/0.0.0.0. Optional: auto-detect the
      ts.net hostname via `tailscale status --json` (best-effort, manual paste fallback).

## (superseded) earlier BUG note — kept for context
- The GUI hardcodes `BROKER_HOST = "127.0.0.1"` (`gui.py:43`, `_run_broker`). The "Private network"
  toggle only (a) allows an http URL in the invite link and (b) skips the https check — it NEVER
  rebinds the broker to a reachable interface. So GUI + Tailscale = buddy's link points at
  `http://100.x.y.z:8787` but the broker listens on loopback only → connection refused.
- ngrok path works ONLY because ngrok runs on the host and forwards to 127.0.0.1:8787; Tailscale
  has no local forwarder, so it needs the broker actually bound to the tailnet interface.
- TWO possible fixes:
  (a) bind broker to `0.0.0.0`, buddy hits `http://100.x.y.z:8787` (plain http). Exposes on the
      physical LAN too, not just tailnet. Needs bind change + `tailscale ip -4` detect.
  (b) **`tailscale serve 8787`** (RECOMMENDED): Tailscale reverse-proxies loopback→tailnet with
      auto HTTPS, tailnet-only. Broker STAYS on loopback (like ngrok). GUI "private network" path
      becomes: instruct host to run `tailscale serve 8787`, paste the `https://host.tailnet.ts.net`
      URL; can even keep the https requirement. Cleaner + more secure than (a).
- [ ] AWAITING OWNER: (a) 0.0.0.0 bind, or (b) `tailscale serve`? And fold into GUI host work?

## Open
- (none else — all decisions made; build in order below)

## Build order (once wording is locked)
1. `state.py` + `tools.py`: add `ready`/`checked`/`blocked` canonical statuses w/ old aliases; update docstrings. Tests.
2. `onboarding.py`: rewrite `role_prompt` generic; add host agent seat to `host_setup(host_role=)`.
3. `gui.py` `start_host(host_role=)`; `gui_app.html` host role selector + host-result screen (Step 1/Step 2).
4. Dashboard stepper labels if needed. pytest + Playwright (pwr) green.
