from __future__ import annotations

import ast
import json
import multiprocessing
import threading
from pathlib import Path

import pytest

from config.v2_config import (
    AgentsConfig,
    CONFIG_LOCK,
    MemoryCloudConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryRecoveryIntent,
    RuntimeConfig,
    SlackConfig,
    V2Config,
    atomic_update_memory,
)
from core.memory.operation_lock import MemoryOperationLease
from vibe import api
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


def _complete_processing(*, llm_url: str = "https://llm.example.test/v1") -> dict:
    return {
        "llm": {"base_url": llm_url, "model": "chat", "api_key": "llm-key"},
        "embedding": {"base_url": "https://embed.example.test/v1", "model": "embed", "api_key": "embed-key"},
    }


# Budget for one interleaved config writer, shared by every test below so the
# value has a single owner. It looks like a handoff between two threads, but it
# has to cover *two* sequential load -> merge -> validate -> write cycles: a
# writer holds CONFIG_LOCK plus the Memory file lock across the rendezvous, so
# the other one cannot even start its own cycle until the first one is done.
# One cycle measures ~0.5s median and ~1.7s worst case on a fast dev machine, so
# the pair alone can reach several seconds before a slower CI runner is priced
# in; the previous 5s went flaky on the `memory-first` orders. These budgets are
# timing slack and never part of what the tests assert, so prefer them loose.
_WRITER_TIMEOUT_SECONDS = 30
# A writer parked on a rendezvous only gives up after `_WRITER_TIMEOUT_SECONDS`
# and still has its write to finish, so joining on that same budget could call a
# writer stuck one moment before it unwinds on its own.
_WRITER_JOIN_TIMEOUT_SECONDS = _WRITER_TIMEOUT_SECONDS * 2


def _config_writer(
    target,
    *,
    name: str,
    args: tuple = (),
) -> threading.Thread:
    """Build a config writer that cannot outlive the test session.

    `get_vibe_remote_dir()` reads `AVIBE_HOME` on every call and falls back to
    `Path.home()`, so a writer still running once monkeypatch has restored the
    environment resolves against the developer's real home -- measured: a
    non-daemon writer released after the session resolved
    `$HOME/.avibe/config/config.json`. A daemon thread is killed at interpreter
    shutdown rather than joined, so a writer nothing can release still cannot
    reach real user state.
    """

    return threading.Thread(target=target, name=name, args=args, daemon=True)


def _join_config_writers(
    *writers: threading.Thread,
    timeout: float = _WRITER_JOIN_TIMEOUT_SECONDS,
) -> None:
    """Join every writer, and fail here rather than leaving one running.

    A writer that outlives its test resolves against whatever the environment
    holds by then; while the session is still running that is the *next* test's
    config, which surfaces as a bogus assertion there instead of a hang here.
    `_config_writer` bounds what happens past the end of the session, and this
    reports the overrun at the test that caused it. Call it from a `finally` so
    a failed wait above still names the writer that actually hung.
    """

    stuck: list[str] = []
    for writer in writers:
        # An unstarted writer (an earlier wait failed before its `start()`)
        # reports the same `is_alive()` as a finished one, and `join()` would
        # raise on it.
        if writer.is_alive():
            writer.join(timeout)
        if writer.is_alive():
            stuck.append(writer.name)
    if stuck:
        pytest.fail(f"config writers still running: {', '.join(stuck)}")


def _stale_whole_config_writer(
    config_path,
    loaded,
    start,
    done,
) -> None:
    stale = V2Config.load(config_path)
    loaded.set()
    if not start.wait(_WRITER_TIMEOUT_SECONDS):
        raise TimeoutError("whole-config writer was not released")
    stale.language = "zh"
    stale.save(config_path)
    done.set()


def _memory_writer(
    config_path,
    action: str,
    ready,
    start,
    done,
) -> None:
    ready.set()
    if not start.wait(_WRITER_TIMEOUT_SECONDS):
        raise TimeoutError("Memory writer was not released")

    def update(memory: MemoryConfig) -> MemoryConfig:
        if action == "settle":
            memory.recovery_intent = None
        else:
            memory.processing.embedding.model = "embed-next"
            memory.recovery_intent = "rebuild"
        return memory

    atomic_update_memory(update, config_path=config_path)
    done.set()


class _ObservedConfigLock:
    def __init__(self, contender_entered: threading.Event) -> None:
        self._contender_entered = contender_entered

    def __enter__(self):
        if threading.current_thread().name == "second-config-save":
            self._contender_entered.set()
        CONFIG_LOCK.acquire()
        return self

    def __exit__(self, *_args) -> None:
        CONFIG_LOCK.release()


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
    assert stored["memory"]["recovery_intent"] == "rebuild"
    assert "embedding_change_pending" not in stored["memory"]
    assert stored["memory"]["diagnostics"] == {"log_provider_calls": True}
    assert projected["memory"]["processing"]["llm"] == {
        "base_url": "https://llm.example.test/v1",
        "model": "chat",
        "api_key": None,
        "has_api_key": True,
    }
    assert "embed-key" not in json.dumps(projected)
    assert "recovery_intent" not in projected["memory"]
    assert projected["memory"]["diagnostics"] == {"log_provider_calls": True}


def test_released_memory_config_without_rerank_loads_and_saves_without_shape_churn(
    tmp_path,
) -> None:
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": _complete_processing()})
    )

    assert config.memory.processing.rerank is None
    config.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "rerank" not in stored["memory"]["processing"]
    assert V2Config.load(tmp_path / "config.json").memory.processing.rerank is None


def test_released_memory_config_without_multimodal_has_no_shape_churn(tmp_path) -> None:
    """MEMORY-IM-ATTACH-003: legacy config remains opted out after round-trip."""

    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": _complete_processing()})
    )

    assert config.memory.processing.multimodal is None
    config.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "multimodal" not in stored["memory"]["processing"]
    assert V2Config.load(tmp_path / "config.json").memory.processing.multimodal is None


def test_empty_multimodal_endpoint_normalizes_to_absent(tmp_path) -> None:
    processing = _complete_processing()
    processing["multimodal"] = {}
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": processing})
    )

    assert config.memory.processing.multimodal is None
    config.save(tmp_path / "config.json")
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "multimodal" not in stored["memory"]["processing"]


def test_disk_load_degrades_only_a_malformed_optional_rerank_section(tmp_path) -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "base_url": "https://rerank.example.test/v1/inference",
        "model": "rerank-model",
    }
    payload = _payload(
        {
            "enabled": True,
            "recovery_intent": "rebuild",
            "processing": processing,
        }
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path)

    assert loaded.memory.enabled is True
    assert loaded.memory.processing.llm.model == "chat"
    assert loaded.memory.processing.embedding.model == "embed"
    assert loaded.memory.processing.rerank is None
    assert loaded.memory.recovery_intent == "rebuild"
    assert any("memory.rerank" in warning for warning in loaded.load_warnings)
    assert "rerank" in json.loads(config_path.read_text(encoding="utf-8"))["memory"]["processing"]


def test_disk_load_degrades_only_a_malformed_optional_multimodal_section(tmp_path) -> None:
    processing = _complete_processing()
    processing["multimodal"] = {
        "base_url": "https://vision.example.test/v1",
        "model": "vision-model",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            _payload(
                {
                    "enabled": True,
                    "recovery_intent": "rebuild",
                    "processing": processing,
                }
            )
        ),
        encoding="utf-8",
    )

    loaded = V2Config.load(config_path)

    assert loaded.memory.enabled is True
    assert loaded.memory.processing.llm.model == "chat"
    assert loaded.memory.processing.embedding.model == "embed"
    assert loaded.memory.processing.multimodal is None
    assert loaded.memory.recovery_intent == "rebuild"
    assert any("memory.multimodal" in warning for warning in loaded.load_warnings)


def test_memory_rerank_round_trips_without_projecting_its_key(tmp_path) -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "base_url": "https://rerank.example.test/v1/inference",
        "model": "rerank-model",
        "api_key": "rerank-secret",
    }
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": processing})
    )
    config.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    projected = config_to_payload(config)["memory"]["processing"]["rerank"]
    assert stored["memory"]["processing"]["rerank"]["api_key"] == "rerank-secret"
    assert stored["memory"]["processing"]["rerank"]["provider"] == "deepinfra"
    assert projected == {
        "base_url": "https://rerank.example.test/v1/inference",
        "model": "rerank-model",
        "api_key": None,
        "has_api_key": True,
        "provider": "deepinfra",
    }


def test_memory_multimodal_round_trips_without_projecting_its_key(tmp_path) -> None:
    """MEMORY-IM-ATTACH-001: a complete endpoint is the explicit opt-in."""

    processing = _complete_processing()
    processing["multimodal"] = {
        "base_url": "https://vision.example.test/v1",
        "model": "vision-model",
        "api_key": "vision-secret",
    }
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": processing})
    )
    config.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    projected = config_to_payload(config)["memory"]["processing"]["multimodal"]
    assert stored["memory"]["processing"]["multimodal"]["api_key"] == "vision-secret"
    assert projected == {
        "base_url": "https://vision.example.test/v1",
        "model": "vision-model",
        "api_key": None,
        "has_api_key": True,
    }


def test_memory_rerank_infers_dashscope_from_maas_url_when_provider_is_omitted() -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "base_url": "https://llm-space.example.maas.aliyuncs.com",
        "model": "gte-rerank-v2",
        "api_key": "rerank-secret",
    }
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": processing})
    )

    assert config.memory.processing.rerank.provider == "dashscope"
    assert config.memory.processing.rerank.rerank_provider() == "dashscope"


def test_memory_rerank_infers_dashscope_from_maas_url_with_explicit_port() -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "base_url": "https://llm-space.example.maas.aliyuncs.com:443",
        "model": "gte-rerank-v2",
        "api_key": "rerank-secret",
    }
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": processing})
    )

    assert config.memory.processing.rerank.provider == "dashscope"


def test_memory_rerank_keeps_omitted_provider_as_deepinfra_for_gte_model_name() -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "base_url": "https://api.deepinfra.com/v1/inference",
        "model": "gte-rerank-v2",
        "api_key": "rerank-secret",
    }
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": processing})
    )

    assert config.memory.processing.rerank.provider == "deepinfra"
    assert config.memory.processing.rerank.rerank_provider() == "deepinfra"


def test_memory_rerank_persists_explicit_provider(tmp_path) -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "provider": "dashscope",
        "base_url": "https://dashscope.aliyuncs.com",
        "model": "gte-rerank-v2",
        "api_key": "rerank-secret",
    }
    config = V2Config.from_payload(
        _payload({"enabled": True, "processing": processing})
    )
    config.save(tmp_path / "config.json")

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    loaded = V2Config.load(tmp_path / "config.json")
    assert stored["memory"]["processing"]["rerank"]["provider"] == "dashscope"
    assert loaded.memory.processing.rerank.rerank_provider() == "dashscope"
    assert loaded.memory.processing.rerank.model == "gte-rerank-v2"


def test_memory_rerank_rejects_unknown_provider() -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "provider": "cohere",
        "base_url": "https://rerank.example.test/v1/inference",
        "model": "rerank-model",
        "api_key": "rerank-secret",
    }
    with pytest.raises(ValueError, match="provider must be deepinfra, vllm, or dashscope"):
        V2Config.from_payload(_payload({"enabled": False, "processing": processing}))


def test_memory_rerank_rejects_unsupported_dashscope_model() -> None:
    processing = _complete_processing()
    processing["rerank"] = {
        "provider": "dashscope",
        "base_url": "https://dashscope.aliyuncs.com",
        "model": "qwen3-vl-rerank",
        "api_key": "rerank-secret",
    }
    with pytest.raises(ValueError, match="gte-rerank-v2"):
        V2Config.from_payload(_payload({"enabled": False, "processing": processing}))


@pytest.mark.parametrize("missing", ["base_url", "model", "api_key"])
def test_memory_config_rejects_partially_filled_multimodal(missing: str) -> None:
    processing = _complete_processing()
    multimodal = {
        "base_url": "https://vision.example.test/v1",
        "model": "vision-model",
        "api_key": "vision-secret",
    }
    multimodal.pop(missing)
    processing["multimodal"] = multimodal

    with pytest.raises(ValueError, match="Memory multimodal endpoint must include"):
        V2Config.from_payload(
            _payload({"enabled": False, "processing": processing})
        )


@pytest.mark.parametrize("missing", ["base_url", "model", "api_key"])
def test_memory_config_rejects_partially_filled_rerank(missing: str) -> None:
    processing = _complete_processing()
    rerank = {
        "base_url": "https://rerank.example.test/v1/inference",
        "model": "rerank-model",
        "api_key": "rerank-secret",
    }
    rerank.pop(missing)
    processing["rerank"] = rerank

    with pytest.raises(ValueError, match="Memory rerank endpoint must include"):
        V2Config.from_payload(
            _payload({"enabled": False, "processing": processing})
        )


@pytest.mark.parametrize(
    "memory",
    [
        {"embedding_change_pending": False, "recovery_intent": "rebuild"},
        {"embedding_change_pending": True, "recovery_intent": None},
    ],
)
def test_memory_config_rejects_conflicting_recovery_fields(memory: dict) -> None:
    with pytest.raises(ValueError, match="conflicting recovery intent"):
        V2Config.from_payload(_payload(memory))


@pytest.mark.parametrize("intent", ["unknown", True, [], {}])
def test_memory_config_rejects_unknown_recovery_intent(intent: object) -> None:
    with pytest.raises(ValueError, match="memory.recovery_intent"):
        V2Config.from_payload(_payload({"recovery_intent": intent}))


def test_memory_config_accepts_factory_reset_recovery_intent() -> None:
    config = V2Config.from_payload(_payload({"recovery_intent": "factory_reset"}))
    assert config.memory.recovery_intent == "factory_reset"


@pytest.mark.parametrize(
    ("initial", "expected", "armed"),
    [
        (None, "rebuild", True),
        ("rebuild", "rebuild", False),
        ("factory_reset", "factory_reset", False),
    ],
)
def test_memory_rebuild_request_never_downgrades_recovery(
    initial: MemoryRecoveryIntent | None,
    expected: MemoryRecoveryIntent,
    armed: bool,
) -> None:
    memory = MemoryConfig(recovery_intent=initial)

    assert memory.arm_rebuild_if_idle() is armed
    assert memory.recovery_intent == expected


@pytest.mark.parametrize(
    "cloud",
    [
        [],
        {"capabilities": []},
        {"scope": "unsupported"},
        {"proxy_base_url": 7},
        {"model_access_key": 7},
        {"transition_rebuild_owned": True},
    ],
)
@pytest.mark.parametrize(
    ("mode", "recovery_intent", "runtime_source"),
    [
        ("custom", "factory_reset", "custom"),
        ("platform", None, "unavailable"),
    ],
)
def test_disk_load_recovers_only_a_malformed_memory_cloud_cache(
    tmp_path,
    cloud: object,
    mode: str,
    recovery_intent: MemoryRecoveryIntent | None,
    runtime_source: str,
) -> None:
    config_path = tmp_path / "config.json"
    memory = {
        "enabled": True,
        "mode": mode,
        "recovery_intent": recovery_intent,
        "processing": _complete_processing(),
        "cloud": cloud,
    }
    payload = _payload(memory)

    with pytest.raises((TypeError, ValueError)):
        V2Config.from_payload(payload)
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path)

    assert loaded.memory.enabled is True
    assert loaded.memory.mode == mode
    assert loaded.memory.custom_processing_complete() is True
    assert loaded.memory.processing.llm.api_key == "llm-key"
    assert loaded.memory.processing.embedding.api_key == "embed-key"
    assert loaded.memory.recovery_intent == (
        recovery_intent
        or (
            "rebuild"
            if mode == "platform" and not isinstance(cloud, dict)
            else None
        )
    )
    assert loaded.memory.cloud == MemoryCloudConfig()
    assert loaded.memory.runtime_source() == runtime_source
    assert loaded.memory.cloud_runtime_selected() is (mode == "platform")
    assert loaded.memory.settings_mode() == mode
    assert any("memory.cloud" in warning for warning in loaded.load_warnings)
    assert json.loads(config_path.read_text(encoding="utf-8"))["memory"]["cloud"] == cloud


def _acknowledged_organization_cloud() -> dict:
    return {
        "scope": "organization",
        "capabilities": {
            "asr": False,
            "chat": True,
            "embedding": True,
            "multimodal": False,
            "memory_llm": True,
        },
        "memory_llm_source": "chat_fallback",
        "embedding_identity": "emb-org",
        "revision": 4,
        "quota_enforced": False,
        "model_access_key": "mak_org",
        "proxy_base_url": "https://backend.example.test/v1/model",
        "source_instance_id": "instance-org",
        "organization_attached": True,
        "transition_notice_pending": False,
        "transition_rebuild_owned": False,
        "applied_embedding_identity": "emb-org",
        "runtime_apply_pending": False,
    }


def test_memory_cloud_persists_effective_memory_llm_source() -> None:
    config = V2Config.from_payload(
        _payload(
            {
                "cloud": {
                    "capabilities": {
                        "asr": False,
                        "chat": False,
                        "embedding": True,
                        "multimodal": False,
                        "memory_llm": True,
                    },
                    "memory_llm_source": "dedicated",
                    "embedding_identity": "emb-dedicated",
                }
            }
        )
    )

    assert config.memory.cloud.memory_llm_source == "dedicated"
    assert config.memory.cloud.capabilities.memory_available() is True

    payload = config_to_payload(config)
    assert payload["memory"]["cloud"]["memory_llm_source"] == "dedicated"
    assert payload["memory"]["cloud"]["capabilities"]["memory_llm"] is True


@pytest.mark.parametrize(
    ("chat", "source"),
    [(False, "chat_fallback"), (True, "future_source")],
)
def test_disk_recovery_of_untrusted_memory_llm_source_disables_cloud_memory(
    tmp_path,
    chat: bool,
    source: str,
) -> None:
    config_path = tmp_path / "config.json"
    cloud = _acknowledged_organization_cloud()
    cloud["capabilities"]["chat"] = chat
    cloud["memory_llm_source"] = source
    payload = _payload(
        {
            "enabled": True,
            "mode": "platform",
            "processing": _complete_processing(),
            "cloud": cloud,
        }
    )

    with pytest.raises(ValueError, match="memory_llm_source"):
        V2Config.from_payload(payload)
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path)

    assert loaded.memory.cloud.memory_llm_source is None
    assert loaded.memory.cloud.capabilities.memory_llm is False
    assert loaded.memory.cloud.runtime_ready() is False
    assert loaded.memory.runtime_source() == "unavailable"


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("scope", "unsupported"),
        ("memory_llm_source", "unsupported"),
        ("capabilities", []),
        ("embedding_identity", 7),
        ("revision", "four"),
        ("quota_enforced", []),
        ("model_access_key", 7),
        ("proxy_base_url", 7),
        ("source_instance_id", 7),
        ("organization_attached", []),
        ("transition_notice_pending", []),
        ("transition_rebuild_owned", []),
        ("applied_embedding_identity", 7),
        ("runtime_apply_pending", []),
    ],
)
def test_disk_cloud_field_recovery_never_reactivates_custom_for_an_acknowledged_org(
    tmp_path,
    field: str,
    invalid: object,
) -> None:
    config_path = tmp_path / "config.json"
    cloud = _acknowledged_organization_cloud()
    cloud[field] = invalid
    payload = _payload(
        {
            "enabled": True,
            "mode": "custom",
            "processing": _complete_processing(),
            "cloud": cloud,
        }
    )
    payload["remote_access"] = {
        "vibe_cloud": {
            "enabled": True,
            "instance_id": "instance-org",
            "instance_secret": "device-secret",
            "instance_kind": "organization",
        }
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path)

    assert loaded.memory.custom_processing_complete() is True
    assert loaded.memory.processing.llm.api_key == "llm-key"
    assert loaded.memory.processing.embedding.api_key == "embed-key"
    assert loaded.memory.settings_mode() == "organization"
    assert loaded.memory.cloud_runtime_selected() is True
    assert loaded.memory.runtime_source() != "custom"
    assert any("memory.cloud" in warning for warning in loaded.load_warnings)


@pytest.mark.parametrize("instance_kind", ["organization", ""])
def test_disk_whole_cloud_recovery_fails_closed_for_managed_or_unknown_pairing(
    tmp_path,
    instance_kind: str,
) -> None:
    config_path = tmp_path / "config.json"
    payload = _payload(
        {
            "enabled": False,
            "mode": "custom",
            "processing": _complete_processing(),
            "cloud": [],
        }
    )
    payload["remote_access"] = {
        "vibe_cloud": {
            "enabled": True,
            "instance_id": "instance-org",
            "instance_secret": "device-secret",
            "instance_kind": instance_kind,
        }
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path)

    assert loaded.memory.settings_mode() == "organization"
    assert loaded.memory.runtime_source() == "unavailable"
    assert loaded.memory.recovery_intent == "rebuild"
    assert loaded.memory.enabled is False
    assert loaded.memory.custom_processing_complete() is True


def test_disk_whole_cloud_recovery_keeps_personal_custom_runtime(
    tmp_path,
) -> None:
    config_path = tmp_path / "config.json"
    payload = _payload(
        {
            "enabled": True,
            "mode": "custom",
            "processing": _complete_processing(),
            "cloud": [],
        }
    )
    payload["remote_access"] = {
        "vibe_cloud": {
            "enabled": True,
            "instance_id": "instance-personal",
            "instance_secret": "device-secret",
            "instance_kind": "personal",
        }
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path)

    assert loaded.memory.settings_mode() == "custom"
    assert loaded.memory.runtime_source() == "custom"
    assert loaded.memory.recovery_intent is None


def test_disk_cloud_field_recovery_preserves_proven_no_pair_org_grandfathering(
    tmp_path,
) -> None:
    config_path = tmp_path / "config.json"
    payload = _payload(
        {
            "enabled": True,
            "mode": "custom",
            "processing": _complete_processing(),
            "cloud": {
                "scope": "organization",
                "capabilities": {
                    "asr": False,
                    "chat": False,
                    "embedding": False,
                    "multimodal": False,
                },
                "revision": "unreadable",
                "organization_attached": False,
            },
        }
    )
    payload["remote_access"] = {
        "vibe_cloud": {
            "enabled": True,
            "instance_id": "instance-org",
            "instance_secret": "device-secret",
            "instance_kind": "organization",
        }
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path)

    assert loaded.memory.settings_mode() == "custom"
    assert loaded.memory.runtime_source() == "custom"
    assert loaded.memory.recovery_intent is None
    assert loaded.memory.custom_processing_complete() is True


def test_memory_transition_rebuild_owner_round_trips(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config = V2Config.from_payload(
        _payload(
            {
                "recovery_intent": "rebuild",
                "cloud": {
                    "transition_notice_pending": True,
                    "transition_rebuild_owned": True,
                },
            }
        )
    )

    config.save(config_path)

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    loaded = V2Config.load(config_path)
    assert stored["memory"]["cloud"]["transition_rebuild_owned"] is True
    assert loaded.memory.cloud.transition_rebuild_owned is True
    assert loaded.memory.recovery_intent == "rebuild"


@pytest.mark.parametrize(
    "memory",
    [
        {
            "recovery_intent": "rebuild",
            "cloud": {"transition_rebuild_owned": True},
        },
        {
            "recovery_intent": "factory_reset",
            "cloud": {
                "transition_notice_pending": True,
                "transition_rebuild_owned": True,
            },
        },
    ],
)
def test_memory_config_rejects_unowned_transition_rebuild_state(memory: dict) -> None:
    with pytest.raises(ValueError, match="transition_rebuild_owned"):
        V2Config.from_payload(_payload(memory))


@pytest.mark.parametrize(
    ("field", "expected_name"),
    [
        ("memory", "memory"),
        ("processing", "memory.processing"),
        ("llm", "memory.processing.llm"),
        ("embedding", "memory.processing.embedding"),
        ("diagnostics", "memory.diagnostics"),
    ],
)
@pytest.mark.parametrize("invalid", [[], False, "", 0, 1.5])
def test_memory_config_rejects_non_object_sections(
    field: str,
    expected_name: str,
    invalid: object,
) -> None:
    if field == "memory":
        memory = invalid
    elif field == "processing":
        memory = {"processing": invalid}
    elif field in {"llm", "embedding"}:
        memory = {"processing": {field: invalid}}
    else:
        memory = {"diagnostics": invalid}

    with pytest.raises(ValueError, match=rf"Config '{expected_name}' must be an object"):
        V2Config.from_payload(_payload(memory))


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


def test_fresh_config_save_fsyncs_parent_after_replace(monkeypatch, tmp_path) -> None:
    from config import v2_config

    config_path = tmp_path / "config.json"
    observed: list[tuple[Path, str]] = []

    def observe_directory_sync(directory: Path) -> None:
        observed.append((directory, config_path.read_text(encoding="utf-8")))

    monkeypatch.setattr(v2_config, "_fsync_directory", observe_directory_sync)

    V2Config.from_payload(
        _payload({"recovery_intent": "rebuild"})
    ).save(config_path)

    assert len(observed) == 1
    assert observed[0][0] == tmp_path
    assert json.loads(observed[0][1])["memory"]["recovery_intent"] == "rebuild"


def test_config_save_cleans_temporary_file_when_replace_fails(
    monkeypatch,
    tmp_path,
) -> None:
    from config import v2_config

    config_path = tmp_path / "config.json"
    config = V2Config.from_payload(_payload({"recovery_intent": "rebuild"}))
    config.save(config_path)
    original = config_path.read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(v2_config.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        config.language = "zh"
        config.save(config_path)

    assert config_path.read_bytes() == original
    assert list(tmp_path.glob(".config.json.*.tmp")) == []


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


def test_memory_config_defaults_when_block_is_absent() -> None:
    payload = _payload({})
    payload.pop("memory")

    config = V2Config.from_payload(payload)

    assert config.memory == MemoryConfig()


def test_memory_config_rejects_explicit_null_block() -> None:
    with pytest.raises(ValueError, match="Config 'memory' must be an object"):
        V2Config.from_payload(_payload(None))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "memory",
    [
        {"processing": None},
        {"processing": {"llm": None, "embedding": {"base_url": "x", "model": "m"}}},
        {"processing": {"llm": {"base_url": "x", "model": "m"}, "embedding": None}},
        {"diagnostics": None},
    ],
)
def test_memory_config_rejects_explicit_null_nested_blocks(memory: dict) -> None:
    """An explicitly null processing/llm/embedding block is corruption, not an omission."""
    with pytest.raises(ValueError, match="must be an object"):
        V2Config.from_payload(_payload(memory))


def test_generic_config_save_preserves_memory_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    original = V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(),
                "recovery_intent": "rebuild",
            }
        )
    )
    original.save()

    saved = api.save_config({"runtime": {"log_level": "DEBUG"}})

    assert saved.runtime.log_level == "DEBUG"
    assert saved.memory.processing.llm.api_key == "llm-key"
    assert saved.memory.processing.embedding.api_key == "embed-key"
    assert saved.memory.recovery_intent == "rebuild"


def test_config_writers_cannot_outlive_the_test_session() -> None:
    """No writer can resolve a config path once the isolation is restored.

    Asserted at the single owner, plus the property that every writer is built
    there -- listing today's writers would pass forever while the next one
    added silently reopens the escape.
    """

    assert _config_writer(lambda: None, name="probe").daemon is True

    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    factory = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_config_writer"
    )
    built_outside_the_factory = [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Thread"
        and not (factory.lineno <= node.lineno <= factory.end_lineno)
    ]
    assert built_outside_the_factory == []


def test_join_config_writers_reports_a_writer_that_overruns() -> None:
    """An overrunning writer is named here instead of leaking into a later test.

    The `finally` releases the worker so this test does not itself leak one.
    Containment for a writer that *cannot* be released is a separate property,
    owned by `test_config_writers_cannot_outlive_the_test_session`.
    """

    release = threading.Event()
    worker = _config_writer(
        release.wait,
        args=(_WRITER_TIMEOUT_SECONDS,),
        name="slow-writer",
    )
    worker.start()
    try:
        with pytest.raises(pytest.fail.Exception, match="slow-writer"):
            _join_config_writers(worker, timeout=0.05)
    finally:
        release.set()
        _join_config_writers(worker)


def test_concurrent_generic_config_saves_preserve_both_updates(
    monkeypatch,
    tmp_path,
) -> None:
    """Generic read/merge/write stays one process-local linearized operation."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.from_payload(_payload({})).save()
    first_merged = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []
    merge = api._deep_merge_dicts

    def hold_first_merge(base: dict, update: dict) -> dict:
        merged = merge(base, update)
        if threading.current_thread().name == "first-config-save":
            first_merged.set()
            if not release_first.wait(_WRITER_TIMEOUT_SECONDS):
                raise TimeoutError("first config save was not released")
        return merged

    def save(update: dict) -> None:
        try:
            api.save_config(update)
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(api, "CONFIG_LOCK", _ObservedConfigLock(second_entered))
    monkeypatch.setattr(api, "_deep_merge_dicts", hold_first_merge)
    first = _config_writer(
        save,
        args=({"language": "zh"},),
        name="first-config-save",
    )
    second = _config_writer(
        save,
        args=({"runtime": {"log_level": "DEBUG"}},),
        name="second-config-save",
    )

    try:
        first.start()
        assert first_merged.wait(_WRITER_TIMEOUT_SECONDS)
        second.start()
        assert second_entered.wait(_WRITER_TIMEOUT_SECONDS)
    finally:
        release_first.set()
        _join_config_writers(first, second)

    assert failures == []
    persisted = V2Config.load()
    assert persisted.language == "zh"
    assert persisted.runtime.log_level == "DEBUG"


@pytest.mark.parametrize("memory_action", ["candidate", "settle"])
@pytest.mark.parametrize("memory_first", [False, True], ids=["config-first", "memory-first"])
def test_generic_config_save_preserves_interleaved_memory_update(
    monkeypatch,
    tmp_path,
    memory_action: str,
    memory_first: bool,
) -> None:
    """Generic and Memory writers preserve both units in either lock order."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    initial_intent = "rebuild" if memory_action == "settle" else None
    V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(),
                "recovery_intent": initial_intent,
            }
        )
    ).save()
    rendezvous = threading.Barrier(2, timeout=_WRITER_TIMEOUT_SECONDS)
    failures: list[BaseException] = []
    merge = api._deep_merge_dicts

    def hold_config_first(base: dict, update: dict) -> dict:
        merged = merge(base, update)
        if not memory_first:
            rendezvous.wait()
        return merged

    def update_memory(memory: MemoryConfig) -> MemoryConfig:
        if memory_first:
            rendezvous.wait()
        if memory_action == "settle":
            memory.recovery_intent = None
        else:
            memory.processing.embedding.model = "embed-next"
            memory.recovery_intent = "rebuild"
        return memory

    def save_generic() -> None:
        try:
            if memory_first:
                rendezvous.wait()
            api.save_config({"language": "zh"})
        except BaseException as exc:
            failures.append(exc)

    def save_memory() -> None:
        try:
            if not memory_first:
                rendezvous.wait()
            atomic_update_memory(update_memory)
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(api, "_deep_merge_dicts", hold_config_first)
    generic_thread = _config_writer(save_generic, name="generic-config-save")
    memory_thread = _config_writer(save_memory, name="memory-config-save")
    first, second = (
        (memory_thread, generic_thread)
        if memory_first
        else (generic_thread, memory_thread)
    )
    try:
        first.start()
        second.start()
    finally:
        _join_config_writers(first, second)

    assert failures == []
    persisted = V2Config.load()
    assert persisted.language == "zh"
    if memory_action == "settle":
        assert persisted.memory.recovery_intent is None
        assert persisted.memory.processing.embedding.model == "embed"
    else:
        assert persisted.memory.recovery_intent == "rebuild"
        assert persisted.memory.processing.embedding.model == "embed-next"


def test_memory_save_uses_dedicated_config_writer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.from_payload(_payload({"enabled": False, "processing": {}})).save()

    saved = api.save_memory_config({"enabled": True, "processing": _complete_processing()})

    assert saved.memory.enabled is True
    assert saved.memory.processing.llm.api_key == "llm-key"


def test_memory_save_is_zero_write_while_operation_lease_is_busy(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.from_payload(_payload({"enabled": False, "processing": {}})).save()
    config_path = tmp_path / "config" / "config.json"
    before = config_path.read_bytes()
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()

    try:
        with pytest.raises(api.MemoryOperationBusy):
            api.save_memory_config(
                {"enabled": True, "processing": _complete_processing()}
            )
        assert config_path.read_bytes() == before

        # Payload validation happens before admission and keeps its public error.
        with pytest.raises(ValueError):
            api.save_memory_config({"enabled": "not-a-boolean"})
    finally:
        lease.release()


def test_direct_v2config_writer_preserves_settled_memory_marker(monkeypatch, tmp_path) -> None:
    """Language/Model Hub style writers must not re-arm a cleared rebuild marker."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    original = V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(),
                "recovery_intent": "rebuild",
            }
        )
    )
    original.save()

    # Stale writer loaded while the marker was still set.
    stale = V2Config.load()
    assert stale.memory.recovery_intent == "rebuild"

    # Controller settlement clears the marker under the shared transaction.
    def settle(memory: MemoryConfig) -> MemoryConfig:
        memory.recovery_intent = None
        return memory

    atomic_update_memory(settle)

    # Direct non-Memory writer (language/model-hub pattern) must keep settlement.
    stale.language = "zh"
    stale.save()

    persisted = V2Config.load()
    assert stale.memory.recovery_intent == "rebuild"
    assert persisted.language == "zh"
    assert persisted.memory.recovery_intent is None
    assert persisted.memory.processing.embedding.api_key == "embed-key"


def test_atomic_memory_mutator_failure_does_not_write_and_releases_lock(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(),
                "recovery_intent": "rebuild",
            }
        )
    ).save(config_path)
    before = config_path.read_bytes()

    def fail(memory: MemoryConfig) -> MemoryConfig:
        memory.recovery_intent = None
        raise RuntimeError("settlement failed")

    with pytest.raises(RuntimeError, match="settlement failed"):
        atomic_update_memory(fail, config_path=config_path)

    assert config_path.read_bytes() == before

    def settle(memory: MemoryConfig) -> MemoryConfig:
        memory.recovery_intent = None
        return memory

    saved = atomic_update_memory(settle, config_path=config_path)
    assert saved.memory.recovery_intent is None
    assert V2Config.load(config_path).memory.recovery_intent is None


@pytest.mark.parametrize("memory_action", ["candidate", "settle"])
@pytest.mark.parametrize("memory_first", [False, True], ids=["config-first", "memory-first"])
def test_spawned_writers_preserve_memory_and_non_memory_updates(
    monkeypatch,
    tmp_path,
    memory_action: str,
    memory_first: bool,
) -> None:
    """The file lock serializes stale config writes with Memory-only updates."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    initial_intent = "rebuild" if memory_action == "settle" else None
    V2Config.from_payload(
        _payload(
            {
                "enabled": True,
                "processing": _complete_processing(),
                "recovery_intent": initial_intent,
            }
        )
    ).save(config_path)

    context = multiprocessing.get_context("spawn")
    stale_loaded = context.Event()
    memory_ready = context.Event()
    config_start = context.Event()
    memory_start = context.Event()
    config_done = context.Event()
    memory_done = context.Event()
    config_process = context.Process(
        target=_stale_whole_config_writer,
        args=(config_path, stale_loaded, config_start, config_done),
    )
    memory_process = context.Process(
        target=_memory_writer,
        args=(config_path, memory_action, memory_ready, memory_start, memory_done),
    )
    processes = [config_process, memory_process]

    try:
        for process in processes:
            process.start()
        assert stale_loaded.wait(_WRITER_TIMEOUT_SECONDS)
        assert memory_ready.wait(_WRITER_TIMEOUT_SECONDS)

        first_start, first_done, second_start = (
            (memory_start, memory_done, config_start)
            if memory_first
            else (config_start, config_done, memory_start)
        )
        first_start.set()
        assert first_done.wait(_WRITER_TIMEOUT_SECONDS)
        second_start.set()

        assert config_done.wait(_WRITER_TIMEOUT_SECONDS)
        assert memory_done.wait(_WRITER_TIMEOUT_SECONDS)
        for process in processes:
            process.join(_WRITER_TIMEOUT_SECONDS)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(1)

    persisted = V2Config.load(config_path)
    assert persisted.language == "zh"
    assert persisted.memory.processing.embedding.api_key == "embed-key"
    if memory_action == "settle":
        assert persisted.memory.recovery_intent is None
        assert persisted.memory.processing.embedding.model == "embed"
    else:
        assert persisted.memory.recovery_intent == "rebuild"
        assert persisted.memory.processing.embedding.model == "embed-next"
