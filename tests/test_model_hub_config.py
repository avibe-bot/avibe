from __future__ import annotations

import copy
import json
import re
from dataclasses import fields
from itertools import product
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker, ValidationError

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


def test_guard_refusal_error_requires_its_corresponding_nonempty_plan_array():
    validator = Draft7Validator(_schema("guard-refusal.schema.json"))
    hop = {
        "backend": "claude",
        "menu_model": "claude-opus-4-6",
        "source_id": "src_anthkey01",
        "model_id": "claude-opus-4-6",
        "position": 1,
    }
    gap = {
        "backend": "claude",
        "model_id": "claude-opus-4-6",
        "agents": ["pm"],
    }

    route_refusal = {
        "ok": False,
        "contract_version": 5,
        "error": "source_in_route_chain",
        "would_remove_hops": [hop],
        "would_interrupt": [],
    }
    model_refusal = {**route_refusal, "error": "source_model_in_route_chain"}
    supplier_refusal = {
        **route_refusal,
        "error": "source_last_supplier",
        "would_remove_hops": [],
        "would_interrupt": [gap],
    }
    for payload in (route_refusal, model_refusal, supplier_refusal):
        validator.validate(payload)

    for payload in (
        {**route_refusal, "would_remove_hops": [], "would_interrupt": [gap]},
        {**model_refusal, "would_remove_hops": [], "would_interrupt": [gap]},
        {**supplier_refusal, "would_remove_hops": [hop], "would_interrupt": []},
        {**route_refusal, "would_remove_hops": [hop, hop]},
        {**supplier_refusal, "would_interrupt": [gap, gap]},
    ):
        with pytest.raises(ValidationError):
            validator.validate(payload)


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


def test_d21_live_backoff_reason_consumers_match_authority_individually():
    registry = json.loads(
        (CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8")
    )
    decision = next(
        item for item in registry["decision_tables"] if item["id"] == "D21"
    )
    authority_spec = decision["authority"]
    authority = _vocabulary(
        _schema(authority_spec["schema"]), authority_spec["paths"]
    )

    observed = []
    for consumer in decision["consumers"]:
        if consumer["kind"] == "schema_vocabulary":
            observed.append(
                _vocabulary(
                    _schema(consumer["schema"]),
                    consumer["paths"],
                    exclude=consumer.get("exclude", ()),
                )
            )
        elif consumer["kind"] == "marker_tail":
            values = set()
            for line in Path(consumer["file"]).read_text(encoding="utf-8").splitlines():
                if consumer["marker"] in line:
                    tail = line.split(consumer["marker"], 1)[1].split("-->", 1)[0]
                    values.update(tail.split())
            observed.append(values)
        elif consumer["kind"] == "json_object_keys":
            payload = json.loads(Path(consumer["file"]).read_text(encoding="utf-8"))
            keys = _json_pointer(payload, consumer["path"])
            observed.append({f"{consumer.get('prefix', '')}{key}" for key in keys})
        else:
            raise AssertionError(consumer)

    assert observed
    assert all(values == authority for values in observed)

    mutated_probe = _schema("probe-result.schema.json")
    mutated_probe["properties"]["error"]["enum"].remove(
        "models.source.backoff.connection_failed"
    )
    probe_spec = next(
        item for item in decision["consumers"] if item["kind"] == "schema_vocabulary"
    )
    assert (
        _vocabulary(
            mutated_probe,
            probe_spec["paths"],
            exclude=probe_spec.get("exclude", ()),
        )
        != authority
    )


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
    api_contract = (CONTRACTS / "api.md").read_text(encoding="utf-8")
    credential_contract = api_contract.split(
        "## Credential replacement and reauth", 1
    )[1].split("## Source refresh and blocked-source recovery", 1)[0]
    assert "{source, recovered, interrupted_pairs}" not in credential_contract
    assert '"recovered"' not in credential_contract
    assert '"interrupted_pairs"' not in credential_contract
    assert api_contract.count(
        '- terminal `intent: "reauth"` → '
        '`{flow, source, recovered, interrupted_pairs}`.'
    ) == 1

    runtime_schema = _schema("runtime-dependency.schema.json")
    runtime_validator = Draft7Validator(runtime_schema)
    installing_runtime = copy.deepcopy(runtime_schema["examples"][0])
    installing_runtime["status"].update(
        {
            "installed_version": None,
            "verified": False,
            "listening": None,
            "health": "installing",
            "error_key": None,
        }
    )
    runtime_validator.validate(installing_runtime)
    for field, contradiction in (
        ("installed_version", "v7.2.95"),
        ("verified", True),
        ("listening", {"host": "127.0.0.1", "port": 15220}),
    ):
        invalid_installing = copy.deepcopy(installing_runtime)
        invalid_installing["status"][field] = contradiction
        with pytest.raises(ValidationError):
            runtime_validator.validate(invalid_installing)
    missing_installing_shape = copy.deepcopy(installing_runtime)
    del missing_installing_shape["status"]["listening"]
    with pytest.raises(ValidationError):
        runtime_validator.validate(missing_installing_shape)

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

    probe_schema = _schema("probe-result.schema.json")
    probe_validator = Draft7Validator(probe_schema)
    connection_failure = next(
        copy.deepcopy(example)
        for example in probe_schema["examples"]
        if example["error"] == "models.source.backoff.connection_failed"
    )
    probe_validator.validate(connection_failure)
    for contradiction in (
        {"channel": "native_cli"},
        {"latency_ms": 1},
    ):
        invalid_connection_failure = {**connection_failure, **contradiction}
        with pytest.raises(ValidationError):
            probe_validator.validate(invalid_connection_failure)

    probe = copy.deepcopy(probe_schema["examples"][0])
    probe["source_id"] = "direct"
    with pytest.raises(ValidationError):
        probe_validator.validate(probe)

    chain_schema = _schema("agent-chain.schema.json")
    chain_validator = Draft7Validator(chain_schema)
    ordinary_backoff = next(
        copy.deepcopy(example)
        for example in chain_schema["examples"]
        if example["chain"]
        and example["chain"][0]["reason"]
        == "models.source.backoff.connection_failed"
    )
    for blocker in (
        {
            "health": "needs_action",
            "reason": "models.source.needs_action.credential_revoked",
            "retry_at": None,
        },
        {
            "health": "healthy",
            "reason": "model_unsupported",
            "retry_at": None,
        },
    ):
        blocked = copy.deepcopy(ordinary_backoff)
        blocked["chain"][0].update(blocker)
        blocked["supply_state"] = "interrupted"
        chain_validator.validate(blocked)
        mislabeled_waiting = {**blocked, "supply_state": "waiting"}
        with pytest.raises(ValidationError):
            chain_validator.validate(mislabeled_waiting)

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
    for waiting in chain_schema["examples"][:2]:
        interrupted = copy.deepcopy(waiting)
        interrupted["supply_state"] = "interrupted"
        with pytest.raises(ValidationError):
            chain_validator.validate(interrupted)
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

    model_supply_validator = Draft7Validator(
        supply_schema["properties"]["model_supply"]["items"]
    )
    empty_model_supply = {
        "model_id": "claude-opus-4-6",
        "chain_length": 0,
        "has_runnable_hop": False,
    }
    model_supply_validator.validate(empty_model_supply)
    with pytest.raises(ValidationError):
        model_supply_validator.validate(
            {**empty_model_supply, "has_runnable_hop": True}
        )

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
