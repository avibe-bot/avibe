from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import fields
from itertools import product
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker, ValidationError

from config.v2_config import (
    MODEL_HUB_BACKENDS,
    _legacy_source_eligible_for_backend,
    MODEL_HUB_ENABLED_ENV,
    MODEL_HUB_LEGACY_CREATED_AT,
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
    is_model_hub_enabled,
)
from core.services.settings import default_config
from core.handlers.model_hub.identifiers import opencode_model_id
from core.handlers.model_hub.adapter import (
    OBSERVATION_TERMINAL_RULES,
    ObservationDiscovery,
    ObservationOutcome,
    SOURCE_PROTOCOLS,
    SourceObservation,
    validate_source_observation,
)
from scripts.check_model_hub_authorities import check as check_model_hub_authorities
from vibe import api

CONTRACTS = Path("docs/plans/model-hub-contracts")
UI_MODEL_CONSUMERS = Path("ui/src/components/settings/models")


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _assert_valid(name: str, payload: dict) -> None:
    errors = sorted(
        Draft7Validator(_schema(name), format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, [error.message for error in errors]


def test_protocol_vocabulary_matches_authority_and_rejects_removed_alias():
    protocols = tuple(_schema("source.schema.json")["properties"]["protocol"]["enum"])
    assert SOURCE_PROTOCOLS == protocols

    type_source = (UI_MODEL_CONSUMERS / "types.ts").read_text(encoding="utf-8")
    type_match = re.search(
        r"export type SourceProtocol\s*=\s*(.*?);",
        type_source,
        re.DOTALL,
    )
    assert type_match is not None
    assert tuple(re.findall(r"'([^']+)'", type_match.group(1))) == protocols

    retired_alias = "openai" + "_compatible"
    for filename in (
        "types.ts",
        "vendorMeta.ts",
        "modelsApi.ts",
        "mockData.ts",
        "modelRows.test.ts",
    ):
        assert retired_alias not in (UI_MODEL_CONSUMERS / filename).read_text(
            encoding="utf-8"
        )

    example = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    example["protocol"] = retired_alias
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("source.schema.json")).validate(example)
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(example)


def test_unsaved_observation_schema_closes_all_terminal_shapes():
    schema = _schema("observation-result.schema.json")
    assert schema["properties"]["contract_version"]["const"] == 5
    assert tuple(schema["properties"]["outcome"]["enum"]) == tuple(
        member.value for member in ObservationOutcome
    )
    assert tuple(schema["properties"]["discovery"]["enum"]) == tuple(
        member.value for member in ObservationDiscovery
    )
    for example in schema["examples"]:
        _assert_valid("observation-result.schema.json", example)
    invalid = copy.deepcopy(schema["examples"][0])
    invalid["protocol"] = "openai" + "_compatible"
    with pytest.raises(ValidationError):
        Draft7Validator(schema).validate(invalid)


def test_observation_terminal_authority_and_schema_accept_the_same_products():
    schema = Draft7Validator(_schema("observation-result.schema.json"))
    reachable_domain = frozenset({True, False, None})
    authenticated_domain = frozenset({True, False, None})
    protocol_domain = frozenset({None, *SOURCE_PROTOCOLS})
    discovery_domain = frozenset(ObservationDiscovery)

    def payload(observation: SourceObservation) -> dict:
        return {
            "contract_version": 5,
            "outcome": observation.outcome.value,
            "reachable": observation.reachable,
            "authenticated": (
                "authenticated"
                if observation.authenticated is True
                else "rejected"
                if observation.authenticated is False
                else "unknown"
            ),
            "protocol": observation.protocol,
            "discovery": observation.discovery.value,
            "models": list(observation.model_ids),
        }

    assert set(OBSERVATION_TERMINAL_RULES) == set(ObservationOutcome)
    for outcome, rule in OBSERVATION_TERMINAL_RULES.items():
        legal_products = product(
            rule.reachable,
            rule.authenticated,
            rule.protocols,
            rule.discoveries,
        )
        for reachable, authenticated, protocol, discovery in legal_products:
            observation = SourceObservation(
                outcome=outcome,
                reachable=reachable,
                authenticated=authenticated,
                protocol=protocol,
                discovery=discovery,
                model_ids=(),
            )
            assert validate_source_observation(observation) is observation
            schema.validate(payload(observation))

        baseline = SourceObservation(
            outcome=outcome,
            reachable=next(iter(rule.reachable)),
            authenticated=next(iter(rule.authenticated)),
            protocol=next(iter(rule.protocols)),
            discovery=next(iter(rule.discoveries)),
            model_ids=(),
        )
        invalid_fields = {
            "reachable": reachable_domain - rule.reachable,
            "authenticated": authenticated_domain - rule.authenticated,
            "protocol": protocol_domain - rule.protocols,
            "discovery": discovery_domain - rule.discoveries,
        }
        for field_name, rejected_values in invalid_fields.items():
            for rejected in rejected_values:
                invalid_observation = SourceObservation(
                    **{**baseline.__dict__, field_name: rejected}
                )
                with pytest.raises(ValueError):
                    validate_source_observation(invalid_observation)
                with pytest.raises(ValidationError):
                    schema.validate(payload(invalid_observation))

        if rule.models_must_be_empty:
            invalid_inventory = SourceObservation(
                **{**baseline.__dict__, "model_ids": ("model-id",)}
            )
            with pytest.raises(ValueError):
                validate_source_observation(invalid_inventory)
            with pytest.raises(ValidationError):
                schema.validate(payload(invalid_inventory))
        else:
            succeeded = SourceObservation(
                **{
                    **baseline.__dict__,
                    "discovery": ObservationDiscovery.SUCCEEDED,
                    "model_ids": ("model-id",),
                }
            )
            assert validate_source_observation(succeeded) is succeeded
            schema.validate(payload(succeeded))
            failed_with_inventory = SourceObservation(
                **{
                    **baseline.__dict__,
                    "discovery": ObservationDiscovery.FAILED,
                    "model_ids": ("model-id",),
                }
            )
            with pytest.raises(ValueError):
                validate_source_observation(failed_with_inventory)
            with pytest.raises(ValidationError):
                schema.validate(payload(failed_with_inventory))


def test_final_model_validator_requires_explicit_credential_free_efforts():
    example = copy.deepcopy(_schema("source.schema.json")["examples"][0]["models"][0])
    example.pop("reasoning_efforts")
    with pytest.raises(ValueError):
        ModelHubModelConfig.from_payload(example)
    example["reasoning_efforts"] = ["authorization: sk-test-credential-material"]
    with pytest.raises(ValueError):
        ModelHubModelConfig.from_payload(example)


def test_source_validator_enforces_final_cross_field_and_inventory_rules():
    native = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    native["models"] = [native["models"][0], copy.deepcopy(native["models"][0])]
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(native)

    manual = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    manual["models"][0]["discovered_at"] = "2026-08-09T00:00:00Z"
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(manual)

    hub_without_ref = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    hub_without_ref["credential_ref"] = None
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(hub_without_ref)

    duplicate_native = ModelHubConfig().to_payload()
    native_example = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    duplicate_native["sources"] = [native_example, {**native_example, "id": "src_claudepro2"}]
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(duplicate_native)

    credential_url = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    credential_url["base_url"] = "https://relay.example/v1?api_key=secret"
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(credential_url)

    safe_query_url = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    safe_query_url["base_url"] = "https://relay.example/v1/?api-version=2026-07-23"
    canonical_vendor = copy.deepcopy(safe_query_url)
    canonical_vendor["vendor"] = " OpenAI "
    assert ModelHubSourceConfig.from_payload(canonical_vendor).vendor == "openai"
    parsed = ModelHubSourceConfig.from_payload(safe_query_url)
    assert parsed.base_url == "https://relay.example/v1?api-version=2026-07-23"


def test_persisted_model_hub_identifiers_share_the_canonical_validation_boundary():
    payload = ModelHubConfig().to_payload()
    opencode = payload["agents"]["opencode"]
    opencode["menu"]["checked"] = ["gpt-5"]
    opencode["routes"]["gpt-5"] = {"hops": []}
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)

    payload = ModelHubConfig().to_payload()
    opencode = payload["agents"]["opencode"]
    opencode["routes"]["custom/authorization: sk-test-credential-material"] = {
        "hops": []
    }
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)

    payload = ModelHubConfig().to_payload()
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    payload["sources"] = [source]
    codex_route = next(iter(payload["agents"]["codex"]["routes"].values()))
    codex_route["hops"] = [
        {
            "source_id": source["id"],
            "model_id": "authorization: sk-test-credential-material",
        }
    ]
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _json_pointer(document: dict, pointer: str):
    value = document
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _vocabulary(schema: dict, paths: list[str], *, exclude=()) -> set:
    values = set()
    for pointer in paths:
        node = _json_pointer(schema, pointer)
        values.update(node["enum"] if "enum" in node else [node["const"]])
    return values - set(exclude)


def _validate_mirror_entry(entry: dict, schemas: dict[str, dict]) -> None:
    rule = entry["rule"]
    if rule == "none":
        assert entry["reason"]
        return
    if rule == "equality":
        normalized = []
        for item in entry["sets"]:
            actual = _vocabulary(
                schemas[item["schema"]],
                item["paths"],
                exclude=item.get("exclude", ()),
            )
            extras = set(item.get("extras", ()))
            normalized.append(actual - extras)
            assert actual == (actual - extras) | extras
        assert all(values == normalized[0] for values in normalized[1:])
        return
    if rule == "mapping":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
        )
        targets = entry.get("targets")
        if targets is None:
            targets = [{**entry["target"], "mapping": entry["mapping"]}]
        for item in targets:
            target = _vocabulary(
                schemas[item["schema"]],
                [item["path"]],
                exclude=item.get("exclude", ()),
            )
            mapping = item["mapping"]
            assert home == set(mapping)
            assert target == set(mapping.values())
        return
    if rule == "partition":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
        )
        member = _vocabulary(
            schemas[entry["member"]["schema"]],
            [entry["member"]["path"]],
        )
        exclusions = set(entry["exclusions"])
        for item in entry["exclusion_sets"]:
            exclusions |= _vocabulary(schemas[item["schema"]], [item["path"]])
        assert not (member & exclusions)
        assert home == member | exclusions
        return
    if rule == "bijection":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
        )
        target = set()
        for item in entry["target_sets"]:
            target |= _vocabulary(schemas[item["schema"]], item["paths"])
        assert set(entry["pairs"]) <= home
        assert len(set(entry["pairs"].values())) == len(entry["pairs"])
        assert target == set(entry["pairs"].values())
        return
    if rule == "projection":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
            exclude=entry["home"].get("exclude", ()),
        )
        for item in entry["targets"]:
            target = _vocabulary(
                schemas[item["schema"]],
                [item["path"]],
                exclude=item.get("exclude", ()),
            )
            assert target == home - set(item["drop_from_home"])
        return
    raise AssertionError(f"unknown mirror rule: {rule}")


def _mirror_schemas(registry: dict) -> dict[str, dict]:
    names = {
        value["schema"]
        for entry in registry["entries"]
        for key in ("sets", "target_sets", "targets", "exclusion_sets")
        for value in entry.get(key, [])
    }
    for entry in registry["entries"]:
        for key in ("home", "target", "member"):
            if key in entry:
                names.add(entry[key]["schema"])
    return {name: _schema(name) for name in names}


def test_frozen_source_and_agent_examples_round_trip_byte_faithfully():
    assert Path("core/handlers/model_hub/adapter.py").read_bytes() == (CONTRACTS / "adapter-interface.py").read_bytes()

    for example in _schema("source.schema.json")["examples"]:
        serialized = ModelHubSourceConfig.from_payload(example).to_payload()
        expected = json.loads(json.dumps(example))
        expected.setdefault("created_at", MODEL_HUB_LEGACY_CREATED_AT)
        if "usage" in expected:
            expected["usage"].setdefault("projected_exhaust_at", None)
        assert _canonical(serialized) == _canonical(expected)
        _assert_valid("source.schema.json", serialized)

    for raw_example in _schema("agent-supply.schema.json")["examples"]:
        example = {
            key: value
            for key, value in raw_example.items()
            if key not in {"builtin_models", "standard_vendors"}
        }
        agent = ModelHubAgentSupplyConfig.from_payload(example)
        # `builtin_models` and `standard_vendors` are read-only endpoint
        # projections (v1.2), not persisted config — reconstruct them
        # the way `_agent_payload` merges them onto to_payload().
        serialized = {
            **agent.to_payload(),
            "builtin_models": raw_example.get("builtin_models"),
            "standard_vendors": raw_example.get("standard_vendors"),
        }
        if "routes" not in raw_example:
            serialized.pop("routes", None)
        if "sources" not in raw_example:
            serialized.pop("sources")
        else:
            serialized["sources"]["eligibility"] = raw_example["sources"].get("eligibility")
        assert _canonical(serialized) == _canonical(raw_example)
        _assert_valid("agent-supply.schema.json", serialized)


def test_every_frozen_schema_example_is_valid_and_json_round_trips():
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema, format_checker=FormatChecker())
        for example in schema.get("examples", []):
            validator.validate(example)
            assert _canonical(json.loads(_canonical(example))) == _canonical(example)


def test_agent_supply_contract_accepts_unmapped_native_alias_selection():
    payload = {
        "backend": "claude",
        "mode": "hub",
        "menu_kind": "fixed",
        "selected_by_agent": None,
        "selected_model_id": "claude-opus-4-5",
        "selected_model_explicit": True,
        "sources": {
            "order": ["src_anthropic1"],
            "eligibility": [
                {
                    "source_id": "src_anthropic1",
                    "eligible": True,
                    "reason_key": None,
                    "in_current_model_chain": True,
                    "process_availability_reason": None,
                }
            ],
        },
        "supply_status": "ok",
        "menu": None,
        "model_supply": [
            {"model_id": "claude-opus-4-5", "chain_length": 1}
        ],
        "builtin_models": ["claude-opus-4-5"],
        "standard_vendors": None,
        "named_agents": [],
    }

    _assert_valid("agent-supply.schema.json", payload)


def test_v5_mirror_registry_is_executable_and_complete():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    schemas = _mirror_schemas(registry)

    assert registry["contract_version"] == 5
    ids = [entry["id"] for entry in registry["entries"]]
    assert ids
    assert len(ids) == len(set(ids))
    for entry in registry["entries"]:
        _validate_mirror_entry(entry, schemas)


def test_v5_mirror_registry_mutation_probes_detect_every_comparable_drift():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    schemas = _mirror_schemas(registry)

    for entry in registry["entries"]:
        if entry["rule"] == "none":
            continue
        if entry["rule"] == "bijection":
            locations = [(location, location["paths"][0]) for location in entry["target_sets"]]
        elif entry["rule"] == "equality":
            locations = [(location, pointer) for location in entry["sets"] for pointer in location["paths"][:1]]
        elif entry["rule"] == "mapping":
            targets = entry.get("targets")
            if targets is None:
                targets = [entry["target"]]
            locations = [
                (entry["home"], entry["home"]["path"]),
                *[(location, location["path"]) for location in targets],
            ]
        elif entry["rule"] == "partition":
            locations = [
                (entry["home"], entry["home"]["path"]),
                (entry["member"], entry["member"]["path"]),
            ]
        else:
            locations = [
                (entry["home"], entry["home"]["path"]),
                *[(location, location["path"]) for location in entry["targets"]],
            ]

        for location, pointer in locations:
            mutated = copy.deepcopy(schemas)
            node = _json_pointer(mutated[location["schema"]], pointer)
            if "enum" in node:
                node["enum"].append(f"mutation_{entry['id'].lower()}")
            else:
                assert "const" in node, entry["id"]
                node["const"] = f"mutation_{entry['id'].lower()}"

            with pytest.raises(AssertionError):
                _validate_mirror_entry(entry, mutated)


def test_model_hub_authority_closure_is_generated_from_live_files():
    result = check_model_hub_authorities(Path.cwd())
    assert result["input_mode"] == "same_run_live_files"
    assert result["input_fingerprint"]
    assert result["ok"], result["findings"]


def test_targeted_permission_denial_contract_is_request_scoped_and_mirrored():
    event_schema = _schema("resolution-event.schema.json")
    event_validator = Draft7Validator(event_schema)
    permission_event = next(
        example
        for example in event_schema["examples"]
        if example["reason"] == "permission_denied"
    )
    event_validator.validate(permission_event)

    invalid_cooldown = {
        **permission_event,
        "kind": "cooldown",
        "to_source": None,
    }
    with pytest.raises(ValidationError):
        event_validator.validate(invalid_cooldown)

    provenance_schema = _schema("turn-provenance.schema.json")
    assert provenance_schema["properties"]["contract_version"]["const"] == 5
    permission_record = next(
        example
        for example in provenance_schema["examples"]
        if any(
            attempt["reason"] == "permission_denied"
            for attempt in example["failed_attempts"]
        )
    )
    Draft7Validator(provenance_schema).validate(permission_record)

    registry = json.loads(
        (CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8")
    )
    schemas = _mirror_schemas(registry)
    failed_reasons = _json_pointer(
        schemas["turn-provenance.schema.json"],
        "/properties/failed_attempts/items/properties/reason",
    )["enum"]
    failed_reasons.remove("permission_denied")
    with pytest.raises(AssertionError):
        _validate_mirror_entry(
            next(entry for entry in registry["entries"] if entry["id"] == "M3"),
            schemas,
        )


def test_v5_shape_amendments_reject_the_false_states_they_replace():
    supply_schema = _schema("agent-supply.schema.json")
    supply_validator = Draft7Validator(supply_schema)
    base_supply = {
        "backend": "claude",
        "mode": "hub",
        "menu_kind": "fixed",
        "selected_by_agent": None,
        "selected_model_id": None,
        "selected_model_explicit": False,
        "sources": {"order": [], "eligibility": []},
        "supply_status": None,
        "menu": None,
        "model_supply": [],
        "named_agents": [],
        "builtin_models": [],
        "standard_vendors": None,
    }
    supply_validator.validate(base_supply)

    invented = {
        **base_supply,
        "selected_model_id": None,
        "current": {
            "model_id": "claude-opus-4-6",
            "source_id": "src_anthkey01",
            "channel": "hub",
        },
    }
    with pytest.raises(ValidationError):
        supply_validator.validate(invented)

    invalid_explicitness = {
        **base_supply,
        "selected_model_explicit": True,
    }
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_explicitness)

    invalid_reason = copy.deepcopy(base_supply)
    invalid_reason["sources"]["eligibility"] = [
        {
            "source_id": "src_anthkey01",
            "eligible": False,
            "reason_key": "models.eligibility.subscription_wrong_clint",
        }
    ]
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_reason)

    signal_supply = copy.deepcopy(base_supply)
    signal_supply.update(
        {
            "selected_model_id": "claude-opus-4-6",
            "supply_status": "degraded",
        }
    )
    signal_supply["sources"] = {
        "order": ["src_anthkey01"],
        "eligibility": [
            {
                "source_id": "src_claudepro1",
                "eligible": True,
                "reason_key": None,
                "in_current_model_chain": True,
                "process_availability_reason": "native_cli_unavailable",
            },
            {
                "source_id": "src_anthkey01",
                "eligible": True,
                "reason_key": None,
                "in_current_model_chain": True,
                "process_availability_reason": None,
            },
            {
                "source_id": "src_otherkey1",
                "eligible": True,
                "reason_key": None,
                "in_current_model_chain": False,
                "process_availability_reason": None,
            },
        ],
    }
    supply_validator.validate(signal_supply)

    invalid_availability = copy.deepcopy(signal_supply)
    invalid_availability["sources"]["eligibility"][0]["process_availability_reason"] = "gateway_down"
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_availability)

    invalid_membership = copy.deepcopy(signal_supply)
    invalid_membership["sources"]["eligibility"][2]["in_current_model_chain"] = "not_supplying"
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_membership)

    event_validator = Draft7Validator(_schema("resolution-event.schema.json"))
    source_wide = _schema("resolution-event.schema.json")["examples"][1]
    event_validator.validate(source_wide)
    invalid_event = {**source_wide, "agent": "system", "kind": "supply_interrupted"}
    with pytest.raises(ValidationError):
        event_validator.validate(invalid_event)

    probe = copy.deepcopy(_schema("probe-result.schema.json")["examples"][0])
    probe["source_id"] = "direct"
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("probe-result.schema.json")).validate(probe)

    canceled = next(
        example
        for example in _schema("turn-provenance.schema.json")["examples"]
        if example["outcome"] == "canceled"
    )
    Draft7Validator(_schema("turn-provenance.schema.json")).validate(canceled)
    invented_failure = copy.deepcopy(canceled)
    invented_failure["canceled_attempt"]["reason"] = "server_error"
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("turn-provenance.schema.json")).validate(invented_failure)

    chain_schema = _schema("agent-chain.schema.json")
    chain_validator = Draft7Validator(chain_schema)
    for example in chain_schema["examples"]:
        chain_validator.validate(example)
    exact_hop = {
        "contract_version": 5,
        "backend": "claude",
        "model_id": "claude-opus-4-6",
        "chain": [{
            "source_id": "src_claudepro1",
            "model_id": "claude-opus-4-6",
            "channel": "native_cli",
            "health": "healthy",
            "runnable": True,
            "reason": None,
            "retry_at": None,
        }],
        "current": {"source_id": "src_claudepro1", "model_id": "claude-opus-4-6"},
        "supply_state": "ok",
    }
    chain_validator.validate(exact_hop)
    for retired in ("via_mapping", "resolved_model_id"):
        invalid = copy.deepcopy(exact_hop)
        invalid["chain"][0][retired] = False if retired == "via_mapping" else "old"
        with pytest.raises(ValidationError):
            chain_validator.validate(invalid)
    invalid_reason = copy.deepcopy(exact_hop)
    invalid_reason["chain"][0]["reason"] = "invented"
    with pytest.raises(ValidationError):
        chain_validator.validate(invalid_reason)

    probe_schema = _schema("probe-result.schema.json")
    probe_validator = Draft7Validator(probe_schema)
    native_ready = copy.deepcopy(probe_schema["examples"][-2])
    probe_validator.validate(native_ready)
    native_ready["latency_ms"] = 12
    with pytest.raises(ValidationError):
        probe_validator.validate(native_ready)

    native_not_ready = copy.deepcopy(probe_schema["examples"][-1])
    probe_validator.validate(native_not_ready)
    native_not_ready_without_reason = copy.deepcopy(native_not_ready)
    native_not_ready_without_reason["error"] = None
    with pytest.raises(ValidationError):
        probe_validator.validate(native_not_ready_without_reason)

    timed_native_not_ready = copy.deepcopy(native_not_ready)
    timed_native_not_ready["latency_ms"] = 12
    with pytest.raises(ValidationError):
        probe_validator.validate(timed_native_not_ready)


def test_model_hub_config_round_trip_and_serializer_completeness(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source_example = {
        **_schema("source.schema.json")["examples"][0],
        "supply_channel": "hub",
        "credential_ref": "cred_serializer_test",
    }
    hub_payload = {
        "sources": [source_example],
        "agents": {
            backend: ModelHubAgentSupplyConfig.default(backend, mode="hub").to_payload()
            for backend in ("claude", "codex", "opencode")
        },
    }
    config = default_config()
    config.model_hub = ModelHubConfig.from_payload(hub_payload)
    config.save()

    loaded = V2Config.load()
    disk_payload = json.loads(Path(tmp_path, "config", "config.json").read_text(encoding="utf-8"))
    api_payload = api.config_to_payload(loaded)
    expected_root = {field.name for field in fields(ModelHubConfig)}
    source_fields = {field.name for field in fields(ModelHubSourceConfig)}
    source_state_fields = {field.name for field in fields(ModelHubSourceStateConfig)}
    source_usage_fields = {field.name for field in fields(ModelHubSourceUsageConfig)}
    source_model_fields = {field.name for field in fields(ModelHubModelConfig)}
    source_model_fields.discard("provenance")
    source_model_fields.add("origin")
    agent_fields = {field.name for field in fields(ModelHubAgentSupplyConfig)}
    agent_sources_fields = {field.name for field in fields(ModelHubAgentSourcesConfig)}
    route_fields = {field.name for field in fields(ModelHubRouteConfig)}
    menu_fields = {field.name for field in fields(ModelHubMenuConfig)}

    assert expected_root == set(api_payload["model_hub"])
    assert expected_root == set(disk_payload["model_hub"])
    for label, serialized_hub in (
        ("config_to_payload", api_payload["model_hub"]),
        ("V2Config.save", disk_payload["model_hub"]),
    ):
        serialized_source = serialized_hub["sources"][0]
        assert source_fields == set(serialized_source), label
        assert serialized_source["last_discovered_at"] == source_example["last_discovered_at"], label
        assert source_state_fields == set(serialized_source["state"]), label
        assert source_usage_fields == set(serialized_source["usage"]), label
        assert source_model_fields == set(serialized_source["models"][0]), label
        assert agent_fields == set(serialized_hub["agents"]["claude"]), label
        assert agent_sources_fields == set(serialized_hub["agents"]["claude"]["sources"]), label
        assert route_fields == set(ModelHubRouteConfig().to_payload()), label
        assert menu_fields == set(serialized_hub["agents"]["opencode"]["menu"]), label

    stale_hub_payload = json.loads(json.dumps(api_payload["model_hub"]))
    stale_hub_payload["priority_order"] = ["legacy-is-dropped"]
    updated = api.save_config({"show_duration": True, "model_hub": stale_hub_payload})
    assert updated.model_hub.to_payload() == loaded.model_hub.to_payload()
    assert api.config_to_payload(updated)["model_hub"] == api_payload["model_hub"]


def test_legacy_and_fresh_configs_both_default_direct():
    payload = api.config_to_payload(default_config(), include_secrets=True)
    payload.pop("model_hub")
    legacy = V2Config.from_payload(payload)

    assert {agent.mode for agent in legacy.model_hub.agents.values()} == {"direct"}
    assert {agent.mode for agent in default_config().model_hub.agents.values()} == {"direct"}


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_model_hub_release_capability_accepts_explicit_truthy_values(value):
    assert is_model_hub_enabled({MODEL_HUB_ENABLED_ENV: value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "unexpected"])
def test_model_hub_release_capability_defaults_and_fails_closed(value):
    assert is_model_hub_enabled({MODEL_HUB_ENABLED_ENV: value}) is False


def test_final_config_rejects_retired_consent_metadata():
    source = _schema("source.schema.json")["examples"][0]
    source = {**source, "supply_channel": "hub"}
    payload = {
        "sources": [source],
        "agents": {},
    }

    payload["sources"][0]["experimental_consent_at"] = "2026-07-23T03:00:00Z"
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)


def test_final_config_rejects_retired_global_priority_key():
    payload = ModelHubConfig().to_payload()
    payload["priority_order"] = {"legacy": "shape-does-not-matter"}

    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)


def test_agent_source_orders_validate_existence_eligibility_and_uniqueness():
    source = {
        **_schema("source.schema.json")["examples"][0],
        "created_at": "2026-07-29T01:00:00Z",
    }
    base = ModelHubConfig().to_payload()
    base["sources"] = [source]
    base["agents"]["claude"]["sources"] = {
        "order": [source["id"]],
    }

    ModelHubConfig.from_payload(base)

    for invalid_order in (
        [source["id"], source["id"]],
        ["src_missing001"],
    ):
        invalid = json.loads(json.dumps(base))
        invalid["agents"]["claude"]["sources"]["order"] = invalid_order
        with pytest.raises(ValueError):
            ModelHubConfig.from_payload(invalid)

    ineligible = json.loads(json.dumps(base))
    ineligible["agents"]["codex"]["sources"] = {
        "order": [source["id"]],
    }
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(ineligible)


def test_route_hops_allow_one_source_to_supply_distinct_models():
    route = ModelHubRouteConfig.from_payload(
        {
            "hops": [
                {"source_id": "src_same0001", "model_id": "model-a"},
                {"source_id": "src_same0001", "model_id": "model-b"},
            ]
        }
    )

    assert [(hop.source_id, hop.model_id) for hop in route.hops] == [
        ("src_same0001", "model-a"),
        ("src_same0001", "model-b"),
    ]
    source = ModelHubSourceConfig.from_payload(
        _schema("source.schema.json")["examples"][0]
    )
    source.models.append(
        ModelHubModelConfig(id="claude-opus-4-5", provenance="manual")
    )
    config = ModelHubConfig(sources=[source])
    config.agents["claude"].sources.order = [source.id]
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig.from_payload(
        {
            "hops": [
                {"source_id": source.id, "model_id": "claude-opus-4-6"},
                {"source_id": source.id, "model_id": "claude-opus-4-5"},
            ]
        }
    )

    canonical = ModelHubConfig.from_payload(config.to_payload())

    assert [hop.model_id for hop in canonical.agents["claude"].routes["claude-opus-4-6"].hops] == [
        "claude-opus-4-6",
        "claude-opus-4-5",
    ]
    with pytest.raises(ValueError, match="unique pairs"):
        ModelHubRouteConfig.from_payload(
            {
                "hops": [
                    {"source_id": "src_same0001", "model_id": "model-a"},
                    {"source_id": "src_same0001", "model_id": "model-a"},
                ]
            }
        )


def _ordering_source(
    source_id: str,
    *,
    kind: str,
    vendor: str,
    channel: str,
    created_at: str = MODEL_HUB_LEGACY_CREATED_AT,
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor=vendor,
        display_name=source_id,
        protocol="anthropic" if vendor == "anthropic" else "openai_responses",
        supply_channel=channel,
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[],
        created_at=created_at,
    )


def test_recommended_order_is_backend_native_then_created_at_and_id():
    native = _ordering_source(
        "src_nativeaaa",
        kind="subscription",
        vendor="anthropic",
        channel="native_cli",
    )
    hub_subscription = _ordering_source(
        "src_hubsubaaa",
        kind="subscription",
        vendor="anthropic",
        channel="hub",
    )
    wrong_vendor_subscription = _ordering_source(
        "src_openaisub",
        kind="subscription",
        vendor="openai",
        channel="native_cli",
    )
    legacy_b = _ordering_source(
        "src_legacybbb",
        kind="api_key",
        vendor="openai",
        channel="hub",
    )
    legacy_a = _ordering_source(
        "src_legacyaaa",
        kind="api_key",
        vendor="anthropic",
        channel="hub",
    )
    newer = _ordering_source(
        "src_newer0001",
        kind="api_key",
        vendor="custom",
        channel="hub",
        created_at="2026-07-29T03:00:00Z",
    )
    same_time_b = _ordering_source(
        "src_tiebbbb1",
        kind="api_key",
        vendor="custom",
        channel="hub",
        created_at="2026-07-29T04:00:00Z",
    )
    same_time_a = _ordering_source(
        "src_tieaaaa1",
        kind="api_key",
        vendor="custom",
        channel="hub",
        created_at="2026-07-29T04:00:00Z",
    )
    config = ModelHubConfig(
        sources=[
            same_time_b,
            newer,
            wrong_vendor_subscription,
            legacy_b,
            hub_subscription,
            same_time_a,
            native,
            legacy_a,
        ],
    )

    assert config.recommended_source_order("claude") == [
        native.id,
        hub_subscription.id,
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]
    assert config.recommended_source_order("codex") == [
        wrong_vendor_subscription.id,
        hub_subscription.id,
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]
    assert config.recommended_source_order("opencode") == [
        hub_subscription.id,
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]


def test_persisted_hub_config_requires_explicit_complete_route_rows():
    payload = ModelHubConfig().to_payload()

    missing = json.loads(json.dumps(payload))
    del missing["agents"]["claude"]["routes"]
    with pytest.raises(ValueError, match="routes.*required"):
        ModelHubConfig.from_payload(missing)

    non_object = json.loads(json.dumps(payload))
    non_object["agents"]["claude"]["routes"] = []
    with pytest.raises(ValueError, match="routes.*object"):
        ModelHubConfig.from_payload(non_object)

    incomplete = json.loads(json.dumps(payload))
    incomplete["agents"]["claude"]["routes"].pop("claude-opus-4-6")
    with pytest.raises(ValueError, match="missing menu model"):
        ModelHubConfig.from_payload(incomplete)

    extra = json.loads(json.dumps(payload))
    extra["agents"]["claude"]["routes"]["claude-hidden-model"] = {
        "hops": []
    }
    with pytest.raises(ValueError, match="contains non-menu model"):
        ModelHubConfig.from_payload(extra)

    dynamic = json.loads(json.dumps(payload))
    dynamic["agents"]["opencode"]["mode"] = "hub"
    dynamic["agents"]["opencode"]["menu"] = {
        "view": "featured",
        "checked": ["custom/model"],
    }
    dynamic["agents"]["opencode"]["routes"] = {}
    with pytest.raises(ValueError, match="missing menu model 'custom/model'"):
        ModelHubConfig.from_payload(dynamic)


def test_config_reload_migrates_fixed_routes_when_bundled_catalog_changes(monkeypatch, tmp_path):
    payload = api.config_to_payload(default_config())
    original_ids = tuple(payload["model_hub"]["agents"]["claude"]["routes"])
    removed_id = original_ids[0]
    added_id = "claude-catalog-added-after-save"
    stale_id = "claude-catalog-removed-after-save"
    routes = payload["model_hub"]["agents"]["claude"]["routes"]
    routes.pop(removed_id)
    routes[stale_id] = {"hops": []}

    def changed_catalog_ids(backend: str) -> tuple[str, ...]:
        if backend == "claude":
            return (*original_ids[1:], added_id)
        return tuple(payload["model_hub"]["agents"][backend]["routes"])

    monkeypatch.setattr("config.v2_config.model_hub_fixed_menu_ids", changed_catalog_ids)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)
    migrated_routes = loaded.model_hub.agents["claude"].routes

    assert set(migrated_routes) == set(changed_catalog_ids("claude"))
    assert migrated_routes[added_id].hops == ()
    assert removed_id not in migrated_routes
    assert stale_id not in migrated_routes


def _legacy_model_hub_payload(payload: dict) -> dict:
    """Rewrite a current config payload into the pre-v5 persisted ``model_hub`` shape.

    Derived from the live default rather than a frozen literal, so every agent
    shape that exists today is seeded and a backend added later is covered
    without editing this helper.
    """

    legacy = copy.deepcopy(payload)
    model_hub = legacy["model_hub"]
    model_hub["subscription_hub_experimental"] = False
    for agent in model_hub["agents"].values():
        agent.pop("routes")
        agent["mappings"] = []
        agent["sources"] = {"policy": "follow", **agent["sources"]}
    return legacy


def test_mh_cfg_mig_001_pre_v5_model_hub_config_still_loads(tmp_path):
    current = api.config_to_payload(default_config())
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_legacy_model_hub_payload(current)), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == current["model_hub"]


def test_mh_cfg_mig_001_pre_v5_mapping_becomes_a_route_over_ordered_sources(tmp_path, caplog):
    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["models"].append({**source["models"][0], "id": "target-model"})
    legacy["model_hub"]["sources"] = [source]
    claude = legacy["model_hub"]["agents"]["claude"]
    claude["sources"] = {"policy": "custom", "order": [source["id"]]}
    mapped_id, disabled_id, *_ = tuple(current["model_hub"]["agents"]["claude"]["routes"])
    claude["mappings"] = [
        {"builtin_id": mapped_id, "target_model_id": "target-model", "enabled": True},
        {"builtin_id": disabled_id, "target_model_id": "never-routed", "enabled": False},
        {"builtin_id": "retired-menu-model", "target_model_id": "target-model", "enabled": True},
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        routes = V2Config.load(config_path=config_path).model_hub.agents["claude"].routes

    assert set(routes) == set(current["model_hub"]["agents"]["claude"]["routes"])
    assert [(hop.source_id, hop.model_id) for hop in routes[mapped_id].hops] == [
        (source["id"], "target-model")
    ]
    assert routes[disabled_id].hops == ()
    # A mapping for a model the current menu no longer offers is reported, not
    # silently swallowed with the mappings it retired.
    assert [
        record.getMessage()
        for record in caplog.records
        if "retired-menu-model" in record.getMessage()
    ] == [
        "Model Hub migration dropped a 'claude' mapping for 'retired-menu-model': "
        "the model is no longer offered"
    ]


def test_mh_cfg_mig_001_pre_v5_root_keys_the_v5_contract_dropped_never_block_startup(tmp_path):
    """The pre-v5 parser read two root keys and silently discarded the rest.

    ``priority_order`` is the known one, but a build old enough to persist it
    can carry others, so migration narrows the root to the v5 contract instead
    of popping known names.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    legacy["model_hub"]["priority_order"] = {"legacy": "shape-does-not-matter"}
    legacy["model_hub"]["a_key_this_build_never_heard_of"] = ["anything"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert set(loaded.model_hub.to_payload()) == {"sources", "agents"}


def test_current_model_hub_config_still_rejects_an_unknown_root_key(tmp_path):
    """Migration must not soften the v5 parse for a config that is already v5.

    The MH-CFG-MIG-001 boundary: an unknown root key in a v5 config is a typo or
    a corrupted write, not a legacy shape, and silently dropping it would lose
    whatever it was meant to say.
    """

    current = api.config_to_payload(default_config())
    current["model_hub"]["priority_order"] = ["not-a-legacy-config"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    with pytest.raises(ValueError, match="model_hub"):
        V2Config.load(config_path=config_path)


def test_mh_cfg_mig_001_pre_v5_first_enabled_mapping_wins_including_an_identity_one(tmp_path):
    """The legacy resolver took the first enabled entry for a menu id.

    Nothing enforced unique ids, and an identity mapping counted as explicit —
    it pinned the model to a source stocking that exact id rather than letting
    an alias resolve — so a later duplicate must not reroute it on upgrade.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    identity_id = source["models"][0]["id"]
    source["models"].append({**source["models"][0], "id": "later-target"})
    legacy["model_hub"]["sources"] = [source]
    claude = legacy["model_hub"]["agents"]["claude"]
    claude["sources"] = {"policy": "custom", "order": [source["id"]]}
    claude["mappings"] = [
        {"builtin_id": identity_id, "target_model_id": identity_id, "enabled": True},
        {"builtin_id": identity_id, "target_model_id": "later-target", "enabled": True},
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["claude"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes[identity_id].hops] == [
        (source["id"], identity_id)
    ]


def test_mh_cfg_mig_001_pre_v5_mapping_skips_sources_that_do_not_stock_the_target(tmp_path):
    """The legacy walk skipped a source whose inventory lacked the mapped target.

    Migrating a hop onto it anyway would leave the route permanently reported as
    degraded over a chain that never resolved.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    empty, stocked = (
        copy.deepcopy(source) for source in _schema("source.schema.json")["examples"][:2]
    )
    stocked["models"] = [{**stocked["models"][0], "id": "target-model"}]
    mapped_id = next(iter(current["model_hub"]["agents"]["claude"]["routes"]))
    legacy["model_hub"]["sources"] = [empty, stocked]
    claude = legacy["model_hub"]["agents"]["claude"]
    claude["sources"] = {"policy": "custom", "order": [empty["id"], stocked["id"]]}
    claude["mappings"] = [
        {"builtin_id": mapped_id, "target_model_id": "target-model", "enabled": True}
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["claude"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes[mapped_id].hops] == [
        (stocked["id"], "target-model")
    ]


def test_mh_cfg_mig_001_pre_v5_follow_policy_recomputes_the_source_order(tmp_path):
    """A legacy ``follow`` order was recomputed on load, so it may be stale.

    Freezing the persisted list into an explicit v5 route would drop a source
    the recommendation had already promoted ahead of it.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    native, relay = (
        copy.deepcopy(source) for source in _schema("source.schema.json")["examples"][:2]
    )
    supplied_id = native["models"][0]["id"]
    legacy["model_hub"]["sources"] = [native, relay]
    claude = legacy["model_hub"]["agents"]["claude"]
    claude["sources"] = {"policy": "follow", "order": [relay["id"]]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    claude_config = V2Config.load(config_path=config_path).model_hub.agents["claude"]

    # The native subscription outranks the metered relay and is walked first,
    # even though the stored order never mentioned it.
    assert [(hop.source_id, hop.model_id) for hop in claude_config.routes[supplied_id].hops] == [
        (native["id"], supplied_id)
    ]
    # v5 has no follow policy to recompute it again, so the walk the routes were
    # built from is also what settings must read back.
    assert claude_config.sources.order == [native["id"], relay["id"]]


def test_mh_cfg_mig_001_pre_v5_custom_policy_keeps_its_source_order(tmp_path):
    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    native, relay = (
        copy.deepcopy(source) for source in _schema("source.schema.json")["examples"][:2]
    )
    supplied_id = native["models"][0]["id"]
    relay["models"] = [
        {**model, "origin": "discovered", "discovered_at": native["last_discovered_at"]}
        for model in native["models"]
    ]
    legacy["model_hub"]["sources"] = [native, relay]
    claude = legacy["model_hub"]["agents"]["claude"]
    claude["sources"] = {"policy": "custom", "order": [relay["id"], native["id"]]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["claude"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes[supplied_id].hops] == [
        (relay["id"], supplied_id),
        (native["id"], supplied_id),
    ]


def test_mh_cfg_mig_001_pre_v5_retired_consent_metadata_never_blocks_migration(tmp_path):
    """Retired per-source consent metadata is stripped before routes are built.

    An unstripped source fails to parse, and a source that does not parse
    supplies nothing, so leaving it in place would both refuse the config and
    quietly migrate every route to empty.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["experimental_consent_at"] = "2026-07-23T03:00:00Z"
    supplied_id = source["models"][0]["id"]
    legacy["model_hub"]["sources"] = [source]
    legacy["model_hub"]["agents"]["claude"]["sources"]["order"] = [source["id"]]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert "experimental_consent_at" not in loaded.model_hub.to_payload()["sources"][0]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(source["id"], supplied_id)]


def test_pre_v5_only_model_hub_fields_are_all_migrated(tmp_path):
    """Every field the v5 dataclasses retired must be handled by the migration.

    The completeness guard behind MH-CFG-MIG-001: a retired field that migration
    forgets is not a cosmetic gap, because the parser rejects unknown fields and
    the install fails to start on upgrade.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["experimental_consent_at"] = None
    legacy["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    retired = {"subscription_hub_experimental", "experimental_consent_at", "policy", "mappings"}

    def keys(payload: object) -> set[str]:
        if isinstance(payload, dict):
            return set(payload) | {key for value in payload.values() for key in keys(value)}
        if isinstance(payload, list):
            return {key for item in payload for key in keys(item)}
        return set()

    assert retired <= keys(legacy["model_hub"])
    assert retired & keys(V2Config.load(config_path=config_path).model_hub.to_payload()) == set()


def test_mh_cfg_mig_001_pre_v5_unmapped_menu_models_keep_their_legacy_route(tmp_path):
    """An unmapped menu model walked the same order, so it must stay routable.

    A Hub-mode install that never needed a mapping is the common shape; leaving
    those models with empty routes would migrate it into a service that starts
    with nothing left to run.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = source["models"][0]["id"]
    legacy["model_hub"]["sources"] = [source]
    claude = legacy["model_hub"]["agents"]["claude"]
    claude["sources"]["order"] = [source["id"]]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["claude"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes[supplied_id].hops] == [
        (source["id"], supplied_id)
    ]
    # The family alias resolved to the newest discovered model, not to itself.
    assert [(hop.source_id, hop.model_id) for hop in routes["opus"].hops] == [
        (source["id"], supplied_id)
    ]
    assert routes["sonnet"].hops == ()


def test_mh_cfg_mig_001_pre_v5_manually_added_model_stays_routable(tmp_path):
    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    menu_id = next(iter(current["model_hub"]["agents"]["codex"]["routes"]))
    source["models"] = [
        {
            "id": menu_id,
            "display_name": None,
            "origin": "manual",
            "reasoning_efforts": [],
            "discovered_at": None,
        }
    ]
    legacy["model_hub"]["sources"] = [source]
    codex = legacy["model_hub"]["agents"]["codex"]
    codex["sources"]["order"] = [source["id"]]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["codex"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes[menu_id].hops] == [
        (source["id"], menu_id)
    ]


def test_mh_cfg_mig_001_pre_v5_open_menu_keeps_checked_models_routable(tmp_path):
    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    model_id = source["models"][0]["id"]
    source["models"][0] |= {
        "origin": "discovered",
        "discovered_at": source["state"]["retry_at"],
    }
    checked_id = opencode_model_id(source["vendor"], model_id)
    legacy["model_hub"]["sources"] = [source]
    opencode = legacy["model_hub"]["agents"]["opencode"]
    opencode["sources"]["order"] = [source["id"]]
    opencode["menu"] = {"view": "featured", "checked": [checked_id]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["opencode"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes[checked_id].hops] == [
        (source["id"], model_id)
    ]


def test_mh_cfg_mig_001_pre_v5_manually_added_opencode_model_stays_routable(tmp_path):
    """A checked OpenCode model the user typed in was routable before the upgrade.

    Add-time matching ignores a manual model by design, so the migration falls
    back to the source inventory under the same canonical ``provider/model``
    identity the menu stores.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    model_id = source["models"][0]["id"]
    assert source["models"][0]["origin"] == "manual"
    checked_id = opencode_model_id(source["vendor"], model_id)
    legacy["model_hub"]["sources"] = [source]
    opencode = legacy["model_hub"]["agents"]["opencode"]
    opencode["sources"]["order"] = [source["id"]]
    opencode["menu"] = {"view": "featured", "checked": [checked_id]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["opencode"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes[checked_id].hops] == [
        (source["id"], model_id)
    ]


def test_mh_cfg_mig_001_pre_v5_mapping_without_eligible_source_degrades_to_empty_route(
    tmp_path,
    caplog,
):
    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    claude = legacy["model_hub"]["agents"]["claude"]
    mapped_id = next(iter(current["model_hub"]["agents"]["claude"]["routes"]))
    # The legacy mapping parser accepted any non-empty target, so the value can
    # be credential-shaped and must never reach the application log.
    secret_target = "api_key=sk-live-should-never-be-logged"
    claude["mappings"] = [
        {"builtin_id": mapped_id, "target_model_id": secret_target, "enabled": True}
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == current["model_hub"]
    assert any(mapped_id in record.getMessage() for record in caplog.records)
    assert not any("sk-live" in record.getMessage() for record in caplog.records)


def test_mh_cfg_mig_001_pre_v5_agent_for_a_removed_backend_never_blocks_startup(tmp_path):
    """A legacy agents map can name a backend this build no longer supports.

    The pre-v5 parser built only the backends it knew and ignored the rest,
    while v5 rejects the whole config over the extra key, so a leftover entry
    would turn an upgrade into a service that refuses to start.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    agents = legacy["model_hub"]["agents"]
    agents["a_backend_this_build_removed"] = copy.deepcopy(agents["claude"])
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert set(loaded.model_hub.agents) == set(MODEL_HUB_BACKENDS)


def test_mh_cfg_mig_001_pre_v5_source_the_v5_contract_cannot_hold_is_dropped(tmp_path, caplog):
    """v5 added cross-field source invariants the pre-v5 parser never enforced.

    A hub source persisted without an engine credential ref was legal then and
    is unrepresentable now. It is dropped so the rest of the config still loads,
    and it must also leave the agent order it appeared in — a persisted order
    naming a source that no longer exists fails the same load it was rescued
    for.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    kept = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = kept["models"][0]["id"]
    dropped = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    dropped["credential_ref"] = None
    legacy["model_hub"]["sources"] = [dropped, kept]
    claude = legacy["model_hub"]["agents"]["claude"]
    claude["sources"] = {"policy": "custom", "order": [dropped["id"], kept["id"]]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        loaded = V2Config.load(config_path=config_path)

    assert [source.id for source in loaded.model_hub.sources] == [kept["id"]]
    assert list(loaded.model_hub.agents["claude"].sources.order) == [kept["id"]]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(kept["id"], supplied_id)]
    assert [
        record.getMessage() for record in caplog.records if dropped["id"] in record.getMessage()
    ] == [
        f"Model Hub migration dropped source '{dropped['id']}': "
        "the current contract cannot represent it"
    ]


def test_mh_cfg_mig_001_pre_v5_unreadable_source_is_dropped_without_logging_its_body(
    tmp_path,
    caplog,
):
    """A source too broken to name is still dropped, and nothing of it is logged.

    Everything in a rejected source is unvalidated config text that can carry
    credential material, so only an id that matches the persisted id shape is
    ever repeated into the application log.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    broken = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    broken["id"] = "sk-live-should-never-be-logged"
    legacy["model_hub"]["sources"] = [broken]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.sources == []
    assert any("<unreadable id>" in record.getMessage() for record in caplog.records)
    assert not any("sk-live" in record.getMessage() for record in caplog.records)


def test_mh_cfg_mig_001_pre_v5_duplicate_native_sources_collapse_under_a_follow_walk(
    tmp_path,
    caplog,
):
    """v5 allows one native source per backend; the pre-v5 shape allowed several.

    The old OAuth creation path could append a second native source, so this is
    a shape a running install can hold. Neither source is invalid on its own —
    only the aggregate is — so the duplicate is collapsed here instead of
    failing the load. A `follow` agent recomputed its walk, and its
    recommendation ordered the two natives by id.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    kept = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = kept["models"][0]["id"]
    duplicate = copy.deepcopy(kept) | {"id": "src_claudepro2", "account_label": "other@gmail.com"}
    legacy["model_hub"]["sources"] = [kept, duplicate]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        loaded = V2Config.load(config_path=config_path)

    assert [source.id for source in loaded.model_hub.sources] == [kept["id"]]
    assert list(loaded.model_hub.agents["claude"].sources.order) == [kept["id"]]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(kept["id"], supplied_id)]
    assert [
        record.getMessage() for record in caplog.records if duplicate["id"] in record.getMessage()
    ] == [
        f"Model Hub migration dropped source '{duplicate['id']}': "
        "'claude' already has a native source"
    ]


def test_mh_cfg_mig_001_pre_v5_duplicate_natives_keep_the_one_the_agent_walked(tmp_path):
    """Which duplicate survives is decided by the legacy walk, not by position.

    A ``custom`` order could name the second native source and ignore the
    first, so collapsing onto the first persisted would drop the source that
    actually served every turn and migrate the agent to empty routes.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    ignored = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    kept = copy.deepcopy(ignored) | {"id": "src_claudepro2", "account_label": "other@gmail.com"}
    supplied_id = kept["models"][0]["id"]
    legacy["model_hub"]["sources"] = [ignored, kept]
    legacy["model_hub"]["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [kept["id"]],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert [source.id for source in loaded.model_hub.sources] == [kept["id"]]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(kept["id"], supplied_id)]


def test_mh_cfg_mig_001_pre_v5_agent_without_a_sources_section_follows(tmp_path):
    """An omitted ``sources`` section was the legacy ``follow`` default.

    The pre-v5 parser built the default `ModelHubAgentSourcesConfig`, whose
    policy was `follow`, so reading the absent section as a missing order would
    migrate a fully routable Hub agent to nothing.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = source["models"][0]["id"]
    legacy["model_hub"]["sources"] = [source]
    legacy["model_hub"]["agents"]["claude"].pop("sources")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    claude = V2Config.load(config_path=config_path).model_hub.agents["claude"]

    assert list(claude.sources.order) == [source["id"]]
    assert [(hop.source_id, hop.model_id) for hop in claude.routes[supplied_id].hops] == [
        (source["id"], supplied_id)
    ]


def test_mh_cfg_mig_001_pre_v5_agent_fields_the_v5_contract_dropped_never_block_startup(tmp_path):
    """The pre-v5 agent parser read the fields it knew and ignored the rest.

    An install can therefore carry agent metadata this build has never heard
    of, which v5 rejects, so agent objects are narrowed to the v5 keys just as
    the root is.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    legacy["model_hub"]["agents"]["claude"]["a_key_this_build_never_heard_of"] = ["anything"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert set(loaded.model_hub.agents["claude"].to_payload()) == {
        "backend",
        "mode",
        "menu_kind",
        "sources",
        "routes",
        "menu",
    }


def test_mh_cfg_mig_001_pre_v5_hub_subscription_stays_on_its_own_backend(tmp_path):
    """Migration walks the sources the *old* eligibility rule offered.

    v5 makes every hub source eligible for every backend; pre-v5 made only
    API-key hub sources universal and kept hub subscriptions to their vendor's
    backend. Reading the old config with the new rule would invent supply paths
    that installation never had.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0]) | {
        "supply_channel": "hub",
        "credential_ref": "cred_hubsub01",
    }
    supplied_id = source["models"][0]["id"]
    legacy["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert list(loaded.model_hub.agents["claude"].sources.order) == [source["id"]]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(source["id"], supplied_id)]
    for backend in ("codex", "opencode"):
        agent = loaded.model_hub.agents[backend]
        assert list(agent.sources.order) == []
        assert all(route.hops == () for route in agent.routes.values())


def test_legacy_source_eligibility_is_narrower_than_the_live_rule():
    """The frozen pre-v5 rule must never admit what the live rule refuses.

    The invariant MH-CFG-MIG-001 rests on: a migrated `sources.order` is
    validated on the way back in by the live
    eligibility rule, so the moment the frozen copy is wider than it, migration
    writes an order that fails the load it was rescuing.
    """

    sources = [
        _ordering_source(
            f"src_probe{index:04d}",
            kind=kind,
            vendor=vendor,
            channel=channel,
        )
        for index, (kind, vendor, channel) in enumerate(
            product(
                ("subscription", "api_key"),
                # The non-canonical spellings are swept too: the frozen rule
                # reads the vendor exactly as it was persisted, so they are
                # part of its domain.
                ("anthropic", "openai", "custom", "Anthropic", " openai "),
                ("native_cli", "hub"),
            )
        )
    ]
    admitted = [
        (source.id, backend)
        for source, backend in product(sources, MODEL_HUB_BACKENDS)
        if _legacy_source_eligible_for_backend(source, backend)
        and not ModelHubConfig.source_eligible_for_backend(source, backend)
    ]

    assert admitted == []


def test_mh_cfg_mig_001_pre_v5_falsy_agent_entry_loads_the_default_agent(tmp_path):
    """The pre-v5 parser read ``agents_payload.get(backend) or <default>``.

    A persisted ``null`` therefore meant the default direct agent, and carrying
    it through would now fail the load over a value that used to mean nothing.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    legacy["model_hub"]["agents"]["claude"] = None
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.agents["claude"].to_payload() == (
        ModelHubAgentSupplyConfig.default("claude", mode="direct").to_payload()
    )


def test_mh_cfg_mig_001_pre_v5_retired_mapping_id_is_never_logged_verbatim(tmp_path, caplog):
    """A retired ``builtin_id`` is unvalidated config text, like a rejected source id.

    The legacy mapping parser accepted any non-empty string, so an upgrade must
    not move a hand-edited or corrupted credential-shaped value out of the
    config and into the more broadly collected application log.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    secret_id = "api_key=sk-live-should-never-be-logged"
    legacy["model_hub"]["agents"]["claude"]["mappings"] = [
        {"builtin_id": secret_id, "target_model_id": "target-model", "enabled": True}
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        V2Config.load(config_path=config_path)

    assert [record.getMessage() for record in caplog.records if "no longer offered" in record.getMessage()] == [
        "Model Hub migration dropped a 'claude' mapping for '<unreadable id>': "
        "the model is no longer offered"
    ]
    assert not any("sk-live" in record.getMessage() for record in caplog.records)


def test_mh_cfg_mig_001_pre_v5_noncanonical_opencode_menu_entry_is_dropped(tmp_path, caplog):
    """A bare OpenCode selection was unavailable before v5 and is refused after it.

    The pre-v5 menu parser accepted any string, so an entry such as ``gpt-4o``
    could sit in a config that still started. v5 validates every checked id and
    route key as a canonical ``provider/model`` identity, so the entry has to
    leave the menu and the routes together.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    checked_id = opencode_model_id(source["vendor"], source["models"][0]["id"])
    legacy["model_hub"]["sources"] = [source]
    opencode = legacy["model_hub"]["agents"]["opencode"]
    opencode["sources"]["order"] = [source["id"]]
    opencode["menu"] = {"view": "featured", "checked": ["gpt-4o", checked_id]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        loaded = V2Config.load(config_path=config_path)

    agent = loaded.model_hub.agents["opencode"]
    assert list(agent.menu.checked) == [checked_id]
    assert set(agent.routes) == {checked_id}
    assert [
        record.getMessage() for record in caplog.records if "menu selection" in record.getMessage()
    ] == ["Model Hub migration dropped an unusable 'opencode' menu selection"]
    # The rejected entry is never repeated, since the same validator rejects
    # credential material in exactly this position.
    assert not any("gpt-4o" in record.getMessage() for record in caplog.records)


def test_mh_cfg_mig_001_pre_v5_source_fields_the_v5_contract_dropped_never_block_startup(tmp_path):
    """The pre-v5 source parsers read the fields they knew and ignored the rest.

    v5 rejects unknown fields at every level of a source, so a single stale key
    in the source, its state, its usage or one of its models would cost the
    whole source — and with it every route the migration builds through it,
    which is the outage this migration exists to prevent. An ignored field is
    representable simply by dropping it, unlike a credential or channel shape
    v5 has no way to hold.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = source["models"][0]["id"]
    source["a_field_this_build_never_heard_of"] = "anything"
    source["state"]["a_stale_state_field"] = "anything"
    source["usage"]["a_stale_usage_field"] = "anything"
    source["models"][0]["a_stale_model_field"] = "anything"
    legacy["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert [source.id for source in loaded.model_hub.sources] == [source["id"]]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(source["id"], supplied_id)]


def test_mh_cfg_mig_001_pre_v5_model_without_reasoning_efforts_keeps_its_source(tmp_path):
    """``reasoning_efforts`` was optional before v5 and required after it.

    The old parser defaulted an absent key to no efforts at all, so writing
    that same default back is the read it performed. Dropping the source over
    it would take every model it supplies, not just the one field.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = source["models"][0]["id"]
    source["models"][0].pop("reasoning_efforts")
    legacy["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert [model.reasoning_efforts for model in loaded.model_hub.sources[0].models] == [[]]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(source["id"], supplied_id)]


def test_mh_cfg_mig_001_pre_v5_menu_fields_the_v5_contract_dropped_never_block_startup(tmp_path):
    """The pre-v5 menu parser read ``view`` and ``checked`` and ignored the rest."""

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    menu = legacy["model_hub"]["agents"]["opencode"]["menu"]
    menu["a_key_this_build_never_heard_of"] = ["anything"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.agents["opencode"].menu.to_payload() == {
        "view": menu["view"],
        "checked": [],
    }


def test_mh_cfg_mig_001_pre_v5_agent_routes_key_never_outranks_its_mappings(tmp_path):
    """A pre-v5 agent that carries ``routes`` carries something no old build wrote.

    The old contract had no such field: the parser never read one, so its value
    is metadata of unknown origin. A ``null`` would fail the load this
    migration exists to rescue, and an empty object would look valid while
    silently discarding the ``mappings`` that were the real supply.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["models"].append({**source["models"][0], "id": "target-model"})
    legacy["model_hub"]["sources"] = [source]
    mapped_id, *_ = tuple(current["model_hub"]["agents"]["claude"]["routes"])
    legacy["model_hub"]["agents"]["claude"]["mappings"] = [
        {"builtin_id": mapped_id, "target_model_id": "target-model", "enabled": True}
    ]
    legacy["model_hub"]["agents"]["claude"]["routes"] = None
    legacy["model_hub"]["agents"]["codex"]["routes"] = {}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[mapped_id].hops
    ] == [(source["id"], "target-model")]
    assert set(loaded.model_hub.agents["codex"].routes) == set(
        current["model_hub"]["agents"]["codex"]["routes"]
    )


def test_mh_cfg_mig_001_pre_v5_unreadable_routes_are_rebuilt_without_a_legacy_agent_field(tmp_path):
    """A legacy payload can hold an agent whose own fields say nothing.

    The file is pre-v5 by its root, so the agent's ``routes`` are foreign
    whatever they say; when they are also unreadable, keeping them would block
    startup on exactly the config this migration is for.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    claude = legacy["model_hub"]["agents"]["claude"]
    claude.pop("mappings")
    claude["sources"].pop("policy")
    menu_id, *_ = tuple(current["model_hub"]["agents"]["claude"]["routes"])
    claude["routes"] = {menu_id: {"hops": "not-an-array"}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert set(loaded.model_hub.agents["claude"].routes) == set(
        current["model_hub"]["agents"]["claude"]["routes"]
    )


def test_mh_cfg_mig_001_pre_v5_noncanonical_vendor_supplied_nothing_and_still_does(tmp_path):
    """The pre-v5 parser kept the vendor string it was given; v5 canonicalizes it.

    A source recorded as ``Anthropic`` matched no vendor before the upgrade, so
    it was eligible for nothing and supplied nothing. Reading it through the
    canonical form would hand the install a supply path it never had.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0]) | {"vendor": "Anthropic"}
    supplied_id = source["models"][0]["id"]
    legacy["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    # The source itself survives — it is representable, and the user can order
    # it by hand — but no agent is given a walk through it.
    assert [source.id for source in loaded.model_hub.sources] == [source["id"]]
    for agent in loaded.model_hub.agents.values():
        assert list(agent.sources.order) == []
        assert agent.routes.get(supplied_id, ModelHubRouteConfig()).hops == ()


def test_mh_cfg_mig_001_pre_v5_native_twin_under_a_noncanonical_vendor_collapses(
    tmp_path,
    caplog,
):
    """Two natives are one native to v5 as soon as their vendors canonicalize alike.

    ``anthropic`` and ``Anthropic`` were two different vendors before the
    upgrade and are the same one after it, so the pair trips the aggregate rule
    that allows a backend only one native source. Counting the duplicates by
    the frozen pre-v5 rule would see a single native, collapse nothing, and
    fail the load this migration exists to rescue.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    kept = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = kept["models"][0]["id"]
    twin = copy.deepcopy(kept) | {"id": "src_claudepro2", "vendor": "Anthropic"}
    # The twin is persisted first: the survivor is the source the legacy walk
    # could reach, not the source that happens to come first in the file.
    legacy["model_hub"]["sources"] = [twin, kept]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        loaded = V2Config.load(config_path=config_path)

    assert [source.id for source in loaded.model_hub.sources] == [kept["id"]]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(kept["id"], supplied_id)]
    assert any(twin["id"] in record.getMessage() for record in caplog.records)


def test_mh_cfg_mig_001_pre_v5_repeated_model_id_keeps_the_source(tmp_path):
    """A repeated model id was legal before v5 and is rejected now.

    The old inventory answered a lookup with the first entry carrying the id,
    so keeping the first and dropping the rest is that same inventory. Refusing
    the source instead would cost every model it supplies over a duplicate that
    never changed an answer.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    supplied_id = source["models"][0]["id"]
    source["models"].append({**source["models"][0], "display_name": "Opus 4.6 (again)"})
    legacy["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert [model.id for model in loaded.model_hub.sources[0].models] == [supplied_id]
    assert [model.display_name for model in loaded.model_hub.sources[0].models] == [
        source["models"][0]["display_name"]
    ]
    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["claude"].routes[supplied_id].hops
    ] == [(source["id"], supplied_id)]


def test_mh_cfg_mig_001_pre_v5_claude_alias_resolves_on_a_non_native_source(tmp_path):
    """A bundled Claude alias resolved on any Anthropic source before v5.

    The frozen add-time matcher only answers an alias on a native source, so a
    hub Anthropic subscription would migrate the whole alias half of the menu
    to empty routes — the models the user actually selected by name.
    """

    legacy = _legacy_model_hub_payload(api.config_to_payload(default_config()))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0]) | {
        "supply_channel": "hub",
        "credential_ref": "cred_hubsub01",
    }
    supplied_id = source["models"][0]["id"]
    legacy["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    routes = V2Config.load(config_path=config_path).model_hub.agents["claude"].routes

    assert [(hop.source_id, hop.model_id) for hop in routes["opus"].hops] == [
        (source["id"], supplied_id)
    ]
    assert [(hop.source_id, hop.model_id) for hop in routes[supplied_id].hops] == [
        (source["id"], supplied_id)
    ]


def test_mh_cfg_mig_001_pre_v5_empty_routes_never_outrank_a_checked_menu(tmp_path):
    """An empty ``routes`` object is readable and still means nothing.

    A legacy agent whose own fields carry no pre-v5 marker can still hold a
    ``routes`` key, and an empty one parses cleanly — so keeping it would look
    valid while discarding the checked menu that was the real supply. The menu,
    the mappings and the order are the only inputs.
    """

    current = api.config_to_payload(default_config())
    legacy = _legacy_model_hub_payload(current)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    model_id = source["models"][0]["id"]
    checked_id = opencode_model_id(source["vendor"], model_id)
    legacy["model_hub"]["sources"] = [source]
    opencode = legacy["model_hub"]["agents"]["opencode"]
    opencode.pop("mappings")
    opencode["sources"].pop("policy")
    opencode["sources"]["order"] = [source["id"]]
    opencode["menu"] = {"view": "featured", "checked": [checked_id]}
    opencode["routes"] = {}
    claude = legacy["model_hub"]["agents"]["claude"]
    claude.pop("mappings")
    claude["sources"].pop("policy")
    claude["routes"] = {}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert [
        (hop.source_id, hop.model_id)
        for hop in loaded.model_hub.agents["opencode"].routes[checked_id].hops
    ] == [(source["id"], model_id)]
    # A fixed backend owes a route to every bundled menu id, so an empty object
    # there is not even loadable — it has to be rebuilt, not validated.
    assert set(loaded.model_hub.agents["claude"].routes) == set(
        current["model_hub"]["agents"]["claude"]["routes"]
    )


def test_current_model_hub_config_survives_load_unchanged(tmp_path):
    """A v5 config is returned untouched — the other half of MH-CFG-MIG-001."""

    current = api.config_to_payload(default_config())
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == current["model_hub"]


@pytest.mark.parametrize(
    ("status", "retry_at", "detail_key"),
    [
        ("standby", "2026-07-29T03:00:00Z", None),
        ("cooldown", None, "models.source.cooldown.network"),
        ("needs_action", None, "models.source.cooldown.network"),
        ("error", None, None),
    ],
)
def test_source_state_rejects_invalid_status_correlations(status, retry_at, detail_key):
    with pytest.raises(ValueError):
        ModelHubSourceStateConfig.from_payload(
            {
                "status": status,
                "retry_at": retry_at,
                "detail_key": detail_key,
            }
        )


def test_source_optional_fields_reject_schema_invalid_values():
    source = _schema("source.schema.json")["examples"][0]
    invalid_sources = []

    invalid = json.loads(json.dumps(source))
    invalid["models"][0]["display_name"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["models"][0]["discovered_at"] = "2026-07-23T03:00:00"
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["last_discovered_at"] = "2026-07-23T03:00:00"
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["state"]["detail_key"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["usage"]["currency"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["experimental_consent_at"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["account_label"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["masked_credential"] = 1
    invalid_sources.append(invalid)

    for invalid in invalid_sources:
        try:
            ModelHubSourceConfig.from_payload(invalid)
        except ValueError:
            continue
        raise AssertionError(f"schema-invalid optional field was accepted: {invalid}")
