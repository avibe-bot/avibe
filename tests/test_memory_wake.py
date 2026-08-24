from __future__ import annotations

import sys
from pathlib import Path

import pytest

import core.memory.runtime as runtime_module
from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.artifact import FakeMemoryArtifactManager
from core.memory.everos import FakeMemoryProvider, ProviderHealthSnapshot
from core.memory.process import FakeEverOSProcessFactory


def _config(*, legacy_needs_repair: bool = False) -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
        legacy_needs_repair=legacy_needs_repair,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig(
                "https://embed.test/v1",
                "embed",
                "embed-key",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_wake_reuses_existing_root_and_proves_native_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    artifact = FakeMemoryArtifactManager(python=Path(sys.executable))
    processes = FakeEverOSProcessFactory()
    provider = FakeMemoryProvider(
        health_snapshot_value=ProviderHealthSnapshot(
            status="ok",
            version="test",
            capabilities={"embed": True},
            disabled_features=(),
            cascade={},
        )
    )
    monkeypatch.setattr(runtime_module, "EverOSPort", lambda *args, **kwargs: provider)
    runtime = memory_runtime_factory(
        _config(),
        artifact_manager=artifact,
        process_factory=processes,
        effective_home=tmp_path,
    )
    sentinel = tmp_path / "memory" / "user-data.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep", encoding="utf-8")

    result = await runtime.wake()

    assert result == {"ok": True, "state": "running"}
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert artifact.ensure_calls == []
    assert len(processes.supervised) == 1
    assert processes.supervised[0].running


@pytest.mark.asyncio
async def test_wake_never_routes_needs_repair_into_deletion(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    artifact = FakeMemoryArtifactManager(python=Path(sys.executable))
    runtime = memory_runtime_factory(
        _config(legacy_needs_repair=True),
        artifact_manager=artifact,
        effective_home=tmp_path,
    )
    sentinel = tmp_path / "memory" / "user-data.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep", encoding="utf-8")

    result = await runtime.wake()

    assert result == {
        "ok": False,
        "state": "needs_repair",
        "error": "memory_legacy_recovery_required",
    }
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert artifact.ensure_calls == []


@pytest.mark.asyncio
async def test_wake_artifact_failure_is_degraded_and_non_destructive(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    artifact = FakeMemoryArtifactManager(
        python=None,
        status_payload={
            "installed": False,
            "status": "missing",
            "reason": "memory_runtime_missing",
        },
        ensure_payload={
            "ok": False,
            "reason": "memory_runtime_install_failed",
            "download_error": None,
        },
    )
    runtime = memory_runtime_factory(
        _config(),
        artifact_manager=artifact,
        effective_home=tmp_path,
    )
    sentinel = tmp_path / "memory" / "user-data.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep", encoding="utf-8")

    result = await runtime.wake()

    assert result == {
        "ok": False,
        "state": "degraded",
        "error": "memory_runtime_install_failed",
    }
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert artifact.ensure_calls == [True]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PermissionError("denied"), "memory_permission_denied"),
        (OSError(28, "full"), "memory_disk_unavailable"),
    ],
)
def test_external_local_faults_are_degraded_not_repair_eligible(
    error: Exception,
    reason: str,
) -> None:
    assert runtime_module._degraded_runtime_reason(error) == reason
    assert runtime_module._local_data_failure_requires_repair(error) is False
