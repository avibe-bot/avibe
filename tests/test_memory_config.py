from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.v2_config import MemoryConfig, MemoryEndpointConfig, V2Config, atomic_update_memory
from vibe.api import config_to_payload


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
def test_released_recovery_fields_are_one_time_needs_repair_input(
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
    loaded = V2Config.load(path)
    assert loaded.memory.legacy_needs_repair is False


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

    assert result.memory.legacy_needs_repair is False
    stored = json.loads(path.read_text(encoding="utf-8"))["memory"]
    assert "recovery_intent" not in stored
    assert "embedding_change_pending" not in stored
