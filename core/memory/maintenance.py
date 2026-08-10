"""Durable Clear, backup/restore, and housekeeping ownership for Memory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncContextManager, Literal, TypeVar

from core.memory.backup_restore_journal import (
    BackupRestoreConflict,
    BackupRestoreOperation,
    MemoryBackupRestoreJournal,
)
from core.memory.blocking import run_blocking
from core.memory.clear_journal import ClearOperation, ClearSurface, MemoryClearJournal
from core.memory.snapshot import MemorySnapshot, MemorySnapshotManager
from core.memory.store import MemoryStore


logger = logging.getLogger(__name__)


_MaintenanceIOResult = TypeVar("_MaintenanceIOResult")


class MemoryStoreUnavailableError(RuntimeError):
    """Raised when the controller cannot safely use the local Memory store."""


@dataclass(frozen=True)
class MaintenanceRuntimeState:
    """Dynamic runtime gates needed by maintenance-owned jobs."""

    artifact_installing: bool


@dataclass(frozen=True)
class ClearRecoveryResult:
    state: str
    operation_id: str
    occurred_at: str
    error_code: str | None
    can_resume: bool
    can_abort: bool


@dataclass(frozen=True)
class ClearResult:
    status: Literal["completed", "aborted", "failed"]
    operation_id: str | None = None
    epoch: int | None = None
    error: str | None = None
    recovery: ClearRecoveryResult | None = None


@dataclass(frozen=True)
class MaintenanceResult:
    data_exists: bool
    can_clear: bool
    clear_recovery: ClearRecoveryResult | None
    error: str | None = None


@dataclass(frozen=True)
class MaintenanceObservation:
    """One point-in-time view of the journals used by read projections."""

    block_reason: str | None
    clear_recovery: ClearRecoveryResult | None
    can_clear: bool


@dataclass(frozen=True)
class MemoryMaintenanceRuntimePort:
    """Capability-shaped access to lifecycle state retained by MemoryRuntime."""

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


class MemoryMaintenance:
    """Own the complete durable maintenance state machine for one Memory store."""

    def __init__(
        self,
        store: MemoryStore | None,
        *,
        effective_home: Path,
        runtime: MemoryMaintenanceRuntimePort,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._backup_active = False
        self._backup_restore_journal: MemoryBackupRestoreJournal | None = None
        self._clear_journal: MemoryClearJournal | None = None
        self._snapshot_manager: MemorySnapshotManager | None = None
        self._backup_manager: MemorySnapshotManager | None = None
        self._initialization_error: Exception | None = None
        self._terminal_snapshot_gc_task: asyncio.Task[None] | None = None
        self._backup_stage_reconcile_task: asyncio.Task[None] | None = None
        self._closing = False
        try:
            self._backup_restore_journal = MemoryBackupRestoreJournal(effective_home)
            self._clear_journal = MemoryClearJournal(effective_home)
            self._snapshot_manager = MemorySnapshotManager(effective_home)
            self._backup_manager = MemorySnapshotManager._for_backup(
                effective_home,
                operation_guard=self._clear_journal.assert_backup_allowed,
            )
            self._backup_restore_journal.mark_boot_recovery_needed()
            self._clear_journal.mark_boot_recovery_needed()
        except Exception as exc:
            self._initialization_error = exc
            logger.exception(
                "Memory maintenance journal initialization failed; maintenance is fenced"
            )

    @property
    def ready(self) -> bool:
        return (
            self._store is not None
            and self._backup_restore_journal is not None
            and self._clear_journal is not None
            and self._snapshot_manager is not None
            and self._backup_manager is not None
            and self._initialization_error is None
        )

    def attach_store(self, store: MemoryStore) -> None:
        """Attach the store after a transient boot-time open failure recovers."""

        self._store = store

    def is_open(self) -> bool:
        """Fail closed unless every durable authority proves terminal."""

        if self._backup_active:
            return True
        restore_journal = self._backup_restore_journal
        if restore_journal is None:
            return True
        try:
            if restore_journal.get_open_operation() is not None:
                return True
        except Exception:
            return True
        journal = self._clear_journal
        if journal is None or self._initialization_error is not None:
            return True
        try:
            return journal.get_open_operation() is not None
        except Exception:
            return True

    def observation_block_reason(self) -> str | None:
        """Describe why read-only provider observations are currently fenced."""

        if self._backup_active:
            return "busy"
        restore_journal = self._backup_restore_journal
        if restore_journal is None or self._initialization_error is not None:
            return "memory_store_unavailable"
        try:
            if restore_journal.get_open_operation() is not None:
                return "busy"
        except Exception:
            return "memory_store_unavailable"
        clear_journal = self._clear_journal
        if clear_journal is None:
            return "memory_store_unavailable"
        try:
            operation = clear_journal.get_open_operation()
        except Exception:
            return "memory_store_unavailable"
        if operation is None:
            return None
        if operation.state == "recovery_needed":
            return operation.closed_error or "memory_clear_failed"
        return "busy"

    def can_disable_without_authority(self) -> bool:
        restore_journal = self._backup_restore_journal
        clear_journal = self._clear_journal
        if (
            restore_journal is None
            or clear_journal is None
            or self._initialization_error is not None
        ):
            return True
        try:
            restore_journal.get_open_operation()
            clear_journal.get_open_operation()
        except Exception:
            return True
        return False

    def has_open_restore(self) -> bool:
        return self._open_backup_restore_operation() is not None

    def recovery(self, *, operator_ref: str | None = None) -> ClearRecoveryResult | None:
        journal = self._clear_journal
        if journal is None:
            return None
        try:
            observation = journal.observe_open_operation(
                operator_ref=(
                    None if self._initialization_error is not None else operator_ref
                )
            )
        except Exception:
            return None
        if observation.operation is None:
            return None
        return self._clear_recovery(
            observation.operation,
            can_resume=observation.can_resume,
            can_abort=observation.can_abort,
        )

    @staticmethod
    def _clear_recovery(
        operation: ClearOperation,
        *,
        can_resume: bool,
        can_abort: bool,
    ) -> ClearRecoveryResult:
        return ClearRecoveryResult(
            state=operation.state,
            operation_id=operation.operation_id,
            occurred_at=operation.updated_at,
            error_code=operation.closed_error,
            can_resume=can_resume,
            can_abort=can_abort,
        )

    async def observe(
        self,
        *,
        operator_ref: str | None = None,
    ) -> MaintenanceObservation:
        """Read both maintenance journals without blocking the controller loop."""

        return await self._run_maintenance_io(
            lambda: self._observe(operator_ref=operator_ref)
        )

    def _observe(
        self,
        *,
        operator_ref: str | None,
    ) -> MaintenanceObservation:
        backup_active = self._backup_active
        restore_operation: BackupRestoreOperation | None = None
        clear_operation: ClearOperation | None = None
        clear_can_resume = False
        clear_can_abort = False
        initialization_unavailable = self._initialization_error is not None

        restore_journal = self._backup_restore_journal
        restore_unavailable = restore_journal is None or initialization_unavailable
        if not restore_unavailable:
            assert restore_journal is not None
            try:
                restore_operation = restore_journal.get_open_operation()
            except Exception:
                restore_unavailable = True

        clear_journal = self._clear_journal
        clear_unavailable = clear_journal is None or initialization_unavailable
        if not clear_unavailable:
            assert clear_journal is not None
            try:
                clear_observation = clear_journal.observe_open_operation(
                    operator_ref=operator_ref
                )
                clear_operation = clear_observation.operation
                clear_can_resume = clear_observation.can_resume
                clear_can_abort = clear_observation.can_abort
            except Exception:
                clear_unavailable = True

        recovery = (
            None
            if clear_operation is None
            else self._clear_recovery(
                clear_operation,
                can_resume=clear_can_resume,
                can_abort=clear_can_abort,
            )
        )
        if backup_active:
            block_reason = "busy"
        elif restore_unavailable:
            block_reason = "memory_store_unavailable"
        elif restore_operation is not None:
            block_reason = "busy"
        elif clear_unavailable:
            block_reason = "memory_store_unavailable"
        elif clear_operation is None:
            block_reason = None
        elif clear_operation.state == "recovery_needed":
            block_reason = clear_operation.closed_error or "memory_clear_failed"
        else:
            block_reason = "busy"
        return MaintenanceObservation(
            block_reason=block_reason,
            clear_recovery=recovery,
            can_clear=(
                self.ready
                and block_reason is None
                and recovery is None
            ),
        )

    async def maintenance_payload(
        self,
        *,
        operator_ref: str | None = None,
        observation: MaintenanceObservation | None = None,
    ) -> MaintenanceResult:
        if observation is None:
            observation = await self.observe(operator_ref=operator_ref)
        try:
            meta, history, manual_required = await asyncio.gather(
                asyncio.to_thread(self._store.get_meta),
                asyncio.to_thread(self._store.has_provider_data_history),
                asyncio.to_thread(self._store.has_manual_required_fence),
            )
        except Exception:
            return MaintenanceResult(
                data_exists=True,
                can_clear=False,
                clear_recovery=observation.clear_recovery,
                error="memory_store_unavailable",
            )
        try:
            latest = await self.observe(operator_ref=operator_ref)
        except Exception:
            return MaintenanceResult(
                data_exists=bool(history or (meta is not None and meta.last_success_at)),
                can_clear=False,
                clear_recovery=None,
                error="memory_store_unavailable",
            )
        changed = latest != observation
        return MaintenanceResult(
            data_exists=bool(history or (meta is not None and meta.last_success_at)),
            can_clear=(
                not changed
                and latest.can_clear
                and not manual_required
            ),
            clear_recovery=latest.clear_recovery,
            error=(
                "memory_store_unavailable"
                if latest.block_reason == "memory_store_unavailable"
                else None
            ),
        )

    async def create_backup(self, backup_id: str | None = None) -> MemorySnapshot:
        return await self._run_backup_operation(lambda manager: manager.create(backup_id))

    async def restore_backup(
        self,
        backup_id: str,
        *,
        expected_manifest_sha256: str,
        expected_surface_digests: Mapping[str, str | None],
    ) -> MemorySnapshot:
        clear_journal = self._require_clear_journal()
        restore_journal = self._require_backup_restore_journal()
        manager = self._require_backup_manager()
        async with self._runtime.exclusive_fence():
            clear_journal.assert_backup_allowed()
            operation = restore_journal.get_open_operation()
            if operation is not None and (
                operation.state != "recovery_needed"
                or operation.backup_id != backup_id
                or operation.manifest_sha256 != expected_manifest_sha256
                or operation.digest_mapping() != dict(expected_surface_digests)
            ):
                raise BackupRestoreConflict(
                    "a different Memory backup restore owns the recovery fence"
                )
            self._backup_active = True
            self._runtime.enter_maintenance()
            try:
                await self._runtime.quiesce(False)
                if operation is not None:
                    operation = await self._run_maintenance_io(
                        lambda: restore_journal.claim_retry(
                            operation.operation_id,
                            expected_revision=operation.revision,
                            actor_ref="system:runtime",
                        )
                    )

                def begin_restore(snapshot: MemorySnapshot) -> None:
                    nonlocal operation
                    operation = restore_journal.start(snapshot)

                restored = await self._run_maintenance_io(
                    lambda: manager.restore(
                        backup_id,
                        expected_manifest_sha256=expected_manifest_sha256,
                        expected_surface_digests=expected_surface_digests,
                        before_replace=None if operation is not None else begin_restore,
                    )
                )
                if operation is None:
                    raise RuntimeError("Memory backup restore intent was not recorded")
                operation = await self._run_maintenance_io(
                    lambda: restore_journal.mark_completed(
                        operation.operation_id,
                        expected_revision=operation.revision,
                        execution_token=self._backup_restore_execution_token(operation),
                    )
                )
                return restored
            except BaseException:
                await self._mark_backup_restore_recovery(operation)
                raise
            finally:
                self._backup_active = False
                try:
                    if restore_journal.get_open_operation() is None:
                        await self._runtime.resume()
                finally:
                    self._runtime.leave_maintenance()

    async def _run_backup_operation(
        self,
        operation: Callable[[MemorySnapshotManager], MemorySnapshot],
    ) -> MemorySnapshot:
        journal = self._require_clear_journal()
        restore_journal = self._require_backup_restore_journal()
        manager = self._require_backup_manager()
        async with self._runtime.exclusive_fence():
            journal.assert_backup_allowed()
            restore_journal.assert_idle()
            self._backup_active = True
            self._runtime.enter_maintenance()
            try:
                await self._runtime.quiesce(False)
                return await self._run_maintenance_io(lambda: operation(manager))
            finally:
                self._backup_active = False
                try:
                    await self._runtime.resume()
                finally:
                    self._runtime.leave_maintenance()

    async def clear(self, *, operator_ref: str) -> ClearResult:
        journal = self._require_clear_journal()
        async with self._runtime.exclusive_fence():
            if journal.get_open_operation() is not None:
                return self._blocked(operator_ref=operator_ref)
            operation: ClearOperation | None = None
            self._runtime.enter_maintenance()
            try:
                try:
                    await self._runtime.pause_claims()
                    manual_required = await self._run_maintenance_io(
                        self._store.has_manual_required_fence
                    )
                except BaseException as error:
                    self._runtime.leave_maintenance()
                    self._runtime.resume_claims()
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    return self._blocked(operator_ref=operator_ref)
                if manual_required:
                    self._runtime.leave_maintenance()
                    self._runtime.resume_claims()
                    return self._blocked(operator_ref=operator_ref)
                try:
                    meta = await self._run_maintenance_io(self._store.ensure_meta)
                    operation = await self._run_maintenance_io(
                        lambda: journal.start(
                            operator_ref=operator_ref,
                            pre_epoch=meta.epoch,
                            target_epoch=meta.epoch + 1,
                        )
                    )
                    await self._run_maintenance_io(self._store.begin_clear_fence)
                    operation = await self._prepare_clear(
                        operation,
                        claims_already_paused=True,
                    )
                    operation = await self._delete_clear_surfaces(operation)
                except BaseException as error:
                    current = await self._mark_clear_recovery(operation)
                    if current is not None and current.state == "completed":
                        self._runtime.leave_maintenance()
                        await self._finish_clear(current)
                    elif current is None:
                        self._runtime.leave_maintenance()
                        self._runtime.resume_claims()
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    return self._blocked(operator_ref=operator_ref)
            finally:
                self._runtime.leave_maintenance()
            if operation is None:
                raise RuntimeError("Memory clear operation did not start")
            return await self._finish_clear(operation)

    async def resume_clear(
        self,
        operation_id: str,
        *,
        operator_ref: str,
    ) -> ClearResult:
        journal = self._require_clear_journal()
        async with self._runtime.exclusive_fence():
            current = journal.get_open_operation()
            if current is None or current.operation_id != operation_id:
                return self._blocked(operator_ref=operator_ref)
            try:
                operation = await self._run_maintenance_io(
                    lambda: journal.claim_resume(
                        operation_id,
                        operator_ref=operator_ref,
                        expected_revision=current.revision,
                    )
                )
                await self._run_maintenance_io(self._store.begin_clear_fence)
                operation = await self._prepare_clear(operation)
                operation = await self._delete_clear_surfaces(operation)
            except BaseException as error:
                recovered = await self._mark_clear_recovery(operation_id=operation_id)
                if recovered is not None and recovered.state == "completed":
                    await self._finish_clear(recovered)
                if isinstance(error, asyncio.CancelledError):
                    raise
                return self._blocked(operator_ref=operator_ref)
            return await self._finish_clear(operation)

    async def abort_clear(
        self,
        operation_id: str,
        *,
        operator_ref: str,
    ) -> ClearResult:
        journal = self._require_clear_journal()
        manager = self._require_snapshot_manager()
        async with self._runtime.exclusive_fence():
            current = journal.get_open_operation()
            if current is None or current.operation_id != operation_id:
                return self._blocked(operator_ref=operator_ref)
            try:
                operation = await self._run_maintenance_io(
                    lambda: journal.claim_abort(
                        operation_id,
                        operator_ref=operator_ref,
                        expected_revision=current.revision,
                    )
                )
                await self._runtime.quiesce(False)
                digests = self._journal_surface_digests(operation.operation_id)
                if operation.manifest_sha256 is None or operation.snapshot_path is None:
                    raise RuntimeError("clear snapshot is not sealed")
                await self._run_maintenance_io(
                    lambda: manager.restore(
                        operation.operation_id,
                        expected_manifest_sha256=operation.manifest_sha256,
                        expected_surface_digests=digests,
                    )
                )
                await self._run_maintenance_io(self._store.release_clear_fence)
                for surface in journal.get_surfaces(operation.operation_id):
                    if surface.state == "restored":
                        continue
                    operation = await self._run_maintenance_io(
                        lambda: journal.record_surface_restored(
                            operation.operation_id,
                            surface.surface,
                            expected_revision=operation.revision,
                            execution_token=self._clear_execution_token(operation),
                        )
                    )
                operation = await self._run_maintenance_io(
                    lambda: journal.mark_aborted(
                        operation.operation_id,
                        expected_revision=operation.revision,
                        execution_token=self._clear_execution_token(operation),
                    )
                )
            except BaseException as error:
                recovered = await self._mark_clear_recovery(operation_id=operation_id)
                if recovered is not None and recovered.state == "aborted":
                    await self._finish_aborted_clear(recovered)
                if isinstance(error, asyncio.CancelledError):
                    raise
                return self._blocked(operator_ref=operator_ref)
            return await self._finish_aborted_clear(operation)

    async def _prepare_clear(
        self,
        operation: ClearOperation,
        *,
        claims_already_paused: bool = False,
    ) -> ClearOperation:
        journal = self._require_clear_journal()
        manager = self._require_snapshot_manager()
        await self._runtime.quiesce(claims_already_paused)
        if operation.state == "preparing":
            if operation.snapshot_path is None or operation.manifest_sha256 is None:
                if operation.resolution == "resume":
                    with journal.authorize_preparing_snapshot_discard(
                        operation.operation_id,
                        expected_revision=operation.revision,
                        execution_token=self._clear_execution_token(operation),
                    ) as permit:
                        await self._run_maintenance_io(
                            lambda: manager.discard_unrecorded(permit)
                        )
                    refreshed = journal.get_operation(operation.operation_id)
                    if refreshed is None:
                        raise RuntimeError("Memory clear operation disappeared")
                    operation = refreshed
                snapshot = await self._run_maintenance_io(
                    lambda: manager.create(operation.operation_id)
                )
                operation = await self._run_maintenance_io(
                    lambda: journal.record_snapshot(
                        operation.operation_id,
                        expected_revision=operation.revision,
                        execution_token=self._clear_execution_token(operation),
                        snapshot=snapshot,
                    )
                )
            else:
                await self._verify_clear_snapshot(operation)
            operation = await self._run_maintenance_io(
                lambda: journal.mark_prepared(
                    operation.operation_id,
                    expected_revision=operation.revision,
                    execution_token=self._clear_execution_token(operation),
                )
            )
        if operation.state == "prepared":
            await self._verify_clear_snapshot(operation)
        return operation

    async def _delete_clear_surfaces(self, operation: ClearOperation) -> ClearOperation:
        journal = self._require_clear_journal()
        if operation.state == "prepared":
            operation = await self._run_maintenance_io(
                lambda: journal.begin_deleting(
                    operation.operation_id,
                    expected_revision=operation.revision,
                    execution_token=self._clear_execution_token(operation),
                )
            )
        if operation.state != "deleting":
            raise RuntimeError("clear operation is not ready for deletion")
        await self._verify_clear_snapshot(operation)
        for surface in journal.get_surfaces(operation.operation_id):
            if surface.state == "deleted":
                continue
            await self._runtime.delete_surface(surface, operation.target_epoch)
            operation = await self._run_maintenance_io(
                lambda: journal.record_surface_deleted(
                    operation.operation_id,
                    surface.surface,
                    expected_revision=operation.revision,
                    execution_token=self._clear_execution_token(operation),
                )
            )
        return await self._run_maintenance_io(
            lambda: journal.mark_completed(
                operation.operation_id,
                expected_revision=operation.revision,
                execution_token=self._clear_execution_token(operation),
            )
        )

    async def _verify_clear_snapshot(self, operation: ClearOperation) -> MemorySnapshot:
        if operation.manifest_sha256 is None or operation.snapshot_path is None:
            raise RuntimeError("clear snapshot is not sealed")
        return await self._run_maintenance_io(
            lambda: self._require_snapshot_manager().verify(
                operation.operation_id,
                expected_manifest_sha256=operation.manifest_sha256,
                expected_surface_digests=self._journal_surface_digests(
                    operation.operation_id
                ),
            )
        )

    async def recover_boot(self) -> bool:
        """Converge an interrupted ordinary restore before activation."""

        journal = self._require_backup_restore_journal()
        operation = journal.get_open_operation()
        if operation is None:
            return True
        if operation.state != "recovery_needed":
            return False
        manager = self._require_backup_manager()
        self._backup_active = True
        self._runtime.enter_maintenance()
        try:
            async with self._runtime.boot_recovery_fence():
                await self._runtime.quiesce(False)
                operation = await self._run_maintenance_io(
                    lambda: journal.claim_retry(
                        operation.operation_id,
                        expected_revision=operation.revision,
                        actor_ref="system:boot",
                    )
                )
                await self._run_maintenance_io(
                    lambda: manager.restore(
                        operation.backup_id,
                        expected_manifest_sha256=operation.manifest_sha256,
                        expected_surface_digests=operation.digest_mapping(),
                    )
                )
                await self._run_maintenance_io(
                    lambda: journal.mark_completed(
                        operation.operation_id,
                        expected_revision=operation.revision,
                        execution_token=self._backup_restore_execution_token(operation),
                        actor_ref="system:boot",
                    )
                )
            return True
        except asyncio.CancelledError:
            await self._mark_backup_restore_recovery(operation)
            raise
        except Exception:
            await self._mark_backup_restore_recovery(operation)
            return False
        finally:
            self._backup_active = False
            self._runtime.leave_maintenance()

    async def _mark_backup_restore_recovery(
        self,
        operation: BackupRestoreOperation | None,
    ) -> BackupRestoreOperation | None:
        journal = self._backup_restore_journal
        if journal is None or operation is None:
            return None
        try:
            current = journal.get_operation(operation.operation_id)
            if current is None or current.state != "restoring":
                return current
            return await self._run_maintenance_io(
                lambda: journal.mark_recovery_needed(
                    current.operation_id,
                    expected_revision=current.revision,
                    execution_token=self._backup_restore_execution_token(current),
                )
            )
        except Exception:
            logger.exception("Memory backup restore failure could not be journaled")
            return None

    async def _mark_clear_recovery(
        self,
        operation: ClearOperation | None = None,
        *,
        operation_id: str | None = None,
    ) -> ClearOperation | None:
        journal = self._clear_journal
        if journal is None:
            return None

        def mark_recovery() -> ClearOperation | None:
            identifier = operation.operation_id if operation is not None else operation_id
            current = (
                journal.get_open_operation()
                if identifier is None
                else journal.get_operation(identifier)
            )
            if current is None:
                return None
            if current.state in {"preparing", "prepared", "deleting"}:
                return journal.mark_recovery_needed(
                    current.operation_id,
                    expected_revision=current.revision,
                    execution_token=self._clear_execution_token(current),
                )
            if current.state == "recovery_needed" and current.execution_token is not None:
                return journal.release_recovery_claim(
                    current.operation_id,
                    expected_revision=current.revision,
                    execution_token=self._clear_execution_token(current),
                )
            return current

        try:
            return await self._run_maintenance_io(mark_recovery)
        except Exception:
            logger.exception("Memory clear failure could not be journaled")
            return None

    async def _finish_clear(self, operation: ClearOperation) -> ClearResult:
        try:
            await self._run_maintenance_io(self._reconcile_terminal_clear_snapshots)
        finally:
            await self._runtime.resume()
        return ClearResult(
            status="completed",
            operation_id=operation.operation_id,
            epoch=operation.target_epoch,
        )

    async def _finish_aborted_clear(self, operation: ClearOperation) -> ClearResult:
        try:
            await self._run_maintenance_io(self._reconcile_terminal_clear_snapshots)
        finally:
            await self._runtime.resume()
        return ClearResult(
            status="aborted",
            operation_id=operation.operation_id,
            epoch=operation.pre_epoch,
        )

    def _blocked(self, *, operator_ref: str | None = None) -> ClearResult:
        return ClearResult(
            status="failed",
            error="memory_clear_failed",
            recovery=self.recovery(operator_ref=operator_ref),
        )

    def _reconcile_terminal_clear_snapshots(self) -> None:
        journal = self._require_clear_journal()
        manager = self._require_snapshot_manager()
        for permit in journal.terminal_snapshot_permits():
            try:
                manager.remove(permit)
            except Exception:
                logger.warning(
                    "Terminal Memory clear snapshot %s could not be removed",
                    permit.snapshot_id,
                    exc_info=True,
                )

    def ensure_housekeeping(self) -> None:
        """Schedule terminal GC before backup-stage reconciliation."""

        self._ensure_terminal_snapshot_gc()
        self._ensure_backup_stage_reconcile()

    def _ensure_terminal_snapshot_gc(self) -> None:
        task = self._terminal_snapshot_gc_task
        state = self._runtime.state()
        if (
            self._closing
            or state.artifact_installing
            or self._clear_journal is None
            or self._snapshot_manager is None
            or self._initialization_error is not None
            or (task is not None and not task.done())
        ):
            return
        task = asyncio.create_task(
            self._run_terminal_snapshot_gc(),
            name="memory-terminal-snapshot-gc",
        )
        self._terminal_snapshot_gc_task = task

        def clear_gc(completed: asyncio.Task[None]) -> None:
            if self._terminal_snapshot_gc_task is completed:
                self._terminal_snapshot_gc_task = None

        task.add_done_callback(clear_gc)

    def _ensure_backup_stage_reconcile(self) -> None:
        task = self._backup_stage_reconcile_task
        state = self._runtime.state()
        if (
            self._closing
            or state.artifact_installing
            or self._store is None
            or self._backup_manager is None
            or self._clear_journal is None
            or self._backup_restore_journal is None
            or self._initialization_error is not None
            or (task is not None and not task.done())
        ):
            return
        task = asyncio.create_task(
            self._run_backup_stage_reconcile(),
            name="memory-backup-stage-reconcile",
        )
        self._backup_stage_reconcile_task = task

        def clear_reconcile(completed: asyncio.Task[None]) -> None:
            if self._backup_stage_reconcile_task is completed:
                self._backup_stage_reconcile_task = None

        task.add_done_callback(clear_reconcile)

    async def _run_backup_stage_reconcile(self) -> None:
        terminal_gc = self._terminal_snapshot_gc_task
        if terminal_gc is not None and terminal_gc is not asyncio.current_task():
            await asyncio.shield(terminal_gc)
        journal = self._require_clear_journal()
        restore_journal = self._require_backup_restore_journal()
        manager = self._require_backup_manager()
        try:
            async with self._runtime.exclusive_fence():
                if self._runtime.state().artifact_installing:
                    return
                journal.assert_backup_allowed()
                restore_journal.assert_idle()
                await self._run_maintenance_io(
                    manager.reconcile_unpublished_backup_stages
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Unpublished Memory backup stages could not be reconciled",
                exc_info=True,
            )

    async def _run_terminal_snapshot_gc(self) -> None:
        journal = self._require_clear_journal()
        manager = self._require_snapshot_manager()
        try:
            permits = await self._run_maintenance_io(journal.terminal_snapshot_permits)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Terminal Memory clear snapshot permits could not be loaded",
                exc_info=True,
            )
            return
        for permit in permits:
            try:
                await self._run_maintenance_io(
                    lambda permit=permit: manager.remove(permit)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Terminal Memory clear snapshot %s could not be removed",
                    permit.snapshot_id,
                    exc_info=True,
                )

    async def close(self) -> None:
        self._closing = True
        caller_cancellation: asyncio.CancelledError | None = None
        for attribute in (
            "_backup_stage_reconcile_task",
            "_terminal_snapshot_gc_task",
        ):
            try:
                await self._stop_task(attribute)
            except asyncio.CancelledError as error:
                caller_cancellation = caller_cancellation or error
        if caller_cancellation is not None:
            raise caller_cancellation

    async def _stop_task(self, attribute: str) -> None:
        task = getattr(self, attribute)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        settlement = asyncio.gather(task, return_exceptions=True)
        caller_cancellation: asyncio.CancelledError | None = None
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError as error:
                caller_cancellation = caller_cancellation or error
        try:
            result = settlement.result()[0]
        finally:
            if getattr(self, attribute) is task:
                setattr(self, attribute, None)
        if isinstance(result, BaseException) and not isinstance(
            result,
            asyncio.CancelledError,
        ):
            raise result
        if caller_cancellation is not None:
            raise caller_cancellation

    def _open_backup_restore_operation(self) -> BackupRestoreOperation | None:
        journal = self._backup_restore_journal
        if journal is None:
            return None
        try:
            return journal.get_open_operation()
        except Exception:
            return None

    def _open_clear_operation(self) -> ClearOperation | None:
        journal = self._clear_journal
        if journal is None:
            return None
        try:
            return journal.get_open_operation()
        except Exception:
            return None

    def _journal_surface_digests(self, operation_id: str) -> dict[str, str | None]:
        journal = self._require_clear_journal()
        return {
            surface.relative_path: surface.snapshot_digest
            for surface in journal.get_surfaces(operation_id)
        }

    def _require_clear_journal(self) -> MemoryClearJournal:
        if self._clear_journal is None or self._initialization_error is not None:
            raise MemoryStoreUnavailableError("Memory clear journal is unavailable")
        return self._clear_journal

    def _require_backup_restore_journal(self) -> MemoryBackupRestoreJournal:
        if (
            self._backup_restore_journal is None
            or self._initialization_error is not None
        ):
            raise MemoryStoreUnavailableError(
                "Memory backup restore journal is unavailable"
            )
        return self._backup_restore_journal

    def _require_snapshot_manager(self) -> MemorySnapshotManager:
        if self._snapshot_manager is None or self._initialization_error is not None:
            raise MemoryStoreUnavailableError("Memory snapshot manager is unavailable")
        return self._snapshot_manager

    def _require_backup_manager(self) -> MemorySnapshotManager:
        if self._backup_manager is None or self._initialization_error is not None:
            raise MemoryStoreUnavailableError("Memory backup manager is unavailable")
        return self._backup_manager

    @staticmethod
    async def _run_maintenance_io(
        operation: Callable[[], _MaintenanceIOResult],
    ) -> _MaintenanceIOResult:
        return await run_blocking(operation)

    @staticmethod
    def _backup_restore_execution_token(
        operation: BackupRestoreOperation,
    ) -> str:
        if operation.execution_token is None:
            raise RuntimeError("Memory backup restore execution claim is missing")
        return operation.execution_token

    @staticmethod
    def _clear_execution_token(operation: ClearOperation) -> str:
        if operation.execution_token is None:
            raise RuntimeError("Memory clear operation is not claimed")
        return operation.execution_token
