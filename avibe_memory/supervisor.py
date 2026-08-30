"""Single process owner for Memory's private EverOS runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Literal, Protocol

from avibe_memory.process import (
    EverOSProcess,
    EverOSProcessFactory,
    EverOSProcessPort,
    EverOSProcessSettings,
    ReleasedEverOSOrphanReconciler,
)


logger = logging.getLogger(__name__)

_RESTART_DELAYS_SECONDS = (0.0, 0.5, 1.0)
_RESTART_WINDOW_SECONDS = 30.0
_RECOVERY_OWNER: ContextVar[object | None] = ContextVar(
    "avibe_memory_recovery_owner",
    default=None,
)


@dataclass(frozen=True, slots=True)
class EverOSSupervisorStatus:
    """Capability projection that deliberately omits process identity."""

    state: Literal["stopped", "starting", "running", "degraded", "closed"]
    retains_configuration: bool
    epoch: int

    @property
    def running(self) -> bool:
        return self.state == "running"


class EverOSSupervisorPort(Protocol):
    """The complete process capability seam used by ``MemoryRuntime``."""

    @property
    def status(self) -> EverOSSupervisorStatus: ...

    def begin_close(self) -> None: ...

    async def wake(
        self,
        python: Path,
        settings: EverOSProcessSettings,
        *,
        provider_root_guard: Callable[[], None] | None = None,
    ) -> bool: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...

    async def processing_healthy(self) -> bool: ...

    async def probe(self, python: Path, settings: EverOSProcessSettings) -> bool: ...

    async def reconcile_orphans(self, *, fail_closed: bool = False) -> bool: ...


class EverOSSupervisorFactory(Protocol):
    """Construct the high-level process owner without exposing child factories."""

    def __call__(
        self,
        *,
        provider_root: Path,
        effective_home: Path,
        socket_path: Path,
        on_ready: Callable[[], Awaitable[None] | None],
        on_unavailable: Callable[[], Awaitable[None] | None],
        on_recover: Callable[[], Awaitable[bool]],
    ) -> EverOSSupervisorPort: ...


class EverOSSupervisor:
    """Own launch, admission, bounded recovery, probes, and safe cleanup."""

    def __init__(
        self,
        *,
        provider_root: Path,
        effective_home: Path,
        socket_path: Path,
        on_ready: Callable[[], Awaitable[None] | None],
        on_unavailable: Callable[[], Awaitable[None] | None],
        on_recover: Callable[[], Awaitable[bool]],
        process_factory: EverOSProcessFactory | None = None,
        restart_delays: tuple[float, ...] = _RESTART_DELAYS_SECONDS,
        restart_window_seconds: float = _RESTART_WINDOW_SECONDS,
    ) -> None:
        self._provider_root = provider_root
        self._effective_home = effective_home
        self._socket_path = socket_path
        self._on_ready = on_ready
        self._on_unavailable = on_unavailable
        self._on_recover = on_recover
        self._process_factory = process_factory or EverOSProcess
        self._restart_delays = tuple(max(0.0, delay) for delay in restart_delays)
        self._restart_window_seconds = max(0.0, restart_window_seconds)
        self._lock = asyncio.Lock()
        self._child: EverOSProcessPort | None = None
        self._python: Path | None = None
        self._settings: EverOSProcessSettings | None = None
        self._provider_root_guard: Callable[[], None] | None = None
        self._launch_task: asyncio.Task[bool] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._restart_attempts = 0
        self._restart_stable_since: float | None = None
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._epoch = 0
        self._starting = False
        self._closing = False
        self._closed = False
        self._processing_probe_active = False
        self._processing_probe_healthy = False

    @property
    def status(self) -> EverOSSupervisorStatus:
        child = self._child
        restart = self._restart_task
        restart_pending = bool(restart is not None and not restart.done())
        configured = bool(
            not self._closed
            and self._python is not None
            and self._settings is not None
        )
        retains_configuration = bool(
            configured and (child is not None or restart_pending or self._starting)
        )
        if self._closed:
            state: Literal["stopped", "starting", "running", "degraded", "closed"] = (
                "closed"
            )
        elif self._starting or restart_pending:
            state = "starting"
        elif child is not None and child.running:
            state = "running"
        elif configured:
            state = "degraded"
        else:
            state = "stopped"
        return EverOSSupervisorStatus(
            state=state,
            retains_configuration=retains_configuration,
            epoch=self._epoch,
        )

    def begin_close(self) -> None:
        """Synchronously fence late child callbacks before shutdown awaits."""

        if self._closing:
            return
        self._closing = True
        self._epoch += 1
        launch = self._launch_task
        if launch is not None:
            launch.cancel()
        restart = self._restart_task
        if restart is not None:
            restart.cancel()
        for task in tuple(self._event_tasks):
            task.cancel()

    async def wake(
        self,
        python: Path,
        settings: EverOSProcessSettings,
        *,
        provider_root_guard: Callable[[], None] | None = None,
    ) -> bool:
        """Replace the current owner and retain authority for bounded recovery."""

        async with self._lock:
            if self._closing or self._closed:
                return False
            recovering = self._recovery_owned_by_current_context()
            self._epoch += 1
            epoch = self._epoch
            if not recovering:
                await self._cancel_restart_locked()
            await self._stop_child_locked()
            await self._reconcile_orphans_locked()
            self._python = Path(python)
            self._settings = settings
            self._provider_root_guard = provider_root_guard
            if not recovering:
                self._restart_attempts = 0
                self._restart_stable_since = None
            return await self._start_attempt_locked(epoch)

    async def stop(self) -> None:
        """Stop all retained execution and revoke future launch authority."""

        async with self._lock:
            recovering = self._recovery_owned_by_current_context()
            if not recovering:
                self._epoch += 1
                await self._cancel_restart_locked()
            await self._stop_child_locked()
            self._python = None
            self._settings = None
            self._provider_root_guard = None

    async def close(self) -> None:
        """Cancel recovery and prove that retained execution has stopped."""

        self.begin_close()
        async with self._lock:
            await self._cancel_restart_locked()
            await self._stop_child_locked()
            self._python = None
            self._settings = None
            self._provider_root_guard = None
            self._closed = True

    async def processing_healthy(self) -> bool:
        """Probe the owned configuration without queueing behind another probe."""

        if self._processing_probe_active:
            return self._processing_probe_healthy
        self._processing_probe_active = True
        healthy = False
        try:
            child = self._child
            healthy = bool(child and child.running and await child.processing_healthy())
        finally:
            self._processing_probe_active = False
        self._processing_probe_healthy = healthy
        return healthy

    async def probe(self, python: Path, settings: EverOSProcessSettings) -> bool:
        """Probe candidate settings in an unadmitted, short-lived child."""

        probe = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=settings,
            socket_path=self._socket_path,
        )
        return await probe.processing_healthy()

    async def reconcile_orphans(self, *, fail_closed: bool = False) -> bool:
        """Consume released ownership only when this supervisor owns no child."""

        async with self._lock:
            if self._child is not None or self._starting:
                return False
            try:
                await self._reconcile_orphans_locked()
            except Exception as exc:
                logger.warning("Recorded EverOS recovery did not finish: %s", exc)
                if fail_closed:
                    raise
                return False
            return True

    async def _reconcile_orphans_locked(self) -> None:
        reconciler = ReleasedEverOSOrphanReconciler(
            effective_home=self._effective_home,
            provider_root=self._provider_root,
        )
        await reconciler.reconcile_orphans()

    async def _start_attempt_locked(self, epoch: int) -> bool:
        python = self._python
        settings = self._settings
        if (
            self._closing
            or self._closed
            or epoch != self._epoch
            or python is None
            or settings is None
        ):
            return False

        child: EverOSProcessPort | None = None
        launch_in_progress = True

        def ready() -> None:
            if child is not None and not launch_in_progress:
                self._schedule_event(self._admit_ready(child, epoch), "memory-everos-ready")

        def unexpected_exit() -> None:
            if child is not None:
                self._schedule_event(
                    self._handle_unexpected_exit(child, epoch),
                    "memory-everos-unexpected-exit",
                )

        def before_start() -> None:
            if child is None or child is not self._child or epoch != self._epoch:
                raise RuntimeError("stale EverOS child launch")

        child = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=settings,
            socket_path=self._socket_path,
            provider_root_guard=self._provider_root_guard,
            on_ready=ready,
            before_start=before_start,
            on_unexpected_exit=unexpected_exit,
        )
        self._child = child
        self._starting = True
        launch = asyncio.create_task(child.start(), name="memory-everos-launch")
        self._launch_task = launch
        try:
            try:
                started = await launch
            except asyncio.CancelledError:
                if not self._closing and not self._closed:
                    raise
                started = False
            except Exception:
                logger.exception("EverOS launch attempt failed")
                started = False
        finally:
            launch_in_progress = False
            if self._launch_task is launch:
                self._launch_task = None
            self._starting = False
        if epoch != self._epoch or child is not self._child:
            return False
        if started and child.running:
            return True
        if not child.retains_active_config:
            self._child = None
            self._schedule_restart_locked(epoch)
        return False

    async def _admit_ready(self, child: EverOSProcessPort, epoch: int) -> None:
        async with self._lock:
            current = bool(
                not self._closing
                and epoch == self._epoch
                and child is self._child
                and child.running
            )
        if current:
            await self._notify(self._on_ready, "ready")

    async def _handle_unexpected_exit(
        self,
        child: EverOSProcessPort,
        epoch: int,
    ) -> None:
        async with self._lock:
            if self._closing or epoch != self._epoch or child is not self._child:
                return
            # A retained helper or process tree still needs the same bounded Wake
            # path. Wake retries stop proof before it can create a replacement.
            # Slow recovery does not earn a fresh budget. Only a replacement
            # that stayed admitted for the full window starts a new episode.
            stable_since = self._restart_stable_since
            if (
                stable_since is not None
                and time.monotonic() - stable_since
                >= self._restart_window_seconds
            ):
                self._restart_attempts = 0
            self._restart_stable_since = None
            if not child.retains_active_config:
                self._child = None
        await self._notify(self._on_unavailable, "unexpected-exit")
        async with self._lock:
            self._schedule_restart_locked(epoch)

    def _schedule_restart_locked(self, epoch: int) -> None:
        if self._closing or self._closed or epoch != self._epoch:
            return
        if self._restart_task is not None and not self._restart_task.done():
            return
        attempt = self._restart_attempts
        if attempt >= len(self._restart_delays):
            return
        self._restart_attempts += 1
        self._restart_task = asyncio.create_task(
            self._restart_after(epoch, self._restart_delays[attempt]),
            name="memory-everos-restart",
        )

    async def _restart_after(self, epoch: int, delay_seconds: float) -> None:
        try:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            async with self._lock:
                if (
                    self._closing
                    or self._closed
                    or epoch != self._epoch
                ):
                    return
            recovery_owner = _RECOVERY_OWNER.set(self)
            try:
                recovered = await self._on_recover()
            finally:
                _RECOVERY_OWNER.reset(recovery_owner)
            async with self._lock:
                if self._restart_task is asyncio.current_task():
                    self._restart_task = None
                if recovered and self._child is not None and self._child.running:
                    self._restart_stable_since = time.monotonic()
                    return
                self._schedule_restart_locked(self._epoch)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("EverOS bounded restart failed")
            async with self._lock:
                if self._restart_task is asyncio.current_task():
                    self._restart_task = None
                self._schedule_restart_locked(self._epoch)

    async def _stop_child_locked(self) -> None:
        child = self._child
        if child is None:
            return
        await child.stop()
        if child is self._child:
            self._child = None

    async def _cancel_restart_locked(self) -> None:
        restart = self._restart_task
        self._restart_task = None
        if restart is None or restart is asyncio.current_task():
            return
        restart.cancel()
        try:
            await restart
        except asyncio.CancelledError:
            pass

    def _recovery_owned_by_current_context(self) -> bool:
        restart = self._restart_task
        return bool(
            restart is not None
            and not restart.done()
            and (
                restart is asyncio.current_task()
                or _RECOVERY_OWNER.get() is self
            )
        )

    def _schedule_event(self, coroutine: Awaitable[None], name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    @staticmethod
    async def _notify(
        callback: Callable[[], Awaitable[None] | None],
        event: str,
    ) -> None:
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning("EverOS supervisor %s callback failed", event)
