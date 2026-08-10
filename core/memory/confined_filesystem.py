"""Shared filesystem primitives confined to an effective Memory home."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_DIRECTORY_ORDER_INSERT_BATCH_SIZE = 256
_DIRECTORY_DESCRIPTOR_CACHE_SIZE = 48
PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


class ConfinedFilesystemError(RuntimeError):
    """A confined filesystem operation refused an unsafe path or entry."""


class PrivateSqliteDatabase:
    """Prepare and validate one owner-private SQLite database and its sidecars."""

    def __init__(self, home: Path, path: Path) -> None:
        self._home = home
        self._path = path

    def prepare(self) -> None:
        """Create the private parent chain and database when they are absent."""

        _ensure_private_parent(self._home, self._path.parent)
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags, 0o600)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(self._path, 0o600)
            _fsync_directory(self._path.parent)
            return
        _require_private_regular(info, "private SQLite database")

    def connect(self) -> sqlite3.Connection:
        """Open SQLite only after validating the database and owned sidecars."""

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
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"PRAGMA busy_timeout={int(PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

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
    child_order: _DirectoryOrderCursor


@dataclass(slots=True)
class _DirectoryOrderCursor:
    order_id: int
    last_name: bytes | None = None
    exhausted: bool = False


class _SpilledDirectoryOrder:
    """Deterministically order directory names without retaining their width."""

    def __init__(self) -> None:
        self._connection = sqlite3.connect("")
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
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> _SpilledDirectoryOrder:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._connection.close()

    def scan(self, descriptor: int) -> _DirectoryOrderCursor:
        order_id = self._next_order_id
        self._next_order_id += 1
        rows: list[tuple[int, bytes]] = []
        try:
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    rows.append((order_id, os.fsencode(entry.name)))
                    if len(rows) >= _DIRECTORY_ORDER_INSERT_BATCH_SIZE:
                        self._insert(rows)
                        rows.clear()
            if rows:
                self._insert(rows)
        except (OSError, sqlite3.Error, UnicodeError) as error:
            raise ConfinedFilesystemError(
                "confined directory cannot be scanned safely"
            ) from error
        return _DirectoryOrderCursor(order_id=order_id)

    def next_name(self, cursor: _DirectoryOrderCursor) -> str | None:
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
            raise ConfinedFilesystemError(
                "confined directory cannot be read safely"
            ) from error

    def _insert(self, rows: Sequence[tuple[int, bytes]]) -> None:
        self._connection.executemany(
            "INSERT INTO directory_name (order_id, name) VALUES (?, ?)",
            rows,
        )


class _DirectoryDescriptorCache:
    """Reopen deep paths safely while keeping descriptor use constant."""

    def __init__(self, root_fd: int) -> None:
        self._root_fd = root_fd
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
                    next_descriptor = os.open(
                        component_node.name,
                        _directory_open_flags(),
                        dir_fd=current,
                    )
                except OSError as error:
                    raise ConfinedFilesystemError(
                        "confined removal parent cannot be opened safely"
                    ) from error
                os.close(current)
                current = next_descriptor
                info = os.fstat(current)
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ConfinedFilesystemError(
                        "confined removal parent is not a safe directory"
                    )
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
) -> None:
    """Remove one entry through anchored, no-follow directory handles."""

    relative = _relative_to_home(path, home)
    if not relative.parts:
        raise ConfinedFilesystemError("refusing to remove the confinement root")
    current: int | None = None
    try:
        current = os.open(home, _directory_open_flags())
        for component in relative.parts[:-1]:
            try:
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current,
                )
            except FileNotFoundError:
                return
            os.close(current)
            current = next_descriptor
        remove_anchored_entry(current, relative.parts[-1])
    finally:
        if current is not None:
            os.close(current)


def remove_anchored_entry(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
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
    )


def _remove_entry_at(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    root_node = _RelativeNode(None, name)
    stack: list[_RemovalFrame | _RelativeNode] = [root_node]
    with (
        _DirectoryDescriptorCache(parent_fd) as cache,
        _SpilledDirectoryOrder() as orders,
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
                        and expected_identity is not None
                        and (
                            not stat.S_ISDIR(before.st_mode)
                            or (before.st_dev, before.st_ino) != expected_identity
                        )
                    ):
                        raise ConfinedFilesystemError(
                            "confined entry changed during removal"
                        )
                    if stat.S_ISLNK(before.st_mode) or stat.S_ISREG(before.st_mode):
                        os.unlink(item.name, dir_fd=node_parent_fd)
                        continue
                    if not stat.S_ISDIR(before.st_mode):
                        raise ConfinedFilesystemError(
                            "confined removal refuses special files"
                        )
                    child_fd = os.open(
                        item.name,
                        _directory_open_flags(),
                        dir_fd=node_parent_fd,
                    )
                    try:
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (
                            before.st_dev,
                            before.st_ino,
                        ):
                            raise ConfinedFilesystemError(
                                "confined directory changed during removal"
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
            finally:
                os.close(node_parent_fd)


def _relative_to_home(path: Path, home: Path) -> Path:
    try:
        return path.relative_to(home)
    except ValueError as error:
        raise ConfinedFilesystemError(
            "confined path must stay within the confinement root"
        ) from error


def _ensure_private_parent(home: Path, directory: Path) -> None:
    relative = _relative_to_home(directory, home)
    if not home.exists():
        home.mkdir(parents=True, mode=0o700)
    _require_directory(os.lstat(home), "confinement root")
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
        _require_directory(info, "private SQLite directory")
        os.chmod(current, 0o700)
        _fsync_directory(current)
        if stat.S_IMODE(os.lstat(current).st_mode) != 0o700:
            raise ConfinedFilesystemError("private SQLite directory is not private")


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, _directory_open_flags())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
