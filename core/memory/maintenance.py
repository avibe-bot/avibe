"""Durable, idempotent Memory Clear maintenance."""

from __future__ import annotations

import asyncio
import logging
import os
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
    inspect_legacy_clear_abort,
    inspect_legacy_backup_restore,
)
from core.memory.confined_filesystem import ConfinedFilesystemError, required_no_follow_flag
from core.memory.operation_lock import MemoryOperationBusy, MemoryOperationLease
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
        self._legacy_migration_deferred = False
        self._legacy_restore_open = False
        self._legacy_restore_unreadable = False
        self._legacy_abort_open = False
        self._closing = False
        try:
            required_no_follow_flag()
        except ConfinedFilesystemError as error:
            self._initialization_error = error
            logger.warning("Memory Clear persistence is unavailable; keeping Memory fenced")
            return
        self._migrate_legacy(store)

    @property
    def ready(self) -> bool:
        return self._store is not None and self._initialization_error is None

    def attach_store(self, store: MemoryStore) -> None:
        self._store = store
        self._migrate_legacy(store)

    def _migrate_legacy(self, store: MemoryStore | None, *, lease_held: bool = False) -> None:
        if self._intent_error is not None:
            return
        legacy_paths = (
            self._intent.home / "state/memory/clear-journal.sqlite",
            self._intent.home / "state/memory/clear-snapshots",
            self._intent.home / "state/memory/backup-restore-journal.sqlite",
            self._intent.home / "state/memory/backups",
        )
        if not any(os.path.lexists(path) for path in legacy_paths):
            self._legacy_migration_deferred = False
            self._legacy_restore_open = False
            self._legacy_restore_unreadable = False
            self._legacy_abort_open = False
            return
        if store is None and os.path.lexists(
            self._intent.home / "state/memory/clear-journal.sqlite"
        ):
            self._legacy_migration_deferred = True
            return
        lease = None
        if not lease_held:
            lease = MemoryOperationLease(self._intent.home)
            try:
                lease.acquire()
            except MemoryOperationBusy:
                self._legacy_migration_deferred = True
                logger.warning(
                    "Legacy Memory cleanup deferred while another operation owns the lease"
                )
                return
            except Exception:
                self._initialization_error = ClearIntentError(
                    "Legacy Memory cleanup lease could not be acquired"
                )
                logger.warning("Legacy Memory cleanup lease could not be acquired", exc_info=True)
                return
        try:
            backup_restore = inspect_legacy_backup_restore(self._intent.home)
            self._legacy_restore_unreadable = False
            if backup_restore == "open":
                self._legacy_restore_open = True
                self._legacy_migration_deferred = True
                logger.warning(
                    "Legacy Memory backup restore remains open; keeping Memory fenced"
                )
                return
            self._legacy_restore_open = False
            if inspect_legacy_clear_abort(self._intent.home):
                self._legacy_abort_open = True
                self._legacy_migration_deferred = True
                logger.warning(
                    "Legacy Memory abort restore remains open; retaining journal and snapshots"
                )
                return
            self._legacy_abort_open = False
            current_epoch = store.ensure_meta().epoch if store is not None else None
            self._intent.migrate_legacy(current_epoch=current_epoch)
            # Validate and migrate the retired Clear journal before deleting
            # snapshots that may be its only recovery material.
            failures = cleanup_legacy_backup_storage(self._intent.home)
            if failures:
                self._legacy_migration_deferred = True
                for relative in failures:
                    logger.warning("Legacy Memory cleanup could not remove %s", relative)
                return
            self._legacy_migration_deferred = False
            self._legacy_abort_open = False
        except ClearIntentUnreadable as error:
            if os.path.lexists(
                self._intent.home / "state/memory/backup-restore-journal.sqlite"
            ):
                self._legacy_migration_deferred = True
                self._legacy_restore_unreadable = True
                logger.warning(
                    "Legacy Memory backup restore state is unreadable; keeping Memory fenced"
                )
            else:
                self._legacy_migration_deferred = False
                self._intent_error = error
                logger.warning("Memory Clear state is unreadable; keeping Memory fenced")
        except Exception as error:
            # The operation lease is held here, so a transient store read or
            # durable journal-removal failure can be retried by boot recovery.
            self._legacy_migration_deferred = True
            logger.exception("Memory Clear state migration failed")
        finally:
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    self._legacy_migration_deferred = True
                    self._initialization_error = ClearIntentError(
                        "Legacy Memory cleanup lease could not be released"
                    )
                    logger.warning(
                        "Legacy Memory cleanup lease could not be released",
                        exc_info=True,
                    )

    def is_open(self) -> bool:
        if (
            self._initialization_error is not None
            or self._intent_error is not None
            or self._legacy_migration_deferred
        ):
            return True
        if self._store is not None:
            try:
                if self._store.clear_in_progress():
                    return True
            except Exception:
                self._initialization_error = ClearIntentError(
                    "Memory Clear fence state could not be read"
                )
                return True
        try:
            return self._intent.load() is not None
        except ClearIntentUnreadable:
            self._intent_error = ClearIntentUnreadable("Memory clear intent marker is unreadable")
            return True

    def can_disable_without_authority(self) -> bool:
        # A pure disable is still safe when a marker cannot be read. Enabled
        # Memory remains fenced until the marker is repaired or re-run.
        return (
            self._initialization_error is not None
            or self._intent_error is not None
            or self._legacy_restore_open
            or self._legacy_abort_open
            or self._legacy_migration_deferred
        )

    def has_open_restore(self) -> bool:
        return self._legacy_restore_open

    def has_legacy_restore_authority(self) -> bool:
        """Return whether an old backup restore still needs to be resolved."""

        return self._legacy_restore_open or self._legacy_restore_unreadable

    def has_readable_intent(self) -> bool:
        """Return whether boot can safely replay a readable Clear marker."""

        if (
            self._store is None
            or self._initialization_error is not None
            or self._intent_error is not None
        ):
            return False
        if self._legacy_migration_deferred:
            return True
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
                if self._store is None:
                    return None
                try:
                    orphaned_fence = self._store.clear_in_progress()
                except Exception:
                    self._initialization_error = ClearIntentError(
                        "Memory Clear fence state could not be read"
                    )
                    orphaned_fence = True
                if orphaned_fence:
                    return ClearInProgressResult(
                        state="failed",
                        operation_id="orphaned-fence",
                        occurred_at="",
                        error_code="memory_clear_failed",
                    )
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
        elif self._legacy_migration_deferred:
            block_reason = "memory_operation_in_progress"
        elif clear_in_progress is None:
            block_reason = None
        elif clear_in_progress.state == "failed":
            block_reason = clear_in_progress.error_code or "memory_clear_failed"
        else:
            block_reason = "busy"
        can_clear = self.ready and (
            block_reason is None
            or self._intent_error is not None
            or (self._legacy_abort_open and not self.has_legacy_restore_authority())
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

    async def reconcile_pending(self, *, lease_held: bool = False) -> bool:
        """Finish a marker-owned Clear during boot/reconcile."""

        if self._store is None:
            return False
        if self._legacy_migration_deferred:
            await self._run_maintenance_io(
                lambda: self._migrate_legacy(self._store, lease_held=lease_held)
            )
        if (
            self._initialization_error is not None
            or self._intent_error is not None
            or self._legacy_migration_deferred
        ):
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
                await self._runtime.quiesce(False)
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
        # Re-inspect while the current operation lease is held so legacy state
        # published before this Clear cannot bypass admission through stale
        # cached flags.
        await self._run_maintenance_io(
            lambda: self._migrate_legacy(self._store, lease_held=True)
        )
        if self.has_legacy_restore_authority() or (
            self._legacy_migration_deferred and not self._legacy_abort_open
        ):
            return ClearResult(status="failed", error="memory_operation_in_progress")
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
                    write_intent = True
                elif intent.state == "failed":
                    meta = await self._run_maintenance_io(self._store.ensure_meta)
                    if meta.epoch not in {intent.pre_epoch, intent.target_epoch}:
                        intent = ClearIntent.new(operator_ref=operator_ref, pre_epoch=meta.epoch)
                    else:
                        intent = intent.deleting()
                    write_intent = True
                else:
                    write_intent = False
                try:
                    await self._settle_prepare_clear(intent, write_intent=write_intent)
                except asyncio.CancelledError:
                    await self._record_failure(intent, "memory_clear_failed")
                    raise
                except Exception:
                    await self._record_failure(intent, "memory_clear_failed")
                    return ClearResult(status="failed", error="memory_clear_failed")
                assert intent is not None
                return await self._run_clear_intent(intent, operator_ref=operator_ref, boot=boot)
            finally:
                self._runtime.leave_maintenance()

    async def _settle_prepare_clear(self, intent: ClearIntent, *, write_intent: bool) -> None:
        cancellation: asyncio.CancelledError | None = None
        write_error: Exception | None = None

        async def prepare() -> None:
            nonlocal cancellation, write_error
            assert self._store is not None
            if write_intent:
                try:
                    await self._run_maintenance_io(lambda: self._intent.write(intent))
                except asyncio.CancelledError as error:
                    # The blocking write has settled durably; finish the
                    # safety fence before propagating the caller cancellation.
                    cancellation = error
                except Exception as error:
                    # The marker may have been renamed before its directory
                    # fsync failed. Continue fencing claims before reporting
                    # the write failure to the clear coordinator.
                    write_error = error
                else:
                    self._intent_error = None
            await self._runtime.pause_claims()
            await self._run_maintenance_io(self._store.begin_clear_fence)
            await self._runtime.quiesce(False)
            await self._run_maintenance_io(self._intent.consume_legacy_clear_state)
            await self._run_maintenance_io(self._intent.consume_legacy_snapshots)
            # Re-scan retired artifacts while the replacement operation owns
            # the lease, clearing an abort-only fence without dropping a
            # separate unresolved backup-restore authority.
            await self._run_maintenance_io(
                lambda: self._migrate_legacy(self._store, lease_held=True)
            )
            if write_error is not None:
                raise write_error
            if cancellation is not None:
                raise cancellation

        task = asyncio.create_task(prepare())
        outer_cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                outer_cancellation = outer_cancellation or error
            except Exception:
                break
        if outer_cancellation is not None:
            try:
                task.result()
            except (Exception, asyncio.CancelledError):
                pass
            raise outer_cancellation
        task.result()

    async def _run_clear_intent(
        self,
        intent: ClearIntent,
        *,
        operator_ref: str,
        boot: bool,
    ) -> ClearResult:
        assert self._store is not None
        if self._legacy_restore_open:
            return ClearResult(status="failed", error="memory_operation_in_progress")
        try:
            for surface in DEFAULT_CLEAR_SURFACES:
                await self._runtime.delete_surface(surface, intent.target_epoch)
            final_cancellation: asyncio.CancelledError | None = None
            try:
                remove_outcome: dict[str, BaseException] = {}

                def remove_marker() -> None:
                    try:
                        self._intent.remove()
                    except BaseException as error:
                        remove_outcome["error"] = error
                        raise

                await self._run_maintenance_io(remove_marker)
            except asyncio.CancelledError as error:
                if "error" in remove_outcome:
                    raise remove_outcome["error"]
                final_cancellation = final_cancellation or error
            except ClearIntentError:
                # All surfaces are already terminal. Keep the deleting marker
                # so the next reconcile retries only its removal. The marker
                # may already be unlinked when its parent fsync fails, so
                # rewrite a failed projection instead of treating that as done.
                logger.warning("Memory Clear completed but intent removal failed", exc_info=True)
                try:
                    await self._run_maintenance_io(
                        lambda: self._intent.write(intent.failed("memory_clear_failed"))
                    )
                except ClearIntentError:
                    logger.exception("Memory Clear recovery marker could not be retained")
                    self._intent_error = ClearIntentError(
                        "Memory Clear recovery marker could not be retained"
                    )
                return ClearResult(
                    status="failed",
                    error="memory_clear_failed",
                    clear_in_progress=self._read_projection() or _projection(intent.failed("memory_clear_failed")),
                )
            release_outcome: dict[str, BaseException] = {}

            def release_fence() -> None:
                try:
                    self._store.release_clear_fence()
                except BaseException as error:
                    release_outcome["error"] = error
                    raise

            try:
                await self._run_maintenance_io(release_fence)
            except asyncio.CancelledError as error:
                if "error" in release_outcome:
                    if not await self._settle_clear_fence_release(intent):
                        return self._failed_result()
                else:
                    final_cancellation = error
            except Exception:
                if not await self._settle_clear_fence_release(intent):
                    return self._failed_result()
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
        if final_cancellation is not None:
            logger.info("Memory Clear reached terminal state after final-stage cancellation")
        return ClearResult("completed", intent.operation_id, intent.target_epoch)

    async def _settle_clear_fence_release(self, intent: ClearIntent) -> bool:
        """Keep replay authority only when the shared queue fence is still held."""

        assert self._store is not None
        try:
            fence_open = await self._run_maintenance_io(self._store.clear_in_progress)
        except Exception:
            self._intent_error = ClearIntentError(
                "Memory Clear fence release could not be determined"
            )
            return False
        if not fence_open:
            return True
        await self._record_failure(intent, "memory_clear_failed")
        return False

    async def _record_failure(self, intent: ClearIntent, error_code: str) -> None:
        try:
            await self._run_maintenance_io(lambda: self._intent.write(intent.failed(error_code)))
        except Exception:
            logger.exception("Memory Clear failure could not be persisted")
            self._intent_error = ClearIntentError("Memory Clear failure could not be persisted")

    def _failed_result(self) -> ClearResult:
        return ClearResult(
            status="failed",
            error="memory_clear_failed",
            clear_in_progress=self._read_projection(),
        )

    async def recover_boot(self, *, lease_held: bool = False) -> bool:
        return await self.reconcile_pending(lease_held=lease_held)

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
