from __future__ import annotations

import hashlib
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.memory.clear_journal import (
    ClearBackupBlocked,
    ClearOperationCASMismatch,
    ClearOperationConflict,
    ClearTransitionError,
    MemoryClearJournal,
    MemoryClearJournalError,
)
from core.memory.snapshot import MemorySnapshot, SnapshotSurfaceReceipt


def _journal(tmp_path: Path) -> MemoryClearJournal:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    return MemoryClearJournal(home)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot_receipt(journal: MemoryClearJournal, operation_id: str) -> MemorySnapshot:
    return MemorySnapshot(
        snapshot_id=operation_id,
        relative_path=f"state/memory/clear-snapshots/{operation_id}",
        manifest_sha256=_digest("manifest"),
        entries=(),
        surface_receipts=tuple(
            SnapshotSurfaceReceipt(
                path=surface.relative_path,
                present=True,
                pre_clear_digest=_digest(f"pre:{surface.name}"),
                snapshot_digest=_digest(f"snapshot:{surface.name}"),
            )
            for surface in journal.surfaces
        ),
    )


def _record_all_snapshots(journal: MemoryClearJournal, operation):
    assert operation.execution_token is not None
    operation = journal.record_snapshot(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
        snapshot=_snapshot_receipt(journal, operation.operation_id),
    )
    assert operation.execution_token is not None
    return journal.mark_prepared(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )


def test_clear_journal_happy_path_is_a_pure_durable_state_machine(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    sentinel = journal.database_path.parent.parent.parent / "memory/everos-root/sentinel"
    sentinel.parent.mkdir(parents=True, mode=0o700)
    sentinel.parent.chmod(0o700)
    sentinel.write_text("must remain")
    sentinel.chmod(0o600)

    operation = journal.start(
        operation_id="clear-01",
        operator_ref="user:operator",
        pre_epoch=7,
        target_epoch=8,
    )

    assert operation.state == "preparing"
    assert operation.revision == 1
    assert operation.execution_token is not None
    assert [surface.surface for surface in journal.get_surfaces(operation.operation_id)] == [
        "queue",
        "provider",
        "call_log",
        "attachments",
    ]
    with pytest.raises(ClearBackupBlocked):
        journal.assert_backup_allowed()
    with pytest.raises(ClearOperationConflict):
        journal.start(
            operation_id="clear-02",
            operator_ref="user:operator",
            pre_epoch=7,
            target_epoch=8,
        )

    operation = _record_all_snapshots(journal, operation)
    assert operation.state == "prepared"
    assert operation.snapshot_path == "state/memory/clear-snapshots/clear-01"
    assert operation.manifest_sha256 == _digest("manifest")
    assert operation.execution_token is not None
    operation = journal.begin_deleting(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    assert operation.state == "deleting"
    assert operation.destructive_started
    for surface in journal.surfaces:
        assert operation.execution_token is not None
        operation = journal.record_surface_deleted(
            operation.operation_id,
            surface.name,
            expected_revision=operation.revision,
            execution_token=operation.execution_token,
        )
    assert operation.execution_token is not None
    operation = journal.mark_completed(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )

    assert operation.state == "completed"
    assert operation.terminal_at is not None
    assert operation.execution_token is None
    assert journal.get_open_operation() is None
    journal.assert_backup_allowed()
    permit = journal.completed_snapshot_permit(operation.operation_id)
    assert permit.snapshot_id == operation.operation_id
    assert permit.manifest_sha256 == _digest("manifest")
    assert len(permit.surface_digests) == 4
    assert sentinel.read_text() == "must remain"
    assert [event.event for event in journal.get_events(operation.operation_id)] == [
        "started",
        "surface_snapshotted",
        "surface_snapshotted",
        "surface_snapshotted",
        "surface_snapshotted",
        "prepared",
        "deleting_started",
        "surface_deleted",
        "surface_deleted",
        "surface_deleted",
        "surface_deleted",
        "completed",
    ]


def test_boot_recovery_is_explicit_and_resume_preserves_interrupted_stage(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    operation = journal.start(
        operation_id="resume-me",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    stale_token = operation.execution_token

    reopened = MemoryClearJournal(journal.database_path.parent.parent.parent)
    unchanged = reopened.get_open_operation()
    assert unchanged is not None
    assert unchanged.state == "preparing"
    assert unchanged.revision == operation.revision

    recovery = reopened.mark_boot_recovery_needed()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "preparing"
    assert recovery.execution_token is None
    with pytest.raises(ClearOperationCASMismatch):
        reopened.record_snapshot(
            recovery.operation_id,
            expected_revision=operation.revision,
            execution_token=stale_token or "",
            snapshot=_snapshot_receipt(reopened, recovery.operation_id),
        )

    resumed = reopened.claim_resume(
        recovery.operation_id,
        operator_ref="user:owner",
        expected_revision=recovery.revision,
    )
    assert resumed.state == "preparing"
    assert resumed.resolution == "resume"
    assert resumed.execution_token is not None
    assert resumed.execution_token != stale_token

    second_recovery = reopened.mark_boot_recovery_needed()
    assert second_recovery is not None
    assert second_recovery.recovery_from_state == "preparing"
    assert second_recovery.resolution == "resume"
    with pytest.raises(ClearTransitionError):
        reopened.claim_abort(
            second_recovery.operation_id,
            operator_ref="user:owner",
            expected_revision=second_recovery.revision,
        )


def test_in_process_failure_marks_exact_stage_with_cas(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    operation = journal.start(
        operation_id="failed-in-process",
        operator_ref="user:owner",
        pre_epoch=4,
        target_epoch=5,
    )
    operation = _record_all_snapshots(journal, operation)
    assert operation.execution_token is not None

    recovery = journal.mark_recovery_needed(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )

    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "prepared"
    assert recovery.execution_token is None
    assert recovery.closed_error == "memory_clear_failed"
    assert journal.get_events(operation.operation_id)[-1].event == "recovery_needed"


def test_failed_recovery_claim_is_released_without_changing_direction(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    operation = journal.start(
        operation_id="retry-abort",
        operator_ref="user:owner",
        pre_epoch=4,
        target_epoch=5,
    )
    operation = _record_all_snapshots(journal, operation)
    recovery = journal.mark_boot_recovery_needed()
    assert recovery is not None
    assert journal.can_abort(recovery.operation_id) is True
    claimed = journal.claim_abort(
        operation.operation_id,
        operator_ref="user:owner",
        expected_revision=recovery.revision,
    )
    assert journal.can_abort(claimed.operation_id) is False
    assert claimed.execution_token is not None

    released = journal.release_recovery_claim(
        claimed.operation_id,
        expected_revision=claimed.revision,
        execution_token=claimed.execution_token,
    )

    assert released.state == "recovery_needed"
    assert released.resolution == "abort"
    assert released.execution_token is None
    assert released.closed_error == "memory_clear_failed"
    assert journal.can_abort(released.operation_id) is True
    assert journal.get_events(operation.operation_id)[-1].event == "recovery_claim_failed"
    reclaimed = journal.claim_abort(
        released.operation_id,
        operator_ref="user:owner",
        expected_revision=released.revision,
    )
    assert reclaimed.resolution == "abort"
    assert reclaimed.execution_token is not None


def test_abort_refuses_an_incomplete_snapshot(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    operation = journal.start(
        operation_id="no-snapshot-abort",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    recovery = journal.mark_boot_recovery_needed()
    assert recovery is not None
    assert journal.can_abort(recovery.operation_id) is False

    with pytest.raises(ClearTransitionError):
        journal.claim_abort(
            operation.operation_id,
            operator_ref="user:owner",
            expected_revision=recovery.revision,
        )

    still_open = journal.get_open_operation()
    assert still_open is not None
    assert still_open.state == "recovery_needed"
    assert still_open.resolution is None


def test_present_snapshot_receipt_requires_both_digests() -> None:
    with pytest.raises(ValueError):
        SnapshotSurfaceReceipt(
            path="state/memory/memory.sqlite",
            present=True,
            pre_clear_digest=None,
            snapshot_digest=None,
        )


def test_abort_direction_survives_a_second_crash_and_requires_restore(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    operation = journal.start(
        operation_id="abort-me",
        operator_ref="user:owner",
        pre_epoch=10,
        target_epoch=11,
    )
    operation = _record_all_snapshots(journal, operation)
    assert operation.execution_token is not None
    operation = journal.begin_deleting(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    assert operation.execution_token is not None
    operation = journal.record_surface_deleted(
        operation.operation_id,
        "queue",
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    recovery = journal.mark_boot_recovery_needed()
    assert recovery is not None and recovery.recovery_from_state == "deleting"
    abort = journal.claim_abort(
        operation.operation_id,
        operator_ref="user:owner",
        expected_revision=recovery.revision,
    )
    with pytest.raises(ClearTransitionError):
        assert abort.execution_token is not None
        journal.mark_aborted(
            abort.operation_id,
            expected_revision=abort.revision,
            execution_token=abort.execution_token,
        )

    assert abort.execution_token is not None
    abort = journal.record_surface_restored(
        abort.operation_id,
        "queue",
        expected_revision=abort.revision,
        execution_token=abort.execution_token,
    )
    interrupted_abort = journal.mark_boot_recovery_needed()
    assert interrupted_abort is not None
    assert interrupted_abort.resolution == "abort"
    with pytest.raises(ClearTransitionError):
        journal.claim_resume(
            interrupted_abort.operation_id,
            operator_ref="user:owner",
            expected_revision=interrupted_abort.revision,
        )

    abort = journal.claim_abort(
        interrupted_abort.operation_id,
        operator_ref="user:owner",
        expected_revision=interrupted_abort.revision,
    )
    for name in ("provider", "call_log", "attachments"):
        assert abort.execution_token is not None
        abort = journal.record_surface_restored(
            abort.operation_id,
            name,
            expected_revision=abort.revision,
            execution_token=abort.execution_token,
        )
    assert abort.execution_token is not None
    aborted = journal.mark_aborted(
        abort.operation_id,
        expected_revision=abort.revision,
        execution_token=abort.execution_token,
        closed_error="memory_clear_failed",
    )

    assert aborted.state == "aborted"
    assert aborted.resolution == "abort"
    assert aborted.closed_error == "memory_clear_failed"
    journal.assert_backup_allowed()


def test_revision_and_execution_token_cas_reject_stale_coordinators(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    operation = journal.start(
        operation_id="cas",
        operator_ref="user:owner",
        pre_epoch=3,
        target_epoch=4,
    )
    assert operation.execution_token is not None
    advanced = journal.record_snapshot(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
        snapshot=_snapshot_receipt(journal, operation.operation_id),
    )

    with pytest.raises(ClearOperationCASMismatch):
        journal.mark_prepared(
            operation.operation_id,
            expected_revision=operation.revision,
            execution_token=operation.execution_token,
        )
    assert advanced.revision == operation.revision + 1
    assert all(
        surface.state == "snapshotted" for surface in journal.get_surfaces(operation.operation_id)
    )


def test_concurrent_starts_create_exactly_one_open_operation(tmp_path: Path) -> None:
    journal = _journal(tmp_path)

    def start(index: int):
        try:
            return journal.start(
                operation_id=f"concurrent-{index}",
                operator_ref="user:owner",
                pre_epoch=1,
                target_epoch=2,
            )
        except ClearOperationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, range(2)))

    winners = [operation for operation in results if operation is not None]
    assert len(winners) == 1
    assert journal.get_open_operation() == winners[0]


def test_terminal_headers_and_events_are_immutable_in_sqlite(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    operation = journal.start(
        operation_id="immutable",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    operation = _record_all_snapshots(journal, operation)
    recovery = journal.mark_boot_recovery_needed()
    assert recovery is not None
    abort = journal.claim_abort(
        operation.operation_id,
        operator_ref="user:owner",
        expected_revision=recovery.revision,
    )
    for surface in journal.surfaces:
        assert abort.execution_token is not None
        abort = journal.record_surface_restored(
            abort.operation_id,
            surface.name,
            expected_revision=abort.revision,
            execution_token=abort.execution_token,
        )
    assert abort.execution_token is not None
    terminal = journal.mark_aborted(
        abort.operation_id,
        expected_revision=abort.revision,
        execution_token=abort.execution_token,
    )

    connection = sqlite3.connect(journal.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE clear_operation SET closed_error = NULL WHERE operation_id = ?",
                (terminal.operation_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE clear_event SET event = 'started' WHERE operation_id = ?",
                (terminal.operation_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM clear_event WHERE operation_id = ?",
                (terminal.operation_id,),
            )
    finally:
        connection.close()


def test_journal_is_private_and_rejects_symlink_database(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    assert stat.S_IMODE(journal.database_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.database_path.stat().st_mode) == 0o600

    other_home = tmp_path / "other-home"
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"")
    target.chmod(0o600)
    database = other_home / "state/memory/clear-journal.sqlite"
    database.parent.mkdir(parents=True, mode=0o700)
    database.symlink_to(target)
    with pytest.raises(MemoryClearJournalError):
        MemoryClearJournal(other_home)

    sidecar_target = tmp_path / "sidecar-target"
    sidecar_target.write_bytes(b"")
    sidecar_target.chmod(0o600)
    sidecar = journal.database_path.with_name(f"{journal.database_path.name}-wal")
    sidecar.symlink_to(sidecar_target)
    with pytest.raises(MemoryClearJournalError):
        journal.get_open_operation()


def test_journal_rejects_database_inside_a_managed_surface(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    with pytest.raises(ValueError):
        MemoryClearJournal(home, database_path="memory/everos-root/journal.sqlite")


@pytest.mark.parametrize(
    ("pre_epoch", "target_epoch"),
    [(0, 0), (1, 3), (-1, 0), (True, 2)],
)
def test_journal_requires_a_single_epoch_advance(
    tmp_path: Path,
    pre_epoch: int,
    target_epoch: int,
) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal.start(
            operator_ref="user:owner",
            pre_epoch=pre_epoch,
            target_epoch=target_epoch,
        )
