from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from jsonschema import Draft7Validator, FormatChecker

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMappingConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
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


def test_frozen_source_and_agent_examples_round_trip_byte_faithfully():
    assert Path("core/handlers/model_hub/adapter.py").read_bytes() == (
        CONTRACTS / "adapter-interface.py"
    ).read_bytes()

    for example in _schema("source.schema.json")["examples"]:
        serialized = ModelHubSourceConfig.from_payload(example).to_payload()
        assert _canonical(serialized) == _canonical(example)
        _assert_valid("source.schema.json", serialized)

    for example in _schema("agent-supply.schema.json")["examples"]:
        agent = ModelHubAgentSupplyConfig.from_payload(example)
        serialized = {**agent.to_payload(), "current": example.get("current")}
        assert _canonical(serialized) == _canonical(example)
        _assert_valid("agent-supply.schema.json", serialized)


def test_every_frozen_schema_example_is_valid_and_json_round_trips():
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema, format_checker=FormatChecker())
        for example in schema.get("examples", []):
            validator.validate(example)
            assert _canonical(json.loads(_canonical(example))) == _canonical(example)


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
        "priority_order": [source_example["id"]],
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
        assert mapping_fields == set(
            ModelHubMappingConfig("builtin", "target", True).to_payload()
        ), label
        assert menu_fields == set(serialized_hub["agents"]["opencode"]["menu"]), label

    stale_hub_payload = json.loads(json.dumps(api_payload["model_hub"]))
    stale_hub_payload["priority_order"] = []
    updated = api.save_config({"show_duration": True, "model_hub": stale_hub_payload})
    assert updated.model_hub.to_payload() == loaded.model_hub.to_payload()
    assert api.config_to_payload(updated)["model_hub"] == api_payload["model_hub"]


def test_legacy_config_defaults_direct_while_fresh_config_defaults_hub():
    payload = api.config_to_payload(default_config(), include_secrets=True)
    payload.pop("model_hub")
    legacy = V2Config.from_payload(payload)

    assert {agent.mode for agent in legacy.model_hub.agents.values()} == {"direct"}
    assert {agent.mode for agent in default_config().model_hub.agents.values()} == {"hub"}


def test_hub_subscription_requires_server_recorded_consent():
    source = _schema("source.schema.json")["examples"][0]
    source = {**source, "supply_channel": "hub"}
    payload = {
        "sources": [source],
        "priority_order": [source["id"]],
        "agents": {},
        "subscription_hub_experimental": True,
    }

    try:
        ModelHubConfig.from_payload(payload)
    except ValueError as exc:
        assert "recorded experimental consent" in str(exc)
    else:
        raise AssertionError("hub-held subscription loaded without recorded consent")


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
