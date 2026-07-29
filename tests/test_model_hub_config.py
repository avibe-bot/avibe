from __future__ import annotations

import copy
import json
from dataclasses import fields
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker, ValidationError

from config.v2_config import (
    MODEL_HUB_ENABLED_ENV,
    MODEL_HUB_LEGACY_CREATED_AT,
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMappingConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
    is_model_hub_enabled,
)
from core.services.settings import default_config
from vibe import api

CONTRACTS = Path("docs/plans/model-hub-contracts")


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _assert_valid(name: str, payload: dict) -> None:
    errors = sorted(
        Draft7Validator(_schema(name), format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, [error.message for error in errors]


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
        target = _vocabulary(
            schemas[entry["target"]["schema"]],
            [entry["target"]["path"]],
        )
        assert home == set(entry["mapping"])
        assert target == set(entry["mapping"].values())
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

    for example in _schema("agent-supply.schema.json")["examples"]:
        agent = ModelHubAgentSupplyConfig.from_payload(example)
        # `current`, `builtin_models` and `standard_vendors` are read-only
        # endpoint projections (v1.2), not persisted config — reconstruct them
        # the way `_agent_payload` merges them onto to_payload().
        serialized = {
            **agent.to_payload(),
            "current": example.get("current"),
            "builtin_models": example.get("builtin_models"),
            "standard_vendors": example.get("standard_vendors"),
        }
        if "sources" not in example:
            serialized.pop("sources")
        else:
            serialized["sources"]["eligibility"] = example["sources"].get("eligibility")
        assert _canonical(serialized) == _canonical(example)
        _assert_valid("agent-supply.schema.json", serialized)


def test_every_frozen_schema_example_is_valid_and_json_round_trips():
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema, format_checker=FormatChecker())
        for example in schema.get("examples", []):
            validator.validate(example)
            assert _canonical(json.loads(_canonical(example))) == _canonical(example)


def test_v4_mirror_registry_is_executable_and_complete():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    schemas = _mirror_schemas(registry)

    assert registry["contract_version"] == 4
    assert [entry["id"] for entry in registry["entries"]] == [
        "M1",
        "M2",
        "M3",
        "M4",
        "M5",
        "M6",
        "M7",
        "M8",
        "N1",
    ]
    for entry in registry["entries"]:
        _validate_mirror_entry(entry, schemas)


def test_v4_mirror_registry_mutation_probes_detect_every_comparable_drift():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    schemas = _mirror_schemas(registry)

    for entry in registry["entries"]:
        if entry["rule"] == "none":
            continue
        mutated = copy.deepcopy(schemas)
        if entry["rule"] == "bijection":
            location = entry["target_sets"][0]
            pointer = location["paths"][0]
        elif entry["rule"] == "equality":
            location = entry["sets"][0]
            pointer = location["paths"][0]
        else:
            location = entry["home"]
            pointer = location["path"]
        node = _json_pointer(mutated[location["schema"]], pointer)
        assert "enum" in node, entry["id"]
        node["enum"].append(f"mutation_{entry['id'].lower()}")

        with pytest.raises(AssertionError):
            _validate_mirror_entry(entry, mutated)


def test_v4_shape_amendments_reject_the_false_states_they_replace():
    supply_schema = _schema("agent-supply.schema.json")
    supply_validator = Draft7Validator(supply_schema)
    base_supply = {
        "backend": "claude",
        "mode": "hub",
        "menu_kind": "fixed",
        "selected_by_agent": None,
        "selected_model_id": None,
        "current": None,
        "sources": {"policy": "follow", "order": [], "eligibility": []},
        "supply_status": None,
        "mappings": [],
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

    canceled = _schema("turn-provenance.schema.json")["examples"][-1]
    Draft7Validator(_schema("turn-provenance.schema.json")).validate(canceled)
    invented_failure = copy.deepcopy(canceled)
    invented_failure["canceled_attempt"]["reason"] = "server_error"
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("turn-provenance.schema.json")).validate(invented_failure)

    chain_schema = _schema("agent-chain.schema.json")
    chain_validator = Draft7Validator(chain_schema)
    native_unavailable = copy.deepcopy(chain_schema["examples"][-1])
    chain_validator.validate(native_unavailable)

    unavailable_needs_action = copy.deepcopy(native_unavailable)
    unavailable_needs_action["chain"][0]["health"] = "needs_action"
    unavailable_needs_action["chain"][0]["retry_at"] = None
    chain_validator.validate(unavailable_needs_action)

    unmarked_healthy_unavailable = copy.deepcopy(chain_schema["examples"][-2])
    unmarked_healthy_unavailable["chain"][0]["reason"] = None
    with pytest.raises(ValidationError):
        chain_validator.validate(unmarked_healthy_unavailable)

    mislabeled_waiting = copy.deepcopy(native_unavailable)
    mislabeled_waiting["supply_state"] = "waiting"
    with pytest.raises(ValidationError):
        chain_validator.validate(mislabeled_waiting)

    unavailable_hub = copy.deepcopy(native_unavailable)
    unavailable_hub["chain"][0]["channel"] = "hub"
    with pytest.raises(ValidationError):
        chain_validator.validate(unavailable_hub)

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
        "experimental_consent_at": "2026-07-23T03:00:00Z",
        "credential_ref": "cred_serializer_test",
    }
    hub_payload = {
        "sources": [source_example],
        "agents": {
            backend: ModelHubAgentSupplyConfig.default(backend, mode="hub").to_payload()
            for backend in ("claude", "codex", "opencode")
        },
        "subscription_hub_experimental": True,
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
    agent_fields = {field.name for field in fields(ModelHubAgentSupplyConfig)}
    agent_sources_fields = {field.name for field in fields(ModelHubAgentSourcesConfig)}
    mapping_fields = {field.name for field in fields(ModelHubMappingConfig)}
    menu_fields = {field.name for field in fields(ModelHubMenuConfig)}

    assert expected_root == set(api_payload["model_hub"])
    assert expected_root == set(disk_payload["model_hub"])
    for label, serialized_hub in (
        ("config_to_payload", api_payload["model_hub"]),
        ("V2Config.save", disk_payload["model_hub"]),
    ):
        serialized_source = serialized_hub["sources"][0]
        assert source_fields == set(serialized_source), label
        assert source_state_fields == set(serialized_source["state"]), label
        assert source_usage_fields == set(serialized_source["usage"]), label
        assert source_model_fields == set(serialized_source["models"][0]), label
        assert agent_fields == set(serialized_hub["agents"]["claude"]), label
        assert agent_sources_fields == set(serialized_hub["agents"]["claude"]["sources"]), label
        assert mapping_fields == set(ModelHubMappingConfig("builtin", "target", True).to_payload()), label
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


def test_hub_subscription_requires_server_recorded_consent():
    source = _schema("source.schema.json")["examples"][0]
    source = {**source, "supply_channel": "hub"}
    payload = {
        "sources": [source],
        "agents": {},
        "subscription_hub_experimental": True,
    }

    try:
        ModelHubConfig.from_payload(payload)
    except ValueError as exc:
        assert "recorded experimental consent" in str(exc)
    else:
        raise AssertionError("hub-held subscription loaded without recorded consent")


def test_legacy_global_priority_key_is_dropped_without_validation():
    payload = ModelHubConfig().to_payload()
    payload["priority_order"] = {"legacy": "shape-does-not-matter"}

    loaded = ModelHubConfig.from_payload(payload)

    assert "priority_order" not in loaded.to_payload()


def test_agent_source_orders_validate_existence_eligibility_and_uniqueness():
    source = {
        **_schema("source.schema.json")["examples"][0],
        "created_at": "2026-07-29T01:00:00Z",
    }
    base = ModelHubConfig().to_payload()
    base["sources"] = [source]
    base["agents"]["claude"]["sources"] = {
        "policy": "custom",
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
        "policy": "custom",
        "order": [source["id"]],
    }
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(ineligible)


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
        experimental_consent_at=("2026-07-29T02:00:00Z" if kind == "subscription" and channel == "hub" else None),
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
        subscription_hub_experimental=True,
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
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]
    assert config.recommended_source_order("opencode") == [
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]


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
