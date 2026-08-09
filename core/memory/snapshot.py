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
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Mapping, Protocol, Sequence


SnapshotSurfaceKind = Literal["sqlite", "tree"]
SnapshotEntryType = Literal["sqlite", "tree", "directory", "file", "missing"]

_SCHEMA_VERSION = 2
_MANIFEST_FILENAME = "manifest.jsonl"
_PAYLOAD_DIRNAME = "payload"
_MANIFEST_BATCH_SIZE = 1_000
# Covers the worst-case JSON escaping of a Windows extended-length path. The
# bound applies to one malformed record, never to total manifest bytes or rows.
_MAX_MANIFEST_LINE_BYTES = 256 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL_PERMIT_AUTHORITY = object()
_PREPARING_DISCARD_PERMIT_AUTHORITY = object()
_ACTIVE_PREPARING_DISCARD_LEASES: set[object] = set()
_SUCCEEDED_PREPARING_DISCARD_LEASES: set[object] = set()


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


@dataclass(frozen=True, slots=True)
class SnapshotSurface:
    """One effective-home-relative surface owned by the snapshot manager."""

    path: str
    kind: SnapshotSurfaceKind

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validated_relative_path(self.path))
        if self.kind not in {"sqlite", "tree"}:
            raise ValueError("unsupported Memory snapshot surface kind")


DEFAULT_MEMORY_SNAPSHOT_SURFACES: tuple[SnapshotSurface, ...] = (
    SnapshotSurface("state/memory/memory.sqlite", "sqlite"),
    SnapshotSurface("memory/everos-root", "tree"),
    SnapshotSurface("memory/call-log/call-log.db", "sqlite"),
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
    entries: tuple[SnapshotEntry, ...]
    surface_receipts: tuple[SnapshotSurfaceReceipt, ...]

    def surface_digests(self) -> dict[str, str | None]:
        """Return the journal-persisted snapshot digest for every surface."""

        return {receipt.path: receipt.snapshot_digest for receipt in self.surface_receipts}


@dataclass(frozen=True, slots=True)
class _TerminalSnapshotPermit:
    """Journal-issued capability for garbage-collecting a terminal snapshot."""

    snapshot_id: str
    relative_path: str
    manifest_sha256: str
    surface_digests: tuple[tuple[str, str | None], ...]
    _authority: object

    def __post_init__(self) -> None:
        if self._authority is not _TERMINAL_PERMIT_AUTHORITY:
            raise TypeError("terminal Memory snapshot permits are journal-issued")
        _validated_snapshot_id(self.snapshot_id)
        object.__setattr__(self, "relative_path", _validated_relative_path(self.relative_path))
        _validated_sha256(self.manifest_sha256)
        normalized: list[tuple[str, str | None]] = []
        for path, digest in self.surface_digests:
            canonical = _validated_relative_path(path)
            normalized.append((canonical, None if digest is None else _validated_sha256(digest)))
        if len({path for path, _digest in normalized}) != len(normalized):
            raise ValueError("terminal Memory snapshot permit has duplicate surfaces")
        object.__setattr__(self, "surface_digests", tuple(normalized))


def _issue_terminal_snapshot_permit(
    *,
    snapshot_id: str,
    relative_path: str,
    manifest_sha256: str,
    surface_digests: tuple[tuple[str, str | None], ...],
) -> _TerminalSnapshotPermit:
    """Issue the opaque capability used by an eligible terminal journal row."""

    return _TerminalSnapshotPermit(
        snapshot_id=snapshot_id,
        relative_path=relative_path,
        manifest_sha256=manifest_sha256,
        surface_digests=surface_digests,
        _authority=_TERMINAL_PERMIT_AUTHORITY,
    )


@dataclass(frozen=True, slots=True)
class _PreparingSnapshotDiscardPermit:
    snapshot_id: str
    relative_path: str
    _lease: object
    _authority: object

    def __post_init__(self) -> None:
        if self._authority is not _PREPARING_DISCARD_PERMIT_AUTHORITY:
            raise TypeError("preparing Memory snapshot discard permits are journal-issued")
        _validated_snapshot_id(self.snapshot_id)
        object.__setattr__(self, "relative_path", _validated_relative_path(self.relative_path))


def _issue_preparing_snapshot_discard_permit(
    *,
    snapshot_id: str,
    relative_path: str,
) -> _PreparingSnapshotDiscardPermit:
    lease = object()
    permit = _PreparingSnapshotDiscardPermit(
        snapshot_id=snapshot_id,
        relative_path=relative_path,
        _lease=lease,
        _authority=_PREPARING_DISCARD_PERMIT_AUTHORITY,
    )
    _ACTIVE_PREPARING_DISCARD_LEASES.add(lease)
    return permit


def _preparing_snapshot_discard_succeeded(permit: _PreparingSnapshotDiscardPermit) -> bool:
    return permit._lease in _SUCCEEDED_PREPARING_DISCARD_LEASES


def _revoke_preparing_snapshot_discard_permit(permit: _PreparingSnapshotDiscardPermit) -> None:
    _ACTIVE_PREPARING_DISCARD_LEASES.discard(permit._lease)
    _SUCCEEDED_PREPARING_DISCARD_LEASES.discard(permit._lease)


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
    backups: list[_RestoreBackup]
    installed: bool = False


class _DigestWriter(Protocol):
    def update(self, value: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


class _ManifestWriter:
    """Write an authenticated manifest without retaining the full tree."""

    def __init__(self, path: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self._descriptor = os.open(path, flags, 0o600)
        self._path = path
        self._buffer: list[bytes] = []
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
        if len(record) > _MAX_MANIFEST_LINE_BYTES:
            raise MemorySnapshotError("Memory snapshot manifest entry is too large")
        self._entries_digest.update(record)
        self._buffer.append(record)
        self._entry_count += 1
        if len(self._buffer) >= _manifest_batch_size():
            self._flush()

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
                    else:
                        self._snapshot_tree(surface, source, destination, manifest)
                manifest.finish()
            finally:
                manifest.close()
            _fsync_directory(payload_root)
            _fsync_directory(stage)
            self._verify_directory(identifier, stage)
            os.replace(stage, final)
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

    def verify(
        self,
        snapshot_id: str,
        *,
        expected_manifest_sha256: str,
        expected_surface_digests: Mapping[str, str | None],
    ) -> MemorySnapshot:
        """Verify the manifest, every byte, every mode, and every tree digest."""

        identifier = _validated_snapshot_id(snapshot_id)
        directory = self.snapshot_path(identifier)
        snapshot = self._verify_directory(identifier, directory)
        expected_manifest = _validated_sha256(expected_manifest_sha256)
        if snapshot.manifest_sha256 != expected_manifest:
            raise MemorySnapshotVerificationError("Memory snapshot manifest digest changed")
        self._verify_surface_digests(snapshot, expected_surface_digests)
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
        snapshot = self.verify(
            snapshot_id,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_surface_digests=expected_surface_digests,
        )
        entries_by_path = {entry.path: entry for entry in snapshot.entries}
        snapshot_dir = self.snapshot_path(snapshot.snapshot_id)
        payload_root = snapshot_dir / _PAYLOAD_DIRNAME
        plans: list[_RestorePlan] = []
        token = snapshot.snapshot_id
        try:
            for index, surface in enumerate(self._surfaces):
                target = self._effective_home / surface.path
                _ensure_private_directory(self._effective_home, target.parent)
                root_entry = entries_by_path[surface.path]
                staged: Path | None = None
                if root_entry.type != "missing":
                    staged = target.parent / f".{target.name}.restore-{token}-{index}"
                    _remove_safe_path(self._effective_home, staged)
                plan = _RestorePlan(surface, target, staged, [])
                plans.append(plan)
                if staged is not None:
                    if surface.kind == "sqlite":
                        _copy_verified_file(
                            payload_root / surface.path,
                            staged,
                            confinement_home=self._effective_home,
                            expected_sha256=root_entry.sha256,
                            expected_size=root_entry.size,
                            mode=root_entry.mode,
                        )
                    else:
                        self._stage_tree_restore(
                            surface,
                            root_entry,
                            snapshot.entries,
                            payload_root,
                            staged,
                        )

            if before_replace is not None:
                before_replace(snapshot)

            for index, plan in enumerate(plans):
                candidates = [plan.target]
                if plan.surface.kind == "sqlite":
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
                    _require_expected_target(info, plan.surface.kind, sidecar=candidate_index > 0)
                    if backup_info is None:
                        os.replace(candidate, backup)
                        plan.backups.append(_RestoreBackup(candidate, backup, True))
                    else:
                        _remove_safe_path(self._effective_home, candidate)
                if plan.staged is not None:
                    os.replace(plan.staged, plan.target)
                    plan.installed = True
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
        if self._operation_guard is not None:
            self._operation_guard()

    def remove(self, permit: _TerminalSnapshotPermit) -> None:
        """Remove only a snapshot authorized by an eligible terminal journal row."""

        if (
            not isinstance(permit, _TerminalSnapshotPermit)
            or permit._authority is not _TERMINAL_PERMIT_AUTHORITY
        ):
            raise TypeError("Memory snapshot removal requires a terminal journal permit")
        directory = self.snapshot_path(permit.snapshot_id)
        tombstone = self._snapshot_root / f".{permit.snapshot_id}.gc"
        expected_relative = (PurePosixPath(self._snapshot_root_relative) / permit.snapshot_id).as_posix()
        if permit.relative_path != expected_relative:
            raise MemorySnapshotVerificationError("Memory snapshot removal permit path is invalid")

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
            permit.snapshot_id,
            expected_manifest_sha256=permit.manifest_sha256,
            expected_surface_digests=dict(permit.surface_digests),
        )
        os.replace(directory, tombstone)
        _fsync_directory(self._snapshot_root)
        _remove_safe_path(self._effective_home, tombstone)
        _fsync_directory(self._snapshot_root)

    def discard_unrecorded(self, permit: _PreparingSnapshotDiscardPermit) -> None:
        """Discard an untrusted published snapshot under a live journal lease."""

        if (
            not isinstance(permit, _PreparingSnapshotDiscardPermit)
            or permit._authority is not _PREPARING_DISCARD_PERMIT_AUTHORITY
            or permit._lease not in _ACTIVE_PREPARING_DISCARD_LEASES
        ):
            raise TypeError("Memory snapshot discard requires an active preparing journal permit")
        expected_relative = (
            PurePosixPath(self._snapshot_root_relative) / permit.snapshot_id
        ).as_posix()
        if permit.relative_path != expected_relative:
            raise MemorySnapshotVerificationError("Memory snapshot discard permit path is invalid")
        _ACTIVE_PREPARING_DISCARD_LEASES.remove(permit._lease)
        directory = self.snapshot_path(permit.snapshot_id)
        _remove_safe_path(self._effective_home, directory)
        root_info = _managed_source_info(self._effective_home, self._snapshot_root)
        if root_info is not None:
            _require_directory_private(root_info, "Memory snapshot root")
            _fsync_directory(self._snapshot_root)
        _SUCCEEDED_PREPARING_DISCARD_LEASES.add(permit._lease)

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
                raise MemorySnapshotError("Memory SQLite snapshot failed integrity check")
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
        entry = self._copy_tree_children(
            source,
            destination,
            PurePosixPath(surface.path),
            SnapshotEntry(
                path=surface.path,
                type="tree",
                mode=stat.S_IMODE(info.st_mode),
                size=0,
                sha256=None,
                tree_digest=None,
            ),
            manifest,
        )
        os.chmod(destination, stat.S_IMODE(info.st_mode))
        _fsync_directory(destination)
        manifest.add(entry)
        return entry

    def _copy_tree_children(
        self,
        source: Path,
        destination: Path,
        relative: PurePosixPath,
        directory_entry: SnapshotEntry,
        manifest: _ManifestWriter,
    ) -> SnapshotEntry:
        before = os.lstat(source)
        _require_directory_private(before, "Memory tree directory")
        try:
            iterator = os.scandir(source)
        except OSError as error:
            raise MemorySnapshotError("Memory tree surface could not be read") from error
        with iterator:
            children = sorted(iterator, key=lambda item: item.name)
        digest = _tree_digest(directory_entry)
        for child in children:
            source_child = Path(child.path)
            destination_child = destination / child.name
            info = child.stat(follow_symlinks=False)
            child_relative = relative / child.name
            path_text = _validated_relative_path(child_relative.as_posix())
            if stat.S_ISLNK(info.st_mode):
                raise MemorySnapshotUnsafePathError("Memory snapshot refuses symlinks")
            if stat.S_ISDIR(info.st_mode):
                _require_directory_private(info, "Memory tree directory")
                _mkdir_private(destination_child)
                entry = self._copy_tree_children(
                    source_child,
                    destination_child,
                    child_relative,
                    SnapshotEntry(
                        path=path_text,
                        type="directory",
                        mode=stat.S_IMODE(info.st_mode),
                        size=0,
                        sha256=None,
                        tree_digest=None,
                    ),
                    manifest,
                )
                os.chmod(destination_child, stat.S_IMODE(info.st_mode))
                _fsync_directory(destination_child)
                manifest.add(entry)
                _update_tree_digest(digest, entry)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise MemorySnapshotUnsafePathError("Memory snapshot refuses special files")
            _require_regular_private(info, "Memory tree file")
            size, file_digest = _copy_regular_file(
                source_child,
                destination_child,
                mode=stat.S_IMODE(info.st_mode),
            )
            entry = SnapshotEntry(
                path=path_text,
                type="file",
                mode=stat.S_IMODE(info.st_mode),
                size=size,
                sha256=file_digest,
                tree_digest=None,
            )
            manifest.add(entry)
            _update_tree_digest(digest, entry)
        after = os.lstat(source)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_mtime_ns,
        )
        if not stable:
            raise MemorySnapshotError("Memory tree directory changed during snapshot")
        return replace(directory_entry, tree_digest=digest.hexdigest())

    def _verify_directory(self, snapshot_id: str, directory: Path) -> MemorySnapshot:
        try:
            directory_info = _managed_source_info(self._effective_home, directory)
        except FileNotFoundError as error:
            raise MemorySnapshotVerificationError("Memory snapshot is missing") from error
        if directory_info is None:
            raise MemorySnapshotVerificationError("Memory snapshot is missing")
        _require_directory_private(directory_info, "Memory snapshot directory")
        manifest_path = directory / _MANIFEST_FILENAME
        entries, manifest_sha256 = _read_manifest(manifest_path)
        self._validate_manifest_surfaces(entries)
        payload_root = directory / _PAYLOAD_DIRNAME
        try:
            payload_info = os.lstat(payload_root)
        except FileNotFoundError as error:
            raise MemorySnapshotVerificationError("Memory snapshot payload is missing") from error
        _require_directory_private(payload_info, "Memory snapshot payload")
        self._verify_payload(payload_root, entries)
        roots = {entry.path: entry for entry in entries}
        receipts = tuple(
            SnapshotSurfaceReceipt(
                path=surface.path,
                present=roots[surface.path].type != "missing",
                pre_clear_digest=roots[surface.path].sha256 or roots[surface.path].tree_digest,
                snapshot_digest=roots[surface.path].sha256 or roots[surface.path].tree_digest,
            )
            for surface in self._surfaces
        )
        return MemorySnapshot(
            snapshot_id=snapshot_id,
            relative_path=(PurePosixPath(self._snapshot_root_relative) / snapshot_id).as_posix(),
            manifest_sha256=manifest_sha256,
            entries=entries,
            surface_receipts=receipts,
        )

    def _verify_surface_digests(
        self,
        snapshot: MemorySnapshot,
        expected: Mapping[str, str | None],
    ) -> None:
        expected_paths = {surface.path for surface in self._surfaces}
        if set(expected) != expected_paths:
            raise ValueError("expected Memory snapshot digests must cover every surface")
        roots = {entry.path: entry for entry in snapshot.entries if entry.path in expected_paths}
        for surface in self._surfaces:
            expected_digest = expected[surface.path]
            if expected_digest is not None:
                expected_digest = _validated_sha256(expected_digest)
            entry = roots[surface.path]
            actual_digest = entry.sha256 or entry.tree_digest
            if actual_digest != expected_digest:
                raise MemorySnapshotVerificationError("Memory snapshot surface digest changed")

    def _validate_manifest_surfaces(self, entries: tuple[SnapshotEntry, ...]) -> None:
        by_path = {entry.path: entry for entry in entries}
        if len(by_path) != len(entries):
            raise MemorySnapshotVerificationError("Memory snapshot manifest contains duplicate paths")
        for surface in self._surfaces:
            root = by_path.get(surface.path)
            if root is None or root.type not in {surface.kind, "missing"}:
                raise MemorySnapshotVerificationError("Memory snapshot surface root is invalid")
        for entry in entries:
            owners = [
                surface
                for surface in self._surfaces
                if entry.path == surface.path
                or PurePosixPath(surface.path) in PurePosixPath(entry.path).parents
            ]
            if len(owners) != 1:
                raise MemorySnapshotVerificationError("Memory snapshot entry is outside its surfaces")
            owner = owners[0]
            if owner.kind == "sqlite" and entry.path != owner.path:
                raise MemorySnapshotVerificationError("Memory SQLite surface has child entries")
            if entry.path != owner.path and entry.type not in {"directory", "file"}:
                raise MemorySnapshotVerificationError("Memory tree child has an invalid type")
            if by_path[owner.path].type == "missing" and entry.path != owner.path:
                raise MemorySnapshotVerificationError("Missing Memory surface has payload entries")

    def _verify_payload(self, payload_root: Path, entries: tuple[SnapshotEntry, ...]) -> None:
        allowed: set[str] = set()
        expected_payload: set[str] = set()
        for entry in entries:
            if entry.type == "missing":
                continue
            expected_payload.add(entry.path)
            path = PurePosixPath(entry.path)
            allowed.add(path.as_posix())
            allowed.update(parent.as_posix() for parent in path.parents if parent.as_posix() != ".")
            target = payload_root / entry.path
            try:
                info = os.lstat(target)
            except FileNotFoundError as error:
                raise MemorySnapshotVerificationError("Memory snapshot payload entry is missing") from error
            mode = stat.S_IMODE(info.st_mode)
            if mode != entry.mode:
                raise MemorySnapshotVerificationError("Memory snapshot payload mode changed")
            if entry.type in {"sqlite", "file"}:
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise MemorySnapshotVerificationError("Memory snapshot file type changed")
                if info.st_size != entry.size or _file_sha256(target) != entry.sha256:
                    raise MemorySnapshotVerificationError("Memory snapshot file digest changed")
            else:
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise MemorySnapshotVerificationError("Memory snapshot directory type changed")
                if entry.size != 0:
                    raise MemorySnapshotVerificationError("Memory snapshot directory size is invalid")

        actual = _payload_paths(payload_root)
        extras = actual - allowed
        if extras:
            raise MemorySnapshotVerificationError("Memory snapshot payload has unmanifested entries")
        if not expected_payload.issubset(actual):
            raise MemorySnapshotVerificationError("Memory snapshot payload is incomplete")
        calculated = {entry.path: entry.tree_digest for entry in _with_tree_digests(entries)}
        for entry in entries:
            if entry.type in {"tree", "directory"} and calculated[entry.path] != entry.tree_digest:
                raise MemorySnapshotVerificationError("Memory snapshot tree digest changed")

    def _stage_tree_restore(
        self,
        surface: SnapshotSurface,
        root: SnapshotEntry,
        entries: tuple[SnapshotEntry, ...],
        payload_root: Path,
        staged: Path,
    ) -> None:
        _mkdir_private(staged)
        descendants = [
            entry
            for entry in entries
            if entry.path != surface.path
            and PurePosixPath(surface.path) in PurePosixPath(entry.path).parents
        ]
        for entry in sorted(descendants, key=lambda value: (len(PurePosixPath(value.path).parts), value.path)):
            relative = PurePosixPath(entry.path).relative_to(PurePosixPath(surface.path))
            target = staged.joinpath(*relative.parts)
            source = payload_root / entry.path
            if entry.type == "directory":
                _mkdir_private(target)
            elif entry.type == "file":
                _copy_verified_file(
                    source,
                    target,
                    confinement_home=self._effective_home,
                    expected_sha256=entry.sha256,
                    expected_size=entry.size,
                    mode=entry.mode,
                )
            else:
                raise MemorySnapshotVerificationError("Memory tree restore entry is invalid")
        for entry in sorted(
            (value for value in descendants if value.type == "directory"),
            key=lambda value: len(PurePosixPath(value.path).parts),
            reverse=True,
        ):
            relative = PurePosixPath(entry.path).relative_to(PurePosixPath(surface.path))
            directory = staged.joinpath(*relative.parts)
            os.chmod(directory, entry.mode)
            _fsync_directory(directory)
        os.chmod(staged, root.mode)
        _fsync_directory(staged)

    def _rollback_restore(self, plans: list[_RestorePlan]) -> None:
        for plan in reversed(plans):
            try:
                if plan.installed:
                    _remove_safe_path(self._effective_home, plan.target)
                for backup in reversed(plan.backups):
                    if backup.created_this_run and backup.backup.exists():
                        os.replace(backup.backup, backup.target)
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
    if directory != home:
        relative = _relative_to_home(directory, home)
    else:
        relative = Path()
    if not home.exists():
        home.mkdir(parents=True, mode=0o700)
        _require_directory(os.lstat(home), "effective Memory home")
        os.chmod(home, 0o700)
        _fsync_directory(home)
    current = home
    for component in relative.parts:
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, mode=0o700)
            _fsync_directory(current.parent)
            info = os.lstat(current)
        _require_directory(info, "Memory private directory")
        os.chmod(current, 0o700)
        _fsync_directory(current)
        if stat.S_IMODE(os.lstat(current).st_mode) != 0o700:
            raise MemorySnapshotUnsafePathError("Memory directory is not owner-only")


def _mkdir_private(path: Path) -> None:
    os.mkdir(path, mode=0o700)
    os.chmod(path, 0o700)
    _require_directory_private(os.lstat(path), "Memory private directory")


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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    destination_fd: int | None = None
    try:
        before = os.fstat(source_fd)
        _require_regular_private(before, "Memory snapshot source file")
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
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


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short Memory snapshot write")
        view = view[written:]


def _read_manifest(path: Path) -> tuple[tuple[SnapshotEntry, ...], str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
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
        entries: list[SnapshotEntry] = []
        footer_seen = False
        line_number = 0
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            while True:
                line = stream.readline(_MAX_MANIFEST_LINE_BYTES + 1)
                if not line:
                    break
                line_number += 1
                if len(line) > _MAX_MANIFEST_LINE_BYTES or not line.endswith(b"\n"):
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
                    entries.append(_parse_manifest_entry(record["entry"]))
                    entries_digest.update(line)
                    continue
                if record.get("record") == "footer":
                    if set(record) != {"entries_sha256", "entry_count", "record"}:
                        raise MemorySnapshotVerificationError(
                            "Memory snapshot manifest footer is invalid"
                        )
                    entry_count = record["entry_count"]
                    expected_digest = record["entries_sha256"]
                    if (
                        not isinstance(entry_count, int)
                        or isinstance(entry_count, bool)
                        or entry_count < 0
                        or not isinstance(expected_digest, str)
                        or _SHA256_RE.fullmatch(expected_digest) is None
                        or entry_count != len(entries)
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
        return tuple(entries), manifest_digest.hexdigest()
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


def _with_tree_digests(entries: Sequence[SnapshotEntry]) -> list[SnapshotEntry]:
    by_path = {entry.path: entry for entry in entries}
    children: dict[str, list[str]] = {}
    for path in by_path:
        parent = PurePosixPath(path).parent.as_posix()
        children.setdefault(parent, []).append(path)
    result = dict(by_path)
    directories = sorted(
        (entry for entry in entries if entry.type in {"tree", "directory"}),
        key=lambda entry: len(PurePosixPath(entry.path).parts),
        reverse=True,
    )
    for directory in directories:
        direct_children = [result[path] for path in children.get(directory.path, [])]
        result[directory.path] = _with_tree_digest(directory, direct_children)
    return [result[entry.path] for entry in entries]


def _with_tree_digest(
    directory: SnapshotEntry,
    children: Sequence[SnapshotEntry],
) -> SnapshotEntry:
    digest = _tree_digest(directory)
    for child in sorted(children, key=lambda entry: entry.path):
        _update_tree_digest(digest, child)
    return replace(directory, tree_digest=digest.hexdigest())


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


def _payload_paths(payload_root: Path) -> set[str]:
    found: set[str] = set()

    def walk(directory: Path, relative: PurePosixPath | None = None) -> None:
        try:
            children = os.scandir(directory)
        except OSError as error:
            raise MemorySnapshotVerificationError("Memory snapshot payload cannot be read") from error
        with children:
            for child in children:
                path = PurePosixPath(child.name) if relative is None else relative / child.name
                path_text = _validated_relative_path(path.as_posix())
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise MemorySnapshotVerificationError("Memory snapshot payload has an unsafe entry")
                if stat.S_ISREG(info.st_mode):
                    _require_regular_private(info, "Memory snapshot payload file")
                else:
                    _require_directory_private(info, "Memory snapshot payload directory")
                found.add(path_text)
                if stat.S_ISDIR(info.st_mode):
                    walk(Path(child.path), path)

    walk(payload_root)
    return found


def _file_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
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


def _sqlite_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
        path.with_name(f"{path.name}-journal"),
    )


def _require_expected_target(info: os.stat_result, kind: SnapshotSurfaceKind, *, sidecar: bool) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise MemorySnapshotUnsafePathError("Memory restore refuses a symlink target")
    if sidecar or kind == "sqlite":
        _require_regular_private(info, "Memory restore file target")
    else:
        _require_directory_private(info, "Memory restore tree target")


def _remove_safe_path(home: Path, path: Path) -> None:
    """Remove a confined entry through anchored, no-follow directory handles."""

    relative = _relative_to_home(path, home)
    if not relative.parts:
        raise MemorySnapshotUnsafePathError("refusing to remove the effective Memory home")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        current = os.open(home, flags)
        descriptors.append(current)
        for component in relative.parts[:-1]:
            try:
                current = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                return
            descriptors.append(current)
        _remove_entry_at(current, relative.parts[-1])
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _remove_entry_at(parent_fd: int, name: str) -> None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(before.st_mode) or stat.S_ISREG(before.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    if not stat.S_ISDIR(before.st_mode):
        raise MemorySnapshotUnsafePathError("Memory snapshot removal refuses special files")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    child_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(child_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MemorySnapshotUnsafePathError("Memory snapshot directory changed during removal")
        with os.scandir(child_fd) as entries:
            child_names = [entry.name for entry in entries]
        for child_name in child_names:
            _remove_entry_at(child_fd, child_name)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise MemorySnapshotUnsafePathError("Memory snapshot directory changed during removal")
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
