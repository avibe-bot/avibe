from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
from datetime import datetime
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
    "capability-matrix.json": "capability-matrix.schema.json",
    "fixtures/apply-mutations.json": "apply-fixture.schema.json",
    "fixtures/hosted-operations.json": "hosted-fixture.schema.json",
    "fixtures/migrations.json": "migration-fixture.schema.json",
    "fixtures/owner-settings.json": "owner-settings-fixture.schema.json",
    "fixtures/show-access.json": "show-access-fixture.schema.json",
    "mirror-registry.json": "mirror-registry.schema.json",
    "rollout.json": "rollout.schema.json",
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


def _walk_strings(value: Any, path: tuple[Any, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_strings(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, (*path, index))
    elif isinstance(value, str):
        yield path, value


def _json_pointer(document: Any, pointer: str) -> Any:
    current = document
    for raw_part in pointer.lstrip("/").split("/") if pointer else []:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


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


def _assert_cleanup_target_matches_committed_state(state: dict[str, Any]) -> None:
    coordinator = state["coordinator"]
    if coordinator.get("kind") != "grant_cleanup":
        return
    assert coordinator["target_access_mode"] == state["access_mode"]
    assert coordinator["target_share_binding"] == state["share_binding"]
    assert (coordinator["operation_id"] is None) == (coordinator["target_grant_commitment"] is None)


def _assert_apply_page_identity(route_page_id: str, request: dict[str, Any], result: dict[str, Any]) -> None:
    assert route_page_id == request["page_id"] == result["page_id"] == result["show_access"]["page_id"]


def _assert_owner_settings_page_identity(route_page_id: str, request: dict[str, Any], result: dict[str, Any]) -> None:
    assert (
        route_page_id
        == request["page_id"]
        == result["page_id"]
        == result["show_access"]["page_id"]
        == result["hosted_current_grant"]["page_id"]
    )


def test_all_json_files_parse_and_all_schemas_are_valid() -> None:
    json_files = sorted(CONTRACTS.rglob("*.json"))
    assert json_files
    for path in json_files:
        json.loads(path.read_text(encoding="utf-8"))

    schemas = _schemas()
    assert schemas
    schema_ids = {schema["$id"] for schema in schemas.values()}
    assert len(schema_ids) == len(schemas)
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        for key_path, reference in _walk_strings(schema):
            if not key_path or key_path[-1] != "$ref":
                continue
            base, _, fragment = reference.partition("#")
            target = schema if not base else next(item for item in schemas.values() if item["$id"] == base)
            _json_pointer(target, fragment)


@pytest.mark.parametrize(("document", "schema"), sorted(DOCUMENT_SCHEMAS.items()))
def test_contract_documents_validate_against_their_schemas(document: str, schema: str) -> None:
    _validator(schema).validate(_load(document))


def test_show_access_closed_vocabularies_and_state_invariants() -> None:
    schema = _load("show-access.schema.json")
    matrix = _load("capability-matrix.json")
    assert schema["$defs"]["availability"]["enum"] == matrix["axes"]["availability"]
    assert schema["$defs"]["access_mode"]["enum"] == matrix["axes"]["access_mode"]
    assert schema["$defs"]["share_admission_gate"]["enum"] == ["open", "closed_pending"]
    assert schema["$defs"]["share_admission_gate"]["enum"] == matrix["axes"]["share_admission_gate"]

    states = _load("fixtures/show-access.json")["fixtures"]
    validator = _validator("show-access.schema.json")
    for fixture in states:
        state = fixture["show_access"]
        validator.validate(state)
        assert (state["grant_revision"] is None) == (state["grant_commitment"] is None)
        assert not any("@" in text for _, text in _walk_strings(state))
        if state["access_mode"] == "private":
            assert state["share_binding"] is None
        else:
            assert state["share_binding"] is not None
        if state["availability"] == "offline" or state["share_admission_gate"] == "closed_pending":
            assert fixture["shared_route_admitted"] is False
        coordinator = state["coordinator"]
        if coordinator["state"] == "pending":
            assert state["audience_revision"] > coordinator["source_audience_revision"]
            assert state["access_mode"] == coordinator["target_access_mode"]
            assert state["share_binding"] == coordinator["target_share_binding"]
        _assert_cleanup_target_matches_committed_state(state)

    invalid = copy.deepcopy(states[0]["show_access"])
    invalid["access_mode"] = "offline"
    with pytest.raises(ValidationError):
        validator.validate(invalid)

    invalid = copy.deepcopy(states[0]["show_access"])
    invalid["unexpected"] = True
    with pytest.raises(ValidationError):
        validator.validate(invalid)

    pending = next(
        copy.deepcopy(fixture["show_access"])
        for fixture in states
        if fixture["show_access"]["coordinator"].get("kind") == "grant_change"
    )
    pending["coordinator"]["source_grant_revision"] = None
    with pytest.raises(ValidationError):
        validator.validate(pending)

    cleanup = next(
        copy.deepcopy(fixture["show_access"])
        for fixture in states
        if fixture["show_access"]["coordinator"].get("kind") == "grant_cleanup"
    )
    cleanup["coordinator"]["operation_id"] = "op_cleanup_pair_0001"
    with pytest.raises(ValidationError):
        validator.validate(cleanup)

    mismatched_cleanup = copy.deepcopy(cleanup)
    mismatched_cleanup["coordinator"]["operation_id"] = None
    mismatched_cleanup["coordinator"]["target_share_binding"] = {"share_id": "different-cleanup-link"}
    with pytest.raises(AssertionError):
        _assert_cleanup_target_matches_committed_state(mismatched_cleanup)


def test_apply_and_hosted_terminal_vocabularies_are_frozen() -> None:
    apply_schema = _load("apply-mutation.schema.json")
    hosted_schema = _load("hosted-operation.schema.json")
    assert apply_schema["$defs"]["apply_result"]["properties"]["outcome"]["enum"] == [
        "applied",
        "no_change",
        "pending",
        "revision_conflict",
    ]
    assert hosted_schema["$defs"]["commit_result"]["properties"]["outcome"]["enum"] == [
        "committed",
        "already_committed",
    ]
    assert hosted_schema["$defs"]["commit_expired_uncommitted"]["properties"]["outcome"]["const"] == (
        "expired_uncommitted"
    )
    assert hosted_schema["$defs"]["commit_revision_conflict"]["properties"]["outcome"]["const"] == ("revision_conflict")
    assert hosted_schema["$defs"]["acknowledgement_result"]["properties"]["outcome"]["enum"] == [
        "acknowledged",
        "already_acknowledged",
        "operation_expired",
    ]

    invalid = copy.deepcopy(_load("fixtures/hosted-operations.json")["fixtures"][0]["messages"][1])
    invalid["outcome"] = "changed"
    with pytest.raises(ValidationError):
        _validator("hosted-operation.schema.json").validate(invalid)

    offline_private = next(
        fixture
        for fixture in _load("fixtures/apply-mutations.json")["fixtures"]
        if fixture["id"] == "APPLY-OFFLINE-TO-PRIVATE"
    )
    assert offline_private["result"]["outcome"] == "pending"
    state = offline_private["result"]["show_access"]
    assert (state["availability"], state["access_mode"], state["share_admission_gate"]) == (
        "offline",
        "private",
        "open",
    )
    assert state["coordinator"] == {
        "state": "pending",
        "kind": "grant_cleanup",
        "phase": "cleanup_pending",
        "mutation_id": "mut_offline_private_001",
        "source_audience_revision": 3,
        "source_grant_revision": 4,
        "target_access_mode": "private",
        "target_share_binding": None,
        "operation_id": None,
        "target_grant_commitment": None,
    }


def test_apply_is_audience_only_page_bound_and_serialized_through_recovery() -> None:
    document = _load("fixtures/apply-mutations.json")
    fixtures = {item["id"]: item for item in document["fixtures"]}
    request_validator = _validator("apply-mutation.schema.json")
    assert "availability" not in _load("apply-mutation.schema.json")["$defs"]["target"]["properties"]

    for fixture in fixtures.values():
        _assert_apply_page_identity(fixture["route_page_id"], fixture["request"], fixture["result"])
        assert "availability" not in fixture["request"]["target"]
        assert fixture["result"]["show_access"]["availability"] == fixture["source_show_access"]["availability"]
        _assert_cleanup_target_matches_committed_state(fixture["result"]["show_access"])

    invalid_availability = copy.deepcopy(fixtures["APPLY-OFFLINE-TO-PRIVATE"]["request"])
    invalid_availability["target"]["availability"] = "active"
    with pytest.raises(ValidationError):
        request_validator.validate(invalid_availability)

    mismatch = document["boundary_cases"][0]
    assert mismatch["outcome"] == "reject_before_coordinator"
    assert mismatch["coordinator_called"] is mismatch["store_write_performed"] is False
    assert mismatch["route_page_id"] != mismatch["request"]["page_id"]
    with pytest.raises(AssertionError):
        _assert_apply_page_identity(
            mismatch["route_page_id"], mismatch["request"], fixtures["APPLY-SAME-SET-NO-CHANGE"]["result"]
        )

    for fixture_id in (
        "APPLY-PUBLIC-TO-LIMITED-HOSTED-NO-CHANGE",
        "APPLY-PRIVATE-TO-LIMITED-HOSTED-NO-CHANGE",
    ):
        fixture = fixtures[fixture_id]
        hosted = fixture["hosted_prepare_result"]
        result = fixture["result"]["show_access"]
        assert hosted["outcome"] == "no_change"
        assert fixture["result"]["outcome"] == "applied"
        assert result["access_mode"] == "limited"
        assert result["audience_revision"] == fixture["source_show_access"]["audience_revision"] + 1
        assert (result["grant_revision"], result["grant_commitment"]) == (
            hosted["grant_revision"],
            hosted["grant_commitment"],
        )

    pending = fixtures["APPLY-PUBLIC-TO-LIMITED-PENDING"]
    source_binding = pending["source_show_access"]["share_binding"]
    assert source_binding == {"share_id": "exact-custom-link"}
    assert {item["case"] for item in pending["terminal_recovery_outcomes"]} == {
        "commit_response_received",
        "lost_response_reconciled",
        "expired_uncommitted_proven",
    }
    for recovery in pending["terminal_recovery_outcomes"]:
        assert recovery["show_access"]["share_binding"] == source_binding

    sequence = document["sequences"][0]
    states = [
        sequence["initial_show_access"],
        sequence["first_apply"]["result"]["show_access"],
        sequence["cleanup_terminal_show_access"],
        sequence["second_apply"]["result"]["show_access"],
    ]
    for step in (sequence["first_apply"], sequence["second_apply"]):
        _assert_apply_page_identity(step["route_page_id"], step["request"], step["result"])
    assert all(state["availability"] == "offline" for state in states)
    assert sequence["first_apply"]["result"]["show_access"]["coordinator"]["kind"] == "grant_cleanup"
    assert sequence["cleanup_terminal_show_access"]["coordinator"] == {"state": "idle"}
    assert sequence["second_apply"]["result"]["show_access"]["share_binding"] == {"share_id": "future-public-link"}
    assert sequence["assertions"] == [
        "single_coordinator_serializes_cleanup_before_second_apply",
        "cleanup_not_overwritten_or_lost",
        "availability_remains_offline",
        "shared_route_never_admitted",
        "future_public_custom_slug_commits",
    ]


def test_exact_emails_exist_only_in_operation_inputs_or_authenticated_hosted_results() -> None:
    for fixture in _load("fixtures/apply-mutations.json")["fixtures"]:
        assert not any("@" in text for _, text in _walk_strings(fixture["result"]["show_access"]))
        email_paths = [path for path, text in _walk_strings(fixture) if "@" in text]
        for path in email_paths:
            if path[0] == "request":
                assert path[1:3] == ("target", "emails")
                continue
            assert path[:2] == ("hosted_prepare_result", "emails")
            assert fixture["hosted_prepare_result"]["authenticated_result"] is True

    for fixture in _load("fixtures/hosted-operations.json")["fixtures"]:
        for message in fixture["messages"]:
            for path, text in _walk_strings(message):
                if "@" not in text:
                    continue
                assert path[0] == "emails"
                assert message["message_type"] == "prepare_request" or message.get("authenticated_result") is True

    for fixture in _load("fixtures/migrations.json")["fixtures"]:
        assert not any("@" in text for _, text in _walk_strings(fixture["expected_show_access"]))
        for path, text in _walk_strings(fixture):
            if "@" in text:
                assert path[:2] == ("hosted_observation", "emails")
                assert fixture["hosted_observation"]["authenticated_result"] is True

    settings = _load("fixtures/owner-settings.json")
    for fixture in settings["fixtures"]:
        assert not any("@" in text for _, text in _walk_strings(fixture["result"]["show_access"]))
        hosted = fixture["result"]["hosted_current_grant"]
        assert hosted["authenticated_result"] is True
        assert all(
            path[:3] == ("result", "hosted_current_grant", "emails")
            for path, text in _walk_strings(fixture)
            if "@" in text
        )


def test_owner_settings_read_is_authorized_transient_and_page_bound() -> None:
    document = _load("fixtures/owner-settings.json")
    fixture = document["fixtures"][0]
    result = fixture["result"]
    hosted = result["hosted_current_grant"]
    _assert_owner_settings_page_identity(fixture["route_page_id"], fixture["request"], result)
    assert fixture["authority"] in {"owner", "sharing_control"}
    assert hosted["authenticated_result"] is True
    assert (result["show_access"]["grant_revision"], result["show_access"]["grant_commitment"]) == (
        hosted["grant_revision"],
        hosted["grant_commitment"],
    )
    assert result["storage_policy"] == "request_scoped_transient_no_store"
    assert result["cache_control"] == "private, no-store"

    mismatched = copy.deepcopy(result)
    mismatched["hosted_current_grant"]["page_id"] = "ses_other_settings"
    with pytest.raises(AssertionError):
        _assert_owner_settings_page_identity(fixture["route_page_id"], fixture["request"], mismatched)

    denied = document["denied_cases"][0]
    assert denied == {
        "id": "SETTINGS-PAGE-EMAIL-DENIED",
        "scenario_ids": ["SHOW-LIVE-002", "SHOW-LIVE-038"],
        "authority": "page_email_only",
        "outcome": "generic_deny",
        "hosted_current_grant_requested": False,
        "settings_returned": False,
    }


def test_hosted_no_change_idempotency_and_recovery_proofs_are_closed() -> None:
    fixtures = {item["id"]: item for item in _load("fixtures/hosted-operations.json")["fixtures"]}

    same_set = fixtures["HOSTED-SAME-SET-NO-CHANGE"]["messages"]
    results = [message for message in same_set if message["message_type"] == "prepare_result"]
    assert results == [results[0], results[0]]
    assert results[0]["outcome"] == "no_change"
    assert all("operation_id" not in message for message in same_set)

    retry = fixtures["HOSTED-IDEMPOTENT-COMMIT"]["messages"]
    commit_results = [message for message in retry if message["message_type"] == "commit_result"]
    assert [message["outcome"] for message in commit_results] == ["committed", "already_committed"]
    assert {
        (message["grant_revision"], message["grant_commitment"], tuple(message["emails"])) for message in commit_results
    } == {(18, "4" * 64, ("retry@example.com",))}

    lost = fixtures["HOSTED-LOST-COMMIT-RESPONSE"]["messages"]
    prepared_results = [
        message for message in lost if message["message_type"] == "prepare_result" and message["outcome"] == "prepared"
    ]
    assert prepared_results == [prepared_results[0], prepared_results[0]]
    status = next(message for message in lost if message.get("outcome") == "committed")
    current = next(message for message in lost if message["message_type"] == "current_grant_result")
    assert (status["grant_revision"], status["grant_commitment"], status["emails"]) == (
        current["grant_revision"],
        current["grant_commitment"],
        current["emails"],
    )

    expired = fixtures["HOSTED-EXPIRED-UNCOMMITTED"]["messages"][-1]
    assert expired["outcome"] == "expired_uncommitted"
    assert (expired["source_grant_revision"], expired["source_grant_commitment"]) == (
        expired["grant_revision"],
        expired["grant_commitment"],
    )
    expired_messages = fixtures["HOSTED-EXPIRED-UNCOMMITTED"]["messages"]
    commit_expiry = next(message for message in expired_messages if message["message_type"] == "commit_result")
    status_expiry = next(
        message for message in expired_messages if message["message_type"] == "operation_status_result"
    )
    assert {
        key: commit_expiry[key]
        for key in (
            "outcome",
            "source_grant_revision",
            "source_grant_commitment",
            "grant_revision",
            "grant_commitment",
            "emails",
        )
    } == {
        key: status_expiry[key]
        for key in (
            "outcome",
            "source_grant_revision",
            "source_grant_commitment",
            "grant_revision",
            "grant_commitment",
            "emails",
        )
    }

    result_expired = fixtures["HOSTED-COMMITTED-RESULT-EXPIRED"]["messages"]
    status_request = next(
        message for message in result_expired if message["message_type"] == "operation_status_request"
    )
    status_result = next(message for message in result_expired if message.get("outcome") == "result_expired")
    current_request = next(message for message in result_expired if message["message_type"] == "current_grant_request")
    current_result = next(message for message in result_expired if message["message_type"] == "current_grant_result")
    assert (
        status_request["page_id"] == status_result["page_id"] == current_request["page_id"] == current_result["page_id"]
    )
    assert status_request["operation_id"] == status_result["operation_id"] == "op_result_expired_0001"
    assert status_result["mutation_id"] == "mut_result_expired_0001"
    assert status_result["source_grant_revision"] < current_result["grant_revision"]
    assert status_result["target_grant_commitment"] == current_result["grant_commitment"]

    prepared = [
        message
        for message in fixtures["HOSTED-LOST-COMMIT-RESPONSE"]["messages"]
        if message.get("outcome") == "prepared"
    ]
    assert len(prepared) == 2
    for message in prepared:
        prepared_at = datetime.fromisoformat(message["prepared_at"].replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(message["expires_at"].replace("Z", "+00:00"))
        assert 0 < (expires_at - prepared_at).total_seconds() <= 24 * 60 * 60
    assert prepared[0] == prepared[1]
    operation_status_prepared = {
        "schema_version": 1,
        "message_type": "operation_status_result",
        "outcome": "prepared",
        "page_id": prepared[0]["page_id"],
        "mutation_id": prepared[0]["mutation_id"],
        "operation_id": prepared[0]["operation_id"],
        "source_grant_revision": prepared[0]["expected_grant_revision"],
        "target_grant_commitment": prepared[0]["target_grant_commitment"],
        "prepared_at": prepared[0]["prepared_at"],
        "expires_at": prepared[0]["expires_at"],
    }
    _validator("hosted-operation.schema.json").validate(operation_status_prepared)
    assert "prepared_lifetime_non_renewable_max_24h" in fixtures["HOSTED-LOST-COMMIT-RESPONSE"]["assertions"]


def test_migration_table_is_fail_closed_and_newer_apply_cancels_pending_work() -> None:
    fixtures = {item["id"]: item for item in _load("fixtures/migrations.json")["fixtures"]}
    expected = {
        "MIG-LEGACY-PUBLIC": ("active", "public", True),
        "MIG-LEGACY-PRIVATE-EMPTY": ("active", "private", False),
        "MIG-LEGACY-OFFLINE": ("offline", "private", False),
        "MIG-PRIVATE-GRANTS-CONNECTED": ("active", "limited", True),
        "MIG-PRIVATE-GRANTS-DISCONNECTED": ("active", "private", False),
        "MIG-NEWER-REVISION-CANCELS": ("active", "private", False),
    }
    assert set(fixtures) == set(expected)
    for fixture_id, (availability, access_mode, shared_route_admitted) in expected.items():
        fixture = fixtures[fixture_id]
        state = fixture["expected_show_access"]
        assert (state["availability"], state["access_mode"], fixture["shared_route_admitted"]) == (
            availability,
            access_mode,
            shared_route_admitted,
        )

    disconnected = fixtures["MIG-PRIVATE-GRANTS-DISCONNECTED"]
    assert disconnected["expected_migration"]["state"] == "pending"
    assert disconnected["expected_migration"]["pending_action"] == "reconcile_legacy_grants"
    assert disconnected["expected_reconciliation_outcome"] == "pending_fail_closed"
    public_unknown = fixtures["MIG-LEGACY-PUBLIC"]
    offline_unknown = fixtures["MIG-LEGACY-OFFLINE"]
    for fixture, outcome in (
        (public_unknown, "pending_public_cleanup"),
        (offline_unknown, "pending_offline_cleanup"),
    ):
        assert fixture["legacy"]["has_exact_email_grants"] == "unknown"
        assert fixture["expected_migration"]["state"] == "pending"
        assert fixture["expected_migration"]["source_audience_revision"] == fixture["legacy"]["audience_revision"]
        assert fixture["expected_migration"]["pending_action"] == "cleanup_unknown_legacy_grants"
        assert fixture["expected_reconciliation_outcome"] == outcome
    empty_private = fixtures["MIG-LEGACY-PRIVATE-EMPTY"]
    assert empty_private["hosted_observation"]["status"] == "available"
    assert empty_private["hosted_observation"]["emails"] == []
    assert empty_private["expected_migration"] == {"state": "none"}
    cancelled = fixtures["MIG-NEWER-REVISION-CANCELS"]
    assert cancelled["expected_show_access"]["audience_revision"] > cancelled["legacy"]["audience_revision"]
    assert cancelled["expected_migration"] == {"state": "none"}
    assert cancelled["expected_reconciliation_outcome"] == "discarded_newer_revision"


def test_capability_rules_form_one_closed_matrix_and_keep_page_email_out_of_show() -> None:
    matrix = _load("capability-matrix.json")
    axes = matrix["axes"]
    rules = matrix["rules"]
    expanded = 0
    for values in itertools.product(*(axes[name] for name in axes)):
        point = dict(zip(axes, values, strict=True))
        matches = [
            rule for rule in rules if all(_matches(rule["selector"][name], value) for name, value in point.items())
        ]
        assert len(matches) == 1, point
        expanded += 1
        decision = matches[0]["decision"]
        if point["availability"] == "offline":
            assert decision["read_decision"] == "deny"
        if point["surface"] == "/p" and point["share_admission_gate"] == "closed_pending":
            assert decision["read_decision"] == "deny"
        if point["surface"] == "/p":
            assert decision["hmr"] is False
            assert decision["runtime_context"] != "private"
            if point["request_kind"] == "other":
                assert decision["top_level_editor_redirect"] is False
        if point["principal"] == "page_email_viewer":
            assert decision["show_editor_capability"] is False
            assert decision["annotations"] is False
            assert decision["hmr"] is False
            if point["surface"] == "/show":
                assert decision["read_decision"] == "deny"
        if decision["top_level_editor_redirect"]:
            assert point == {
                "surface": "/p",
                "availability": "active",
                "access_mode": point["access_mode"],
                "share_admission_gate": "open",
                "request_kind": "trusted_top_level_navigation",
                "limited_reauthorization_state": point["limited_reauthorization_state"],
                "principal": "owner_editor",
                "keyed_context": "supported",
            }
            assert point["access_mode"] in {"limited", "public"}
        if decision["hmr"]:
            assert point["surface"] == "/show"
            assert point["principal"] == "owner_editor"
            assert decision["show_editor_capability"] is True

    assert expanded == 2 * 2 * 3 * 2 * 2 * 2 * 4 * 3

    anonymous_limited = [
        rule
        for rule in rules
        if rule["selector"]["surface"] == "/p"
        and rule["selector"]["access_mode"] == "limited"
        and rule["selector"]["principal"] == "anonymous"
    ]
    assert {
        rule["selector"]["limited_reauthorization_state"]: rule["decision"]["read_decision"]
        for rule in anonymous_limited
    } == {
        "not_attempted": "login_required",
        "attempted_without_current_page_grant": "deny",
    }

    authorization = matrix["limited_read_authorization"]
    assert authorization["generic_session_behavior"] == "one_page_specific_reauthorization"
    assert authorization["handshake_correlation"]["required_carriers"] == [
        "signed_oauth_state",
        "browser_bound_cookie",
        "single_use_server_side_fallback",
    ]
    assert authorization["callback_requires"][-1] == (
        "claim_grant_revision_equals_authenticated_local_current_grant_revision"
    )
    assert authorization["local_grant_revision_sources"] == [
        "authenticated_current_grant_result",
        "authenticated_committed_operation_result",
    ]
    assert authorization["attempt_marker"] == {
        "authority": "negative_only",
        "maximum_uses": 1,
        "effect": "suppress_repeat_reauthorization",
        "removed_from_safe_return_url": True,
        "positive_authority": False,
    }
    current_case, stale_case = authorization["claim_revision_cases"]
    assert current_case["claim_grant_revision"] == current_case["authenticated_local_grant_revision"]
    assert current_case["outcome"] == "page_email_viewer"
    assert stale_case["claim_grant_revision"] != stale_case["authenticated_local_grant_revision"]
    assert stale_case["outcome"] == "generic_deny"


def test_runtime_safety_context_proxy_and_hmr_property_owners_are_executable() -> None:
    contract = _load("runtime-context.json")
    operations = contract["app_graph_operations"]

    loopback = contract["loopback_request_boundary"]
    assert loopback["browser_supplied_headers_removed"] == [
        SHOW_RUNTIME_PROTOCOL_HEADER,
        SHOW_RUNTIME_CONTEXT_HEADER,
    ]
    assert loopback["server_owned_envelope_count"] == 1
    assert loopback["applies_to_operations"] == operations

    validation = contract["protocol1_validation"]
    assert validation["applies_to_operations"] == operations
    assert validation["validation_precedes"] == [
        "session_resolution",
        "graph_lookup",
        "graph_create",
        "graph_rebase",
        "graph_ownership_mutation",
        "hmr_connection",
    ]
    assert validation["invalid_context_outcome"] == "reject_without_graph_side_effects"
    assert validation["unknown_protocol_outcome"] == "reject_without_graph_side_effects"

    ownership = contract["runtime_graph_ownership"]
    assert ownership["graph_key"] == ["session_id", "context"]
    assert ownership["maximum_contexts_per_session"] == 2
    assert "other_session_activity" in ownership["private_lifecycle_independent_of"]
    assert ownership["shared_rebase_scope"] == "same_session_shared_graph_only"
    assert ownership["shared_activity_closes_private_hmr"] is False

    headerless = contract["request_protocol_cases"][0]
    assert headerless == {
        "id": "released_headerless",
        "protocol_header": "absent",
        "context_header": "ignored",
        "outcome": "legacy_singleton",
        "legacy_base_header": "x-vibe-show-base",
        "legacy_base_propagation": [
            "entry_http",
            "module_http",
            "spa_fallback_http",
            "startup_reconciliation_prewarm",
            "show_update_prewarm",
        ],
        "keyed_context_enabled": False,
    }

    cache = contract["capability_cache"]
    assert cache["maximum_retry_delay_seconds"] == 5
    assert cache["identity_inputs"] == ["runtime_process_identity", "runtime_base_url"]
    assert cache["identity_change_clears"] == ["all_outcomes", "retry_deadline"]

    namespace = contract["shared_namespace_confinement"]
    closure = namespace["recursive_dependency_closure"]
    assert closure["dependency_kinds"] == ["tsx", "css", "raw_loader", "worker"]
    assert closure["namespace_relation"] == "same_opaque_namespace"
    assert closure["provenance"] == "immutable_captured"
    assert closure["nested_fs_path_allowed"] is False
    assert closure["mutable_source_fallback_allowed"] is False
    assert closure["cross_namespace_escape_allowed"] is False
    handle = namespace["handle"]
    assert handle["source_path_reopen"] == "forbidden"
    assert handle["path_swap_existing_handle"] == "serve_captured_response_only"
    assert handle["new_admission_after_unsafe_path_swap"] == "deny_before_allocation"

    lifetime = contract["namespace_lifetime"]
    assert lifetime["idle_deadline_seconds"] == 30 * 60
    assert lifetime["absolute_lifetime_seconds"] == 2 * 60 * 60
    assert lifetime["absolute_lifetime_renewable"] is False
    assert lifetime["reclaim_unit"] == "whole_namespace"

    budget = contract["resource_governance"]
    assert budget["budget_scope"] == "one_per_runtime_process"
    assert budget["hard_limit_dimensions"] == ["context_count", "weighted_memory_cost"]
    assert budget["runtime_and_per_session_limits"] == "finite"
    assert budget["per_session_bounds"] is True
    assert budget["private_editor_reserve"] == "finite_nonzero"
    assert budget["private_editor_reserve_relation"] == "0 < reserve < process_wide_budget"
    assert budget["shared_may_consume_private_reserve"] is False
    assert budget["reclaim_order"] == [
        "expired_namespaces",
        "oldest_shared_bundle_without_in_flight_request",
    ]
    assert budget["private_admission_reclaim"] == "any_shared_bundle_without_in_flight_request"
    assert budget["in_flight_pinning"] == "until_response_finishes_only"
    assert budget["shared_overload_outcome"] == "bounded_sanitized_retryable_unavailable"

    proxy = contract["shared_proxy_policy"]
    assert proxy["service_worker_allowed_header"] == "strip"
    assert proxy["maximum_service_worker_scope"] == "/p/<share_id>/"
    assert proxy["shared_entry_cache_control"] == proxy["editor_redirect_cache_control"] == "private, no-store"
    assert proxy["limited_response_cache_control"] == "private, no-store"
    assert proxy["limited_response_surfaces"] == ["entry", "module", "spa_fallback", "api_handler"]
    assert proxy["identity_vary_header"] == "Cookie"
    assert proxy["cross_principal_response_reuse"] is False
    redirect = contract["route_invariants"]["editor_redirect"]
    assert redirect["preserve_route_suffix"] is redirect["preserve_query"] is True
    assert redirect["cache_control"] == "private, no-store"
    assert redirect["vary"] == "Cookie"

    monitor = contract["avibe_private_hmr_monitor"]
    assert monitor["implementation_owner"] == "avibe"
    assert monitor["runtime_implementation_owner"] is False
    assert monitor["monitor_scope"] == "one_coalesced_monitor_per_active_session"
    assert monitor["authorization_triggers"] == [
        "editor_capability_loss",
        "resource_access_revocation",
        "authorization_revision_change",
    ]
    assert monitor["authorization_trigger_action"] == "reevaluate_and_close_unauthorized_existing_sockets"
    assert monitor["durable_offline_source"] == "ShowPageStore"
    assert monitor["durable_offline_poll_max_seconds"] == 5
    assert monitor["durable_offline_close_all_deadline_seconds"] == 5


def test_runtime_constants_and_release_advertisement_match_the_frozen_contract() -> None:
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
    assert _load("capability-matrix.json")["axes"]["keyed_context"] == [
        case["outcome"] for case in contract["negotiation_cases"]
    ]
    assert [case["outcome"] for case in contract["request_protocol_cases"]] == [
        "legacy_singleton",
        "keyed_context",
        "reject",
    ]

    response_cases = {case["id"]: case for case in contract["shared_response_cases"]}
    assert set(response_cases) == {
        "keyed_immutable_graph_success",
        "keyed_development_diagnostic",
        "legacy_unclassified_transform_error",
        "legacy_raw_or_nested_fs_graph",
    }
    for case in response_cases.values():
        assert all(
            case[field] is False
            for field in ("raw_source", "host_path", "private_session_path", "development_diagnostic")
        )
    legacy_failures = [
        response_cases["legacy_unclassified_transform_error"],
        response_cases["legacy_raw_or_nested_fs_graph"],
    ]
    for case in legacy_failures:
        assert case["runtime_mode"] == "legacy_singleton"
        assert case["outcome"] == "fixed_path_free_unavailable"
        assert case["body_source"] == "fixed_sanitized_representation"
        assert case["url_header_policy"] == "remove_url_bearing_headers"

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
        _validator("runtime-context.schema.json").validate(mismatched)
        with pytest.raises(AssertionError):
            _assert_runtime_release_gate(mismatched["release_gate"])


def test_legacy_put_has_a_one_way_enforcement_and_retirement_boundary() -> None:
    rollout = _load("rollout.json")
    phases = rollout["phases"]
    assert [phase["phase"] for phase in phases] == ["additive", "enforced", "retired"]
    assert phases[0]["new_access_schema_may_activate"] is False
    assert phases[0]["current_grant_legacy_write_policy"] == "temporarily_allowed"
    for phase in phases[1:]:
        assert phase["new_access_schema_may_activate"] is True
        assert phase["current_grant_legacy_write_policy"] == "disabled"
        assert phase["legacy_put_result"] != "accepted_for_released_clients"
    assert rollout["enforcement_marker"] == {
        "producer": "authenticated_current_grant_result",
        "field": "legacy_write_policy",
        "enforced_value": "disabled",
    }
    hosted_policies = set(
        _load("hosted-operation.schema.json")["$defs"]["current_grant_result"]["properties"]["legacy_write_policy"][
            "enum"
        ]
    )
    assert {phase["current_grant_legacy_write_policy"] for phase in phases} == hosted_policies


def test_mirror_registry_names_every_repository_and_security_boundary() -> None:
    registry = _load("mirror-registry.json")
    repositories = {item["id"] for item in registry["repositories"]}
    assert repositories == {"avibe", "avibe-backend", "vibe-show-runtime"}
    interfaces = {item["id"]: item for item in registry["interfaces"]}
    assert set(interfaces) == {f"C{index:02d}" for index in range(1, 24)}
    assert len(interfaces) == len(registry["interfaces"])
    touched_repositories = {
        endpoint["repository"] for item in registry["interfaces"] for endpoint in [item["producer"], *item["consumers"]]
    }
    assert touched_repositories == repositories
    assert registry["process_ownership"] == {
        "stable_writer_lease": "ShowAccessCoordinator.stable_writer_lease",
        "serialization_owner": "controller_process.ShowAccessCoordinator",
        "store_write_owner": "controller_process.ShowAccessCoordinator",
        "ui_process_coordinator_allowed": False,
        "serialized_operations": ["apply", "recovery", "migration_reconciliation", "grant_cleanup"],
    }
    assert interfaces["C01"]["delivery"]["authentication"] == "owner_or_sharing_control_resource_authority"
    assert interfaces["C01"]["delivery"]["mechanism"] == "local_http_json"
    assert interfaces["C02"]["consumers"] == [
        {
            "repository": "avibe",
            "path": "vibe/ui_server.py",
            "symbol": "show_page_access_apply",
        }
    ]
    assert interfaces["C08"]["signature"]["covered_fields"] == [
        "vibe_show_page_id",
        "vibe_show_page_grant_revision",
        "vibe_instance_access_source",
    ]
    assert interfaces["C09"]["signature"]["schema"] == "capability-matrix.json"
    assert "request_kind" in interfaces["C09"]["signature"]["covered_fields"]
    assert interfaces["C10"]["signature"]["covered_fields"][:2] == [
        SHOW_RUNTIME_PROTOCOL_HEADER,
        SHOW_RUNTIME_CONTEXT_HEADER,
    ]
    assert interfaces["C11"]["signature"]["covered_fields"] == ["protocol", "features"]
    assert interfaces["C13"]["signature"]["covered_fields"][:4] == [
        "reviewed_runtime_sha",
        "reviewed_runtime_pr",
        "smoke_tested_runtime_sha",
        "bundled_runtime_sha",
    ]
    assert "runtime_source.ref" in interfaces["C13"]["signature"]["result"]
    assert interfaces["C13"]["delivery"]["mechanism"] == "reviewed_release_manifest"
    assert interfaces["C14"]["signature"]["schema"] == "rollout.json"
    assert interfaces["C15"]["signature"]["schema"] == "runtime-context.json#/shared_response_cases"
    assert interfaces["C15"]["delivery"]["mechanism"] == "loopback_http_response"
    assert interfaces["C16"]["delivery"]["mechanism"] == "internal_socket_json"
    assert interfaces["C16"]["consumers"][0]["path"] == "vibe/internal_server.py"
    assert "stable_writer_lease" in interfaces["C16"]["delivery"]["serialization_owner"]
    assert interfaces["C18"]["signature"]["schema"] == "owner-settings.schema.json"
    assert interfaces["C18"]["delivery"]["authentication"] == ("owner_or_sharing_control_resource_authority")
    assert interfaces["C19"]["delivery"]["mechanism"] == "internal_socket_json"
    assert interfaces["C20"]["signature"]["schema"] == "runtime-context.json#/loopback_request_boundary"
    assert interfaces["C20"]["delivery"]["authentication"] == ("browser_untrusted_replaced_by_server_envelope")
    assert interfaces["C21"]["signature"]["covered_fields"] == [
        "protocol1_validation",
        "runtime_graph_ownership",
        "shared_namespace_confinement",
        "namespace_lifetime",
        "resource_governance",
        "shared_proxy_policy",
    ]
    assert interfaces["C22"]["signature"]["schema"] == "runtime-context.json#/avibe_private_hmr_monitor"
    assert interfaces["C23"]["signature"]["schema"] == ("capability-matrix.json#/limited_read_authorization")
    trust_boundaries = {
        "C01": (
            "avibe:vibe/ui_server.py#show_page_access_get",
            ["avibe:ui/src/lib/showPageAccess.ts#ShowAccess"],
            "local_http_json",
            "owner_or_sharing_control_resource_authority",
            "controller_process.ShowAccessCoordinator",
        ),
        "C02": (
            "avibe:ui/src/lib/showPageAccess.ts#ShowAccessApplyRequest",
            ["avibe:vibe/ui_server.py#show_page_access_apply"],
            "local_http_json",
            "owner_resource_authority",
            "controller_process.ShowAccessCoordinator",
        ),
        "C16": (
            "avibe:vibe/ui_server.py#show_page_access_apply",
            ["avibe:vibe/internal_server.py#show_page_access_apply"],
            "internal_socket_json",
            "controller_internal_socket_peer",
            "controller_process.ShowAccessCoordinator under stable_writer_lease",
        ),
        "C17": (
            "avibe:vibe/ui_server.py#show_page_access_get",
            ["avibe:vibe/internal_server.py#show_page_access_read"],
            "internal_socket_json",
            "controller_internal_socket_peer",
            "controller_process.ShowAccessCoordinator under stable_writer_lease",
        ),
        "C18": (
            "avibe:ui/src/lib/showPageAccess.ts#OwnerShowAccessSettingsRequest",
            ["avibe:vibe/ui_server.py#show_page_owner_settings_get"],
            "local_http_json",
            "owner_or_sharing_control_resource_authority",
            "controller_process.ShowAccessCoordinator",
        ),
        "C19": (
            "avibe:vibe/ui_server.py#show_page_owner_settings_get",
            ["avibe:vibe/internal_server.py#show_page_owner_settings_read"],
            "internal_socket_json",
            "controller_internal_socket_peer",
            "controller_process.ShowAccessCoordinator under stable_writer_lease",
        ),
        "C20": (
            "avibe:vibe/ui_server.py#build_show_runtime_request",
            ["avibe:core/show_runtime.py#ShowRuntimeProtocolEnvelope"],
            "in_process_value",
            "browser_untrusted_replaced_by_server_envelope",
            "core.show_runtime.ShowRuntimeManager",
        ),
        "C21": (
            "avibe:docs/plans/show-access-contracts/runtime-context.json#RuntimeSafetyOwner",
            [
                "vibe-show-runtime:packages/runtime/src/runtime.ts#RuntimeContextRegistry",
                "vibe-show-runtime:packages/runtime/src/server.ts#routeRequest",
            ],
            "reviewed_contract_mirror",
            "reviewed_ci_head",
            "vibe-show-runtime.RuntimeContextRegistry",
        ),
        "C22": (
            "avibe:core/show_access.py#PrivateHmrRevocationMonitor",
            ["avibe:vibe/ui_server.py#show_runtime_hmr_websocket"],
            "in_process_value",
            "existing_resource_broker_and_durable_state",
            "avibe.PrivateHmrRevocationMonitor",
        ),
        "C23": (
            "avibe:vibe/ui_server.py#start_limited_page_authorization",
            [
                "avibe-backend:app/api/v1/oauth/authorize/route.ts#GET",
                "avibe-backend:lib/oidc/authorization-context.ts#resolveShowPageEmailGrant",
            ],
            "authorization_handshake",
            "signed_state_cookie_or_device_fallback",
            "single_use_page_authorization_handshake",
        ),
    }

    def endpoint_name(endpoint: dict[str, str]) -> str:
        return f"{endpoint['repository']}:{endpoint['path']}#{endpoint['symbol']}"

    for interface_id, expected in trust_boundaries.items():
        interface = interfaces[interface_id]
        delivery = interface["delivery"]
        assert (
            endpoint_name(interface["producer"]),
            [endpoint_name(consumer) for consumer in interface["consumers"]],
            delivery["mechanism"],
            delivery["authentication"],
            delivery["serialization_owner"],
        ) == expected


def test_every_design_scenario_is_bound_and_every_anchor_resolves() -> None:
    binding_document = _load("scenario-bindings.json")
    design_sha = binding_document["design_sha"]
    authority = _load("mirror-registry.json")["authority"]
    assert design_sha == authority["source_sha"]
    assert binding_document["design_blob_sha"] == authority["source_design_blob_sha"]
    design_bytes = DESIGN.read_bytes()
    git_blob = b"blob " + str(len(design_bytes)).encode("ascii") + b"\0" + design_bytes
    assert hashlib.sha1(git_blob, usedforsecurity=False).hexdigest() == binding_document["design_blob_sha"]
    pinned_design = design_bytes.decode("utf-8")
    design_ids = set(re.findall(r"`(SHOW-LIVE-[0-9]{3})`", pinned_design))
    bindings = binding_document["bindings"]
    binding_ids = [binding["scenario_id"] for binding in bindings]
    assert len(binding_ids) == len(set(binding_ids))
    assert set(binding_ids) == design_ids == {f"SHOW-LIVE-{index:03d}" for index in range(1, 39)}
    assert {"SHOW-LIVE-037", "SHOW-LIVE-038"} <= set(binding_ids)

    for binding in bindings:
        for anchor in binding["anchors"]:
            file_name, fragment = anchor.split("#", 1)
            document = _load(file_name)
            if fragment.startswith("/"):
                _json_pointer(document, fragment)
                continue
            candidates = []
            for collection in (
                "fixtures",
                "boundary_cases",
                "sequences",
                "rules",
                "global_invariants",
                "interfaces",
            ):
                candidates.extend(document.get(collection, []))
            assert any(item.get("id") == fragment for item in candidates), anchor

    bound = set(binding_ids)
    for path in sorted(CONTRACTS.rglob("*.json")):
        if path.name.endswith(SCHEMA_SUFFIX) or path.name == "scenario-bindings.json":
            continue
        for key_path, text in _walk_strings(json.loads(path.read_text(encoding="utf-8"))):
            if key_path and key_path[-2:-1] == ("scenario_ids",):
                assert text in bound
