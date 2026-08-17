from __future__ import annotations

import base64
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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
NORMALIZED_EMAIL_PATTERN = (
    r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


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
        if not re.fullmatch(NORMALIZED_EMAIL_PATTERN, value):
            raise ValueError("invalid email")
        canonical.append(value)
    return sorted(set(canonical))


def _known_key_cache_decision(
    policy: dict[str, Any],
    *,
    cached_at: int,
    now: int,
    refetch_succeeds: bool,
) -> tuple[str, int]:
    if now - cached_at <= policy["known_key_cache_ttl_seconds"]:
        return "validate_with_cached_key", 0
    if refetch_succeeds:
        return "validate_with_refetched_key", 1
    return policy["known_key_refetch_failure"], 1


def _decode_shared_request_body(transport: dict[str, Any], body: dict[str, Any]) -> bytes:
    if body["encoding"] != "base64":
        raise ValueError("unsupported body encoding")
    decoded = base64.b64decode(body["data"], validate=True)
    if len(decoded) != body["length_bytes"]:
        raise ValueError("body length mismatch")
    if len(decoded) > transport["maximum_page_api_body_bytes"]:
        raise ValueError("request too large")
    return decoded


def _project_shared_access_log_path(policy: dict[str, Any], path: str) -> str:
    if policy["redact_entire_suffix"] and path.startswith(policy["protected_prefix"]):
        return policy["logged_path"]
    return path


def _serve_settings_read(
    authorized_route_page_id: str,
    request: dict[str, str],
    controller_read: Any,
) -> tuple[str, dict[str, Any] | None, bool]:
    if authorized_route_page_id != request["page_id"]:
        return "page_mismatch", None, False
    result = controller_read(request)
    if result["show_access"]["page_id"] != request["page_id"]:
        return "internal_protocol_failure", None, True
    return "ok", result, True


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
        "authorized_route_page_id == http_request.page_id == ipc_request.page_id == "
        "ipc_result.show_access.page_id == http_result.show_access.page_id"
    )
    interfaces = {interface["id"]: interface for interface in contract["interfaces"]}
    settings = interfaces["show_access_settings_read"]
    assert settings["request"] == "#/$defs/SettingsReadRequest"
    assert settings["result"] == "#/$defs/SettingsReadResult"
    assert settings["authorization"] == "owner_or_existing_sharing_control_authority"
    assert "cache_control_private_no_store" in settings["delivery"]
    settings_ipc = interfaces["show_access_settings_read_ipc"]
    assert settings_ipc["request"] == settings["request"]
    assert settings_ipc["result"] == settings["result"]


def test_settings_read_binds_route_request_and_result_before_returning_emails() -> None:
    document = _load(ARTIFACTS["show"])
    request = {"page_id": "page:alpha"}
    alpha = {
        "show_access": {
            "page_id": "page:alpha",
            "access_mode": "limited",
            "share_id": "stable_alpha",
            "revision": 5,
            "normalized_emails": ["alice@example.com"],
        }
    }
    beta = {
        "show_access": {
            "page_id": "page:beta",
            "access_mode": "limited",
            "share_id": "stable_beta",
            "revision": 8,
            "normalized_emails": ["bob@example.com"],
        }
    }
    _validate(document, "#/$defs/SettingsReadRequest", request)
    _validate(document, "#/$defs/SettingsReadResult", alpha)
    _validate(document, "#/$defs/SettingsReadResult", beta)

    reads = 0

    def read_alpha(_request: dict[str, str]) -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return alpha

    status, response, store_read = _serve_settings_read("page:alpha", request, read_alpha)
    assert (status, response, store_read) == ("ok", alpha, True)
    assert reads == 1

    status, response, store_read = _serve_settings_read("page:beta", request, read_alpha)
    assert (status, response, store_read) == ("page_mismatch", None, False)
    assert reads == 1

    status, response, store_read = _serve_settings_read("page:alpha", request, lambda _request: beta)
    assert (status, response, store_read) == ("internal_protocol_failure", None, True)
    assert response is None


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
    assert document["x-contract"]["normalization"]["syntax"] == (
        "one_at; dot_atom_local_without_edge_or_consecutive_dot; lowercase_dns_labels_without_edge_hyphen"
    )
    assert _canonical_emails([" Bob@Example.COM ", "alice@example.com", "bob@example.com"]) == [
        "alice@example.com",
        "bob@example.com",
    ]
    for malformed in (
        "not-an-email",
        "\u00e5l\u00eece@example.com",
        "a@.",
        "a@-",
        ".a@example.com",
        "a.@example.com",
        "a..b@example.com",
        "a@-example.com",
        "a@example-.com",
        "a@example..com",
    ):
        with pytest.raises(ValueError):
            _canonical_emails([malformed])

    identity = _load(ARTIFACTS["identity"])
    assert document["$defs"]["NormalizedEmail"]["pattern"] == NORMALIZED_EMAIL_PATTERN
    assert identity["$defs"]["IdentityAssertionClaims"]["properties"]["verified_email"]["pattern"] == (
        NORMALIZED_EMAIL_PATTERN
    )
    assert identity["$defs"]["IdentitySessionRecord"]["properties"]["normalized_verified_email"]["pattern"] == (
        NORMALIZED_EMAIL_PATTERN
    )
    for malformed in ("a@.", "a@-", ".a@example.com"):
        with pytest.raises(ValidationError):
            _validate(
                identity,
                "#/$defs/IdentityAssertionClaims",
                {**identity["x-examples"][1]["value"], "verified_email": malformed},
            )

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
    assert contract["assertion"]["jti"] == "unique_per_issuer_assertion"
    assert flow["state_and_pending_flow_cookie_lifetime_seconds"] == 300
    assert flow["post_expiry_grace_seconds"] == 0
    assert flow["active_flow_cardinality"] == "one_browser_correlation_cookie_per_configured_callback_origin"
    assert flow["latest_login_start_replaces_previous"] is True
    assert flow["stale_or_cookie_less_callback"] == "identity_retry_required"
    assert flow["pending_flow_server_ledger"] is False
    assert flow["successful_callback_consumption"] == {
        "fingerprint": "sha256_utf8(verified_assertion.iss + ascii_nul + verified_assertion.jti)",
        "storage": "local_durable_unique_record",
        "atomic_order": "insert_if_absent_after_complete_verification_and_before_identity_session_creation",
        "duplicate": "identity_retry_required_without_session_creation_or_session_expiry_extension",
        "expires_at": "verified_assertion.exp",
        "cleanup": "delete_after_expiry",
        "failed_or_cookie_less_callback_allocates_record": False,
    }
    assert flow["start_path"] == "/auth/show-identity/start"
    assert flow["shared_cookie_path"] == "/auth/show-identity"
    assert flow["pending_flow_cookie"]["maximum_age_seconds"] == 300
    assert flow["pending_flow_cookie"]["same_site"] == "None"
    assert flow["pending_flow_cookie"]["path"] == "/auth/show-identity"
    assert flow["pending_flow_cookie"]["visible_to"] == ["login_start", "form_post_callback"]
    assert flow["server_pending_flow_records"] == 0
    assert flow["request_cookie_wire"] == "#/$defs/PendingFlowCookiePair"
    assert flow["set_cookie_wire"] == "#/$defs/PendingFlowSetCookie"
    assert flow["callback_expiry_wire"] == "#/$defs/PendingFlowExpiredSetCookie"
    assert flow["repeated_cookie_less_starts"] == ("allocate_zero_durable_or_in_memory_pending_flow_records")
    assert "pending_flow_store" not in flow
    assert flow["callback_checks_in_order"][-1] == "reresolve_safe_return_share"
    assert "atomically_consume_successful_callback_fingerprint_once" in flow["callback_checks_in_order"]
    assert key_lookup["maximum_forced_refreshes_per_login_attempt"] == 1
    assert key_lookup["refresh_is_coalesced_by_issuer"] is True
    assert key_lookup["refresh_is_not_keyed_by_untrusted_kid"] is True
    assert key_lookup["seamless_rotation_or_previous_key_overlap_required"] is False
    assert key_lookup["known_key_cache_ttl_seconds"] == 300
    assert key_lookup["known_kid_older_than_cache_ttl"] == ("refetch_paired_issuer_jwks_once_before_validation")
    assert _known_key_cache_decision(key_lookup, cached_at=100, now=400, refetch_succeeds=False) == (
        "validate_with_cached_key",
        0,
    )
    assert _known_key_cache_decision(key_lookup, cached_at=100, now=401, refetch_succeeds=True) == (
        "validate_with_refetched_key",
        1,
    )
    assert _known_key_cache_decision(key_lookup, cached_at=100, now=401, refetch_succeeds=False) == (
        "identity_retry_required",
        1,
    )

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
    examples = {example["name"]: example["value"] for example in _load(ARTIFACTS["identity"])["x-examples"]}
    pending_flow_cookie = examples["pending_flow_cookie_pair"]
    pending_flow_set_cookie = examples["pending_flow_set_cookie"]
    expired_cookie = examples["pending_flow_expired_set_cookie"]
    assert claims["exp"] - claims["iat"] == 300
    assert state["exp"] - state["iat"] == 300
    assert (
        state["pending_flow_cookie_sha256"] == hashlib.sha256(pending_flow_cookie["value"].encode("ascii")).hexdigest()
    )
    assert pending_flow_set_cookie["value"] == pending_flow_cookie["value"]
    document = _load(ARTIFACTS["identity"])
    _validate(document, "#/$defs/PendingFlowCookiePair", pending_flow_cookie)
    _validate(document, "#/$defs/PendingFlowSetCookie", pending_flow_set_cookie)
    _validate(document, "#/$defs/PendingFlowExpiredSetCookie", expired_cookie)
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/PendingFlowCookiePair", pending_flow_set_cookie)
    callback_only_cookie = {**pending_flow_set_cookie, "path": "/auth/show-identity/callback"}
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/PendingFlowSetCookie", callback_only_cookie)


def test_identity_session_wire_is_minimal_fixed_lifetime_and_identity_only() -> None:
    document = _load(ARTIFACTS["identity"])
    contract = document["x-contract"]
    session = contract["identity_session"]
    interfaces = {interface["id"]: interface for interface in contract["interfaces"]}

    assert interfaces["show_identity_login_start"] == {
        "id": "show_identity_login_start",
        "producer": "show_identity_browser",
        "consumer": "avibe.local_http.GET /auth/show-identity/start",
        "delivery": "browser_navigation_with_optional_cookie_pair",
        "request": "#/$defs/LoginStartRequest",
        "request_cookie": "#/$defs/PendingFlowCookiePair",
        "request_cookie_optional_on_first_start": True,
        "result_cookie": "#/$defs/PendingFlowSetCookie",
    }
    examples = {example["name"]: example["value"] for example in document["x-examples"]}
    login_start = examples["login_start_request"]
    _validate(document, "#/$defs/LoginStartRequest", login_start)
    assert login_start["safe_return_path"] == ("/p/stable_alpha/users/alice@example.com")
    _validate(
        document,
        "#/$defs/LoginStartRequest",
        {"safe_return_path": "/p/stable_alpha/users/alice@example.com/"},
    )
    for invalid_path in (
        "https://attacker.example/p/stable_alpha/",
        "/show/session-1/",
        "/p/stable_alpha/../private",
        "/p/stable_alpha/users/alice@example.com?token=1",
    ):
        with pytest.raises(ValidationError):
            _validate(
                document,
                "#/$defs/LoginStartRequest",
                {"safe_return_path": invalid_path},
            )
    assert interfaces["show_identity_form_post"]["request_cookie"] == "#/$defs/PendingFlowCookiePair"
    assert interfaces["show_identity_form_post"]["correlation_cookie_expiry"] == ("#/$defs/PendingFlowExpiredSetCookie")
    assert interfaces["show_identity_session_cookie"]["result"] == "#/$defs/IdentitySessionCookie"
    assert interfaces["show_identity_session_lookup"]["request"] == "#/$defs/IdentitySessionLookup"
    assert interfaces["show_identity_session_lookup"]["result"] == "#/$defs/IdentitySessionRecord"
    assert session["cookie_name"] == "__Host-avibe_show_identity_session"
    assert session["cookie_entropy_bytes"] == 32
    assert session["cookie_attributes"] == {
        "host_only": True,
        "domain_attribute_allowed": False,
        "secure": True,
        "http_only": True,
        "same_site": "Lax",
        "path": "/",
        "maximum_age_seconds": 2_592_000,
    }
    assert session["fixed_lifetime_seconds"] == 2_592_000
    assert session["lookup_invariant"] == (
        "sha256_utf8(request.session_token) == record.token_sha256 && "
        "request.instance_id == record.instance_id && "
        "request.callback_origin == record.callback_origin && now < record.expires_at"
    )
    assert session["sliding_refresh"] is False
    assert session["session_family"] is False
    assert session["cross_session_revocation"] is False
    assert session["older_bearer_records"] == "may_remain_valid_until_their_fixed_expiry"
    assert session["later_limited_navigation"].endswith("current_local_show_access_and_membership_once")

    examples = {example["name"]: example["value"] for example in document["x-examples"]}
    cookie = examples["identity_session_cookie"]
    lookup = examples["identity_session_lookup"]
    record = examples["identity_session_record"]
    assert lookup["session_token"] == cookie["value"]
    assert record["token_sha256"] == hashlib.sha256(cookie["value"].encode("ascii")).hexdigest()
    assert record["expires_at"] - record["created_at"] == 2_592_000
    assert set(record) == {
        "token_sha256",
        "instance_id",
        "callback_origin",
        "subject",
        "normalized_verified_email",
        "created_at",
        "expires_at",
    }
    assert set(record).isdisjoint(session["session_forbidden_authority"])

    invalid_record = {**record, "page_id": "page:alpha"}
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/IdentitySessionRecord", invalid_record)

    invalid_cookie = {**cookie, "same_site": "None"}
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/IdentitySessionCookie", invalid_cookie)


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
    assert isolation["shared_graph_key"] == [
        "source_session_id",
        "page_id",
        "share_id",
        "admitted_revision",
        "shared",
    ]
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
    assert boundary["csp_required_directives"]["base-uri"] == "<server_owned_capability_prefix>"
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


def test_shared_admission_result_and_protected_resource_wire_are_closed() -> None:
    document = _load(ARTIFACTS["runtime"])
    runtime = document["x-contract"]
    interfaces = {interface["id"]: interface for interface in runtime["interfaces"]}
    assert interfaces["shared_document_admission"]["result"] == "#/$defs/SharedAdmissionResult"
    assert interfaces["shared_protected_resource"]["request"] == "#/$defs/SharedBrowserRequest"
    assert interfaces["shared_page_api_preflight"] == {
        "id": "shared_page_api_preflight",
        "producer": "shared_browser_document",
        "consumer": "avibe.local_http.SharedCapabilityProxy",
        "delivery": "opaque_origin_cors_preflight_terminated_before_runtime",
        "request": "#/$defs/SharedPageApiPreflightRequest",
        "result": "#/$defs/SharedPageApiPreflightResult",
    }

    admissions = [
        example["value"] for example in document["x-examples"] if example["schema_ref"] == "#/$defs/SharedAdmission"
    ]
    assert admissions
    assert all(admission["source_session_id"] == "session-1" for admission in admissions)
    assert runtime["shared_admission"]["source_session_id_visibility"] == (
        "trusted_internal_only_never_browser_exposed"
    )
    assert "source_session_id" not in document["$defs"]["SharedDocumentCapability"]["properties"]
    missing_session = deepcopy(admissions[0])
    missing_session.pop("source_session_id")
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/SharedAdmission", missing_session)
    limited = next(admission for admission in admissions if admission["audience"] == "limited")
    limited_without_subject = {**limited, "identity_subject": None}
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/SharedAdmission", limited_without_subject)
    public = next(admission for admission in admissions if admission["audience"] == "public")
    _validate(document, "#/$defs/SharedAdmission", public)

    capability = next(
        example["value"] for example in document["x-examples"] if example["name"] == "document_capability"
    )
    for field in ("namespace_id", "document_id"):
        for invalid_value in (
            "identifier/invalid_01",
            "identifier:invalid_01",
            "identifier invalid_01",
            "identifier_unicode_\u65e0\u6548",
        ):
            invalid_capability = deepcopy(capability)
            invalid_capability[field] = invalid_value
            with pytest.raises(ValidationError):
                _validate(document, "#/$defs/SharedDocumentCapability", invalid_capability)

    failures = runtime["shared_admission"]["result"]["failures"]
    assert set(failures) == {
        "shared_runtime_unavailable",
        "snapshot_too_large",
        "capacity_exhausted",
        "capture_timeout",
        "reload_required",
    }
    for code, response in failures.items():
        _validate(
            document,
            "#/$defs/SharedAdmissionResult",
            {"outcome": "failure", "code": code, **response},
        )
        assert all(forbidden not in response["body"] for forbidden in ("/", "workspace", "source", "stack"))
        with pytest.raises(ValidationError):
            _validate(
                document,
                "#/$defs/SharedAdmissionResult",
                {"outcome": "failure", "code": code, "http_status": 418, "body": response["body"]},
            )

    transport = runtime["shared_response_transport"]
    assert transport["path_prefix_template"] == ("/__avibe_show_shared/v1/{namespace_id}/{document_id}/{capability}/")
    assert transport["document_url_has_trailing_slash"] is True
    assert transport["suffix_forms"] == {
        "document_root": "<prefix>",
        "history_fallback": "<prefix>history/{normalized_relative_history_path}",
        "opaque_asset": "<prefix>asset/{resource_handle}",
        "page_api": "<prefix>api/{normalized_relative_api_path}",
    }
    assert transport["fallback_document_base_url"] == (
        "strip_page_authored_base_elements_then_inject_exact_capability_root_before_page_content"
    )
    assert transport["relative_url_resolution"] == (
        "modules_styles_raw_assets_and_fetch(./api/data)_resolve_from_exact_capability_root_on_root_and_history_documents"
    )
    assert transport["document_local_resource_rewrite"] == {
        "attributes": [
            "script.src",
            "link.href",
            "img.src",
            "img.srcset",
            "source.src",
            "source.srcset",
            "video.src",
            "video.poster",
            "audio.src",
        ],
        "resolution_base": "captured_source_document_before_server_owned_base_injection",
        "output": "absolute_url_under_same_capability_prefix_asset_opaque_resource_handle",
        "srcset": "rewrite_each_local_candidate_and_preserve_its_density_or_width_descriptor",
        "inline_style_css_urls": "rewrite_each_local_url_with_the_same_opaque_asset_rule",
        "rewrite_before_browser_response": True,
        "authored_local_path_browser_visible": False,
    }
    assert transport["page_api_namespace_mapping"] == "captured_page_api_only"
    assert transport["nested_import_rewrite"] == (
        "module_import_css_import_and_css_url_dependencies_use_absolute_opaque_asset_urls_under_same_namespace_document_capability_prefix"
    )
    assert transport["query_authority"] is False
    assert transport["cookie_authority"] is False
    assert transport["custom_request_header_required"] is False
    assert transport["capability_path_access_log_policy"] == {
        "protected_prefix": "/__avibe_show_shared/v1/",
        "logged_path": "/__avibe_show_shared/v1/[redacted]",
        "redact_entire_suffix": True,
        "query_logged": False,
        "body_logged": False,
    }
    examples = {example["name"]: example["value"] for example in document["x-examples"]}
    document_path = examples["shared_document_request"]["path"]
    history_path = examples["shared_history_fallback_request"]["path"]
    asset_path = examples["shared_module_request"]["path"]
    entry_rewrite = examples["default_entry_resource_rewrite"]
    api_request = examples["shared_page_api_request"]
    preflight_request = examples["shared_page_api_preflight"]
    preflight_result = examples["shared_page_api_preflight_result"]
    api_path = api_request["path"]
    assert document_path.endswith("/")
    _validate(document, "#/$defs/DocumentResourceRewrite", entry_rewrite)
    assert entry_rewrite["authored_value"] == "./src/main.tsx"
    assert entry_rewrite["rewritten_url"] == (f"{document_path}asset/{entry_rewrite['resource_handle']}")
    assert "src/main.tsx" not in entry_rewrite["rewritten_url"]
    assert urljoin(f"https://show.example.test{document_path}", "./api/data") == (
        f"https://show.example.test{api_path}"
    )
    assert urljoin(f"https://show.example.test{history_path}", "./api/data") != (f"https://show.example.test{api_path}")
    injected_base_url = f"https://show.example.test{document_path}"
    assert urljoin(injected_base_url, "./api/data") == f"https://show.example.test{api_path}"
    assert transport["top_level_history_mapping"] == {
        "browser_url": "/p/<share_id>/{normalized_relative_history_path}",
        "protected_request": "<prefix>history/{normalized_relative_history_path}",
        "preserve_browser_url": True,
        "terminal_slash": "optional_and_preserved",
    }
    top_level_url = "/p/stable_alpha/users/alice@example.com"
    relative_history_path = top_level_url.removeprefix("/p/stable_alpha/")
    assert history_path == f"{document_path}history/{relative_history_path}"
    for surface in ("document", "fallback"):
        _validate(
            document,
            "#/$defs/SharedBrowserRequest",
            {"method": "GET", "surface": surface, "path": document_path},
        )
    _validate(
        document,
        "#/$defs/SharedBrowserRequest",
        {"method": "GET", "surface": "fallback", "path": history_path},
    )
    _validate(
        document,
        "#/$defs/SharedBrowserRequest",
        {"method": "GET", "surface": "fallback", "path": f"{history_path}/"},
    )
    history_prefix = history_path.removesuffix("users/alice@example.com")
    _validate(
        document,
        "#/$defs/SharedBrowserRequest",
        {"method": "GET", "surface": "fallback", "path": f"{history_prefix}reports/daily"},
    )
    with pytest.raises(ValidationError):
        _validate(
            document,
            "#/$defs/SharedBrowserRequest",
            {"method": "GET", "surface": "document", "path": history_path},
        )
    for surface in ("module", "style", "raw_asset"):
        _validate(
            document,
            "#/$defs/SharedBrowserRequest",
            {"method": "GET", "surface": surface, "path": asset_path},
        )
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        _validate(
            document,
            "#/$defs/SharedBrowserRequest",
            {"method": method, "surface": "page_api", "path": f"{api_path}/nested-item"},
        )
    api_prefix = api_path.removesuffix("data")
    _validate(
        document,
        "#/$defs/SharedBrowserRequest",
        {"method": "GET", "surface": "page_api", "path": f"{api_prefix}users/alice@example.com"},
    )
    _validate(document, "#/$defs/SharedBrowserRequest", api_request)
    assert api_request["content_type"] == "application/json"
    assert _decode_shared_request_body(transport, api_request["body"]) == b'{"ping":true}'
    multipart_boundary = "----AvibeBoundary7MA4YWxkTrZu0gW"
    multipart_payload = f"--{multipart_boundary}--\r\n".encode()
    multipart_request = {
        **api_request,
        "content_type": f"multipart/form-data; boundary={multipart_boundary}",
        "body": {
            "encoding": "base64",
            "data": base64.b64encode(multipart_payload).decode("ascii"),
            "length_bytes": len(multipart_payload),
        },
    }
    _validate(document, "#/$defs/SharedBrowserRequest", multipart_request)
    assert _decode_shared_request_body(transport, multipart_request["body"]) == multipart_payload
    assert transport["page_api_content_type_forwarding"] == (
        "forward_the_validated_value_unchanged_without_reconstructing_parameters"
    )
    assert transport["maximum_page_api_body_bytes"] == 1_048_576
    assert transport["oversized_page_api_request"] == {"http_status": 413, "body": "request too large"}
    assert transport["forwarded_page_api_metadata_exact"] == ["content_type", "body"]
    assert transport["forbidden_ambient_request_metadata"] == ["cookie", "authorization", "arbitrary_headers"]
    assert transport["page_api_preflight"]["termination"] == (
        "trusted_avibe_proxy_returns_fixed_response_without_runtime_forwarding"
    )
    _validate(document, "#/$defs/SharedPageApiPreflightRequest", preflight_request)
    _validate(document, "#/$defs/SharedPageApiPreflightResult", preflight_result)
    assert preflight_result["forwarded_to_runtime"] is False
    assert preflight_result["access_control_allow_origin"] == "*"
    assert preflight_result["access_control_allow_credentials"] is False
    for invalid_preflight in (
        {**preflight_request, "origin": "https://show.example.test"},
        {**preflight_request, "access_control_request_headers": ["authorization"]},
        {**preflight_request, "access_control_request_headers": ["content-type", "x-page-token"]},
    ):
        with pytest.raises(ValidationError):
            _validate(document, "#/$defs/SharedPageApiPreflightRequest", invalid_preflight)
    mismatched_body = {**api_request["body"], "length_bytes": api_request["body"]["length_bytes"] - 1}
    with pytest.raises(ValueError, match="body length mismatch"):
        _decode_shared_request_body(transport, mismatched_body)
    oversized_body = {"encoding": "base64", "data": "", "length_bytes": 1_048_577}
    with pytest.raises(ValidationError):
        _validate(document, "#/$defs/SharedRequestBody", oversized_body)
    for forbidden_field in ("cookie", "authorization", "headers"):
        with pytest.raises(ValidationError):
            _validate(
                document,
                "#/$defs/SharedBrowserRequest",
                {**api_request, forbidden_field: "ambient"},
            )
    with pytest.raises(ValidationError):
        _validate(
            document,
            "#/$defs/SharedBrowserRequest",
            {**api_request, "content_type": "Application/JSON"},
        )
    for invalid_content_type in (
        "multipart/form-data",
        "multipart/form-data; boundary=",
        "multipart/form-data; boundary=valid; charset=utf-8",
    ):
        with pytest.raises(ValidationError):
            _validate(
                document,
                "#/$defs/SharedBrowserRequest",
                {**api_request, "content_type": invalid_content_type},
            )

    log_policy = transport["capability_path_access_log_policy"]
    for sensitive_path in (history_path, f"{api_prefix}users/alice@example.com"):
        logged_path = _project_shared_access_log_path(log_policy, sensitive_path)
        assert logged_path == "/__avibe_show_shared/v1/[redacted]"
        assert "alice@example.com" not in logged_path
        assert sensitive_path.split("/api/")[-1] not in logged_path
    for forbidden_path in (
        f"{api_path}?token=ambient",
        f"{api_prefix}",
        f"{api_prefix}/data",
        f"{api_prefix}./data",
        f"{api_prefix}../data",
        f"{api_prefix}%2e%2e/data",
        f"{api_prefix}private%2Fdata",
        f"{api_prefix}private%5Cdata",
        "/workspace/src/main.tsx",
        "/p/stable_alpha/main.ts",
    ):
        with pytest.raises(ValidationError):
            _validate(
                document,
                "#/$defs/SharedBrowserRequest",
                {"method": "GET", "surface": "page_api", "path": forbidden_path},
            )

    for forbidden_path in (
        history_prefix,
        f"{history_prefix}/daily",
        f"{history_prefix}./daily",
        f"{history_prefix}../daily",
        f"{history_prefix}%2e%2e/daily",
        f"{history_prefix}reports%2Fdaily",
        f"{history_path}?identity=ambient",
    ):
        with pytest.raises(ValidationError):
            _validate(
                document,
                "#/$defs/SharedBrowserRequest",
                {"method": "GET", "surface": "fallback", "path": forbidden_path},
            )


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
    assert cache["public_non_versioned_surfaces"] == {
        "surfaces": [
            "shell",
            "document",
            "module",
            "style",
            "raw_asset",
            "fallback",
            "page_api",
            "error",
            "redirect",
        ],
        "response_headers": {"Cache-Control": "private, no-store"},
    }
    assert cache["public_access_dependent_redirects"] == [
        "resource_viewer_p_to_canonical_show",
        "resource_editor_p_to_canonical_show",
    ]
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
    assert "Version 1 adds no server replay ledger" not in plan
    assert "atomically records the assertion issuer/JTI fingerprint" in plan
    assert "does not yet prove production Avibe, Backend, Runtime, browser" in readme
    assert "SHOW-LIVE-" not in plan
    assert "scenario-bindings" not in readme
    identity = _load(ARTIFACTS["identity"])["x-contract"]["reference_evidence"]
    assert identity["scenario"] == "AUTH-SETUP-401"
    assert identity["harness"] == "tests/scenario_harness/show_identity_callback.py"
    assert identity["production_crypto_browser_conformance"].startswith("future_")


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
    assert "tests/scenario_harness/show_identity_callback.py" in scenario_index
    assert "tests/scenario_harness/show_identity_callback.py" in auth_catalog
    assert "AUTH-SETUP-404" not in auth_catalog
    assert "ShowIdentityLimitedPageScenarioTests::test_identity_session_rechecks_limited_membership_on_navigation" in (
        auth_catalog
    )
    assert "backend: show_page_email" not in auth_catalog
