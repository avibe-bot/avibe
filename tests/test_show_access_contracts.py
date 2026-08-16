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
    "mirror-registry.json": "mirror-registry.schema.json",
    "retirement.json": "retirement.schema.json",
    "runtime-context.json": "runtime-context.schema.json",
    "scenario-bindings.json": "scenario-bindings.schema.json",
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
    assert release_gate["delivery_item_6"] == "implemented"
    assert release_gate["delivery_item_9"] == "implemented"
    assert release_gate["smoke_test"] == "passed"
    assert release_gate["reviewed_runtime_pr"] is not None
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

    assert evaluated == 2 * 3 * 3 * 2 * 3 * 2 * 3 * 2 * 3
    assert valid_transitions > 0
    assert algebra["normalization"] == {
        "steps": ["trim_ascii_surrounding_whitespace", "lowercase", "deduplicate", "sort"],
        "provider_specific_alias_normalization": False,
        "canonical_set_comparison": "exact",
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
    }

    rotate = {
        "availability": "active",
        "source_mode": "limited",
        "target_mode": "limited",
        "source_binding": "bound",
        "share_intent": "rotate",
        "binding_relation": "different",
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
    for case in settings["denied_cases"]:
        assert case["authority"] in {"resource_viewer", "page_member_only", "anonymous"}
        assert case["result"]["store_read"] is case["result"]["settings_returned"] is False


def test_identity_assertion_is_instance_bound_identity_only_and_membership_is_fresh() -> None:
    contract = _load("identity-auth.json")
    assertion = contract["backend_assertion"]
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
    assert assertion["maximum_lifetime_seconds"] <= 600
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

    cases = {case["id"]: case for case in contract["cases"]}
    assert cases["IDENTITY-LISTED-CURRENT"]["outcome"] == "serve_shared"
    assert cases["IDENTITY-REMOVED-AFTER-SESSION"]["outcome"] == "generic_deny"
    assert cases["IDENTITY-CROSS-INSTANCE"]["outcome"] == "reject_identity_assertion"
    for case in cases.values():
        assert all(
            case[field] is False
            for field in ("instance_access", "canonical_show_access", "hmr", "annotations", "agents")
        )


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

    redirect = contract["route_invariants"]["resource_redirect"]
    assert redirect["requires"] == [
        "active_share_binding",
        "resource_viewer_or_editor_authority",
        "trusted_fetch_metadata",
    ]
    assert redirect["preserve_route_suffix"] is redirect["preserve_query"] is True


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
            "delivery_item_6": "implemented",
            "delivery_item_9": "implemented",
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
    assert set(interfaces) == {f"C{index:02d}" for index in range(1, 17)}
    assert len(interfaces) == len(registry["interfaces"])
    assert registry["process_ownership"] == {
        "stable_writer_lease": "ShowAccessService.stable_writer_lease",
        "serialization_owner": "controller_process.ShowAccessService",
        "store_write_owner": "controller_process.ShowAccessStore.replace_transactionally",
        "apply_receipt_store": "controller_process.ShowAccessStore keyed by page_id plus mutation_id",
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
    expected_ids = {f"SHOW-LIVE-{index:03d}" for index in range(1, 39)}
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
    expected_ids = {f"SHOW-LIVE-{index:03d}" for index in range(1, 39)}
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
