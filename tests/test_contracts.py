"""Specs for contract-shape validation (SPEC §6)."""

from __future__ import annotations

import copy

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
