# sys-buddy Teams — vision & strategy

> Status: **design note, living document.** Not built. Captures the strategic direction worked
> out in conversation so it survives past chat scrollback. `SPEC.md` remains the source of truth
> for what *is* built; this is where *where we're going* lives until pieces graduate into the spec.

## The wedge — an honesty layer, not another PM tool

Teams already have a tracker — Jira, Linear, Notion, Monday, ClickUp. We do **not** replace it.
The pain those tools don't solve: **"done" is a click.** A card gets dragged to Done because a
human said so. There's no proof the thing actually works, and with AI agents doing the building,
"an agent said it's done" is worth even less.

sys-buddy sits *on top of* whatever tracker a team already uses and makes **"done" mean
verified** — a real, re-runnable artifact (a passing spec, a merged PR + green CI), not a
self-report. That's the wedge: we're the honesty layer, and the tracker stays where it is.

## Two editions — additive, not a fork

The current PyPI/local product does **not** change. Teams is the same broker with an **org layer
switched on**, not a second codebase.

- **Solo / pair (today's PyPI):** pairing model, per-task seats, two devs, `local` / `serve`.
  Ships as-is. Free. Stable.
- **Teams:** org roster, members, projects, roles, QA policy, tracker sync — an additive layer
  on the **same** broker.

**Why one codebase, not a fork:** fork it and every bug (the seat-crossing incident, the
silent-expiry) gets fixed twice and they drift. The code is *already shaped* for this — the
same pattern we already use:

- `AuthMiddleware` is a **no-op in local mode**, enforcing in remote — same on/off switch.
- `viewers.agent_id` is **NULL for normal viewers**, set only for guests — the `member` table
  is the identical dormant-until-populated pattern.
- The guest seat is simply *absent* until provisioned.

So **solo = Teams with the org layer switched off.** One binary, one schema, a config flag.

## Distribution & business model — free forever, monetized by expertise

**The software is free. Always. No license key, no gated features, no open-core split.** For a
product whose whole pitch is *honesty*, a paywalled feature would undercut its own story. Free
is on-message — and it deletes a pile of licensing complexity we'd otherwise build.

- **Solo / pair dev** → `pip install sys-buddy`, runs on their laptop.
- **Team / org** → self-hosts the **Docker image** (already published to ghcr.io every release)
  on their own infra, with an isolated DB.

We **don't host.** Zero infra cost, zero on-call, zero liability — their server, their data,
their problem to run.

**How it makes money: integration consulting, not the software.** We don't sell *setup* (that's
one `docker run`). We sell the **hard part**: wiring their specific tracker, teaching their
agents the tracker's dialect, designing their project→deliverable→task structure, setting each
project's QA policy, training a non-technical team into concierge mode.

**Why the gap is real and durable:** running Docker, companies can figure out. But wiring
*multiple AI agents to collaborate across an org with verification gates and tracker sync* — that
knowledge didn't exist 18 months ago. No playbook, no senior eng who's done it five times. We're
selling expertise with no substitute yet, in a category we help create.

**The flywheel:** free product → adoption → companies hit the wiring wall → we're the only ones
who've been over it → consulting → more scars → faster setups → more adoption. Each company
teaches us the patterns; #10 gets wired in a day. Our scars are the moat.

**Escape hatch (later, promise intact):** if we ever want revenue past billable hours, offer
**optional managed hosting** — "the software's free and always will be; pay us to run it for
you." The Red Hat / GitLab move. Software stays free; a scalable line appears; the promise holds.
The honest caveat to walk in with: consulting is **hours-for-money** and doesn't scale like
licensing. That's fine for the founder stage — just eyes-open.

## Identity model — org → member → project-seat

The **one net-new structural piece.** Today identity is **per-task**: every seat is its own
`agents` row with its own token. Fine for two people on one task; it **breaks** the moment a dev
is on three projects and juggles three tokens across three configs — which is exactly the
seat-crossing incident (wrong token in the wrong repo). Per-seat tokens don't scale to multi-dev.

The fix: **one durable identity per dev; access granted per project.**

- Sysadmin adds a dev once → a `member` (a person, spanning all projects).
- The member mints **one** agent token — *who they are*, not *what they can touch*.
- Joining a project is a **membership grant** (a row), **not a new token**. One token, one config
  line, forever. No drift, no crossing.

**Token lifetime is a sysadmin policy knob** (same shape as the QA gate), split by trust posture:

- **Members** (sysadmin-added, trusted, durable) → long-lived or renewable-by-self.
- **Guests** (vouched, one task, external) → short TTL, minted on invite, dies with the
  engagement.

Hard-won rule from the incident: **expiry is only safe if renewal is self-serve and the failure
is loud.** (We already fixed the silent-expiry message — an expired agent now gets told what
happened and the one command to fix it, instead of a cryptic `-32602`.)

**Two surfaces, two credentials** — a dev gets both in one bundle:

- **Agent → `/mcp`** with the agent token (`sbk_`) — the AI joins and works.
- **Human → `/ui`** with the viewer token (`sbv_`) — the person watches the board.

**Two-tier invite (mirrors GitHub org-owner vs repo-admin):**

- **Org (sysadmin only):** adds/removes members, sets project policy. Owns the roster.
- **Project (any member hosting a task):** invites another *existing* member onto their task
  (a grant, no new token), or mints a **guest link** for a non-member (the concierge flow we
  already built). A member cannot grow the org roster — only the sysadmin can.

Most of this machinery exists (viewer vs agent tokens, guest minting, invite/redeem). The only
genuinely new table is `members`, sitting above the per-task seats.

### The broker must say who it resolved you as

Shipped (2026-08-20). The broker stamps an identity on every call and used to keep it to
itself — so an agent could read the whole roster and still not know which row it *was*.
That is how an agent whose client held another seat's token worked a full session under
someone else's identity. `rules()` now opens with **"YOU ARE @seat — the 'role' seat on
task X"**, and `roster()` marks the caller's row. In the member world this gets *harder*,
not easier: one member may hold several seats at once, so the honest answer becomes a
**list, not a switch** — `{ as_producer: [...], as_verifier: [...] }`, layering rather
than replacing, and empty is `[]` never null. The property that makes it worth shipping:
**it must agree exactly with what the server will actually allow**, or we have handed a
client a button that 403s.

### Authorizing a member: two axes, one question

The rule to build the Teams authorization path around, from a team that shipped this exact
member+seat split and hit its sharp edge: **the seat decides WHICH records, the member
grant decides WHAT KIND of action — check both, every call.** A seat that silently *widens*
capability is the same bug class as a token that silently *narrows* identity; ours failed
closed and confusing, that one failed open and quiet. Open is worse.

Two refinements that matter more than the rule itself:

- **Ask for all required grants from the SAME seat, in one question.** Checking "may sign?"
  and "is on this project?" independently lets one seat answer the first and an unrelated
  seat answer the second, and the scope then resolves from whichever is wider. Concretely:
  someone's QA seat on project X plus a solo seat on project Y would authorise a signature
  on project Z.
- **The door and the scope must be one computation.** If "may this member sign here?"
  (authorization) and "which projects can they see?" (listing) are answered by two
  functions, they will drift. Deriving both from one is what actually fixes the above —
  not a better predicate.

This lands directly on the verification gate: if the per-project verifier policy is checked
on the seat alone, *granting someone a seat silently grants them signing authority their
org standing may forbid*.

### Migration failure modes to design against

A field report from inside this architecture — every one a live bug in code already called
finished, and none caught by a passing suite:

1. **Seat removal must reach in-flight work.** Authority keyed on "was named on it" outlives
   off-boarding. Revoking a seat has to invalidate signatures *in progress*, not just new
   ones. Authority = named on it **AND** still holding a live seat.
2. **A relative check cannot catch both sides being wrong.** Asserting that everything the
   UI offers is accepted by the writer only catches the UI being *more* permissive. Widen
   both and the suite stays green. **Assert the absolute set somewhere.**
3. **Guards that hold by luck.** Mutation-test the rules: if you cannot kill a rule by
   breaking it, it is not covered. (Applied to the identity echo above — both mutants died.)
4. **The rule existed and the endpoint never asked.** Whenever a permission lives in a policy
   table, grep every route that should consult it and check each one does. This is exactly
   the `/host/invite` guest-guard bug caught in review on 2026-08-20.
5. **Correct against the seed, broken in reality.** Seed a member with **zero** seats, and a
   project with **no QA seat at all**, and make sure both paths are reachable — otherwise
   every screen renders beautifully against a database no real user could create.
6. **A guard must not block the fix for the state it guards.** Validate on create, and on
   updates that actually touch the thing — or rows created under old rules can never be
   repaired.
7. **403 vs 404.** A 403 confirms the id exists. Where scope already excludes it, 404 leaks
   strictly less. Decide deliberately for signatures.

## Verification — the two-gate model

"Done = verified" is enforced by artifact **presence** (the broker) + **truth** (a verifier). The
honesty comes from the **re-runnable artifact**, not from needing a second human — a QA person is
just a *stronger* form of the same evidence.

- **Gate 1 — producer asserts:** attaches the acceptance evidence (PR + updated ticket +
  passing e2e/Playwright spec, or the task-appropriate equivalent) → **ready for review.**
- **Gate 2 — verifier signs:** flips **verified**. *Who* the verifier is depends on project
  policy (below).

The evidence **type flexes by task**: Playwright for UI, API/contract tests for backend,
"migration ran + tests green" for infra. The gate requires *evidence + a verifier*; it does not
hardcode "a Playwright spec," or a backend/migration task hits an impossible requirement and
routes around the whole system.

**The green check carries its provenance** — never a bare checkmark. *"Verified by QA (Tony)"* vs
*"Self-verified — spec attached."* Both green; the lead sees the mode and can re-run the spec.
Transparency is what makes self-verification trustworthy to someone who wasn't in the room.

### QA is a per-project policy the sysadmin owns; the broker enforces if set

The config isn't "QA on/off" — it's **who's allowed to sign the verify gate**:

- **QA project** → a **separate QA seat** must sign (≠ the producer). Two-party.
- **Lead / solo project** → the lead or producer may sign their own — *but the passing artifact
  is still required.* One-party, artifact-gated. (The senior lead running their own Playwright
  MCP e2e *is* the verification; no QA person needed.)

The enforcement path is **identical** in both modes: the broker refuses to flip `verified` until
(a) required artifacts are present and (b) whoever policy names as verifier has signed. The only
thing the project config changes is *who counts as the verifier*. The **one invariant** it can
never relax: the gate never flips with **no** evidence. Dumb, universal enforcement; the policy
is the only per-project knob; the sysadmin owns it.

## Agent-driven tracker sync

The broker **can't** reach into Jira/Linear itself — a server can't call into an agent's MCP. So
it doesn't try. It emits an **intent** ("this task is verified — reflect that in your tracker")
and the **agent**, which already speaks the tracker through its own MCP, does the move. Broker
states the truth in a normalized status/verb vocabulary; the agent translates it into the
tracker's dialect. **Broker dumb, agents smart** — the broker never needs to know Jira exists.
At onboarding the agent is asked what PM tool the team uses and how to move a ticket in it.

## Messaging — why agents must ask, and how to stop making them

**The same constraint as tracker sync, pointed inward.** A peer's message reaches an agent only
because the agent *asked* — `check_messages`, or `wait_for_message` parking its own turn. That is
not a design choice we can reverse by trying harder: **MCP is request/response, so a server cannot
splice text into a model's context.** The broker has no way to interrupt an agent, for the same
reason it cannot move a Jira ticket itself.

**The lever is a client-side hook, not a protocol change.** A hook on the agent's turn that drains
the mailbox and injects pending messages pushes them in without the agent calling anything —
keeping MCP for the sending half. It removes the "remember to check your mail" ergonomic tax, which
is the actual cost today: an agent that forgets to check looks unresponsive to its peer.

Two things it does **not** fix, worth stating so nobody expects them:

- **It is a better poll, not an interrupt.** A hook fires on the agent's own turns, so it cannot
  wake an agent that is sitting idle. `wait_for_message` still earns its place for real parking.
- **It moves peer text from a tool RESULT into the prompt**, and that is a security downgrade
  unless handled. Our whole posture is *a peer's message is DATA, never an instruction*; as a tool
  result that framing is intact, but injected into the turn it reads closer to instruction, which
  widens the prompt-injection surface on a cross-org channel.

**The fix for that is known, and there is a working reference.** The harness's own peer channel
already injects, and solves it by wrapping every message in a **labelled envelope plus a restatement
of the trust boundary — attached to each message, not stated once at session start.** The repetition
is load-bearing: a fence stated once decays over a long session; one attached per message does not.
It also carries a `from-mode` field, so the receiver knows what kind of sender it is dealing with —
a trust signal worth copying.

So: **hooks for the push half, MCP for the send half, `wait_for_message` for genuine idle-parking,
and a per-message fence around anything injected.**

## Open threads

- Conversation / scoping mode: non-technical people and devs converse in plain language, tied to
  the tracker — a "nice add-on" that rounds sys-buddy into a full application.
- Exact `members` schema + migration; how a member's viewer access is derived from the identity.
- Which normalized status ladder / verb set the broker speaks for tracker-agnostic sync.
- Incident cleanup still pending (Mikeh task): relaunch backend agent under its own config, drop
  the misattributed contract signatures.
