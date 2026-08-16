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
    "capability-matrix.json": "capability-matrix.schema.json",
    "fixtures/apply-mutations.json": "apply-fixture.schema.json",
    "fixtures/hosted-operations.json": "hosted-fixture.schema.json",
    "fixtures/migrations.json": "migration-fixture.schema.json",
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


def test_exact_emails_exist_only_in_operation_inputs_or_authenticated_hosted_results() -> None:
    for fixture in _load("fixtures/apply-mutations.json")["fixtures"]:
        assert not any("@" in text for _, text in _walk_strings(fixture["result"]["show_access"]))
        email_paths = [path for path, text in _walk_strings(fixture) if "@" in text]
        assert all(path[0] == "request" and path[1:3] == ("target", "emails") for path in email_paths)

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
    assert disconnected["expected_reconciliation_outcome"] == "pending_fail_closed"
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
                "principal": "owner_editor",
                "keyed_context": "supported",
            }
            assert point["access_mode"] in {"limited", "public"}
        if decision["hmr"]:
            assert point["surface"] == "/show"
            assert point["principal"] == "owner_editor"
            assert decision["show_editor_capability"] is True

    assert expanded == 2 * 2 * 3 * 2 * 2 * 4 * 3


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
    assert set(interfaces) == {f"C{index:02d}" for index in range(1, 16)}
    assert len(interfaces) == len(registry["interfaces"])
    touched_repositories = {
        endpoint["repository"] for item in registry["interfaces"] for endpoint in [item["producer"], *item["consumers"]]
    }
    assert touched_repositories == repositories
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
            for collection in ("fixtures", "rules", "global_invariants", "interfaces"):
                candidates.extend(document.get(collection, []))
            assert any(item.get("id") == fragment for item in candidates), anchor

    bound = set(binding_ids)
    for path in sorted(CONTRACTS.rglob("*.json")):
        if path.name.endswith(SCHEMA_SUFFIX) or path.name == "scenario-bindings.json":
            continue
        for key_path, text in _walk_strings(json.loads(path.read_text(encoding="utf-8"))):
            if key_path and key_path[-2:-1] == ("scenario_ids",):
                assert text in bound
