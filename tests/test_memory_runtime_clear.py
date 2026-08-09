from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import pytest

from config.v2_config import MemoryConfig
from core.memory.runtime import MemoryRuntime
from core.memory.snapshot import MemorySnapshotManager
from core.memory.types import CaptureAccepted, CaptureRequest, CaptureSkipped


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "p-22222222222222222222222222222222"


def _enqueue(runtime: MemoryRuntime, source: str) -> None:
    result = runtime._store.enqueue_request(
        source_message_id=source,
        session_id="session",
        principal_id=PRINCIPAL,
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="private payload",
        occurred_at_ms=1,
        max_provider_timestamp_ms=100,
    )
    assert result.outcome == "accepted"


async def test_interrupted_queue_delete_requires_explicit_resume_at_target_epoch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "resume-source")
    pre_epoch = runtime._store.ensure_meta().epoch
    journal = runtime._clear_journal
    assert journal is not None
    original_record = journal.record_surface_deleted
    interrupted = False

    def interrupt_queue_receipt(operation_id, surface, **kwargs):
        nonlocal interrupted
        if surface == "queue" and not interrupted:
            interrupted = True
            raise OSError("injected receipt failure")
        return original_record(operation_id, surface, **kwargs)

    monkeypatch.setattr(journal, "record_surface_deleted", interrupt_queue_receipt)

    failed = await runtime.clear(operator_ref="user:owner")

    assert failed["status"] == "failed"
    recovery = journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "deleting"
    assert failed["recovery"]["can_abort"] is True
    assert runtime._store.ensure_meta().epoch == pre_epoch + 1
    assert runtime._store.list_queue_rows() == ()

    monkeypatch.setattr(journal, "record_surface_deleted", original_record)
    completed = await runtime.resume_clear(
        recovery.operation_id,
        operator_ref="user:owner",
    )

    assert completed["status"] == "completed"
    assert completed["epoch"] == pre_epoch + 1
    assert runtime._store.ensure_meta().epoch == pre_epoch + 1
    assert journal.get_open_operation() is None
    await runtime.close()


async def test_recovery_payload_disables_abort_before_initial_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    journal = runtime._clear_journal
    assert journal is not None
    operation = journal.start(
        operation_id="incomplete-snapshot",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    recovery = journal.mark_boot_recovery_needed()

    assert recovery is not None
    maintenance = await runtime.maintenance_payload()
    assert maintenance["clear_recovery"] == {
        "state": "recovery_needed",
        "operation_id": operation.operation_id,
        "occurred_at": recovery.updated_at,
        "error_code": "memory_clear_failed",
        "can_resume": True,
        "can_abort": False,
    }
    await runtime.close()


async def test_cancelled_clear_start_and_resume_claim_settle_before_recovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    journal = runtime._clear_journal
    assert journal is not None
    start_entered = threading.Event()
    start_release = threading.Event()
    original_start = journal.start

    def blocking_start(**kwargs):
        operation = original_start(**kwargs)
        start_entered.set()
        assert start_release.wait(2)
        return operation

    monkeypatch.setattr(journal, "start", blocking_start)
    clearing = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    assert await asyncio.to_thread(start_entered.wait, 1)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    start_release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    recovery = journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "preparing"
    assert recovery.execution_token is None
    assert runtime._store.ensure_meta().clear_in_progress is False

    claim_entered = threading.Event()
    claim_release = threading.Event()
    original_claim_resume = journal.claim_resume

    def blocking_claim_resume(operation_id: str, **kwargs):
        operation = original_claim_resume(operation_id, **kwargs)
        claim_entered.set()
        assert claim_release.wait(2)
        return operation

    monkeypatch.setattr(journal, "claim_resume", blocking_claim_resume)
    resuming = asyncio.create_task(
        runtime.resume_clear(recovery.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(claim_entered.wait, 1)

    resuming.cancel()
    await asyncio.sleep(0)

    assert resuming.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    claim_release.set()
    with pytest.raises(asyncio.CancelledError):
        await resuming

    claimed_recovery = journal.get_open_operation()
    assert claimed_recovery is not None
    assert claimed_recovery.state == "recovery_needed"
    assert claimed_recovery.recovery_from_state == "preparing"
    assert claimed_recovery.resolution == "resume"
    assert claimed_recovery.execution_token is None

    monkeypatch.setattr(journal, "claim_resume", original_claim_resume)
    completed = await runtime.resume_clear(
        claimed_recovery.operation_id,
        operator_ref="user:owner",
    )
    assert completed["status"] == "completed"
    assert journal.get_open_operation() is None
    await runtime.close()


async def test_cancelled_completed_clear_resumes_runtime_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    journal = runtime._clear_journal
    assert journal is not None
    completed_entered = threading.Event()
    completed_release = threading.Event()
    completed_ids: list[str] = []
    original_mark_completed = journal.mark_completed

    def blocking_mark_completed(operation_id: str, **kwargs):
        operation = original_mark_completed(operation_id, **kwargs)
        completed_ids.append(operation.operation_id)
        completed_entered.set()
        assert completed_release.wait(2)
        return operation

    resume_calls = 0
    original_resume = runtime._resume_after_clear

    async def counted_resume() -> None:
        nonlocal resume_calls
        resume_calls += 1
        await original_resume()

    monkeypatch.setattr(journal, "mark_completed", blocking_mark_completed)
    monkeypatch.setattr(runtime, "_resume_after_clear", counted_resume)
    clearing = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    assert await asyncio.to_thread(completed_entered.wait, 1)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    completed_release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert completed_ids
    completed = journal.get_operation(completed_ids[0])
    assert completed is not None
    assert completed.state == "completed"
    assert journal.get_open_operation() is None
    assert resume_calls == 1
    await runtime.close()


async def test_cancelled_post_terminal_resume_holds_lifecycle_fences(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=True), effective_home=tmp_path)
    resume_entered = asyncio.Event()
    resume_release = asyncio.Event()

    async def blocking_reconcile(*_args, **_kwargs):
        resume_entered.set()
        await resume_release.wait()
        runtime.module._worker.resume_claims()
        return {"ok": True, "state": "running"}

    monkeypatch.setattr(runtime, "_reconcile_locked", blocking_reconcile)
    clearing = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    await asyncio.wait_for(resume_entered.wait(), timeout=1)

    assert runtime._clear_journal.get_open_operation() is None
    assert runtime.module._worker._claims_paused is True
    assert runtime.module._worker._coordinator._paused is True
    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    resume_release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert runtime.module._worker._claims_paused is False
    assert runtime.module._worker._coordinator._paused is False
    assert runtime._reconcile_lock.locked() is False
    assert runtime.module._lifecycle_lock.locked() is False
    assert runtime.module._root_lifecycle_lock().locked() is False
    await runtime.close()


async def test_abort_restores_all_surfaces_after_destructive_work(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "abort-source")
    pre_meta = runtime._store.ensure_meta()
    original_delete = runtime._delete_clear_surface

    async def interrupt_provider(surface, *, target_epoch):
        if surface.surface == "provider":
            raise OSError("injected provider delete failure")
        await original_delete(surface, target_epoch=target_epoch)

    monkeypatch.setattr(runtime, "_delete_clear_surface", interrupt_provider)
    failed = await runtime.clear(operator_ref="user:owner")
    recovery = runtime._clear_journal.get_open_operation()

    assert failed["status"] == "failed"
    assert recovery is not None and recovery.destructive_started
    assert runtime._store.list_queue_rows() == ()

    monkeypatch.setattr(runtime, "_delete_clear_surface", original_delete)
    aborted = await runtime.abort_clear(
        recovery.operation_id,
        operator_ref="user:owner",
    )

    assert aborted["status"] == "aborted"
    restored_meta = runtime._store.ensure_meta()
    assert restored_meta.epoch == pre_meta.epoch
    assert restored_meta.clear_in_progress is False
    assert len(runtime._store.list_queue_rows()) == 1
    assert runtime._clear_journal.get_open_operation() is None
    operation = runtime._clear_journal.get_operation(recovery.operation_id)
    assert operation is not None and operation.state == "aborted"
    assert runtime._snapshot_manager is not None
    assert not runtime._snapshot_manager.snapshot_path(recovery.operation_id).exists()
    await runtime.close()


async def test_completed_clear_snapshot_removal_retries_on_reconcile_and_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    manager = runtime._snapshot_manager
    assert manager is not None
    original_remove = manager.remove
    removal_attempts: list[str] = []

    def fail_removal(permit) -> None:
        if manager.snapshot_path(permit.snapshot_id).exists():
            removal_attempts.append(permit.snapshot_id)
            raise OSError("injected snapshot removal failure")
        original_remove(permit)

    monkeypatch.setattr(manager, "remove", fail_removal)
    completed = await runtime.clear(operator_ref="user:owner")
    snapshot_path = manager.snapshot_path(completed["operation_id"])

    assert completed["status"] == "completed"
    assert removal_attempts == [completed["operation_id"]]
    assert snapshot_path.is_dir()

    monkeypatch.setattr(manager, "remove", original_remove)
    assert await runtime.reconcile(MemoryConfig()) == {"ok": True, "state": "disabled"}
    assert not snapshot_path.exists()

    monkeypatch.setattr(manager, "remove", fail_removal)
    completed_before_restart = await runtime.clear(operator_ref="user:owner")
    restart_snapshot_path = manager.snapshot_path(completed_before_restart["operation_id"])
    assert restart_snapshot_path.is_dir()
    await runtime.close()

    restarted = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    assert not restart_snapshot_path.exists()
    assert restarted._clear_journal is not None
    assert restarted._clear_journal.get_open_operation() is None
    await restarted.close()


async def test_aborted_clear_snapshot_removal_retries_on_reconcile_and_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    journal = runtime._clear_journal
    manager = runtime._snapshot_manager
    assert journal is not None
    assert manager is not None
    original_delete = runtime._delete_clear_surface
    original_remove = manager.remove
    removal_attempts: list[str] = []

    async def interrupt_provider(surface, *, target_epoch):
        if surface.surface == "provider":
            raise OSError("injected provider delete failure")
        await original_delete(surface, target_epoch=target_epoch)

    def fail_removal(permit) -> None:
        if manager.snapshot_path(permit.snapshot_id).exists():
            removal_attempts.append(permit.snapshot_id)
            raise OSError("injected snapshot removal failure")
        original_remove(permit)

    async def abort_after_interrupted_clear() -> dict:
        monkeypatch.setattr(runtime, "_delete_clear_surface", interrupt_provider)
        failed = await runtime.clear(operator_ref="user:owner")
        recovery = journal.get_open_operation()
        assert failed["status"] == "failed"
        assert recovery is not None
        monkeypatch.setattr(runtime, "_delete_clear_surface", original_delete)
        return await runtime.abort_clear(
            recovery.operation_id,
            operator_ref="user:owner",
        )

    monkeypatch.setattr(manager, "remove", fail_removal)
    aborted = await abort_after_interrupted_clear()
    snapshot_path = manager.snapshot_path(aborted["operation_id"])

    assert aborted["status"] == "aborted"
    assert removal_attempts == [aborted["operation_id"]]
    assert snapshot_path.is_dir()
    terminal = journal.get_operation(aborted["operation_id"])
    assert terminal is not None and terminal.state == "aborted"

    monkeypatch.setattr(manager, "remove", original_remove)
    assert await runtime.reconcile(MemoryConfig()) == {"ok": True, "state": "disabled"}
    assert not snapshot_path.exists()

    monkeypatch.setattr(manager, "remove", fail_removal)
    aborted_before_restart = await abort_after_interrupted_clear()
    restart_snapshot_path = manager.snapshot_path(aborted_before_restart["operation_id"])
    assert restart_snapshot_path.is_dir()
    await runtime.close()

    restarted = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    assert not restart_snapshot_path.exists()
    assert restarted._clear_journal is not None
    assert restarted._clear_journal.get_open_operation() is None
    await restarted.close()


async def test_cancelled_clear_waits_for_snapshot_creation_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_create = MemorySnapshotManager.create

    def blocking_create(manager: MemorySnapshotManager, snapshot_id: str | None = None):
        if manager is runtime._snapshot_manager:
            started.set()
            assert release.wait(2)
        try:
            return original_create(manager, snapshot_id)
        finally:
            if manager is runtime._snapshot_manager:
                finished.set()

    monkeypatch.setattr(MemorySnapshotManager, "create", blocking_create)
    clearing = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    assert await asyncio.to_thread(started.wait, 1)
    operation = runtime._clear_journal.get_open_operation()
    assert operation is not None

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    resuming = asyncio.create_task(
        runtime.resume_clear(operation.operation_id, operator_ref="user:owner")
    )
    await asyncio.sleep(0)
    assert resuming.done() is False
    resuming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resuming

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert finished.is_set()
    recovery = runtime._clear_journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "preparing"

    discard_started = threading.Event()
    discard_release = threading.Event()
    discard_finished = threading.Event()
    original_discard = MemorySnapshotManager.discard_unrecorded

    def blocking_discard(manager: MemorySnapshotManager, permit) -> None:
        if manager is runtime._snapshot_manager:
            discard_started.set()
            assert discard_release.wait(2)
        try:
            original_discard(manager, permit)
        finally:
            if manager is runtime._snapshot_manager:
                discard_finished.set()

    monkeypatch.setattr(MemorySnapshotManager, "discard_unrecorded", blocking_discard)
    resuming = asyncio.create_task(
        runtime.resume_clear(recovery.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(discard_started.wait, 1)
    resuming.cancel()
    await asyncio.sleep(0)
    assert resuming.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()

    discard_release.set()
    with pytest.raises(asyncio.CancelledError):
        await resuming

    assert discard_finished.is_set()
    pending = runtime._clear_journal.get_open_operation()
    assert pending is not None
    assert pending.state == "recovery_needed"
    assert pending.resolution == "resume"

    monkeypatch.setattr(MemorySnapshotManager, "discard_unrecorded", original_discard)
    completed = await runtime.resume_clear(pending.operation_id, operator_ref="user:owner")
    assert completed["status"] == "completed"
    await runtime.close()


async def test_cancelled_clear_waits_for_snapshot_verification_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_verify = MemorySnapshotManager.verify

    def blocking_verify(manager: MemorySnapshotManager, snapshot_id: str, **kwargs):
        if manager is runtime._snapshot_manager:
            started.set()
            assert release.wait(2)
        try:
            return original_verify(manager, snapshot_id, **kwargs)
        finally:
            if manager is runtime._snapshot_manager:
                finished.set()

    monkeypatch.setattr(MemorySnapshotManager, "verify", blocking_verify)
    clearing = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    assert await asyncio.to_thread(started.wait, 1)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert finished.is_set()
    recovery = runtime._clear_journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "prepared"

    monkeypatch.setattr(MemorySnapshotManager, "verify", original_verify)
    completed = await runtime.resume_clear(recovery.operation_id, operator_ref="user:owner")
    assert completed["status"] == "completed"
    await runtime.close()


async def test_cancelled_clear_waits_for_provider_delete_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    runtime.module._ensure_owned_provider_root(runtime._store.ensure_meta())
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_recreate = runtime.module._recreate_owned_provider_root

    def blocking_recreate(meta) -> None:
        started.set()
        assert release.wait(2)
        try:
            original_recreate(meta)
        finally:
            finished.set()

    monkeypatch.setattr(
        runtime.module,
        "_recreate_owned_provider_root",
        blocking_recreate,
    )
    clearing = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    assert await asyncio.to_thread(started.wait, 1)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert finished.is_set()
    recovery = runtime._clear_journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "deleting"

    monkeypatch.setattr(
        runtime.module,
        "_recreate_owned_provider_root",
        original_recreate,
    )
    completed = await runtime.resume_clear(
        recovery.operation_id,
        operator_ref="user:owner",
    )
    assert completed["status"] == "completed"
    await runtime.close()


async def test_cancelled_abort_waits_for_restore_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "cancel-abort-source")
    original_delete = runtime._delete_clear_surface

    async def interrupt_provider(surface, *, target_epoch):
        if surface.surface == "provider":
            raise OSError("injected provider delete failure")
        await original_delete(surface, target_epoch=target_epoch)

    monkeypatch.setattr(runtime, "_delete_clear_surface", interrupt_provider)
    failed = await runtime.clear(operator_ref="user:owner")
    recovery = runtime._clear_journal.get_open_operation()
    assert failed["status"] == "failed"
    assert recovery is not None
    monkeypatch.setattr(runtime, "_delete_clear_surface", original_delete)

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_restore = MemorySnapshotManager.restore

    def blocking_restore(manager: MemorySnapshotManager, snapshot_id: str, **kwargs):
        if manager is runtime._snapshot_manager:
            started.set()
            assert release.wait(2)
        try:
            return original_restore(manager, snapshot_id, **kwargs)
        finally:
            if manager is runtime._snapshot_manager:
                finished.set()

    monkeypatch.setattr(MemorySnapshotManager, "restore", blocking_restore)
    aborting = asyncio.create_task(
        runtime.abort_clear(recovery.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(started.wait, 1)

    aborting.cancel()
    await asyncio.sleep(0)

    assert aborting.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await aborting

    assert finished.is_set()
    pending = runtime._clear_journal.get_open_operation()
    assert pending is not None
    assert pending.state == "recovery_needed"
    assert pending.resolution == "abort"
    assert pending.execution_token is None
    assert runtime._clear_recovery_payload()["can_resume"] is False

    monkeypatch.setattr(MemorySnapshotManager, "restore", original_restore)
    journal = runtime._clear_journal
    terminal_entered = threading.Event()
    terminal_release = threading.Event()
    original_mark_aborted = journal.mark_aborted

    def blocking_mark_aborted(operation_id: str, **kwargs):
        operation = original_mark_aborted(operation_id, **kwargs)
        terminal_entered.set()
        assert terminal_release.wait(2)
        return operation

    resume_calls = 0
    original_resume = runtime._resume_after_clear

    async def counted_resume() -> None:
        nonlocal resume_calls
        resume_calls += 1
        await original_resume()

    monkeypatch.setattr(journal, "mark_aborted", blocking_mark_aborted)
    monkeypatch.setattr(runtime, "_resume_after_clear", counted_resume)
    aborting = asyncio.create_task(
        runtime.abort_clear(pending.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(terminal_entered.wait, 1)

    aborting.cancel()
    await asyncio.sleep(0)

    assert aborting.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    terminal_release.set()
    with pytest.raises(asyncio.CancelledError):
        await aborting

    aborted = journal.get_operation(pending.operation_id)
    assert aborted is not None
    assert aborted.state == "aborted"
    assert runtime._clear_journal.get_open_operation() is None
    assert resume_calls == 1
    await runtime.close()


async def test_runtime_backup_fences_capture_and_clear_for_the_full_copy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=True), effective_home=tmp_path)
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    original_create = MemorySnapshotManager.create
    original_enqueue = runtime._store.enqueue_request

    def blocking_enqueue(**kwargs):
        enqueue_entered.set()
        assert release_enqueue.wait(2)
        return original_enqueue(**kwargs)

    def blocking_create(manager: MemorySnapshotManager, snapshot_id: str | None = None):
        if manager.snapshot_root == tmp_path / "state" / "memory" / "backups":
            entered.set()
            assert release.wait(2)
        return original_create(manager, snapshot_id)

    async def resume_without_sidecar() -> None:
        runtime.module._worker.resume_claims()

    monkeypatch.setattr(MemorySnapshotManager, "create", blocking_create)
    monkeypatch.setattr(runtime._store, "enqueue_request", blocking_enqueue)
    monkeypatch.setattr(runtime, "_resume_after_clear", resume_without_sidecar)

    capturing = asyncio.create_task(
        runtime.module.capture(
            CaptureRequest(
                source_message_id="before-backup",
                session_id="session",
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provenance="user_input",
                text="must be included",
                occurred_at_ms=1,
            )
        )
    )
    assert await asyncio.to_thread(enqueue_entered.wait, 1)
    creating = asyncio.create_task(runtime.create_backup("runtime-fence"))
    await asyncio.sleep(0)
    assert entered.is_set() is False
    release_enqueue.set()
    assert await capturing == CaptureAccepted()
    assert await asyncio.to_thread(entered.wait, 1)
    assert runtime._maintenance_open() is True
    receipt = await runtime.module.capture(
        CaptureRequest(
            source_message_id="blocked-capture",
            session_id="session",
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provenance="user_input",
            text="must wait for backup",
            occurred_at_ms=2,
        )
    )
    assert receipt == CaptureSkipped(reason="memory_clear_failed")

    clearing = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    await asyncio.sleep(0)
    assert clearing.done() is False
    assert runtime._clear_journal.get_open_operation() is None

    clearing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await clearing
    creating.cancel()
    await asyncio.sleep(0)
    assert creating.done() is False
    assert runtime._maintenance_open() is True
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert (tmp_path / "state" / "memory" / "backups" / "runtime-fence").is_dir()
    assert runtime._maintenance_open() is False
    await runtime.close()


async def test_runtime_backup_restore_round_trips_queue_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "backup-source")

    backup = await runtime.create_backup("runtime-round-trip")
    meta = runtime._store.ensure_meta()
    runtime._store.reset_for_clear(target_epoch=meta.epoch + 1)
    assert runtime._store.list_queue_rows() == ()

    restored = await runtime.restore_backup(
        backup.snapshot_id,
        expected_manifest_sha256=backup.manifest_sha256,
        expected_surface_digests=backup.surface_digests(),
    )

    assert restored == backup
    rows = runtime._store.list_queue_rows()
    assert len(rows) == 1
    assert rows[0].source_message_digest
    await runtime.close()
