from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

import core.memory.runtime as runtime_module
from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.artifact import (
    EVEROS_VERSION,
    FakeMemoryArtifactManager,
    MemoryArtifactManager,
)
from core.memory.confined_filesystem import ConfinedFilesystemError
from core.memory.everos import FakeMemoryProvider, ProviderHealthSnapshot
from core.memory.operation_lock import MemoryOperationBusy
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
    """MEMORY-WAKE-001: Wake preserves the existing root."""

    event_loop_thread = threading.get_ident()
    admission_threads: list[int] = []

    class Artifact(FakeMemoryArtifactManager):
        def status(self) -> dict[str, object]:
            admission_threads.append(threading.get_ident())
            return super().status()

    artifact = Artifact(python=Path(sys.executable))
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
    assert admission_threads and event_loop_thread not in admission_threads
    assert len(processes.supervised) == 1
    assert processes.supervised[0].running


@pytest.mark.asyncio
async def test_pinned_everos_runtime_wakes_through_production_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    """MEMORY-WAKE-001: the pinned wheel satisfies production Wake and health."""

    required = os.environ.get("AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT") == "1"
    if importlib.util.find_spec("everos") is None:
        if required:
            pytest.fail("managed EverOS runtime is required for this contract")
        pytest.skip("managed EverOS runtime is not installed")
    assert importlib.metadata.version("everos") == EVEROS_VERSION

    runtime_python = Path.cwd() / "scripts" / "memory_runtime" / ".venv" / "bin" / "python"
    if not runtime_python.is_file():
        if required:
            pytest.fail("provisioned Memory runtime interpreter is missing")
        pytest.skip("provisioned Memory runtime interpreter is missing")
    monkeypatch.setenv("AVIBE_MEMORY_DEV_RUNTIME", str(runtime_python))
    with tempfile.TemporaryDirectory(prefix="avw-", dir="/tmp") as temporary:
        effective_home = Path(temporary).resolve()
        artifact = MemoryArtifactManager(
            runtime_dir=effective_home / "runtime",
            offline=True,
            provider_root=effective_home / "memory" / "everos-root",
        )
        runtime = memory_runtime_factory(
            _config(),
            artifact_manager=artifact,
            effective_home=effective_home,
        )
        try:
            result = await asyncio.wait_for(runtime.wake(), timeout=60)

            assert result == {"ok": True, "state": "running"}
            assert runtime.runtime_state() == "running"
        finally:
            await memory_runtime_factory.close(runtime)


@pytest.mark.asyncio
async def test_wake_never_routes_needs_repair_into_deletion(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """MEMORY-WAKE-002: local repair eligibility never implies deletion by Wake."""

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
    """MEMORY-WAKE-002: artifact failures degrade without deleting data."""

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


@pytest.mark.asyncio
async def test_wake_classifies_incompatible_local_root_as_repairable(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    artifact = FakeMemoryArtifactManager(
        python=None,
        status_payload={
            "installed": False,
            "status": "invalid",
            "reason": "memory_runtime_install_failed",
        },
        ensure_payload={
            "ok": False,
            "reason": "memory_local_data_unusable",
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
        "state": "needs_repair",
        "error": "memory_local_data_unusable",
    }
    assert runtime.needs_repair is True
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_wake_retries_short_operation_lease_contention(
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
    attempts = 0
    releases = 0

    class Lease:
        def __init__(self, _home: Path) -> None:
            pass

        def acquire(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise MemoryOperationBusy("busy")

        def release(self) -> None:
            nonlocal releases
            releases += 1

    monkeypatch.setattr(runtime_module, "EverOSPort", lambda *args, **kwargs: provider)
    monkeypatch.setattr(runtime_module, "MemoryOperationLease", Lease)
    monkeypatch.setattr(runtime_module, "_WAKE_LEASE_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))
    runtime = memory_runtime_factory(
        _config(),
        artifact_manager=artifact,
        process_factory=processes,
        effective_home=tmp_path,
    )

    result = await runtime.wake()

    assert result == {"ok": True, "state": "running"}
    assert attempts == 3
    assert releases == 1


@pytest.mark.asyncio
async def test_cancelled_wake_releases_a_completed_lease_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    acquire_started = threading.Event()
    finish_acquire = threading.Event()
    lease_events: list[str] = []

    class Lease:
        def __init__(self, _home: Path) -> None:
            pass

        def acquire(self) -> None:
            acquire_started.set()
            finish_acquire.wait(timeout=5)
            lease_events.append("acquire")

        def release(self) -> None:
            lease_events.append("release")

    monkeypatch.setattr(runtime_module, "MemoryOperationLease", Lease)
    runtime = memory_runtime_factory(_config(), effective_home=tmp_path)
    task = asyncio.create_task(runtime.wake())
    assert await asyncio.to_thread(acquire_started.wait, 2)

    task.cancel()
    await asyncio.sleep(0.05)
    assert task.done() is False

    finish_acquire.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease_events == ["acquire", "release"]


@pytest.mark.asyncio
async def test_disabled_wake_reaps_orphans_without_installing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    artifact = FakeMemoryArtifactManager(
        python=None,
        status_payload={"installed": False, "status": "missing"},
        ensure_payload={"ok": True},
    )
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=artifact,
        effective_home=tmp_path,
    )
    reaped = 0

    async def reap(*, fail_closed: bool = False) -> bool:
        nonlocal reaped
        assert fail_closed is False
        reaped += 1
        return True

    monkeypatch.setattr(runtime, "_reap_recorded_sidecar_if_unowned", reap)

    result = await runtime.wake()

    assert result == {
        "ok": False,
        "state": "disabled",
        "error": "memory_disabled",
    }
    assert reaped == 1
    assert artifact.ensure_calls == []


@pytest.mark.asyncio
async def test_orphan_recovery_reaps_released_sync_before_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    events: list[str] = []

    class SyncReaper:
        def __init__(self, **_kwargs) -> None:
            pass

        async def reconcile_orphan(self) -> None:
            events.append("sync")

    class SidecarReaper:
        def __init__(self, **_kwargs) -> None:
            pass

        async def reconcile_orphan(self) -> None:
            events.append("sidecar")

    monkeypatch.setattr(runtime_module, "RecordedSyncReaper", SyncReaper)
    monkeypatch.setattr(runtime_module, "RecordedSidecarReaper", SidecarReaper)
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        effective_home=tmp_path,
    )

    assert await runtime._reap_recorded_sidecar_if_unowned() is True
    assert events == ["sync", "sidecar"]


@pytest.mark.asyncio
async def test_orphan_recovery_degrades_without_no_follow_but_reset_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        effective_home=tmp_path,
    )

    def unsupported() -> int:
        raise ConfinedFilesystemError("no-follow unavailable")

    def unexpected_reaper(**_kwargs):
        pytest.fail("orphan reapers must not touch paths without no-follow support")

    monkeypatch.setattr(runtime_module, "required_no_follow_flag", unsupported)
    monkeypatch.setattr(runtime_module, "RecordedSyncReaper", unexpected_reaper)
    monkeypatch.setattr(runtime_module, "RecordedSidecarReaper", unexpected_reaper)

    assert await runtime._reap_recorded_sidecar_if_unowned() is False
    with pytest.raises(ConfinedFilesystemError, match="no-follow unavailable"):
        await runtime._reap_recorded_sidecar_if_unowned(fail_closed=True)


@pytest.mark.asyncio
async def test_cancelled_artifact_install_joins_before_releasing_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    install_started = threading.Event()
    finish_install = threading.Event()
    lease_events: list[str] = []

    class BlockingArtifact(FakeMemoryArtifactManager):
        def ensure(self, *, force: bool = False) -> dict[str, object]:
            self.ensure_calls.append(force)
            install_started.set()
            finish_install.wait(timeout=5)
            return {"ok": True, "reason": None, "download_error": None}

    class Lease:
        def __init__(self, _home: Path) -> None:
            pass

        def acquire(self) -> None:
            lease_events.append("acquire")

        def release(self) -> None:
            lease_events.append("release")

    monkeypatch.setattr(runtime_module, "MemoryOperationLease", Lease)
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=BlockingArtifact(),
        effective_home=tmp_path,
    )
    task = asyncio.create_task(runtime.install_artifact())
    assert await asyncio.to_thread(install_started.wait, 2)

    task.cancel()
    await asyncio.sleep(0.05)

    assert task.done() is False
    assert lease_events == ["acquire"]
    finish_install.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease_events == ["acquire", "release"]


@pytest.mark.asyncio
async def test_unexpected_exit_reenters_wake_and_repairs_the_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    """MEMORY-WAKE-201: an unexpected exit reuses the public Wake path."""

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
    assert await runtime.wake() == {"ok": True, "state": "running"}

    artifact.status_payload = {
        "installed": False,
        "status": "invalid",
        "reason": "memory_runtime_invalid",
    }
    await processes.supervised[0].unexpected_exit()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(processes.supervised) == 2 and processes.supervised[1].running:
            break

    assert artifact.ensure_calls == [True]
    assert len(processes.supervised) == 2
    assert processes.supervised[1].running is True
    assert runtime.runtime_state() == "running"
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_unexpected_exit_retries_when_replacement_crashes_during_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    artifact = FakeMemoryArtifactManager(python=Path(sys.executable))
    processes = FakeEverOSProcessFactory()
    provider = FakeMemoryProvider()
    monkeypatch.setattr(runtime_module, "EverOSPort", lambda *args, **kwargs: provider)
    monkeypatch.setattr(runtime_module, "_UNEXPECTED_WAKE_DELAYS_SECONDS", (0.0, 0.0, 0.0))
    runtime = memory_runtime_factory(
        _config(),
        artifact_manager=artifact,
        process_factory=processes,
        effective_home=tmp_path,
    )
    assert await runtime.wake() == {"ok": True, "state": "running"}
    original_health_snapshot = provider.health_snapshot
    crashed_replacement = False

    async def health_snapshot():
        nonlocal crashed_replacement
        current = processes.supervised[-1]
        if len(processes.supervised) == 2 and not crashed_replacement:
            crashed_replacement = True
            await current.unexpected_exit()
        if not current.running:
            raise RuntimeError("replacement exited")
        return await original_health_snapshot()

    monkeypatch.setattr(provider, "health_snapshot", health_snapshot)
    await processes.supervised[0].unexpected_exit()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(processes.supervised) == 3 and runtime.runtime_state() == "running":
            break

    assert crashed_replacement is True
    assert len(processes.supervised) == 3
    assert processes.supervised[-1].running is True
    assert runtime.runtime_state() == "running"


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
    """MEMORY-REPAIR-205: external faults stay degraded."""

    assert runtime_module._degraded_runtime_reason(error) == reason
    assert runtime_module._local_data_failure_requires_repair(error) is False


@pytest.mark.asyncio
async def test_wake_reopens_store_after_environmental_fault_is_corrected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    store_type = runtime_module.MemoryStore

    def unavailable_store(*_args, **_kwargs):
        raise PermissionError("denied")

    provider = FakeMemoryProvider(
        health_snapshot_value=ProviderHealthSnapshot(
            status="ok",
            version="test",
            capabilities={"embed": True},
            disabled_features=(),
            cascade={},
        )
    )
    monkeypatch.setattr(runtime_module, "MemoryStore", unavailable_store)
    monkeypatch.setattr(runtime_module, "EverOSPort", lambda *args, **kwargs: provider)
    runtime = memory_runtime_factory(
        _config(),
        artifact_manager=FakeMemoryArtifactManager(python=Path(sys.executable)),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    assert runtime.available is False
    assert runtime.runtime_state() == "degraded"

    monkeypatch.setattr(runtime_module, "MemoryStore", store_type)
    result = await runtime.wake()

    assert result == {"ok": True, "state": "running"}
    assert runtime.available is True
