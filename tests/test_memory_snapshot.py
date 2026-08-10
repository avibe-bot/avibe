from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
import tracemalloc
from pathlib import Path

import pytest

import core.memory.confined_filesystem as confined_filesystem_module
import core.memory.snapshot as snapshot_module
from core.memory.clear_journal import (
    ClearBackupBlocked,
    MemoryClearJournal,
)
from core.memory.clear_snapshot_storage import MemoryClearSnapshotStorage
from core.memory.confined_filesystem import remove_confined_path
from core.memory.snapshot import (
    MemorySnapshotError,
    MemorySnapshotManager,
    MemorySnapshotUnsafePathError,
    MemorySnapshotVerificationError,
    SnapshotSurface,
)


def _private_directory(path: Path, home: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    while True:
        current.chmod(0o700)
        if current == home:
            break
        current = current.parent
    return path


def _private_file(path: Path, payload: bytes, home: Path) -> Path:
    _private_directory(path.parent, home)
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _create_deep_file_at(root: Path, components: list[str], name: str, payload: bytes) -> None:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in components:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=descriptor,
        )
        try:
            os.write(file_descriptor, payload)
            os.fchmod(file_descriptor, 0o600)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _read_deep_file_at(root: Path, components: list[str], name: str) -> bytes:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in components:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            return os.read(file_descriptor, 1024)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _sqlite_surface(path: Path, value: str, home: Path, *, keep_wal: bool = False):
    _private_directory(path.parent, home)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
    connection.execute("INSERT INTO records VALUES (?)", (value,))
    connection.commit()
    path.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(f"{path.name}{suffix}")
        if sidecar.exists():
            sidecar.chmod(0o600)
    if keep_wal:
        return connection
    connection.close()
    return None


def _sqlite_values(path: Path) -> list[str]:
    uri = f"{path.absolute().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        return [row[0] for row in connection.execute("SELECT value FROM records ORDER BY rowid")]


def _build_all_surfaces(home: Path) -> sqlite3.Connection:
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    queue = home / "state/memory/memory.sqlite"
    queue_connection = _sqlite_surface(queue, "queued-before-clear", home, keep_wal=True)
    assert queue_connection is not None
    _sqlite_surface(home / "memory/call-log/call-log.db", "call-before-clear", home)
    _private_file(home / "memory/everos-root/profiles/user.json", b'{"name":"Ada"}', home)
    _private_directory(home / "memory/everos-root/episodes/empty", home)
    _private_file(home / "memory/attachments/bundles/a1/00.txt", b"attachment", home)
    return queue_connection


def _complete_clear_audit(
    journal: MemoryClearJournal,
    manager: MemorySnapshotManager,
    operation_id: str,
) -> None:
    operation = journal.start(
        operation_id=operation_id,
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    snapshot = manager.create(operation.operation_id)
    assert operation.execution_token is not None
    operation = journal.record_snapshot(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
        snapshot=snapshot,
    )
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
    journal.mark_completed(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )


def _abort_clear_audit(
    journal: MemoryClearJournal,
    manager: MemorySnapshotManager,
    operation_id: str,
) -> None:
    operation = journal.start(
        operation_id=operation_id,
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    snapshot = manager.create(operation.operation_id)
    assert operation.execution_token is not None
    operation = journal.record_snapshot(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
        snapshot=snapshot,
    )
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
    journal.mark_aborted(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )


def test_snapshot_round_trip_covers_all_surfaces_and_wal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    queue_connection = _build_all_surfaces(home)
    manager = MemorySnapshotManager(home)

    snapshot = manager.create("clear-01")
    queue_connection.close()

    assert snapshot.relative_path == "state/memory/clear-snapshots/clear-01"
    assert len(snapshot.manifest_sha256) == 64
    roots = {entry.path: entry for entry in snapshot.entries}
    assert roots["state/memory/memory.sqlite"].type == "sqlite"
    assert roots["memory/everos-root"].type == "tree"
    assert roots["memory/everos-root"].tree_digest is not None
    assert roots["memory/call-log/call-log.db"].type == "sqlite"
    assert roots["memory/attachments"].type == "tree"
    assert all(
        receipt.pre_clear_digest == receipt.snapshot_digest
        and receipt.pre_clear_digest is not None
        for receipt in snapshot.surface_receipts
    )
    surface_digests = {
        surface.path: roots[surface.path].sha256 or roots[surface.path].tree_digest
        for surface in manager.surfaces
    }
    assert manager.verify(
        "clear-01",
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=surface_digests,
    ) == snapshot
    with pytest.raises(MemorySnapshotVerificationError):
        manager.verify(
            "clear-01",
            expected_manifest_sha256="0" * 64,
            expected_surface_digests=surface_digests,
        )
    with pytest.raises(TypeError):
        manager.verify("clear-01")  # type: ignore[call-arg]

    manifest_path = manager.snapshot_path("clear-01") / "manifest.jsonl"
    manifest_bytes = manifest_path.read_bytes()
    manifest = [json.loads(line) for line in manifest_bytes.splitlines()]
    assert str(home).encode() not in manifest_bytes
    assert manifest[0] == {
        "format": "avibe-memory-snapshot",
        "record": "header",
        "schema_version": 2,
    }
    entry_records = [record for record in manifest if record["record"] == "entry"]
    assert all(
        set(row) == {"path", "type", "mode", "size", "sha256", "tree_digest"}
        for row in (record["entry"] for record in entry_records)
    )
    assert all(
        not Path(record["entry"]["path"]).is_absolute()
        for record in entry_records
    )
    assert manifest[-1]["record"] == "footer"
    assert manifest[-1]["entry_count"] == len(entry_records)
    assert stat.S_IMODE(manager.snapshot_path("clear-01").stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600

    # The row committed only to the live WAL must be present in the standalone copy.
    copied_queue = (
        manager.snapshot_path("clear-01")
        / "payload"
        / "state/memory/memory.sqlite"
    )
    assert _sqlite_values(copied_queue) == ["queued-before-clear"]

    with sqlite3.connect(home / "state/memory/memory.sqlite") as connection:
        connection.execute("DELETE FROM records")
        connection.execute("INSERT INTO records VALUES ('queued-after-clear')")
    with sqlite3.connect(home / "memory/call-log/call-log.db") as connection:
        connection.execute("DELETE FROM records")
    shutil.rmtree(home / "memory/everos-root")
    _private_file(home / "memory/everos-root/new.txt", b"new provider bytes", home)
    shutil.rmtree(home / "memory/attachments")
    _private_file(home / "memory/attachments/new.txt", b"new attachment", home)

    restored = manager.restore(
        "clear-01",
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=surface_digests,
    )

    assert restored == snapshot
    assert _sqlite_values(home / "state/memory/memory.sqlite") == ["queued-before-clear"]
    assert _sqlite_values(home / "memory/call-log/call-log.db") == ["call-before-clear"]
    assert (home / "memory/everos-root/profiles/user.json").read_bytes() == b'{"name":"Ada"}'
    assert (home / "memory/everos-root/episodes/empty").is_dir()
    assert not (home / "memory/everos-root/new.txt").exists()
    assert (home / "memory/attachments/bundles/a1/00.txt").read_bytes() == b"attachment"
    assert not (home / "memory/attachments/new.txt").exists()
    assert not list(home.rglob("*.before-restore-*"))
    assert not list(home.rglob("*.restore-*"))


def test_snapshot_restore_relocates_and_restores_missing_as_absent(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    source_home.mkdir(mode=0o700)
    source_home.chmod(0o700)
    _sqlite_surface(source_home / "state/memory/memory.sqlite", "source", source_home)
    source_manager = MemorySnapshotManager(source_home)
    source_snapshot = source_manager.create("relocatable")
    roots = {entry.path: entry for entry in source_snapshot.entries}
    assert roots["memory/everos-root"].type == "missing"
    assert roots["memory/call-log/call-log.db"].type == "missing"
    assert roots["memory/attachments"].type == "missing"

    relocated_home = tmp_path / "relocated-home"
    relocated_snapshot = relocated_home / source_snapshot.relative_path
    _private_directory(relocated_snapshot.parent, relocated_home)
    shutil.copytree(source_manager.snapshot_path("relocatable"), relocated_snapshot)
    _private_file(relocated_home / "memory/everos-root/unexpected.txt", b"remove me", relocated_home)
    _sqlite_surface(relocated_home / "memory/call-log/call-log.db", "remove me", relocated_home)
    _private_file(relocated_home / "memory/attachments/unexpected.txt", b"remove me", relocated_home)

    relocated_manager = MemorySnapshotManager(relocated_home)
    relocated_manager.restore(
        "relocatable",
        expected_manifest_sha256=source_snapshot.manifest_sha256,
        expected_surface_digests=source_snapshot.surface_digests(),
    )

    assert _sqlite_values(relocated_home / "state/memory/memory.sqlite") == ["source"]
    assert not (relocated_home / "memory/everos-root").exists()
    assert not (relocated_home / "memory/call-log/call-log.db").exists()
    assert not (relocated_home / "memory/attachments").exists()
    assert source_home / "state/memory/memory.sqlite" != relocated_home / "state/memory/memory.sqlite"


def test_snapshot_restore_preserves_and_cleans_owner_only_tree_modes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = _private_directory(home / "memory/everos-root", home)
    original = _private_file(target / "profile.json", b"before", home)
    target.chmod(0o500)
    manager = MemorySnapshotManager(
        home,
        surfaces=(SnapshotSurface("memory/everos-root", "tree"),),
    )
    snapshot = manager.create("owner-only-tree")

    target.chmod(0o700)
    original.write_bytes(b"after")
    original.chmod(0o600)
    target.chmod(0o500)

    restored = manager.restore(
        snapshot.snapshot_id,
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=snapshot.surface_digests(),
    )

    assert restored == snapshot
    assert stat.S_IMODE(target.stat().st_mode) == 0o500
    assert original.read_bytes() == b"before"
    assert not list(home.rglob("*.before-restore-*"))
    assert not list(home.rglob("*.restore-*"))


def test_streaming_manifest_crosses_batches_and_restores_exact_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    expected = {
        f"memory/everos-root/profiles/{index:02d}.json": f"profile-{index}".encode()
        for index in range(7)
    }
    expected.update(
        {
            "memory/attachments/bundles/a/00.txt": b"attachment-a",
            "memory/attachments/bundles/b/00.txt": b"attachment-b",
        }
    )
    for relative, payload in expected.items():
        _private_file(home / relative, payload, home)
    _private_directory(home / "memory/everos-root/episodes/empty", home)
    monkeypatch.setattr(snapshot_module, "_MANIFEST_BATCH_SIZE", 2)
    monkeypatch.setattr(snapshot_module, "_DIRECTORY_ORDER_INSERT_BATCH_SIZE", 2)

    manager = MemorySnapshotManager(home)
    snapshot = manager.create("many-entries")
    with snapshot_module._indexed_manifest(
        manager.snapshot_path(snapshot.snapshot_id) / "manifest.jsonl"
    ) as (entries, _digest):
        assert entries.count() > snapshot_module._MANIFEST_BATCH_SIZE * 4
    assert len(snapshot.entries) == len(manager.surfaces)
    assert manager.verify(
        snapshot.snapshot_id,
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=snapshot.surface_digests(),
    ) == snapshot

    shutil.rmtree(home / "memory/everos-root")
    shutil.rmtree(home / "memory/attachments")
    _private_file(home / "memory/everos-root/unexpected.txt", b"unexpected", home)
    manager.restore(
        snapshot.snapshot_id,
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=snapshot.surface_digests(),
    )

    for relative, payload in expected.items():
        assert (home / relative).read_bytes() == payload
    assert (home / "memory/everos-root/episodes/empty").is_dir()
    assert not (home / "memory/everos-root/unexpected.txt").exists()


def test_deep_tree_create_verify_restore_and_terminal_clear_gc_converge(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    manager = MemorySnapshotManager(home)
    source_root = _private_directory(home / "memory/everos-root", home)

    path_max = os.pathconf(home, "PC_PATH_MAX")
    longest_prefix = max(
        len(os.fsencode(source_root)),
        len(
            os.fsencode(
                manager.snapshot_root
                / ".deep-tree.tmp/payload/memory/everos-root"
            )
        ),
        len(os.fsencode(home / "memory/.everos-root.restore-deep-tree-1")),
    )
    depth = min(1_100, (path_max - longest_prefix - len("/leaf.txt") - 16) // 2)
    recursion_limit = min(sys.getrecursionlimit(), depth - 32)
    assert recursion_limit > 200
    assert depth > recursion_limit

    leaf_parent = source_root
    for _ in range(depth):
        leaf_parent /= "d"
        leaf_parent.mkdir(mode=0o700)
    leaf = leaf_parent / "leaf.txt"
    leaf.write_bytes(b"sealed deep bytes")
    leaf.chmod(0o600)
    assert len(os.fsencode(leaf)) < path_max

    journal = MemoryClearJournal(home)
    operation = journal.start(
        operation_id="deep-tree",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    previous_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(recursion_limit)
        snapshot = manager.create("deep-tree")
        assert manager.verify(
            snapshot.snapshot_id,
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        ) == snapshot

        manifest_records = [
            json.loads(line)
            for line in (
                manager.snapshot_path(snapshot.snapshot_id) / "manifest.jsonl"
            ).read_bytes().splitlines()
        ]
        provider_paths = [
            record["entry"]["path"]
            for record in manifest_records
            if record.get("record") == "entry"
            and record["entry"]["path"].startswith("memory/everos-root")
        ]
        expected_directories = [
            "memory/everos-root" + "/d" * level
            for level in range(depth, 0, -1)
        ]
        assert provider_paths == [
            f"memory/everos-root{'/d' * depth}/leaf.txt",
            *expected_directories,
            "memory/everos-root",
        ]

        snapshot_leaf = (
            manager.snapshot_path(snapshot.snapshot_id)
            / "payload"
            / leaf.relative_to(home)
        )
        snapshot_leaf.write_bytes(b"corrupt deep bytes")
        snapshot_leaf.chmod(0o600)
        with pytest.raises(MemorySnapshotVerificationError):
            manager.verify(
                snapshot.snapshot_id,
                expected_manifest_sha256=snapshot.manifest_sha256,
                expected_surface_digests=snapshot.surface_digests(),
            )
        snapshot_leaf.write_bytes(b"sealed deep bytes")
        snapshot_leaf.chmod(0o600)

        outside = _private_file(home / "outside.txt", b"outside survives", home)
        snapshot_leaf.unlink()
        snapshot_leaf.symlink_to(outside)
        with pytest.raises(MemorySnapshotVerificationError):
            manager.verify(
                snapshot.snapshot_id,
                expected_manifest_sha256=snapshot.manifest_sha256,
                expected_surface_digests=snapshot.surface_digests(),
            )
        assert outside.read_bytes() == b"outside survives"
        snapshot_leaf.unlink()
        snapshot_leaf.write_bytes(b"sealed deep bytes")
        snapshot_leaf.chmod(0o600)

        leaf.write_bytes(b"new live bytes")
        manager.restore(
            snapshot.snapshot_id,
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        )
        assert leaf.read_bytes() == b"sealed deep bytes"

        leaf.unlink()
        leaf.symlink_to(outside)
        with pytest.raises(MemorySnapshotUnsafePathError):
            manager.create("deep-symlink")
        assert not manager.snapshot_path("deep-symlink").exists()
        assert outside.read_bytes() == b"outside survives"
        manager.restore(
            snapshot.snapshot_id,
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        )

        assert operation.execution_token is not None
        operation = journal.record_snapshot(
            operation.operation_id,
            expected_revision=operation.revision,
            execution_token=operation.execution_token,
            snapshot=snapshot,
        )
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
        MemoryClearSnapshotStorage(journal, manager).remove_terminal_snapshot(
            operation.operation_id
        )
    finally:
        sys.setrecursionlimit(previous_recursion_limit)

    assert not manager.snapshot_path(snapshot.snapshot_id).exists()


def test_streaming_manifest_index_stays_memory_bounded_beyond_old_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_dir = _private_directory(tmp_path / "snapshot", tmp_path)
    manifest_path = snapshot_dir / "manifest.jsonl"
    writer = snapshot_module._ManifestWriter(manifest_path)
    try:
        for index in range(100_001):
            writer.add(
                snapshot_module.SnapshotEntry(
                    path=f"memory/everos-root/items/{index:06d}.json",
                    type="file",
                    mode=0o600,
                    size=index,
                    sha256="a" * 64,
                    tree_digest=None,
                )
            )
        writer.finish()
    finally:
        writer.close()

    assert manifest_path.stat().st_size > 8 * 1024 * 1024
    sqlite_temp = _private_directory(tmp_path / "sqlite-temp", tmp_path)
    monkeypatch.setenv("SQLITE_TMPDIR", str(sqlite_temp))
    tracemalloc.start()
    try:
        with snapshot_module._indexed_manifest(manifest_path) as (entries, digest):
            _current, peak = tracemalloc.get_traced_memory()
            assert entries.count() == 100_001
            assert entries.entry("memory/everos-root/items/000000.json") is not None
            assert entries.entry("memory/everos-root/items/100000.json") is not None
            assert digest == snapshot_module._file_sha256(manifest_path)
    finally:
        tracemalloc.stop()

    # Retaining the old tuple plus dictionaries consumes tens of MiB here.
    # The indexed reader holds one JSON record and a fixed SQLite page cache.
    assert peak < 8 * 1024 * 1024
    assert list(sqlite_temp.iterdir()) == []


def test_spilled_directory_order_is_exact_and_memory_bounded_for_wide_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_count = 20_001
    suffix = "x" * 180

    class FakeEntry:
        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

    class ReverseScandir:
        def __init__(self) -> None:
            self._next_index = entry_count - 1

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self) -> FakeEntry:
            if self._next_index < 0:
                raise StopIteration
            index = self._next_index
            self._next_index -= 1
            return FakeEntry(f"entry-{index:05d}-{suffix}")

    sqlite_temp = _private_directory(tmp_path / "sqlite-temp", tmp_path)
    monkeypatch.setenv("SQLITE_TMPDIR", str(sqlite_temp))
    monkeypatch.setattr(
        confined_filesystem_module.os,
        "scandir",
        lambda _descriptor: ReverseScandir(),
    )

    connection: sqlite3.Connection | None = None
    tracemalloc.start()
    try:
        with confined_filesystem_module.SpilledDirectoryOrder(
            insert_batch_size=11
        ) as orders:
            connection = orders._connection
            cursor = orders.scan(-1)
            for index in range(entry_count):
                assert orders.next_name(cursor) == f"entry-{index:05d}-{suffix}"
            assert orders.next_name(cursor) is None
            assert orders.next_name(cursor) is None
            _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Retaining and sorting these names requires more than 4 MiB. The spill
    # index holds a fixed insertion batch, one name per DFS frame, and a fixed cache.
    assert peak < 3 * 1024 * 1024
    assert connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    assert list(sqlite_temp.iterdir()) == []


def test_streaming_manifest_accepts_extended_unicode_path_record(tmp_path: Path) -> None:
    manifest_path = _private_directory(tmp_path / "snapshot", tmp_path) / "manifest.jsonl"
    relative_path = "memory/everos-root/" + "/".join(["\u754c" * 200] * 55)
    writer = snapshot_module._ManifestWriter(manifest_path)
    try:
        writer.add(
            snapshot_module.SnapshotEntry(
                path=relative_path,
                type="file",
                mode=0o600,
                size=1,
                sha256="a" * 64,
                tree_digest=None,
            )
        )
        writer.finish()
    finally:
        writer.close()

    lines = manifest_path.read_bytes().splitlines(keepends=True)
    assert len(lines[1]) > 64 * 1024
    with snapshot_module._indexed_manifest(manifest_path) as (entries, _digest):
        assert entries.entry(relative_path) is not None


def test_snapshot_accepts_path_record_beyond_256k_and_clear_gc(
    tmp_path: Path,
) -> None:
    home = _private_directory(tmp_path / "home", tmp_path)
    source_root = _private_directory(home / "memory/everos-root", home)
    component_length = min(os.pathconf(source_root, "PC_NAME_MAX") - 1, 250)
    component_count = (256 * 1024 // (component_length + 1)) + 2
    components = [
        f"d{index:04d}-" + "x" * (component_length - 6)
        for index in range(component_count)
    ]
    relative_leaf = "memory/everos-root/" + "/".join(components) + "/leaf.bin"
    assert len(relative_leaf.encode()) > 256 * 1024
    _create_deep_file_at(source_root, components, "leaf.bin", b"deep payload")

    manager = MemorySnapshotManager(home)
    journal = MemoryClearJournal(home)
    operation = journal.start(
        operation_id="beyond-line-limit",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    snapshot = manager.create(operation.operation_id)
    assert manager.verify(
        snapshot.snapshot_id,
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=snapshot.surface_digests(),
    ) == snapshot
    with snapshot_module._indexed_manifest(
        manager.snapshot_path(snapshot.snapshot_id) / "manifest.jsonl"
    ) as (entries, _digest):
        assert entries.entry(relative_leaf) is not None

    _create_deep_file_at(source_root, components, "leaf.bin", b"changed")
    manager.restore(
        snapshot.snapshot_id,
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=snapshot.surface_digests(),
    )
    assert _read_deep_file_at(source_root, components, "leaf.bin") == b"deep payload"

    assert operation.execution_token is not None
    operation = journal.record_snapshot(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
        snapshot=snapshot,
    )
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
    MemoryClearSnapshotStorage(journal, manager).remove_terminal_snapshot(
        operation.operation_id
    )
    remove_confined_path(home, source_root)
    assert not manager.snapshot_path(snapshot.snapshot_id).exists()


def test_manifest_index_temp_database_closes_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_dir = _private_directory(tmp_path / "snapshot", tmp_path)
    manifest_path = snapshot_dir / "manifest.jsonl"
    writer = snapshot_module._ManifestWriter(manifest_path)
    try:
        writer.add(
            snapshot_module.SnapshotEntry(
                path="memory/everos-root/profile.json",
                type="file",
                mode=0o600,
                size=1,
                sha256="a" * 64,
                tree_digest=None,
            )
        )
        writer.finish()
    finally:
        writer.close()

    connections: list[sqlite3.Connection] = []
    real_init = snapshot_module._ManifestIndex.__init__

    def capture_connection(index) -> None:
        real_init(index)
        connections.append(index._connection)

    monkeypatch.setattr(snapshot_module._ManifestIndex, "__init__", capture_connection)
    with snapshot_module._indexed_manifest(manifest_path):
        pass
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[-1].execute("SELECT 1")

    with pytest.raises(SystemExit, match="injected cancellation"):
        with snapshot_module._indexed_manifest(manifest_path):
            raise SystemExit("injected cancellation")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[-1].execute("SELECT 1")

    manifest_path.write_bytes(manifest_path.read_bytes().splitlines(keepends=True)[0])
    with pytest.raises(MemorySnapshotVerificationError):
        with snapshot_module._indexed_manifest(manifest_path):
            pass
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[-1].execute("SELECT 1")
    assert set(snapshot_dir.iterdir()) == {manifest_path}


@pytest.mark.parametrize("damage", ["corrupt", "truncated", "appended", "symlink"])
def test_streaming_manifest_damage_fails_closed(tmp_path: Path, damage: str) -> None:
    home = tmp_path / "home"
    _private_file(home / "memory/everos-root/profile.json", b"profile", home)
    manager = MemorySnapshotManager(home)
    snapshot = manager.create(f"manifest-{damage}")
    manifest_path = manager.snapshot_path(snapshot.snapshot_id) / "manifest.jsonl"
    lines = manifest_path.read_bytes().splitlines(keepends=True)
    if damage == "corrupt":
        record = json.loads(lines[1])
        record["entry"]["size"] += 1
        lines[1] = (
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        manifest_path.write_bytes(b"".join(lines))
    elif damage == "truncated":
        manifest_path.write_bytes(b"".join(lines[:-1]))
    elif damage == "appended":
        manifest_path.write_bytes(b"".join(lines) + lines[1])
    else:
        target = manifest_path.with_name("manifest-real.jsonl")
        manifest_path.replace(target)
        manifest_path.symlink_to(target.name)
    manifest_path.chmod(0o600)

    with pytest.raises(MemorySnapshotVerificationError):
        manager.verify(
            snapshot.snapshot_id,
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        )


def test_ordinary_backup_includes_clear_audit_and_blocks_on_open_clear(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    queue_connection = _build_all_surfaces(home)
    queue_connection.close()
    journal = MemoryClearJournal(home)
    clear_manager = MemorySnapshotManager(home)
    _complete_clear_audit(journal, clear_manager, "completed-audit")

    backup_manager = MemorySnapshotManager._for_backup(
        home,
        operation_guard=journal.assert_backup_allowed,
    )
    backup = backup_manager.create("backup-01")

    assert backup.relative_path == "state/memory/backups/backup-01"
    roots = {entry.path: entry for entry in backup.entries}
    assert roots["state/memory/clear-journal.sqlite"].type == "sqlite"
    copied_journal = (
        backup_manager.snapshot_path(backup.snapshot_id)
        / "payload/state/memory/clear-journal.sqlite"
    )
    with sqlite3.connect(copied_journal) as connection:
        assert connection.execute(
            "SELECT state FROM clear_operation WHERE operation_id = 'completed-audit'"
        ).fetchone() == ("completed",)
        assert connection.execute(
            "SELECT event FROM clear_event ORDER BY event_id DESC LIMIT 1"
        ).fetchone() == ("completed",)

    journal.start(
        operation_id="open-clear",
        operator_ref="user:owner",
        pre_epoch=1,
        target_epoch=2,
    )
    with pytest.raises(ClearBackupBlocked):
        backup_manager.create("blocked-backup")
    with pytest.raises(ClearBackupBlocked):
        backup_manager.restore(
            backup.snapshot_id,
            expected_manifest_sha256=backup.manifest_sha256,
            expected_surface_digests=backup.surface_digests(),
        )


def test_snapshot_verify_fails_before_restore_on_tampered_payload(tmp_path: Path) -> None:
    home = tmp_path / "home"
    queue_connection = _build_all_surfaces(home)
    manager = MemorySnapshotManager(home)
    snapshot = manager.create("tamper")
    queue_connection.close()
    live_provider = home / "memory/everos-root/profiles/user.json"
    live_provider.write_bytes(b"live sentinel")

    copied_provider = (
        manager.snapshot_path("tamper")
        / "payload"
        / "memory/everos-root/profiles/user.json"
    )
    copied_provider.write_bytes(b"tampered")
    copied_provider.chmod(0o600)

    with pytest.raises(MemorySnapshotVerificationError):
        manager.verify(
            "tamper",
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        )
    with pytest.raises(MemorySnapshotVerificationError):
        manager.restore(
            "tamper",
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        )
    assert live_provider.read_bytes() == b"live sentinel"

    assert manager.snapshot_path("tamper").exists()


def test_restore_intent_failure_happens_before_any_live_replacement(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    queue_connection = _build_all_surfaces(home)
    manager = MemorySnapshotManager(home)
    snapshot = manager.create("intent-failure")
    queue_connection.close()
    provider = home / "memory/everos-root/profiles/user.json"
    provider.write_bytes(b"live generation")

    def reject_intent(_snapshot) -> None:
        raise OSError("journal unavailable")

    with pytest.raises(OSError, match="journal unavailable"):
        manager.restore(
            snapshot.snapshot_id,
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
            before_replace=reject_intent,
        )

    assert provider.read_bytes() == b"live generation"
    assert _sqlite_values(home / "state/memory/memory.sqlite") == [
        "queued-before-clear"
    ]
    assert not any(".restore-intent-failure-" in path.name for path in home.rglob("*"))


def test_snapshot_rejects_symlinks_special_files_and_public_modes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    provider = _private_directory(home / "memory/everos-root", home)
    outside = _private_file(home / "outside.txt", b"outside", home)
    link = provider / "escape"
    link.symlink_to(outside)
    manager = MemorySnapshotManager(home)

    with pytest.raises(MemorySnapshotUnsafePathError):
        manager.create("symlink")
    assert outside.read_bytes() == b"outside"
    assert not manager.snapshot_path("symlink").exists()

    link.unlink()
    public = _private_file(provider / "public.txt", b"public", home)
    public.chmod(0o644)
    with pytest.raises(MemorySnapshotUnsafePathError):
        manager.create("public-mode")
    public.unlink()

    hard_link = provider / "hard-link.txt"
    os.link(outside, hard_link)
    with pytest.raises(MemorySnapshotUnsafePathError):
        manager.create("hard-link")
    hard_link.unlink()

    if hasattr(os, "mkfifo"):
        fifo = provider / "pipe"
        os.mkfifo(fifo, mode=0o600)
        with pytest.raises(MemorySnapshotUnsafePathError):
            manager.create("fifo")
        fifo.unlink()


def test_snapshot_rejects_unsafe_ids_and_unmanifested_payload(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    manager = MemorySnapshotManager(home)

    with pytest.raises(ValueError):
        manager.create("../escape")

    snapshot = manager.create("extra")
    extra = manager.snapshot_path("extra") / "payload/unexpected"
    extra.write_bytes(b"not manifested")
    extra.chmod(0o600)
    with pytest.raises(MemorySnapshotVerificationError):
        manager.verify(
            "extra",
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        )


def test_snapshot_rejects_sqlite_sidecar_symlink_and_overlapping_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    queue = home / "state/memory/memory.sqlite"
    _sqlite_surface(queue, "queued", home)
    outside = _private_file(home / "outside-wal", b"outside", home)
    queue.with_name(f"{queue.name}-wal").symlink_to(outside)

    with pytest.raises(MemorySnapshotUnsafePathError):
        MemorySnapshotManager(home).create("unsafe-sidecar")
    with pytest.raises(ValueError):
        MemorySnapshotManager(home, snapshot_root="memory")


@pytest.mark.parametrize("unsafe_kind", ("symlink", "directory"))
def test_corrupt_call_log_snapshot_refuses_unsafe_sidecars(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    home = tmp_path / "home"
    call_log = _private_file(
        home / "memory/call-log/call-log.db",
        b"not-a-sqlite-database",
        home,
    )
    sidecar = call_log.with_name(f"{call_log.name}-wal")
    outside = _private_file(home / "outside-wal", b"outside", home)
    if unsafe_kind == "symlink":
        sidecar.symlink_to(outside)
    else:
        _private_directory(sidecar, home)
    manager = MemorySnapshotManager(home)

    with pytest.raises(MemorySnapshotUnsafePathError):
        manager.create(f"unsafe-call-log-{unsafe_kind}")

    assert not manager.snapshot_path(f"unsafe-call-log-{unsafe_kind}").exists()
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize(
    ("relative_path", "backup"),
    (
        ("state/memory/memory.sqlite", False),
        ("state/memory/clear-journal.sqlite", True),
    ),
)
def test_authoritative_sqlite_surfaces_still_reject_corrupt_databases(
    tmp_path: Path,
    relative_path: str,
    backup: bool,
) -> None:
    home = tmp_path / "home"
    _private_file(home / relative_path, b"not-a-sqlite-database", home)
    manager = (
        MemorySnapshotManager._for_backup(home, operation_guard=lambda: None)
        if backup
        else MemorySnapshotManager(home)
    )

    with pytest.raises(MemorySnapshotError):
        manager.create(f"corrupt-authority-{backup}")


def test_restore_retry_converges_after_crash_between_backup_and_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    queue_connection = _build_all_surfaces(home)
    manager = MemorySnapshotManager(home)
    snapshot = manager.create("crash-retry")
    queue_connection.close()
    queue = home / "state/memory/memory.sqlite"
    with sqlite3.connect(queue) as connection:
        connection.execute("DELETE FROM records")
        connection.execute("INSERT INTO records VALUES ('new-live-value')")

    real_replace = snapshot_module.os.replace
    crashed = False

    def crash_after_backup(source, destination, *args, **kwargs):
        nonlocal crashed
        real_replace(source, destination, *args, **kwargs)
        if (
            not crashed
            and Path(source).name == queue.name
            and ".before-restore-crash-retry-0-0" in Path(destination).name
        ):
            crashed = True
            raise SystemExit("injected restore crash")

    monkeypatch.setattr(snapshot_module.os, "replace", crash_after_backup)
    with pytest.raises(SystemExit):
        manager.restore(
            "crash-retry",
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests(),
        )
    monkeypatch.setattr(snapshot_module.os, "replace", real_replace)

    assert crashed
    assert not queue.exists()
    assert any(".before-restore-crash-retry-" in path.name for path in home.rglob("*"))

    manager.restore(
        "crash-retry",
        expected_manifest_sha256=snapshot.manifest_sha256,
        expected_surface_digests=snapshot.surface_digests(),
    )

    assert _sqlite_values(queue) == ["queued-before-clear"]
    assert not any(
        ".before-restore-crash-retry-" in path.name
        or ".restore-crash-retry-" in path.name
        for path in home.rglob("*")
    )


def test_create_retry_reclaims_stage_left_by_process_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    for index in range(6):
        _private_file(
            home / f"memory/everos-root/items/{index}.json",
            f"item-{index}".encode(),
            home,
        )
    monkeypatch.setattr(snapshot_module, "_MANIFEST_BATCH_SIZE", 2)
    manager = MemorySnapshotManager(home)
    real_replace = snapshot_module.os.replace

    def crash_before_publish(source, destination, *args, **kwargs):
        if Path(destination).name == "stage-crash":
            raise SystemExit("injected snapshot process death")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.os, "replace", crash_before_publish)
    with pytest.raises(SystemExit):
        manager.create("stage-crash")

    orphaned = list(manager.snapshot_root.glob(".stage-crash*.tmp"))
    assert len(orphaned) == 1

    monkeypatch.setattr(snapshot_module.os, "replace", real_replace)
    snapshot = manager.create("stage-crash")

    assert snapshot.snapshot_id == "stage-crash"
    with snapshot_module._indexed_manifest(
        manager.snapshot_path(snapshot.snapshot_id) / "manifest.jsonl"
    ) as (entries, _digest):
        assert entries.count() > snapshot_module._MANIFEST_BATCH_SIZE
    assert not list(manager.snapshot_root.glob(".stage-crash*.tmp"))


@pytest.mark.parametrize(
    "crash_boundary",
    (
        "queue-copy",
        "provider-copy",
        "call-log-copy",
        "attachments-copy",
        "before-publish",
        "after-publish",
    ),
)
def test_auto_id_backup_stage_reconcile_covers_copy_and_publish_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_boundary: str,
) -> None:
    home = tmp_path / "home"
    queue_connection = _build_all_surfaces(home)
    queue_connection.close()
    manager = MemorySnapshotManager._for_backup(home, operation_guard=lambda: None)
    real_snapshot_sqlite = MemorySnapshotManager._snapshot_sqlite
    real_snapshot_tree = MemorySnapshotManager._snapshot_tree
    real_replace = snapshot_module.os.replace

    sqlite_boundaries = {
        "state/memory/memory.sqlite": "queue-copy",
        "memory/call-log/call-log.db": "call-log-copy",
    }
    tree_boundaries = {
        "memory/everos-root": "provider-copy",
        "memory/attachments": "attachments-copy",
    }

    def crash_after_sqlite_copy(self, surface, source, destination):
        result = real_snapshot_sqlite(self, surface, source, destination)
        if sqlite_boundaries.get(surface.path) == crash_boundary:
            raise SystemExit(crash_boundary)
        return result

    def crash_after_tree_copy(self, surface, source, destination, manifest):
        result = real_snapshot_tree(self, surface, source, destination, manifest)
        if tree_boundaries.get(surface.path) == crash_boundary:
            raise SystemExit(crash_boundary)
        return result

    def crash_at_publish(source, destination, *args, **kwargs):
        is_publish = (
            Path(source).name.endswith(".tmp")
            and not Path(destination).name.startswith(".")
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        )
        if is_publish and crash_boundary == "before-publish":
            raise SystemExit(crash_boundary)
        result = real_replace(source, destination, *args, **kwargs)
        if is_publish and crash_boundary == "after-publish":
            raise SystemExit(crash_boundary)
        return result

    monkeypatch.setattr(MemorySnapshotManager, "_snapshot_sqlite", crash_after_sqlite_copy)
    monkeypatch.setattr(MemorySnapshotManager, "_snapshot_tree", crash_after_tree_copy)
    monkeypatch.setattr(snapshot_module.os, "replace", crash_at_publish)
    with pytest.raises(SystemExit, match=crash_boundary):
        manager.create()
    monkeypatch.setattr(snapshot_module.os, "replace", real_replace)

    stages = list(manager.snapshot_root.glob(".*.tmp"))
    published = [path for path in manager.snapshot_root.iterdir() if not path.name.startswith(".")]
    restarted = MemorySnapshotManager._for_backup(home, operation_guard=lambda: None)
    if crash_boundary == "after-publish":
        assert stages == []
        assert len(published) == 1
        assert restarted.reconcile_unpublished_backup_stages() == ()
        assert published[0].is_dir()
    else:
        assert len(stages) == 1
        stage_id = stages[0].name[1:-4]
        assert restarted.reconcile_unpublished_backup_stages() == (stage_id,)
        assert not stages[0].exists()
        assert published == []


@pytest.mark.parametrize("unsafe_kind", ("symlink", "fifo"))
def test_backup_stage_reconcile_refuses_unsafe_managed_candidates(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    home = _private_directory(tmp_path / "home", tmp_path)
    manager = MemorySnapshotManager._for_backup(home, operation_guard=lambda: None)
    backup_root = _private_directory(manager.snapshot_root, home)
    outside = _private_file(tmp_path / "outside.txt", b"outside", tmp_path)
    candidate = backup_root / f".{('a' * 32)}.tmp"
    if unsafe_kind == "symlink":
        candidate.symlink_to(outside)
    else:
        os.mkfifo(candidate, mode=0o600)

    with pytest.raises(MemorySnapshotUnsafePathError):
        manager.reconcile_unpublished_backup_stages()

    assert candidate.exists() or candidate.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_backup_stage_reconcile_preserves_published_and_unmanaged_entries(
    tmp_path: Path,
) -> None:
    home = _private_directory(tmp_path / "home", tmp_path)
    manager = MemorySnapshotManager._for_backup(home, operation_guard=lambda: None)
    published = manager.create("published-backup")
    unmanaged = _private_directory(manager.snapshot_root / ".manual.tmp", home)
    stage_id = "b" * 32
    stage = _private_directory(manager.snapshot_root / f".{stage_id}.tmp", home)
    _private_file(stage / "partial.bin", b"partial", home)

    assert manager.reconcile_unpublished_backup_stages() == (stage_id,)
    assert manager.snapshot_path(published.snapshot_id).is_dir()
    assert unmanaged.is_dir()
    assert not stage.exists()


def test_explicit_id_backup_retry_still_reclaims_its_deterministic_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_directory(tmp_path / "home", tmp_path)
    manager = MemorySnapshotManager._for_backup(home, operation_guard=lambda: None)
    real_replace = snapshot_module.os.replace

    def crash_before_publish(source, destination, *args, **kwargs):
        if Path(destination).name == "explicit-retry":
            raise SystemExit("injected backup process death")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.os, "replace", crash_before_publish)
    with pytest.raises(SystemExit):
        manager.create("explicit-retry")
    monkeypatch.setattr(snapshot_module.os, "replace", real_replace)

    backup = manager.create("explicit-retry")
    assert backup.snapshot_id == "explicit-retry"
    assert not (manager.snapshot_root / ".explicit-retry.tmp").exists()
