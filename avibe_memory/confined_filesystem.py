"""Shared filesystem primitives confined to an effective Memory home."""

from __future__ import annotations

import ctypes
import errno
import os
import sqlite3
import stat
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from config import paths


_DIRECTORY_ORDER_INSERT_BATCH_SIZE = 256
_DIRECTORY_DESCRIPTOR_CACHE_SIZE = 48
_LINUX_OPENAT2_SYSCALL = 437
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


class _LinuxOpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class ConfinedFilesystemError(RuntimeError):
    """A confined filesystem operation refused an unsafe path or entry."""


@dataclass(frozen=True, slots=True)
class ConfinedRoot:
    """Map one logical trust anchor and its children to one physical spelling."""

    logical_home: Path
    physical_home: Path

    @classmethod
    def from_home(cls, home: Path | str) -> "ConfinedRoot":
        logical_home = Path(
            os.path.abspath(os.path.expanduser(os.fspath(home)))
        )
        return cls(
            logical_home=logical_home,
            physical_home=paths.physical_home(logical_home),
        )

    def confine(self, path: Path | str) -> Path:
        """Return the same lexical child below the physical root.

        Only the trusted root is resolved. Descendant components remain lexical
        so a symlink planted below the root is still visible to no-follow opens.
        """

        candidate = Path(
            os.path.abspath(os.path.expanduser(os.fspath(path)))
        )
        for base in (self.logical_home, self.physical_home):
            if candidate.is_relative_to(base):
                return self.physical_home.joinpath(*candidate.relative_to(base).parts)
        raise ConfinedFilesystemError(
            "confined path must stay within the confinement root"
        )

    def confine_if_child(self, path: Path | str) -> Path | None:
        """Map a child when confined, preserving explicit external paths."""

        try:
            return self.confine(path)
        except ConfinedFilesystemError:
            return None


@dataclass(slots=True)
class ConfinedRemovalProgress:
    """Track entries removed before a confined deletion raises."""

    removed_entries: int = 0

    @property
    def changed(self) -> bool:
        return self.removed_entries > 0

    def record(self) -> None:
        self.removed_entries += 1


def required_no_follow_flag() -> int:
    """Return the host no-follow capability or disable Memory persistence."""

    flag = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(flag, int) or isinstance(flag, bool) or flag == 0:
        raise ConfinedFilesystemError(
            "Memory persistence requires strict no-follow filesystem support"
        )
    return flag


def strict_directory_open_flags() -> int:
    """Flags for a directory descriptor that must not follow its final entry."""

    return (
        os.O_RDONLY
        | required_no_follow_flag()
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def strict_file_read_flags(*, nonblocking: bool = False) -> int:
    """Flags for a read descriptor that must not follow its final entry."""

    flags = os.O_RDONLY | required_no_follow_flag() | int(getattr(os, "O_CLOEXEC", 0))
    if nonblocking:
        flags |= int(getattr(os, "O_NONBLOCK", 0))
    return flags


def strict_file_create_flags(*, read_write: bool = False) -> int:
    """Flags for exclusive creation of a no-follow regular file."""

    access = os.O_RDWR if read_write else os.O_WRONLY
    return (
        access
        | os.O_CREAT
        | os.O_EXCL
        | required_no_follow_flag()
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def ensure_private_directory(
    home: Path,
    directory: Path,
    *,
    harden_confinement_root: bool = True,
) -> None:
    """Create one owner-private directory chain through anchored descriptors."""

    required_no_follow_flag()
    relative = _relative_to_home(directory, home)
    if not home.exists():
        if not harden_confinement_root:
            raise ConfinedFilesystemError("confinement root does not exist")
        try:
            home.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise ConfinedFilesystemError(
                "confinement root cannot be created safely"
            ) from error
    try:
        current = os.open(home, strict_directory_open_flags())
    except OSError as error:
        raise ConfinedFilesystemError(
            "confinement root cannot be opened safely"
        ) from error
    try:
        if harden_confinement_root:
            _harden_private_directory_fd(current, "confinement root")
        else:
            _require_exact_private_directory(os.fstat(current), "confinement root")
        for component in relative.parts:
            try:
                child = os.open(
                    component,
                    strict_directory_open_flags(),
                    dir_fd=current,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ConfinedFilesystemError(
                        "private directory cannot be created safely"
                    ) from error
                try:
                    child = os.open(
                        component,
                        strict_directory_open_flags(),
                        dir_fd=current,
                    )
                except OSError as error:
                    raise ConfinedFilesystemError(
                        "private directory cannot be opened safely"
                    ) from error
            except OSError as error:
                raise ConfinedFilesystemError(
                    "private directory cannot be opened safely"
                ) from error
            try:
                _harden_private_directory_fd(child, "private directory")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
    finally:
        os.close(current)


def fsync_directory(path: Path) -> None:
    """Synchronize one strict, owner-private directory descriptor."""

    try:
        descriptor = os.open(path, strict_directory_open_flags())
    except OSError as error:
        raise ConfinedFilesystemError("directory cannot be opened safely") from error
    try:
        _require_private_directory(os.fstat(descriptor), "directory")
        os.fsync(descriptor)
    except OSError as error:
        raise ConfinedFilesystemError("directory cannot be synchronized safely") from error
    finally:
        os.close(descriptor)


def create_confined_file(
    home: Path,
    path: Path,
    *,
    mode: int = 0o600,
    read_write: bool = False,
) -> int:
    """Exclusively create one private regular file through its anchored parent."""

    relative = _relative_to_home(path, home)
    if not relative.parts:
        raise ConfinedFilesystemError("confined file must be below the confinement root")
    root = _open_confined_directory(home, ())
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent = _open_descendant_directory(root, relative.parts[:-1])
        try:
            descriptor = os.open(
                relative.parts[-1],
                strict_file_create_flags(read_write=read_write),
                mode,
                dir_fd=parent,
            )
        except OSError as error:
            raise ConfinedFilesystemError(
                "confined file cannot be created safely"
            ) from error
        try:
            _require_private_regular(
                os.fstat(descriptor),
                "confined file",
                require_mode=False,
            )
            os.fchmod(descriptor, mode)
            _require_private_regular(os.fstat(descriptor), "confined file")
            os.fsync(descriptor)
            os.fsync(parent)
        except BaseException:
            os.close(descriptor)
            descriptor = None
            try:
                os.unlink(relative.parts[-1], dir_fd=parent)
            except OSError:
                pass
            raise
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)
        os.close(root)


def open_confined_regular_file(home: Path, path: Path) -> int:
    """Open one owner-private regular file through its anchored parent."""

    relative = _relative_to_home(path, home)
    if not relative.parts:
        raise ConfinedFilesystemError("confined file must be below the confinement root")
    root = _open_confined_directory(home, ())
    parent: int | None = None
    try:
        parent = _open_descendant_directory(root, relative.parts[:-1])
        try:
            descriptor = os.open(
                relative.parts[-1],
                strict_file_read_flags(),
                dir_fd=parent,
            )
        except OSError as error:
            raise ConfinedFilesystemError(
                "confined file cannot be opened safely"
            ) from error
        try:
            _require_private_regular(os.fstat(descriptor), "confined file")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        if parent is not None:
            os.close(parent)
        os.close(root)


def open_and_harden_confined_regular_file(
    home: Path,
    path: Path,
    *,
    mode: int = 0o600,
) -> int:
    """Open and harden one owned, single-link regular file through its parent."""

    relative = _relative_to_home(path, home)
    if not relative.parts:
        raise ConfinedFilesystemError("confined file must be below the confinement root")
    root = _open_confined_directory(home, ())
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent = _open_descendant_directory(root, relative.parts[:-1])
        try:
            descriptor = os.open(
                relative.parts[-1],
                strict_file_read_flags(),
                dir_fd=parent,
            )
        except OSError as error:
            raise ConfinedFilesystemError(
                "confined file cannot be opened safely"
            ) from error
        try:
            _require_private_regular(
                os.fstat(descriptor),
                "confined file",
                require_mode=False,
            )
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            _require_private_regular(os.fstat(descriptor), "confined file")
            os.fsync(parent)
        except OSError as error:
            raise ConfinedFilesystemError(
                "confined file cannot be hardened safely"
            ) from error
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)
        os.close(root)


def open_confined_directory(home: Path, path: Path) -> int:
    """Open one owner-private directory beneath a pinned confinement root."""

    relative = _relative_to_home(path, home)
    return _open_confined_directory(home, relative.parts)


def replace_confined(home: Path, source: Path, destination: Path) -> None:
    """Atomically replace two entries through confinement-anchored parents."""

    required_no_follow_flag()
    source_relative = _relative_to_home(source, home)
    destination_relative = _relative_to_home(destination, home)
    if not source_relative.parts or not destination_relative.parts:
        raise ConfinedFilesystemError("atomic replacement cannot replace the confinement root")
    root = _open_confined_directory(home, ())
    source_parent: int | None = None
    destination_parent: int | None = None
    try:
        source_parent = _open_descendant_directory(
            root,
            source_relative.parts[:-1],
        )
        destination_parent = _open_descendant_directory(
            root,
            destination_relative.parts[:-1],
        )
        try:
            source_info = os.stat(
                source_relative.parts[-1],
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(source_info.st_mode):
                _require_private_directory(source_info, "atomic replacement source")
            else:
                _require_private_regular(source_info, "atomic replacement source")
            os.replace(
                source_relative.parts[-1],
                destination_relative.parts[-1],
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
            destination_info = os.stat(
                destination_relative.parts[-1],
                dir_fd=destination_parent,
                follow_symlinks=False,
            )
            source_changed = (
                destination_info.st_dev,
                destination_info.st_ino,
                stat.S_IFMT(destination_info.st_mode),
            ) != (
                source_info.st_dev,
                source_info.st_ino,
                stat.S_IFMT(source_info.st_mode),
            )
            if not source_changed:
                try:
                    if stat.S_ISDIR(source_info.st_mode):
                        _require_private_directory(
                            destination_info,
                            "atomic replacement destination",
                        )
                    else:
                        _require_private_regular(
                            destination_info,
                            "atomic replacement destination",
                        )
                except ConfinedFilesystemError:
                    source_changed = True
            if source_changed:
                remove_anchored_entry(
                    destination_parent,
                    destination_relative.parts[-1],
                    expected_identity=(
                        destination_info.st_dev,
                        destination_info.st_ino,
                    ),
                )
                raise ConfinedFilesystemError(
                    "confined atomic replacement source changed"
                )
        except OSError as error:
            raise ConfinedFilesystemError(
                "confined atomic replacement failed safely"
            ) from error
    finally:
        if destination_parent is not None:
            os.close(destination_parent)
        if source_parent is not None:
            os.close(source_parent)
        os.close(root)


class PrivateSqliteDatabase:
    """Prepare and validate one owner-private SQLite database and its sidecars."""

    def __init__(self, home: Path, path: Path) -> None:
        self._home = home
        self._path = path

    def prepare(self) -> None:
        """Create the private parent chain and database when they are absent."""

        required_no_follow_flag()
        _ensure_private_parent(self._home, self._path.parent)
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            descriptor = create_confined_file(
                self._home,
                self._path,
                read_write=True,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        _require_private_regular(info, "private SQLite database")

    def connect(self) -> sqlite3.Connection:
        """Open SQLite only after validating the database and owned sidecars."""

        required_no_follow_flag()
        try:
            info = os.lstat(self._path)
        except FileNotFoundError as error:
            raise ConfinedFilesystemError(
                "private SQLite database is missing"
            ) from error
        _require_private_regular(info, "private SQLite database")
        for path in _database_sidecars(self._path):
            try:
                sidecar_info = os.lstat(path)
            except FileNotFoundError:
                continue
            _require_private_regular(sidecar_info, "private SQLite sidecar")

        connection = sqlite3.connect(
            self._path,
            timeout=PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                f"PRAGMA busy_timeout={int(PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"
            )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            try:
                connection.close()
            except BaseException:
                pass
            raise
        return connection

    def connect_read_only(self) -> sqlite3.Connection:
        """Open a validated SQLite database without mutating its journal mode."""

        required_no_follow_flag()
        try:
            info = os.lstat(self._path)
        except FileNotFoundError as error:
            raise ConfinedFilesystemError(
                "private SQLite database is missing"
            ) from error
        _require_private_regular(info, "private SQLite database")
        for path in _database_sidecars(self._path):
            try:
                sidecar_info = os.lstat(path)
            except FileNotFoundError:
                continue
            _require_private_regular(sidecar_info, "private SQLite sidecar")

        connection = sqlite3.connect(
            f"{self._path.as_uri()}?mode=ro",
            uri=True,
            timeout=PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute(
                f"PRAGMA busy_timeout={int(PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"
            )
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def transaction(
        self,
        *,
        translate_connect_error: Callable[[ConfinedFilesystemError], Exception]
        | None = None,
        translate_harden_error: Callable[[ConfinedFilesystemError], Exception]
        | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Own one immediate transaction through commit and file hardening.

        Translation callbacks preserve an owner's public errors without moving
        domain-specific exception types into this filesystem module.
        """

        try:
            connection = self.connect()
        except ConfinedFilesystemError as error:
            if translate_connect_error is None:
                raise
            raise translate_connect_error(error) from error
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            # Cleanup is bounded and cannot replace the transaction failure.
            try:
                connection.rollback()
            except BaseException:
                pass
            try:
                connection.close()
            except BaseException:
                pass
            try:
                self.harden()
            except BaseException:
                pass
            raise
        try:
            connection.close()
        except BaseException:
            try:
                self.harden()
            except BaseException:
                pass
            raise
        try:
            self.harden()
        except ConfinedFilesystemError as error:
            if translate_harden_error is None:
                raise
            raise translate_harden_error(error) from error

    def harden(self, *, sync_parent: bool = False) -> None:
        """Set owner-only modes after SQLite may have created sidecar files."""

        for path in (self._path, *_database_sidecars(self._path)):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            _require_private_regular(
                info,
                "private SQLite file",
                require_mode=False,
            )
            os.chmod(path, 0o600)
        if sync_parent:
            _fsync_directory(self._path.parent)


@dataclass(frozen=True, slots=True)
class _RelativeNode:
    parent: _RelativeNode | None
    name: str


@dataclass(slots=True)
class _RemovalFrame:
    node: _RelativeNode
    before: tuple[int, int]
    child_order: DirectoryOrderCursor


@dataclass(slots=True)
class DirectoryOrderCursor:
    order_id: int
    last_name: bytes | None = None
    exhausted: bool = False


class SpilledDirectoryOrder:
    """Deterministically order directory names without retaining their width."""

    def __init__(self, *, insert_batch_size: int = _DIRECTORY_ORDER_INSERT_BATCH_SIZE) -> None:
        if not isinstance(insert_batch_size, int) or isinstance(insert_batch_size, bool) or insert_batch_size < 1:
            raise ValueError("directory order insertion batch size must be positive")
        self._insert_batch_size = insert_batch_size
        try:
            self._connection = sqlite3.connect("")
        except sqlite3.Error as error:
            raise ConfinedFilesystemError(
                "confined directory ordering cannot be initialized safely"
            ) from error
        self._next_order_id = 1
        try:
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute("PRAGMA cache_size=-512")
            self._connection.execute("PRAGMA journal_mode=OFF")
            self._connection.execute(
                """
                CREATE TABLE directory_name (
                    order_id INTEGER NOT NULL,
                    name BLOB NOT NULL,
                    PRIMARY KEY (order_id, name)
                ) WITHOUT ROWID
                """
            )
        except sqlite3.Error as error:
            self._close_after_failure()
            raise ConfinedFilesystemError(
                "confined directory ordering cannot be initialized safely"
            ) from error
        except BaseException:
            self._close_after_failure()
            raise

    def __enter__(self) -> SpilledDirectoryOrder:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _close_after_failure(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass

    def scan(
        self,
        descriptor: int,
        *,
        include: Callable[[str], bool] | None = None,
    ) -> DirectoryOrderCursor:
        order_id = self._next_order_id
        self._next_order_id += 1
        rows: list[tuple[int, bytes]] = []
        try:
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    if include is not None and not include(entry.name):
                        continue
                    rows.append((order_id, os.fsencode(entry.name)))
                    if len(rows) >= self._insert_batch_size:
                        self._insert(rows)
                        rows.clear()
            if rows:
                self._insert(rows)
        except sqlite3.Error as error:
            self._close_after_failure()
            raise ConfinedFilesystemError(
                "confined directory cannot be scanned safely"
            ) from error
        except (OSError, UnicodeError) as error:
            raise ConfinedFilesystemError(
                "confined directory cannot be scanned safely"
            ) from error
        return DirectoryOrderCursor(order_id=order_id)

    def next_name(self, cursor: DirectoryOrderCursor) -> str | None:
        if cursor.exhausted:
            return None
        try:
            if cursor.last_name is None:
                row = self._connection.execute(
                    """
                    SELECT name FROM directory_name
                    WHERE order_id = ? ORDER BY name LIMIT 1
                    """,
                    (cursor.order_id,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """
                    SELECT name FROM directory_name
                    WHERE order_id = ? AND name > ? ORDER BY name LIMIT 1
                    """,
                    (cursor.order_id, cursor.last_name),
                ).fetchone()
            if row is not None:
                cursor.last_name = bytes(row[0])
                return os.fsdecode(cursor.last_name)
            self._connection.execute(
                "DELETE FROM directory_name WHERE order_id = ?",
                (cursor.order_id,),
            )
            cursor.exhausted = True
            return None
        except sqlite3.Error as error:
            self._close_after_failure()
            raise ConfinedFilesystemError(
                "confined directory cannot be read safely"
            ) from error
        except UnicodeError as error:
            raise ConfinedFilesystemError(
                "confined directory cannot be read safely"
            ) from error

    def names(self, cursor: DirectoryOrderCursor) -> Iterator[str]:
        """Yield one cursor in raw filename-byte order."""

        while (name := self.next_name(cursor)) is not None:
            yield name

    def _insert(self, rows: Sequence[tuple[int, bytes]]) -> None:
        self._connection.executemany(
            "INSERT INTO directory_name (order_id, name) VALUES (?, ?)",
            rows,
        )


class _DirectoryDescriptorCache:
    """Reopen deep paths safely while keeping descriptor use constant."""

    def __init__(self, root_fd: int) -> None:
        self._root_fd = root_fd
        self._root_device = os.fstat(root_fd).st_dev
        self._descriptors: OrderedDict[int, tuple[_RelativeNode, int]] = OrderedDict()

    def __enter__(self) -> _DirectoryDescriptorCache:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        for _node, descriptor in self._descriptors.values():
            os.close(descriptor)
        self._descriptors.clear()

    def open(self, node: _RelativeNode | None) -> int:
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
                    next_descriptor = _open_directory_without_mount_crossing(
                        current,
                        component_node.name,
                        strict_directory_open_flags(),
                    )
                except OSError as error:
                    if error.errno == errno.EXDEV:
                        raise ConfinedFilesystemError(
                            "confined removal parent crosses a filesystem boundary"
                        ) from error
                    raise ConfinedFilesystemError(
                        "confined removal parent cannot be opened safely"
                    ) from error
                os.close(current)
                current = next_descriptor
                info = os.fstat(current)
                _require_private_directory(info, "confined removal parent")
                _require_device(info, self._root_device, "confined removal parent")
                self._remember(component_node, current)
            return current
        except BaseException:
            os.close(current)
            raise

    def _remember(self, node: _RelativeNode, descriptor: int) -> None:
        previous = self._descriptors.pop(id(node), None)
        if previous is not None:
            os.close(previous[1])
        self._descriptors[id(node)] = (node, os.dup(descriptor))
        while len(self._descriptors) > _DIRECTORY_DESCRIPTOR_CACHE_SIZE:
            _key, (_old_node, old_descriptor) = self._descriptors.popitem(last=False)
            os.close(old_descriptor)


def remove_confined_path(
    home: Path,
    path: Path,
    *,
    progress: ConfinedRemovalProgress | None = None,
) -> None:
    """Remove one entry through anchored, no-follow directory handles."""

    required_no_follow_flag()
    relative = _relative_to_home(path, home)
    if not relative.parts:
        raise ConfinedFilesystemError("refusing to remove the confinement root")
    anchored: list[int] = []
    try:
        current = os.open(home, strict_directory_open_flags())
        anchored.append(current)
        root_device = os.fstat(current).st_dev
        # ``paths.ensure_data_dirs`` historically created the home with the
        # process umask (commonly 0755). We own the descriptor and pin it with
        # O_NOFOLLOW, so harden that app-created mode in place before traversing
        # instead of leaving a durable reset marker that can never be retried.
        _harden_private_directory_fd(current, "confinement root", sync=False)
        for component in relative.parts[:-1]:
            try:
                next_descriptor = _open_directory_without_mount_crossing(
                    current,
                    component,
                    strict_directory_open_flags(),
                )
            except FileNotFoundError:
                _fsync_anchored_deletion(anchored)
                return
            except OSError as error:
                if error.errno == errno.EXDEV:
                    raise ConfinedFilesystemError(
                        "confined directory crosses a filesystem boundary"
                    ) from error
                raise ConfinedFilesystemError(
                    "confined directory cannot be opened safely"
                ) from error
            current = next_descriptor
            anchored.append(current)
            _require_device(os.fstat(current), root_device, "confined directory")
            _harden_private_directory_fd(current, "confined directory", sync=False)
        remove_anchored_entry(current, relative.parts[-1], progress=progress)
        _fsync_anchored_deletion(anchored)
    finally:
        for descriptor in reversed(anchored):
            os.close(descriptor)


def _fsync_anchored_deletion(anchored: list[int]) -> None:
    """Persist an entry's absence from its nearest parent through the root."""

    try:
        for descriptor in reversed(anchored):
            os.fsync(descriptor)
    except OSError as error:
        raise ConfinedFilesystemError(
            "confined deletion parent cannot be synchronized safely"
        ) from error


def remove_anchored_entry(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
    progress: ConfinedRemovalProgress | None = None,
) -> None:
    """Remove one safe relative name beneath an already anchored directory."""

    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
    ):
        raise ConfinedFilesystemError("anchored removal requires one safe entry name")
    _remove_entry_at(
        parent_fd,
        name,
        expected_identity=expected_identity,
        progress=progress,
    )


def _remove_entry_at(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
    progress: ConfinedRemovalProgress | None = None,
) -> None:
    root_device = os.fstat(parent_fd).st_dev
    try:
        initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if expected_identity is not None and (
        initial.st_dev,
        initial.st_ino,
    ) != expected_identity:
        raise ConfinedFilesystemError("confined entry changed during removal")
    if stat.S_ISLNK(initial.st_mode) or stat.S_ISREG(initial.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        if progress is not None:
            progress.record()
        return
    if not stat.S_ISDIR(initial.st_mode):
        raise ConfinedFilesystemError("confined removal refuses special files")
    _require_device(initial, root_device, "confined removal directory")
    root_identity = expected_identity or (initial.st_dev, initial.st_ino)

    root_node = _RelativeNode(None, name)
    stack: list[_RemovalFrame | _RelativeNode] = [root_node]
    with (
        _DirectoryDescriptorCache(parent_fd) as cache,
        SpilledDirectoryOrder() as orders,
    ):
        while stack:
            item = stack[-1]
            if isinstance(item, _RelativeNode):
                stack.pop()
                node_parent_fd = cache.open(item.parent)
                try:
                    try:
                        before = os.stat(
                            item.name,
                            dir_fd=node_parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    if (
                        item is root_node
                        and (before.st_dev, before.st_ino) != root_identity
                    ):
                        raise ConfinedFilesystemError(
                            "confined entry changed during removal"
                        )
                    if stat.S_ISLNK(before.st_mode) or stat.S_ISREG(before.st_mode):
                        os.unlink(item.name, dir_fd=node_parent_fd)
                        if progress is not None:
                            progress.record()
                        continue
                    if not stat.S_ISDIR(before.st_mode):
                        raise ConfinedFilesystemError(
                            "confined removal refuses special files"
                        )
                    _require_directory(before, "confined removal directory")
                    _require_device(
                        before,
                        root_device,
                        "confined removal directory",
                    )
                    child_fd = _open_removal_directory(
                        node_parent_fd,
                        item.name,
                        before,
                        root_device,
                    )
                    try:
                        _harden_private_directory_fd(
                            child_fd,
                            "confined removal directory",
                        )
                        child_order = orders.scan(child_fd)
                    finally:
                        os.close(child_fd)
                finally:
                    os.close(node_parent_fd)
                stack.append(
                    _RemovalFrame(
                        node=item,
                        before=(before.st_dev, before.st_ino),
                        child_order=child_order,
                    )
                )
                continue

            child_name = orders.next_name(item.child_order)
            if child_name is not None:
                stack.append(_RelativeNode(item.node, child_name))
                continue

            stack.pop()
            node_parent_fd = cache.open(item.node.parent)
            try:
                current = os.stat(
                    item.node.name,
                    dir_fd=node_parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != item.before
                ):
                    raise ConfinedFilesystemError(
                        "confined directory changed during removal"
                    )
                os.rmdir(item.node.name, dir_fd=node_parent_fd)
                if progress is not None:
                    progress.record()
            finally:
                os.close(node_parent_fd)


def _open_removal_directory(
    parent_fd: int,
    name: str,
    before: os.stat_result,
    root_device: int,
) -> int:
    try:
        descriptor = _open_directory_without_mount_crossing(
            parent_fd,
            name,
            strict_directory_open_flags(),
        )
    except OSError as error:
        if error.errno == errno.EXDEV:
            raise ConfinedFilesystemError(
                "confined removal directory crosses a filesystem boundary"
            ) from error
        if (
            error.errno not in {errno.EACCES, errno.EPERM}
            or stat.S_IMODE(before.st_mode) == 0o700
        ):
            raise ConfinedFilesystemError(
                "confined removal directory cannot be opened safely"
            ) from error
        _harden_inaccessible_removal_directory(
            parent_fd,
            name,
            before,
            root_device,
        )
        try:
            descriptor = _open_directory_without_mount_crossing(
                parent_fd,
                name,
                strict_directory_open_flags(),
            )
        except OSError as retry_error:
            if retry_error.errno == errno.EXDEV:
                raise ConfinedFilesystemError(
                    "confined removal directory crosses a filesystem boundary"
                ) from retry_error
            raise ConfinedFilesystemError(
                "confined removal directory cannot be opened safely"
            ) from retry_error
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(descriptor)
        raise ConfinedFilesystemError("confined directory changed during removal")
    try:
        _require_directory(opened, "confined removal directory")
        _require_device(opened, root_device, "confined removal directory")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory_without_mount_crossing(
    parent_fd: int,
    name: str,
    flags: int,
) -> int:
    if sys.platform != "linux":
        return os.open(name, flags, dir_fd=parent_fd)

    how = _LinuxOpenHow(
        flags=flags,
        mode=0,
        resolve=_RESOLVE_NO_XDEV | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH,
    )
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.syscall(
        ctypes.c_long(_LINUX_OPENAT2_SYSCALL),
        ctypes.c_int(parent_fd),
        ctypes.c_char_p(os.fsencode(name)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), name)
    return int(descriptor)


def _harden_inaccessible_removal_directory(
    parent_fd: int,
    name: str,
    before: os.stat_result,
    root_device: int,
) -> None:
    path_descriptor_flag = getattr(os, "O_PATH", 0)
    if not path_descriptor_flag:
        raise ConfinedFilesystemError(
            "confined removal directory cannot be opened safely"
        )

    # O_PATH pins even a mode-000 directory; procfs then applies chmod to that
    # inode instead of resolving the possibly swapped parent entry.
    try:
        anchor = _open_directory_without_mount_crossing(
            parent_fd,
            name,
            path_descriptor_flag
            | required_no_follow_flag()
            | int(getattr(os, "O_DIRECTORY", 0))
            | int(getattr(os, "O_CLOEXEC", 0)),
        )
    except OSError as error:
        if error.errno == errno.EXDEV:
            raise ConfinedFilesystemError(
                "confined removal directory crosses a filesystem boundary"
            ) from error
        raise ConfinedFilesystemError(
            "confined removal directory cannot be anchored safely"
        ) from error
    try:
        anchored = os.fstat(anchor)
        _require_removal_directory_identity(anchored, before, root_device)
        try:
            os.chmod(f"/proc/self/fd/{anchor}", 0o700)
        except (NotImplementedError, OSError, ValueError) as error:
            raise ConfinedFilesystemError(
                "confined removal directory cannot be hardened safely"
            ) from error
        _require_removal_directory_identity(
            os.fstat(anchor),
            before,
            root_device,
            require_private=True,
        )
    finally:
        os.close(anchor)

    try:
        hardened = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ConfinedFilesystemError(
            "confined directory changed during removal"
        ) from error
    _require_removal_directory_identity(
        hardened,
        before,
        root_device,
        require_private=True,
    )


def _require_removal_directory_identity(
    info: os.stat_result,
    before: os.stat_result,
    root_device: int,
    *,
    require_private: bool = False,
) -> None:
    _require_directory(info, "confined removal directory")
    _require_device(info, root_device, "confined removal directory")
    if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
        raise ConfinedFilesystemError("confined directory changed during removal")
    if require_private:
        _require_exact_private_directory(info, "confined removal directory")


def _relative_to_home(path: Path, home: Path) -> Path:
    try:
        return path.relative_to(home)
    except ValueError as error:
        raise ConfinedFilesystemError(
            "confined path must stay within the confinement root"
        ) from error


def _ensure_private_parent(home: Path, directory: Path) -> None:
    ensure_private_directory(home, directory)


def _database_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
        path.with_name(f"{path.name}-journal"),
    )


def _require_private_regular(
    info: os.stat_result,
    label: str,
    *,
    require_mode: bool = True,
) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfinedFilesystemError(f"{label} is not a safe regular file")
    _require_owned(info, label)
    if info.st_nlink != 1:
        raise ConfinedFilesystemError(f"{label} has multiple hard links")
    if require_mode and stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfinedFilesystemError(f"{label} is not private")


def _require_owned(info: os.stat_result, label: str) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfinedFilesystemError(f"{label} is not owned by the current user")


def _require_directory(info: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfinedFilesystemError(f"{label} is not a safe directory")
    _require_owned(info, label)


def _require_device(info: os.stat_result, device: int, label: str) -> None:
    if info.st_dev != device:
        raise ConfinedFilesystemError(f"{label} crosses a filesystem boundary")


def _require_private_directory(info: os.stat_result, label: str) -> None:
    _require_directory(info, label)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfinedFilesystemError(f"{label} is not private")


def _require_exact_private_directory(info: os.stat_result, label: str) -> None:
    _require_private_directory(info, label)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ConfinedFilesystemError(f"{label} mode mismatch")


def _harden_private_directory_fd(
    descriptor: int,
    label: str,
    *,
    sync: bool = True,
) -> None:
    _require_directory(os.fstat(descriptor), label)
    try:
        os.fchmod(descriptor, 0o700)
        if sync:
            os.fsync(descriptor)
    except OSError as error:
        raise ConfinedFilesystemError(f"{label} cannot be hardened safely") from error
    _require_exact_private_directory(os.fstat(descriptor), label)


def _open_confined_directory(home: Path, components: Sequence[str]) -> int:
    try:
        current = os.open(home, strict_directory_open_flags())
    except OSError as error:
        raise ConfinedFilesystemError(
            "confinement root cannot be opened safely"
        ) from error
    try:
        _require_exact_private_directory(os.fstat(current), "confinement root")
        descendant = _open_descendant_directory(current, components)
        os.close(current)
        return descendant
    except BaseException:
        os.close(current)
        raise


def _open_descendant_directory(
    root_fd: int,
    components: Sequence[str],
) -> int:
    current = os.dup(root_fd)
    try:
        for component in components:
            try:
                child = os.open(
                    component,
                    strict_directory_open_flags(),
                    dir_fd=current,
                )
            except OSError as error:
                raise ConfinedFilesystemError(
                    "confined directory cannot be opened safely"
                ) from error
            try:
                _require_private_directory(os.fstat(child), "confined directory")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _fsync_directory(path: Path) -> None:
    fsync_directory(path)


def _directory_open_flags() -> int:
    return strict_directory_open_flags()
