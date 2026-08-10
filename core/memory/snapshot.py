"""Crash-conscious snapshots for the durable Memory surfaces.

The manager deliberately knows paths, bytes, and integrity only.  It does not
own the maintenance fence or decide when clear may proceed.  SQLite inputs are
copied through the SQLite backup API so a live WAL is included in the resulting
standalone database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Literal, Mapping, Protocol, Sequence

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    DirectoryOrderCursor as _DirectoryOrderCursor,
    SpilledDirectoryOrder,
    ensure_private_directory as ensure_confined_private_directory,
    fsync_directory as fsync_confined_directory,
    remove_anchored_entry,
    remove_confined_path,
    replace_confined,
    required_no_follow_flag,
    strict_directory_open_flags,
    strict_file_create_flags,
    strict_file_read_flags,
)


SnapshotSurfaceKind = Literal["sqlite", "tree", "call_log"]
SnapshotEntryType = Literal["sqlite", "tree", "directory", "file", "missing"]

_SCHEMA_VERSION = 2
_MANIFEST_FILENAME = "manifest.jsonl"
_PAYLOAD_DIRNAME = "payload"
_MANIFEST_BATCH_SIZE = 1_000
# Keep aggregate writer memory fixed while allowing one record to scale with the
# current canonical path. Deep trees created through dir-fd operations can
# legitimately exceed host whole-path limits.
_MANIFEST_WRITE_BUFFER_BYTES = 256 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_DIRECTORY_ORDER_INSERT_BATCH_SIZE = 256
# Eviction only causes a path segment to be reopened; it never limits accepted
# tree depth. Two simultaneous caches therefore stay well below common fd limits.
_DIRECTORY_DESCRIPTOR_CACHE_SIZE = 48
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_AUTOMATIC_BACKUP_STAGE_RE = re.compile(r"\.([0-9a-f]{32})\.tmp\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CALL_LOG_FILESET: tuple[tuple[str, str], ...] = (
    ("database", ""),
    ("journal", "-journal"),
    ("shm", "-shm"),
    ("wal", "-wal"),
)


def _validated_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid effective-home-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid effective-home-relative path")
    canonical = path.as_posix()
    if canonical != value:
        raise ValueError("effective-home-relative path must be canonical")
    return canonical


class MemorySnapshotError(RuntimeError):
    """Base class for a refused or failed Memory snapshot operation."""


class MemorySnapshotUnsafePathError(MemorySnapshotError):
    """A managed path escaped confinement or contained an unsafe file type."""


class MemorySnapshotVerificationError(MemorySnapshotError):
    """A snapshot did not match its authenticated manifest."""


class _MemorySQLiteCorruptionError(MemorySnapshotError):
    """A SQLite source is readable as a file but not as a valid database."""


def _new_directory_order(
    *,
    error_type: type[MemorySnapshotError],
    message: str,
) -> SpilledDirectoryOrder:
    try:
        return SpilledDirectoryOrder(
            insert_batch_size=_directory_order_insert_batch_size()
        )
    except ConfinedFilesystemError as error:
        raise error_type(message) from error


def _scan_directory_order(
    orders: SpilledDirectoryOrder,
    descriptor: int,
    *,
    error_type: type[MemorySnapshotError],
    message: str,
    include: Callable[[str], bool] | None = None,
) -> _DirectoryOrderCursor:
    try:
        return orders.scan(descriptor, include=include)
    except ConfinedFilesystemError as error:
        raise error_type(message) from error


def _next_directory_name(
    orders: SpilledDirectoryOrder,
    cursor: _DirectoryOrderCursor,
    *,
    error_type: type[MemorySnapshotError],
    message: str,
) -> str | None:
    try:
        return orders.next_name(cursor)
    except ConfinedFilesystemError as error:
        raise error_type(message) from error


@dataclass(frozen=True, slots=True)
class SnapshotSurface:
    """One effective-home-relative surface owned by the snapshot manager."""

    path: str
    kind: SnapshotSurfaceKind

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validated_relative_path(self.path))
        if self.kind not in {"sqlite", "tree", "call_log"}:
            raise ValueError("unsupported Memory snapshot surface kind")


DEFAULT_MEMORY_SNAPSHOT_SURFACES: tuple[SnapshotSurface, ...] = (
    SnapshotSurface("state/memory/memory.sqlite", "sqlite"),
    SnapshotSurface("memory/everos-root", "tree"),
    SnapshotSurface("memory/call-log/call-log.db", "call_log"),
    SnapshotSurface("memory/attachments", "tree"),
)

DEFAULT_MEMORY_BACKUP_SURFACES: tuple[SnapshotSurface, ...] = (
    *DEFAULT_MEMORY_SNAPSHOT_SURFACES,
    SnapshotSurface("state/memory/clear-journal.sqlite", "sqlite"),
)


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """One manifest row.  Paths are always relative to the effective home."""

    path: str
    type: SnapshotEntryType
    mode: int
    size: int
    sha256: str | None
    tree_digest: str | None

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "type": self.type,
            "mode": self.mode,
            "size": self.size,
            "sha256": self.sha256,
            "tree_digest": self.tree_digest,
        }


@dataclass(frozen=True, slots=True)
class SnapshotSurfaceReceipt:
    """Verified pre-clear identity for one fixed Memory surface."""

    path: str
    present: bool
    pre_clear_digest: str | None
    snapshot_digest: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validated_relative_path(self.path))
        if not isinstance(self.present, bool):
            raise ValueError("Memory snapshot surface presence must be boolean")
        if self.present:
            _validated_sha256(self.pre_clear_digest)
            _validated_sha256(self.snapshot_digest)
        elif self.pre_clear_digest is not None or self.snapshot_digest is not None:
            raise ValueError("an absent Memory snapshot surface cannot have digests")


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """A verified snapshot reference suitable for a clear journal."""

    snapshot_id: str
    relative_path: str
    manifest_sha256: str
    # Only the fixed surface roots are retained. Full manifest entries live in
    # an operation-scoped on-disk index while verification or restore runs.
    entries: tuple[SnapshotEntry, ...]
    surface_receipts: tuple[SnapshotSurfaceReceipt, ...]

    def surface_digests(self) -> dict[str, str | None]:
        """Return the journal-persisted snapshot digest for every surface."""

        return {receipt.path: receipt.snapshot_digest for receipt in self.surface_receipts}


@dataclass(slots=True)
class _RestoreBackup:
    target: Path
    backup: Path
    created_this_run: bool


@dataclass(slots=True)
class _RestorePlan:
    surface: SnapshotSurface
    target: Path
    staged: Path | None
    installs: list[tuple[Path, Path]]
    backups: list[_RestoreBackup]
    installed_targets: list[Path]


@dataclass(frozen=True, slots=True)
class _RelativeNode:
    parent: _RelativeNode | None
    name: str


@dataclass(slots=True)
class _TreeCopyFrame:
    node: _RelativeNode | None
    entry_type: Literal["tree", "directory"]
    mode: int
    before: tuple[int, int, int, int]
    child_order: _DirectoryOrderCursor
    digest: _DigestWriter


@dataclass(slots=True)
class _DirectoryWalkFrame:
    node: _RelativeNode | None
    before: tuple[int, int, int, int]
    child_order: _DirectoryOrderCursor


class _DirectoryDescriptorCache:
    """Reopen deep paths safely while keeping descriptor use constant."""

    def __init__(
        self,
        root_fd: int,
        *,
        verification: bool = False,
        require_private: bool = True,
    ) -> None:
        self._root_fd = root_fd
        self._verification = verification
        self._require_private = require_private
        self._descriptors: OrderedDict[object, tuple[object, int]] = OrderedDict()
        self._maximum = _DIRECTORY_DESCRIPTOR_CACHE_SIZE

    def __enter__(self) -> "_DirectoryDescriptorCache":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for _identity, descriptor in self._descriptors.values():
            os.close(descriptor)
        self._descriptors.clear()

    def open(self, node: _RelativeNode | None, label: str) -> int:
        if node is None:
            return os.dup(self._root_fd)
        trail: list[_RelativeNode] = []
        ancestor = node
        cached: tuple[_RelativeNode, int] | None = None
        while ancestor is not None:
            cached = self._descriptors.get(id(ancestor))
            if cached is not None and cached[0] is ancestor:
                self._descriptors.move_to_end(id(ancestor))
                break
            trail.append(ancestor)
            ancestor = ancestor.parent
        current = os.dup(self._root_fd if cached is None else cached[1])
        try:
            for component_node in reversed(trail):
                try:
                    next_descriptor = os.open(
                        component_node.name,
                        _directory_open_flags(),
                        dir_fd=current,
                    )
                except OSError as error:
                    if self._verification:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot payload path is unsafe"
                        ) from error
                    raise MemorySnapshotUnsafePathError(
                        f"{label} cannot be opened safely"
                    ) from error
                os.close(current)
                current = next_descriptor
                self._validate(os.fstat(current), label)
                self._remember(id(component_node), component_node, current)
            return current
        except BaseException:
            os.close(current)
            raise

    def open_components(self, components: Sequence[str], label: str) -> int:
        target = tuple(components)
        prefix = target
        cached: tuple[object, int] | None = None
        while prefix:
            cached = self._descriptors.get(prefix)
            if cached is not None and cached[0] == prefix:
                self._descriptors.move_to_end(prefix)
                break
            prefix = prefix[:-1]
        current = os.dup(self._root_fd if cached is None else cached[1])
        try:
            for index in range(len(prefix), len(target)):
                try:
                    next_descriptor = os.open(
                        target[index],
                        _directory_open_flags(),
                        dir_fd=current,
                    )
                except OSError as error:
                    if self._verification:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot payload path is unsafe"
                        ) from error
                    raise MemorySnapshotUnsafePathError(
                        f"{label} cannot be opened safely"
                    ) from error
                os.close(current)
                current = next_descriptor
                self._validate(os.fstat(current), label)
                component_prefix = target[: index + 1]
                self._remember(component_prefix, component_prefix, current)
            return current
        except BaseException:
            os.close(current)
            raise

    def _validate(self, info: os.stat_result, label: str) -> None:
        if self._verification:
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise MemorySnapshotVerificationError(
                    "Memory snapshot payload path is unsafe"
                )
            try:
                _require_owned(info, label)
            except MemorySnapshotUnsafePathError as error:
                raise MemorySnapshotVerificationError(
                    "Memory snapshot payload path is unsafe"
                ) from error
            return
        if self._require_private:
            _require_directory_private(info, label)
        elif not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise MemorySnapshotUnsafePathError(f"{label} is not a safe directory")

    def _remember(self, key: object, identity: object, descriptor: int) -> None:
        previous = self._descriptors.pop(key, None)
        if previous is not None:
            os.close(previous[1])
        self._descriptors[key] = (identity, os.dup(descriptor))
        while len(self._descriptors) > self._maximum:
            _key, (_old_identity, old_descriptor) = self._descriptors.popitem(last=False)
            os.close(old_descriptor)


class _DigestWriter(Protocol):
    def update(self, value: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


class _ManifestWriter:
    """Write an authenticated manifest without retaining the full tree."""

    def __init__(self, path: Path) -> None:
        flags = strict_file_create_flags()
        self._descriptor = os.open(path, flags, 0o600)
        self._path = path
        self._buffer: list[bytes] = []
        self._buffer_bytes = 0
        self._entries_digest = hashlib.sha256()
        self._entry_count = 0
        self._finished = False
        try:
            header = _json_line(
                {
                    "format": "avibe-memory-snapshot",
                    "record": "header",
                    "schema_version": _SCHEMA_VERSION,
                }
            )
            _write_all(self._descriptor, header)
        except BaseException:
            os.close(self._descriptor)
            raise

    def add(self, entry: SnapshotEntry) -> None:
        if self._finished:
            raise RuntimeError("Memory snapshot manifest is already finished")
        record = _json_line({"entry": entry.payload(), "record": "entry"})
        self._entries_digest.update(record)
        self._entry_count += 1
        if self._buffer and (
            self._buffer_bytes + len(record) > _MANIFEST_WRITE_BUFFER_BYTES
            or len(self._buffer) >= _manifest_batch_size()
        ):
            self._flush()
        if len(record) > _MANIFEST_WRITE_BUFFER_BYTES:
            _write_all(self._descriptor, record)
            return
        self._buffer.append(record)
        self._buffer_bytes += len(record)

    def finish(self) -> None:
        if self._finished:
            return
        self._flush()
        footer = _json_line(
            {
                "entries_sha256": self._entries_digest.hexdigest(),
                "entry_count": self._entry_count,
                "record": "footer",
            }
        )
        _write_all(self._descriptor, footer)
        os.fchmod(self._descriptor, 0o600)
        os.fsync(self._descriptor)
        self._finished = True

    def close(self) -> None:
        os.close(self._descriptor)
        if self._finished:
            _fsync_directory(self._path.parent)

    def _flush(self) -> None:
        if not self._buffer:
            return
        _write_all(self._descriptor, b"".join(self._buffer))
        self._buffer.clear()
        self._buffer_bytes = 0


class _ManifestIndex:
    """Bounded-memory, automatically deleted index for manifest entries."""

    def __init__(self) -> None:
        # An empty SQLite filename creates an on-disk temporary database which
        # SQLite deletes automatically when this connection closes. It leaves
        # no named snapshot metadata behind after success, failure, or process
        # termination.
        self._connection = sqlite3.connect("")
        try:
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute("PRAGMA cache_size=-2048")
            self._connection.execute("PRAGMA journal_mode=OFF")
            self._connection.execute(
                """
                CREATE TABLE manifest_entry (
                    path TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    mode INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT,
                    tree_digest TEXT,
                    parent TEXT NOT NULL,
                    depth INTEGER NOT NULL
                ) WITHOUT ROWID
                """
            )
            self._connection.execute(
                "CREATE INDEX manifest_entry_parent ON manifest_entry(parent, path)"
            )
            self._connection.execute(
                "CREATE INDEX manifest_entry_depth ON manifest_entry(depth, path)"
            )
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> "_ManifestIndex":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[SnapshotEntry]:
        return self.entries()

    def close(self) -> None:
        self._connection.close()

    def add(self, entry: SnapshotEntry) -> None:
        path = PurePosixPath(entry.path)
        try:
            self._connection.execute(
                """
                INSERT INTO manifest_entry (
                    path, type, mode, size, sha256, tree_digest, parent, depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.path,
                    entry.type,
                    entry.mode,
                    entry.size,
                    entry.sha256,
                    entry.tree_digest,
                    path.parent.as_posix(),
                    len(path.parts),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise MemorySnapshotVerificationError(
                "Memory snapshot manifest contains duplicate paths"
            ) from error

    def finish(self) -> None:
        self._connection.commit()

    def count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM manifest_entry").fetchone()
        assert row is not None
        return int(row[0])

    def entry(self, path: str) -> SnapshotEntry | None:
        row = self._connection.execute(
            """
            SELECT path, type, mode, size, sha256, tree_digest
            FROM manifest_entry WHERE path = ?
            """,
            (path,),
        ).fetchone()
        return None if row is None else _entry_from_row(row)

    def entries(self) -> Iterator[SnapshotEntry]:
        rows = self._connection.execute(
            """
            SELECT path, type, mode, size, sha256, tree_digest
            FROM manifest_entry
            """
        )
        for row in rows:
            yield _entry_from_row(row)

    def descendants(
        self,
        path: str,
        *,
        entry_type: str | None = None,
        depth_order: Literal["ASC", "DESC"] | None = None,
    ) -> Iterator[SnapshotEntry]:
        lower, upper = _descendant_bounds(path)
        clauses = ["path >= ?", "path < ?"]
        parameters: list[object] = [lower, upper]
        if entry_type is not None:
            clauses.append("type = ?")
            parameters.append(entry_type)
        order = "path"
        if depth_order is not None:
            order = f"depth {depth_order}, path"
        rows = self._connection.execute(
            f"""
            SELECT path, type, mode, size, sha256, tree_digest
            FROM manifest_entry
            WHERE {' AND '.join(clauses)}
            ORDER BY {order}
            """,
            parameters,
        )
        for row in rows:
            yield _entry_from_row(row)

    def children(self, path: str) -> Iterator[SnapshotEntry]:
        rows = self._connection.execute(
            """
            SELECT path, type, mode, size, sha256, tree_digest
            FROM manifest_entry WHERE parent = ? ORDER BY path
            """,
            (path,),
        )
        for row in rows:
            yield _entry_from_row(row)

    def has_descendant(self, path: str) -> bool:
        lower, upper = _descendant_bounds(path)
        return (
            self._connection.execute(
                "SELECT 1 FROM manifest_entry WHERE path >= ? AND path < ? LIMIT 1",
                (lower, upper),
            ).fetchone()
            is not None
        )


class MemorySnapshotManager:
    """Create, verify, relocate, restore, and remove Memory snapshots."""

    def __init__(
        self,
        effective_home: Path | str,
        *,
        snapshot_root: Path | str = "state/memory/clear-snapshots",
        surfaces: Sequence[SnapshotSurface] = DEFAULT_MEMORY_SNAPSHOT_SURFACES,
        operation_guard: Callable[[], None] | None = None,
    ) -> None:
        self._effective_home = _absolute_without_resolve(effective_home)
        root_value = Path(snapshot_root)
        if root_value.is_absolute():
            self._snapshot_root = _absolute_without_resolve(root_value)
            self._snapshot_root_relative = _relative_to_home(
                self._snapshot_root,
                self._effective_home,
            ).as_posix()
        else:
            self._snapshot_root_relative = _validated_relative_path(root_value.as_posix())
            self._snapshot_root = self._effective_home / self._snapshot_root_relative

        if not surfaces:
            raise ValueError("at least one Memory snapshot surface is required")
        self._surfaces = tuple(surfaces)
        self._operation_guard = operation_guard
        self._validate_surface_layout()

    @classmethod
    def _for_backup(
        cls,
        effective_home: Path | str,
        *,
        operation_guard: Callable[[], None],
    ) -> "MemorySnapshotManager":
        """Build the low-level ordinary-backup manager.

        Only ``MemoryRuntime`` may expose this manager: it holds the runtime and
        provider-root maintenance fences for the full create/restore operation.
        """

        if not callable(operation_guard):
            raise TypeError("Memory backup requires a clear-journal guard")
        return cls(
            effective_home,
            snapshot_root="state/memory/backups",
            surfaces=DEFAULT_MEMORY_BACKUP_SURFACES,
            operation_guard=operation_guard,
        )

    @property
    def effective_home(self) -> Path:
        return self._effective_home

    @property
    def snapshot_root(self) -> Path:
        return self._snapshot_root

    @property
    def surfaces(self) -> tuple[SnapshotSurface, ...]:
        return self._surfaces

    def snapshot_path(self, snapshot_id: str) -> Path:
        return self._snapshot_root / _validated_snapshot_id(snapshot_id)

    def create(self, snapshot_id: str | None = None) -> MemorySnapshot:
        """Create and verify one all-surface snapshot before publishing it."""

        self._assert_operation_allowed()
        identifier = _validated_snapshot_id(snapshot_id or uuid.uuid4().hex)
        _ensure_private_directory(self._effective_home, self._effective_home)
        _ensure_private_directory(self._effective_home, self._snapshot_root)
        final = self.snapshot_path(identifier)
        _require_absent(final, "Memory snapshot already exists")
        # A process death bypasses exception cleanup. Reusing one operation-
        # scoped stage lets the explicit retry reclaim that unpublished copy
        # without scanning or trusting arbitrary entries in the snapshot root.
        stage = self._snapshot_root / f".{identifier}.tmp"
        _remove_safe_path(self._effective_home, stage)
        _fsync_directory(self._snapshot_root)
        payload_root = stage / _PAYLOAD_DIRNAME
        published = False
        try:
            _mkdir_private(stage)
            _mkdir_private(payload_root)
            manifest = _ManifestWriter(stage / _MANIFEST_FILENAME)
            try:
                for surface in self._surfaces:
                    source = self._effective_home / surface.path
                    destination = payload_root / surface.path
                    if surface.kind == "sqlite":
                        manifest.add(self._snapshot_sqlite(surface, source, destination))
                    elif surface.kind == "call_log":
                        self._snapshot_call_log(surface, source, destination, manifest)
                    else:
                        self._snapshot_tree(surface, source, destination, manifest)
                manifest.finish()
            finally:
                manifest.close()
            _fsync_directory(payload_root)
            _fsync_directory(stage)
            self._verify_directory(identifier, stage)
            _replace_safe_path(self._effective_home, stage, final)
            published = True
            _fsync_directory(self._snapshot_root)
            return self._verify_directory(identifier, final)
        except Exception:
            cleanup = final if published else stage
            try:
                _remove_safe_path(self._effective_home, cleanup)
                _fsync_directory(self._snapshot_root)
            except (FileNotFoundError, MemorySnapshotError, OSError):
                pass
            raise

    def reconcile_unpublished_backup_stages(self) -> tuple[str, ...]:
        """Remove abandoned auto-ID backup stages through the managed root fd."""

        if self._snapshot_root_relative != "state/memory/backups":
            raise RuntimeError("backup stage reconciliation requires the backup manager")
        self._assert_operation_allowed()
        root_info = _managed_source_info(self._effective_home, self._snapshot_root)
        if root_info is None:
            return ()
        _require_directory_private(root_info, "Memory backup root")
        root_fd = _open_directory(self._snapshot_root, "Memory backup root")
        removed: list[str] = []
        orders: SpilledDirectoryOrder | None = None
        try:
            orders = _new_directory_order(
                error_type=MemorySnapshotUnsafePathError,
                message="Memory backup root cannot be scanned safely",
            )
            candidates = _scan_directory_order(
                orders,
                root_fd,
                error_type=MemorySnapshotUnsafePathError,
                message="Memory backup root cannot be scanned safely",
                include=lambda name: _AUTOMATIC_BACKUP_STAGE_RE.fullmatch(name)
                is not None,
            )
            while True:
                name = _next_directory_name(
                    orders,
                    candidates,
                    error_type=MemorySnapshotUnsafePathError,
                    message="Memory backup root cannot be scanned safely",
                )
                if name is None:
                    break
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except OSError as error:
                    raise MemorySnapshotUnsafePathError(
                        "Memory backup stage cannot be inspected safely"
                    ) from error
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise MemorySnapshotUnsafePathError(
                        "Memory backup stage is not a safe directory"
                    )
                _require_directory_private(info, "Memory backup stage")
                _remove_safe_entry(
                    root_fd,
                    name,
                    expected_identity=(info.st_dev, info.st_ino),
                )
                removed.append(name[1:-4])
            if removed:
                os.fsync(root_fd)
            return tuple(removed)
        finally:
            if orders is not None:
                orders.close()
            os.close(root_fd)

    def verify(
        self,
        snapshot_id: str,
        *,
        expected_manifest_sha256: str,
        expected_surface_digests: Mapping[str, str | None],
    ) -> MemorySnapshot:
        """Verify the manifest, every byte, every mode, and every tree digest."""

        self._require_no_follow()
        identifier = _validated_snapshot_id(snapshot_id)
        directory = self.snapshot_path(identifier)
        with self._verified_directory(identifier, directory) as (snapshot, _index):
            self._verify_expected_snapshot(
                snapshot,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_surface_digests=expected_surface_digests,
            )
            return snapshot

    def restore(
        self,
        snapshot_id: str,
        *,
        expected_manifest_sha256: str,
        expected_surface_digests: Mapping[str, str | None],
        before_replace: Callable[[MemorySnapshot], None] | None = None,
    ) -> MemorySnapshot:
        """Restore every surface relative to this manager's effective home.

        The complete snapshot is verified before any target is touched.  All
        replacement payloads are staged first, and an in-process failure rolls
        already-swapped targets back. ``before_replace`` runs after staging and
        before the first target mutation so the caller can publish its durable
        crash-recovery intent while holding the process-wide maintenance fence.
        """

        self._assert_operation_allowed()
        identifier = _validated_snapshot_id(snapshot_id)
        snapshot_dir = self.snapshot_path(identifier)
        with self._verified_directory(identifier, snapshot_dir) as (snapshot, manifest_index):
            self._verify_expected_snapshot(
                snapshot,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_surface_digests=expected_surface_digests,
            )
            payload_root = snapshot_dir / _PAYLOAD_DIRNAME
            plans: list[_RestorePlan] = []
            token = snapshot.snapshot_id
            try:
                for index, surface in enumerate(self._surfaces):
                    target = self._effective_home / surface.path
                    _ensure_private_directory(self._effective_home, target.parent)
                    root_entry = manifest_index.entry(surface.path)
                    assert root_entry is not None
                    staged: Path | None = None
                    if root_entry.type != "missing":
                        staged = target.parent / f".{target.name}.restore-{token}-{index}"
                        _remove_safe_path(self._effective_home, staged)
                    plan = _RestorePlan(
                        surface=surface,
                        target=target,
                        staged=staged,
                        installs=[],
                        backups=[],
                        installed_targets=[],
                    )
                    plans.append(plan)
                    if staged is not None:
                        if root_entry.type == "sqlite":
                            _copy_verified_file(
                                payload_root / surface.path,
                                staged,
                                confinement_home=self._effective_home,
                                expected_sha256=root_entry.sha256,
                                expected_size=root_entry.size,
                                mode=root_entry.mode,
                            )
                            plan.installs.append((staged, target))
                        else:
                            self._stage_tree_restore(
                                surface,
                                root_entry,
                                manifest_index,
                                payload_root,
                                staged,
                            )
                            if surface.kind == "call_log":
                                for member, suffix in _CALL_LOG_FILESET:
                                    if manifest_index.entry(f"{surface.path}/{member}") is not None:
                                        plan.installs.append(
                                            (
                                                staged / member,
                                                target.with_name(f"{target.name}{suffix}"),
                                            )
                                        )
                            else:
                                plan.installs.append((staged, target))

                if before_replace is not None:
                    before_replace(snapshot)

                for index, plan in enumerate(plans):
                    candidates = [plan.target]
                    if plan.surface.kind in {"sqlite", "call_log"}:
                        candidates.extend(_sqlite_sidecars(plan.target))
                    for candidate_index, candidate in enumerate(candidates):
                        backup = candidate.parent / (
                            f".{candidate.name}.before-restore-{token}-{index}-{candidate_index}"
                        )
                        try:
                            backup_info = os.lstat(backup)
                        except FileNotFoundError:
                            backup_info = None
                        if backup_info is not None:
                            _require_expected_target(
                                backup_info,
                                plan.surface.kind,
                                sidecar=candidate_index > 0,
                            )
                            plan.backups.append(_RestoreBackup(candidate, backup, False))
                        try:
                            info = os.lstat(candidate)
                        except FileNotFoundError:
                            continue
                        _require_expected_target(
                            info,
                            plan.surface.kind,
                            sidecar=candidate_index > 0,
                        )
                        if backup_info is None:
                            _replace_safe_path(self._effective_home, candidate, backup)
                            plan.backups.append(_RestoreBackup(candidate, backup, True))
                        else:
                            _remove_safe_path(self._effective_home, candidate)
                    for staged_file, installed_target in plan.installs:
                        _replace_safe_path(
                            self._effective_home,
                            staged_file,
                            installed_target,
                        )
                        plan.installed_targets.append(installed_target)
                    _fsync_directory(plan.target.parent)
            except Exception:
                self._rollback_restore(plans)
                raise

            for plan in plans:
                for backup in plan.backups:
                    _remove_safe_path(self._effective_home, backup.backup)
                if plan.staged is not None:
                    _remove_safe_path(self._effective_home, plan.staged)
                _fsync_directory(plan.target.parent)
            return snapshot

    def _assert_operation_allowed(self) -> None:
        self._require_no_follow()
        if self._operation_guard is not None:
            self._operation_guard()

    @staticmethod
    def _require_no_follow() -> None:
        try:
            required_no_follow_flag()
        except ConfinedFilesystemError as error:
            raise MemorySnapshotUnsafePathError(
                "Memory snapshots require no-follow filesystem support"
            ) from error

    def _remove_clear_snapshot(
        self,
        snapshot_id: str,
        *,
        expected_relative_path: str,
        expected_manifest_sha256: str,
        expected_surface_digests: Mapping[str, str | None],
    ) -> None:
        """Remove one journal-authorized clear snapshot."""

        self._require_no_follow()
        identifier = _validated_snapshot_id(snapshot_id)
        directory = self.snapshot_path(identifier)
        tombstone = self._snapshot_root / f".{identifier}.gc"
        expected_relative = (
            PurePosixPath(self._snapshot_root_relative) / identifier
        ).as_posix()
        if expected_relative_path != expected_relative:
            raise MemorySnapshotVerificationError(
                "Memory snapshot removal path is invalid"
            )

        # A prior removal may have crashed after the verified snapshot was
        # renamed.  The deterministic tombstone is already authorized by this
        # permit and can be retried without requiring its partial tree to verify.
        if _managed_source_info(self._effective_home, tombstone) is not None:
            _remove_safe_path(self._effective_home, tombstone)
            _fsync_directory(self._snapshot_root)

        info = _managed_source_info(self._effective_home, directory)
        if info is None:
            return
        _require_directory_private(info, "Memory snapshot directory")
        self.verify(
            identifier,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_surface_digests=expected_surface_digests,
        )
        _replace_safe_path(self._effective_home, directory, tombstone)
        _fsync_directory(self._snapshot_root)
        _remove_safe_path(self._effective_home, tombstone)
        _fsync_directory(self._snapshot_root)

    def _discard_unrecorded_clear_snapshot(
        self,
        snapshot_id: str,
        *,
        expected_relative_path: str,
    ) -> None:
        """Discard one storage-authorized unrecorded clear snapshot."""

        self._require_no_follow()
        identifier = _validated_snapshot_id(snapshot_id)
        expected_relative = (
            PurePosixPath(self._snapshot_root_relative) / identifier
        ).as_posix()
        if expected_relative_path != expected_relative:
            raise MemorySnapshotVerificationError(
                "Memory snapshot discard path is invalid"
            )
        directory = self.snapshot_path(identifier)
        _remove_safe_path(self._effective_home, directory)
        root_info = _managed_source_info(self._effective_home, self._snapshot_root)
        if root_info is not None:
            _require_directory_private(root_info, "Memory snapshot root")
            _fsync_directory(self._snapshot_root)

    def _validate_surface_layout(self) -> None:
        paths = [PurePosixPath(surface.path) for surface in self._surfaces]
        if len(paths) != len(set(paths)):
            raise ValueError("Memory snapshot surfaces must be unique")
        snapshot_root = PurePosixPath(self._snapshot_root_relative)
        for index, path in enumerate(paths):
            if (
                path == snapshot_root
                or path in snapshot_root.parents
                or snapshot_root in path.parents
            ):
                raise ValueError("Memory snapshot root cannot overlap a managed surface")
            for other in paths[index + 1 :]:
                if path in other.parents or other in path.parents:
                    raise ValueError("Memory snapshot surfaces cannot overlap")

    def _snapshot_sqlite(
        self,
        surface: SnapshotSurface,
        source: Path,
        destination: Path,
    ) -> SnapshotEntry:
        info = _managed_source_info(self._effective_home, source)
        if info is None:
            return _missing_entry(surface.path)
        _require_regular_private(info, "Memory SQLite surface")
        for sidecar in _sqlite_sidecars(source):
            try:
                sidecar_info = os.lstat(sidecar)
            except FileNotFoundError:
                continue
            _require_regular_private(sidecar_info, "Memory SQLite sidecar")
        _ensure_private_directory(self._effective_home, destination.parent)
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        source_conn: sqlite3.Connection | None = None
        destination_conn: sqlite3.Connection | None = None
        try:
            source_conn = sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True, timeout=5.0)
            source_conn.execute("PRAGMA query_only=ON")
            source_conn.execute("PRAGMA busy_timeout=5000")
            destination_conn = sqlite3.connect(destination)
            source_conn.backup(destination_conn)
            destination_conn.commit()
            check = destination_conn.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise _MemorySQLiteCorruptionError(
                    "Memory SQLite snapshot failed integrity check"
                )
        except sqlite3.Error as error:
            raise MemorySnapshotError("Memory SQLite snapshot failed") from error
        finally:
            if destination_conn is not None:
                destination_conn.close()
            if source_conn is not None:
                source_conn.close()
        after_info = os.lstat(source)
        after = (after_info.st_dev, after_info.st_ino, after_info.st_size, after_info.st_mtime_ns)
        if before != after or not stat.S_ISREG(after_info.st_mode):
            raise MemorySnapshotError("Memory SQLite surface changed during snapshot")
        mode = stat.S_IMODE(info.st_mode)
        os.chmod(destination, mode)
        _fsync_file(destination)
        _fsync_directory(destination.parent)
        destination_info = os.lstat(destination)
        return SnapshotEntry(
            path=surface.path,
            type="sqlite",
            mode=mode,
            size=destination_info.st_size,
            sha256=_file_sha256(destination),
            tree_digest=None,
        )

    def _snapshot_call_log(
        self,
        surface: SnapshotSurface,
        source: Path,
        destination: Path,
        manifest: _ManifestWriter,
    ) -> SnapshotEntry:
        """Snapshot a diagnostic call log without weakening SQLite surfaces.

        Healthy call logs retain the standalone SQLite-backup representation.
        A positively identified corrupt database instead uses a fixed raw file
        set so Clear can preserve the diagnostic bytes for an exact Abort.
        """

        source_state = _call_log_fileset_state(self._effective_home, source)
        if not source_state:
            entry = _missing_entry(surface.path)
            manifest.add(entry)
            return entry
        if "database" in source_state:
            if not _call_log_has_sqlite_header(self._effective_home, source):
                return self._snapshot_call_log_fileset(
                    surface,
                    source,
                    destination,
                    manifest,
                )
            try:
                entry = self._snapshot_sqlite(surface, source, destination)
            except MemorySnapshotError as error:
                if not _is_sqlite_corruption(error):
                    raise
                _remove_safe_path(self._effective_home, destination)
                for sidecar in _sqlite_sidecars(destination):
                    _remove_safe_path(self._effective_home, sidecar)
            else:
                manifest.add(entry)
                return entry
        return self._snapshot_call_log_fileset(
            surface,
            source,
            destination,
            manifest,
        )

    def _snapshot_call_log_fileset(
        self,
        surface: SnapshotSurface,
        source: Path,
        destination: Path,
        manifest: _ManifestWriter,
    ) -> SnapshotEntry:
        _ensure_private_directory(self._effective_home, destination.parent)
        _mkdir_private(destination)
        source_fd = _open_directory(source.parent, "Memory call-log directory")
        destination_fd = _open_directory(
            destination,
            "Memory call-log snapshot destination",
        )
        try:
            before = _call_log_fileset_state_at(source_fd, source.name)
            if not before:
                raise MemorySnapshotError("Memory call-log changed during snapshot")
            root_template = SnapshotEntry(
                path=surface.path,
                type="tree",
                mode=0o700,
                size=0,
                sha256=None,
                tree_digest=None,
            )
            digest = _tree_digest(root_template)
            for member, suffix in _CALL_LOG_FILESET:
                info = before.get(member)
                if info is None:
                    continue
                size, file_digest = _copy_regular_file_at(
                    source_fd,
                    destination_fd,
                    f"{source.name}{suffix}",
                    destination_name=member,
                    mode=stat.S_IMODE(info.st_mode),
                )
                entry = SnapshotEntry(
                    path=f"{surface.path}/{member}",
                    type="file",
                    mode=stat.S_IMODE(info.st_mode),
                    size=size,
                    sha256=file_digest,
                    tree_digest=None,
                )
                manifest.add(entry)
                _update_tree_digest(digest, entry)
            after = _call_log_fileset_state_at(source_fd, source.name)
            if _call_log_fileset_identity(before) != _call_log_fileset_identity(after):
                raise MemorySnapshotError("Memory call-log changed during snapshot")
            os.fchmod(destination_fd, 0o700)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
            os.close(source_fd)
        root = SnapshotEntry(
            path=surface.path,
            type="tree",
            mode=0o700,
            size=0,
            sha256=None,
            tree_digest=digest.hexdigest(),
        )
        manifest.add(root)
        return root

    def _snapshot_tree(
        self,
        surface: SnapshotSurface,
        source: Path,
        destination: Path,
        manifest: _ManifestWriter,
    ) -> SnapshotEntry:
        info = _managed_source_info(self._effective_home, source)
        if info is None:
            entry = _missing_entry(surface.path)
            manifest.add(entry)
            return entry
        _require_directory_private(info, "Memory tree surface")
        _ensure_private_directory(self._effective_home, destination.parent)
        _mkdir_private(destination)
        source_root_fd = _open_directory(source, "Memory tree surface")
        destination_root_fd = _open_directory(destination, "Memory snapshot destination")
        source_cache = _DirectoryDescriptorCache(source_root_fd)
        destination_cache = _DirectoryDescriptorCache(destination_root_fd)
        orders: SpilledDirectoryOrder | None = None
        try:
            orders = _new_directory_order(
                error_type=MemorySnapshotError,
                message="Memory tree surface could not be read",
            )
            root_mode = stat.S_IMODE(info.st_mode)
            stack = [
                _tree_copy_frame(
                    source_cache,
                    orders,
                    None,
                    base_path=surface.path,
                    entry_type="tree",
                    mode=root_mode,
                    expected=info,
                )
            ]
            root_entry: SnapshotEntry | None = None
            while stack:
                frame = stack[-1]
                child_name = _next_directory_name(
                    orders,
                    frame.child_order,
                    error_type=MemorySnapshotError,
                    message="Memory tree surface could not be read",
                )
                if child_name is not None:
                    child_node = _RelativeNode(frame.node, child_name)
                    source_parent_fd = source_cache.open(
                        frame.node,
                        "Memory tree directory",
                    )
                    destination_parent_fd = destination_cache.open(
                        frame.node,
                        "Memory snapshot destination",
                    )
                    try:
                        child_info = os.stat(
                            child_name,
                            dir_fd=source_parent_fd,
                            follow_symlinks=False,
                        )
                        path_text = _node_manifest_path(surface.path, child_node)
                        if stat.S_ISLNK(child_info.st_mode):
                            raise MemorySnapshotUnsafePathError(
                                "Memory snapshot refuses symlinks"
                            )
                        if stat.S_ISDIR(child_info.st_mode):
                            _require_directory_private(
                                child_info,
                                "Memory tree directory",
                            )
                            _mkdir_private_at(destination_parent_fd, child_name)
                            stack.append(
                                _tree_copy_frame(
                                    source_cache,
                                    orders,
                                    child_node,
                                    base_path=surface.path,
                                    entry_type="directory",
                                    mode=stat.S_IMODE(child_info.st_mode),
                                    expected=child_info,
                                )
                            )
                            continue
                        if not stat.S_ISREG(child_info.st_mode):
                            raise MemorySnapshotUnsafePathError(
                                "Memory snapshot refuses special files"
                            )
                        _require_regular_private(child_info, "Memory tree file")
                        size, file_digest = _copy_regular_file_at(
                            source_parent_fd,
                            destination_parent_fd,
                            child_name,
                            mode=stat.S_IMODE(child_info.st_mode),
                        )
                    finally:
                        os.close(destination_parent_fd)
                        os.close(source_parent_fd)
                    entry = SnapshotEntry(
                        path=path_text,
                        type="file",
                        mode=stat.S_IMODE(child_info.st_mode),
                        size=size,
                        sha256=file_digest,
                        tree_digest=None,
                    )
                    manifest.add(entry)
                    _update_tree_digest(frame.digest, entry)
                    continue

                stack.pop()
                source_fd = source_cache.open(
                    frame.node,
                    "Memory tree directory",
                )
                destination_fd = destination_cache.open(
                    frame.node,
                    "Memory snapshot destination",
                )
                try:
                    after = os.fstat(source_fd)
                    if _directory_identity(after) != frame.before:
                        raise MemorySnapshotError(
                            "Memory tree directory changed during snapshot"
                        )
                    os.fchmod(destination_fd, frame.mode)
                    os.fsync(destination_fd)
                finally:
                    os.close(destination_fd)
                    os.close(source_fd)
                entry = SnapshotEntry(
                    path=_node_manifest_path(surface.path, frame.node),
                    type=frame.entry_type,
                    mode=frame.mode,
                    size=0,
                    sha256=None,
                    tree_digest=frame.digest.hexdigest(),
                )
                manifest.add(entry)
                if stack:
                    _update_tree_digest(stack[-1].digest, entry)
                else:
                    root_entry = entry
            assert root_entry is not None
            return root_entry
        finally:
            if orders is not None:
                orders.close()
            destination_cache.close()
            source_cache.close()
            os.close(destination_root_fd)
            os.close(source_root_fd)

    def _verify_directory(self, snapshot_id: str, directory: Path) -> MemorySnapshot:
        with self._verified_directory(snapshot_id, directory) as (snapshot, _index):
            return snapshot

    @contextmanager
    def _verified_directory(
        self,
        snapshot_id: str,
        directory: Path,
    ) -> Iterator[tuple[MemorySnapshot, _ManifestIndex]]:
        try:
            directory_info = _managed_source_info(self._effective_home, directory)
        except FileNotFoundError as error:
            raise MemorySnapshotVerificationError("Memory snapshot is missing") from error
        if directory_info is None:
            raise MemorySnapshotVerificationError("Memory snapshot is missing")
        _require_directory_private(directory_info, "Memory snapshot directory")
        manifest_path = directory / _MANIFEST_FILENAME
        with _indexed_manifest(manifest_path) as (manifest_index, manifest_sha256):
            roots = self._validate_manifest_surfaces(manifest_index)
            payload_root = directory / _PAYLOAD_DIRNAME
            try:
                payload_info = os.lstat(payload_root)
            except FileNotFoundError as error:
                raise MemorySnapshotVerificationError(
                    "Memory snapshot payload is missing"
                ) from error
            _require_directory_private(payload_info, "Memory snapshot payload")
            self._verify_payload(payload_root, manifest_index)
            receipts = tuple(
                SnapshotSurfaceReceipt(
                    path=surface.path,
                    present=root.type != "missing",
                    pre_clear_digest=root.sha256 or root.tree_digest,
                    snapshot_digest=root.sha256 or root.tree_digest,
                )
                for surface, root in zip(self._surfaces, roots, strict=True)
            )
            snapshot = MemorySnapshot(
                snapshot_id=snapshot_id,
                relative_path=(
                    PurePosixPath(self._snapshot_root_relative) / snapshot_id
                ).as_posix(),
                manifest_sha256=manifest_sha256,
                entries=roots,
                surface_receipts=receipts,
            )
            yield snapshot, manifest_index

    def _verify_expected_snapshot(
        self,
        snapshot: MemorySnapshot,
        *,
        expected_manifest_sha256: str,
        expected_surface_digests: Mapping[str, str | None],
    ) -> None:
        expected_manifest = _validated_sha256(expected_manifest_sha256)
        if snapshot.manifest_sha256 != expected_manifest:
            raise MemorySnapshotVerificationError("Memory snapshot manifest digest changed")
        self._verify_surface_digests(snapshot, expected_surface_digests)

    def _verify_surface_digests(
        self,
        snapshot: MemorySnapshot,
        expected: Mapping[str, str | None],
    ) -> None:
        expected_paths = {surface.path for surface in self._surfaces}
        if set(expected) != expected_paths:
            raise ValueError("expected Memory snapshot digests must cover every surface")
        receipts = {receipt.path: receipt for receipt in snapshot.surface_receipts}
        for surface in self._surfaces:
            expected_digest = expected[surface.path]
            if expected_digest is not None:
                expected_digest = _validated_sha256(expected_digest)
            actual_digest = receipts[surface.path].snapshot_digest
            if actual_digest != expected_digest:
                raise MemorySnapshotVerificationError("Memory snapshot surface digest changed")

    def _validate_manifest_surfaces(
        self,
        entries: _ManifestIndex,
    ) -> tuple[SnapshotEntry, ...]:
        roots: list[SnapshotEntry] = []
        for surface in self._surfaces:
            root = entries.entry(surface.path)
            allowed_root_types = (
                {"sqlite", "tree", "missing"}
                if surface.kind == "call_log"
                else {surface.kind, "missing"}
            )
            if root is None or root.type not in allowed_root_types:
                raise MemorySnapshotVerificationError("Memory snapshot surface root is invalid")
            if root.type == "missing" and entries.has_descendant(surface.path):
                raise MemorySnapshotVerificationError("Missing Memory surface has payload entries")
            if surface.kind == "call_log":
                descendants = tuple(entries.descendants(surface.path))
                if root.type == "tree":
                    expected_paths = {
                        f"{surface.path}/{member}" for member, _suffix in _CALL_LOG_FILESET
                    }
                    if not descendants or any(
                        entry.path not in expected_paths or entry.type != "file"
                        for entry in descendants
                    ):
                        raise MemorySnapshotVerificationError(
                            "Memory call-log snapshot file set is invalid"
                        )
                elif descendants:
                    raise MemorySnapshotVerificationError(
                        "Memory call-log SQLite surface has child entries"
                    )
            roots.append(root)
        for entry in entries:
            owner: SnapshotSurface | None = None
            entry_path = PurePosixPath(entry.path)
            for surface in self._surfaces:
                surface_path = PurePosixPath(surface.path)
                if entry_path == surface_path or surface_path in entry_path.parents:
                    if owner is not None:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot entry is outside its surfaces"
                        )
                    owner = surface
            if owner is None:
                raise MemorySnapshotVerificationError("Memory snapshot entry is outside its surfaces")
            if owner.kind == "sqlite" and entry.path != owner.path:
                raise MemorySnapshotVerificationError("Memory SQLite surface has child entries")
            if entry.path != owner.path and entry.type not in {"directory", "file"}:
                raise MemorySnapshotVerificationError("Memory tree child has an invalid type")
        return tuple(roots)

    def _verify_payload(self, payload_root: Path, entries: _ManifestIndex) -> None:
        payload_fd = _open_verification_directory(payload_root, "Memory snapshot payload")
        payload_cache = _DirectoryDescriptorCache(payload_fd, verification=True)
        try:
            for entry in entries:
                if entry.type == "missing":
                    continue
                parts = entry.path.split("/")
                parent_fd = payload_cache.open_components(
                    parts[:-1],
                    "Memory snapshot payload directory",
                )
                try:
                    try:
                        info = os.stat(
                            parts[-1],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError as error:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot payload entry is missing"
                        ) from error
                    mode = stat.S_IMODE(info.st_mode)
                    if mode != entry.mode:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot payload mode changed"
                        )
                    if entry.type in {"sqlite", "file"}:
                        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                            raise MemorySnapshotVerificationError(
                                "Memory snapshot file type changed"
                            )
                        if (
                            info.st_size != entry.size
                            or _file_sha256_at(parent_fd, parts[-1]) != entry.sha256
                        ):
                            raise MemorySnapshotVerificationError(
                                "Memory snapshot file digest changed"
                            )
                    else:
                        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                            raise MemorySnapshotVerificationError(
                                "Memory snapshot directory type changed"
                            )
                        if entry.size != 0:
                            raise MemorySnapshotVerificationError(
                                "Memory snapshot directory size is invalid"
                            )
                finally:
                    os.close(parent_fd)

            _verify_payload_paths(payload_fd, payload_cache, entries)
        finally:
            payload_cache.close()
            os.close(payload_fd)
        for entry in entries:
            if entry.type not in {"tree", "directory"}:
                continue
            digest = _tree_digest(entry)
            for child in entries.children(entry.path):
                _update_tree_digest(digest, child)
            if digest.hexdigest() != entry.tree_digest:
                raise MemorySnapshotVerificationError("Memory snapshot tree digest changed")

    def _stage_tree_restore(
        self,
        surface: SnapshotSurface,
        root: SnapshotEntry,
        entries: _ManifestIndex,
        payload_root: Path,
        staged: Path,
    ) -> None:
        _mkdir_private(staged)
        payload_fd = _open_verification_directory(payload_root, "Memory snapshot payload")
        staged_fd = _open_directory(staged, "Memory restore staging directory")
        payload_cache = _DirectoryDescriptorCache(payload_fd, verification=True)
        staged_cache = _DirectoryDescriptorCache(staged_fd)
        surface_parts = surface.path.split("/")
        try:
            for entry in entries.descendants(
                surface.path,
                entry_type="directory",
                depth_order="ASC",
            ):
                relative_parts = entry.path.split("/")[len(surface_parts) :]
                parent_fd = staged_cache.open_components(
                    relative_parts[:-1],
                    "Memory restore staging directory",
                )
                try:
                    _mkdir_private_at(parent_fd, relative_parts[-1])
                finally:
                    os.close(parent_fd)
            for entry in entries.descendants(surface.path, entry_type="file"):
                source_parts = entry.path.split("/")
                relative_parts = source_parts[len(surface_parts) :]
                source_parent_fd = payload_cache.open_components(
                    source_parts[:-1],
                    "Memory snapshot payload directory",
                )
                destination_parent_fd = staged_cache.open_components(
                    relative_parts[:-1],
                    "Memory restore staging directory",
                )
                try:
                    _copy_verified_file_at(
                        source_parent_fd,
                        destination_parent_fd,
                        source_parts[-1],
                        relative_parts[-1],
                        expected_sha256=entry.sha256,
                        expected_size=entry.size,
                        mode=entry.mode,
                    )
                finally:
                    os.close(destination_parent_fd)
                    os.close(source_parent_fd)
            for entry in entries.descendants(
                surface.path,
                entry_type="directory",
                depth_order="DESC",
            ):
                relative_parts = entry.path.split("/")[len(surface_parts) :]
                directory_fd = staged_cache.open_components(
                    relative_parts,
                    "Memory restore staging directory",
                )
                try:
                    os.fchmod(directory_fd, entry.mode)
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            os.fchmod(staged_fd, root.mode)
            os.fsync(staged_fd)
        finally:
            staged_cache.close()
            payload_cache.close()
            os.close(staged_fd)
            os.close(payload_fd)

    def _rollback_restore(self, plans: list[_RestorePlan]) -> None:
        for plan in reversed(plans):
            try:
                for installed_target in reversed(plan.installed_targets):
                    _remove_safe_path(self._effective_home, installed_target)
                for backup in reversed(plan.backups):
                    if backup.created_this_run and backup.backup.exists():
                        _replace_safe_path(
                            self._effective_home,
                            backup.backup,
                            backup.target,
                        )
                if plan.staged is not None:
                    _remove_safe_path(self._effective_home, plan.staged)
                _fsync_directory(plan.target.parent)
            except (MemorySnapshotError, OSError):
                continue


def _absolute_without_resolve(value: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _validated_snapshot_id(value: str) -> str:
    if not isinstance(value, str) or _SNAPSHOT_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid Memory snapshot id")
    return value


def _validated_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("invalid Memory snapshot SHA-256 digest")
    return value


def _relative_to_home(path: Path, home: Path) -> Path:
    try:
        return path.relative_to(home)
    except ValueError as error:
        raise ValueError("Memory snapshot path must stay within the effective home") from error


def _managed_source_info(home: Path, source: Path) -> os.stat_result | None:
    relative = _relative_to_home(source, home)
    current = home
    _require_directory(os.lstat(current), "effective Memory home")
    for index, component in enumerate(relative.parts):
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        if index < len(relative.parts) - 1:
            _require_directory(info, "Memory surface parent")
    return info


def _ensure_private_directory(home: Path, directory: Path) -> None:
    try:
        ensure_confined_private_directory(home, directory)
    except ConfinedFilesystemError as error:
        raise MemorySnapshotUnsafePathError(
            "Memory directory cannot be prepared safely"
        ) from error


def _mkdir_private(path: Path) -> None:
    os.mkdir(path, mode=0o700)
    os.chmod(path, 0o700)
    _require_directory_private(os.lstat(path), "Memory private directory")


def _directory_open_flags() -> int:
    return strict_directory_open_flags()


def _open_directory(path: Path, label: str) -> int:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as error:
        raise MemorySnapshotUnsafePathError(f"{label} cannot be opened safely") from error
    try:
        _require_directory_private(os.fstat(descriptor), label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_verification_directory(path: Path, label: str) -> int:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as error:
        raise MemorySnapshotVerificationError(f"{label} cannot be opened safely") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise MemorySnapshotVerificationError(f"{label} is not a private directory")
        _require_owned(info, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _mkdir_private_at(parent_fd: int, name: str) -> None:
    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        os.fchmod(descriptor, 0o700)
        _require_directory_private(os.fstat(descriptor), "Memory private directory")
    finally:
        os.close(descriptor)


def _node_components(node: _RelativeNode | None) -> list[str]:
    components: list[str] = []
    while node is not None:
        components.append(node.name)
        node = node.parent
    components.reverse()
    return components


def _node_manifest_path(base_path: str, node: _RelativeNode | None) -> str:
    components = _node_components(node)
    value = base_path if not components else f"{base_path}/{'/'.join(components)}"
    return _validated_relative_path(value)


def _directory_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns)


def _tree_copy_frame(
    source_cache: _DirectoryDescriptorCache,
    orders: SpilledDirectoryOrder,
    node: _RelativeNode | None,
    *,
    base_path: str,
    entry_type: Literal["tree", "directory"],
    mode: int,
    expected: os.stat_result,
) -> _TreeCopyFrame:
    descriptor = source_cache.open(
        node,
        "Memory tree directory",
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise MemorySnapshotError("Memory tree directory changed during snapshot")
        entry = SnapshotEntry(
            path=_node_manifest_path(base_path, node),
            type=entry_type,
            mode=mode,
            size=0,
            sha256=None,
            tree_digest=None,
        )
        return _TreeCopyFrame(
            node=node,
            entry_type=entry_type,
            mode=mode,
            before=_directory_identity(opened),
            child_order=_scan_directory_order(
                orders,
                descriptor,
                error_type=MemorySnapshotError,
                message="Memory tree surface could not be read",
            ),
            digest=_tree_digest(entry),
        )
    finally:
        os.close(descriptor)


def _require_absent(path: Path, message: str) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    raise MemorySnapshotUnsafePathError(message)


def _require_owned(info: os.stat_result, label: str) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise MemorySnapshotUnsafePathError(f"{label} is not owned by the current user")


def _require_directory(info: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MemorySnapshotUnsafePathError(f"{label} is not a safe directory")
    _require_owned(info, label)


def _require_directory_private(info: os.stat_result, label: str) -> None:
    _require_directory(info, label)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise MemorySnapshotUnsafePathError(f"{label} is not owner-only")


def _require_regular_private(info: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MemorySnapshotUnsafePathError(f"{label} is not a safe regular file")
    _require_owned(info, label)
    if info.st_nlink != 1:
        raise MemorySnapshotUnsafePathError(f"{label} has multiple hard links")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise MemorySnapshotUnsafePathError(f"{label} is not owner-only")


def _missing_entry(path: str) -> SnapshotEntry:
    return SnapshotEntry(path, "missing", 0, 0, None, None)


def _copy_regular_file(source: Path, destination: Path, *, mode: int) -> tuple[int, str]:
    flags = strict_file_read_flags()
    source_fd = os.open(source, flags)
    destination_fd: int | None = None
    try:
        before = os.fstat(source_fd)
        _require_regular_private(before, "Memory snapshot source file")
        destination_flags = strict_file_create_flags()
        destination_fd = os.open(destination, destination_flags, 0o600)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(destination_fd, chunk)
        after = os.fstat(source_fd)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable or size != before.st_size:
            raise MemorySnapshotError("Memory snapshot source changed during copy")
        os.fchmod(destination_fd, mode)
        os.fsync(destination_fd)
        return size, digest.hexdigest()
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        if destination.exists():
            _fsync_directory(destination.parent)


def _copy_regular_file_at(
    source_parent_fd: int,
    destination_parent_fd: int,
    source_name: str,
    *,
    mode: int,
    destination_name: str | None = None,
) -> tuple[int, str]:
    target_name = source_name if destination_name is None else destination_name
    source_fd = os.open(
        source_name,
        strict_file_read_flags(),
        dir_fd=source_parent_fd,
    )
    destination_fd: int | None = None
    created = False
    try:
        before = os.fstat(source_fd)
        _require_regular_private(before, "Memory snapshot source file")
        destination_fd = os.open(
            target_name,
            strict_file_create_flags(),
            0o600,
            dir_fd=destination_parent_fd,
        )
        created = True
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(destination_fd, chunk)
        after = os.fstat(source_fd)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable or size != before.st_size:
            raise MemorySnapshotError("Memory snapshot source changed during copy")
        os.fchmod(destination_fd, mode)
        os.fsync(destination_fd)
        return size, digest.hexdigest()
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        if created:
            os.fsync(destination_parent_fd)


def _copy_verified_file(
    source: Path,
    destination: Path,
    *,
    confinement_home: Path,
    expected_sha256: str | None,
    expected_size: int,
    mode: int,
) -> None:
    if expected_sha256 is None:
        raise MemorySnapshotVerificationError("Memory snapshot file has no digest")
    size, digest = _copy_regular_file(source, destination, mode=mode)
    if size != expected_size or digest != expected_sha256:
        _remove_safe_path(confinement_home, destination)
        raise MemorySnapshotVerificationError("Memory snapshot changed during restore staging")


def _copy_verified_file_at(
    source_parent_fd: int,
    destination_parent_fd: int,
    source_name: str,
    destination_name: str,
    *,
    expected_sha256: str | None,
    expected_size: int,
    mode: int,
) -> None:
    if expected_sha256 is None:
        raise MemorySnapshotVerificationError("Memory snapshot file has no digest")
    size, digest = _copy_regular_file_at(
        source_parent_fd,
        destination_parent_fd,
        source_name,
        destination_name=destination_name,
        mode=mode,
    )
    if size != expected_size or digest != expected_sha256:
        try:
            os.unlink(destination_name, dir_fd=destination_parent_fd)
        except FileNotFoundError:
            pass
        raise MemorySnapshotVerificationError(
            "Memory snapshot changed during restore staging"
        )


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short Memory snapshot write")
        view = view[written:]


@contextmanager
def _indexed_manifest(path: Path) -> Iterator[tuple[_ManifestIndex, str]]:
    with _ManifestIndex() as entries:
        manifest_sha256 = _read_manifest(path, entries)
        yield entries, manifest_sha256


def _read_manifest(path: Path, entries: _ManifestIndex) -> str:
    flags = strict_file_read_flags()
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MemorySnapshotVerificationError(
            "Memory snapshot manifest cannot be opened safely"
        ) from error
    try:
        before = os.fstat(descriptor)
        _require_regular_private(before, "Memory snapshot manifest")
        manifest_digest = hashlib.sha256()
        entries_digest = hashlib.sha256()
        entry_count = 0
        footer_seen = False
        line_number = 0
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            for line in stream:
                line_number += 1
                if not line.endswith(b"\n"):
                    raise MemorySnapshotVerificationError(
                        "Memory snapshot manifest record is invalid"
                    )
                manifest_digest.update(line)
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise MemorySnapshotVerificationError(
                        "Memory snapshot manifest is invalid"
                    ) from error
                if line_number == 1:
                    if record != {
                        "format": "avibe-memory-snapshot",
                        "record": "header",
                        "schema_version": _SCHEMA_VERSION,
                    }:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot manifest version is unsupported"
                        )
                    continue
                if footer_seen or not isinstance(record, dict):
                    raise MemorySnapshotVerificationError(
                        "Memory snapshot manifest shape is invalid"
                    )
                if record.get("record") == "entry":
                    if set(record) != {"entry", "record"}:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot manifest entry is invalid"
                        )
                    entries.add(_parse_manifest_entry(record["entry"]))
                    entry_count += 1
                    entries_digest.update(line)
                    continue
                if record.get("record") == "footer":
                    if set(record) != {"entries_sha256", "entry_count", "record"}:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot manifest footer is invalid"
                        )
                    footer_entry_count = record["entry_count"]
                    expected_digest = record["entries_sha256"]
                    if (
                        not isinstance(footer_entry_count, int)
                        or isinstance(footer_entry_count, bool)
                        or footer_entry_count < 0
                        or not isinstance(expected_digest, str)
                        or _SHA256_RE.fullmatch(expected_digest) is None
                        or footer_entry_count != entry_count
                        or expected_digest != entries_digest.hexdigest()
                    ):
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot manifest footer is invalid"
                        )
                    footer_seen = True
                    continue
                raise MemorySnapshotVerificationError(
                    "Memory snapshot manifest record is invalid"
                )
        after = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable:
            raise MemorySnapshotVerificationError("Memory snapshot manifest changed during read")
        if line_number == 0 or not footer_seen:
            raise MemorySnapshotVerificationError("Memory snapshot manifest is incomplete")
        entries.finish()
        return manifest_digest.hexdigest()
    except OSError as error:
        raise MemorySnapshotVerificationError("Memory snapshot manifest cannot be read") from error
    finally:
        os.close(descriptor)


def _json_line(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _manifest_batch_size() -> int:
    if (
        not isinstance(_MANIFEST_BATCH_SIZE, int)
        or isinstance(_MANIFEST_BATCH_SIZE, bool)
        or _MANIFEST_BATCH_SIZE <= 0
    ):
        raise MemorySnapshotError("Memory snapshot manifest batch size is invalid")
    return _MANIFEST_BATCH_SIZE


def _directory_order_insert_batch_size() -> int:
    if (
        not isinstance(_DIRECTORY_ORDER_INSERT_BATCH_SIZE, int)
        or isinstance(_DIRECTORY_ORDER_INSERT_BATCH_SIZE, bool)
        or _DIRECTORY_ORDER_INSERT_BATCH_SIZE <= 0
    ):
        raise MemorySnapshotError("Memory snapshot directory order batch size is invalid")
    return _DIRECTORY_ORDER_INSERT_BATCH_SIZE


def _parse_manifest_entry(raw: object) -> SnapshotEntry:
    expected_keys = {"path", "type", "mode", "size", "sha256", "tree_digest"}
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise MemorySnapshotVerificationError("Memory snapshot manifest entry is invalid")
    try:
        path = _validated_relative_path(raw["path"])
    except (TypeError, ValueError) as error:
        raise MemorySnapshotVerificationError("Memory snapshot path is invalid") from error
    entry_type = raw["type"]
    mode = raw["mode"]
    size = raw["size"]
    digest = raw["sha256"]
    tree_digest = raw["tree_digest"]
    if entry_type not in {"sqlite", "tree", "directory", "file", "missing"}:
        raise MemorySnapshotVerificationError("Memory snapshot entry type is invalid")
    if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777:
        raise MemorySnapshotVerificationError("Memory snapshot entry mode is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise MemorySnapshotVerificationError("Memory snapshot entry size is invalid")
    if digest is not None and (
        not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
    ):
        raise MemorySnapshotVerificationError("Memory snapshot file digest is invalid")
    if tree_digest is not None and (
        not isinstance(tree_digest, str) or _SHA256_RE.fullmatch(tree_digest) is None
    ):
        raise MemorySnapshotVerificationError("Memory snapshot tree digest is invalid")
    if entry_type in {"sqlite", "file"}:
        if digest is None or tree_digest is not None or mode & 0o077:
            raise MemorySnapshotVerificationError("Memory snapshot file metadata is invalid")
    elif entry_type in {"tree", "directory"}:
        if digest is not None or tree_digest is None or size != 0 or mode & 0o077:
            raise MemorySnapshotVerificationError("Memory snapshot directory metadata is invalid")
    elif any((mode, size, digest is not None, tree_digest is not None)):
        raise MemorySnapshotVerificationError("Missing Memory surface metadata is invalid")
    return SnapshotEntry(path, entry_type, mode, size, digest, tree_digest)


def _tree_digest(directory: SnapshotEntry) -> _DigestWriter:
    digest = hashlib.sha256()
    digest.update(f"path={directory.path}\ntype={directory.type}\n".encode("utf-8"))
    digest.update(f"mode={directory.mode:o}\n".encode("ascii"))
    return digest


def _update_tree_digest(digest: _DigestWriter, child: SnapshotEntry) -> None:
    child_digest = child.sha256 or child.tree_digest or ""
    name = PurePosixPath(child.path).name
    digest.update(
        f"{name}\0{child.type}\0{child.mode:o}\0{child.size}\0{child_digest}\n".encode(
            "utf-8"
        )
    )


def _verify_payload_paths(
    payload_root_fd: int,
    cache: _DirectoryDescriptorCache,
    entries: _ManifestIndex,
) -> None:
    root_info = os.fstat(payload_root_fd)
    orders = _new_directory_order(
        error_type=MemorySnapshotVerificationError,
        message="Memory snapshot payload cannot be read",
    )
    try:
        stack = [
            _DirectoryWalkFrame(
                node=None,
                before=_directory_identity(root_info),
                child_order=_scan_directory_order(
                    orders,
                    payload_root_fd,
                    error_type=MemorySnapshotVerificationError,
                    message="Memory snapshot payload cannot be read",
                ),
            )
        ]
        while stack:
            frame = stack[-1]
            child_name = _next_directory_name(
                orders,
                frame.child_order,
                error_type=MemorySnapshotVerificationError,
                message="Memory snapshot payload cannot be read",
            )
            if child_name is None:
                stack.pop()
                descriptor = cache.open(
                    frame.node,
                    "Memory snapshot payload directory",
                )
                try:
                    if _directory_identity(os.fstat(descriptor)) != frame.before:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot payload changed during verification"
                        )
                finally:
                    os.close(descriptor)
                continue

            child_node = _RelativeNode(frame.node, child_name)
            parent_fd = cache.open(
                frame.node,
                "Memory snapshot payload directory",
            )
            try:
                info = os.stat(
                    child_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise MemorySnapshotVerificationError(
                    "Memory snapshot payload cannot be read"
                ) from error
            finally:
                os.close(parent_fd)
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
            ):
                raise MemorySnapshotVerificationError(
                    "Memory snapshot payload has an unsafe entry"
                )
            try:
                if stat.S_ISREG(info.st_mode):
                    _require_regular_private(info, "Memory snapshot payload file")
                else:
                    _require_directory_private(info, "Memory snapshot payload directory")
            except MemorySnapshotUnsafePathError as error:
                raise MemorySnapshotVerificationError(
                    "Memory snapshot payload has an unsafe entry"
                ) from error
            path_text = _validated_relative_path("/".join(_node_components(child_node)))
            if entries.entry(path_text) is None and not (
                stat.S_ISDIR(info.st_mode) and entries.has_descendant(path_text)
            ):
                raise MemorySnapshotVerificationError(
                    "Memory snapshot payload has unmanifested entries"
                )
            if stat.S_ISDIR(info.st_mode):
                descriptor = cache.open(
                    child_node,
                    "Memory snapshot payload directory",
                )
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot payload changed during verification"
                        )
                    stack.append(
                        _DirectoryWalkFrame(
                            node=child_node,
                            before=_directory_identity(opened),
                            child_order=_scan_directory_order(
                                orders,
                                descriptor,
                                error_type=MemorySnapshotVerificationError,
                                message="Memory snapshot payload cannot be read",
                            ),
                        )
                    )
                finally:
                    os.close(descriptor)
    finally:
        orders.close()


def _descendant_bounds(path: str) -> tuple[str, str]:
    # Canonical manifest paths use '/' as the separator. Replacing that final
    # separator with its immediate successor creates an exact indexed range for
    # every descendant without relying on LIKE escaping.
    return f"{path}/", f"{path}0"


def _entry_from_row(row: Sequence[object]) -> SnapshotEntry:
    return SnapshotEntry(
        path=str(row[0]),
        type=row[1],  # type: ignore[arg-type]
        mode=int(row[2]),
        size=int(row[3]),
        sha256=None if row[4] is None else str(row[4]),
        tree_digest=None if row[5] is None else str(row[5]),
    )


def _file_sha256(path: Path) -> str:
    flags = strict_file_read_flags()
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        _require_regular_private(before, "Memory snapshot digest target")
        size = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable or size != before.st_size:
            raise MemorySnapshotVerificationError("Memory snapshot changed during digest")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _file_sha256_at(parent_fd: int, name: str) -> str:
    descriptor = os.open(
        name,
        strict_file_read_flags(),
        dir_fd=parent_fd,
    )
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        try:
            _require_regular_private(before, "Memory snapshot digest target")
        except MemorySnapshotUnsafePathError as error:
            raise MemorySnapshotVerificationError(
                "Memory snapshot file type changed"
            ) from error
        size = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable or size != before.st_size:
            raise MemorySnapshotVerificationError(
                "Memory snapshot changed during digest"
            )
    except OSError as error:
        raise MemorySnapshotVerificationError(
            "Memory snapshot file cannot be read safely"
        ) from error
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _is_sqlite_corruption(error: MemorySnapshotError) -> bool:
    if isinstance(error, _MemorySQLiteCorruptionError):
        return True
    cause = error.__cause__
    if not isinstance(cause, sqlite3.DatabaseError):
        return False
    error_code = getattr(cause, "sqlite_errorcode", None)
    return isinstance(error_code, int) and error_code & 0xFF in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }


def _call_log_fileset_state(
    home: Path,
    database: Path,
) -> dict[str, os.stat_result]:
    directory_info = _managed_source_info(home, database.parent)
    if directory_info is None:
        return {}
    _require_directory_private(directory_info, "Memory call-log directory")
    directory_fd = _open_directory(database.parent, "Memory call-log directory")
    try:
        return _call_log_fileset_state_at(directory_fd, database.name)
    finally:
        os.close(directory_fd)


def _call_log_fileset_state_at(
    directory_fd: int,
    database_name: str,
) -> dict[str, os.stat_result]:
    files: dict[str, os.stat_result] = {}
    for member, suffix in _CALL_LOG_FILESET:
        try:
            info = os.stat(
                f"{database_name}{suffix}",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        _require_regular_private(info, "Memory call-log file")
        files[member] = info
    return files


def _call_log_fileset_identity(
    files: Mapping[str, os.stat_result],
) -> dict[str, tuple[int, int, int, int, int, int, int]]:
    return {
        member: (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        for member, info in files.items()
    }


def _call_log_has_sqlite_header(home: Path, database: Path) -> bool:
    directory_info = _managed_source_info(home, database.parent)
    if directory_info is None:
        raise MemorySnapshotError("Memory call-log changed during snapshot")
    _require_directory_private(directory_info, "Memory call-log directory")
    directory_fd = _open_directory(database.parent, "Memory call-log directory")
    database_fd: int | None = None
    try:
        database_fd = os.open(
            database.name,
            strict_file_read_flags(),
            dir_fd=directory_fd,
        )
        before = os.fstat(database_fd)
        _require_regular_private(before, "Memory call-log file")
        header = os.read(database_fd, 16)
        after = os.fstat(database_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MemorySnapshotError("Memory call-log changed during snapshot")
        return header == b"SQLite format 3\x00"
    finally:
        if database_fd is not None:
            os.close(database_fd)
        os.close(directory_fd)


def _sqlite_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
        path.with_name(f"{path.name}-journal"),
    )


def _require_expected_target(info: os.stat_result, kind: SnapshotSurfaceKind, *, sidecar: bool) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise MemorySnapshotUnsafePathError("Memory restore refuses a symlink target")
    if sidecar or kind in {"sqlite", "call_log"}:
        _require_regular_private(info, "Memory restore file target")
    else:
        _require_directory_private(info, "Memory restore tree target")


def _remove_safe_path(
    home: Path,
    path: Path,
) -> None:
    try:
        remove_confined_path(home, path)
    except ConfinedFilesystemError as error:
        raise MemorySnapshotUnsafePathError(
            "Memory snapshot path could not be removed safely"
        ) from error


def _remove_safe_entry(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    try:
        remove_anchored_entry(
            parent_fd,
            name,
            expected_identity=expected_identity,
        )
    except ConfinedFilesystemError as error:
        raise MemorySnapshotUnsafePathError(
            "Memory snapshot entry could not be removed safely"
        ) from error


def _replace_safe_path(home: Path, source: Path, destination: Path) -> None:
    try:
        replace_confined(home, source, destination)
    except ConfinedFilesystemError as error:
        raise MemorySnapshotUnsafePathError(
            "Memory snapshot entry could not be replaced safely"
        ) from error


def _fsync_file(path: Path) -> None:
    flags = strict_file_read_flags()
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        fsync_confined_directory(path)
    except ConfinedFilesystemError as error:
        raise MemorySnapshotUnsafePathError(
            "Memory directory could not be synchronized safely"
        ) from error
