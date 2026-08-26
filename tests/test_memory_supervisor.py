from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
import sys

from avibe_memory.process import (
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
)
from avibe_memory.supervisor import EverOSSupervisor


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
    restart_window_seconds: float = 30.0,
    recover_in_child_task: bool = False,
) -> EverOSSupervisor:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    supervisor: EverOSSupervisor | None = None

    async def recover() -> bool:
        assert supervisor is not None
        recovery = supervisor.wake(
            Path(sys.executable), _settings()
        )
        if recover_in_child_task:
            return await asyncio.create_task(
                recovery,
                name="memory-artifact-activation-test",
            )
        return await recovery

    supervisor = EverOSSupervisor(
        provider_root=provider_root,
        effective_home=home,
        socket_path=home / "memory" / ".rt" / "everos.sock",
        on_ready=ready,
        on_unavailable=unavailable,
        on_recover=recover,
        process_factory=factory,
        restart_delays=restart_delays,
        restart_window_seconds=restart_window_seconds,
    )
    return supervisor


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
    assert ready == 0

    assert await supervisor.wake(Path(sys.executable), _settings()) is True
    await _settle()
    second = factory.supervised[1]
    assert first.stops == 1
    assert ready == 0

    await second.ready()
    await _settle()
    assert ready == 1

    await first.ready()
    await first.unexpected_exit()
    await _settle()

    assert len(factory.supervised) == 2
    assert supervisor.status.running is True
    assert second.running is True
    assert ready == 1
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


async def test_slow_recovery_attempts_keep_one_bounded_budget(tmp_path: Path) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    recovery_calls = 0

    async def recover() -> bool:
        nonlocal recovery_calls
        recovery_calls += 1
        await asyncio.sleep(0.02)
        return False

    factory = FakeEverOSProcessFactory()
    supervisor = EverOSSupervisor(
        provider_root=provider_root,
        effective_home=home,
        socket_path=home / "memory" / ".rt" / "everos.sock",
        on_ready=lambda: None,
        on_unavailable=lambda: None,
        on_recover=recover,
        process_factory=factory,
        restart_delays=(0.0, 0.0),
        restart_window_seconds=0.005,
    )
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await factory.supervised[0].unexpected_exit()
    for _ in range(100):
        await asyncio.sleep(0.005)
        if recovery_calls == 2 and supervisor._restart_task is None:
            break
    await asyncio.sleep(0.05)

    assert recovery_calls == 2
    assert supervisor.status.state == "degraded"
    await supervisor.close()


async def test_stable_recovery_starts_a_new_crash_budget(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    supervisor = _supervisor(
        tmp_path,
        factory,
        restart_delays=(0.0,),
        restart_window_seconds=0.01,
    )
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await factory.supervised[0].unexpected_exit()
    for _ in range(20):
        await _settle()
        if len(factory.supervised) == 2 and supervisor._restart_task is None:
            break
    await asyncio.sleep(0.02)
    await factory.supervised[1].unexpected_exit()
    for _ in range(20):
        await _settle()
        if len(factory.supervised) == 3:
            break

    assert len(factory.supervised) == 3
    assert factory.supervised[2].running is True
    await supervisor.close()


async def test_recovery_replaces_a_running_child_that_failed_admission(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    factory = FakeEverOSProcessFactory()
    supervisor: EverOSSupervisor | None = None
    recovery_calls = 0

    async def recover() -> bool:
        nonlocal recovery_calls
        recovery_calls += 1
        assert supervisor is not None
        started = await supervisor.wake(Path(sys.executable), _settings())
        return bool(started and recovery_calls == 2)

    supervisor = EverOSSupervisor(
        provider_root=provider_root,
        effective_home=home,
        socket_path=home / "memory" / ".rt" / "everos.sock",
        on_ready=lambda: None,
        on_unavailable=lambda: None,
        on_recover=recover,
        process_factory=factory,
        restart_delays=(0.0, 0.0),
    )
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await factory.supervised[0].unexpected_exit()
    for _ in range(100):
        await _settle()
        if recovery_calls == 2 and supervisor._restart_task is None:
            break

    assert recovery_calls == 2
    assert len(factory.supervised) == 3
    assert factory.supervised[1].stops == 1
    assert factory.supervised[2].running is True
    assert supervisor.status.running is True
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


async def test_close_interrupts_an_in_flight_launch_before_waiting_for_the_lock(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProcess(FakeEverOSProcess):
        async def start(self) -> bool:
            self.starts += 1
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    factory = FakeEverOSProcessFactory(template=BlockingProcess)
    supervisor = _supervisor(tmp_path, factory)
    wake = asyncio.create_task(supervisor.wake(Path(sys.executable), _settings()))
    await entered.wait()

    await asyncio.wait_for(supervisor.close(), timeout=1)

    assert cancelled.is_set()
    assert await wake is False
    assert supervisor.status.state == "closed"


async def test_recovery_authority_reaches_an_artifact_activation_task(
    tmp_path: Path,
) -> None:
    start_results = deque((True, False, True))
    factory = FakeEverOSProcessFactory(
        template=lambda: FakeEverOSProcess(
            start_results=deque((start_results.popleft(),))
        )
    )
    supervisor = _supervisor(
        tmp_path,
        factory,
        restart_delays=(0.0, 0.0),
        recover_in_child_task=True,
    )
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await factory.supervised[0].unexpected_exit()
    for _ in range(20):
        await _settle()
        if (
            len(factory.supervised) == 3
            and factory.supervised[2].running
            and supervisor._restart_task is None
        ):
            break

    assert len(factory.supervised) == 3
    assert factory.supervised[1].running is False
    assert factory.supervised[2].running is True
    assert supervisor.status.running is True
    await supervisor.close()


async def test_retained_child_reenters_bounded_wake_before_replacement(
    tmp_path: Path,
) -> None:
    """MEMORY-WAKE-202: retained execution is stopped before replacement."""

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

    first = True

    def process() -> FakeEverOSProcess:
        nonlocal first
        if first:
            first = False
            return RetainedProcess()
        return FakeEverOSProcess()

    factory = FakeEverOSProcessFactory(template=process)
    supervisor = _supervisor(
        tmp_path,
        factory,
        unavailable=observe_unavailable,
    )
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    retained = factory.supervised[0]
    await retained.unexpected_exit()
    for _ in range(20):
        await _settle()
        if (
            len(factory.supervised) == 2
            and factory.supervised[1].running
            and supervisor._restart_task is None
        ):
            break

    assert unavailable == 1
    assert retained.stops == 1
    assert retained.retains_active_config is False
    assert len(factory.supervised) == 2
    assert factory.supervised[1].running is True
    assert supervisor.status.state == "running"
    assert supervisor.status.retains_configuration is True
    await supervisor.close()


async def test_retained_child_exhausts_recovery_without_dual_ownership(
    tmp_path: Path,
) -> None:
    """MEMORY-WAKE-202: failed stop proof exhausts one bounded recovery budget."""

    class RetainedProcess(FakeEverOSProcess):
        async def unexpected_exit(self) -> None:
            self._running = False
            self._process_tree_retained = True
            if self.on_unexpected_exit is not None:
                result = self.on_unexpected_exit()
                if asyncio.iscoroutine(result):
                    await result

    retained = RetainedProcess(stop_failure=RuntimeError("tree retained"))
    factory = FakeEverOSProcessFactory(template=lambda: retained)
    supervisor = _supervisor(
        tmp_path,
        factory,
        restart_delays=(0.0, 0.0),
    )
    assert await supervisor.wake(Path(sys.executable), _settings()) is True

    await retained.unexpected_exit()
    for _ in range(40):
        await _settle()
        if supervisor._restart_task is None and retained.stops == 2:
            break

    assert retained.stops == 2
    assert retained.retains_active_config is True
    assert len(factory.supervised) == 1
    assert supervisor.status.state == "degraded"

    retained.stop_failure = None
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
