"""Deep lifecycle module for Memory's private EverOS sidecar."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from core.memory.process import EverOSProcessFactory, EverOSProcessPort, EverOSProcessSettings

@dataclass(frozen=True, slots=True)
class SidecarSnapshot:
    """The current sidecar ownership projection for its Runtime caller."""

    generation: int
    launch_token: int
    process: EverOSProcessPort | None

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.running)

    @property
    def supervisor_can_restart(self) -> bool:
        """Whether retained launch authority can still produce a child."""

        return bool(self.process and self.process.restart_authorized)

    @property
    def retains_active_config(self) -> bool:
        """Whether this supervisor can still execute under captured settings."""

        return bool(self.process and self.process.retains_active_config)

class MemorySidecarLifecycle:
    """Own sidecar supervision and readiness admission."""

    def __init__(
        self,
        process_factory: EverOSProcessFactory,
        *,
        provider_root: Path,
        effective_home: Path,
        socket_path: Path,
        on_current_sidecar_ready: Callable[[int], Awaitable[None] | None],
    ) -> None:
        self._process_factory = process_factory
        self._provider_root = provider_root
        self._effective_home = effective_home
        self._socket_path = socket_path
        self._on_current_sidecar_ready = on_current_sidecar_ready
        self._process: EverOSProcessPort | None = None
        self._generation = 0
        self._launch_token = 0
        self._ready_launch: tuple[int, int] | None = None
        self._ready_task: asyncio.Task[None] | None = None
        self._ready_admission_open = True
        self._closed = False
        self._processing_probe_active = False
        self._processing_probe_healthy = False

    def snapshot(self) -> SidecarSnapshot:
        return SidecarSnapshot(
            self._generation,
            self._launch_token,
            self._process,
        )

    def _replace_for_runtime(self, process: EverOSProcessPort | None) -> None:
        """Temporary Runtime compatibility for lifecycle paths not yet migrated."""

        self._generation += 1
        self._process = process

    async def start(
        self,
        python: Path,
        settings: EverOSProcessSettings,
        *,
        provider_root_guard: Callable[[], None] | None = None,
    ) -> bool:
        """Replace the supervised sidecar, assigning ownership before start."""

        await self.stop()
        self._ready_admission_open = False
        self._generation += 1
        generation = self._generation
        sidecar: EverOSProcessPort | None = None

        async def before_start() -> None:
            if not self._is_current(sidecar, generation):
                raise RuntimeError("stale EverOS sidecar supervisor")
            self._launch_token += 1

        def ready() -> None:
            if self._is_current(sidecar, generation):
                self._schedule_ready(generation, self._launch_token)

        sidecar = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=settings,
            socket_path=self._socket_path,
            provider_root_guard=provider_root_guard,
            on_ready=ready,
            before_start=before_start,
        )
        self._process = sidecar
        try:
            started = await sidecar.start()
        except BaseException:
            self._reopen_ready_admission()
            raise
        if not started:
            self._reopen_ready_admission()
            return False
        launch_token = self._launch_token
        self._reopen_ready_admission()
        launch_is_current = self._launch_is_current(
            sidecar,
            generation,
            launch_token,
        )
        # Runtime accepts the successful explicit start synchronously. The
        # supervisor's same-turn ready notification is therefore redundant;
        # later recovery notifications still emit the semantic event.
        if launch_is_current and self._ready_launch == (generation, launch_token):
            self._ready_launch = None
        elif self._ready_launch is not None:
            self._ensure_ready_task()
        return launch_is_current

    async def stop(self) -> None:
        """Stop the current child; failed cleanup intentionally retains ownership."""

        process = self._process
        if process is None:
            return
        await process.stop()
        if process is self._process:
            self._process = None
            self._generation += 1

    async def probe(self, python: Path, settings: EverOSProcessSettings) -> bool:
        probe = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=settings,
            socket_path=self._socket_path,
        )
        return await probe.processing_healthy()

    async def processing_healthy(self) -> bool:
        if self._processing_probe_active:
            return self._processing_probe_healthy
        self._processing_probe_active = True
        try:
            process = self._process
            healthy = bool(process and await process.processing_healthy())
        finally:
            self._processing_probe_active = False
        self._processing_probe_healthy = healthy
        return healthy

    async def close(self) -> None:
        """Close ready admission, settle internal work, then stop ownership."""

        self.close_ready_admission()
        task = self._ready_task
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.shield(task)
            except Exception:
                pass
        await self.stop()

    def close_ready_admission(self) -> None:
        """Synchronously reject late supervisor readiness before shutdown awaits."""

        self._ready_admission_open = False
        self._closed = True
        self._ready_launch = None

    def _is_current(self, process: EverOSProcessPort | None, generation: int) -> bool:
        return process is not None and process is self._process and generation == self._generation

    def _reopen_ready_admission(self) -> None:
        if not self._closed:
            self._ready_admission_open = True

    def _launch_is_current(
        self,
        process: EverOSProcessPort | None,
        generation: int,
        launch_token: int,
    ) -> bool:
        return bool(
            self._is_current(process, generation)
            and launch_token == self._launch_token
            and process is not None
            and process.running
        )

    def _schedule_ready(self, generation: int, launch_token: int) -> None:
        self._ready_launch = (generation, launch_token)
        if not self._ready_admission_open:
            return
        self._ensure_ready_task()

    def _ensure_ready_task(self) -> None:
        task = self._ready_task
        if task is None or task.done():
            self._ready_task = asyncio.create_task(self._emit_ready(), name="memory-ready-activation")

    async def _emit_ready(self) -> None:
        while self._ready_launch is not None:
            generation, launch_token = self._ready_launch
            self._ready_launch = None
            snapshot = self.snapshot()
            if (
                self._ready_admission_open
                and snapshot.generation == generation
                and launch_token == self._launch_token
                and snapshot.running
            ):
                result = self._on_current_sidecar_ready(generation)
                if asyncio.iscoroutine(result):
                    await result
