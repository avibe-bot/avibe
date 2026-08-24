from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from config import paths
from config.v2_config import (
    MemoryConfig,
    MemoryCloudConfig,
    MemoryEndpointConfig,
    V2Config,
    atomic_update_memory,
    memory_config_to_payload,
)
from vibe.api import config_to_payload, save_memory_config


def _payload(memory: object) -> dict:
    return {
        "mode": "self_host",
        "version": "v2",
        "slack": {"bot_token": ""},
        "runtime": {"default_cwd": "."},
        "agents": {},
        "memory": memory,
    }


def _complete_processing() -> dict:
    return {
        "llm": {
            "base_url": "https://llm.example.test/v1",
            "model": "chat",
            "api_key": "llm-key",
        },
        "embedding": {
            "base_url": "https://embed.example.test/v1",
            "model": "embed",
            "api_key": "embed-key",
        },
    }


def test_memory_config_round_trips_without_recovery_protocol_or_secret_projection(
    tmp_path: Path,
) -> None:
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": _complete_processing()})
    )
    path = tmp_path / "config.json"
    config.save(path)

    stored = json.loads(path.read_text(encoding="utf-8"))
    projected = config_to_payload(config)

    assert stored["memory"]["processing"]["llm"]["api_key"] == "llm-key"
    assert projected["memory"]["processing"]["llm"] == {
        "base_url": "https://llm.example.test/v1",
        "model": "chat",
        "api_key": None,
        "has_api_key": True,
    }
    assert "embed-key" not in json.dumps(projected)
    for field in ("recovery_intent", "embedding_change_pending"):
        assert field not in stored["memory"]
        assert field not in projected["memory"]
    assert "repair_required" not in stored["memory"]
    assert "repair_required" not in projected["memory"]
    assert "transition_rebuild_owned" not in stored["memory"]["cloud"]


@pytest.mark.parametrize(
    "legacy",
    [
        {"embedding_change_pending": True},
        {"recovery_intent": "rebuild"},
        {"recovery_intent": "factory_reset"},
        {"cloud": {"transition_rebuild_owned": True}},
    ],
)
def test_released_recovery_fields_become_a_durable_internal_repair_fence(
    tmp_path: Path,
    legacy: dict,
) -> None:
    memory = {"enabled": False, **legacy}
    config = V2Config.from_payload(_payload(memory))

    assert config.memory.legacy_needs_repair is True

    path = tmp_path / "config.json"
    config.save(path)
    stored = json.loads(path.read_text(encoding="utf-8"))["memory"]

    assert "recovery_intent" not in stored
    assert "embedding_change_pending" not in stored
    assert "transition_rebuild_owned" not in stored["cloud"]
    assert stored["repair_required"] is True
    assert "repair_required" not in config_to_payload(config)["memory"]
    loaded = V2Config.load(path)
    assert loaded.memory.legacy_needs_repair is True


def test_released_recovery_config_fixture_loads_and_saves_canonically(
    tmp_path: Path,
) -> None:
    """Released recovery evidence stays fenced without retaining its old stages."""

    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "memory"
        / "released_recovery_protocol.json"
    )
    path = tmp_path / "config.json"
    path.write_bytes(fixture.read_bytes())

    loaded = V2Config.load(path)

    assert loaded.memory.enabled is True
    assert loaded.memory.legacy_needs_repair is True
    loaded.save(path)
    stored = json.loads(path.read_text(encoding="utf-8"))["memory"]
    assert "recovery_intent" not in stored
    assert "embedding_change_pending" not in stored
    assert "transition_rebuild_owned" not in stored["cloud"]
    assert stored["repair_required"] is True
    assert V2Config.load(path).memory.legacy_needs_repair is True


@pytest.mark.parametrize("managed", [False, True])
def test_unreadable_authoritative_cloud_cache_becomes_durable_repair_fence(
    tmp_path: Path,
    managed: bool,
) -> None:
    memory = {
        "enabled": False,
        "mode": "custom" if managed else "platform",
        "cloud": "unreadable",
    }
    payload = _payload(memory)
    if managed:
        payload["remote_access"] = {
            "provider": "vibe_cloud",
            "vibe_cloud": {
                "enabled": True,
                "instance_id": "instance",
                "instance_kind": "organization",
                "instance_secret": "secret",
            },
        }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(path)

    assert loaded.memory.legacy_needs_repair is True
    updated = atomic_update_memory(lambda memory: memory, config_path=path)
    assert updated.memory.legacy_needs_repair is True
    stored = json.loads(path.read_text(encoding="utf-8"))["memory"]
    assert stored["repair_required"] is True


def test_unreadable_applied_cloud_identity_without_live_identity_is_fenced(
    tmp_path: Path,
) -> None:
    payload = _payload(
        {
            "enabled": False,
            "mode": "platform",
            "cloud": {
                "embedding_identity": None,
                "applied_embedding_identity": [],
            },
        }
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(path)

    assert loaded.memory.legacy_needs_repair is True
    updated = atomic_update_memory(lambda memory: memory, config_path=path)
    assert updated.memory.legacy_needs_repair is True


def test_any_recovered_active_cloud_field_retains_a_baseline_or_fence(
    tmp_path: Path,
) -> None:
    """Cloud recovery cannot create a fresh identity over an existing root."""

    path = tmp_path / "config.json"
    for field_info in fields(MemoryCloudConfig):
        payload = _payload(
            {
                "enabled": False,
                "mode": "platform",
                "cloud": {
                    "scope": "platform",
                    "embedding_identity": "emb-v1",
                    "applied_embedding_identity": None,
                    field_info.name: [],
                },
            }
        )
        path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = V2Config.load(path)

        assert loaded.load_warnings, field_info.name
        assert (
            loaded.memory.cloud.applied_embedding_identity == "emb-v1"
            or loaded.memory.legacy_needs_repair is True
        ), field_info.name


def test_recovered_managed_attachment_without_applied_identity_is_fenced(
    tmp_path: Path,
) -> None:
    payload = _payload(
        {
            "enabled": False,
            "mode": "custom",
            "processing": _complete_processing(),
            "cloud": {
                "scope": "organization",
                "embedding_identity": "emb-org",
                "applied_embedding_identity": None,
                "organization_attached": [],
            },
        }
    )
    payload["remote_access"] = {
        "provider": "vibe_cloud",
        "vibe_cloud": {
            "enabled": True,
            "instance_id": "instance",
            "instance_kind": "organization",
            "instance_secret": "secret",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(path)

    assert loaded.memory.cloud.organization_attached is True
    assert loaded.memory.legacy_needs_repair is True


@pytest.mark.parametrize("intent", ["unknown", "", True, 1, {}])
def test_unknown_legacy_recovery_intent_is_rejected_as_malformed_input(
    intent: object,
) -> None:
    with pytest.raises(ValueError, match="memory.recovery_intent"):
        V2Config.from_payload(_payload({"recovery_intent": intent}))


def test_malformed_released_recovery_field_recovers_memory_section_on_load(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(_payload({"enabled": True, "recovery_intent": "unknown"})),
        encoding="utf-8",
    )

    loaded = V2Config.load(path)

    assert loaded.memory.enabled is False
    assert loaded.memory.legacy_needs_repair is False
    assert any("memory.recovery_intent" in warning for warning in loaded.load_warnings)


def test_released_optional_endpoint_shapes_remain_stable(tmp_path: Path) -> None:
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": _complete_processing()})
    )

    assert config.memory.processing.rerank is None
    assert config.memory.processing.multimodal is None

    path = tmp_path / "config.json"
    config.save(path)
    stored = json.loads(path.read_text(encoding="utf-8"))["memory"]["processing"]
    assert "rerank" not in stored
    assert "multimodal" not in stored


def test_atomic_memory_update_writes_only_canonical_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir(parents=True)
    V2Config.from_payload(
        _payload({"enabled": False, "recovery_intent": "rebuild"})
    ).save(path)

    result = atomic_update_memory(
        lambda memory: memory,
        config_path=path,
    )

    assert result.memory.legacy_needs_repair is True
    stored = json.loads(path.read_text(encoding="utf-8"))["memory"]
    assert "recovery_intent" not in stored
    assert "embedding_change_pending" not in stored
    assert stored["repair_required"] is True


def test_successful_repair_can_clear_the_durable_compatibility_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir(parents=True)
    V2Config.from_payload(
        _payload({"enabled": False, "recovery_intent": "rebuild"})
    ).save(path)

    result = atomic_update_memory(
        lambda memory: replace(memory, legacy_needs_repair=False),
        config_path=path,
    )

    assert result.memory.legacy_needs_repair is False
    stored = json.loads(path.read_text(encoding="utf-8"))["memory"]
    assert "repair_required" not in stored


def test_public_memory_save_cannot_drop_the_durable_repair_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config.from_payload(
        _payload({"enabled": False, "recovery_intent": "rebuild"})
    )
    config.save()
    current = V2Config.load().memory

    saved = save_memory_config(
        memory_config_to_payload(current, include_secrets=True),
        expected=current,
    )

    assert saved.memory.legacy_needs_repair is True
    stored = json.loads(paths.get_config_path().read_text(encoding="utf-8"))["memory"]
    assert stored["repair_required"] is True
