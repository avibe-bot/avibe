from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import core.memory.runtime as memory_runtime
from config.v2_config import (
    AgentsConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)
from core.memory.artifact import (
    FakeMemoryArtifactManager,
    MemoryArtifactCandidate,
    MemoryProviderRootState,
)
from core.memory.everos import FakeMemoryProvider, ProviderHealthSnapshot
from core.memory.operation_lock import MemoryOperationBusy, MemoryOperationLease
from core.memory.process import FakeEverOSProcess, FakeEverOSProcessFactory
from core.memory.sync_process import SyncProcessResult


def _config(*, enabled: bool = True) -> MemoryConfig:
    return MemoryConfig(
        enabled=enabled,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig(
                "https://embed.test/v1",
                "embed",
                "embed-key",
            ),
        ),
    )


def _cascade_health(*, healthy: bool) -> dict[str, object]:
    return {
        "healthy": healthy,
        "reasons": [] if healthy else ["drain_failures"],
        "pending": 0,
        "failed_permanent": 0 if healthy else 1,
        "failed_retryable": 0,
        "drain_consecutive_failures": 0,
        "unrecoverable_total": 0 if healthy else 1,
        "optimize_failure_streak": 0,
        "prune_stale_seconds": 0.0,
    }


def _runtime(
    memory_runtime_factory,
    tmp_path: Path,
    *,
    sync_available: bool = True,
    healthy: bool = True,
):
    artifact = FakeMemoryArtifactManager(
        python=Path(sys.executable),
        sync_available=sync_available,
    )
    runtime = memory_runtime_factory(
        _config(),
        artifact_manager=artifact,
        effective_home=tmp_path,
    )
    sidecar = FakeEverOSProcess()
    runtime._process = sidecar
    provider = FakeMemoryProvider(
        health_snapshot_value=ProviderHealthSnapshot(
            status="ok",
            version="1.2.3",
            capabilities={"embed": True},
            disabled_features=(),
            cascade=_cascade_health(healthy=healthy),
        )
    )
    runtime._provider = provider
    runtime.module.replace_provider(provider)
    return runtime, sidecar


class _FirstRebuildInstallArtifact(FakeMemoryArtifactManager):
    """Publish a fake first-install pointer through the real activation bridge."""

    def __init__(self) -> None:
        super().__init__(
            python=None,
            root_format=None,
            fingerprint=None,
            compatible_formats=frozenset(),
            status_payload={
                "installed": False,
                "status": "missing",
                "reason": "memory_runtime_missing",
            },
        )
        self.commits = 0
        self.rollbacks = 0

    def ensure(self, *, force: bool = False) -> dict:
        self.ensure_calls.append(force)
        assert force is True
        assert self.activation_coordinator is not None

        def commit() -> None:
            self.commits += 1
            self.python = Path(sys.executable)
            self.root_format = "everos-1.2.3"
            self.fingerprint = "first-rebuild-install"
            self.compatible_formats = frozenset({"everos-1.2.3"})
            self.status_payload = {
                "installed": True,
                "status": "ready",
                "reason": None,
            }

        def rollback() -> None:
            self.rollbacks += 1

        self.activation_coordinator(
            MemoryArtifactCandidate(
                provider_root_format="everos-1.2.3",
                compatible_provider_root_formats=frozenset({"everos-1.2.3"}),
                artifact_fingerprint="first-rebuild-install",
            ),
            MemoryProviderRootState(exists=False),
            commit,
            rollback,
        )
        return dict(self.ensure_payload)


@pytest.mark.parametrize(
    ("healthy", "expected_result"),
    [(True, "completed"), (False, "completed_with_warnings")],
)
async def test_repair_runs_sync_beside_live_sidecar_and_projects_health(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
    healthy: bool,
    expected_result: str,
) -> None:
    """Scenarios: MEMORY-REPAIR-001, MEMORY-REPAIR-002, MEMORY-REPAIR-006."""

    runtime, sidecar = _runtime(
        memory_runtime_factory,
        tmp_path,
        healthy=healthy,
    )
    children = []

    class Sync:
        def __init__(self, python, **kwargs) -> None:
            children.append((python, kwargs))

        async def run(self) -> SyncProcessResult:
            assert sidecar.running
            assert runtime.module._writer._intake_paused is False
            return SyncProcessResult.COMPLETED

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)

    assert await runtime.repair() == {
        "ok": True,
        "result": expected_result,
        "health": _cascade_health(healthy=healthy),
    }
    assert len(children) == 1
    assert children[0][0] == Path(sys.executable)
    assert children[0][1]["provider_root"] == tmp_path / "memory" / "everos-root"
    assert sidecar.running is True
    assert sidecar.stops == 0


async def test_repair_is_fenced_by_durable_factory_reset_intent(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)
    runtime._config = replace(runtime._config, recovery_intent="factory_reset")
    runtime._restart_config = replace(runtime._restart_config, recovery_intent="factory_reset")

    assert await runtime.repair() == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }


@pytest.mark.parametrize("config_attr", ["_config", "_restart_config"])
async def test_repair_is_fenced_by_durable_rebuild_intent(
    tmp_path: Path,
    memory_runtime_factory,
    config_attr: str,
) -> None:
    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)
    setattr(
        runtime,
        config_attr,
        replace(getattr(runtime, config_attr), recovery_intent="rebuild"),
    )

    assert await runtime.repair() == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }
    assert runtime._repair_task is None


async def test_pending_rebuild_install_publishes_artifact_for_explicit_retry(
    tmp_path: Path,
    memory_runtime_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-REBUILD-202."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    pending = replace(_config(), recovery_intent="rebuild")
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=pending,
    ).save()
    artifact = _FirstRebuildInstallArtifact()
    process_factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        pending,
        artifact_manager=artifact,
        process_factory=process_factory,
        effective_home=tmp_path,
    )

    assert runtime.module._writer._intake_paused is True
    assert await runtime.install_artifact() == {
        "ok": True,
        "reason": None,
        "download_error": None,
    }
    assert artifact.commits == 1
    assert artifact.rollbacks == 0
    assert artifact.resolve_python() == Path(sys.executable)
    assert process_factory.created == []
    assert runtime.module._writer._intake_paused is True
    assert V2Config.load().memory.recovery_intent == "rebuild"

    async def healthy_preflight(_config: MemoryConfig | None = None) -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(runtime, "preflight", healthy_preflight)
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)
    monkeypatch.setattr(
        runtime,
        "_probe_processing",
        lambda *_args: asyncio.sleep(0, result=True),
    )
    assert await runtime.rebuild() == {
        "ok": True,
        "result": "completed_empty",
        "state": "ready",
    }
    assert V2Config.load().memory.recovery_intent is None
    assert runtime._restart_config.recovery_intent is None
    assert len(process_factory.supervised) == 1
    assert runtime.module._writer._intake_paused is False


async def test_repair_rejects_artifact_without_sync_capability(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """Scenario: MEMORY-REPAIR-005."""

    runtime, sidecar = _runtime(
        memory_runtime_factory,
        tmp_path,
        sync_available=False,
    )

    class UnexpectedSync:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("unsupported artifact must not launch sync")

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", UnexpectedSync)

    assert await runtime.repair() == {
        "ok": False,
        "error": "memory_runtime_unsupported",
        "result": "failed",
    }
    assert sidecar.running is True
    assert sidecar.stops == 0


async def test_repair_requires_enabled_live_sidecar_and_final_cascade_health(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime, sidecar = _runtime(memory_runtime_factory, tmp_path)
    runs = 0

    class Sync:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> SyncProcessResult:
            nonlocal runs
            runs += 1
            return SyncProcessResult.COMPLETED

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)

    runtime._config = _config(enabled=False)
    assert await runtime.repair() == {
        "ok": False,
        "error": "memory_disabled",
        "result": "failed",
    }
    runtime._config = _config()
    sidecar._running = False
    assert await runtime.repair() == {
        "ok": False,
        "error": "memory_sidecar_unavailable",
        "result": "failed",
    }
    sidecar._running = True
    runtime._provider.health_snapshot_value = ProviderHealthSnapshot(
        status="ok",
        version="1.2.3",
        capabilities={"embed": True},
        disabled_features=(),
        cascade=None,
    )
    assert await runtime.repair() == {
        "ok": False,
        "error": "memory_repair_failed",
        "result": "failed",
    }
    assert runs == 1


@pytest.mark.parametrize(
    ("child_result", "expected"),
    [
        (
            SyncProcessResult.ALREADY_RUNNING,
            {
                "ok": False,
                "error": "memory_operation_in_progress",
                "result": "failed",
            },
        ),
        (
            SyncProcessResult.INTERRUPTED,
            {
                "ok": False,
                "error": "memory_repair_failed",
                "result": "interrupted",
            },
        ),
        (
            SyncProcessResult.TIMED_OUT,
            {
                "ok": False,
                "error": "memory_repair_failed",
                "result": "timed_out",
            },
        ),
        (
            SyncProcessResult.FAILED,
            {
                "ok": False,
                "error": "memory_repair_failed",
                "result": "failed",
            },
        ),
    ],
)
async def test_repair_maps_closed_child_results(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
    child_result: SyncProcessResult,
    expected: dict[str, object],
) -> None:
    """Scenario: MEMORY-REPAIR-004."""

    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)

    class Sync:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> SyncProcessResult:
            return child_result

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)
    assert await runtime.repair() == expected


async def test_repair_is_retained_joined_and_holds_lease_after_caller_cancel(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """Scenario: MEMORY-REPAIR-003."""

    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)
    started = asyncio.Event()
    finish = asyncio.Event()
    runs = 0

    class Sync:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> SyncProcessResult:
            nonlocal runs
            runs += 1
            started.set()
            await finish.wait()
            return SyncProcessResult.COMPLETED

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)

    detached = asyncio.create_task(runtime.repair())
    await started.wait()
    joined = asyncio.create_task(runtime.repair())
    detached.cancel()
    with pytest.raises(asyncio.CancelledError):
        await detached
    competing = MemoryOperationLease(tmp_path)
    with pytest.raises(MemoryOperationBusy):
        competing.acquire()

    finish.set()
    assert (await joined)["result"] == "completed"
    assert runs == 1
    competing.acquire()
    competing.release()


async def test_repair_gates_other_runtime_mutations(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)
    started = asyncio.Event()
    finish = asyncio.Event()

    class Sync:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> SyncProcessResult:
            started.set()
            await finish.wait()
            return SyncProcessResult.COMPLETED

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)
    repairing = asyncio.create_task(runtime.repair())
    await started.wait()

    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }
    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }
    assert await runtime.reconcile(_config()) == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }
    assert await runtime.install_artifact() == {
        "ok": False,
        "reason": "memory_operation_in_progress",
        "download_error": None,
    }
    clear_called = False

    async def clear_operation():
        nonlocal clear_called
        clear_called = True
        raise AssertionError("conflicting clear must not run")

    assert await runtime._run_clear_with_operation_lease(clear_operation) == {
        "status": "failed",
        "error": "memory_operation_in_progress",
    }
    assert clear_called is False
    finish.set()
    assert (await repairing)["ok"] is True


async def test_repair_lease_gates_second_controller_mutations(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)
    other = memory_runtime_factory(
        _config(),
        artifact_manager=FakeMemoryArtifactManager(python=Path(sys.executable)),
        effective_home=tmp_path,
    )
    started = asyncio.Event()
    finish = asyncio.Event()

    class Sync:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> SyncProcessResult:
            started.set()
            await finish.wait()
            return SyncProcessResult.COMPLETED

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)
    repairing = asyncio.create_task(runtime.repair())
    await started.wait()

    assert await other.restart() == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }
    assert await other.install_artifact() == {
        "ok": False,
        "reason": "memory_operation_in_progress",
        "download_error": None,
    }

    finish.set()
    assert (await repairing)["ok"] is True


async def test_close_cancels_and_joins_repair_cleanup(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class Sync:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> SyncProcessResult:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaned.set()
                return SyncProcessResult.INTERRUPTED

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)
    repairing = asyncio.create_task(runtime.repair())
    await started.wait()
    await memory_runtime_factory.close(runtime)

    assert cleaned.is_set()
    assert await repairing == {
        "ok": False,
        "error": "memory_repair_failed",
        "result": "interrupted",
    }
    competing = MemoryOperationLease(tmp_path)
    competing.acquire()
    competing.release()


async def test_sync_orphan_reconciliation_does_not_stop_sidecar(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime, sidecar = _runtime(memory_runtime_factory, tmp_path)
    events: list[str] = []

    class Sync:
        def __init__(self, python, **kwargs) -> None:
            assert python is None
            assert kwargs["provider_root"] == tmp_path / "memory" / "everos-root"

        async def reconcile_orphan(self) -> None:
            assert sidecar.running
            events.append("sync")

    monkeypatch.setattr(memory_runtime, "EverOSSyncProcess", Sync)

    assert await runtime._reap_recorded_sync_if_unowned() is True
    assert events == ["sync"]
    assert sidecar.stops == 0


async def test_boot_runs_sync_orphan_reconciliation_before_sidecar_recovery(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime, _sidecar = _runtime(memory_runtime_factory, tmp_path)
    events: list[str] = []

    async def sync_reap() -> bool:
        events.append("sync")
        return True

    async def sidecar_reap() -> bool:
        events.append("sidecar")
        return False

    monkeypatch.setattr(runtime, "_reap_recorded_sync_if_unowned", sync_reap)
    monkeypatch.setattr(runtime, "_reap_recorded_sidecar_if_unowned", sidecar_reap)
    monkeypatch.setattr(
        "core.memory.runtime.V2Config.load",
        lambda: type("C", (), {"memory": _config()})(),
    )

    await runtime.reconcile(_config())
    assert events[:2] == ["sync", "sidecar"]
