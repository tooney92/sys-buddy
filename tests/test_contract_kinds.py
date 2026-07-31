"""Specs for contract KINDS — a contract is ≥1 named unit, not necessarily an API.

Design: ``docs/contract-kinds-design.md``. The kind is inferred from the unit key
(``endpoints`` → http, ``types`` → schema, ``screens`` → ui, ``criteria`` → none);
``interface_type`` is accepted only to catch a contradiction.

The most important tests in this file are the backward-compatibility ones: every
contract that validated before kinds existed must still validate byte-for-byte, with
no ``interface_type`` field and no ``spec_json`` migration.
"""

from __future__ import annotations

import pytest

from sys_buddy import contracts


# --- backward compatibility: http is the default and is UNCHANGED ------------
def _legacy_http_spec() -> dict:
    """A contract exactly as agents have written them since before kinds existed —
    no 'interface_type' anywhere. The owner's live DB holds several of these."""
    return {
        "version": 2,
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/auth/login",
                "request": [{"n": "email", "t": "string", "req": True}],
                "response": [{"n": "token", "t": "string"}],
            },
            {"method": "GET", "path": "/api/me"},
        ],
    }


def test_legacy_http_spec_still_validates_with_no_interface_type():
    assert contracts.validate_spec(_legacy_http_spec()) == []


def test_legacy_http_spec_is_inferred_as_http():
    assert contracts.infer_kind(_legacy_http_spec()) == "http"


def test_http_no_longer_takes_a_staging_url_from_the_agent():
    """It never took one from a HUMAN, and now it takes one from nobody: the target is
    host-owned configuration, so a spec carrying it is refused whatever the kind."""
    spec = dict(_legacy_http_spec(), staging_url="https://api-staging.example.com")
    assert any("not yours to set" in e for e in contracts.validate_spec(spec))


def test_the_ssrf_rules_are_unchanged_where_the_value_now_lives():
    """Same rules, new door — `validate_staging_url` is what every host surface calls."""
    assert any(
        "staging_url" in e
        for e in contracts.validate_staging_url("https://169.254.169.254/latest/meta-data/")
    )


def test_http_endpoint_rules_are_unchanged():
    spec = _legacy_http_spec()
    spec["endpoints"][0]["method"] = "FOO"
    assert any("endpoint 0" in e and "FOO" in e for e in contracts.validate_spec(spec))


def test_declaring_http_explicitly_changes_nothing():
    spec = _legacy_http_spec()
    spec["interface_type"] = "http"
    assert contracts.validate_spec(spec) == []


def test_too_many_endpoints_message_is_unchanged():
    spec = _legacy_http_spec()
    spec["endpoints"] = [{"method": "GET", "path": f"/{i}"} for i in range(101)]
    assert "too many endpoints (max 100)" in contracts.validate_spec(spec)


# --- schema: named types with named fields and their shapes ------------------
def _schema_spec() -> dict:
    return {
        "types": [
            {
                "name": "Session",
                "fields": [
                    {"name": "id", "type": "string"},
                    {"name": "user", "type": "User"},
                ],
            }
        ]
    }


def test_schema_contract_is_valid_without_endpoints_or_staging_url():
    """Two frontends agreeing <SessionProvider> have no HTTP surface at all."""
    assert contracts.validate_spec(_schema_spec()) == []
    assert contracts.infer_kind(_schema_spec()) == "schema"


def test_schema_accepts_the_terse_endpoint_field_spelling():
    """'n'/'t' is the house wire format for endpoint fields; an agent that has seen an
    http contract will reuse it, and refusing that buys no correctness."""
    spec = {"types": [{"name": "Session", "fields": [{"n": "id", "t": "string"}]}]}
    assert contracts.validate_spec(spec) == []


def test_schema_type_must_be_named():
    spec = {"types": [{"fields": [{"name": "id", "type": "string"}]}]}
    assert any("type 0" in e and "name" in e for e in contracts.validate_spec(spec))


def test_schema_type_must_have_fields():
    spec = {"types": [{"name": "Session"}]}
    assert any("type 0" in e and "fields" in e for e in contracts.validate_spec(spec))


def test_schema_type_with_empty_field_list_is_rejected():
    spec = {"types": [{"name": "Session", "fields": []}]}
    assert any("at least one field" in e for e in contracts.validate_spec(spec))


def test_schema_field_needs_a_shape():
    spec = {"types": [{"name": "Session", "fields": [{"name": "id"}]}]}
    errors = contracts.validate_spec(spec)
    assert any("type 0 field 0" in e and "'type'" in e for e in errors)


def test_schema_field_needs_a_name():
    spec = {"types": [{"name": "Session", "fields": [{"type": "string"}]}]}
    errors = contracts.validate_spec(spec)
    assert any("type 0 field 0" in e and "'name'" in e for e in errors)


def test_empty_types_list_is_rejected():
    assert any("at least one type" in e for e in contracts.validate_spec({"types": []}))


def test_too_many_types_is_rejected():
    spec = {"types": [{"name": f"T{i}", "fields": [{"name": "a", "type": "b"}]}
                      for i in range(contracts.MAX_TYPES + 1)]}
    assert any("too many types" in e for e in contracts.validate_spec(spec))


# --- ui: named screens with their states -------------------------------------
def _ui_spec() -> dict:
    return {
        "screens": [
            {"name": "ForgotPassword", "states": ["idle", "sending", "sent", "error"]},
            {"name": "ResetPassword", "states": ["idle", "invalid-token"]},
        ]
    }


def test_ui_contract_is_valid_without_endpoints_or_staging_url():
    """A designer and a frontend agreeing six screens — zero HTTP surface."""
    assert contracts.validate_spec(_ui_spec()) == []
    assert contracts.infer_kind(_ui_spec()) == "ui"


def test_ui_screen_must_be_named():
    spec = {"screens": [{"states": ["idle"]}]}
    assert any("screen 0" in e and "name" in e for e in contracts.validate_spec(spec))


def test_ui_screen_must_declare_states():
    spec = {"screens": [{"name": "ForgotPassword"}]}
    assert any("screen 0" in e and "states" in e for e in contracts.validate_spec(spec))


def test_ui_screen_with_no_states_is_rejected():
    spec = {"screens": [{"name": "ForgotPassword", "states": []}]}
    assert any("at least one state" in e for e in contracts.validate_spec(spec))


def test_ui_state_must_be_a_non_empty_string():
    spec = {"screens": [{"name": "ForgotPassword", "states": ["idle", ""]}]}
    assert any("screen 0 state 1" in e for e in contracts.validate_spec(spec))


def test_empty_screens_list_is_rejected():
    errors = contracts.validate_spec({"screens": []})
    assert any("at least one screen" in e for e in errors)


def test_too_many_screens_is_rejected():
    spec = {"screens": [{"name": f"S{i}", "states": ["idle"]}
                        for i in range(contracts.MAX_SCREENS + 1)]}
    assert any("too many screens" in e for e in contracts.validate_spec(spec))


# --- none: still a checklist, never free prose -------------------------------
def _none_spec() -> dict:
    return {
        "criteria": [
            "the nightly job writes a summary row for every active task",
            "a failed run retries twice, then alerts",
        ]
    }


def test_none_contract_is_valid_with_only_criteria():
    assert contracts.validate_spec(_none_spec()) == []
    assert contracts.infer_kind(_none_spec()) == "none"


def test_none_still_requires_criteria():
    """'none' is the kind with no interface, NOT the kind with no contract: the
    invariant 'this is how we have built' has to stay checkable."""
    errors = contracts.validate_spec({"interface_type": "none"})
    assert any("criteria" in e for e in errors)


def test_empty_criteria_list_is_rejected():
    errors = contracts.validate_spec({"criteria": []})
    assert any("at least one criterion" in e for e in errors)


def test_blank_criterion_is_rejected():
    errors = contracts.validate_spec({"criteria": ["a real one", "   "]})
    assert any("criterion 1" in e for e in errors)


def test_non_string_criterion_is_rejected():
    errors = contracts.validate_spec({"criteria": [{"text": "do the thing"}]})
    assert any("criterion 0" in e for e in errors)


def test_too_many_criteria_is_rejected():
    spec = {"criteria": [f"criterion {i}" for i in range(contracts.MAX_CRITERIA + 1)]}
    assert any("too many criteria" in e for e in contracts.validate_spec(spec))


# --- inferring the kind, and the two jobs of interface_type ------------------
@pytest.mark.parametrize("key,kind", [
    ("endpoints", "http"),
    ("types", "schema"),
    ("screens", "ui"),
    ("criteria", "none"),
])
def test_the_unit_key_names_the_kind(key, kind):
    """The agent writes 'screens:' and the broker knows what it is looking at —
    no declaration required."""
    assert contracts.infer_kind({key: []}) == kind


@pytest.mark.parametrize("spec", [
    {"types": [{"name": "S", "fields": [{"name": "a", "type": "b"}]}],
     "interface_type": "schema"},
    {"screens": [{"name": "S", "states": ["idle"]}], "interface_type": "ui"},
    {"criteria": ["it works"], "interface_type": "none"},
])
def test_a_matching_declaration_is_accepted(spec):
    assert contracts.validate_spec(spec) == []


def test_declaration_is_case_insensitive():
    spec = {"screens": [{"name": "S", "states": ["idle"]}], "interface_type": " UI "}
    assert contracts.validate_spec(spec) == []


def test_contradiction_names_both_and_refuses():
    """Declared http but supplied screens: one of the two is a mistake and the broker
    must not pick which."""
    spec = {"interface_type": "http", "screens": [{"name": "S", "states": ["idle"]}]}
    errors = contracts.validate_spec(spec)
    assert errors
    joined = " ".join(errors)
    assert "http" in joined and "screens" in joined and "ui" in joined
    # It must NOT quietly fall through to the http rules and demand endpoints/URLs.
    assert not any("staging_url" in e for e in errors)


def test_two_unit_keys_are_ambiguous_not_silently_preferred():
    spec = {
        "endpoints": [{"method": "GET", "path": "/x"}],
        "screens": [{"name": "S", "states": ["idle"]}],
    }
    errors = contracts.validate_spec(spec)
    assert errors, "two kinds in one document must be refused, not resolved"
    joined = " ".join(errors)
    assert "endpoints" in joined and "screens" in joined
    assert contracts.infer_kind(spec) is None


def test_unknown_interface_type_is_reported_but_the_key_still_resolves():
    """A garbage declaration is a bad field, not a genuine conflict — reporting it must
    not suppress validation of the units that ARE present."""
    spec = {"interface_type": "grpc", "screens": [{"name": "S", "states": []}]}
    errors = contracts.validate_spec(spec)
    assert any("interface_type" in e and "grpc" in e for e in errors)
    assert any("screen 0" in e for e in errors)


def test_non_string_interface_type_is_reported():
    spec = {"interface_type": 7, "criteria": ["it works"]}
    assert any("interface_type" in e for e in contracts.validate_spec(spec))


def test_a_declared_kind_with_no_units_is_held_to_that_kind():
    """Declaring 'ui' and supplying nothing must not fall back to demanding endpoints."""
    errors = contracts.validate_spec({"interface_type": "ui"})
    assert any("screens" in e for e in errors)
    assert not any("endpoints" in e for e in errors)


def test_a_spec_with_no_units_names_the_other_kinds():
    """Never just 'endpoints required' — that sends the agent off to invent a fake
    endpoint instead of reconsidering the kind."""
    joined = " ".join(contracts.validate_spec({"version": 1}))
    for key in ("endpoints", "types", "screens", "criteria"):
        assert key in joined
    for kind in ("schema", "ui", "none"):
        assert kind in joined


def test_infer_kind_tolerates_non_dicts():
    assert contracts.infer_kind("not a spec") is None
    assert contracts.infer_kind(None) is None


# --- staging_url stays the only fetchable URL --------------------------------
def test_non_http_kinds_do_not_require_a_staging_url():
    """No kind requires one, because no kind may CARRY one — the target is host-owned.
    Note this is not "non-http kinds get no target": every kind RESOLVES the host's
    target (see tests/test_staging_url.py). See DECISIONS.md D13."""
    for spec in (_schema_spec(), _ui_spec(), _none_spec()):
        assert "staging_url" not in spec
        assert contracts.validate_spec(spec) == []


@pytest.mark.parametrize("url", [
    "https://api-staging.example.com",
    "https://169.254.169.254/latest/meta-data/",
    "https://127.0.0.1/admin",
    "http://api-staging.example.com",
    "https://foo.internal/api",
])
def test_no_kind_is_a_side_door_for_a_target(url):
    """Nothing lands a fetchable URL in `spec_json` — not a valid one, and not by riding
    in on a kind that has no HTTP surface. There is no such field to fill."""
    assert any("not yours to set" in e for e in contracts.validate_spec(dict(_ui_spec(), staging_url=url)))


@pytest.mark.parametrize("value", [None, "", "   "])
def test_even_a_blank_staging_url_key_is_refused(value):
    """PRESENCE is what is refused, not emptiness. A blank one is still an agent
    reaching for a field it does not own, and letting it through would mean the rule
    depends on how carefully the injection was written."""
    assert any(
        "not yours to set" in e
        for e in contracts.validate_spec(dict(_schema_spec(), staging_url=value))
    )


# --- house style: every error in one pass ------------------------------------
@pytest.mark.parametrize("spec,expected", [
    ({"screens": [{"states": []}, {"name": "", "states": ["ok", ""]}]}, 4),
    ({"types": [{"fields": [{}]}, {"name": "T", "fields": []}]}, 4),
])
def test_new_kinds_collect_all_errors_in_one_pass(spec, expected):
    """An agent must be able to fix everything in a single revision."""
    assert len(contracts.validate_spec(spec)) >= expected


def test_version_is_still_checked_on_every_kind():
    for spec in (_schema_spec(), _ui_spec(), _none_spec()):
        assert any("version" in e for e in contracts.validate_spec(dict(spec, version="1")))
