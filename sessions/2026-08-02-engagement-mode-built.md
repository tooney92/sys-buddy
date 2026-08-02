# Engagement mode — built, tested, proven live

**Branch:** `feat/engagement-mode` · **1340 tests** (from 995) · **nothing pushed**
**Design:** `docs/enhancements.md` · **Screenshots:** `demo/v2.1.0/`

---

## What this is

An owner commissions work and cannot check it. Devs say "we built the landing page with
4 buttons" and he has no way to know. That is the ordinary failure of freelance work —
not sophisticated fraud, just a person unable to evaluate a claim in a domain he does not
speak.

sys-buddy already made two devs accountable **to each other**. This extends it to the
person paying, without making him learn the vocabulary.

> We cannot eliminate 100% of issues with humans. What we offer is that the owner gets
> **due representation in a domain he is usually alien to.**

---

## Proven live

The session in `demo/v2.1.0/` is the cast you asked for, driven entirely through real
ops — no fixtures:

| seat | who | |
|---|---|---|
| `@frontend` | Tony | **the host**, and a builder |
| `@backend-1` | Dele | builder |
| `@backend-2` | Kola | builder |
| `@owner` | Ada | commissioned the work |

**Host ≠ owner** — a dev created the session and invited everyone, including the client.

### The arc that ran

1. **Ada set three deliverables** in her own words. Nothing could be built yet.
2. **Dele pushed back on #3** — *"not checkable as written"*. Ada reworded it; the list
   went to **v2** and every earlier acceptance was cleared. That is the scope argument
   happening *before* anyone burned a day, which is the entire point of the gate.
3. All three builders accepted, **the list locked**, and only then could todos exist.
4. Four todos: two shared, one **internal** (repo + CI — links to no deliverable and
   never appears in Ada's register), and one **solo** (Tony's mobile layout).
5. Devs left their claims and finished. **Todos reached `verified` — the builders saying
   they were done.**
6. **Ada's agent ran the check** against the staging site and found:

```
#1 Landing page, 3 buttons     ✓ working
#2 Contact form → email        ✗ BROKEN
#3 Fits a phone                ✓ working
```

**#2 is the whole thesis.** The form says *"Thanks — we will be in touch"* and sends
nothing — I filled it in through Playwright and **zero network requests left the page**.
A human ticks that off as done. Only going and looking catches it.

7. The task did **not** confirm. A client does not accept four fifths of what he asked
   for.
8. The form was fixed, the run repeated, and the task moved to **`confirmed`** — and
   stayed `confirmed` through a rollup, which is the regression that would otherwise have
   destroyed the feature silently.

---

## What you caught while watching

Four real defects, all found by looking at the running dashboard rather than at tests.

**1. The owner verified work the devs never finished.** The todo strip read *"0 of 3
verified"* while the register already claimed *"Verified — this ran"*. There are two
levels of done and I had collapsed them:

| | who says it |
|---|---|
| todo `verified` | the **builders**, peer to peer — "we're done" |
| task `confirmed` | the **owner's agent** — "this is what I asked for" |

A run is now refused while any linked todo is unfinished, naming which one and its state.

**2. A deliverable nobody had picked up got verified anyway.** #3 passed because the
landing-page work happened to be responsive. No todo named it, so no contract covered it,
so nothing was ever agreed about *how* — and nobody had committed to keeping it working.
The tick was a promise no one made. An unclaimed deliverable now blocks the run.

**3. A landing page does not need two devs.** You spotted that the ≥2-seat rule made no
sense here. It is right on a peer task — the second seat *is* the accountability — but an
engagement has an outer ring: the deliverable agreed with the client, and a run that
checks it. Solo todos are now allowed **on engagements only**.

**4. `idle` claimed we track something we don't.** Removed. The column keeps the unjoined
states, which are the whole reason the roster lists empty seats.

And one you predicted before I hit it: a solo todo could never reach `verified`, because
*"the producer doesn't report checks on its own work"* — deadlocking the run forever. The
producer may now check a solo engagement todo, and nothing else.

---

## The architecture, briefly

**8 tables, 3 new modules, 13 tools.** Every engagement rule sits behind a mode check; a
`contract` or `debug` task serialises **byte-identically** to before, and a parametrised
test asserts it.

Three decisions carry the design:

- **Deliverables carry no roles.** The client says "three pages"; decomposing that into
  frontend and backend work is the team's job — which is what todos already are. His
  language is outcomes, theirs is work, todos are the translation.
- **"Playwright" means the Playwright MCP.** An agent that browses and judges. That
  removed an entire execution layer and the DSL I had started designing to feed it.
- **The broker looks things up; agents do the judging.** The version stamp on a spec is a
  database lookup, not an opinion. Everything the broker "decides" reduces to a query.

**The one line that makes it work:** `confirmed` had to join `stuck`/`resolved` in
`todos.apply_rollup`'s do-not-overwrite tuple. The rollup fires on every proposal, lock,
reopen and report, and can only derive one of six march states — so the client's
acceptance would have been stomped back to `verified` the moment anyone touched anything.

---

## Where it stands

**Committed, not pushed** — 7 commits on `feat/engagement-mode`.

Migration verified against **two copies of your real database**, migrated side by side
(one on `main`, one on the branch) so the diff is this change alone: 8 tables added, 3
columns added, **zero rows created or destroyed**, no FK violations, repeat boots
identical.

### Open, deliberately

- **`_host_row` identifies the host positionally** — the first agent row on the task.
  Correct today, but fragile. One function to change if a real marker is added.
- **The event-kind vocabulary drifted** across the three new modules (`deliverable`,
  `verification`, `task`). Worth one pass to align.
- **Per-dev verdicts don't reach the dashboard.** The API collapses per-claim results into
  one deliverable-level verdict, so `James ✓ · John ✗ found 2` cannot render yet. The UI
  reads `spec.result` defensively and draws nothing when absent — an unmarked claim never
  looks like a passed one. Adding `result` to each spec dict lights it up.
- **`docs/enhancements.md` still says "planned, not built"** for items 1–4. Needs updating
  to reflect what shipped, plus the four rules discovered live that were not in the plan.
