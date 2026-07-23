"""Specs for contract-shape validation (SPEC §6)."""

from __future__ import annotations

import copy

import pytest

from sys_buddy import contracts


def _valid_spec() -> dict:
    return {
        "version": 1,
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/auth/login",
                "request": [
                    {"n": "email", "t": "string", "req": True},
                    {"n": "password", "t": "string", "req": True},
                ],
                "response": [{"n": "token", "t": "string"}],
            }
        ],
        "staging_url": "https://api-staging.example.com",
    }


def test_valid_spec_has_no_errors():
    assert contracts.validate_spec(_valid_spec()) == []


def test_missing_endpoints_is_reported():
    spec = _valid_spec()
    del spec["endpoints"]
    errors = contracts.validate_spec(spec)
    assert any("endpoints" in e for e in errors)


def test_empty_endpoints_is_rejected():
    spec = _valid_spec()
    spec["endpoints"] = []
    assert contracts.validate_spec(spec)


def test_missing_staging_url_is_reported():
    spec = _valid_spec()
    del spec["staging_url"]
    assert any("staging_url" in e for e in contracts.validate_spec(spec))


def test_invalid_http_verb_names_the_endpoint():
    spec = _valid_spec()
    spec["endpoints"][0]["method"] = "FOO"
    errors = contracts.validate_spec(spec)
    assert any("endpoint 0" in e and "FOO" in e for e in errors)


def test_empty_path_is_rejected():
    spec = _valid_spec()
    spec["endpoints"][0]["path"] = ""
    assert any("path" in e for e in contracts.validate_spec(spec))


def test_non_https_staging_url_is_rejected():
    spec = _valid_spec()
    spec["staging_url"] = "http://api-staging.example.com"
    errors = contracts.validate_spec(spec)
    assert any("https" in e for e in errors)


def test_staging_url_without_host_is_rejected():
    spec = _valid_spec()
    spec["staging_url"] = "https://"
    assert contracts.validate_spec(spec)


def test_ftp_scheme_staging_url_is_rejected():
    spec = _valid_spec()
    spec["staging_url"] = "ftp://api-staging.example.com"
    assert contracts.validate_spec(spec)


def test_non_string_field_type_is_rejected():
    spec = _valid_spec()
    spec["endpoints"][0]["request"][0]["t"] = 123
    errors = contracts.validate_spec(spec)
    assert any("field 0" in e and "string" in e for e in errors)


def test_non_int_version_is_rejected():
    spec = _valid_spec()
    spec["version"] = "1"
    assert any("version" in e for e in contracts.validate_spec(spec))


def test_boolean_version_is_rejected():
    """bool is an int subclass in Python, but True is not a version number."""
    spec = _valid_spec()
    spec["version"] = True
    assert any("version" in e for e in contracts.validate_spec(spec))


def test_version_is_optional():
    spec = _valid_spec()
    del spec["version"]
    assert contracts.validate_spec(spec) == []


def test_non_dict_spec_is_rejected():
    assert contracts.validate_spec("not a dict")  # type: ignore[arg-type]


def test_all_errors_collected_in_one_pass():
    """An agent should be able to fix everything in a single revision."""
    spec = copy.deepcopy(_valid_spec())
    spec["endpoints"][0]["method"] = "NOPE"
    spec["endpoints"][0]["path"] = ""
    spec["staging_url"] = "http://insecure"
    errors = contracts.validate_spec(spec)
    assert len(errors) >= 3


# --- connectivity-keyed staging_url strictness ------------------------------
# The GUI always runs the broker in remote AUTH mode (token auth needs it), so the
# staging_url rule keys on the TASK's connectivity instead. Both branches below.
@pytest.mark.parametrize("url", [
    "http://localhost:3000",
    "http://127.0.0.1:8000/api",
    "http://[::1]:8080",
    "localhost:3000",              # bare host — fine on one box
    "https://api-staging.example.com",
])
def test_same_machine_task_accepts_any_target(url):
    """One human, one box: localhost/http IS the real target and there is no
    cross-machine test-runner to point at internal infrastructure."""
    spec = _valid_spec()
    spec["staging_url"] = url
    assert contracts.validate_spec(spec, is_remote=True, same_machine=True) == []


@pytest.mark.parametrize("url", ["", "   ", None, 42])
def test_same_machine_still_requires_a_non_empty_string(url):
    """Leniency is about the URL's *shape*, not about skipping the field."""
    spec = _valid_spec()
    spec["staging_url"] = url
    assert any("staging_url" in e for e in contracts.validate_spec(
        spec, is_remote=True, same_machine=True
    ))


@pytest.mark.parametrize("url", [
    "http://localhost:3000",
    "http://api-staging.example.com",      # cleartext
    "https://127.0.0.1/admin",
    "https://169.254.169.254/latest/meta-data/",
    "https://10.0.0.5/api",
    "https://foo.internal/api",
])
def test_remote_task_rejects_the_same_targets(url):
    """REGRESSION: with same_machine=False nothing is relaxed — the strict https +
    SSRF rules apply exactly as before."""
    spec = _valid_spec()
    spec["staging_url"] = url
    assert any("staging_url" in e for e in contracts.validate_spec(
        spec, is_remote=True, same_machine=False
    ))


def test_staging_url_strictness_defaults_to_strict():
    """A caller that passes neither flag gets the remote rules (fail safe)."""
    spec = _valid_spec()
    spec["staging_url"] = "http://localhost:3000"
    assert contracts.validate_spec(spec)


# --- same_machine_origin: positive evidence only ----------------------------
@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:8787",
    "http://localhost:8787",
    "https://127.0.0.1:8787",
    "http://[::1]:8787",
    "http://127.9.9.9:8787",     # all of 127/8 is loopback
])
def test_same_machine_origin_true_for_loopback_without_public_url(base_url):
    assert contracts.same_machine_origin(base_url) is True
    assert contracts.same_machine_origin(base_url, "") is True
    assert contracts.same_machine_origin(base_url, "   ") is True


@pytest.mark.parametrize("base_url,public_url", [
    ("http://127.0.0.1:8787", "https://abc.ngrok-free.app"),   # a tunnel exists
    ("http://127.0.0.1:8787", "https://host.tailnet.ts.net"),  # tailscale
    ("https://abc.ngrok-free.app", None),                      # public origin
    ("http://192.168.1.20:8787", None),                        # LAN, not this box
    ("http://10.0.0.5:8787", None),
    ("http://buddy.local:8787", None),
    ("127.0.0.1:8787", None),        # no scheme → unparseable origin, can't prove it
    ("", None),
    (None, None),
    (12345, None),
    ("http://", None),
])
def test_same_machine_origin_false_without_positive_evidence(base_url, public_url):
    assert contracts.same_machine_origin(base_url, public_url) is False
