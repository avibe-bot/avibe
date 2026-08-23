from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import asyncio
import sqlite3
import threading

import pytest

from core.memory.clear_intent import (
    ClearIntent,
    ClearIntentError,
    ClearIntentStore,
    DEFAULT_CLEAR_SURFACES,
)
from core.memory.maintenance import MemoryMaintenance
from core.memory.operation_lock import MemoryOperationLease
from core.memory.store import MemoryStore


class _Port:
    def __init__(self, home: Path) -> None:
        self.deleted: list[tuple[str, int]] = []
        self.resumed = 0
        self.entered = 0
        self.left = 0
        self.home = home
        self.assert_clear_fenced = None
        self.fail_strict_quiesce = False
        self.fail_quiesce = False
        self.quiesce_modes: list[bool] = []
        self.claim_events: list[str] = []

    def exclusive_fence(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fence():
            yield

        return fence()

    def boot_recovery_fence(self):
        return self.exclusive_fence()

    def state(self):
        return SimpleNamespace(artifact_installing=False)

    def enter_maintenance(self):
        self.entered += 1

    def leave_maintenance(self):
        self.left += 1

    async def pause_claims(self):
        self.claim_events.append("pause")
        return None

    def resume_claims(self):
        return None

    async def quiesce(self, _strict: bool):
        self.claim_events.append("quiesce")
        self.quiesce_modes.append(_strict)
        if self.fail_quiesce or (_strict and self.fail_strict_quiesce):
            raise RuntimeError("quiesce failed")
        return None

    async def resume(self):
        self.resumed += 1

    async def delete_surface(self, surface, target_epoch: int):
        if self.assert_clear_fenced is not None:
            self.assert_clear_fenced()
        self.deleted.append((surface.surface, target_epoch))

    def restore_completed(self):
        return None


def _maintenance(tmp_path: Path) -> tuple[MemoryMaintenance, _Port]:
    port = _Port(tmp_path)
    maintenance = MemoryMaintenance(
        MemoryStore(),
        effective_home=tmp_path,
        runtime=port,
    )
    return maintenance, port


@pytest.mark.asyncio
async def test_clear_writes_marker_and_repeats_four_surfaces(tmp_path: Path):
    """MEMORY-CLEAR-201: Clear fences and repeats every owned deletion surface."""

    maintenance, port = _maintenance(tmp_path)
    port.assert_clear_fenced = lambda: (
        assert_marker_and_fence(maintenance, tmp_path)
    )

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert [surface for surface, _epoch in port.deleted] == [
        "metadata",
        "provider",
        "call_log",
        "attachments",
    ]
    assert len({epoch for _surface, epoch in port.deleted}) == 1
    assert ClearIntentStore(tmp_path).load() is None
    assert port.resumed == 1
    assert maintenance._store is not None
    assert maintenance._store.clear_in_progress() is False


def assert_marker_and_fence(maintenance: MemoryMaintenance, home: Path) -> None:
    assert ClearIntentStore(home).load() is not None
    assert maintenance._store is not None
    assert maintenance._store.clear_in_progress() is True


@pytest.mark.asyncio
async def test_failed_clear_persists_failed_projection_and_boot_retries(tmp_path: Path):
    """MEMORY-CLEAR-202: a failed marker is retained and retried on reconcile."""

    maintenance, port = _maintenance(tmp_path)

    async def fail(surface, target_epoch: int):
        port.deleted.append((surface.surface, target_epoch))
        if surface.surface == "provider":
            raise RuntimeError("provider unavailable")

    port.delete_surface = fail
    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "failed"
    intent = ClearIntentStore(tmp_path).load()
    assert intent is not None
    assert intent.state == "failed"
    assert result.clear_in_progress is not None
    assert result.clear_in_progress.state == "failed"

    port.delete_surface = _Port.delete_surface.__get__(port, _Port)
    assert await maintenance.reconcile_pending() is True
    assert ClearIntentStore(tmp_path).load() is None
    assert port.resumed == 0


@pytest.mark.asyncio
async def test_marker_removal_failure_retries_after_surfaces_are_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    maintenance, _port = _maintenance(tmp_path)
    store = maintenance._store
    assert store is not None
    remove_calls = 0
    original_remove = maintenance._intent.remove

    def fail_once() -> None:
        nonlocal remove_calls
        remove_calls += 1
        assert store.clear_in_progress() is False
        if remove_calls == 1:
            raise ClearIntentError("marker remove failed")
        original_remove()

    monkeypatch.setattr(maintenance._intent, "remove", fail_once)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "failed"
    intent = ClearIntentStore(tmp_path).load()
    assert intent is not None
    assert intent.state == "failed"
    assert store.clear_in_progress() is False

    assert await maintenance.reconcile_pending() is True
    assert remove_calls == 2
    assert ClearIntentStore(tmp_path).load() is None


@pytest.mark.asyncio
async def test_marker_unlink_fsync_failure_retains_failed_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    maintenance, _port = _maintenance(tmp_path)
    original_remove = maintenance._intent.remove

    def unlink_then_fail() -> None:
        original_remove()
        raise ClearIntentError("marker fsync failed")

    monkeypatch.setattr(maintenance._intent, "remove", unlink_then_fail)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "failed"
    retained = ClearIntentStore(tmp_path).load()
    assert retained is not None
    assert retained.state == "failed"


@pytest.mark.asyncio
async def test_corrupt_marker_can_be_replaced_by_user_clear(tmp_path: Path):
    marker = tmp_path / "state/memory/clear-intent.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("not json", encoding="utf-8")
    maintenance, _port = _maintenance(tmp_path)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert ClearIntentStore(tmp_path).load() is None
    assert maintenance.is_open() is False
    assert (await maintenance.observe()).can_clear is True


@pytest.mark.asyncio
async def test_explicit_clear_replaces_legacy_failed_marker(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not sqlite")
    maintenance, port = _maintenance(tmp_path)
    legacy_intent = ClearIntentStore(tmp_path).load()
    assert legacy_intent is not None
    assert legacy_intent.error_code == "memory_clear_legacy_state_requires_rerun"

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert result.operation_id != legacy_intent.operation_id
    assert len(port.deleted) == len(DEFAULT_CLEAR_SURFACES)
    assert ClearIntentStore(tmp_path).load() is None
    assert maintenance.is_open() is False


def test_legacy_cleanup_defers_while_operation_lease_is_busy(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not sqlite")
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()
    try:
        maintenance, _port = _maintenance(tmp_path)
        assert journal.exists()
        assert ClearIntentStore(tmp_path).load() is None
        assert maintenance.is_open() is True
    finally:
        lease.release()

    assert asyncio.run(maintenance.reconcile_pending()) is False
    failed = ClearIntentStore(tmp_path).load()
    assert failed is not None
    assert failed.error_code == "memory_clear_legacy_state_requires_rerun"
    assert not journal.exists()


def test_legacy_cleanup_lease_failure_fences_memory(tmp_path: Path, monkeypatch):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not sqlite")
    from core.memory import maintenance as maintenance_module

    def fail_acquire(_lease):
        raise OSError("lock path unavailable")

    monkeypatch.setattr(maintenance_module.MemoryOperationLease, "acquire", fail_acquire)
    maintenance, _port = _maintenance(tmp_path)

    assert maintenance.is_open() is True
    assert maintenance._initialization_error is not None
    assert ClearIntentStore(tmp_path).load() is None
    assert journal.exists()


def test_legacy_cleanup_lease_release_failure_is_contained(tmp_path: Path, monkeypatch):
    snapshot = tmp_path / "state/memory/clear-snapshots/snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("obsolete", encoding="utf-8")
    from core.memory import maintenance as maintenance_module

    original_release = maintenance_module.MemoryOperationLease.release

    def release_then_fail(lease):
        original_release(lease)
        raise OSError("lock descriptor close failed")

    monkeypatch.setattr(maintenance_module.MemoryOperationLease, "release", release_then_fail)

    maintenance, _port = _maintenance(tmp_path)

    assert maintenance.is_open() is True
    assert maintenance._initialization_error is not None
    assert maintenance._legacy_migration_deferred is True


def test_legacy_open_row_creates_failed_projection_without_boot_delete(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute("CREATE TABLE clear_operation (open_slot)")
    connection.execute("INSERT INTO clear_operation VALUES (?)", ("open",))
    connection.commit()
    connection.close()
    journal.chmod(0o600)

    maintenance, port = _maintenance(tmp_path)
    projection = maintenance._read_projection()

    assert projection is not None
    assert projection.state == "failed"
    assert projection.error_code == "memory_clear_legacy_state_requires_rerun"
    assert asyncio.run(maintenance.reconcile_pending()) is False
    assert port.deleted == []
    assert not journal.exists()


def test_terminal_legacy_journal_is_cleaned_without_fence(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute("CREATE TABLE clear_operation (open_slot)")
    connection.execute("INSERT INTO clear_operation VALUES (NULL)")
    connection.commit()
    connection.close()
    journal.chmod(0o600)

    maintenance, _port = _maintenance(tmp_path)

    assert maintenance.is_open() is False
    assert ClearIntentStore(tmp_path).load() is None
    assert not journal.exists()


def test_legacy_backup_and_snapshot_residue_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backup_journal = tmp_path / "state/memory/backup-restore-journal.sqlite"
    snapshot = tmp_path / "state/memory/clear-snapshots/old/data"
    for path in (backup_journal, snapshot):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("obsolete", encoding="utf-8")
    from core.memory import clear_intent

    original_remove = clear_intent.remove_confined_path

    def fail_backup_remove(home: Path, path: Path):
        if path == backup_journal:
            raise clear_intent.ConfinedFilesystemError("busy")
        return original_remove(home, path)

    monkeypatch.setattr(clear_intent, "remove_confined_path", fail_backup_remove)

    maintenance, _port = _maintenance(tmp_path)

    assert maintenance.is_open() is False
    assert backup_journal.exists()
    assert not snapshot.exists()


@pytest.mark.asyncio
async def test_clear_runs_legacy_rescan_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    maintenance, _port = _maintenance(tmp_path)
    event_loop_thread = threading.get_ident()
    rescan_threads: list[int] = []
    original_migrate = maintenance._migrate_legacy

    def tracked_migrate(store: MemoryStore | None, *, lease_held: bool = False) -> None:
        rescan_threads.append(threading.get_ident())
        original_migrate(store, lease_held=lease_held)

    monkeypatch.setattr(maintenance, "_migrate_legacy", tracked_migrate)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert rescan_threads
    assert all(thread_id != event_loop_thread for thread_id in rescan_threads)

@pytest.mark.asyncio
async def test_failed_clear_retry_rebases_after_store_epoch_moves(tmp_path: Path):
    maintenance, port = _maintenance(tmp_path)
    store = maintenance._store
    assert store is not None
    failed = ClearIntent.new(operator_ref="old", pre_epoch=store.ensure_meta().epoch).failed(
        "memory_clear_failed"
    )
    ClearIntentStore(tmp_path).write(failed)
    store.reset_for_clear()
    store.reset_for_clear()

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert result.epoch == 3
    assert {epoch for _surface, epoch in port.deleted} == {3}


@pytest.mark.asyncio
async def test_boot_quiesce_failure_persists_failed_marker(tmp_path: Path):
    maintenance, port = _maintenance(tmp_path)
    meta = maintenance._store.ensure_meta()  # type: ignore[union-attr]
    intent = ClearIntent.new(operator_ref="boot", pre_epoch=meta.epoch)
    ClearIntentStore(tmp_path).write(intent)
    port.fail_quiesce = True

    assert await maintenance.reconcile_pending() is False
    failed = ClearIntentStore(tmp_path).load()
    assert failed is not None
    assert failed.state == "failed"
    assert failed.error_code == "memory_clear_failed"


@pytest.mark.asyncio
async def test_boot_retry_requiesces_claims(tmp_path: Path):
    maintenance, port = _maintenance(tmp_path)
    meta = maintenance._store.ensure_meta()  # type: ignore[union-attr]
    ClearIntentStore(tmp_path).write(ClearIntent.new(operator_ref="boot", pre_epoch=meta.epoch))
    assert await maintenance.reconcile_pending() is True
    assert port.quiesce_modes == [False]


@pytest.mark.asyncio
async def test_terminal_marker_removal_cancellation_resumes_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import threading

    maintenance, port = _maintenance(tmp_path)
    original_remove = maintenance._intent.remove
    started = threading.Event()
    release = threading.Event()

    def remove_then_cancel():
        started.set()
        release.wait()
        original_remove()

    monkeypatch.setattr(maintenance._intent, "remove", remove_then_cancel)
    clear_task = asyncio.create_task(maintenance.clear(operator_ref="user-1"))
    assert await asyncio.to_thread(started.wait, 1.0)
    clear_task.cancel()
    release.set()
    result = await clear_task

    assert result.status == "completed"
    assert maintenance.is_open() is False
    assert port.resumed == 1


@pytest.mark.asyncio
async def test_committed_clear_fence_release_failure_does_not_restore_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    maintenance, port = _maintenance(tmp_path)
    store = maintenance._store
    assert store is not None
    original_release = store.release_clear_fence

    def release_then_fail():
        original_release()
        raise OSError("post-commit mode hardening failed")

    monkeypatch.setattr(store, "release_clear_fence", release_then_fail)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert store.clear_in_progress() is False
    assert ClearIntentStore(tmp_path).load() is None
    assert maintenance.is_open() is False
    assert port.resumed == 1


def test_corrupt_marker_is_fail_closed(tmp_path: Path):
    marker = tmp_path / "state/memory/clear-intent.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("not json", encoding="utf-8")
    maintenance, _port = _maintenance(tmp_path)

    observation = maintenance._read_projection()

    assert observation is not None
    assert observation.state == "failed"
    assert observation.error_code == "memory_clear_marker_unreadable"


def test_orphaned_queue_clear_fence_is_exposed_for_explicit_repair(tmp_path: Path):
    maintenance, _port = _maintenance(tmp_path)
    store = maintenance._store
    assert store is not None
    store.begin_clear_fence()

    assert maintenance.is_open() is True
    assert maintenance.has_readable_intent() is False
    observation = maintenance._read_projection()
    assert observation is not None
    assert observation.state == "failed"
    assert observation.operation_id == "orphaned-fence"
    assert observation.error_code == "memory_clear_failed"
    payload = asyncio.run(maintenance.observe())
    assert payload.can_clear is True


def test_queue_fence_read_failure_is_retryable(tmp_path: Path, monkeypatch):
    maintenance, _port = _maintenance(tmp_path)
    store = maintenance._store
    assert store is not None
    reads = 0

    def fail_fence_read():
        nonlocal reads
        reads += 1
        if reads <= 2:
            raise OSError("temporary queue read failure")
        return False

    monkeypatch.setattr(store, "clear_in_progress", fail_fence_read)

    assert maintenance.is_open() is True
    observation = asyncio.run(maintenance.observe())
    assert observation.can_clear is True
    assert observation.clear_in_progress is not None
    assert observation.clear_in_progress.operation_id == "orphaned-fence"
    assert maintenance.ready is True

    assert maintenance.is_open() is False
