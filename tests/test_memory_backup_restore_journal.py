from __future__ import annotations

from pathlib import Path

import pytest

from core.memory.backup_restore_journal import (
    BackupRestoreCASMismatch,
    BackupRestoreConflict,
    MemoryBackupRestoreJournal,
    MemoryBackupRestoreJournalError,
)
from core.memory.snapshot import MemorySnapshotManager

from .test_memory_snapshot import _build_all_surfaces


def _snapshot(tmp_path: Path):
    home = tmp_path / "home"
    connection = _build_all_surfaces(home)
    connection.close()
    return home, MemorySnapshotManager(home).create("ordinary-backup")


def test_restore_journal_persists_boot_retry_and_terminal_audit(tmp_path: Path) -> None:
    home, snapshot = _snapshot(tmp_path)
    journal = MemoryBackupRestoreJournal(home)

    started = journal.start(snapshot)
    restarted = MemoryBackupRestoreJournal(home)
    recovery = restarted.mark_boot_recovery_needed()

    assert recovery is not None
    assert recovery.operation_id == started.operation_id
    assert recovery.state == "recovery_needed"
    assert recovery.execution_token is None
    assert recovery.digest_mapping() == snapshot.surface_digests()

    retry = restarted.claim_retry(
        recovery.operation_id,
        expected_revision=recovery.revision,
        actor_ref="system:boot",
    )
    assert retry.state == "restoring"
    assert retry.attempt_count == 2
    assert retry.execution_token is not None

    completed = restarted.mark_completed(
        retry.operation_id,
        expected_revision=retry.revision,
        execution_token=retry.execution_token,
        actor_ref="system:boot",
    )
    assert completed.state == "completed"
    assert completed.terminal_at is not None
    assert restarted.get_open_operation() is None
    assert [event.event for event in restarted.get_events(completed.operation_id)] == [
        "started",
        "recovery_needed",
        "retry_started",
        "completed",
    ]

    with pytest.raises(BackupRestoreCASMismatch):
        restarted.mark_completed(
            completed.operation_id,
            expected_revision=retry.revision,
            execution_token=retry.execution_token,
        )


def test_restore_journal_keeps_one_open_operation_until_completion(tmp_path: Path) -> None:
    home, snapshot = _snapshot(tmp_path)
    journal = MemoryBackupRestoreJournal(home)
    operation = journal.start(snapshot)

    with pytest.raises(BackupRestoreConflict):
        journal.start(snapshot)
    with pytest.raises(BackupRestoreConflict):
        journal.assert_idle()

    recovery = journal.mark_recovery_needed(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token or "",
    )
    assert recovery.state == "recovery_needed"
    assert recovery.last_error == "memory_clear_failed"
    with pytest.raises(BackupRestoreCASMismatch):
        journal.claim_retry(
            recovery.operation_id,
            expected_revision=operation.revision,
            actor_ref="system:runtime",
        )


def test_restore_journal_translates_hardening_failure_after_durable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, snapshot = _snapshot(tmp_path)
    journal = MemoryBackupRestoreJournal(home)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def fail_journal_hardening(target, mode: int) -> None:
        if Path(target) == journal.database_path and mode == 0o600:
            raise confined_filesystem.ConfinedFilesystemError("unsafe journal")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.os, "chmod", fail_journal_hardening)

    with pytest.raises(
        MemoryBackupRestoreJournalError,
        match="Memory backup restore journal files could not be hardened safely",
    ):
        journal.start(snapshot)

    committed = journal.get_open_operation()
    assert committed is not None
    assert committed.backup_id == snapshot.snapshot_id
