from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import asyncio
import sqlite3

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


def _add_surface_receipts(connection: sqlite3.Connection, operation_id: str) -> None:
    connection.execute(
        """
        CREATE TABLE clear_surface (
            operation_id TEXT,
            surface TEXT,
            relative_path TEXT,
            relative_snapshot_path TEXT,
            present INTEGER,
            pre_clear_digest TEXT,
            snapshot_digest TEXT,
            state TEXT,
            updated_at TEXT
        )
        """
    )
    for surface in ("queue", "provider", "call_log", "attachments"):
        path = next(item.relative_path for item in DEFAULT_CLEAR_SURFACES if item.surface == surface)
        connection.execute(
            "INSERT INTO clear_surface VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (operation_id, surface, path, f"state/memory/clear-snapshots/{operation_id}/{surface}", 0, None, None, "snapshotted", "now"),
        )


def _write_backup_restore_journal(tmp_path: Path, *, state: str, open_slot: int | None) -> Path:
    journal = tmp_path / "state/memory/backup-restore-journal.sqlite"
    journal.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        """
        CREATE TABLE backup_restore_operation (
            operation_id TEXT, backup_id TEXT, manifest_sha256 TEXT,
            surface_digests_json TEXT, state TEXT, started_at TEXT,
            updated_at TEXT, terminal_at TEXT, attempt_count INTEGER,
            last_error TEXT, open_slot INTEGER, revision INTEGER,
            execution_token TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO backup_restore_operation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "a" * 32,
            "backup-one",
            "a" * 64,
            '{"state/memory/memory.sqlite":null}',
            state,
            "2026-08-13T00:00:00Z",
            "2026-08-13T00:00:00Z",
            None,
            1,
            None,
            open_slot,
            1,
            None,
        ),
    )
    connection.commit()
    connection.close()
    journal.chmod(0o600)
    return journal


@pytest.mark.asyncio
async def test_clear_writes_marker_and_repeats_four_surfaces(tmp_path: Path):
    maintenance, port = _maintenance(tmp_path)
    port.assert_clear_fenced = lambda: (
        assert_marker_and_fence(maintenance, tmp_path)
    )

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert [surface for surface, _epoch in port.deleted] == [
        surface.surface for surface in DEFAULT_CLEAR_SURFACES
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
    remove_calls = 0
    original_remove = maintenance._intent.remove

    def fail_once() -> None:
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 1:
            raise ClearIntentError("marker remove failed")
        original_remove()

    monkeypatch.setattr(maintenance._intent, "remove", fail_once)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "failed"
    intent = ClearIntentStore(tmp_path).load()
    assert intent is not None
    assert intent.state == "failed"
    assert maintenance._store is not None
    assert maintenance._store.clear_in_progress() is False

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
async def test_explicit_clear_consumes_unreadable_legacy_journal(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not sqlite")
    maintenance, _port = _maintenance(tmp_path)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert not journal.exists()
    assert maintenance.is_open() is False


@pytest.mark.asyncio
async def test_legacy_journal_removal_failure_keeps_claims_fenced(tmp_path: Path, monkeypatch):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not sqlite")
    maintenance, port = _maintenance(tmp_path)

    def unlink_then_fail():
        journal.unlink()
        raise ClearIntentError("legacy journal sync failed")

    monkeypatch.setattr(maintenance._intent, "consume_legacy_clear_state", unlink_then_fail)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "failed"
    assert port.claim_events == ["pause", "quiesce"]
    assert port.resumed == 0
    failed = ClearIntentStore(tmp_path).load()
    assert failed is not None
    assert failed.state == "failed"
    assert failed.error_code == "memory_clear_failed"


@pytest.mark.asyncio
async def test_marker_write_cancellation_settles_claim_fence(tmp_path: Path, monkeypatch):
    import asyncio
    import threading

    maintenance, port = _maintenance(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original_write = maintenance._intent.write

    def gated_write(intent):
        started.set()
        release.wait()
        original_write(intent)

    monkeypatch.setattr(maintenance._intent, "write", gated_write)
    clear_task = asyncio.create_task(maintenance.clear(operator_ref="user-1"))
    assert await asyncio.to_thread(started.wait, 1.0)
    clear_task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await clear_task

    assert port.claim_events == ["pause", "quiesce"]
    assert port.resumed == 0
    assert maintenance._store is not None
    assert maintenance._store.clear_in_progress() is True
    failed = ClearIntentStore(tmp_path).load()
    assert failed is not None
    assert failed.state == "failed"


@pytest.mark.asyncio
async def test_marker_post_rename_write_failure_settles_claim_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from core.memory import clear_intent

    maintenance, port = _maintenance(tmp_path)
    original_fsync = clear_intent.fsync_directory
    fsync_calls = 0

    def fail_once(path: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise clear_intent.ConfinedFilesystemError("marker directory fsync failed")
        original_fsync(path)

    monkeypatch.setattr(clear_intent, "fsync_directory", fail_once)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "failed"
    assert port.claim_events == ["pause", "quiesce"]
    assert port.resumed == 0
    failed = ClearIntentStore(tmp_path).load()
    assert failed is not None
    assert failed.state == "failed"


def test_legacy_cleanup_defers_while_operation_lease_is_busy(tmp_path: Path):
    snapshot = tmp_path / "state/memory/clear-snapshots/snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("retained", encoding="utf-8")
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()
    try:
        maintenance, _port = _maintenance(tmp_path)
        assert snapshot.read_text(encoding="utf-8") == "retained"
        assert maintenance.is_open() is True
    finally:
        lease.release()

    _maintenance(tmp_path)
    assert not snapshot.parent.exists()


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
    assert (maintenance._initialization_error is not None)


def test_open_legacy_backup_restore_is_preserved_and_fences_memory(tmp_path: Path):
    journal = _write_backup_restore_journal(tmp_path, state="recovery_needed", open_slot=1)
    backup = tmp_path / "state/memory/backups/backup-one/manifest.json"
    backup.parent.mkdir(parents=True)
    backup.write_text("retained", encoding="utf-8")

    maintenance, _port = _maintenance(tmp_path)

    assert maintenance.has_open_restore() is True
    assert maintenance.is_open() is True
    assert maintenance._legacy_migration_deferred is True
    assert journal.exists()
    assert backup.read_text(encoding="utf-8") == "retained"


def test_terminal_legacy_backup_restore_is_removed(tmp_path: Path):
    journal = _write_backup_restore_journal(tmp_path, state="completed", open_slot=None)
    backup = tmp_path / "state/memory/backups/backup-one/manifest.json"
    backup.parent.mkdir(parents=True)
    backup.write_text("obsolete", encoding="utf-8")

    maintenance, _port = _maintenance(tmp_path)

    assert maintenance.has_open_restore() is False
    assert maintenance.is_open() is False
    assert not journal.exists()
    assert not backup.exists()


def test_failed_legacy_backup_cleanup_retains_migration_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = _write_backup_restore_journal(tmp_path, state="completed", open_slot=None)
    from core.memory import clear_intent

    original_remove = clear_intent.remove_confined_path

    def fail_journal_remove(home: Path, path: Path):
        if path == journal:
            raise clear_intent.ConfinedFilesystemError("journal is busy")
        return original_remove(home, path)

    monkeypatch.setattr(clear_intent, "remove_confined_path", fail_journal_remove)
    maintenance, _port = _maintenance(tmp_path)

    assert maintenance.is_open() is True
    assert maintenance._legacy_migration_deferred is True
    assert journal.exists()


def test_legacy_migration_retries_after_transient_store_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, 1)",
        ("legacy-one", "user-1", 0, "deleting", "2026-08-13T00:00:00Z"),
    )
    _add_surface_receipts(connection, "legacy-one")
    connection.commit()
    connection.close()
    journal.chmod(0o600)
    maintenance = MemoryMaintenance(None, effective_home=tmp_path, runtime=_Port(tmp_path))
    assert maintenance.is_open() is True

    store = MemoryStore(tmp_path / "state/memory/memory.sqlite")
    original_ensure_meta = store.ensure_meta
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary store read failure")
        return original_ensure_meta()

    monkeypatch.setattr(store, "ensure_meta", fail_once)
    maintenance.attach_store(store)

    assert maintenance.is_open() is True
    assert maintenance._legacy_migration_deferred is True
    assert asyncio.run(maintenance.reconcile_pending()) is True
    assert not journal.exists()


def test_deferred_legacy_migration_is_retryable_after_lease_release(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("legacy-one", "user-1", 0, 1, "deleting", "2026-08-13T00:00:00Z"),
    )
    _add_surface_receipts(connection, "legacy-one")
    connection.commit()
    connection.close()
    journal.chmod(0o600)
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()
    try:
        maintenance, _port = _maintenance(tmp_path)
        assert maintenance.is_open() is True
    finally:
        lease.release()

    assert asyncio.run(maintenance.reconcile_pending()) is True
    assert ClearIntentStore(tmp_path).load() is None
    assert not journal.exists()


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
