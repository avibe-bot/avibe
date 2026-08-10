from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from core.memory.process import EverOSProcessSettings, FakeEverOSProcess, FakeEverOSProcessFactory
from core.memory.sidecar_lifecycle import MemorySidecarLifecycle


def _settings() -> EverOSProcessSettings:
    return EverOSProcessSettings(
        llm_base_url="https://llm.example.test",
        llm_model="model",
        llm_api_key="key",
        embedding_base_url="https://embedding.example.test",
        embedding_model="embedding",
        embedding_api_key="key",
    )


def _lifecycle(tmp_path: Path, factory: FakeEverOSProcessFactory, ready) -> MemorySidecarLifecycle:
    async def retain() -> str | None:
        return None

    return MemorySidecarLifecycle(
        factory,
        provider_root=tmp_path / "memory" / "everos-root",
        effective_home=tmp_path,
        socket_path=tmp_path / "memory" / ".rt" / "everos.sock",
        call_log_db_path=tmp_path / "memory" / "call-log" / "call-log.db",
        retain_call_log=retain,
        on_current_sidecar_ready=ready,
        on_recorder_health=lambda _health: None,
    )


async def test_start_assigns_owner_before_sidecar_callbacks_and_ignores_stale_reap(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    ready: list[int] = []
    lifecycle = _lifecycle(tmp_path, factory, lambda generation: ready.append(generation))

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    first = factory.supervised[0]
    first_generation = lifecycle.snapshot().generation
    await first.ready()
    await asyncio.sleep(0)
    assert ready == [first_generation]

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    second = factory.supervised[1]
    assert first.stops == 1
    stale_reap = first.on_reaped
    assert stale_reap is not None
    await stale_reap()
    assert lifecycle.snapshot().process is second
    assert lifecycle.snapshot().records_calls is True


async def test_stop_failure_retains_current_supervisor(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory(template=lambda: FakeEverOSProcess(stop_failure=RuntimeError("stuck")))
    lifecycle = _lifecycle(tmp_path, factory, lambda _generation: None)
    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    process = lifecycle.snapshot().process

    with pytest.raises(RuntimeError, match="stuck"):
        await lifecycle.stop()
    assert lifecycle.snapshot().process is process


async def test_processing_health_is_single_flight_without_waiting(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingProcess(FakeEverOSProcess):
        async def processing_healthy(self) -> bool:
            entered.set()
            await release.wait()
            return True

    factory = FakeEverOSProcessFactory(template=BlockingProcess)
    lifecycle = _lifecycle(tmp_path, factory, lambda _generation: None)
    assert await lifecycle.start(Path(sys.executable), _settings()) is True

    first = asyncio.create_task(lifecycle.processing_healthy())
    await entered.wait()
    assert await asyncio.wait_for(lifecycle.processing_healthy(), timeout=1) is False
    release.set()
    assert await first is True


async def test_close_never_restarts_host_retention_after_stopping_sidecar(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    call_log = tmp_path / "memory" / "call-log" / "call-log.db"
    call_log.parent.mkdir(parents=True)
    call_log.touch()
    lifecycle = _lifecycle(tmp_path, factory, lambda _generation: None)

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    await lifecycle.close()

    assert lifecycle.snapshot().process is None
    assert lifecycle.retention_task is None


async def test_corrupt_host_log_stays_blocked_across_sidecar_reaps(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    call_log = tmp_path / "memory" / "call-log" / "call-log.db"
    call_log.parent.mkdir(parents=True)
    call_log.touch()
    attempts = 0

    async def retain() -> str | None:
        nonlocal attempts
        attempts += 1
        return "call_log_corrupt"

    lifecycle = MemorySidecarLifecycle(
        factory,
        provider_root=tmp_path / "memory" / "everos-root",
        effective_home=tmp_path,
        socket_path=tmp_path / "memory" / ".rt" / "everos.sock",
        call_log_db_path=call_log,
        retain_call_log=retain,
        on_current_sidecar_ready=lambda _generation: None,
        on_recorder_health=lambda _health: None,
    )
    lifecycle.handoff_to_host_retention()
    task = lifecycle.retention_task
    assert task is not None
    await task
    assert attempts == 1

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    await lifecycle.stop()
    await asyncio.sleep(0)
    assert attempts == 1

    lifecycle.reset_host_retention_after_clear()
    lifecycle.handoff_to_host_retention()
    resumed = lifecycle.retention_task
    assert resumed is not None
    await resumed
    assert attempts == 2
