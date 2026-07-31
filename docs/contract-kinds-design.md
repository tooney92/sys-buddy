# A contract is a list of agreed things — not necessarily an API

**Status:** IMPLEMENTED (`DECISIONS.md` D12). The kind table and validator live in
`contracts.py` (`KINDS`, `infer_kind`); the four surfaces that make it visible are the
`propose_contract` docstrings in `tools.py`, `rules.py` + `onboarding.py`, the `propose`
question and grader in `readiness.py`, and `api._units_of` → the `CONTRACT_KINDS` renderer
table in `ui.html`. Specs: `tests/test_contract_kinds.py` (the validator) and
`tests/test_contract_kinds_surfaces.py` (the surfaces). The "Open, not decided here"
section at the bottom is still open. Successor to the `interface_type` note in `v2.md`
("Non-HTTP / contract-less interfaces"), which reached the same conclusion from a BLE task.

**Owner's framing:** *"a contract is a list of things with possible attributes which we
agree on, and it doesn't matter if it is an API, UI, frontend components etc."* And the
invariant it has to preserve: *"we said we will build like this, and this is how we have
built."*

## The problem

`contracts.validate_spec` hard-requires `endpoints` (≥1, each a valid HTTP method +
`path`) and a `staging_url`. So the only shape a contract can take is an HTTP API.
(The `staging_url` half has since gone further — see **Security** below: the target is
host-owned configuration and no spec may carry one at all.)

This is NOT a permissions problem. Nothing is restricted to backend — `state.py:320` says
so explicitly ("Nothing is hardcoded to 'backend'"), and any party to a todo may propose.
It is a SCHEMA problem: a frontend+designer pair is entitled to propose and simply cannot
produce a spec that validates.

Two real cases that break today:

- a designer and a frontend agreeing six screens — zero HTTP surface between them;
- two frontend engineers, one building `<SessionProvider>` and the other consuming it —
  a producer/consumer relationship with no network hop.

Today both force a bad choice, as `v2.md` put it: skip the contract (lose the signed,
versioned, both-locked artifact) or fake an HTTP one (theater that documents nothing real).
The second is currently *possible* — `rules.py:109` tells agents extra spec keys are kept —
which makes it the likelier failure: a real agreement smuggled into a spec beside a dummy
endpoint, invisible to the dashboard.

## The model

A contract is **≥1 named unit, each with attributes**. What changes per kind is the unit;
what never changes is everything else.

| kind | unit | the broker enforces |
|---|---|---|
| `http` (default) | endpoint | ≥1, each a method + path |
| `schema` | type | ≥1, each with named fields and their shapes |
| `ui` | screen / component | ≥1, each named, with its states |
| `none` | acceptance criterion | ≥1 checkable statement |

Unchanged for every kind: per-todo versioning, both-sides `lock_contract`, `decline`,
`reopen_negotiations`, and `get_contract` as the single source of truth. `interface_type`
selects a VALIDATOR and a RENDERER. It changes nothing about how contracts work, which is
why this is a far smaller change than it sounds — the propose → sign → build → report →
verify cycle never mentions HTTP anywhere. Only the validator does.

## Keep the natural key per kind

Do NOT rewrite the wire format to a generic `units: [...]`.

- `http` keeps `endpoints`, so **every existing contract keeps its units byte-for-byte**
  and no migration of `spec_json` is needed. (An existing spec also carries a
  `staging_url`; those documents are never rewritten and still resolve — see Security.)
- `ui` uses `screens`, `schema` uses `types`, `none` uses `criteria` — an agent writes the
  word for the thing it is actually describing, which is easier to get right than an
  abstraction.

The generalisation lives in the validator and the renderer: a table of
`kind → (spec key, unit label, required attributes)`. Only that table grows when a kind is
added.

## How the agent knows which key to use

Three channels, in the order they fire:

1. **The tool docstring**, before the call. This is already the primary teaching surface —
   FastMCP exposes it as the tool description, and `propose_contract`'s docstring currently
   spells out "`spec` must contain `endpoints` … and an absolute https `staging_url`". It
   grows a list of kinds and their keys.
2. **The validator's errors**, on failure. Already written to be human-fixable and returned
   all-at-once so one revision fixes everything.
3. **The pre-flight quiz** (`readiness.py`), before the agent may act at all. Comprehension
   is already graded here; "which kind fits this work?" fits the existing machinery.

### Infer the kind from the key — do not make the agent declare it

Because each kind has a DISTINCT unit key, the key already names the kind:

```
{"endpoints": [...]}  → http        {"types":    [...]}  → schema
{"screens":   [...]}  → ui          {"criteria": [...]}  → none
```

So `interface_type` is INFERRED when omitted. Requiring the agent to declare a kind AND
pick the matching key is two chances to get it wrong instead of none; the agent simply
writes `screens:` and the broker knows what it is looking at.

`interface_type` stays ACCEPTED, for two jobs only:

- catching contradictions — declared `http` but supplied `screens` is refused, naming both,
  because one of the two is a mistake and the broker must not pick which;
- future kinds that might share a unit key.

The broker therefore enforces three things: exactly one unit key present (two → refused as
ambiguous, never silently preferred); the declared kind matching the key when both are
given; and the kind's required attributes on each unit.

## Why "named unit" and not "any JSON"

The invariant is *"this is how we have built"* — so the second half must be **checkable**.
A contract reading "make the login nice" cannot be verified, so `ok #N` would be meaningless
and propose→sign→build→verify degrades into vibes.

Naming units keeps it real: the consumer can look at the running thing and say whether
`ForgotPassword` exists with those states. This is also why **`none` still requires
acceptance criteria** rather than being free prose — a genuinely empty contract is the one
case that breaks the invariant, so the kind that sounds like "no structure" is really "the
structure is a checklist".

## Enforcement, not training

Three layers hardcode the HTTP assumption today, and each needs the same switch:

| layer | today | with kinds |
|---|---|---|
| agent | authors the spec | writes the KEY for the thing it agreed (`screens`, `types`, …) — the kind follows from it, see below |
| broker | `validate_spec` rejects a spec with no endpoints | picks the validator for the kind |
| dashboard | `ui.html:946` maps over `data.endpoints` | picks the renderer for the kind |

The broker half is the important one. It is not the agent's prompt that makes contracts
well-formed today — an agent that omits endpoints gets a hard, unarguable error. Training
alone drifts; enforcement does not. That is this project's whole posture: *the broker
enforces, agents request.*

So the CHOICE of kind must be enforced too, not merely taught. Concretely: an agent that
declares `http` and supplies zero endpoints should be refused **with the kind it probably
meant named back at it** — not told "endpoints required", which sends it off to invent a
fake endpoint rather than reconsider the kind.

## Rendering

Because every kind is "a list of named things with attributes", the dashboard needs ONE
renderer, not four. Today's endpoint table generalises to a unit table with a per-kind
heading and per-kind columns. Later, the verify step can let the consumer tick units off,
and that works identically for endpoints, screens or types.

## Security

**`staging_url` remains the ONLY fetchable URL the broker will ever hand out.** It is
**host-owned configuration**: it lives on `tasks.staging_url` (overridable per
deliverable on `todos.staging_url`), is **resolved live on every read**, and **no kind
may carry one in its spec** — a spec that does is refused. See DECISIONS.md D13.

The rule, in full, and it is **KIND-AGNOSTIC**:

- the target is **not part of the signed shape and is not granted by it**. A contract
  describes what was agreed; the target is where that agreed thing currently runs, and
  the two move independently;
- it is **resolved for ANY kind** whenever the host has set one — `http`, `schema`, `ui`
  and `none` alike;
- **no agent can ever write it**, and there is deliberately no tool to request a change;
- it stays **withheld until every party has signed**, keyed on the LOCK rather than on
  whether a target happens to be configured.

Why kind-agnostic, explicitly, because it is the thing most likely to be "fixed" back:
`has_http_surface` answers *does this contract describe HTTP?* — **not** *does the
consumer need somewhere to go and look?* Those diverge precisely for `ui`, the kind that
prompted the question: a designer↔frontend agreement about screens is verified by
**opening the deployed app**. Gating the target on `has_http_surface` would strip the URL
from the kind that most obviously needs one and make `ok #N` unanswerable. So
`has_http_surface` no longer gates anything about the target at all — it records only
which kind describes an HTTP surface, for the validator and the renderer.

> **Stale wording, kept here as a warning.** Before D13 this section read *"a kind with
> no HTTP surface has nothing to grant"*. That was true of the PRE-D13 design, where the
> target lived INSIDE the signed spec: a non-HTTP contract needn't carry a URL, and there
> was no fetchable thing for the signature to release. Once the target became host-owned
> configuration resolved live, the contract stopped "granting" it at all — so the
> sentence describes a design that no longer exists. Do not reintroduce it, and do not
> change the code to match it.

Nothing here is loosened. The https/SSRF rules are unchanged; `contracts.validate_staging_url`
is where they are enforced now, called by every surface that WRITES the value.

## Open, not decided here

- Does a `ui` contract want a design reference (Figma/file) as a first-class attribute, or
  is that just an attribute on a screen? Leaning first-class, since it is per-contract.
- Should `ui` be its own kind at all, or a `schema` with a design reference? `schema` is the
  more general kind — it covers frontend↔frontend, backend↔backend and anything with typed
  shapes. If only two kinds beyond `http` get built, build `schema` and `none`.
- Can a todo's contract change kind between versions (v1 `http`, v2 `schema`)? Probably yes
  — a renegotiation may legitimately discover the interface was never HTTP — but the
  dashboard's version diff has to cope.
