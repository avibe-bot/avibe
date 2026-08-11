from __future__ import annotations

import copy
import json
import re
import stat
from dataclasses import fields
from itertools import product
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker, ValidationError

import config.v2_config as v2_config
from config.v2_config import (
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


def _legacy_model_hub_payload(current: dict) -> dict:
    """Build the persisted v3.0.9 Model Hub shape from a current fixture."""

    agents = {}
    for backend, agent in current["agents"].items():
        agents[backend] = {
            "backend": agent["backend"],
            "mode": agent["mode"],
            "menu_kind": agent["menu_kind"],
            "sources": {"policy": "follow", "order": []},
            "mappings": [],
            "menu": agent.get("menu"),
        }
    return {
        "sources": [],
        "priority_order": [],
        "agents": agents,
        "subscription_hub_experimental": False,
    }


def test_config_reload_migrates_v3_model_hub_shape_and_persists_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _legacy_model_hub_payload(payload["model_hub"])
    payload["migration_sentinel"] = {"keep": True}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings == ()
    assert set(loaded.model_hub.agents["claude"].routes) == set(
        ModelHubConfig().agents["claude"].routes
    )
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(persisted["model_hub"]) == {"sources", "agents"}
    assert persisted["migration_sentinel"] == {"keep": True}
    backups = list(config_path.parent.glob("config.json.bak-model-hub-migration-*"))
    assert backups
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_config_reload_recovers_malformed_legacy_collections(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    malformed_values = [
        42,
        [dict(source, models=42)],
    ]

    for index, malformed_sources in enumerate(malformed_values):
        payload = copy.deepcopy(current)
        payload["show_duration"] = True
        malformed = copy.deepcopy(legacy)
        malformed["sources"] = malformed_sources
        payload["model_hub"] = malformed
        config_path = tmp_path / f"config-{index}.json"
        original = json.dumps(payload)
        config_path.write_text(original, encoding="utf-8")

        loaded = V2Config.load(config_path=config_path)

        assert loaded.show_duration is True
        assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
        assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]
        assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "invalid_platform",
    [
        "enabled",
        "enabled-type",
        "enabled-entry-type",
        "primary-type",
        "legacy",
        "legacy-type",
    ],
)
def test_config_reload_recovers_invalid_platform_metadata_only(monkeypatch, tmp_path, invalid_platform):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    if invalid_platform == "enabled":
        payload["platforms"]["enabled"] = ["not-a-platform"]
        payload["platforms"]["primary"] = "not-a-platform"
    elif invalid_platform == "enabled-type":
        payload["platforms"]["enabled"] = {"slack": True}
    elif invalid_platform == "enabled-entry-type":
        payload["platforms"]["enabled"] = [{}]
    elif invalid_platform == "primary-type":
        payload["platforms"]["primary"] = {}
    elif invalid_platform == "legacy":
        payload.pop("platforms", None)
        payload["platform"] = "not-a-platform"
    else:
        payload.pop("platforms", None)
        payload["platform"] = {}
    config_path = tmp_path / f"{invalid_platform}-platform.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.platforms.enabled == []
    assert loaded.platform == "avibe"
    assert loaded.load_warnings and "platforms" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("platform", "invalid_fields"),
    [
        ("slack", {"bot_token": "invalid"}),
        ("slack", {"bot_token": 123}),
        ("slack", {"app_token": 123}),
        ("discord", {"thread_auto_archive_minutes": 1}),
        ("discord", {"bot_token": {}}),
        ("telegram", {"bot_token": "invalid"}),
        ("lark", {"domain": "invalid"}),
        ("lark", {"app_id": 123}),
    ],
)
def test_config_reload_recovers_invalid_platform_adapter_only(
    monkeypatch,
    tmp_path,
    platform,
    invalid_fields,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload["platform"] = platform
    payload["platforms"] = {"enabled": [platform], "primary": platform}
    platform_payload = dict(payload.get(platform) or {})
    platform_payload.update(invalid_fields)
    payload[platform] = platform_payload
    config_path = tmp_path / f"{platform}-adapter.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.platforms.enabled == []
    assert loaded.platform == "avibe"
    assert loaded.load_warnings and platform in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_ignores_stale_legacy_platform_when_platforms_are_valid(monkeypatch):
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["platform"] = "removed-platform"
    payload["platforms"] = {"enabled": ["slack"], "primary": "slack"}

    loaded = V2Config.from_payload(payload)

    assert loaded.platform == "slack"
    assert loaded.platforms.enabled == ["slack"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", {}),
        ("ack_mode", []),
    ],
)
def test_config_reload_recovers_invalid_scalar_enum_only(monkeypatch, tmp_path, field, value):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload[field] = value
    config_path = tmp_path / f"{field}-scalar.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.platform == payload["platform"]
    assert loaded.load_warnings and field in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_backup_deduplication_repairs_permissions(tmp_path):
    config_path = tmp_path / "config.json"
    content = b"sensitive config"
    config_path.write_bytes(content)
    backup = config_path.with_name("config.json.bak-test-existing")
    backup.write_bytes(content)
    backup.chmod(0o644)

    result = v2_config._backup_config_file(config_path, "test", content=content)

    assert result == backup
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_config_reload_does_not_replace_file_when_migration_backup_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _legacy_model_hub_payload(payload["model_hub"])
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(v2_config, "_backup_config_file", lambda *args, **kwargs: None)

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() != payload["model_hub"]
    assert config_path.read_text(encoding="utf-8") == original
    assert loaded.load_warnings and "could not be backed up" in loaded.load_warnings[0]


def test_config_reload_recovery_backs_up_original_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = {"sources": "invalid", "agents": {}}
    config_path = tmp_path / "config.json"
    original = json.dumps(payload).encode("utf-8")
    config_path.write_bytes(original)
    original_backup = v2_config._backup_config_file

    def replace_before_backup(path, label, *, content=None):
        path.write_text(json.dumps(api.config_to_payload(default_config())), encoding="utf-8")
        return original_backup(path, label, content=content)

    monkeypatch.setattr(v2_config, "_backup_config_file", replace_before_backup)

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings
    backups = list(config_path.parent.glob("config.json.bak-recovery-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_config_reload_migrates_legacy_mapping_to_exact_route_hop(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = "native_cli"
    source["models"][0]["provenance"] = source["models"][0].pop("origin")
    model_id = source["models"][0]["id"]
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    legacy["agents"]["claude"]["mappings"] = [
        {
            "builtin_id": model_id,
            "target_model_id": model_id,
            "enabled": True,
        }
    ]
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    route = loaded.model_hub.agents["claude"].routes[model_id]
    assert route.hops[0].source_id == source["id"]
    assert route.hops[0].model_id == model_id
    assert loaded.load_warnings == ()
    assert loaded.model_hub.sources[0].models[0].reasoning_efforts == []


def test_config_reload_recovers_dangling_legacy_custom_source_order(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": ["src_missing123"],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "dangling-source-order.json"
    original = json.dumps(current)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in " ".join(loaded.load_warnings)
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_recovers_backend_ineligible_legacy_custom_source_order(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["codex"]["mode"] = "hub"
    legacy["agents"]["codex"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "ineligible-source-order.json"
    original = json.dumps(current)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in " ".join(loaded.load_warnings)
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_preserves_enabled_unchecked_legacy_opencode_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    source["models"][0]["provenance"] = source["models"][0].pop("origin")
    source["models"][0].pop("reasoning_efforts")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["opencode"]["mode"] = "hub"
    legacy["agents"]["opencode"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    legacy["agents"]["opencode"]["menu"] = {"view": "featured", "checked": []}
    legacy["agents"]["opencode"]["mappings"] = [
        {
            "builtin_id": "custom/glm-5.2-air",
            "target_model_id": "custom/glm-5.2-air",
            "enabled": True,
        }
    ]
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    route = loaded.model_hub.agents["opencode"].routes["custom/glm-5.2-air"]
    assert [(hop.source_id, hop.model_id) for hop in route.hops] == [
        (source["id"], source["models"][0]["id"]),
    ]
    assert loaded.load_warnings == ()


def test_config_reload_keeps_legacy_hub_subscription_backend_specific(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = "hub"
    source["credential_ref"] = "cred_anthropic_hub"
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    legacy["agents"]["codex"]["mode"] = "hub"
    legacy["agents"]["codex"]["sources"] = {
        "policy": "follow",
        "order": [],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.agents["claude"].sources.order == [source["id"]]
    assert loaded.model_hub.agents["codex"].sources.order == []
    assert loaded.load_warnings == ()


@pytest.mark.parametrize("supply_channel", ["native_cli", "hub"])
def test_config_reload_preserves_legacy_claude_alias_resolution(
    monkeypatch,
    tmp_path,
    supply_channel,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = supply_channel
    if supply_channel == "hub":
        source["credential_ref"] = "cred_anthropic_hub"
    older = source["models"][0]
    newer = {
        **older,
        "id": "claude-opus-5-20260724",
        "display_name": "Opus 5",
    }
    source["models"] = [older, newer]
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    route = loaded.model_hub.agents["claude"].routes["opus"]
    assert [(hop.source_id, hop.model_id) for hop in route.hops] == [
        (source["id"], newer["id"]),
    ]
    assert loaded.load_warnings == ()


def test_config_reload_defaults_omitted_legacy_backend_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["agents"].pop("codex")
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.agents["codex"].mode == "direct"
    assert loaded.load_warnings == ()
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert "codex" in persisted["model_hub"]["agents"]


def test_config_reload_uses_first_enabled_duplicate_legacy_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    first_target = source["models"][0]["id"]
    later_target = "claude-opus-5-20260724"
    source["models"].append(
        {
            **source["models"][0],
            "id": later_target,
            "display_name": "Opus 5",
        }
    )
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    legacy["agents"]["claude"]["mappings"] = [
        {"builtin_id": "opus", "target_model_id": later_target, "enabled": False},
        {"builtin_id": "opus", "target_model_id": first_target, "enabled": True},
        {"builtin_id": "opus", "target_model_id": later_target, "enabled": True},
    ]
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    route = loaded.model_hub.agents["claude"].routes["opus"]
    assert [(hop.source_id, hop.model_id) for hop in route.hops] == [
        (source["id"], first_target),
    ]


@pytest.mark.parametrize(
    "invalid_invariant",
    [
        "healthy-detail",
        "hub-credential",
        "manual-discovered-at",
        "subscription-api-key",
        "opencode-identity",
    ],
)
def test_config_reload_recovers_inner_model_hub_invariant_only(
    monkeypatch,
    tmp_path,
    invalid_invariant,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    if invalid_invariant == "healthy-detail":
        source["state"]["detail_key"] = "models.source.invalid"
    elif invalid_invariant == "hub-credential":
        source["supply_channel"] = "hub"
        source["credential_ref"] = None
    elif invalid_invariant == "manual-discovered-at":
        source["models"][0]["origin"] = "manual"
    elif invalid_invariant == "subscription-api-key":
        source["base_url"] = "https://api.anthropic.com"
    else:
        payload["model_hub"]["agents"]["opencode"]["menu"] = {
            "view": "featured",
            "checked": ["invalid-opencode-identity"],
        }
    if invalid_invariant != "opencode-identity":
        payload["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]
    persisted = json.loads(config_path.read_text(encoding="utf-8"))["model_hub"]
    if invalid_invariant == "opencode-identity":
        assert persisted["agents"]["opencode"]["menu"]["checked"] == [
            "invalid-opencode-identity"
        ]
    else:
        assert persisted["sources"] == [source]


def test_config_reload_allows_empty_target_on_disabled_legacy_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(payload["model_hub"])
    legacy["agents"]["claude"]["mappings"] = [
        {"builtin_id": "opus", "target_model_id": "", "enabled": False}
    ]
    payload["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings == ()
    assert loaded.model_hub.agents["claude"].routes["opus"].hops == ()


def test_invalid_json_recovery_backs_up_the_original_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    malformed = b'{"mode": '
    config_path.write_bytes(malformed)
    original_backup = v2_config._backup_config_file

    def replace_before_backup(path, label, *, content=None):
        path.write_text(json.dumps(api.config_to_payload(default_config())), encoding="utf-8")
        return original_backup(path, label, content=content)

    monkeypatch.setattr(v2_config, "_backup_config_file", replace_before_backup)

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings and "JSON" in loaded.load_warnings[0]
    backups = list(config_path.parent.glob("config.json.bak-invalid-json-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == malformed


def test_config_reload_does_not_overwrite_config_changed_during_migration(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _legacy_model_hub_payload(payload["model_hub"])
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    concurrent_payload = {**payload, "show_duration": True}
    original_persist = v2_config._persist_migrated_config_payload

    def persist_after_concurrent_save(path, expected_raw, migrated_payload):
        path.write_text(json.dumps(concurrent_payload), encoding="utf-8")
        return original_persist(path, expected_raw, migrated_payload)

    monkeypatch.setattr(
        v2_config,
        "_persist_migrated_config_payload",
        persist_after_concurrent_save,
    )

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["show_duration"] is True
    assert set(persisted["model_hub"]) == {"sources", "agents"}
    assert loaded.load_warnings and "changed during load" in loaded.load_warnings[0]


def test_config_reload_does_not_overwrite_config_changed_before_replace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _legacy_model_hub_payload(payload["model_hub"])
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")
    concurrent_payload = {**payload, "show_duration": True}
    original_write = v2_config._write_config_payload_if_unchanged

    def write_after_concurrent_save(path, migrated_payload, expected_raw):
        path.write_text(json.dumps(concurrent_payload), encoding="utf-8")
        return original_write(path, migrated_payload, expected_raw)

    monkeypatch.setattr(
        v2_config,
        "_write_config_payload_if_unchanged",
        write_after_concurrent_save,
    )

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["show_duration"] is True
    assert set(persisted["model_hub"]) == {"sources", "agents"}
    assert loaded.load_warnings and "before replacement" in " ".join(loaded.load_warnings)
    backups = list(config_path.parent.glob("config.json.bak-model-hub-migration-*"))
    assert backups and any(backup.read_text(encoding="utf-8") == original for backup in backups)


def test_config_reload_recovers_invalid_optional_section_without_overwriting_file(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = {"sources": "invalid", "agents": {}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]
    recovery = api.client_config_payload(loaded)["config_recovery"]
    assert recovery["required"] is True
    assert recovery["warnings"]
    assert recovery["warnings"] != list(loaded.load_warnings)
    assert json.loads(config_path.read_text(encoding="utf-8"))["model_hub"]["sources"] == "invalid"
    assert list(config_path.parent.glob("config.json.bak-recovery-*"))
    V2Config.load(config_path=config_path)
    assert len(list(config_path.parent.glob("config.json.bak-recovery-*"))) == 1

    monkeypatch.setattr(api, "load_config", lambda: loaded)
    with pytest.raises(ValueError, match="recovery warnings"):
        api.save_config({"show_duration": True})
    with pytest.raises(ValueError, match="recovery warnings"):
        loaded.save(config_path)


def test_client_config_recovery_projection_redacts_validator_details(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["platforms"] = {"enabled": ["sk-leaked-platform-token"], "primary": "avibe"}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)
    raw_warnings = json.dumps(loaded.load_warnings)
    projected = api.client_config_payload(loaded)

    assert "sk-leaked-platform-token" in raw_warnings
    assert "sk-leaked-platform-token" not in json.dumps(projected)
    assert projected["config_recovery"]["warnings"]


def test_config_reload_recovers_runtime_with_the_canonical_default(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["runtime"] = {"log_level": "DEBUG"}
    payload["show_duration"] = True
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.runtime.default_cwd == str(Path.home() / "work")
    assert loaded.runtime.log_level == "INFO"
    assert loaded.show_duration is True
    assert loaded.load_warnings and "runtime" in loaded.load_warnings[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda hub: hub["agents"]["claude"].update({"sources": "invalid"}),
        lambda hub: hub["agents"]["claude"].update(
            {"sources": {"policy": "custom", "order": "invalid"}}
        ),
        lambda hub: hub.update({"priority_order": {"invalid": True}}),
        lambda hub: hub.update({"subscription_hub_experimental": "false"}),
        lambda hub: hub["agents"]["claude"].update({"mappings": ["invalid"]}),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "",
                        "target_model_id": "claude-opus-4-5",
                        "enabled": True,
                    }
                ]
            }
        ),
        lambda hub: hub["agents"]["claude"].update({"mode": "hbu"}),
        lambda hub: hub["agents"]["claude"].update({"mode": []}),
        lambda hub: hub["agents"].update({"future-backend": {"mode": "hub"}}),
        lambda hub: hub["agents"]["claude"].update({"future-field": True}),
        lambda hub: hub["agents"]["claude"].update({"backend": "codex"}),
        lambda hub: hub["agents"]["claude"].pop("backend"),
        lambda hub: hub["agents"]["claude"].update({"menu_kind": "open"}),
        lambda hub: hub["agents"]["claude"].pop("menu_kind"),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "opus",
                        "target_model_id": "claude-opus-4-5",
                        "enabled": True,
                        "future_field": True,
                    }
                ]
            }
        ),
        lambda hub: hub["agents"]["claude"].update({"routes": "invalid"}),
        lambda hub: hub["agents"]["claude"].update(
            {"mappings": [{"builtin_id": "opus", "enabled": True}]}
        ),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "retired-model",
                        "target_model_id": "claude-opus-4-6",
                        "enabled": True,
                    }
                ]
            }
        ),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "opus",
                        "target_model_id": "claude-opus-4-5",
                        "enabled": "yes",
                    }
                ]
            }
        ),
    ],
    ids=[
        "sources-not-object",
        "custom-order-not-array",
        "priority-order-not-array",
        "subscription-hub-experimental-not-bool",
        "mapping-not-object",
        "mapping-empty-builtin",
        "agent-invalid-mode",
        "agent-non-scalar-mode",
        "agent-unknown-backend",
        "agent-unknown-field",
        "agent-backend-mismatch",
        "agent-backend-missing",
        "agent-menu-kind-mismatch",
        "agent-menu-kind-missing",
        "mapping-unknown-field",
        "routes-not-object",
        "mapping-missing-target",
        "mapping-retired-menu",
        "mapping-enabled-not-boolean",
    ],
)
def test_config_reload_does_not_infer_malformed_legacy_source_order(
    monkeypatch,
    tmp_path,
    mutate,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(payload["model_hub"])
    mutate(legacy)
    payload["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in " ".join(loaded.load_warnings)
    assert config_path.read_text(encoding="utf-8") == original
    assert list(config_path.parent.glob("config.json.bak-recovery-*"))


def test_config_reload_recovers_invalid_codex_agent_with_disabled_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload["agents"]["codex"] = "invalid"
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.agents.codex.enabled is False
    assert loaded.agents.claude.enabled is True
    assert loaded.load_warnings and "agents.codex" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_recovers_invalid_agents_with_canonical_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload["agents"] = "invalid"
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.agents.opencode.enabled is True
    assert loaded.agents.claude.enabled is True
    assert loaded.agents.codex.enabled is False
    assert loaded.agents.avault.cli_path == "avault"
    assert loaded.load_warnings and "agents" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_recovers_invalid_json_with_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    config_path.write_text('{"mode": ', encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.mode == "self_host"
    assert loaded.platform == "avibe"
    assert loaded.load_warnings and "JSON" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == '{"mode": '
    assert list(config_path.parent.glob("config.json.bak-invalid-json-*"))


def test_config_reload_recovers_invalid_encoding_with_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    config_path.write_bytes(b"\xff\xfe")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.mode == "self_host"
    assert loaded.load_warnings and "UTF-8" in loaded.load_warnings[0]
    assert config_path.read_bytes() == b"\xff\xfe"
    assert list(config_path.parent.glob("config.json.bak-invalid-encoding-*"))


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
