from __future__ import annotations

import json

import pytest

from config.v2_config import AgentsConfig, MemoryConfig, MemoryEndpointConfig, RuntimeConfig, SlackConfig, V2Config
from vibe import api
from vibe.api import config_to_payload


def _payload(memory: dict) -> dict:
    return {
        "mode": "self_host",
        "version": "v2",
        "slack": {"bot_token": ""},
        "runtime": {"default_cwd": "."},
        "agents": {},
        "memory": memory,
    }


def _complete_processing(*, llm_url: str = "https://llm.example.test/v1") -> dict:
    return {
        "llm": {"base_url": llm_url, "model": "chat", "api_key": "llm-key"},
        "embedding": {"base_url": "https://embed.example.test/v1", "model": "embed", "api_key": "embed-key"},
    }


def test_memory_config_round_trips_and_hides_keys(tmp_path) -> None:
    config = V2Config.from_payload(_payload({"enabled": True, "processing": _complete_processing()}))
    config.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    projected = config_to_payload(config)

    assert stored["memory"]["processing"]["llm"]["api_key"] == "llm-key"
    assert projected["memory"]["processing"]["llm"] == {
        "base_url": "https://llm.example.test/v1",
        "model": "chat",
        "api_key": None,
        "has_api_key": True,
    }
    assert "embed-key" not in json.dumps(projected)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://example.test/v1",
        "https://example.test/v1?token=x",
        "https://user:pass@example.test/v1",
        "ftp://127.0.0.1/v1",
    ],
)
def test_memory_config_rejects_unsafe_endpoint_urls(url: str) -> None:
    processing = _complete_processing(llm_url=url)
    with pytest.raises(ValueError):
        V2Config.from_payload(_payload({"enabled": False, "processing": processing}))


def test_memory_config_allows_numeric_loopback_http() -> None:
    config = V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(llm_url="http://127.0.0.1:11434/v1"),
            }
        )
    )
    assert config.memory.processing.llm.base_url == "http://127.0.0.1:11434/v1"


def test_memory_endpoint_repr_never_exposes_api_key() -> None:
    endpoint = MemoryEndpointConfig(
        base_url="https://llm.example.test/v1",
        model="chat",
        api_key="memory-config-secret",
    )

    assert "memory-config-secret" not in repr(endpoint)


def test_memory_enable_requires_complete_authenticated_processing_config() -> None:
    processing = _complete_processing()
    processing["embedding"].pop("api_key")
    with pytest.raises(ValueError, match="Both Memory processing endpoints"):
        V2Config.from_payload(_payload({"enabled": True, "processing": processing}))


def test_memory_config_defaults_disabled_for_legacy_payload() -> None:
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )
    assert config.memory == MemoryConfig()


def test_generic_config_save_preserves_memory_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    original = V2Config.from_payload(_payload({"enabled": True, "processing": _complete_processing()}))
    original.save()

    saved = api.save_config({"runtime": {"log_level": "DEBUG"}})

    assert saved.runtime.log_level == "DEBUG"
    assert saved.memory.processing.llm.api_key == "llm-key"
    assert saved.memory.processing.embedding.api_key == "embed-key"


def test_memory_save_uses_dedicated_config_writer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.from_payload(_payload({"enabled": False, "processing": {}})).save()

    saved = api.save_memory_config({"enabled": True, "processing": _complete_processing()})

    assert saved.memory.enabled is True
    assert saved.memory.processing.llm.api_key == "llm-key"
