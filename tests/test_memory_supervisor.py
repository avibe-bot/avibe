from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
import sys

from core.memory.process import (
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
)
from core.memory.supervisor import EverOSSupervisor


def _settings() -> EverOSProcessSettings:
    return EverOSProcessSettings(
        llm_base_url="https://llm.example.test",
        llm_model="model",
        llm_api_key="key",
        embedding_base_url="https://embedding.example.test",
        embedding_model="embedding",
        embedding_api_key="key",
    )


def _supervisor(
    tmp_path: Path,
    factory: FakeEverOSProcessFactory,
    *,
    ready=lambda: None,
    unavailable=lambda: None,
    restart_delays: tuple[float, ...] = (0.0, 0.0),
) -> EverOSSupervisor:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    return EverOSSupervisor(
        provider_root=provider_root,
        effective_home=home,
        socket_path=home / "memory" / ".rt" / "everos.sock",
        on_ready=ready,
        on_unavailable=unavailable,
        process_factory=factory,
        restart_delays=restart_delays,
    )


async def _settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_wake_keeps_one_owner_and_fences_stale_callbacks(tmp_path: Path) -> None:
    ready = 0
    unavailable = 0

    def observe_ready() -> None:
        nonlocal ready
        ready += 1

    def observe_unavailable() -> None:
        nonlocal unavailable
        unavailable += 1

    factory = FakeEverOSProcessFactory()
    supervisor = _supervisor(
        tmp_path,
        factory,
        ready=observe_ready,
        unavailable=observe_unavailable,
    )

    assert await supervisor.wake(Path(sys.executable), _settings()) is True
    await _settle()
    first = factory.supervised[0]
    assert ready == 1

    assert await supervisor.wake(Path(sys.executable), _settings()) is True
    await _settle()
    second = factory.supervised[1]
    assert first.stops == 1
    assert ready == 2

    await first.ready()
    await first.unexpected_exit()
    await _settle()

    assert len(factory.supervised) == 2
    assert supervisor.status.running is True
    assert second.running is True
    assert ready == 2
    assert unavailable == 0
    await supervisor.close()


async def test_crash_recovery_is_bounded(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    supervisor = _supervisor(tmp_path, factory, restart_delays=(0.0, 0.0))
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await factory.supervised[0].unexpected_exit()
    for _ in range(20):
        await _settle()
        if len(factory.supervised) == 2:
            break
    await factory.supervised[1].unexpected_exit()
    for _ in range(20):
        await _settle()
        if len(factory.supervised) == 3:
            break
    await factory.supervised[2].unexpected_exit()
    await _settle()

    assert len(factory.supervised) == 3
    assert supervisor.status.state == "degraded"
    assert supervisor.status.retains_configuration is False
    await supervisor.close()


async def test_close_cancels_pending_restart(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    supervisor = _supervisor(tmp_path, factory, restart_delays=(1.0,))
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await factory.supervised[0].unexpected_exit()
    await _settle()
    assert supervisor.status.state == "starting"

    await supervisor.close()
    await asyncio.sleep(0.01)

    assert len(factory.supervised) == 1
    assert supervisor.status.state == "closed"


async def test_unreaped_child_degrades_without_launching_a_replacement(
    tmp_path: Path,
) -> None:
    unavailable = 0

    def observe_unavailable() -> None:
        nonlocal unavailable
        unavailable += 1

    class RetainedProcess(FakeEverOSProcess):
        async def unexpected_exit(self) -> None:
            self._running = False
            self._process_tree_retained = True
            if self.on_unexpected_exit is not None:
                result = self.on_unexpected_exit()
                if asyncio.iscoroutine(result):
                    await result

    factory = FakeEverOSProcessFactory(template=RetainedProcess)
    supervisor = _supervisor(
        tmp_path,
        factory,
        unavailable=observe_unavailable,
    )
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await factory.supervised[0].unexpected_exit()
    await _settle()

    assert unavailable == 1
    assert len(factory.supervised) == 1
    assert supervisor.status.state == "degraded"
    assert supervisor.status.retains_configuration is True
    await supervisor.close()


async def test_failed_launch_can_be_replaced_without_dual_ownership(
    tmp_path: Path,
) -> None:
    first = True

    def process() -> FakeEverOSProcess:
        nonlocal first
        failure = RuntimeError("failed") if first else None
        first = False
        return FakeEverOSProcess(start_failure=failure)

    factory = FakeEverOSProcessFactory(template=process)
    supervisor = _supervisor(tmp_path, factory, restart_delays=())

    assert await supervisor.wake(Path(sys.executable), _settings()) is False
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    assert len(factory.supervised) == 2
    assert factory.supervised[0].running is False
    assert factory.supervised[1].running is True
    await supervisor.close()


async def test_processing_probe_is_single_flight_without_waiting(
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
    supervisor = _supervisor(tmp_path, factory)
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    first = asyncio.create_task(supervisor.processing_healthy())
    await entered.wait()
    assert await asyncio.wait_for(supervisor.processing_healthy(), timeout=1) is False
    release.set()
    assert await first is True
    await supervisor.close()


async def test_failed_start_result_enters_bounded_restart(tmp_path: Path) -> None:
    results = deque([False, True])
    factory = FakeEverOSProcessFactory(
        template=lambda: FakeEverOSProcess(start_results=deque([results.popleft()]))
    )
    supervisor = _supervisor(tmp_path, factory, restart_delays=(0.0,))

    assert await supervisor.wake(Path(sys.executable), _settings()) is False
    for _ in range(20):
        await _settle()
        if supervisor.status.running:
            break

    assert len(factory.supervised) == 2
    assert supervisor.status.running is True
    await supervisor.close()
