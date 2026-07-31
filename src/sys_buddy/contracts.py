"""Contract-shape validation (SPEC §6, ``docs/contract-kinds-design.md``).

Contracts are structured JSON, not freeform prose, for two reasons the spec spells
out: the broker can validate shape before it permits a lock (freeform can't be
validated), and the dashboard renders method badges / field tables straight from
this JSON.

**The deployment target is NOT part of the shape.** ``staging_url`` is host-owned
configuration living on the task (and, per deliverable, on the todo) — see
``state.resolve_staging_url``. A spec that carries one is REFUSED here, which is a
stronger posture than the one it replaces, not a weaker one: the field used to be
agent-authored and defended only by a human noticing "test against evil.com" during
review, and now there is no field for an injection to land in at all. It also stops
an ngrok restart from being a renegotiation — a locked contract used to go stale on a
tunnel bounce, and re-signing people on non-events teaches them to sign without
reading, which costs every signature that would have caught something real.

``staging_url`` remains the ONLY fetchable URL the broker will ever hand out. That
rule now lives on the host's value: see :func:`validate_staging_url`, which is what
every host-facing surface calls before storing one.

A contract is **≥1 named unit, each with attributes**. What changes per KIND is the
unit — endpoints, types, screens, criteria — and nothing else: versioning, both-sides
``lock_contract``, ``decline``, ``reopen_negotiations`` and ``get_contract`` never
mention HTTP. Only this validator (and the dashboard's renderer) is kind-aware, which
is why supporting a designer↔frontend or frontend↔frontend agreement costs a table
rather than a redesign. See :data:`KINDS`.

``validate_spec`` is deliberately a *pure* function returning a list of actionable
error strings — empty means valid. Keeping it pure (no DB, no raising) lets the
state machine decide policy (raise, reject, surface to the agent) while this module
owns only the question "is this shape correct?". Every error names the exact
location an agent must fix, e.g. ``"endpoint 0: method 'FOO' is not a valid HTTP
verb"`` — a validation error the receiving agent can act on without a human.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

# Hostnames that always point somewhere internal — the test-runner must never fetch
# them, even though they aren't literal IPs. (DNS rebinding — a public name that
# resolves to a private IP — is a residual the fetch-time layer must also guard.)
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal"}

# Hostnames that are, by definition, THIS machine. Used only to recognise a
# same-machine broker origin (see :func:`same_machine_origin`) — never to relax
# anything on its own.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}

# The HTTP verbs a contract endpoint may declare (SPEC §6).
VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# Upper bound on endpoints in one contract — a sane API surface, and a guard against
# a proposal with thousands of endpoints bloating the dashboard payload / DB row.
MAX_ENDPOINTS = 100

# The same bound, for the same reason, on every other kind's unit. They are all 100
# because the constraint is not "how many endpoints is a reasonable API" but "how big
# a JSON blob may one agent push into a DB row and the dashboard payload"; a contract
# that genuinely has >100 screens or types is a task that wanted splitting.
MAX_TYPES = 100
MAX_SCREENS = 100
MAX_CRITERIA = 100

# The kind a spec is assumed to be when it names none: HTTP, because every contract
# written before kinds existed is one and must keep validating byte-for-byte.
DEFAULT_KIND = "http"


@dataclass(frozen=True)
class _Kind:
    """One row of the kind table: what the unit is called and how it is checked.

    Adding a kind is adding a row — the propose → sign → build → report → verify
    cycle is untouched, because only the validator and the renderer are kind-aware.
    """

    name: str  # the value of 'interface_type'
    unit_key: str  # the spec key holding the units — and the thing that NAMES the kind
    unit_label: str  # singular, for error prefixes: "endpoint 0: ..."
    max_units: int
    validate_unit: Callable[[int, object], list[str]]
    # Does this kind DESCRIBE an HTTP surface? True for http alone.
    #
    # It gates the validator and the renderer, and NOTHING about the deployment target.
    # It used to decide which kinds required a `staging_url` in the spec; the target is
    # host-owned configuration now (see the module docstring), so no kind carries one and
    # every kind RESOLVES one whenever the host has set it.
    #
    # Do not repurpose this to gate the target. It answers "does this contract describe
    # HTTP?", not "does the consumer need somewhere to go and look?" — and those diverge
    # for exactly the kind that invites the mistake. A `ui` contract is verified by
    # OPENING THE DEPLOYED APP, so gating on this would strip the URL from the kind that
    # most obviously needs one and make the verify step unanswerable.
    has_http_surface: bool
    # Shown when a spec has no unit key at all, so the agent reconsiders the KIND
    # instead of inventing a fake unit of the default one.
    blurb: str


# Built at the bottom of the module, once the per-unit validators it references exist.
KINDS: dict[str, _Kind] = {}
UNIT_KEY_TO_KIND: dict[str, str] = {}


def same_machine_origin(base_url: object, public_url: object = None) -> bool:
    """POSITIVE evidence that a task never leaves this one machine.

    True only when BOTH hold: there is no public/tunnel origin at all, and the
    broker origin the agents pair against is a literal loopback address (or the
    ``localhost`` name). Anything we cannot prove is loopback — a bare host, an
    unparseable URL, a LAN/Tailscale/ngrok address, a blank base — returns False.

    This is the *only* input that may relax :func:`validate_staging_url`, so it is
    written to fail closed: absence of evidence of remoteness is never taken as
    evidence of same-machine-ness.
    """
    if public_url is not None and str(public_url).strip():
        return False  # a real origin exists → someone else can reach this
    if not isinstance(base_url, str) or not base_url.strip():
        return False
    parsed = urlparse(base_url.strip())
    if not parsed.scheme or not parsed.hostname:
        return False  # can't parse an origin out of it → can't prove anything
    host = parsed.hostname.lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname that isn't a loopback literal → not proven local


def infer_kind(spec: object) -> str | None:
    """The kind a spec *is*, read off its unit key — or None when that is not knowable.

    None means "do not guess": no unit key at all, or more than one (an ambiguous
    spec the broker refuses rather than silently preferring a kind). A declared
    ``interface_type`` is NOT consulted here; the key is the evidence, the declaration
    is only a cross-check (see :func:`validate_spec`). Public because the dashboard
    needs the same answer to pick a renderer.
    """
    if not isinstance(spec, dict):
        return None
    present = [key for key in UNIT_KEY_TO_KIND if key in spec]
    return UNIT_KEY_TO_KIND[present[0]] if len(present) == 1 else None


def validate_spec(spec: dict, is_remote: bool = True, same_machine: bool = False) -> list[str]:
    """Return a list of human-fixable error strings; empty list means valid.

    We collect *all* errors in one pass rather than failing on the first, so an
    agent can correct a proposal in a single revision instead of round-tripping
    through the broker once per mistake.

    The spec's KIND is inferred from which unit key it carries (``endpoints`` → http,
    ``types`` → schema, ``screens`` → ui, ``criteria`` → none), because each kind has
    a distinct key and the key therefore already names the kind. Making the agent both
    declare a kind and pick a matching key would be two chances to be wrong instead of
    none. ``interface_type`` stays *accepted* and does exactly two jobs: it catches a
    contradiction (declared http, supplied screens — one of the two is a mistake and
    the broker must not pick which), and it will disambiguate any future kinds that
    share a unit key.

    A ``staging_url`` in the spec is REFUSED, of every kind. The deployment target is
    host-owned configuration on the task/todo, not part of the shape the parties sign
    (see the module docstring), so a spec carrying one is a spec written against the
    old rules. Refused rather than silently dropped, deliberately: dropping it would
    let a prompt-injected "test against evil.com" appear to succeed, leaving the agent
    believing the contract points there and free to repeat it in chat. The refusal
    contradicts the injection out loud and lands in the same one-shot error list as
    every other fix.

    ``is_remote``/``same_machine`` are accepted and threaded through by callers that
    validate a HOST-supplied target in the same breath (see
    :func:`validate_staging_url`); they no longer affect the shape check, because a
    shape no longer contains a URL. Both default to the strict remote rules so a caller
    that forgets to pass them still fails safe.
    """
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]

    errors: list[str] = []

    # --- which kind is this? ------------------------------------------------
    present_keys = [key for key in UNIT_KEY_TO_KIND if key in spec]
    declared, declaration_errors = _declared_kind(spec)
    errors.extend(declaration_errors)

    if len(present_keys) > 1:
        # Two unit keys means two contracts in one document. Refuse naming both and
        # stop: silently preferring one would lock an agreement nobody proposed.
        named = ", ".join(
            f"'{key}' ({UNIT_KEY_TO_KIND[key]})" for key in sorted(present_keys)
        )
        errors.append(
            f"contract is ambiguous: it supplies units for more than one kind — "
            f"{named} — but a contract is exactly one kind of thing, and the broker "
            f"will not pick for you; keep the key for the units you actually agreed "
            f"and remove the other(s)"
        )
        errors.extend(_validate_version(spec))
        return errors

    kind_name = UNIT_KEY_TO_KIND[present_keys[0]] if present_keys else None

    if kind_name and declared and declared != kind_name:
        # A valid-but-conflicting declaration. Every further error would be an error
        # about a spec that may not be the one intended, so we say the one true thing
        # and stop — the broker must not decide which half was the mistake.
        errors.append(
            f"'interface_type' says {declared!r} but the spec supplies "
            f"'{present_keys[0]}', which is a {kind_name!r} contract; one of the two "
            f"is wrong and the broker will not guess which — fix whichever is the "
            f"mistake (omitting 'interface_type' is fine: the unit key names the kind)"
        )
        errors.extend(_validate_version(spec))
        return errors

    # A declared kind with no units still tells us what the agent MEANT, so we hold it
    # to that kind rather than falling back to the http default and demanding endpoints.
    kind = KINDS[kind_name or declared or DEFAULT_KIND]

    # --- units (required: ≥1 named unit, each with attributes) --------------
    if kind.unit_key not in spec:
        errors.append(_missing_unit_key_error(kind, declared))
    elif not isinstance(spec[kind.unit_key], list):
        errors.append(f"'{kind.unit_key}' must be a list")
    elif not spec[kind.unit_key]:
        errors.append(f"'{kind.unit_key}' must contain at least one {kind.unit_label}")
    elif len(spec[kind.unit_key]) > kind.max_units:
        errors.append(f"too many {kind.unit_key} (max {kind.max_units})")
    else:
        for i, unit in enumerate(spec[kind.unit_key]):
            errors.extend(kind.validate_unit(i, unit))

    # --- staging_url: not yours to write ------------------------------------
    # The deployment target is the ONLY URL the broker will ever hand a test-runner to
    # fetch, and it is HOST-OWNED — it lives on the task (or on the todo, per
    # deliverable) and is resolved live at read time. No agent tool writes it, which is
    # what makes "an injected target" unrepresentable rather than merely unlikely.
    #
    # So a spec carrying one is refused for EVERY kind, with the reason and the fix
    # named, and it stays in the same collected-errors pass so one revision fixes
    # everything.
    if "staging_url" in spec:
        errors.append(_staging_url_refusal(kind))

    # --- version (optional, but if present must be a plain int) -------------
    errors.extend(_validate_version(spec))

    return errors


def _declared_kind(spec: dict) -> tuple[str | None, list[str]]:
    """Read and sanity-check ``interface_type``; returns (kind or None, errors).

    Absent is the normal case and never an error — the unit key names the kind. A
    *garbage* value is a bad field rather than a genuine conflict, so we report it and
    return None, letting the unit key resolve the kind; that keeps one broken field
    from suppressing every other error in the pass.
    """
    if "interface_type" not in spec:
        return None, []
    raw = spec["interface_type"]
    declared = raw.strip().lower() if isinstance(raw, str) else None
    if declared in KINDS:
        return declared, []
    return None, [
        f"'interface_type' {raw!r} is not a kind of contract "
        f"(expected one of {sorted(KINDS)}, or omit it — the unit key names the kind)"
    ]


def _missing_unit_key_error(kind: _Kind, declared: str | None) -> str:
    """No units at all. Name the kinds back at the agent rather than saying "endpoints
    required", which sends it off to invent a fake endpoint instead of reconsidering
    whether this agreement was ever an HTTP one."""
    if declared:
        return (
            f"'interface_type' is {declared!r} but the spec has no '{kind.unit_key}' — "
            f"contracts of kind {kind.name!r} need {kind.blurb}"
        )
    alternatives = ", ".join(
        f"'{other.unit_key}' for kind {other.name!r} ({other.blurb})"
        for other in KINDS.values()
        if other.name != kind.name
    )
    return (
        f"missing required key '{kind.unit_key}' — contracts of kind {kind.name!r} need "
        f"{kind.blurb}. If this agreement has no HTTP surface it is a different KIND of "
        f"contract, not a missing endpoint: use {alternatives}. Do not invent an "
        f"endpoint to satisfy this check"
    )


def _staging_url_refusal(kind: _Kind) -> str:
    """Why a spec may not carry a target, and what to do instead.

    Names the OWNER (the host) rather than just the rule, because the agent's next move
    is a sentence to its human, not another proposal.

    The same sentence for EVERY kind, deliberately. Telling a `ui` or `schema` agent
    "there is no target for your kind" would be false — the host's target is resolved
    for any kind, and a screens contract is verified by opening the deployed app — and a
    refusal that misdescribes the rule teaches the wrong thing at the one moment the
    agent is definitely reading.
    """
    return (
        "'staging_url' is not yours to set — remove it from the spec. The deployment "
        "target is host-owned configuration, not part of the shape you sign: the host "
        "sets it on the task (or per-todo), and get_contract hands it to you once every "
        "party has signed — whatever kind of contract this is. That is also why a tunnel "
        "restart no longer stales a locked contract: the target is resolved live, so "
        "nothing has to be re-signed."
    )


def _validate_version(spec: dict) -> list[str]:
    if "version" in spec and not _is_int(spec["version"]):
        return ["'version' must be an integer"]
    return []


def _validate_endpoint(index: int, endpoint: object) -> list[str]:
    """Validate one endpoint; errors are prefixed with its index for the agent."""
    if not isinstance(endpoint, dict):
        return [f"endpoint {index}: must be an object"]

    errors: list[str] = []

    method = endpoint.get("method")
    if method not in VALID_METHODS:
        errors.append(
            f"endpoint {index}: method {method!r} is not a valid HTTP verb "
            f"(expected one of {sorted(VALID_METHODS)})"
        )

    path = endpoint.get("path")
    if not isinstance(path, str) or not path.strip():
        errors.append(f"endpoint {index}: 'path' must be a non-empty string")

    # request/response are optional lists of field descriptors; when present,
    # each field's declared type ('t') must be a string (SPEC §6).
    for section in ("request", "response"):
        if section not in endpoint:
            continue
        fields = endpoint[section]
        if not isinstance(fields, list):
            errors.append(f"endpoint {index}: '{section}' must be a list of fields")
            continue
        for j, field in enumerate(fields):
            if not isinstance(field, dict):
                errors.append(f"endpoint {index} {section} field {j}: must be an object")
                continue
            field_type = field.get("t")
            if field_type is not None and not isinstance(field_type, str):
                errors.append(
                    f"endpoint {index} {section} field {j}: type 't' must be a string"
                )

    return errors


def _validate_type(index: int, unit: object) -> list[str]:
    """One ``schema`` unit: a named type with named fields and their shapes.

    The shapes are REQUIRED here, unlike an endpoint's optional request/response
    lists: a schema contract's units are the only thing it agrees on, so a type with
    no fields agrees on nothing and "this is how we have built" stops being checkable.
    """
    if not isinstance(unit, dict):
        return [f"type {index}: must be an object"]

    errors: list[str] = []
    if not _non_empty_str(unit.get("name")):
        errors.append(f"type {index}: 'name' must be a non-empty string naming the type")

    fields = unit.get("fields")
    if fields is None:
        errors.append(
            f"type {index}: 'fields' must list the type's fields, e.g. "
            f'[{{"name": "id", "type": "string"}}]'
        )
    elif not isinstance(fields, list):
        errors.append(f"type {index}: 'fields' must be a list of fields")
    elif not fields:
        errors.append(f"type {index}: 'fields' must contain at least one field")
    else:
        for j, field in enumerate(fields):
            if not isinstance(field, dict):
                errors.append(f"type {index} field {j}: must be an object")
                continue
            # Both spellings are accepted on purpose: endpoint field descriptors have
            # always used the terse 'n'/'t' (SPEC §6), so an agent that has seen an http
            # contract writes those, while an agent working only from the docstring
            # writes 'name'/'type'. Refusing either is a round-trip that buys no
            # correctness. Renderers must read both.
            if not _non_empty_str(_alias(field, "name", "n")):
                errors.append(
                    f"type {index} field {j}: 'name' must be a non-empty string"
                )
            if not _non_empty_str(_alias(field, "type", "t")):
                errors.append(
                    f"type {index} field {j}: 'type' must be a non-empty string "
                    f"describing the field's shape, e.g. 'string' or 'User[]'"
                )

    return errors


def _validate_screen(index: int, unit: object) -> list[str]:
    """One ``ui`` unit: a named screen/component and the states it can be in.

    States are what makes a ui contract verifiable — the consumer can look at the
    running thing and say whether ``ForgotPassword`` really has an ``error`` state.
    A screen with a name and nothing else is a wish, not an agreement.
    """
    if not isinstance(unit, dict):
        return [f"screen {index}: must be an object"]

    errors: list[str] = []
    if not _non_empty_str(unit.get("name")):
        errors.append(
            f"screen {index}: 'name' must be a non-empty string naming the screen or "
            f"component, e.g. 'ForgotPassword'"
        )

    states = unit.get("states")
    if states is None:
        errors.append(
            f"screen {index}: 'states' must list the states this screen can be in, "
            f'e.g. ["loading", "empty", "error"]'
        )
    elif not isinstance(states, list):
        errors.append(f"screen {index}: 'states' must be a list")
    elif not states:
        errors.append(f"screen {index}: 'states' must contain at least one state")
    else:
        for j, state in enumerate(states):
            if not _non_empty_str(state):
                errors.append(
                    f"screen {index} state {j}: must be a non-empty string naming the "
                    f'state, e.g. "loading"'
                )

    return errors


def _validate_criterion(index: int, unit: object) -> list[str]:
    """One ``none`` unit: a checkable acceptance criterion, as a plain string.

    'none' is the kind for work with no interface to describe — but it is NOT the kind
    with no contract. The invariant is "we said we will build like this, and this is
    how we have built", so the second half has to be checkable; free prose would make
    the verify step meaningless. The kind that sounds like "no structure" is really
    "the structure is a checklist".
    """
    if not _non_empty_str(unit):
        return [
            f"criterion {index}: must be a non-empty string stating something that can "
            f'be checked, e.g. "the CSV import rejects rows with a missing email"'
        ]
    return []


def _alias(obj: dict, *keys: str) -> object:
    """First of ``keys`` actually present in ``obj`` — for accepted spellings."""
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_staging_url(
    url: object, is_remote: bool = True, same_machine: bool = False
) -> list[str]:
    """The one place the ONLY fetchable URL the broker hands out is checked.

    PUBLIC, and called by every surface that stores a HOST-supplied target — host
    setup, ``admin.set_staging_url``, the CLI, the desktop app. It used to be reached
    only through :func:`validate_spec`, back when the target rode inside an
    agent-authored spec; now the target never appears in a spec at all, so the check
    had to move to where the value is actually written. Same rules, one owner.

    The staging URL is the security-load-bearing field: the test-runner agent will
    hit it (SPEC §9). It must be an absolute https URL, and — crucially — must NOT
    point at internal infrastructure. A backend that set it to http://169.254.169.254
    (cloud metadata → IAM creds) or http://127.0.0.1/admin would turn the buddy's
    test-runner into an SSRF gadget, so private/reserved/loopback/link-local targets
    and known-internal hostnames are rejected here (OWASP SSRF Prevention).

    Strictness is keyed on CONNECTIVITY, not on the broker's auth mode — the GUI
    always runs the broker in remote mode (token auth needs it) even when both
    "agents" are one human on one laptop. Two things unlock the lenient path:

    * ``is_remote=False`` — the loopback, no-auth CLI broker (``sys-buddy local``).
    * ``same_machine=True`` — a task the HOST declared as same-machine at setup:
      loopback broker origin AND no public/tunnel URL (see :func:`same_machine_origin`).

    On either, the SSRF threat model collapses: the "peer" test-runner is this same
    box, ``http://localhost:PORT`` is the real and correct target, and there is
    nothing to deploy — so any non-empty string is accepted. Every other task keeps
    the full https + SSRF checks below, unchanged."""
    if not isinstance(url, str) or not url.strip():
        return ["'staging_url' must be a non-empty string"]
    if same_machine or not is_remote:
        return []  # one box: any non-empty URL is fine (localhost, http, a bare host…)
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return [
            f"'staging_url' must be an absolute https URL (got scheme "
            f"{parsed.scheme or 'none'!r})"
        ]
    host = parsed.hostname
    if not host:
        return ["'staging_url' must include a host, e.g. https://api-staging.example.com"]
    if host.lower() in _BLOCKED_HOSTS or host.lower().endswith((".local", ".internal")):
        return [f"'staging_url' host {host!r} is internal and not allowed"]
    try:
        # A literal IP must be globally routable — reject private/reserved/loopback/
        # link-local (incl. 169.254.169.254 metadata) ranges, IPv4 and IPv6.
        if not ipaddress.ip_address(host).is_global:
            return [f"'staging_url' host {host!r} is a private/reserved address"]
    except ValueError:
        pass  # not a literal IP — a hostname; allowed (subject to fetch-time checks)
    return []


def _is_int(value: object) -> bool:
    """True for real integers only. ``bool`` is an ``int`` subclass in Python, but
    a version of ``True`` is a bug, not a version — exclude it explicitly."""
    return isinstance(value, int) and not isinstance(value, bool)


# --- the kind table ---------------------------------------------------------
# Everything kind-specific lives here; adding a kind is adding a row. The natural key
# per kind is deliberate: http keeps 'endpoints' so every contract written before kinds
# existed stays valid byte-for-byte with no spec_json migration, and the other kinds use
# the word for the thing they actually describe, which an agent gets right more often
# than an abstract 'units'. Keep the keys DISTINCT — the key is what names the kind.
KINDS = {
    kind.name: kind
    for kind in (
        _Kind(
            name="http",
            unit_key="endpoints",
            unit_label="endpoint",
            max_units=MAX_ENDPOINTS,
            validate_unit=_validate_endpoint,
            has_http_surface=True,
            blurb="at least one endpoint: an HTTP method and a path",
        ),
        _Kind(
            name="schema",
            unit_key="types",
            unit_label="type",
            max_units=MAX_TYPES,
            validate_unit=_validate_type,
            has_http_surface=False,
            blurb="at least one type: a name, and named fields with their shapes",
        ),
        _Kind(
            name="ui",
            unit_key="screens",
            unit_label="screen",
            max_units=MAX_SCREENS,
            validate_unit=_validate_screen,
            has_http_surface=False,
            blurb="at least one screen: a name, and the states it can be in",
        ),
        _Kind(
            name="none",
            unit_key="criteria",
            unit_label="criterion",
            max_units=MAX_CRITERIA,
            validate_unit=_validate_criterion,
            has_http_surface=False,
            blurb="at least one acceptance criterion: a checkable statement",
        ),
    )
}

UNIT_KEY_TO_KIND = {kind.unit_key: kind.name for kind in KINDS.values()}
