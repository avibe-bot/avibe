from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from core.show_runtime import (
    SHOW_RUNTIME_CONTEXT_HEADER,
    SHOW_RUNTIME_CONTEXT_KEY_FEATURE,
    SHOW_RUNTIME_PROTOCOL_HEADER,
    SHOW_RUNTIME_PROTOCOL_VERSION,
    ShowRuntimeContext,
    ShowRuntimeContextCapability,
)


CONTRACTS = Path("docs/plans/show-access-contracts")
DESIGN = Path("docs/plans/public-show-live-update.md")
SCHEMA_SUFFIX = ".schema.json"
DOCUMENT_SCHEMAS = {
    "apply-invocation.json": "apply-invocation.schema.json",
    "apply-transition-algebra.json": "apply-transition-algebra.schema.json",
    "capability-matrix.json": "capability-matrix.schema.json",
    "fixtures/apply-mutations.json": "apply-fixture.schema.json",
    "fixtures/owner-settings.json": "owner-settings-fixture.schema.json",
    "fixtures/show-access.json": "show-access-fixture.schema.json",
    "identity-auth.json": "identity-auth.schema.json",
    "local-legacy-mapping.json": "local-legacy-mapping.schema.json",
    "mirror-registry.json": "mirror-registry.schema.json",
    "retirement.json": "retirement.schema.json",
    "runtime-context.json": "runtime-context.schema.json",
    "scenario-bindings.json": "scenario-bindings.schema.json",
    "shared-browser-containment.json": "shared-browser-containment.schema.json",
}


def _load(relative: str) -> Any:
    return json.loads((CONTRACTS / relative).read_text(encoding="utf-8"))


def _schemas() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted(CONTRACTS.glob(f"*{SCHEMA_SUFFIX}"))
    }


def _registry(schemas: dict[str, dict[str, Any]]) -> Registry:
    registry = Registry()
    for schema in schemas.values():
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validator(schema_name: str) -> Draft202012Validator:
    schemas = _schemas()
    return Draft202012Validator(
        schemas[schema_name],
        registry=_registry(schemas),
        format_checker=FormatChecker(),
    )


def _matches(selector: Any, value: str) -> bool:
    return selector == "*" or selector == value or isinstance(selector, list) and value in selector


def _json_pointer(document: Any, pointer: str) -> Any:
    current = document
    for raw_part in pointer.lstrip("/").split("/") if pointer else []:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _find_object(document: dict[str, Any], collection: str, object_id: str) -> dict[str, Any]:
    matches = [item for item in document[collection] if item.get("id") == object_id]
    assert len(matches) == 1, (collection, object_id)
    return matches[0]


def _canonical_emails(emails: list[str]) -> list[str]:
    return sorted({email.strip().lower() for email in emails})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _validate_show_access_invariants(state: dict[str, Any]) -> None:
    if state["emails"] != _canonical_emails(state["emails"]):
        raise ValueError("ShowAccess emails must be canonical, unique, and lexicographically sorted")


def _matrix_decision(point: dict[str, str]) -> dict[str, Any]:
    matrix = _load("capability-matrix.json")
    matches = [
        rule
        for rule in matrix["rules"]
        if all(_matches(rule["selector"][name], value) for name, value in point.items())
    ]
    assert len(matches) == 1, point
    return matches[0]["decision"]


def _evaluate_algebra(point: dict[str, str]) -> dict[str, Any]:
    algebra = _load("apply-transition-algebra.json")
    preconditions = [
        rule
        for rule in algebra["precondition_rules"]
        if all(_matches(selector, point[name]) for name, selector in rule["selector"].items())
    ]
    assert len(preconditions) == 1, point
    decision = preconditions[0]["decision"]
    result: dict[str, Any] = {
        "decision": decision,
        "availability": point["availability"],
        "external_service_calls": 0,
    }
    if decision != "evaluate_local_transition":
        return result

    binding_rules = [
        rule
        for rule in algebra["binding_semantics"]
        if all(_matches(selector, point[name]) for name, selector in rule["selector"].items())
    ]
    assert len(binding_rules) == 1, point
    binding_rule = binding_rules[0]
    result["binding_outcome"] = binding_rule["outcome"]
    if point["binding_relation"] not in binding_rule["allowed_relation"]:
        result.update(decision="invalid_target", outcome="invalid_target")
        return result

    email_rules = [
        rule
        for rule in algebra["email_semantics"]
        if all(_matches(selector, point[name]) for name, selector in rule["selector"].items())
    ]
    assert len(email_rules) == 1, point
    if point["email_relation"] not in email_rules[0]["allowed_relation"]:
        result.update(decision="invalid_target", outcome="invalid_target")
        return result

    candidate_status = point.get("share_candidate_status")
    if candidate_status is None:
        candidate_status = "available" if binding_rule["candidate_required"] else "not_required"
    if binding_rule["candidate_required"]:
        if candidate_status == "owned_by_other_page":
            result.update(decision="share_id_taken", outcome="share_id_taken", store_write=False)
            return result
        if candidate_status != "available":
            result.update(decision="invalid_target", outcome="invalid_target")
            return result
    elif candidate_status != "not_required":
        result.update(decision="invalid_target", outcome="invalid_target")
        return result

    changed = (
        point["source_mode"] != point["target_mode"]
        or point["binding_relation"] == "different"
        or point["email_relation"] == "different"
    )
    result.update(
        decision="local_transition",
        outcome="applied" if changed else "no_change",
        audience_revision_delta=1 if changed else 0,
    )
    return result


def _evaluate_claim(claim: dict[str, Any]) -> Any:
    source = claim["source"]
    kind = source["kind"]
    if kind == "pointer":
        return _json_pointer(_load(source["document"]), source["pointer"])
    if kind == "object":
        item = _find_object(_load(source["document"]), source["collection"], source["id"])
        return _json_pointer(item, source["pointer"])
    if kind == "matrix":
        return _matrix_decision(source["point"])[source["field"]]
    if kind == "algebra":
        return _evaluate_algebra(source["point"])[source["field"]]
    if kind == "contains":
        return source["value"] in _json_pointer(_load(source["document"]), source["pointer"])
    if kind == "canonical_emails":
        emails = _json_pointer(_load(source["document"]), source["pointer"])
        return ",".join(_canonical_emails(emails))
    if kind == "path_exists":
        return Path(source["path"]).exists()
    if kind == "semantic":
        document = _load(source["document"])
        check = source["check"]
        if check == "identity_closed_loop":
            loop = document["closed_loop_scenario"]
            start, callback, request = loop["start"], loop["callback"], loop["protected_request"]
            handshake = document["local_handshake"]
            cookie = handshake["correlation_cookie"]
            return (
                callback["http_method"] == "POST"
                and callback["delivery"] == "form_post"
                and callback["assertion_location"] == "form_body"
                and callback["content_type"] == "application/x-www-form-urlencoded"
                and callback["cross_site_source"] == "https://avibe.bot"
                and callback["callback_url"].endswith("/auth/show-identity/callback")
                and "?" not in callback["callback_url"]
                and "#" not in callback["callback_url"]
                and callback["callback_url"].split("/", 3)[2] == start["callback_hostname"]
                and callback["callback_url"].split("/", 3)[2] in start["allowed_callback_hostnames"]
                and start["instance_id"] == callback["instance_id"] == request["credential_binding"]["instance_id"]
                and start["page_id"] == callback["resolved_page_id"] == request["credential_binding"]["page_id"]
                and start["share_id"] == callback["resolved_share_id"] == request["credential_binding"]["share_id"]
                and start["nonce"] == callback["nonce"]
                and start["correlation_cookie"] == callback["correlation_cookie"]
                and hashlib.sha256(start["correlation_cookie"].encode("utf-8")).hexdigest()
                == start["correlation_cookie_sha256"]
                and start["correlation_cookie_expires_at"] <= start["signed_state_expires_at"]
                and cookie
                == {
                    "name": "avibe_show_identity_correlation",
                    "same_site": "None",
                    "secure": True,
                    "http_only": True,
                    "path": "/auth/show-identity/callback",
                    "domain_attribute_allowed": False,
                    "single_use": True,
                    "expires_no_later_than": "signed_state.expires_at",
                }
                and callback["verified_email"] in callback["current_local_emails"]
                and callback["resolved_audience_revision"] == request["credential_binding"]["audience_revision"]
                and request["membership_rechecked"] is True
            )
        if check == "public_and_limited_admission":
            admissions = document["admission_rule"]["capability_issued_when"]
            cases = {case["id"]: case for case in document["cases"]}
            return (
                admissions
                == [
                    {
                        "availability": "active",
                        "access_mode": "public",
                        "verified_identity": "not_required",
                        "current_local_membership": "not_required",
                    },
                    {
                        "availability": "active",
                        "access_mode": "limited",
                        "verified_identity": "required",
                        "current_local_membership": "required",
                    },
                ]
                and cases["CONTAINMENT-TRUSTED-SHELL-PUBLIC-ANONYMOUS"]["outcome"] == "serve_shared"
                and cases["CONTAINMENT-TRUSTED-SHELL-CURRENT-MEMBER"]["outcome"] == "serve_shared"
                and cases["CONTAINMENT-PUBLIC-SIBLING-FETCH"]["outcome"] == "generic_deny_without_page_bytes"
            )
        if check == "credentialless_api_preflight":
            cors = document["credentialless_cors"]
            options = cors["options"]
            mutations = document["api_mutation_cases"]
            positive = [case for case in mutations if case["actor"] == "admitted_shared_document"]
            sibling = [case for case in mutations if case["actor"] == "public_sibling_page_code"]
            routes = document["browser_request_shapes"][1:]
            return (
                options["allowed_methods"] == ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
                and options["allowed_headers"] == ["Content-Type"]
                and options["access_control_allow_origin"] == "*"
                and options["access_control_allow_credentials"] is False
                and options["set_cookie_allowed"] is False
                and options["capability_source"] == "opaque_route_namespace_segment"
                and options["valid_capability_requires"]
                == [
                    "capability_integrity_and_entropy",
                    "current_instance_page_share_binding",
                    "current_audience_revision",
                    "live_namespace_and_document_handle",
                ]
                and options["admission_proof"] == "capability_minted_only_after_admission_rule"
                and options["ambient_identity_or_cookie_recheck"] is False
                and {case["method"] for case in positive} == {"POST", "PUT", "PATCH", "DELETE"}
                and all(case["outcome"] == "serve_shared_api" for case in positive)
                and len(sibling) == 1
                and sibling[0]["outcome"] == "fixed_sanitized_not_found"
                and all("/<capability>/" in shape["route"] for shape in routes)
                and document["share_browsing_credential"]["transport"]["header_allowed"] is False
            )
        if check == "shared_response_matrix":
            matrix = document["shared_proxy_policy"]["response_policy_matrix"]
            points = list(itertools.product(matrix["axes"]["audience_mode"], matrix["axes"]["surface"]))
            return all(
                len(
                    matches := [
                        rule
                        for rule in matrix["rules"]
                        if _matches(rule["audience_mode"], mode) and _matches(rule["surface"], surface)
                    ]
                )
                == 1
                and matches[0]["cache_control"] == "private, no-store"
                and matches[0]["vary"] == ("Cookie" if mode == "limited" else None)
                for mode, surface in points
            )
        if check == "canonical_email_storage":
            states = [fixture["show_access"] for fixture in document["fixtures"]]
            return all(state["emails"] == _canonical_emails(state["emails"]) for state in states)
        if check == "share_collision_atomic":
            boundary = _find_object(document, "boundary_cases", "BOUNDARY-SHARE-ID-TAKEN")["result"]
            contenders = document["concurrency_cases"][0]["contenders"]
            return (
                boundary["outcome"] == "share_id_taken"
                and boundary["effects"]["store_write"] is False
                and sum(item["store_write"] for item in contenders) == 1
                and {item["outcome"] for item in contenders} == {"applied", "share_id_taken"}
            )
        if check == "receipt_a_b_a":
            a, b, replay_a = document["receipt_sequences"][0]["steps"]
            return (
                a["mutation_id"] != b["mutation_id"]
                and replay_a["returned_mutation_id"] == a["mutation_id"]
                and replay_a["returned_audience_revision"] == a["returned_audience_revision"]
                and replay_a["current_audience_revision"] == b["current_audience_revision"]
                and replay_a["store_write"] is False
            )
        if check == "canonical_receipt_digest":
            vectors = document["receipt_digest_vectors"]
            by_digest = {vector["canonical_request_sha256"]: vector for vector in vectors}
            sequence = document["receipt_sequences"][0]["steps"]
            return (
                len(vectors) == len(by_digest) == 2
                and all(
                    _canonical_json(vector["normalized_request"]) == vector["canonical_json_utf8"]
                    and hashlib.sha256(vector["canonical_json_utf8"].encode("utf-8")).hexdigest()
                    == vector["canonical_request_sha256"]
                    for vector in vectors
                )
                and sequence[0]["canonical_request_sha256"] == sequence[2]["canonical_request_sha256"]
                and sequence[0]["canonical_request_sha256"] != sequence[1]["canonical_request_sha256"]
                and all(step["canonical_request_sha256"] in by_digest for step in sequence)
            )
        if check == "legacy_null_binding":
            cases = {case["id"]: case for case in document["cases"]}
            null_cases = [
                cases["LOCAL-LEGACY-PRIVATE-NO-BINDING"],
                cases["LOCAL-LEGACY-OFFLINE-NO-BINDING"],
            ]
            retained = [case for case in document["cases"] if case["source"]["share_id"] is not None]
            return all(case["result"]["share_binding"] is None for case in null_cases) and all(
                case["result"]["share_binding"] == {"share_id": case["source"]["share_id"]} for case in retained
            )
        if check == "private_hmr_authority":
            authority = document["avibe_private_hmr_monitor"]
            admission = authority["websocket_admission"]
            deadline = authority["durable_offline_deadline"]
            cases = {case["id"]: case for case in admission["cases"]}
            trace = deadline["worst_case_just_after_poll_trace_ms"]
            return (
                authority["owner"] == "PrivateHmrAuthority"
                and admission["origin_header_cardinality"] == "exactly_one"
                and admission["validation_order"] == "before_any_upstream_websocket_open"
                and all(
                    cases[case_id]["outcome"] == "reject_without_upstream_websocket"
                    for case_id in ("HMR-ORIGIN-MISSING", "HMR-ORIGIN-MULTIPLE", "HMR-ORIGIN-CROSS-SITE")
                )
                and admission["rejection"]["upstream_websocket_opened"] is False
                and deadline["poll_interval_max_seconds"] + deadline["post_detection_close_budget_seconds"]
                <= deadline["total_close_deadline_seconds"]
                and trace["all_sockets_closed_no_later_than"] - trace["durable_offline_commit"]
                == trace["elapsed_from_offline_commit"]
                and trace["elapsed_from_offline_commit"] <= 5000
            )
        if check == "settings_projection_excludes_receipts":
            invariant = document["x-avibe-projection-invariants"]
            return invariant["authoritative_aggregate_embedded"] is False and {
                "last_mutation",
                "canonical_request_sha256",
                "apply_receipt",
            } <= set(invariant["forbidden_fields"])
        if check == "sibling_isolation":
            isolation = document["cross_share_isolation"]
            cases = {case["id"]: case for case in document["cases"]}
            sibling = [case for case in cases.values() if case["actor"] == "public_sibling_page_code"]
            return (
                isolation["trusted_shell_current_member_can_load"] is True
                and all(value is False for key, value in isolation.items() if key.startswith("public_sibling_"))
                and {case["attempt"] for case in sibling}
                == {"fetch_limited_share", "frame_limited_share", "open_and_read_limited_share"}
                and all(case["outcome"] == "generic_deny_without_page_bytes" for case in sibling)
                and cases["CONTAINMENT-TRUSTED-SHELL-CURRENT-MEMBER"]["outcome"] == "serve_shared"
            )
        raise AssertionError(f"unknown semantic claim: {check}")
    raise AssertionError(f"unknown claim source: {kind}")


def _mutate_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if value is None:
        return "unexpected"
    return f"{value}__mutated"


def _assert_runtime_release_gate(release_gate: dict[str, Any]) -> None:
    if not release_gate["feature_advertisement_allowed"]:
        return
    assert release_gate["required_property_owners"] == {
        "runtime_context_isolation": "implemented",
        "opaque_shared_capture_admission": "implemented",
    }
    assert release_gate["smoke_test"] == "passed"
    assert release_gate["reviewed_runtime_pr"] is not None
    assert release_gate["manifest_source_policy"] == "exact_reviewed_sha_only_no_dynamic_main"
    assert (
        len(
            {
                release_gate["reviewed_runtime_sha"],
                release_gate["smoke_tested_runtime_sha"],
                release_gate["bundled_runtime_sha"],
            }
        )
        == 1
    )


def test_all_json_files_parse_and_all_schemas_are_valid() -> None:
    json_paths = sorted(CONTRACTS.rglob("*.json"))
    assert json_paths
    for path in json_paths:
        json.loads(path.read_text(encoding="utf-8"))

    schemas = _schemas()
    assert schemas
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    registry = _registry(schemas)
    for schema in schemas.values():
        Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


@pytest.mark.parametrize(("document", "schema"), sorted(DOCUMENT_SCHEMAS.items()))
def test_contract_documents_validate_against_their_schemas(document: str, schema: str) -> None:
    _validator(schema).validate(_load(document))


def test_local_show_access_state_is_closed_canonical_and_route_safe() -> None:
    schema = _load("show-access.schema.json")
    assert schema["$defs"]["access_mode"]["enum"] == ["private", "limited", "public"]
    assert schema["$defs"]["availability"]["enum"] == ["active", "offline"]
    assert set(schema["required"]) == {
        "schema_version",
        "page_id",
        "availability",
        "access_mode",
        "share_binding",
        "audience_revision",
        "emails",
        "last_mutation",
    }

    fixtures = _load("fixtures/show-access.json")["fixtures"]
    for fixture in fixtures:
        state = fixture["show_access"]
        _validate_show_access_invariants(state)
        assert state["emails"] == _canonical_emails(state["emails"])
        if state["access_mode"] == "limited":
            assert state["emails"] and state["share_binding"] is not None
        else:
            assert state["emails"] == []
        if state["access_mode"] == "public":
            assert state["share_binding"] is not None
        assert fixture["shared_route_admitted"] is (
            state["availability"] == "active" and state["access_mode"] in {"limited", "public"}
        )

    private_retained = _find_object({"fixtures": fixtures}, "fixtures", "STATE-ACTIVE-PRIVATE-RETAINED-BINDING")
    assert private_retained["show_access"]["share_binding"] == {"share_id": "stable-disabled"}
    assert private_retained["shared_route_admitted"] is False

    invalid = copy.deepcopy(private_retained["show_access"])
    invalid["access_mode"] = "limited"
    invalid["emails"] = []
    with pytest.raises(ValidationError):
        _validator("show-access.schema.json").validate(invalid)

    unsorted = copy.deepcopy(
        _find_object({"fixtures": fixtures}, "fixtures", "STATE-ACTIVE-LIMITED-LOCAL")["show_access"]
    )
    unsorted["emails"] = list(reversed(unsorted["emails"]))
    _validator("show-access.schema.json").validate(unsorted)
    with pytest.raises(ValueError, match="canonical"):
        _validate_show_access_invariants(unsorted)
    invariant = schema["x-avibe-invariants"]["canonical_email_set"]
    assert invariant["applies_before"] == [
        "request_digest",
        "equality_comparison",
        "persistence",
        "result_serialization",
    ]
    assert invariant["reject_noncanonical_persisted_or_result"] is True


def test_apply_invocation_allows_both_sharing_authorities_and_denies_early() -> None:
    contract = _load("apply-invocation.json")
    cases = {case["authority"]: case for case in contract["cases"]}
    assert contract["allowed_authorities"] == ["owner", "sharing_control"]
    for authority in contract["allowed_authorities"]:
        assert cases[authority]["outcome"] == "allow"
        assert cases[authority]["controller_called"] is True
    for authority in {"resource_viewer", "page_member_only", "anonymous"}:
        case = cases[authority]
        assert case["outcome"] == "deny"
        assert case["controller_called"] is case["store_read"] is case["store_write"] is False


def test_apply_fixtures_are_local_atomic_page_bound_and_idempotent() -> None:
    document = _load("fixtures/apply-mutations.json")
    message_validator = _validator("apply-mutation.schema.json")
    for fixture in document["fixtures"]:
        request = fixture["request"]
        result = fixture["result"]
        message_validator.validate(request)
        message_validator.validate(result)
        assert fixture["route_page_id"] == request["page_id"] == result["page_id"]
        assert fixture["external_service_calls"] == 0
        if result["message_type"] == "apply_result":
            source = fixture["source_show_access"]
            state = result["show_access"]
            assert state["page_id"] == result["page_id"]
            assert state["availability"] == source["availability"]
            assert "availability" not in request["target"]
            expected_emails = (
                _canonical_emails(request["target"]["emails"]) if request["target"]["access_mode"] == "limited" else []
            )
            assert state["emails"] == expected_emails
            changed = (
                source["access_mode"] != state["access_mode"]
                or source["share_binding"] != state["share_binding"]
                or source["emails"] != state["emails"]
            )
            expected_delta = 1 if changed and result["outcome"] == "applied" else 0
            if result["outcome"] != "idempotent_replay":
                assert state["audience_revision"] == source["audience_revision"] + expected_delta
            assert state["last_mutation"]["mutation_id"] == request["mutation_id"]

    for boundary in document["boundary_cases"]:
        message_validator.validate(boundary["request"])
        message_validator.validate(boundary["result"])
        effects = boundary["result"]["effects"]
        assert effects["store_write"] is effects["external_service_call"] is False

    mismatch = _find_object(document, "boundary_cases", "BOUNDARY-PAGE-MISMATCH")
    assert mismatch["route_page_id"] != mismatch["request"]["page_id"]
    assert mismatch["result"]["effects"]["controller_called"] is False

    replay = _find_object(document, "fixtures", "APPLY-IDEMPOTENT-REPLAY")
    assert replay["result"]["outcome"] == "idempotent_replay"
    assert replay["source_show_access"] == replay["result"]["show_access"]

    taken = _find_object(document, "boundary_cases", "BOUNDARY-SHARE-ID-TAKEN")
    assert taken["result"]["outcome"] == "share_id_taken"
    assert taken["result"]["effects"]["store_write"] is False

    collision = document["concurrency_cases"][0]
    assert collision["custom_share_id"] == "concurrent-custom-share"
    assert [item["outcome"] for item in collision["contenders"]] == ["applied", "share_id_taken"]
    assert [item["store_write"] for item in collision["contenders"]] == [True, False]


def test_apply_algebra_exhaustively_selects_one_transition_or_reject() -> None:
    algebra = _load("apply-transition-algebra.json")
    axes = algebra["axes"]
    evaluated = 0
    valid_transitions = 0
    for values in itertools.product(*(axes[name] for name in axes)):
        point = dict(zip(axes, values, strict=True))
        result = _evaluate_algebra(point)
        evaluated += 1
        assert result["decision"] in {
            "idempotent_replay",
            "mutation_conflict",
            "revision_conflict",
            "invalid_target",
            "share_id_taken",
            "local_transition",
        }
        assert result["availability"] == point["availability"]
        assert result["external_service_calls"] == 0
        if result["decision"] == "local_transition":
            valid_transitions += 1
            changed = (
                point["source_mode"] != point["target_mode"]
                or point["binding_relation"] == "different"
                or point["email_relation"] == "different"
            )
            assert result["outcome"] == ("applied" if changed else "no_change")
            assert result["audience_revision_delta"] == (1 if changed else 0)

    assert evaluated == 2 * 3 * 3 * 2 * 3 * 2 * 3 * 3 * 2 * 3
    assert valid_transitions > 0
    assert algebra["normalization"] == {
        "steps": [
            "trim_ascii_surrounding_whitespace",
            "lowercase",
            "deduplicate",
            "unicode_codepoint_lexicographic_sort",
        ],
        "provider_specific_alias_normalization": False,
        "canonical_set_comparison": "exact",
        "reject_noncanonical_persisted_or_result": True,
    }
    assert algebra["idempotency"] == {
        "store_owner": "controller_process.ShowAccessStore",
        "key_fields": ["page_id", "mutation_id"],
        "value_fields": ["canonical_request_sha256", "terminal_result"],
        "lookup_order": "before_expected_revision_cas",
        "same_payload_action": "return_stored_terminal_result",
        "different_payload_action": "reject_without_write",
        "survives_later_apply": True,
        "atomic_with_effective_or_no_change_apply": True,
        "exposed_by_owner_settings_read": False,
        "canonical_request_sha256": {
            "owner": "CanonicalApplyReceipt",
            "input": "fully_normalized_apply_request",
            "included_fields": [
                "schema_version",
                "message_type",
                "page_id",
                "mutation_id",
                "expected_audience_revision",
                "target",
            ],
            "excluded_context": ["route_authority", "actor_context"],
            "canonical_json": "RFC8785_JSON_Canonicalization_Scheme_recursive",
            "object_key_order": "lexicographic_recursive",
            "array_order": "preserved_after_field_normalization",
            "number_domain": "nonnegative_base10_integers_only",
            "text_encoding": "UTF-8",
            "digest": "SHA-256",
            "output_encoding": "lowercase_hex_64",
        },
        "retention": {
            "lifetime": "page_lifetime",
            "time_eviction_allowed": False,
            "count_eviction_allowed": False,
            "cascade_delete_with_page": True,
        },
    }

    rotate = {
        "availability": "active",
        "source_mode": "limited",
        "target_mode": "limited",
        "source_binding": "bound",
        "share_intent": "rotate",
        "binding_relation": "different",
        "share_candidate_status": "available",
        "email_relation": "same",
        "expected_revision": "current",
        "mutation_status": "new",
    }
    assert _evaluate_algebra(rotate) == {
        "decision": "local_transition",
        "availability": "active",
        "external_service_calls": 0,
        "binding_outcome": "rotated",
        "outcome": "applied",
        "audience_revision_delta": 1,
    }

    impossible_custom_without_source = {
        **rotate,
        "source_binding": "none",
        "share_intent": "custom",
        "binding_relation": "same",
    }
    assert _evaluate_algebra(impossible_custom_without_source)["decision"] == "invalid_target"
    impossible_private_to_limited_same_emails = {
        **rotate,
        "source_mode": "private",
        "target_mode": "limited",
        "email_relation": "same",
    }
    assert _evaluate_algebra(impossible_private_to_limited_same_emails)["decision"] == "invalid_target"

    taken_custom = {
        **rotate,
        "share_intent": "custom",
        "share_candidate_status": "owned_by_other_page",
    }
    assert _evaluate_algebra(taken_custom) == {
        "decision": "share_id_taken",
        "availability": "active",
        "external_service_calls": 0,
        "binding_outcome": "custom",
        "outcome": "share_id_taken",
        "store_write": False,
    }
    assert algebra["share_binding_allocator"] == {
        "owner": "controller_process.ShowAccessStore",
        "uniqueness_scope": "all_show_pages",
        "check_and_binding_write": "same_stable_writer_transaction",
        "same_page_existing_binding_allowed": True,
        "collision_outcome": "share_id_taken",
        "collision_store_write": False,
        "concurrent_collision_winners": 1,
    }


def test_local_removal_and_crash_traces_need_no_backend_or_reconciliation() -> None:
    sequences = {item["id"]: item for item in _load("fixtures/apply-mutations.json")["sequences"]}
    removal = sequences["SEQUENCE-LOCAL-REMOVAL-NEXT-REQUEST"]
    assert "bob@example.com" in removal["steps"][0]["state"]["emails"]
    assert "bob@example.com" not in removal["steps"][1]["state"]["emails"]
    assert removal["steps"][2]["request_outcome"] == "deny"
    assert all(step["backend_available"] is False for step in removal["steps"])
    assert all(step["active_guest_tab_closed"] is False for step in removal["steps"])

    crash = sequences["SEQUENCE-LOCAL-CRASH-IDEMPOTENT-RETRY"]
    before, committed, replayed = [step["state"] for step in crash["steps"]]
    assert (before["audience_revision"], before["emails"]) == (70, ["old@example.com"])
    assert (committed["audience_revision"], committed["emails"]) == (71, ["new@example.com"])
    assert committed == replayed
    assert crash["assertions"] == [
        "aggregate_is_old_or_complete_new_never_partial",
        "same_mutation_retry_returns_original_terminal_result",
        "no_external_coordinator_or_reconciliation",
    ]

    receipt = _load("fixtures/apply-mutations.json")["receipt_sequences"][0]
    apply_a, apply_b, replay_a = receipt["steps"]
    assert (apply_a["mutation_id"], apply_a["returned_audience_revision"]) == (
        "mut_receipt_sequence_a_0001",
        81,
    )
    assert (apply_b["mutation_id"], apply_b["current_audience_revision"]) == (
        "mut_receipt_sequence_b_0001",
        82,
    )
    assert replay_a["outcome"] == "idempotent_replay"
    assert replay_a["current_audience_revision"] == 82
    assert replay_a["returned_audience_revision"] == 81
    assert replay_a["returned_mutation_id"] == apply_a["mutation_id"]
    assert replay_a["store_write"] is False
    assert apply_a["canonical_request_sha256"] == replay_a["canonical_request_sha256"]
    assert apply_a["canonical_request_sha256"] != apply_b["canonical_request_sha256"]

    vectors = _load("fixtures/apply-mutations.json")["receipt_digest_vectors"]
    assert {vector["canonical_request_sha256"] for vector in vectors} == {
        "fce9ed95782f988c893c0cd1e5ac6f5dc403e7db4212882089e46864c29e9919",
        "997f1adf1dfb7eb727bf6e7d581ffedf81ba4f1dac166141d595358058775a0b",
    }
    for vector in vectors:
        canonical = _canonical_json(vector["normalized_request"])
        assert canonical == vector["canonical_json_utf8"]
        assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == vector["canonical_request_sha256"]


def test_local_legacy_sqlite_mapping_is_deterministic_and_fail_closed() -> None:
    contract = _load("local-legacy-mapping.json")
    assert contract["scope"] == "local_sqlite_schema_migration_only"
    assert contract["initial_audience_revision"] == 0
    assert contract["email_initialization"] == "empty"
    assert contract["hosted_import_allowed"] is contract["dual_write_allowed"] is False
    cases = {case["id"]: case for case in contract["cases"]}
    assert len(cases) == len(contract["cases"]) == 5
    expected = [
        ("LOCAL-LEGACY-PRIVATE", "active", "private", False),
        ("LOCAL-LEGACY-PUBLIC", "active", "public", True),
        ("LOCAL-LEGACY-OFFLINE-FAIL-CLOSED", "offline", "private", False),
        ("LOCAL-LEGACY-PRIVATE-NO-BINDING", "active", "private", False),
        ("LOCAL-LEGACY-OFFLINE-NO-BINDING", "offline", "private", False),
    ]
    for case_id, availability, access_mode, admitted in expected:
        case = cases[case_id]
        result = case["result"]
        assert result["page_id"] == case["source"]["page_id"]
        assert (result["availability"], result["access_mode"]) == (availability, access_mode)
        expected_binding = {"share_id": case["source"]["share_id"]} if case["source"]["share_id"] is not None else None
        assert result["share_binding"] == expected_binding
        assert result["audience_revision"] == 0
        assert result["emails"] == []
        assert result["last_mutation"] is None
        assert case["shared_route_admitted"] is admitted


def test_owner_settings_reads_exact_emails_only_from_local_authorized_state() -> None:
    settings = _load("fixtures/owner-settings.json")
    assert {case["authority"] for case in settings["allowed_cases"]} == {"owner", "sharing_control"}
    for case in settings["allowed_cases"]:
        result = case["result"]
        assert case["route_page_id"] == case["request"]["page_id"] == result["page_id"]
        assert result["page_id"] == result["show_access"]["page_id"]
        assert result["storage_source"] == "local_authoritative_transactional_store"
        assert result["cache_control"] == "private, no-store"
        assert result["show_access"]["emails"]
        assert "last_mutation" not in result["show_access"]
    invalid_projection = copy.deepcopy(settings["allowed_cases"][0]["result"])
    invalid_projection["show_access"]["last_mutation"] = None
    with pytest.raises(ValidationError):
        _validator("owner-settings.schema.json").validate(invalid_projection)
    for case in settings["denied_cases"]:
        assert case["authority"] in {"resource_viewer", "page_member_only", "anonymous"}
        assert case["result"]["store_read"] is case["result"]["settings_returned"] is False


def test_identity_assertion_is_instance_bound_identity_only_and_membership_is_fresh() -> None:
    contract = _load("identity-auth.json")
    assertion = contract["backend_assertion"]
    assert assertion["format"] == "short_lived_signed_identity_assertion"
    assert assertion["authorize_endpoint"] == {
        "method": "GET",
        "path_template": "/api/v1/instances/{instanceId}/show-identity/authorize",
    }
    assert assertion["request_inputs"]["required"] == ["state", "nonce", "redirect_uri"]
    assert assertion["request_inputs"]["additional_allowed"] is False
    assert assertion["delivery"] == {
        "response_mode": "form_post",
        "fixed_callback_path": "/auth/show-identity/callback",
        "assertion_parameter": "assertion",
        "state_parameter": "state",
        "form_fields": ["state", "assertion"],
        "additional_form_fields_allowed": False,
        "forbidden_assertion_locations": ["url_query", "url_fragment", "browser_history", "referrer"],
    }
    assert assertion["required_signed_claims"] == [
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
    assert assertion["audience_derivation"] == "avibe-show-identity:<oauthClientId>"
    assert assertion["ttl_seconds"] == 300
    assert assertion["maximum_lifetime_seconds"] == 600
    assert assertion["verifier_clock_skew_seconds"] == 60
    assert assertion["signing_keys"]["minimum_overlap_seconds"] >= (
        assertion["maximum_lifetime_seconds"] + assertion["verifier_clock_skew_seconds"]
    )
    assert assertion["verified_email_source"] == {
        "lookup": "fresh_backend_identity_provider_or_user_lookup",
        "explicit_verification_required": True,
        "browser_input_allowed": False,
        "stale_cookie_field_allowed": False,
    }
    assert assertion["jti_unique"] is True
    assert {item["code"] for item in assertion["terminal_errors"]} == {
        "identity_not_verified",
        "identity_unavailable",
    }
    assert all(item["cache_control"] == "no-store" for item in assertion["terminal_errors"])
    assert all(item["assertion_returned"] is False for item in assertion["terminal_errors"])
    assert {"page_id", "share_id", "instance_role", "instance_access_source", "show_page_email"} <= set(
        assertion["forbidden_claims"]
    )
    assert assertion["instance_access_context_created"] is False
    assert assertion["instance_role_derived"] is False
    assert assertion["page_membership_decided"] is False
    assert contract["local_handshake"]["membership_lookup"] == (
        "fresh_local_page_email_lookup_on_every_limited_request"
    )
    assert contract["local_handshake"]["identity_session_is_authorization"] is False
    assert contract["local_handshake"]["removed_member_active_tab_policy"] == ("no_active_closure_future_requests_only")
    assert contract["local_handshake"]["correlation_cookie"] == {
        "name": "avibe_show_identity_correlation",
        "same_site": "None",
        "secure": True,
        "http_only": True,
        "path": "/auth/show-identity/callback",
        "domain_attribute_allowed": False,
        "single_use": True,
        "expires_no_later_than": "signed_state.expires_at",
    }
    assert contract["local_handshake"]["reference_harness"] == {
        "scenario_id": "AUTH-SETUP-404",
        "actual_http_boundary": "POST /auth/show-identity/callback",
        "content_type": "application/x-www-form-urlencoded",
        "cross_site_source": "https://avibe.bot",
        "active_custom_callback_origin": "https://show.example.test",
        "production_conformance": "future_avibe_and_backend_implementation_lanes",
    }

    cases = {case["id"]: case for case in contract["cases"]}
    assert cases["IDENTITY-LISTED-CURRENT"]["outcome"] == "serve_shared"
    assert cases["IDENTITY-REMOVED-AFTER-SESSION"]["outcome"] == "generic_deny"
    assert cases["IDENTITY-CROSS-INSTANCE"]["outcome"] == "reject_identity_assertion"
    for case in cases.values():
        assert all(
            case[field] is False
            for field in ("instance_access", "canonical_show_access", "hmr", "annotations", "agents")
        )

    closed_loop = contract["closed_loop_scenario"]
    assert closed_loop["callback"]["assertion_format"] == "short_lived_signed_identity_assertion"
    assert closed_loop["callback"]["http_method"] == "POST"
    assert closed_loop["callback"]["delivery"] == "form_post"
    assert closed_loop["callback"]["callback_url"] == "https://show.example.test/auth/show-identity/callback"
    assert closed_loop["start"]["instance_id"] == closed_loop["callback"]["instance_id"]
    assert closed_loop["start"]["nonce"] == closed_loop["callback"]["nonce"]
    assert closed_loop["start"]["correlation_cookie"] == closed_loop["callback"]["correlation_cookie"]
    assert (
        hashlib.sha256(closed_loop["start"]["correlation_cookie"].encode("utf-8")).hexdigest()
        == (closed_loop["start"]["correlation_cookie_sha256"])
    )
    assert closed_loop["start"]["correlation_cookie_expires_at"] <= closed_loop["start"]["signed_state_expires_at"]
    assert closed_loop["start"]["page_id"] == closed_loop["callback"]["resolved_page_id"]
    assert closed_loop["start"]["share_id"] == closed_loop["callback"]["resolved_share_id"]
    assert closed_loop["callback"]["verified_email"] in closed_loop["callback"]["current_local_emails"]
    assert closed_loop["protected_request"]["membership_rechecked"] is True
    assert {case["mutation"] for case in closed_loop["negative_callbacks"]} == {
        "reuse_consumed_nonce",
        "replace_assertion_instance_id",
        "replace_signed_return_share",
        "replace_correlation_cookie",
        "add_callback_query",
        "add_callback_fragment",
        "place_assertion_in_browser_history",
        "place_assertion_in_referrer",
        "replace_callback_hostname",
        "add_page_authorization_claim",
        "identity_not_verified",
        "identity_unavailable",
    }


def test_shared_browser_containment_denies_sibling_code_and_binds_protected_requests() -> None:
    contract = _load("shared-browser-containment.json")
    shell = contract["trusted_shell"]
    assert contract["canonical_public_url"] == "/p/<share_id>/"
    assert shell["arbitrary_page_code_location"] == "sandboxed_opaque_origin_iframe"
    assert shell["iframe_sandbox_tokens"] == ["allow-scripts"]
    assert shell["allow_same_origin"] is False
    assert all(value is False for value in shell["page_code_access"].values())

    admission = contract["admission_rule"]
    assert admission["authorization_precedes_capture"] is True
    assert admission["capability_issued_when"] == [
        {
            "availability": "active",
            "access_mode": "public",
            "verified_identity": "not_required",
            "current_local_membership": "not_required",
        },
        {
            "availability": "active",
            "access_mode": "limited",
            "verified_identity": "required",
            "current_local_membership": "required",
        },
    ]

    credential = contract["share_browsing_credential"]
    assert credential["binding_fields"] == [
        "instance_id",
        "page_id",
        "share_id",
        "audience_revision",
        "namespace_handle",
        "document_handle",
    ]
    assert credential["entropy_bits"] == 256
    assert credential["derived_from_binding_or_path"] is False
    assert credential["required_surfaces"] == [
        "document",
        "module",
        "css",
        "raw",
        "worker",
        "spa_fallback",
        "api_handler",
    ]
    assert credential["transport"] == {
        "location": "opaque_protected_route_namespace_segment",
        "header_allowed": False,
        "query_allowed": False,
        "fragment_allowed": False,
        "ambient_cookie_allowed": False,
        "request_credentials": "omit",
        "referrer_policy": "no-referrer",
    }
    assert credential["browser_cookie"] is credential["instance_access_context"] is False

    capture = contract["runtime_capture_admission"]
    assert capture["authorization_precedes_capture"] is True
    assert capture["browser_supplied_context_allowed"] is False
    assert capture["browser_visible_session_or_path_fields"] is False
    assert capture["result_fields"] == ["namespace_handle", "document_handle", "expires_at"]

    for shape in contract["browser_request_shapes"]:
        assert {"session_id", "workspace_path", "source_path", "runtime_context"} <= set(shape["forbidden_inputs"])
    protected_shapes = contract["browser_request_shapes"][1:]
    assert all("capability" in shape["required_inputs"] for shape in protected_shapes)
    assert all("/<capability>/" in shape["route"] for shape in protected_shapes)

    cors = contract["credentialless_cors"]
    assert cors["access_control_allow_origin"] == "*"
    assert cors["access_control_allow_credentials"] is False
    assert cors["set_cookie_allowed"] is False
    assert cors["request_credentials"] == "omit"
    assert cors["referrer_policy"] == "no-referrer"
    assert cors["options"] == {
        "allowed_methods": ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
        "allowed_headers": ["Content-Type"],
        "access_control_allow_origin": "*",
        "access_control_allow_credentials": False,
        "set_cookie_allowed": False,
        "cache_control": "private, no-store",
        "capability_source": "opaque_route_namespace_segment",
        "valid_capability_requires": [
            "capability_integrity_and_entropy",
            "current_instance_page_share_binding",
            "current_audience_revision",
            "live_namespace_and_document_handle",
        ],
        "admission_proof": "capability_minted_only_after_admission_rule",
        "ambient_identity_or_cookie_recheck": False,
        "valid_capability_outcome": "credentialless_preflight_allowed",
        "invalid_capability_outcome": "fixed_sanitized_not_found",
    }
    api_mutations = contract["api_mutation_cases"]
    positive_mutations = [case for case in api_mutations if case["actor"] == "admitted_shared_document"]
    assert {case["method"] for case in positive_mutations} == {"POST", "PUT", "PATCH", "DELETE"}
    assert all(case["content_type"] == "application/json" for case in positive_mutations)
    assert all(case["outcome"] == "serve_shared_api" for case in positive_mutations)
    sibling_mutation = [case for case in api_mutations if case["actor"] == "public_sibling_page_code"]
    assert len(sibling_mutation) == 1
    assert sibling_mutation[0]["outcome"] == "fixed_sanitized_not_found"
    assert contract["vendor_assets"] == {
        "representation": "namespace_scoped",
        "global_hashed_public_assets_allowed": False,
        "capability_required": True,
        "cross_namespace_reuse": False,
    }
    isolation = contract["cross_share_isolation"]
    assert isolation["trusted_shell_current_member_can_load"] is True
    assert all(value is False for key, value in isolation.items() if key.startswith("public_sibling_"))
    assert {case["kind"] for case in contract["dependency_cases"]} == {
        "history_navigation",
        "worker_import",
        "api_request",
        "css_import",
        "raw_import",
    }
    assert all(case["capability_required"] and case["same_namespace"] for case in contract["dependency_cases"])
    assert all(not case["page_bytes"] and not case["path_bytes"] for case in contract["failure_responses"])
    assert contract["residual_evidence"] == {
        "browser_sandbox_cors": "future_local_incus_real_browser",
        "contract_proof_is_browser_conformance": False,
    }

    cases = {case["id"]: case for case in contract["cases"]}
    assert cases["CONTAINMENT-TRUSTED-SHELL-CURRENT-MEMBER"]["outcome"] == "serve_shared"
    public = cases["CONTAINMENT-TRUSTED-SHELL-PUBLIC-ANONYMOUS"]
    assert (public["access_mode"], public["identity"], public["membership"], public["outcome"]) == (
        "public",
        "anonymous",
        "not_required",
        "serve_shared",
    )
    attacks = {
        "fetch_limited_share",
        "frame_limited_share",
        "open_and_read_limited_share",
    }
    sibling_cases = [case for case in cases.values() if case["actor"] == "public_sibling_page_code"]
    assert {case["attempt"] for case in sibling_cases} == attacks
    assert all(case["credential"] == "unavailable" for case in sibling_cases)
    assert all(case["outcome"] == "generic_deny_without_page_bytes" for case in sibling_cases)
    assert cases["CONTAINMENT-STALE-REVISION-PROOF"]["outcome"] == "generic_deny_without_page_bytes"

    matrix_precondition = _load("capability-matrix.json")["limited_shared_admission_precondition"]
    assert matrix_precondition["contract"] == "shared-browser-containment.json"
    assert matrix_precondition["matrix_input_stage"] == "after_valid_share_bound_request_proof"
    assert matrix_precondition["raw_browser_requests_evaluated_directly"] is False


def test_capability_matrix_is_closed_and_resource_membership_axes_are_orthogonal() -> None:
    matrix = _load("capability-matrix.json")
    axes = matrix["axes"]
    expanded = 0
    for values in itertools.product(*(axes[name] for name in axes)):
        point = dict(zip(axes, values, strict=True))
        decision = _matrix_decision(point)
        expanded += 1
        if point["availability"] == "offline":
            assert decision["read_decision"] == "deny"
        if point["surface"] == "/p":
            assert decision["show_editor_capability"] is False
            assert decision["hmr"] is decision["annotations"] is False
            assert decision["runtime_context"] in {None, "shared"}
            if decision["top_level_redirect"]:
                assert point["availability"] == "active"
                assert point["access_mode"] in {"limited", "public"}
                assert point["request_kind"] == "trusted_top_level_navigation"
                assert point["resource_authority"] in {"viewer", "editor"}
        if decision["hmr"]:
            assert point["surface"] == "/show"
            assert point["resource_authority"] == "editor"
            assert decision["show_editor_capability"] is True

    assert expanded == 2 * 2 * 3 * 2 * 3 * 2 * 2 * 3
    authority = matrix["authority_axes"]
    assert authority["independent"] is True
    assert authority["page_membership_creates_instance_access_context"] is False
    assert authority["page_membership_creates_instance_role"] is False
    assert authority["editor_capability_source"] == "resource_editor_only"

    common = {
        "availability": "active",
        "access_mode": "limited",
        "identity_state": "verified",
        "page_membership": "current",
        "keyed_context": "supported",
    }
    dual_p = _matrix_decision(
        {
            "surface": "/p",
            "request_kind": "trusted_top_level_navigation",
            "resource_authority": "viewer",
            **common,
        }
    )
    dual_show = _matrix_decision(
        {
            "surface": "/show",
            "request_kind": "other",
            "resource_authority": "viewer",
            **common,
        }
    )
    assert dual_p["read_decision"] == "redirect_private"
    assert dual_show["read_decision"] == "serve_private"
    assert dual_show["hmr"] is dual_show["annotations"] is False

    listed_only_show = _matrix_decision(
        {
            "surface": "/show",
            "request_kind": "other",
            "resource_authority": "none",
            **common,
        }
    )
    assert listed_only_show["read_decision"] == "deny"


def test_direct_retirement_has_no_migration_bridge_or_hosted_protocol_artifacts() -> None:
    contract = _load("retirement.json")
    assert contract["production_exact_email_data_exists"] is False
    assert contract["strategy"] == "direct_retirement"
    assert contract["migration_allowed"] is False
    assert contract["compatibility_bridge_allowed"] is False
    assert contract["dual_write_allowed"] is False
    assert contract["release_dependency"] == {
        "application_deployment": "stop_all_legacy_table_and_show_page_id_column_selects_and_writes",
        "ddl_deployment": "drop_legacy_column_and_table",
        "ddl_requires_application_deployment_complete": True,
        "data_bridge_backfill_or_import": False,
    }
    assert {item["future_action"] for item in contract["retired_surfaces"]} == {
        "delete_storage",
        "delete_endpoints",
        "delete_clients",
        "delete_authorization_source",
    }
    for removed in (
        "fixtures/hosted-operations.json",
        "fixtures/migrations.json",
        "hosted-operation.schema.json",
        "hosted-fixture.schema.json",
        "migration-fixture.schema.json",
        "rollout.json",
        "rollout.schema.json",
    ):
        assert not (CONTRACTS / removed).exists()

    forbidden_keys = {
        "grant_revision",
        "grant_commitment",
        "share_admission_gate",
        "coordinator",
        "hosted_current_grant",
        "operation_id",
    }
    for path in sorted(CONTRACTS.rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        stack = [document]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                assert forbidden_keys.isdisjoint(value), path
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)


def test_runtime_safety_owners_and_repeated_edit_trace_are_executable() -> None:
    contract = _load("runtime-context.json")
    protocol = contract["protocol"]
    assert protocol == {
        "version": SHOW_RUNTIME_PROTOCOL_VERSION,
        "protocol_header": SHOW_RUNTIME_PROTOCOL_HEADER,
        "context_header": SHOW_RUNTIME_CONTEXT_HEADER,
        "keyed_context_feature": SHOW_RUNTIME_CONTEXT_KEY_FEATURE,
        "contexts": [context.value for context in ShowRuntimeContext],
    }
    assert [case["outcome"] for case in contract["negotiation_cases"]] == [
        capability.value for capability in ShowRuntimeContextCapability
    ]
    assert all(case["redirect_eligible"] is True for case in contract["negotiation_cases"])

    boundary = contract["loopback_request_boundary"]
    assert boundary["browser_supplied_headers_removed"] == [
        SHOW_RUNTIME_PROTOCOL_HEADER,
        SHOW_RUNTIME_CONTEXT_HEADER,
    ]
    assert boundary["server_owned_envelope_count"] == 1
    assert set(boundary["applies_to_operations"]) == set(contract["app_graph_operations"])

    ownership = contract["runtime_graph_ownership"]
    assert ownership["graph_key"] == ["session_id", "context"]
    assert ownership["shared_activity_closes_private_hmr"] is False
    capture = contract["shared_capture_admission"]
    assert capture["request_source"] == "trusted_avibe_server_envelope_only"
    assert capture["authorization_precedes_request"] is True
    assert capture["browser_request_allowed"] is capture["browser_supplied_context_allowed"] is False
    assert capture["result_reveals_session_or_path"] is False
    edits = contract["operation_context_eligibility"]["ordinary_editor_file_edit"]
    assert edits == {
        "private_hmr_identity": "stable",
        "shared_graph_create": False,
        "shared_graph_rebase": False,
        "shared_background_build": False,
        "edit_count_input": "arbitrary_nonnegative_integer",
        "resource_bound": "resource_governance",
    }
    eligibility = contract["operation_context_eligibility"]
    assert eligibility["implicit_shared_work_from_private_edit"] is False
    assert eligibility["explicit_shared_prewarm_operations"] == [
        "startup_reconciliation_prewarm",
        "show_update_prewarm",
    ]
    assert set(eligibility["shared_prewarm_requires"]) == {
        "explicit_operation",
        "shared_context_envelope",
        "protocol1_validation",
        "resource_budget_admission",
    }

    namespace = contract["shared_namespace_confinement"]
    assert namespace["recursive_dependency_closure"]["dependency_kinds"] == [
        "tsx",
        "css",
        "raw_loader",
        "worker",
    ]
    assert namespace["handle"]["source_path_reopen"] == "forbidden"
    assert namespace["handle"]["new_admission_after_unsafe_path_swap"] == "deny_before_allocation"
    assert contract["namespace_lifetime"]["absolute_lifetime_renewable"] is False
    assert contract["resource_governance"]["per_session_bounds"] is True
    assert contract["resource_governance"]["shared_may_consume_private_reserve"] is False

    response_policy = contract["shared_proxy_policy"]["response_policy_matrix"]
    response_points = list(
        itertools.product(
            response_policy["axes"]["audience_mode"],
            response_policy["axes"]["surface"],
        )
    )
    assert response_policy["axes"]["surface"] == [
        "shell",
        "entry",
        "document",
        "module",
        "css",
        "raw",
        "worker",
        "spa_fallback",
        "api_handler",
        "redirect",
        "error",
    ]
    assert len(response_points) == 2 * len(response_policy["axes"]["surface"])
    for audience_mode, surface in response_points:
        matches = [
            rule
            for rule in response_policy["rules"]
            if _matches(rule["audience_mode"], audience_mode) and _matches(rule["surface"], surface)
        ]
        assert len(matches) == 1, (audience_mode, surface)
        assert matches[0]["cache_control"] == "private, no-store"
        assert matches[0]["vary"] == ("Cookie" if audience_mode == "limited" else None)

    redirect = contract["route_invariants"]["resource_redirect"]
    assert redirect["requires"] == [
        "active_share_binding",
        "resource_viewer_or_editor_authority",
        "trusted_fetch_metadata",
    ]
    assert redirect["preserve_route_suffix"] is redirect["preserve_query"] is True

    hmr_authority = contract["avibe_private_hmr_monitor"]
    assert hmr_authority["owner"] == "PrivateHmrAuthority"
    websocket = hmr_authority["websocket_admission"]
    assert websocket["origin_allowlist"] == {
        "derived_from": ["https://<active_instance_hostname>", "https://<active_custom_hostname>"],
        "fact_source": "server_owned_active_instance_and_custom_hostname_configuration",
        "wildcards_allowed": False,
        "suffix_matching_allowed": False,
        "forwarded_or_browser_host_inputs_allowed": False,
    }
    assert websocket["origin_header_cardinality"] == "exactly_one"
    assert websocket["validation_order"] == "before_any_upstream_websocket_open"
    assert websocket["rejection"] == {
        "outcome": "reject_without_upstream_websocket",
        "upstream_websocket_opened": False,
    }
    origin_cases = {case["id"]: case for case in websocket["cases"]}
    assert origin_cases["HMR-ORIGIN-TRUSTED-INSTANCE"]["outcome"] == "open_after_editor_authorization"
    for case_id in ("HMR-ORIGIN-MISSING", "HMR-ORIGIN-MULTIPLE", "HMR-ORIGIN-CROSS-SITE"):
        assert origin_cases[case_id]["outcome"] == "reject_without_upstream_websocket"

    deadline = hmr_authority["durable_offline_deadline"]
    assert deadline["measured_from"] == "durable_offline_transaction_commit"
    assert deadline["poll_interval_max_seconds"] + deadline["post_detection_close_budget_seconds"] <= 5
    assert deadline["total_close_deadline_seconds"] == 5
    trace = deadline["worst_case_just_after_poll_trace_ms"]
    assert trace["all_sockets_closed_no_later_than"] - trace["durable_offline_commit"] == 4999
    assert trace["elapsed_from_offline_commit"] <= 5000


def test_runtime_release_advertisement_is_pinned_to_one_reviewed_smoked_sha() -> None:
    contract = _load("runtime-context.json")
    release_gate = contract["release_gate"]
    assert release_gate["reviewed_runtime_sha"] == "ee3b0b490ad8b4afafb59cf37e2d57a20325208a"
    assert release_gate["smoke_tested_runtime_sha"] is None
    assert release_gate["bundled_runtime_sha"] is None
    assert release_gate["feature_advertisement_allowed"] is False
    _assert_runtime_release_gate(release_gate)

    invalid = copy.deepcopy(contract)
    invalid["release_gate"]["feature_advertisement_allowed"] = True
    with pytest.raises(ValidationError):
        _validator("runtime-context.schema.json").validate(invalid)

    eligible = copy.deepcopy(contract)
    candidate_sha = "9" * 40
    eligible["release_gate"].update(
        {
            "reviewed_runtime_sha": candidate_sha,
            "reviewed_runtime_pr": "https://github.com/avibe-bot/vibe-show-runtime/pull/999",
            "smoke_tested_runtime_sha": candidate_sha,
            "bundled_runtime_sha": candidate_sha,
            "required_property_owners": {
                "runtime_context_isolation": "implemented",
                "opaque_shared_capture_admission": "implemented",
            },
            "smoke_test": "passed",
            "feature_advertisement_allowed": True,
        }
    )
    _validator("runtime-context.schema.json").validate(eligible)
    _assert_runtime_release_gate(eligible["release_gate"])
    for field in ("smoke_tested_runtime_sha", "bundled_runtime_sha"):
        mismatched = copy.deepcopy(eligible)
        mismatched["release_gate"][field] = "8" * 40
        with pytest.raises(AssertionError):
            _assert_runtime_release_gate(mismatched["release_gate"])


def test_mirror_registry_names_exact_local_identity_retirement_and_runtime_boundaries() -> None:
    registry = _load("mirror-registry.json")
    assert {item["id"] for item in registry["repositories"]} == {
        "avibe",
        "avibe-backend",
        "vibe-show-runtime",
    }
    interfaces = {item["id"]: item for item in registry["interfaces"]}
    assert set(interfaces) == {f"C{index:02d}" for index in range(1, 20)}
    assert len(interfaces) == len(registry["interfaces"])
    assert registry["process_ownership"] == {
        "stable_writer_lease": "ShowAccessService.stable_writer_lease",
        "serialization_owner": "controller_process.ShowAccessService",
        "store_write_owner": "controller_process.ShowAccessStore.replace_transactionally",
        "apply_receipt_store": "controller_process.ShowAccessStore keyed by page_id plus mutation_id",
        "apply_receipt_retention": "page_lifetime with page cascade and no time or count eviction",
        "canonical_email_set_owner": "controller_process.ShowAccessCanonicalizer",
        "share_binding_allocator": "controller_process.ShowAccessStore under stable_writer_lease",
        "settings_projection_owner": "controller_process.ShowAccessService.project_owner_settings",
        "ui_process_coordinator_allowed": False,
        "atomic_fields": [
            "access_mode",
            "share_binding",
            "emails",
            "audience_revision",
            "last_mutation",
            "apply_receipt",
        ],
        "serialized_operations": ["apply"],
    }
    assert interfaces["C02"]["delivery"]["authentication"] == ("owner_or_sharing_control_resource_authority")
    assert interfaces["C03"]["delivery"]["mechanism"] == "internal_socket_json"
    assert "stable_writer_lease" in interfaces["C03"]["delivery"]["serialization_owner"]
    assert interfaces["C08"]["signature"]["covered_fields"] == [
        "iss",
        "aud",
        "sub",
        "iat",
        "exp",
        "jti",
        "nonce",
        "instance_id",
        "verified_email",
        "state",
        "assertion",
    ]
    assert interfaces["C09"]["delivery"]["authentication"] == "server_owned_orthogonal_facts"
    assert interfaces["C13"]["signature"]["covered_fields"] == [
        "runtime_graph_ownership",
        "operation_context_eligibility",
        "shared_namespace_confinement",
        "namespace_lifetime",
        "resource_governance",
        "shared_proxy_policy",
    ]
    assert interfaces["C15"]["signature"]["schema"] == "retirement.schema.json"
    assert interfaces["C16"]["delivery"]["mechanism"] == "reviewed_release_manifest"
    assert interfaces["C17"]["signature"]["schema"] == ("shared-browser-containment.json#/runtime_capture_admission")
    assert interfaces["C17"]["delivery"]["mechanism"] == "loopback_internal_capture_protocol"
    assert interfaces["C18"]["signature"]["schema"] == "shared-browser-containment.schema.json"
    assert interfaces["C18"]["delivery"]["authentication"] == (
        "local_share_browsing_credential_after_public_or_current_member_admission"
    )
    assert interfaces["C19"]["signature"]["schema"] == "local-legacy-mapping.schema.json"
    assert interfaces["C04"]["delivery"]["serialization_owner"].startswith("CanonicalApplyReceipt")
    assert interfaces["C07"]["delivery"]["serialization_owner"] == "avibe.ExecutableIdentityHandshake"
    assert interfaces["C14"]["delivery"]["serialization_owner"] == "avibe.PrivateHmrAuthority"
    for interface in interfaces.values():
        assert interface["delivery"]["authentication"]
        assert interface["delivery"]["serialization_owner"]
        for endpoint in [interface["producer"], *interface["consumers"]]:
            assert not endpoint["path"].startswith("/")


def test_design_blob_and_every_scenario_clause_have_sensitive_executable_claims() -> None:
    document = _load("scenario-bindings.json")
    registry = _load("mirror-registry.json")
    design_bytes = DESIGN.read_bytes()
    git_blob = b"blob " + str(len(design_bytes)).encode("ascii") + b"\0" + design_bytes
    blob_sha = hashlib.sha1(git_blob, usedforsecurity=False).hexdigest()
    assert document["design_sha"] == document["design_blob_sha"] == blob_sha
    assert registry["authority"]["source_ref_kind"] == "git_blob"
    assert registry["authority"]["source_sha"] == registry["authority"]["source_design_blob_sha"] == blob_sha
    assert registry["authority"]["source_issue"] == "https://github.com/avibe-bot/avibe/issues/1498"

    scenario_rows: dict[str, str] = {}
    pattern = re.compile(r"^\| `(SHOW-LIVE-[0-9]{3})` \| .* \| (.*) \|$")
    for line in design_bytes.decode("utf-8").splitlines():
        if match := pattern.match(line):
            scenario_rows[match.group(1)] = match.group(2)
    expected_ids = {f"SHOW-LIVE-{index:03d}" for index in range(1, 40)}
    assert set(scenario_rows) == expected_ids

    claims = {claim["id"]: claim for claim in document["claims"]}
    assert len(claims) == len(document["claims"])
    bindings = {binding["scenario_id"]: binding for binding in document["bindings"]}
    assert set(bindings) == expected_ids
    used_claims: set[str] = set()
    for scenario_id, binding in bindings.items():
        assert binding["expected_evidence"] == scenario_rows[scenario_id]
        assert "; ".join(clause["text"] for clause in binding["clauses"]) == binding["expected_evidence"]
        for clause in binding["clauses"]:
            assert clause["claim_ids"]
            for claim_id in clause["claim_ids"]:
                used_claims.add(claim_id)
                claim = claims[claim_id]
                actual = _evaluate_claim(claim)
                assert isinstance(actual, (str, bool, int)) or actual is None
                assert actual == claim["expected"], claim_id
                mutated_claim = copy.deepcopy(claim)
                mutated_claim["expected"] = _mutate_scalar(claim["expected"])
                assert _evaluate_claim(mutated_claim) != mutated_claim["expected"], claim_id
    assert used_claims == set(claims)


def test_all_scenario_references_use_the_closed_catalog() -> None:
    expected_ids = {f"SHOW-LIVE-{index:03d}" for index in range(1, 40)}
    for path in sorted(CONTRACTS.rglob("*.json")):
        if path.name.endswith(SCHEMA_SUFFIX) or path.name == "scenario-bindings.json":
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        stack = [document]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "scenario_ids":
                        assert set(child) <= expected_ids
                    else:
                        stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
