# How "can sys-buddy make us money?" became the Guest seat — shipped as v2.10.0

**Shipped:** `v2.10.0` — Concierge mode / the **Guest seat** (PyPI + ghcr + release note + website) · **1671 tests** on main
**Merged:** PR #87 (feature) · #88 (release) · #89 (release note)
**Reviewed:** `/code-review` found 4, all fixed before the commit even landed
**Killed:** the owner's `:8787` broker, on request — to be restarted on 2.10.0

---

## Where it started — a money question, not a feature

The session opened as a strategy conversation: *how does sys-buddy make us money?* It did **not**
end where the standard options pointed (hosted SaaS, agency tier, enterprise). It ended somewhere
the owner surfaced from lived experience: **lean AI startups whose management can't get empirical
insight into progress** — young teams ship things that break, and when management asks, they get
"tech-bro" terms that don't translate.

The striking part was how well that mapped onto what already exists. Engagement mode's *two
registers of one record*, the verified/evidence-reviewed/not-checked strength labels, coverage
counts, milestones — the product was already shaped for "translated, verification-backed progress a
non-technical stakeholder can trust." The moat, in one line: **every PM tool reports what people
say they did; sys-buddy could report what's machine-verified to actually work.**

## The architecture question that decided the rest

The owner asked the right hard question: *does the broker now need to be AI-powered? Can I have an
agent in the cloud — my Claude is on my machine?*

The answer that held: **no, and it must not be.** The founding principle — the broker enforces,
agents request; a strike counter can't be talked out of a database column — dies the moment an LLM
sits inside the broker. So: **broker stays a dumb, deterministic store + counter; all AI lives at
the edges as participants.** And "an agent in the cloud" is just the same Claude engine (the Agent
SDK, headless) running on a box as *another participant with its own bearer token* — the broker
can't tell it from a laptop and needs zero change. Two things were being conflated and separating
them dissolved the worry: a **cloud-hosted broker** (no AI) vs a **cloud agent** (AI, but an edge
seat, not the broker).

Downstream we sketched but did **not** build: a Jira-but-simpler sprint/goal/task model, team-level
velocity (deliberately *not* a per-person leaderboard — that Goodharts the verified signal it's
selling), and a desktop app. The desktop thread ran Electron → Tauri (sidecar-a-Python-binary,
OS-webview, ~10MB vs ~150MB) → **"not worth ~$200/yr in signing certs for now"**, deferred. The
web/hosted route serves the non-technical buyer better anyway.

## What we actually built — the Guest seat

The concrete wedge the owner wanted first (motivated by building a site for his non-technical
fiancée): a **Guest** — a person who joins with **no AI of their own**, through a browser message
box. Named by the owner: **Concierge** = the experience, **Guest** = the role. (He also caught that
"sherpa" names the *guide*, i.e. the host's agent, not the guided.)

The one architectural decision — how a Guest *writes* without cracking the read-only dashboard
(D11) — resolved cleanly and against the existing grain:

- A Guest is a **real `agents` seat** (`role="guest"`, auto-`ready`, **no agent token at all** — she
  never calls `/mcp`).
- Her **viewer token is linked to that seat** via a new nullable `viewers.agent_id`, **NULL on every
  existing viewer**, which is exactly what keeps them read-only.
- A narrow **`POST /guest/message`** (its own `guest.py` module, *not* a route under `/api/*`)
  refuses any viewer whose `agent_id` is NULL. Identity is **stamped server-side**; type is
  **hard-coded to a new `note`** conversational type, so a Guest can't forge a `verified`.
- Provisioning is one host command, `sys-buddy task add-guest <task> --name`, printing a `/ui?v=`
  link. The Guest installs nothing.
- Like the owner, the guest role is **exempt from guidelines and pre-flight** — nothing gradeable.

## The bug the tests could not see (a recurring genre here)

The Playwright run refused to send. Console: `state is not defined`, `guestSend is not defined`.
The dashboard script is wrapped in an **IIFE** — so `state` and the functions are closures, and my
inline `onclick="guestSend()"` / `oninput` ran in global scope and saw nothing. Every other
interactive element wires through `bind()` after render via `data-*` attributes. Rewired the
composer the same way. **pytest was green the whole time** — it asserts Python, not what a browser
makes of an HTML string. Only the live run caught it, again.

Then the owner's UX call: the message box does **not** belong inside the message thread (the thread
is for *reading*). Moved it to its **own card, above the contract**, shown only to a guest viewer.

## Proven live on :9292

Seeded a real task (`ada-site`, frontend+backend seats) + a Guest via the real flow, on a throwaway
db, broker on `:9292` (never the owner's `:8787`). The Guest opened her link, the box appeared above
the contract, she sent *"can we make the header green?"* — it landed in the thread as **Ada ·
@guest**. The **host viewer saw the message but no box.** HTTP: host→**403**, anon→**403**,
guest→**201**, `GET /api`→**200**. Light + dark. (Mobile is gated to desktop — the owner's answer:
tell guests to use a laptop.)

## The review that earned its keep

`/code-review low` found four, all real:

1. **`add-guest` printed the wrong origin** — no `--port`/`--public-url`, so the guest link fell back
   to `:8787` while the broker was elsewhere. This is the *exact* class `_cfg_from_args` already
   documents ("confidently tell you to open :8787 while your broker is on :9292"). Fixed to match
   `invite`/`host-viewer`.
2. **`add_guest` check-then-insert race with `close_task`** — added the `WHERE EXISTS (task open)`
   guard `redeem_invite` uses.
3. **`_guest_identity` skipped the task-match** when `viewer.task_id` was NULL — a host-scoped
   credential could have written as a seat. Tightened + a regression test.
4. Dead `seats.is_guest` — removed.

All fixed before commit; `1671 passing` (a new guard test added).

## The release — and the account that couldn't push

The whole ship-release playbook ran. One snag: the active gh account (`anthugny`) has **no write
access** to `tooney92/sys-buddy` — push 403'd. With the owner's explicit go, switched to
**`tooney92`** for the release (pushing per-command through gh's credential helper, no persistent
git-config change) and **switched back to `anthugny`** when done.

The known release traps behaved: the release PR needed the **uv.lock relock** (2.9.0→2.10.0), and
that push from a real account **fired the checks by itself** — so *no* close/reopen dance this time
(the playbook's step-2 trap only returns if step-3 ever disappears). Three publish jobs green; PyPI
confirmed **2.10.0**; local install upgraded `2.9.0 → 2.10.0`. Release note is a `docs:` PR (#89,
main is branch-protected). Website `releases.html` got its new `<li>` at the top; the pulsing
"latest" dot moved to 2.10.0 on its own (verified in Playwright, and that 2.9.0 went static).

## The broker, killed on request

The owner then said *"kill my broker."* `:8787` (pid 1102, a 2.9.0 install started by hand) was
serving old code from memory. Killed on the explicit instruction (the standing rule is never to
kill a broker you did not start), **not restarted from the session** — starting the production
broker here would parent it to a shell that dies. To be brought back on 2.10.0 by the owner.

## Known and carried forward

- **Host-vouches, not independent representation.** This Guest talks to the *host's* agent — right
  for a trusted collaborator, wrong for an arm's-length paying client (that case wants the client's
  own agent). Recorded as a boundary, not a gap.
- **The dashboard is desktop-only**, and a Guest inherits that gate. Reaching a non-technical guest
  on *mobile* is the obvious next friction, deferred deliberately.
- **The bigger vision is parked, not lost:** verified-progress-for-management as the monetization
  wedge, the Jira-simpler model, cloud reporter/QA agents via the Agent SDK. The Guest seat is the
  first brick — it's what makes the non-technical side of that product *reachable at all*.
