from __future__ import annotations

from pathlib import Path

import pytest

import core.memory.snapshot as snapshot_module
from core.memory.clear_journal import (
    ClearOperation,
    ClearOperationCASMismatch,
    ClearTransitionError,
    MemoryClearJournal,
)
from core.memory.clear_snapshot_storage import MemoryClearSnapshotStorage
from core.memory.snapshot import (
    MemorySnapshotManager,
    MemorySnapshotUnsafePathError,
    MemorySnapshotVerificationError,
)


def _storage(
    tmp_path: Path,
) -> tuple[MemoryClearJournal, MemorySnapshotManager, MemoryClearSnapshotStorage]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    journal = MemoryClearJournal(home)
    manager = MemorySnapshotManager(home)
    return journal, manager, MemoryClearSnapshotStorage(journal, manager)


def _record_snapshot(
    journal: MemoryClearJournal,
    manager: MemorySnapshotManager,
    operation_id: str,
) -> ClearOperation:
    operation = journal.start(
        operation_id=operation_id,
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    snapshot = manager.create(operation.operation_id)
    assert operation.execution_token is not None
    return journal.record_snapshot(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
        snapshot=snapshot,
    )


def _complete_clear(
    journal: MemoryClearJournal,
    manager: MemorySnapshotManager,
    operation_id: str,
) -> ClearOperation:
    operation = _record_snapshot(journal, manager, operation_id)
    assert operation.execution_token is not None
    operation = journal.mark_prepared(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    assert operation.execution_token is not None
    operation = journal.begin_deleting(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    for surface in journal.surfaces:
        assert operation.execution_token is not None
        operation = journal.record_surface_deleted(
            operation.operation_id,
            surface.name,
            expected_revision=operation.revision,
            execution_token=operation.execution_token,
        )
    assert operation.execution_token is not None
    return journal.mark_completed(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )


def _abort_clear(
    journal: MemoryClearJournal,
    manager: MemorySnapshotManager,
    operation_id: str,
) -> ClearOperation:
    operation = _record_snapshot(journal, manager, operation_id)
    assert operation.execution_token is not None
    operation = journal.mark_prepared(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    recovery = journal.mark_boot_recovery_needed()
    assert recovery is not None
    operation = journal.claim_abort(
        operation.operation_id,
        operator_ref="user:owner",
        expected_revision=recovery.revision,
    )
    for surface in journal.surfaces:
        assert operation.execution_token is not None
        operation = journal.record_surface_restored(
            operation.operation_id,
            surface.name,
            expected_revision=operation.revision,
            execution_token=operation.execution_token,
        )
    assert operation.execution_token is not None
    return journal.mark_aborted(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )


def test_terminal_snapshot_removal_requires_a_complete_audit_and_verifies_bytes(
    tmp_path: Path,
) -> None:
    journal, manager, storage = _storage(tmp_path)
    operation = _record_snapshot(journal, manager, "remove-completed")
    snapshot_path = manager.snapshot_path(operation.operation_id)

    assert storage.eligible_terminal_snapshot_ids() == ()
    with pytest.raises(ClearTransitionError, match="only a terminal"):
        storage.remove_terminal_snapshot(operation.operation_id)
    assert snapshot_path.is_dir()

    assert operation.execution_token is not None
    operation = journal.mark_prepared(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    assert operation.execution_token is not None
    operation = journal.begin_deleting(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
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

    assert storage.eligible_terminal_snapshot_ids() == (operation.operation_id,)
    manifest_path = snapshot_path / "manifest.jsonl"
    manifest = manifest_path.read_bytes()
    manifest_path.write_bytes(b"corrupt")
    with pytest.raises(MemorySnapshotVerificationError):
        storage.remove_terminal_snapshot(operation.operation_id)
    assert snapshot_path.is_dir()
    manifest_path.write_bytes(manifest)

    storage.remove_terminal_snapshot(operation.operation_id)
    storage.remove_terminal_snapshot(operation.operation_id)
    assert not snapshot_path.exists()


def test_aborted_restored_snapshot_is_eligible_for_idempotent_removal(
    tmp_path: Path,
) -> None:
    journal, manager, storage = _storage(tmp_path)
    operation = _abort_clear(journal, manager, "remove-aborted")
    snapshot_path = manager.snapshot_path(operation.operation_id)

    assert storage.eligible_terminal_snapshot_ids() == (operation.operation_id,)
    storage.remove_terminal_snapshot(operation.operation_id)
    storage.remove_terminal_snapshot(operation.operation_id)

    assert not snapshot_path.exists()


def test_preparing_discard_requires_exact_cas_and_refuses_a_recorded_snapshot(
    tmp_path: Path,
) -> None:
    journal, manager, storage = _storage(tmp_path)
    operation = journal.start(
        operation_id="publish-before-record",
        operator_ref="user:owner",
        pre_epoch=2,
        target_epoch=3,
    )
    manager.create(operation.operation_id)
    assert operation.execution_token is not None

    with pytest.raises(ClearOperationCASMismatch):
        storage.discard_unrecorded_preparing_snapshot(
            operation.operation_id,
            expected_revision=operation.revision + 1,
            execution_token=operation.execution_token,
        )
    assert manager.snapshot_path(operation.operation_id).is_dir()

    discarded = storage.discard_unrecorded_preparing_snapshot(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    assert discarded.revision == operation.revision + 1
    assert journal.get_events(operation.operation_id)[-1].event == "snapshot_discarded"
    assert not manager.snapshot_path(operation.operation_id).exists()

    rebuilt = manager.create(operation.operation_id)
    assert discarded.execution_token is not None
    recorded = journal.record_snapshot(
        operation.operation_id,
        expected_revision=discarded.revision,
        execution_token=discarded.execution_token,
        snapshot=rebuilt,
    )
    assert recorded.execution_token is not None
    with pytest.raises(ClearTransitionError, match="only an unrecorded"):
        storage.discard_unrecorded_preparing_snapshot(
            recorded.operation_id,
            expected_revision=recorded.revision,
            execution_token=recorded.execution_token,
        )
    assert manager.snapshot_path(operation.operation_id).is_dir()


def test_terminal_removal_retries_a_partially_deleted_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, manager, storage = _storage(tmp_path)
    operation = _complete_clear(journal, manager, "remove-retry")
    snapshot_path = manager.snapshot_path(operation.operation_id)
    tombstone = manager.snapshot_root / f".{operation.operation_id}.gc"
    real_unlink = snapshot_module.os.unlink
    interrupted = False

    def interrupt_after_unlink(path, *args, **kwargs):
        nonlocal interrupted
        real_unlink(path, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("injected tombstone removal failure")

    monkeypatch.setattr(snapshot_module.os, "unlink", interrupt_after_unlink)
    with pytest.raises(OSError, match="injected tombstone removal failure"):
        storage.remove_terminal_snapshot(operation.operation_id)

    assert interrupted
    assert not snapshot_path.exists()
    assert tombstone.is_dir()

    monkeypatch.setattr(snapshot_module.os, "unlink", real_unlink)
    storage.remove_terminal_snapshot(operation.operation_id)

    assert not tombstone.exists()
    assert not snapshot_path.exists()


def test_terminal_removal_refuses_a_symlinked_snapshot_directory(
    tmp_path: Path,
) -> None:
    journal, manager, storage = _storage(tmp_path)
    operation = _complete_clear(journal, manager, "remove-symlink")
    snapshot_path = manager.snapshot_path(operation.operation_id)
    displaced = manager.snapshot_root / ".displaced"
    snapshot_path.rename(displaced)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("must remain")
    snapshot_path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(MemorySnapshotUnsafePathError):
        storage.remove_terminal_snapshot(operation.operation_id)

    assert snapshot_path.is_symlink()
    assert displaced.is_dir()
    assert sentinel.read_text() == "must remain"
