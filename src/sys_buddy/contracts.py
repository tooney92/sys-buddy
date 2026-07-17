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

from urllib.parse import urlparse

# The HTTP verbs a contract endpoint may declare (SPEC §6).
VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# Upper bound on endpoints in one contract — a sane API surface, and a guard against
# a proposal with thousands of endpoints bloating the dashboard payload / DB row.
MAX_ENDPOINTS = 100


def validate_spec(spec: dict) -> list[str]:
    """Return a list of human-fixable error strings; empty list means valid.

    We collect *all* errors in one pass rather than failing on the first, so an
    agent can correct a proposal in a single revision instead of round-tripping
    through the broker once per mistake.
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
        errors.extend(_validate_staging_url(spec["staging_url"]))

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


def _validate_staging_url(url: object) -> list[str]:
    """The staging URL is the security-load-bearing field: it must be an absolute
    https URL with a host, because the test-runner agent will hit it (SPEC §9)."""
    if not isinstance(url, str) or not url.strip():
        return ["'staging_url' must be a non-empty string"]
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return [
            f"'staging_url' must be an absolute https URL (got scheme "
            f"{parsed.scheme or 'none'!r})"
        ]
    if not parsed.netloc:
        return ["'staging_url' must include a host, e.g. https://api-staging.example.com"]
    return []


def _is_int(value: object) -> bool:
    """True for real integers only. ``bool`` is an ``int`` subclass in Python, but
    a version of ``True`` is a bug, not a version — exclude it explicitly."""
    return isinstance(value, int) and not isinstance(value, bool)
