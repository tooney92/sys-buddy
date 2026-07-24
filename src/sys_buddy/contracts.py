"""Contract-shape validation (SPEC §6).

Contracts are structured JSON, not freeform prose, for three reasons the spec
spells out: the ``staging_url`` lives in a signed document (so an injected "test
against evil.com" has nowhere to land), the broker can validate shape before it
permits a lock (freeform can't be validated), and the dashboard renders method
badges / field tables straight from this JSON.

``validate_spec`` is deliberately a *pure* function returning a list of actionable
error strings — empty means valid. Keeping it pure (no DB, no raising) lets the
state machine decide policy (raise, reject, surface to the agent) while this module
owns only the question "is this shape correct?". Every error names the exact
location an agent must fix, e.g. ``"endpoint 0: method 'FOO' is not a valid HTTP
verb"`` — a validation error the receiving agent can act on without a human.
"""

from __future__ import annotations

import ipaddress
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


def same_machine_origin(base_url: object, public_url: object = None) -> bool:
    """POSITIVE evidence that a task never leaves this one machine.

    True only when BOTH hold: there is no public/tunnel origin at all, and the
    broker origin the agents pair against is a literal loopback address (or the
    ``localhost`` name). Anything we cannot prove is loopback — a bare host, an
    unparseable URL, a LAN/Tailscale/ngrok address, a blank base — returns False.

    This is the *only* input that may relax :func:`_validate_staging_url`, so it is
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


def validate_spec(spec: dict, is_remote: bool = True, same_machine: bool = False) -> list[str]:
    """Return a list of human-fixable error strings; empty list means valid.

    We collect *all* errors in one pass rather than failing on the first, so an
    agent can correct a proposal in a single revision instead of round-tripping
    through the broker once per mistake.

    ``is_remote``/``same_machine`` control how strict the ``staging_url`` check is —
    see :func:`_validate_staging_url`. Both default to the strict remote rules so a
    caller that forgets to pass them fails safe.
    """
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]

    errors: list[str] = []

    # --- endpoints (required, list of endpoint objects) ---------------------
    if "endpoints" not in spec:
        errors.append("missing required key 'endpoints'")
    elif not isinstance(spec["endpoints"], list):
        errors.append("'endpoints' must be a list")
    elif not spec["endpoints"]:
        errors.append("'endpoints' must contain at least one endpoint")
    elif len(spec["endpoints"]) > MAX_ENDPOINTS:
        errors.append(f"too many endpoints (max {MAX_ENDPOINTS})")
    else:
        for i, endpoint in enumerate(spec["endpoints"]):
            errors.extend(_validate_endpoint(i, endpoint))

    # --- staging_url (required, absolute https URL) -------------------------
    if "staging_url" not in spec:
        errors.append("missing required key 'staging_url'")
    else:
        errors.extend(_validate_staging_url(spec["staging_url"], is_remote, same_machine))

    # --- version (optional, but if present must be a plain int) -------------
    if "version" in spec and not _is_int(spec["version"]):
        errors.append("'version' must be an integer")

    return errors


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


def _validate_staging_url(
    url: object, is_remote: bool = True, same_machine: bool = False
) -> list[str]:
    """The staging URL is the security-load-bearing field: the test-runner agent will
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
