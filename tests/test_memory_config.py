from __future__ import annotations

import json
import multiprocessing
import os

import pytest

from config.v2_config import (
    AgentsConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
    config_write_transaction,
)
from vibe import api
from vibe.api import config_to_payload


def _save_stale_general_config(
    avibe_home: str,
    loaded,
    release,
) -> None:
    os.environ["AVIBE_HOME"] = avibe_home
    stale = V2Config.load()
    loaded.set()
    if not release.wait(timeout=5):
        raise TimeoutError("test did not release stale general config writer")
    stale.runtime.log_level = "DEBUG"
    stale.save()


def _save_memory_candidate(avibe_home: str) -> None:
    os.environ["AVIBE_HOME"] = avibe_home
    api.save_memory_config(
        {
            "enabled": False,
            "processing": {
                "llm": {
                    "base_url": "https://llm.example.test/v1",
                    "model": "chat",
                    "api_key": "llm-key",
                },
                "embedding": {
                    "base_url": "https://embed.example.test/v1",
                    "model": "embed-v2",
                    "api_key": "embed-key",
                },
            },
        },
        embedding_change_pending=True,
    )


def _save_paused_memory_candidate(
    avibe_home: str,
    about_to_lock,
    release,
) -> None:
    os.environ["AVIBE_HOME"] = avibe_home
    from storage.lock import MigrationFileLock

    original_acquire = MigrationFileLock.acquire

    def acquire_after_release(lock) -> None:
        about_to_lock.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release stale Memory config writer")
        original_acquire(lock)

    MigrationFileLock.acquire = acquire_after_release
    _save_memory_candidate(avibe_home)


def _save_general_config(avibe_home: str) -> None:
    os.environ["AVIBE_HOME"] = avibe_home
    api.save_config({"runtime": {"log_level": "DEBUG"}})


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
    config = V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(),
                "diagnostics": {"log_provider_calls": True},
                "embedding_change_pending": True,
            }
        )
    )
    config.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    projected = config_to_payload(config)

    assert stored["memory"]["processing"]["llm"]["api_key"] == "llm-key"
    assert stored["memory"]["embedding_change_pending"] is True
    assert stored["memory"]["diagnostics"] == {"log_provider_calls": True}
    assert projected["memory"]["processing"]["llm"] == {
        "base_url": "https://llm.example.test/v1",
        "model": "chat",
        "api_key": None,
        "has_api_key": True,
    }
    assert "embed-key" not in json.dumps(projected)
    assert "embedding_change_pending" not in projected["memory"]
    assert projected["memory"]["diagnostics"] == {"log_provider_calls": True}


def test_memory_config_drops_retired_proactive_capture_flag(tmp_path) -> None:
    """A config written by a release that had the opt-in flag still loads."""

    upgraded = V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "proactive_capture": True,
                "processing": _complete_processing(),
            }
        )
    )
    upgraded.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "proactive_capture" not in stored["memory"]
    assert "proactive_capture" not in config_to_payload(upgraded)["memory"]


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


def test_memory_config_defaults_provider_logging_on_for_legacy_payload() -> None:
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )
    assert config.memory == MemoryConfig()
    assert config.memory.diagnostics.log_provider_calls is True


def test_memory_config_upgrades_disabled_provider_logging_to_always_on() -> None:
    config = V2Config.from_payload(
        _payload(
            {
                "enabled": False,
                "processing": {},
                "diagnostics": {"log_provider_calls": False},
            }
        )
    )

    assert config.memory.diagnostics.log_provider_calls is True
    assert config_to_payload(config)["memory"]["diagnostics"] == {
        "log_provider_calls": True,
    }


@pytest.mark.parametrize(
    "diagnostics",
    [True, [], {"log_provider_calls": "yes"}],
)
def test_memory_config_rejects_invalid_diagnostics(diagnostics: object) -> None:
    with pytest.raises(ValueError, match="memory.diagnostics"):
        V2Config.from_payload(
            _payload(
                {
                    "enabled": False,
                    "processing": {},
                    "diagnostics": diagnostics,
                }
            )
        )


def test_generic_config_save_preserves_memory_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    original = V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(),
                "embedding_change_pending": True,
            }
        )
    )
    original.save()

    saved = api.save_config({"runtime": {"log_level": "DEBUG"}})

    assert saved.runtime.log_level == "DEBUG"
    assert saved.memory.processing.llm.api_key == "llm-key"
    assert saved.memory.processing.embedding.api_key == "embed-key"
    assert saved.memory.embedding_change_pending is True


def test_config_write_transaction_reuses_same_path_lock(monkeypatch, tmp_path) -> None:
    from storage.lock import MigrationFileLock

    acquire_count = 0
    original_acquire = MigrationFileLock.acquire

    def counted_acquire(lock) -> None:
        nonlocal acquire_count
        acquire_count += 1
        original_acquire(lock)

    monkeypatch.setattr(MigrationFileLock, "acquire", counted_acquire)
    config_path = tmp_path / "config.json"
    config = V2Config.from_payload(_payload({"enabled": False, "processing": {}}))

    with config_write_transaction(config_path):
        config.save(config_path, preserve_memory=False)

    assert acquire_count == 1


def test_config_write_transaction_rejects_nested_different_path(tmp_path) -> None:
    first_path = tmp_path / "first" / "config.json"
    second_path = tmp_path / "second" / "config.json"

    with config_write_transaction(first_path):
        with pytest.raises(RuntimeError, match="different paths"):
            with config_write_transaction(second_path):
                pass


def test_stale_general_writer_cannot_overwrite_memory_candidate_across_processes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.from_payload(
        _payload(
            {
                "enabled": False,
                "processing": _complete_processing(),
            }
        )
    ).save()
    context = multiprocessing.get_context("spawn")
    loaded = context.Event()
    release = context.Event()
    general = context.Process(
        target=_save_stale_general_config,
        args=(str(tmp_path), loaded, release),
    )
    memory = context.Process(
        target=_save_memory_candidate,
        args=(str(tmp_path),),
    )

    general.start()
    try:
        assert loaded.wait(timeout=5)
        memory.start()
        memory.join(timeout=5)
        assert memory.exitcode == 0
        release.set()
        general.join(timeout=5)
        assert general.exitcode == 0
    finally:
        release.set()
        for process in (memory, general):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    persisted = V2Config.load()
    assert persisted.runtime.log_level == "DEBUG"
    assert persisted.memory.processing.embedding.model == "embed-v2"
    assert persisted.memory.embedding_change_pending is True


def test_stale_memory_writer_cannot_overwrite_general_config_across_processes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.from_payload(
        _payload(
            {
                "enabled": False,
                "processing": _complete_processing(),
            }
        )
    ).save()
    context = multiprocessing.get_context("spawn")
    about_to_lock = context.Event()
    release = context.Event()
    memory = context.Process(
        target=_save_paused_memory_candidate,
        args=(str(tmp_path), about_to_lock, release),
    )
    general = context.Process(
        target=_save_general_config,
        args=(str(tmp_path),),
    )

    memory.start()
    try:
        assert about_to_lock.wait(timeout=5)
        general.start()
        general.join(timeout=5)
        assert general.exitcode == 0
        release.set()
        memory.join(timeout=5)
        assert memory.exitcode == 0
    finally:
        release.set()
        for process in (general, memory):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    persisted = V2Config.load()
    assert persisted.runtime.log_level == "DEBUG"
    assert persisted.memory.processing.embedding.model == "embed-v2"
    assert persisted.memory.embedding_change_pending is True


def test_memory_save_uses_dedicated_config_writer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.from_payload(_payload({"enabled": False, "processing": {}})).save()

    saved = api.save_memory_config({"enabled": True, "processing": _complete_processing()})

    assert saved.memory.enabled is True
    assert saved.memory.processing.llm.api_key == "llm-key"
