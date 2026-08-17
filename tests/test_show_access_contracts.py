from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from core.show_runtime import (
    SHOW_RUNTIME_CONTEXT_HEADER,
    SHOW_RUNTIME_CONTEXT_KEY_FEATURE,
    SHOW_RUNTIME_PROTOCOL_HEADER,
    SHOW_RUNTIME_PROTOCOL_VERSION,
    ShowRuntimeContext,
    ShowRuntimeContextCapability,
)


CONTRACTS = Path("docs/plans/show-access-contracts")
PLAN = Path("docs/plans/public-show-live-update.md")
ARTIFACTS = {
    "show": CONTRACTS / "show-access.json",
    "identity": CONTRACTS / "identity-auth.json",
    "runtime": CONTRACTS / "runtime-containment.json",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator_for(document: dict[str, Any], ref: str) -> Draft202012Validator:
    return Draft202012Validator(
        {
            "$schema": document["$schema"],
            "$defs": document["$defs"],
            "$ref": ref,
        }
    )


def _validate(document: dict[str, Any], ref: str, value: object) -> None:
    _validator_for(document, ref).validate(value)


def _canonical_emails(values: list[str]) -> list[str]:
    canonical = []
    for raw in values:
        value = raw.strip(" \t\r\n\f\v")
        if not value.isascii():
            raise ValueError("invalid email")
        value = value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+", value):
            raise ValueError("invalid email")
        canonical.append(value)
    return sorted(set(canonical))


def test_contract_directory_has_only_the_three_versioned_boundaries() -> None:
    assert {path.name for path in CONTRACTS.glob("*.json")} == {
        "identity-auth.json",
        "runtime-containment.json",
        "show-access.json",
    }
    assert all(_load(path)["x-contract"]["version"] == 1 for path in ARTIFACTS.values())


@pytest.mark.parametrize("path", ARTIFACTS.values(), ids=ARTIFACTS.keys())
def test_schema_and_every_embedded_example_validate(path: Path) -> None:
    document = _load(path)
    Draft202012Validator.check_schema(document)
    for example in document["x-examples"]:
        _validate(document, example["schema_ref"], example["value"])


@pytest.mark.parametrize("path", ARTIFACTS.values(), ids=ARTIFACTS.keys())
def test_interface_signatures_reference_owned_definitions(path: Path) -> None:
    document = _load(path)
    definitions = document["$defs"]
    for interface in document["x-contract"]["interfaces"]:
        for field in ("request", "result"):
            if field not in interface:
                continue
            prefix = "#/$defs/"
            assert interface[field].startswith(prefix)
            assert interface[field][len(prefix) :] in definitions
        assert interface["producer"]
        assert interface["consumer"]
        assert interface["delivery"]


def test_show_access_is_one_closed_local_aggregate() -> None:
    document = _load(ARTIFACTS["show"])
    contract = document["x-contract"]
    aggregate = contract["storage_boundary"]["aggregate"]
    properties = document["$defs"]["ShowAccess"]["properties"]

    assert contract["authority"] == "local_avibe_only"
    assert aggregate == ["access_mode", "share_id", "revision", "normalized_emails"]
    assert set(properties) == {"page_id", *aggregate}
    assert properties["access_mode"]["enum"] == ["private", "limited", "public"]
    assert contract["storage_boundary"]["transaction"] == "one_local_transaction"
    assert contract["storage_boundary"]["exact_email_persistence"] == "local_avibe_only"
    assert contract["page_identity"]["required_equality"] == (
        "authorized_route_page_id == request.page_id == result.show_access.page_id"
    )
    settings = next(interface for interface in contract["interfaces"] if interface["id"] == "show_access_settings_read")
    assert settings["authorization"] == "owner_or_existing_sharing_control_authority"
    assert "cache_control_private_no_store" in settings["delivery"]


def test_show_access_mode_invariants_reject_invalid_points() -> None:
    document = _load(ARTIFACTS["show"])
    base = {
        "page_id": "page:alpha",
        "share_id": "stable_alpha",
        "revision": 1,
        "normalized_emails": [],
    }

    invalid = [
        {**base, "access_mode": "private", "normalized_emails": ["a@example.com"]},
        {**base, "access_mode": "limited", "share_id": None, "normalized_emails": ["a@example.com"]},
        {**base, "access_mode": "limited", "normalized_emails": []},
        {**base, "access_mode": "public", "normalized_emails": ["a@example.com"]},
        {**base, "access_mode": "organization"},
    ]
    for value in invalid:
        with pytest.raises(ValidationError):
            _validate(document, "#/$defs/ShowAccess", value)


def test_apply_is_revision_cas_without_hosted_protocol_or_receipts() -> None:
    document = _load(ARTIFACTS["show"])
    contract = document["x-contract"]
    request_properties = document["$defs"]["ApplyRequest"]["properties"]

    assert set(request_properties) == {
        "page_id",
        "expected_revision",
        "target_access_mode",
        "target_share_id",
        "target_emails",
    }
    assert contract["apply"] == {
        "cas_field": "expected_revision",
        "canonical_change_fields": ["target_access_mode", "target_share_id", "target_emails"],
        "canonical_change": "advance_revision_exactly_once",
        "canonical_no_change": "return_no_change_without_revision_advance",
        "stale_expected_revision": "conflict_without_write",
        "share_id_collision": "share_id_taken_without_write",
        "lost_response": "reread_current_show_access_then_submit_again_with_current_revision_if_needed",
        "durable_mutation_receipts": False,
    }
    assert "mutation_id" not in request_properties
    assert {"hosted_grant", "prepare_commit", "cleanup", "mutation_receipt"}.issubset(contract["forbidden_concepts"])


def test_apply_reuses_show_access_mode_invariants() -> None:
    document = _load(ARTIFACTS["show"])
    invalid_limited = {
        "page_id": "page:alpha",
        "expected_revision": 4,
        "target_access_mode": "limited",
        "target_share_id": None,
        "target_emails": [],
    }
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/ApplyRequest", invalid_limited)


def test_canonical_email_algorithm_is_executable_and_examples_are_canonical() -> None:
    document = _load(ARTIFACTS["show"])
    assert _canonical_emails([" Bob@Example.COM ", "alice@example.com", "bob@example.com"]) == [
        "alice@example.com",
        "bob@example.com",
    ]
    with pytest.raises(ValueError):
        _canonical_emails(["not-an-email"])
    with pytest.raises(ValueError):
        _canonical_emails(["\u00e5l\u00eece@example.com"])

    for example in document["x-examples"]:
        value = example["value"]
        for field in ("normalized_emails", "target_emails"):
            if field in value:
                assert value[field] == _canonical_emails(value[field])
        if "show_access" in value:
            emails = value["show_access"]["normalized_emails"]
            assert emails == _canonical_emails(emails)


def test_identity_wire_is_exactly_identity_only() -> None:
    document = _load(ARTIFACTS["identity"])
    contract = document["x-contract"]
    expected_claims = [
        "iss",
        "aud",
        "sub",
        "iat",
        "exp",
        "jti",
        "nonce",
        "instance_id",
        "verified_email",
    ]

    assert contract["purpose"] == "verified_identity_only"
    assert contract["authorize"]["request_fields_exact"] == ["state", "nonce", "redirect_uri"]
    assert contract["assertion"]["algorithm"] == "RS256"
    assert contract["assertion"]["protected_header_fields_exact"] == ["alg", "typ", "kid"]
    assert contract["assertion"]["claims_exact"] == expected_claims
    assert set(document["$defs"]["IdentityAssertionClaims"]["properties"]) == set(expected_claims)
    assert contract["backend_forbidden_behavior"] == [
        "receive_local_whitelist",
        "decide_page_membership",
        "derive_instance_role",
        "create_instance_access_context",
    ]


def test_identity_schema_rejects_page_authority_and_non_string_audience() -> None:
    document = _load(ARTIFACTS["identity"])
    claims = deepcopy(document["x-examples"][1]["value"])
    claims["page_id"] = "page:alpha"
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/IdentityAssertionClaims", claims)

    claims.pop("page_id")
    claims["aud"] = ["avibe-show-identity:oauth-client-1"]
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/IdentityAssertionClaims", claims)


def test_identity_flow_has_one_fixed_lifetime_and_latest_flow_wins() -> None:
    contract = _load(ARTIFACTS["identity"])["x-contract"]
    flow = contract["local_flow"]
    key_lookup = contract["key_lookup"]

    assert contract["assertion"]["ttl_seconds"] == 300
    assert contract["assertion"]["clock_skew_seconds"] == 0
    assert flow["state_and_correlation_cookie_lifetime_seconds"] == 300
    assert flow["post_expiry_grace_seconds"] == 0
    assert flow["active_flow_cardinality"] == "one_per_browser_and_configured_callback_origin"
    assert flow["latest_login_start_replaces_previous"] is True
    assert flow["stale_or_replayed_callback"] == "identity_retry_required"
    assert flow["correlation_cookie"]["maximum_age_seconds"] == 300
    assert flow["correlation_cookie"]["same_site"] == "None"
    assert flow["callback_checks_in_order"][-1] == "reresolve_safe_return_share"
    assert key_lookup["maximum_forced_refreshes_per_login_attempt"] == 1
    assert key_lookup["refresh_is_coalesced_by_issuer"] is True
    assert key_lookup["refresh_is_not_keyed_by_untrusted_kid"] is True
    assert key_lookup["seamless_rotation_or_previous_key_overlap_required"] is False

    claims = next(
        example["value"]
        for example in _load(ARTIFACTS["identity"])["x-examples"]
        if example["name"] == "identity_assertion_claims"
    )
    state = next(
        example["value"]
        for example in _load(ARTIFACTS["identity"])["x-examples"]
        if example["name"] == "signed_state_claims"
    )
    assert claims["exp"] - claims["iat"] == 300
    assert state["exp"] - state["iat"] == 300


def test_runtime_protocol_constants_match_frozen_contract() -> None:
    contract = _load(ARTIFACTS["runtime"])["x-contract"]
    protocol = contract["protocol"]

    assert protocol["version"] == SHOW_RUNTIME_PROTOCOL_VERSION == 1
    assert protocol["version_header"] == SHOW_RUNTIME_PROTOCOL_HEADER
    assert protocol["context_header"] == SHOW_RUNTIME_CONTEXT_HEADER
    assert protocol["keyed_context_feature"] == SHOW_RUNTIME_CONTEXT_KEY_FEATURE
    assert protocol["contexts"] == [context.value for context in ShowRuntimeContext]
    assert {capability.value for capability in ShowRuntimeContextCapability} == {
        "supported",
        "unsupported",
        "transient-unknown",
    }


def test_shared_runtime_admission_fails_closed_without_keyed_context() -> None:
    runtime = _load(ARTIFACTS["runtime"])["x-contract"]
    admission = runtime["shared_admission"]
    assert admission["keyed_context_supported"] == "required_before_any_shared_runtime_work"
    assert admission["keyed_context_unsupported"] == ("shared_runtime_unavailable_without_legacy_graph_access")
    assert admission["keyed_context_transient_unknown"] == (
        "bounded_probe_then_shared_runtime_unavailable_without_upstream"
    )
    assert runtime["top_level_route_decision"] == {
        "resource_viewer_or_editor": "redirect_to_canonical_show_without_shared_runtime_admission",
        "listed_only_identity": "serve_shared_p_without_editor_capability",
        "public_anonymous": "serve_shared_p_without_editor_capability",
    }


def test_repeated_private_edits_never_build_or_rebase_shared_graphs() -> None:
    isolation = _load(ARTIFACTS["runtime"])["x-contract"]["context_isolation"]
    assert isolation["private_graph_key"] != isolation["shared_graph_key"]
    assert isolation["lifecycle"] == "private_and_shared_graphs_are_independent"
    assert isolation["shared_build_trigger"] == ("successful_new_navigation_or_manual_refresh_admission_only")

    for _edit_count in (1, 2, 1000):
        assert isolation["ordinary_private_editor_edit"] == (
            "keeps_private_graph_identity_and_never_creates_builds_or_rebases_shared_graph"
        )
        assert isolation["shared_hmr"] is False


def test_shared_authorization_is_once_per_document_not_per_request() -> None:
    runtime = _load(ARTIFACTS["runtime"])["x-contract"]
    admission = runtime["shared_admission"]
    lifetime = runtime["admitted_document_lifetime"]

    assert admission["entry_events"] == ["new_top_level_navigation", "manual_refresh"]
    assert lifetime["valid_while"] == "runtime_namespace_and_document_handle_exist"
    assert lifetime["capability_entropy_bits"] == 256
    assert lifetime["capability_derivation"] == "csprng_non_derived"
    assert lifetime["subresource_validation"].endswith("without_show_access_lookup")
    assert lifetime["permission_recheck_per_subresource"] is False
    assert lifetime["permission_polling"] is False
    assert lifetime["permission_change_push_refresh"] is False
    assert lifetime["permission_change_revokes_loaded_document"] is False
    assert lifetime["membership_mode_binding_or_revision_change"] == ("affects_next_navigation_or_manual_refresh_only")
    assert lifetime["time_based_capability_or_namespace_expiry"] is False
    assert lifetime["namespace_loss_causes"] == [
        "runtime_restart",
        "explicit_operational_shutdown",
        "global_resource_budget_pressure",
    ]


def test_shared_browser_boundary_denies_privileged_surfaces_and_all_workers() -> None:
    runtime = _load(ARTIFACTS["runtime"])["x-contract"]
    boundary = runtime["browser_boundary"]
    assert boundary["iframe_sandbox_tokens_exact"] == ["allow-scripts"]
    assert boundary["allow_same_origin"] is False
    assert boundary["csp_required_directives"]["worker-src"] == "'none'"
    assert boundary["unsupported_worker_kinds"] == ["Worker", "SharedWorker", "ServiceWorker"]
    assert boundary["worker_attempt_outcome"] == "shared_worker_unsupported"
    assert boundary["service_worker_allowed_header_emitted"] is False
    assert set(boundary["shared_code_forbidden_data"]) == {
        "avibe_cookies",
        "avibe_storage",
        "local_apis",
        "hmr",
        "annotations",
        "session_id",
        "workspace_path",
        "source_path",
    }
    transport = runtime["shared_response_transport"]
    assert transport["access_control_allow_origin"] == "*"
    assert transport["access_control_allow_credentials"] is False
    assert transport["set_cookie_allowed"] is False
    assert transport["ambient_identity_or_cookie_authority"] is False


def test_private_editor_authority_is_connection_admission_only() -> None:
    editor = _load(ARTIFACTS["runtime"])["x-contract"]["private_editor"]
    assert editor["surface"] == "/show/<session_id>/"
    assert editor["hmr_and_annotations"] == "resource_editor_only_at_connection_admission"
    assert editor["permission_revocation_polling"] is False
    assert editor["permission_change_forced_close"] is False
    assert editor["new_connections_reauthorize"] is True


def test_runtime_limits_keep_request_timeout_separate_from_document_lifetime() -> None:
    runtime = _load(ARTIFACTS["runtime"])["x-contract"]
    assert runtime["fixed_limits"] == {
        "shared_request_execution_seconds": 60,
        "request_timeout_outcome": "sanitized_request_timeout",
        "maximum_shared_snapshot_bytes": 67108864,
        "maximum_shared_namespaces_per_process": 64,
        "maximum_shared_process_bytes": 536870912,
        "global_budget_scope": "one_per_runtime_process",
        "resource_pressure_eviction_unit": "whole_shared_namespace",
        "resource_pressure_outcome": "reload_required_without_paths_or_diagnostics",
        "timer_or_permission_driven_eviction": False,
    }
    capability = _load(ARTIFACTS["runtime"])["$defs"]["SharedDocumentCapability"]
    assert "expires_at" not in capability["properties"]
    assert "expires_at" not in capability["required"]
    flattened = json.dumps(runtime, sort_keys=True).lower()
    assert "idle_seconds" not in flattened
    assert "absolute_seconds" not in flattened
    assert "pinning" not in flattened
    assert "reclaim_order" not in flattened
    assert "weighted_budget" not in flattened


def test_cache_and_runtime_release_boundaries_are_closed() -> None:
    runtime = _load(ARTIFACTS["runtime"])["x-contract"]
    cache = runtime["cache_policy"]
    release = runtime["release_gate"]

    assert cache["limited_all_surfaces"] == "private, no-store"
    assert cache["public_versioned_asset_exception"] == "public, max-age=31536000, immutable"
    assert cache["public_versioned_asset_constraint"] == (
        "contains_only_public_bytes_and_no_identity_or_page_private_data"
    )
    assert re.fullmatch(r"[0-9a-f]{40}", release["reviewed_runtime_sha"])
    assert release["required_equality"] == ("reviewed_runtime_sha == smoke_tested_runtime_sha == bundled_runtime_sha")
    assert release["feature_advertisement_before_equality"] is False


def test_plan_and_readme_keep_contract_evidence_distinct_from_production() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    readme = (CONTRACTS / "README.md").read_text(encoding="utf-8")

    assert "Production Avibe, Backend, Runtime, browser, and Incus behavior remains future" in plan
    assert "does not yet prove production Avibe, Backend, Runtime, browser" in readme
    assert "SHOW-LIVE-" not in plan
    assert "scenario-bindings" not in readme


def test_superseded_contract_and_scenario_artifacts_are_absent() -> None:
    removed = [
        CONTRACTS / "apply-transition-algebra.json",
        CONTRACTS / "capability-matrix.json",
        CONTRACTS / "local-legacy-mapping.json",
        CONTRACTS / "mirror-registry.json",
        CONTRACTS / "runtime-context.json",
        CONTRACTS / "scenario-bindings.json",
        CONTRACTS / "shared-browser-containment.json",
        Path("tests/scenario_harness/show_identity.py"),
        Path("tests/scenarios/auth_setup/test_show_identity_scenario.py"),
    ]
    assert all(not path.exists() for path in removed)

    scenario_index = Path("tests/scenarios/INDEX.yaml").read_text(encoding="utf-8")
    auth_catalog = Path("tests/scenarios/auth_setup/catalog.yaml").read_text(encoding="utf-8")
    assert "test_show_identity_scenario.py" not in scenario_index
    assert "AUTH-SETUP-404" not in auth_catalog
