"""Durable, idempotent Memory Clear maintenance."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncContextManager, Literal, TypeVar

from core.memory.blocking import run_blocking
from core.memory.clear_intent import (
    ClearIntent,
    ClearIntentError,
    ClearIntentStore,
    ClearIntentUnreadable,
    ClearSurface,
    DEFAULT_CLEAR_SURFACES,
    LEGACY_ABORT_ERROR_CODE,
    cleanup_legacy_backup_storage,
)
from core.memory.store import MemoryStore


logger = logging.getLogger(__name__)
_MaintenanceIOResult = TypeVar("_MaintenanceIOResult")


class MemoryStoreUnavailableError(RuntimeError):
    """Raised when the controller cannot safely use the local Memory store."""


@dataclass(frozen=True)
class MaintenanceRuntimeState:
    artifact_installing: bool


@dataclass(frozen=True)
class ClearInProgressResult:
    state: Literal["deleting", "failed"]
    operation_id: str
    occurred_at: str
    error_code: str | None


@dataclass(frozen=True)
class ClearResult:
    status: Literal["completed", "failed"]
    operation_id: str | None = None
    epoch: int | None = None
    error: str | None = None
    clear_in_progress: ClearInProgressResult | None = None


@dataclass(frozen=True)
class MaintenanceResult:
    data_exists: bool
    can_clear: bool
    clear_in_progress: ClearInProgressResult | None
    error: str | None = None


@dataclass(frozen=True)
class MaintenanceObservation:
    block_reason: str | None
    clear_in_progress: ClearInProgressResult | None
    can_clear: bool


@dataclass(frozen=True)
class MemoryMaintenanceRuntimePort:
    exclusive_fence: Callable[[], AsyncContextManager[None]]
    boot_recovery_fence: Callable[[], AsyncContextManager[None]]
    state: Callable[[], MaintenanceRuntimeState]
    enter_maintenance: Callable[[], None]
    leave_maintenance: Callable[[], None]
    pause_claims: Callable[[], Awaitable[None]]
    resume_claims: Callable[[], None]
    quiesce: Callable[[bool], Awaitable[None]]
    resume: Callable[[], Awaitable[None]]
    delete_surface: Callable[[ClearSurface, int], Awaitable[None]]
    restore_completed: Callable[[], None]


class MemoryMaintenance:
    """Own one durable Clear marker and its fenced deletion sweep."""

    def __init__(
        self,
        store: MemoryStore | None,
        *,
        effective_home: Path,
        runtime: MemoryMaintenanceRuntimePort,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._intent = ClearIntentStore(effective_home)
        self._initialization_error: Exception | None = None
        self._intent_error: Exception | None = None
        self._closing = False
        try:
            failures = cleanup_legacy_backup_storage(effective_home)
            for relative in failures:
                logger.warning("Legacy Memory cleanup could not remove %s", relative)
        except Exception:
            logger.warning("Legacy Memory backup cleanup could not complete", exc_info=True)
        self._migrate_legacy(store)

    @property
    def ready(self) -> bool:
        return self._store is not None and self._initialization_error is None

    def attach_store(self, store: MemoryStore) -> None:
        self._store = store
        self._migrate_legacy(store)

    def _migrate_legacy(self, store: MemoryStore | None) -> None:
        if self._intent_error is not None:
            return
        try:
            current_epoch = store.ensure_meta().epoch if store is not None else None
            self._intent.migrate_legacy(current_epoch=current_epoch)
        except ClearIntentUnreadable as error:
            self._intent_error = error
            logger.warning("Memory Clear state is unreadable; keeping Memory fenced")
        except Exception as error:
            self._initialization_error = error
            logger.exception("Memory Clear state migration failed")

    def is_open(self) -> bool:
        if self._initialization_error is not None or self._intent_error is not None:
            return True
        try:
            return self._intent.load() is not None
        except ClearIntentUnreadable:
            self._intent_error = ClearIntentUnreadable("Memory clear intent marker is unreadable")
            return True

    def can_disable_without_authority(self) -> bool:
        # A pure disable is still safe when a marker cannot be read. Enabled
        # Memory remains fenced until the marker is repaired or re-run.
        return self._initialization_error is not None or self._intent_error is not None

    def has_open_restore(self) -> bool:
        return False

    def has_readable_intent(self) -> bool:
        """Return whether boot can safely replay a readable Clear marker."""

        if (
            self._store is None
            or self._initialization_error is not None
            or self._intent_error is not None
        ):
            return False
        try:
            return self._intent.load() is not None
        except ClearIntentUnreadable:
            self._intent_error = ClearIntentUnreadable("Memory clear intent marker is unreadable")
            return False

    def recovery(self, *, operator_ref: str | None = None) -> ClearInProgressResult | None:
        return self._read_projection()

    def _read_projection(self) -> ClearInProgressResult | None:
        try:
            intent = self._intent.load()
        except ClearIntentUnreadable:
            self._intent_error = ClearIntentUnreadable("Memory clear intent marker is unreadable")
            return ClearInProgressResult(
                state="failed",
                operation_id="unreadable",
                occurred_at="",
                error_code="memory_clear_marker_unreadable",
            )
        if intent is None:
            if self._intent_error is None:
                return None
            return ClearInProgressResult(
                state="failed",
                operation_id="unreadable",
                occurred_at="",
                error_code="memory_clear_marker_unreadable",
            )
        return _projection(intent)

    async def observe(self, *, operator_ref: str | None = None) -> MaintenanceObservation:
        return await self._run_maintenance_io(self._observe)

    def _observe(self) -> MaintenanceObservation:
        clear_in_progress = self._read_projection()
        if self._initialization_error is not None or self._intent_error is not None:
            block_reason = "memory_clear_failed"
        elif clear_in_progress is None:
            block_reason = None
        elif clear_in_progress.state == "failed":
            block_reason = clear_in_progress.error_code or "memory_clear_failed"
        else:
            block_reason = "busy"
        can_clear = self.ready and (
            block_reason is None
            or self._intent_error is not None
            or (clear_in_progress is not None and clear_in_progress.state == "failed")
        )
        return MaintenanceObservation(
            block_reason=block_reason,
            clear_in_progress=clear_in_progress,
            can_clear=can_clear,
        )

    async def maintenance_payload(
        self,
        *,
        operator_ref: str | None = None,
        observation: MaintenanceObservation | None = None,
    ) -> MaintenanceResult:
        observation = observation or await self.observe(operator_ref=operator_ref)
        if self._store is None:
            return MaintenanceResult(True, False, observation.clear_in_progress, "memory_store_unavailable")
        try:
            meta, history = await asyncio.gather(
                asyncio.to_thread(self._store.get_meta),
                asyncio.to_thread(self._store.has_provider_data_history),
            )
        except Exception:
            return MaintenanceResult(True, False, observation.clear_in_progress, "memory_store_unavailable")
        latest = await self.observe(operator_ref=operator_ref)
        return MaintenanceResult(
            data_exists=bool(history or (meta is not None and meta.last_success_at)),
            can_clear=latest.can_clear,
            clear_in_progress=latest.clear_in_progress,
            error="memory_clear_failed" if latest.block_reason == "memory_clear_failed" else None,
        )

    async def clear(self, *, operator_ref: str) -> ClearResult:
        return await self._run_clear(operator_ref=operator_ref, boot=False)

    async def reconcile_pending(self) -> bool:
        """Finish a marker-owned Clear during boot/reconcile."""

        if self._intent_error is not None or self._store is None:
            return False
        try:
            intent = await self._run_maintenance_io(self._intent.load)
        except ClearIntentUnreadable:
            self._intent_error = ClearIntentUnreadable("Memory clear intent marker is unreadable")
            return False
        if intent is None:
            return True
        if intent.state == "failed" and intent.error_code == LEGACY_ABORT_ERROR_CODE:
            # The old Abort-and-restore choice cannot be replayed after the
            # snapshot stack is removed. Keep the migrated operation fenced;
            # an explicit new Clear is the destructive opt-in.
            return False
        if intent.state == "failed":
            intent = intent.deleting()
            try:
                await self._run_maintenance_io(lambda: self._intent.write(intent))
            except ClearIntentError:
                self._intent_error = ClearIntentError(
                    "Memory Clear retry marker could not be written"
                )
                return False
        async with self._runtime.boot_recovery_fence():
            self._runtime.enter_maintenance()
            try:
                await self._run_maintenance_io(self._store.begin_clear_fence)
                await self._runtime.quiesce(True)
                result = await self._run_clear_intent(intent, operator_ref=intent.operator_ref, boot=True)
                return result.status == "completed"
            except asyncio.CancelledError:
                await self._record_failure(intent, "memory_clear_failed")
                raise
            except Exception:
                await self._record_failure(intent, "memory_clear_failed")
                return False
            finally:
                self._runtime.leave_maintenance()

    async def _run_clear(self, *, operator_ref: str, boot: bool) -> ClearResult:
        if self._store is None:
            raise MemoryStoreUnavailableError("Memory store is unavailable")
        async with self._runtime.exclusive_fence():
            self._runtime.enter_maintenance()
            try:
                try:
                    intent = await self._run_maintenance_io(self._intent.load)
                except ClearIntentUnreadable:
                    # A user initiated re-run is explicitly allowed to replace
                    # an unreadable marker with a fresh operation.
                    intent = None
                if intent is None:
                    meta = await self._run_maintenance_io(self._store.ensure_meta)
                    intent = ClearIntent.new(operator_ref=operator_ref, pre_epoch=meta.epoch)
                    await self._run_maintenance_io(lambda: self._intent.write(intent))
                    self._intent_error = None
                elif intent.state == "failed":
                    intent = intent.deleting()
                    await self._run_maintenance_io(lambda: self._intent.write(intent))
                    self._intent_error = None
                try:
                    await self._run_maintenance_io(self._store.begin_clear_fence)
                    await self._runtime.pause_claims()
                    await self._runtime.quiesce(False)
                except asyncio.CancelledError:
                    self._runtime.resume_claims()
                    raise
                except Exception:
                    self._runtime.resume_claims()
                    await self._record_failure(intent, "memory_clear_failed")
                    return ClearResult(status="failed", error="memory_clear_failed")
                assert intent is not None
                return await self._run_clear_intent(intent, operator_ref=operator_ref, boot=boot)
            finally:
                self._runtime.leave_maintenance()

    async def _run_clear_intent(
        self,
        intent: ClearIntent,
        *,
        operator_ref: str,
        boot: bool,
    ) -> ClearResult:
        assert self._store is not None
        try:
            for surface in DEFAULT_CLEAR_SURFACES:
                await self._runtime.delete_surface(surface, intent.target_epoch)
            await self._run_maintenance_io(self._store.release_clear_fence)
            try:
                await self._run_maintenance_io(self._intent.remove)
            except ClearIntentError:
                # All surfaces are already terminal. Keep the deleting marker
                # so the next reconcile retries only its removal.
                logger.warning("Memory Clear completed but intent removal failed", exc_info=True)
                return ClearResult(
                    status="failed",
                    error="memory_clear_failed",
                    clear_in_progress=_projection(intent),
                )
        except asyncio.CancelledError:
            await self._record_failure(intent, "memory_clear_failed")
            raise
        except Exception as error:
            error_code = getattr(error, "error", None) or "memory_clear_failed"
            await self._record_failure(intent, str(error_code))
            return self._failed_result()
        self._runtime.restore_completed()
        if not boot:
            await self._runtime.resume()
        self._intent_error = None
        return ClearResult("completed", intent.operation_id, intent.target_epoch)

    async def _record_failure(self, intent: ClearIntent, error_code: str) -> None:
        try:
            await self._run_maintenance_io(lambda: self._intent.write(intent.failed(error_code)))
        except Exception:
            logger.exception("Memory Clear failure could not be persisted")

    def _failed_result(self) -> ClearResult:
        return ClearResult(
            status="failed",
            error="memory_clear_failed",
            clear_in_progress=self._read_projection(),
        )

    async def recover_boot(self) -> bool:
        return await self.reconcile_pending()

    def ensure_housekeeping(self) -> None:
        return None

    async def close(self) -> None:
        self._closing = True

    @staticmethod
    async def _run_maintenance_io(operation: Callable[[], _MaintenanceIOResult]) -> _MaintenanceIOResult:
        return await run_blocking(operation)


def _projection(intent: ClearIntent) -> ClearInProgressResult:
    return ClearInProgressResult(
        state=intent.state,
        operation_id=intent.operation_id,
        occurred_at=intent.updated_at,
        error_code=intent.error_code,
    )
