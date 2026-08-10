"""Deep lifecycle module for Memory's private EverOS sidecar."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from core.memory.process import EverOSProcessFactory, EverOSProcessPort, EverOSProcessSettings


logger = logging.getLogger(__name__)
_CALL_LOG_RETENTION_INTERVAL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True, slots=True)
class SidecarSnapshot:
    """The current sidecar ownership projection for its Runtime caller."""

    generation: int
    process: EverOSProcessPort | None
    records_calls: bool

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.running)


@dataclass(frozen=True, slots=True)
class RecorderAdmission:
    """Closed projection of the child's recorder compatibility handshake."""

    state: str
    reason: str | None

    @classmethod
    def from_health(cls, value: object) -> "RecorderAdmission":
        if not isinstance(value, dict):
            return cls("degraded", "writer_failures")
        state = value.get("state")
        reason = value.get("reason")
        if state == "active" and reason is None:
            return cls(state, reason)
        if state == "degraded" and isinstance(reason, str):
            return cls(state, reason)
        if state == "disabled" and reason in {None, "writer_failures"}:
            return cls(state, reason)
        return cls("degraded", "writer_failures")

    @property
    def records_calls(self) -> bool:
        return self.state != "disabled"

    def health(self) -> dict[str, str | None]:
        return {"state": self.state, "reason": self.reason}


class MemorySidecarLifecycle:
    """Own sidecar supervision, readiness admission, and recorder handoff.

    Runtime supplies its existing lifecycle fence for retention work; this module
    keeps the ownership state and all supervisor callbacks local.
    """

    def __init__(
        self,
        process_factory: EverOSProcessFactory,
        *,
        provider_root: Path,
        effective_home: Path,
        socket_path: Path,
        call_log_db_path: Path,
        retain_call_log: Callable[[], Awaitable[str | None]],
        on_current_sidecar_ready: Callable[[int], Awaitable[None] | None],
        on_recorder_health: Callable[[dict[str, str | None]], None],
        read_recorder_health: Callable[[], Awaitable[dict[str, str | None]]],
    ) -> None:
        self._process_factory = process_factory
        self._provider_root = provider_root
        self._effective_home = effective_home
        self._socket_path = socket_path
        self._call_log_db_path = call_log_db_path
        self._retain_call_log = retain_call_log
        self._on_current_sidecar_ready = on_current_sidecar_ready
        self._on_recorder_health = on_recorder_health
        self._read_recorder_health = read_recorder_health
        self._process: EverOSProcessPort | None = None
        self._generation = 0
        self._launch_token = 0
        self._records_calls = False
        self._ready_launch: tuple[int, int] | None = None
        self._ready_task: asyncio.Task[None] | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._retention_blocked = False
        self._ready_admission_open = True
        self._closed = False
        self._processing_probe_active = False
        self._processing_probe_healthy = False

    def snapshot(self) -> SidecarSnapshot:
        return SidecarSnapshot(self._generation, self._process, self._records_calls)

    def _replace_for_runtime(self, process: EverOSProcessPort | None) -> None:
        """Temporary Runtime compatibility for lifecycle paths not yet migrated."""

        self._generation += 1
        self._process = process

    def _set_records_calls_for_runtime(self, value: bool) -> None:
        self._records_calls = value

    async def start(self, python: Path, settings: EverOSProcessSettings) -> bool:
        """Replace the supervised sidecar, assigning ownership before start."""

        await self.stop()
        await self._stop_retention()
        self._ready_admission_open = False
        self._generation += 1
        generation = self._generation
        sidecar: EverOSProcessPort | None = None

        async def before_start() -> None:
            if not self._is_current(sidecar, generation):
                raise RuntimeError("stale EverOS recorder supervisor")
            self._launch_token += 1
            self._records_calls = True
            await self._stop_retention()

        async def reaped() -> None:
            if not self._is_current(sidecar, generation):
                return
            self._records_calls = False
            self._ensure_retention()

        def ready() -> None:
            if self._is_current(sidecar, generation):
                self._schedule_ready(generation, self._launch_token)

        sidecar = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=settings,
            socket_path=self._socket_path,
            on_ready=ready,
            before_start=before_start,
            on_reaped=reaped,
        )
        self._process = sidecar
        self._records_calls = True
        try:
            started = await sidecar.start()
        except BaseException:
            if self._is_current(sidecar, generation):
                self._records_calls = False
                self._ensure_retention()
            self._ready_admission_open = True
            raise
        if not started:
            if self._is_current(sidecar, generation):
                self._records_calls = False
                self._ensure_retention()
            self._ready_admission_open = True
            return False
        launch_token = self._launch_token
        try:
            await self._admit_recorder_health(sidecar, generation, launch_token)
        finally:
            self._ready_admission_open = True
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

    async def _admit_recorder_health(
        self,
        sidecar: EverOSProcessPort,
        generation: int,
        launch_token: int,
    ) -> None:
        """Make the child health projection, not file timing, own recorder state."""

        try:
            health = await self._read_recorder_health()
        except Exception:
            health = {"state": "degraded", "reason": "writer_failures"}
        if not self._launch_is_current(sidecar, generation, launch_token):
            return
        admission = RecorderAdmission.from_health(health)
        self._on_recorder_health(admission.health())
        if not admission.records_calls:
            self._records_calls = False
            self._ensure_retention()

    async def stop(self) -> None:
        """Stop the current child; failed cleanup intentionally retains ownership."""

        process = self._process
        if process is None:
            return
        await process.stop()
        if process is self._process:
            self._process = None
            self._generation += 1
            self._records_calls = False
            self._ensure_retention()

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
        await self._stop_retention()
        await self.stop()

    def close_ready_admission(self) -> None:
        """Synchronously reject late supervisor readiness before shutdown awaits."""

        self._ready_admission_open = False
        self._closed = True
        self._ready_launch = None

    def handoff_to_host_retention(self) -> None:
        """Start host maintenance after an external orphan handoff proved safe."""

        if not self._closed:
            self._ensure_retention()

    @property
    def retention_task(self) -> asyncio.Task[None] | None:
        return self._retention_task

    async def stop_host_retention(self) -> None:
        await self._stop_retention()

    def observe_recorder_health(self, health: dict[str, str | None]) -> None:
        """Keep host retention aligned with the live recorder's ownership."""

        if health.get("reason") == "call_log_corrupt":
            self._retention_blocked = True
            return
        admission = RecorderAdmission.from_health(health)
        if not admission.records_calls:
            self._records_calls = False
            self._ensure_retention()

    def reset_host_retention_after_clear(self) -> None:
        """Reopen retention only after Clear repaired its corrupt call-log surface."""

        self._retention_blocked = False

    def _is_current(self, process: EverOSProcessPort | None, generation: int) -> bool:
        return process is not None and process is self._process and generation == self._generation

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
                assert snapshot.process is not None
                await self._admit_recorder_health(
                    snapshot.process,
                    generation,
                    launch_token,
                )
                snapshot = self.snapshot()
                if (
                    not self._ready_admission_open
                    or snapshot.generation != generation
                    or launch_token != self._launch_token
                    or not snapshot.running
                ):
                    continue
                result = self._on_current_sidecar_ready(generation)
                if asyncio.iscoroutine(result):
                    await result

    def _ensure_retention(self) -> None:
        if (
            self._closed
            or self._retention_blocked
            or self._records_calls
            or not self._call_log_exists()
        ):
            return
        if self._retention_task is None or self._retention_task.done():
            self._retention_task = asyncio.create_task(
                self._retention_loop(), name="memory-call-log-retention"
            )

    async def _stop_retention(self) -> None:
        task = self._retention_task
        self._retention_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _retention_loop(self) -> None:
        current = asyncio.current_task()
        try:
            while True:
                if self._records_calls or not self._call_log_exists():
                    return
                reason = await self._retain_call_log()
                if reason is not None:
                    self._on_recorder_health({"state": "degraded", "reason": reason})
                    if reason == "call_log_corrupt":
                        self._retention_blocked = True
                        return
                else:
                    self._on_recorder_health({"state": "disabled", "reason": None})
                await asyncio.sleep(_CALL_LOG_RETENTION_INTERVAL_SECONDS)
        finally:
            if self._retention_task is current:
                self._retention_task = None

    def _call_log_exists(self) -> bool:
        try:
            self._call_log_db_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True
