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


def _lifecycle(
    tmp_path: Path,
    factory: FakeEverOSProcessFactory,
    ready,
    recorder_health=None,
    on_recorder_health=None,
) -> MemorySidecarLifecycle:
    async def retain() -> str | None:
        return None

    async def read_recorder_health() -> dict[str, str | None]:
        return {"state": "active", "reason": None}

    return MemorySidecarLifecycle(
        factory,
        provider_root=tmp_path / "memory" / "everos-root",
        effective_home=tmp_path,
        socket_path=tmp_path / "memory" / ".rt" / "everos.sock",
        call_log_db_path=tmp_path / "memory" / "call-log" / "call-log.db",
        retain_call_log=retain,
        on_current_sidecar_ready=ready,
        on_recorder_health=on_recorder_health or (lambda _health: None),
        read_recorder_health=recorder_health or read_recorder_health,
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


async def test_start_handoffs_disabled_recorder_from_child_health_without_file_inference(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    seen: list[dict[str, str | None]] = []

    async def disabled() -> dict[str, str | None]:
        return {"state": "disabled", "reason": "writer_failures"}

    lifecycle = _lifecycle(
        tmp_path,
        factory,
        lambda _generation: None,
        disabled,
        seen.append,
    )

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    assert seen == [{"state": "disabled", "reason": "writer_failures"}]
    assert lifecycle.snapshot().records_calls is False


async def test_start_keeps_recorder_ownership_when_child_health_is_malformed(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    seen: list[dict[str, str | None]] = []

    async def malformed() -> dict[str, str | None]:
        return {"state": "disabled", "reason": "unexpected"}

    lifecycle = _lifecycle(
        tmp_path,
        factory,
        lambda _generation: None,
        malformed,
        seen.append,
    )

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    assert seen == [{"state": "degraded", "reason": "writer_failures"}]
    assert lifecycle.snapshot().records_calls is True


async def test_supervised_restart_readmits_recorder_health_before_ready(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    health = iter(
        (
            {"state": "active", "reason": None},
            {"state": "disabled", "reason": "writer_failures"},
        )
    )
    seen: list[dict[str, str | None]] = []
    ready_health: list[dict[str, str | None]] = []
    ready = asyncio.Event()

    async def recorder_health() -> dict[str, str | None]:
        return next(health)

    def observe_recorder_health(value: dict[str, str | None]) -> None:
        seen.append(value)

    def observe_ready(_generation: int) -> None:
        ready_health.append(seen[-1])
        ready.set()

    lifecycle = _lifecycle(
        tmp_path,
        factory,
        observe_ready,
        recorder_health,
        observe_recorder_health,
    )

    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    supervised = factory.supervised[0]
    await supervised.start()
    await asyncio.wait_for(ready.wait(), timeout=1.0)

    assert seen == [
        {"state": "active", "reason": None},
        {"state": "disabled", "reason": "writer_failures"},
    ]
    assert ready_health == [{"state": "disabled", "reason": "writer_failures"}]
    assert lifecycle.snapshot().records_calls is False


async def test_recovery_during_initial_admission_discards_stale_health_and_queues_ready(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    initial_health_entered = asyncio.Event()
    release_initial_health = asyncio.Event()
    recovery_ready = asyncio.Event()
    seen: list[dict[str, str | None]] = []
    ready_health: list[dict[str, str | None]] = []
    health_reads = 0

    async def recorder_health() -> dict[str, str | None]:
        nonlocal health_reads
        health_reads += 1
        if health_reads == 1:
            initial_health_entered.set()
            await release_initial_health.wait()
            return {"state": "degraded", "reason": "writer_failures"}
        return {"state": "disabled", "reason": "writer_failures"}

    def observe_ready(_generation: int) -> None:
        ready_health.append(seen[-1])
        recovery_ready.set()

    lifecycle = _lifecycle(
        tmp_path,
        factory,
        observe_ready,
        recorder_health,
        seen.append,
    )

    initial_start = asyncio.create_task(
        lifecycle.start(Path(sys.executable), _settings())
    )
    await asyncio.wait_for(initial_health_entered.wait(), timeout=1.0)
    supervised = factory.supervised[0]

    assert await supervised.start() is True
    release_initial_health.set()
    assert await initial_start is False
    await asyncio.wait_for(recovery_ready.wait(), timeout=1.0)

    assert health_reads == 2
    assert seen == [{"state": "disabled", "reason": "writer_failures"}]
    assert ready_health == [{"state": "disabled", "reason": "writer_failures"}]
    assert lifecycle.snapshot().records_calls is False


async def test_cancelled_initial_admission_schedules_queued_ready(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    initial_health_entered = asyncio.Event()
    block_initial_health = asyncio.Event()
    ready = asyncio.Event()
    seen: list[dict[str, str | None]] = []
    health_reads = 0

    async def recorder_health() -> dict[str, str | None]:
        nonlocal health_reads
        health_reads += 1
        if health_reads == 1:
            initial_health_entered.set()
            await block_initial_health.wait()
        return {"state": "disabled", "reason": "writer_failures"}

    lifecycle = _lifecycle(
        tmp_path,
        factory,
        lambda _generation: ready.set(),
        recorder_health,
        seen.append,
    )
    start = asyncio.create_task(lifecycle.start(Path(sys.executable), _settings()))
    try:
        await asyncio.wait_for(initial_health_entered.wait(), timeout=1.0)
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start

        await asyncio.wait_for(ready.wait(), timeout=1.0)
        assert health_reads == 2
        assert seen == [{"state": "disabled", "reason": "writer_failures"}]
        assert lifecycle.snapshot().records_calls is False
    finally:
        block_initial_health.set()
        if not start.done():
            start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        await lifecycle.close()


async def test_runtime_disabled_recorder_hands_call_log_to_host_retention(
    tmp_path: Path,
) -> None:
    factory = FakeEverOSProcessFactory()
    call_log = tmp_path / "memory" / "call-log" / "call-log.db"
    call_log.parent.mkdir(parents=True)
    call_log.touch()
    retained = asyncio.Event()

    async def retain() -> str | None:
        retained.set()
        return None

    lifecycle = MemorySidecarLifecycle(
        factory,
        provider_root=tmp_path / "memory" / "everos-root",
        effective_home=tmp_path,
        socket_path=tmp_path / "memory" / ".rt" / "everos.sock",
        call_log_db_path=call_log,
        retain_call_log=retain,
        on_current_sidecar_ready=lambda _generation: None,
        on_recorder_health=lambda _health: None,
        read_recorder_health=lambda: asyncio.sleep(
            0, result={"state": "active", "reason": None}
        ),
    )
    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    assert lifecycle.snapshot().records_calls is True

    lifecycle.observe_recorder_health(
        {"state": "disabled", "reason": "writer_failures"}
    )
    await asyncio.wait_for(retained.wait(), timeout=1.0)

    assert lifecycle.snapshot().records_calls is False
    assert lifecycle.snapshot().running is True
    assert lifecycle.retention_task is not None
    await lifecycle.close()


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
        read_recorder_health=lambda: asyncio.sleep(0, result={"state": "active", "reason": None}),
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


async def test_recorder_corruption_blocks_host_retention_after_a_reap(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    call_log = tmp_path / "memory" / "call-log" / "call-log.db"
    call_log.parent.mkdir(parents=True)
    call_log.touch()
    attempts = 0

    async def retain() -> str | None:
        nonlocal attempts
        attempts += 1
        return None

    lifecycle = MemorySidecarLifecycle(
        factory,
        provider_root=tmp_path / "memory" / "everos-root",
        effective_home=tmp_path,
        socket_path=tmp_path / "memory" / ".rt" / "everos.sock",
        call_log_db_path=call_log,
        retain_call_log=retain,
        on_current_sidecar_ready=lambda _generation: None,
        on_recorder_health=lambda _health: None,
        read_recorder_health=lambda: asyncio.sleep(0, result={"state": "active", "reason": None}),
    )
    lifecycle.observe_recorder_health({"state": "degraded", "reason": "call_log_corrupt"})
    assert await lifecycle.start(Path(sys.executable), _settings()) is True
    await lifecycle.stop()
    await asyncio.sleep(0)
    assert attempts == 0
