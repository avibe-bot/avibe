from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from core.memory.process import (
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
)
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


def _lifecycle(
    tmp_path: Path,
    factory: FakeEverOSProcessFactory,
    ready,
) -> MemorySidecarLifecycle:
    return MemorySidecarLifecycle(
        factory,
        provider_root=tmp_path / "memory" / "everos-root",
        effective_home=tmp_path,
        socket_path=tmp_path / "memory" / ".rt" / "everos.sock",
        on_current_sidecar_ready=ready,
    )


async def test_start_assigns_owner_before_callbacks_and_ignores_stale_ready(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    ready: list[int] = []
    lifecycle = _lifecycle(tmp_path, factory, ready.append)

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    first = factory.supervised[0]
    first_generation = lifecycle.snapshot().generation
    await first.ready()
    await asyncio.sleep(0)
    assert ready == [first_generation]

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    second = factory.supervised[1]
    second_generation = lifecycle.snapshot().generation
    assert first.stops == 1

    await first.ready()
    await asyncio.sleep(0)
    assert ready == [first_generation]

    await second.ready()
    await asyncio.sleep(0)
    assert ready == [first_generation, second_generation]


async def test_supervised_restart_emits_current_generation_ready(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    ready = asyncio.Event()
    generations: list[int] = []

    def observe_ready(generation: int) -> None:
        generations.append(generation)
        ready.set()

    lifecycle = _lifecycle(tmp_path, factory, observe_ready)
    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    generation = lifecycle.snapshot().generation

    await factory.supervised[0].start()
    await asyncio.wait_for(ready.wait(), timeout=1.0)

    assert generations == [generation]
    assert lifecycle.snapshot().running is True


async def test_close_rejects_late_ready(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    ready = asyncio.Event()
    lifecycle = _lifecycle(tmp_path, factory, lambda _generation: ready.set())
    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    supervised = factory.supervised[0]

    lifecycle.close_ready_admission()
    await supervised.ready()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ready.wait(), timeout=0.05)
    await lifecycle.close()


async def test_failed_start_leaves_lifecycle_reusable(tmp_path: Path) -> None:
    first = True

    def process() -> FakeEverOSProcess:
        nonlocal first
        failure = RuntimeError("failed") if first else None
        first = False
        return FakeEverOSProcess(start_failure=failure)

    factory = FakeEverOSProcessFactory(template=process)
    lifecycle = _lifecycle(tmp_path, factory, lambda _generation: None)

    with pytest.raises(RuntimeError, match="failed"):
        await lifecycle.start(Path(sys.executable), _settings())

    assert await lifecycle.start(Path(sys.executable), _settings()) is True


async def test_stop_failure_retains_current_supervisor(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory(
        template=lambda: FakeEverOSProcess(stop_failure=RuntimeError("stuck"))
    )
    lifecycle = _lifecycle(tmp_path, factory, lambda _generation: None)
    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    process = lifecycle.snapshot().process

    with pytest.raises(RuntimeError, match="stuck"):
        await lifecycle.stop()
    assert lifecycle.snapshot().process is process


async def test_processing_health_is_single_flight_without_waiting(
    tmp_path: Path,
) -> None:
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
