from __future__ import annotations

import ctypes
import errno
import fnmatch
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_LIST_ENTRIES = 5000
DELETE_UNDO_TTL_SECONDS = 3600
DELETE_UNDO_MAX_ENTRIES = 32
DELETE_UNDO_ENTRY_SIZE_CAP_BYTES = 512 * 1024 * 1024
DELETE_UNDO_TOTAL_SIZE_CAP_BYTES = 2 * 1024 * 1024 * 1024
COPY_TOTAL_SIZE_CAP_BYTES = 2 * 1024 * 1024 * 1024

INLINE_SAFE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/bmp",
    "image/x-icon",
    "image/heic",
    "image/heif",
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/flac",
    "audio/x-m4a",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/quicktime",
}

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_NOREPLACE_FALLBACK_WARNED = False
_WRITE_LOCKS_MUTEX = threading.Lock()
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_TEMP_PREFIX = ".avibe-write-"
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_COPY_TEMP_PREFIX = ".avibe-copy-"
_NO_REPLACE_UNSUPPORTED_ERRNOS = {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
_HARD_LINK_UNSUPPORTED_ERRNOS = {errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP}
_DELETE_UNDO_DIR_NAME = "files_undo"
_DELETE_UNDO_ENTRY_NAME = "entry"
_DELETE_UNDO_METADATA_NAME = "metadata.json"
_DELETE_UNDO_LOCK = threading.Lock()
_DELETE_UNDO_INITIALIZED = False
_DELETE_UNDO_EXPIRY_TIMER: threading.Timer | None = None
_DELETE_UNDO_EXPIRY_TIMER_DEADLINE: float | None = None
_DELETE_UNDO_EXPIRY_TIMER_ROOT: Path | None = None
_DELETE_UNDO_ENTRY_SIZE_CAP_ENV = "AVIBE_FILES_UNDO_SIZE_CAP_BYTES"
_DELETE_UNDO_TTL_ENV = "AVIBE_FILES_UNDO_TTL_SECONDS"
_DELETE_UNDO_MAX_ENTRIES_ENV = "AVIBE_FILES_UNDO_MAX_ENTRIES"
_DELETE_UNDO_TOTAL_SIZE_CAP_ENV = "AVIBE_FILES_UNDO_TOTAL_SIZE_CAP_BYTES"
_COPY_TOTAL_SIZE_CAP_ENV = "AVIBE_FILES_COPY_SIZE_CAP_BYTES"


class FileBrowserError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class NotFoundError(FileBrowserError):
    def __init__(self, message: str = "Path not found") -> None:
        super().__init__("not_found", message, 404)


class ConflictError(FileBrowserError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 409)


@dataclass(frozen=True)
class FileContent:
    path: Path
    mime: str
    disposition: str
    data: bytes


@dataclass(frozen=True)
class DeleteUndoEntry:
    token: str
    directory: Path
    entry_path: Path
    metadata_path: Path
    original_path: Path
    parent_dev: int
    parent_ino: int
    deleted_at: float
    size: int


def resolve_safe_path(raw: str) -> Path:
    """Expand and canonicalize one user-supplied absolute filesystem path."""
    expanded = _expanded_absolute_path(raw)
    try:
        return expanded.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FileBrowserError("invalid_path", str(exc), 400) from exc


def _expanded_absolute_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise FileBrowserError("invalid_path", "Path is required", 400)
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        raise FileBrowserError("invalid_path", "Path must be absolute", 400)
    return expanded


def resolve_existing_directory(raw: str) -> Path:
    """Resolve an absolute path to an existing directory WITHOUT following a symlink on the
    final component.

    Same hardening family as the upload-destination check (``_resolve_upload_directory``) and
    the list/write/rename mutations: expand ``~``, require absolute, canonicalize the PARENT
    strictly, append the final component without following a symlink there, then require the
    result to be a real directory. Raises ``FileBrowserError`` / ``NotFoundError`` on any
    violation. Shared by the terminal service's "Open Terminal Here" start directory.
    """
    resolve_safe_path(raw)  # reject blank / relative / uncanonicalizable early
    expanded = _expanded_absolute_path(raw)
    if expanded.parent == expanded:
        directory = expanded  # a filesystem root is a valid, already-canonical directory
    else:
        if expanded.name in {"", ".", ".."} or _raw_final_component(raw) in {".", ".."}:
            raise NotFoundError("Directory not found")
        try:
            parent = expanded.parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise NotFoundError("Directory not found") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise FileBrowserError("fs_error", str(exc), 400) from exc
        directory = parent / expanded.name
    try:
        stat_result = directory.lstat()
    except FileNotFoundError as exc:
        raise NotFoundError("Directory not found") from exc
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc
    if not stat.S_ISDIR(stat_result.st_mode):
        raise FileBrowserError("not_dir", "Path is not a directory", 400)
    return directory


def _resolve_existing_path(raw: str) -> Path:
    resolved = resolve_safe_path(raw)
    try:
        return resolved.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError() from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise FileBrowserError("invalid_path", str(exc), 400) from exc


def _resolve_entry_path(raw: str) -> Path:
    expanded = _expanded_absolute_path(raw)
    if expanded.name in {"", ".", ".."} or _raw_final_component(raw) in {".", ".."}:
        raise FileBrowserError("invalid_path", "Path must include a valid entry name", 400)
    try:
        parent = expanded.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError("Parent directory not found") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise FileBrowserError("invalid_path", str(exc), 400) from exc
    return parent / expanded.name


def _raw_final_component(raw: str) -> str:
    expanded_raw = os.path.expanduser(raw)
    separators = os.sep
    if os.altsep:
        separators += os.altsep
    stripped = expanded_raw.rstrip(separators)
    if not stripped:
        return ""
    return os.path.basename(stripped)


def _resolve_existing_entry_path(raw: str) -> Path:
    path = _resolve_entry_path(raw)
    _stat_existing(path, follow_symlinks=False)
    return path


def _exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _same_entry_no_follow(left: Path, right: Path) -> bool:
    try:
        left_stat = left.lstat()
        right_stat = right.lstat()
    except FileNotFoundError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)


def _same_directory_entry(left: Path, right: Path) -> bool:
    try:
        left_parent = left.parent.resolve(strict=True)
        right_parent = right.parent.resolve(strict=True)
        left_stat = left.lstat()
    except (OSError, RuntimeError):
        return False
    if left_parent != right_parent:
        return False
    if left.name == right.name:
        return True
    same_inode_entries = 0
    for entry in left_parent.iterdir():
        try:
            entry_stat = entry.lstat()
        except OSError:
            continue
        if (entry_stat.st_dev, entry_stat.st_ino) == (left_stat.st_dev, left_stat.st_ino):
            same_inode_entries += 1
            if same_inode_entries > 1:
                return False
    return same_inode_entries == 1


def _is_dir_no_follow(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _entry_contains_no_follow(parent: Path, child: Path) -> bool:
    parent_resolved = parent.resolve(strict=True)
    child_resolved = child.parent.resolve(strict=True) / child.name
    return child_resolved == parent_resolved or parent_resolved in child_resolved.parents


def _rename_no_replace(source: Path, target: Path) -> None:
    """Rename ``source`` -> ``target`` without ever replacing an existing target.

    ``os.rename()`` REPLACES an existing destination on POSIX, so a separate existence
    check before it is a TOCTOU race — a target created in between is silently clobbered.
    ``os.link()`` is atomic and fails with ``FileExistsError`` if the target exists, so use
    link()+unlink() for the common same-directory, regular-file case. Symlinks and
    directories cannot be hard-linked; for those use renameat2(RENAME_NOREPLACE) where
    available, with a documented platform fallback.
    """
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise ConflictError("exists", "Destination already exists") from exc
    except OSError:
        _os_rename_noreplace(source, target)
        return
    _unlink_source_after_hard_link(source, target)


def _os_rename_noreplace(source: Path, target: Path) -> None:
    """Rename ``source`` to ``target`` without replacing an existing destination."""
    try:
        _glibc_renameat2_noreplace(source, target)
        return
    except FileExistsError as exc:
        raise ConflictError("exists", "Destination already exists") from exc
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise
        if exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    except AttributeError:
        pass

    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise ConflictError("exists", "Destination already exists") from exc
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise
    else:
        _unlink_source_after_hard_link(source, target)
        return

    _warn_rename_noreplace_fallback()
    if _exists_no_follow(target):
        raise ConflictError("exists", "Destination already exists")
    # Non-Linux or old libc fallback: POSIX rename can still replace a target
    # that appears after this check, notably an empty directory during directory
    # rename. Linux deployments should take the atomic renameat2 path above.
    source.rename(target)


def _rename_no_replace_into_dir(
    source: Path,
    target_parent_fd: int | None,
    target_parent: Path,
    target_name: str,
) -> None:
    if target_parent_fd is None:
        _rename_no_replace(source, target_parent / target_name)
        return

    try:
        _glibc_renameat2_noreplace_between(
            _AT_FDCWD,
            os.fsencode(source),
            target_parent_fd,
            os.fsencode(target_name),
            target_name,
        )
        return
    except FileExistsError as exc:
        raise ConflictError("exists", "Destination already exists") from exc
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise
        if exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    except AttributeError:
        pass

    try:
        os.link(source, target_name, dst_dir_fd=target_parent_fd, follow_symlinks=False)
    except FileExistsError as exc:
        raise ConflictError("exists", "Destination already exists") from exc
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise
    else:
        _unlink_source_after_hard_link_at(source, target_parent_fd, target_name)
        return

    _warn_rename_noreplace_fallback()
    try:
        os.stat(target_name, dir_fd=target_parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ConflictError("exists", "Destination already exists")
    os.rename(source, target_name, dst_dir_fd=target_parent_fd)


def _unlink_source_after_hard_link(source: Path, target: Path) -> None:
    try:
        os.unlink(source)
    except OSError:
        _remove_created_hard_link(target, source)
        raise


def _unlink_source_after_hard_link_at(source: Path, target_parent_fd: int, target_name: str) -> None:
    try:
        os.unlink(source)
    except OSError:
        _remove_created_hard_link_at(target_parent_fd, target_name, source)
        raise


def _remove_created_hard_link(target: Path, source: Path) -> None:
    try:
        source_stat = source.lstat()
        target_stat = target.lstat()
        if (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            os.unlink(target)
    except OSError:
        logger.debug("Failed to remove rollback hard link after no-replace rename failure", exc_info=True)


def _remove_created_hard_link_at(target_parent_fd: int, target_name: str, source: Path) -> None:
    try:
        source_stat = source.lstat()
        target_stat = os.stat(target_name, dir_fd=target_parent_fd, follow_symlinks=False)
        if (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            os.unlink(target_name, dir_fd=target_parent_fd)
    except OSError:
        logger.debug("Failed to remove rollback hard link after no-replace rename failure", exc_info=True)


def _glibc_renameat2_noreplace(source: Path, target: Path) -> None:
    _glibc_renameat2_noreplace_between(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        str(target),
    )


def _glibc_renameat2_noreplace_at(dir_fd: int, source_name: str, target_name: str) -> None:
    _glibc_renameat2_noreplace_between(
        dir_fd,
        os.fsencode(source_name),
        dir_fd,
        os.fsencode(target_name),
        target_name,
    )


def _glibc_renameat2_noreplace_between(
    source_dir_fd: int,
    source_name: bytes,
    target_dir_fd: int,
    target_name: bytes,
    target_for_error: str,
) -> None:
    if sys.platform != "linux":
        raise AttributeError("renameat2 is only available on Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_dir_fd,
        source_name,
        target_dir_fd,
        target_name,
        _RENAME_NOREPLACE,
    )
    if result != 0:
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), target_for_error)
        raise OSError(err, os.strerror(err), target_for_error)


def _warn_rename_noreplace_fallback() -> None:
    global _RENAME_NOREPLACE_FALLBACK_WARNED
    if _RENAME_NOREPLACE_FALLBACK_WARNED:
        return
    _RENAME_NOREPLACE_FALLBACK_WARNED = True
    logger.warning(
        "Atomic no-replace rename is unavailable on this platform; falling back to "
        "check-then-rename with a residual empty-directory race."
    )


def _stat_existing(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
    try:
        return path.stat() if follow_symlinks else path.lstat()
    except FileNotFoundError as exc:
        raise NotFoundError() from exc
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc


def _require_stable_resolved_path(path: Path) -> None:
    try:
        current = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError() from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise FileBrowserError("invalid_path", str(exc), 400) from exc
    if current != path:
        raise NotFoundError()


def _require_regular_file(raw: str) -> Path:
    path = _resolve_existing_path(raw)
    _require_stable_resolved_path(path)
    if not path.is_file():
        raise FileBrowserError("not_file", "Path is not a regular file", 400)
    return path


def _require_directory(raw: str) -> Path:
    path = _resolve_existing_path(raw)
    if not path.is_dir():
        raise FileBrowserError("not_dir", "Path is not a directory", 400)
    return path


def _kind_from_mode(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file"


def _mtime_seconds(stat_result: os.stat_result) -> float:
    return stat_result.st_mtime_ns / 1_000_000_000


def _extension(path: Path) -> str:
    suffix = path.suffix
    return suffix[1:].lower() if suffix.startswith(".") else suffix.lower()


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _entry_payload(entry: os.DirEntry[str]) -> dict[str, Any] | None:
    try:
        stat_result = entry.stat(follow_symlinks=False)
    except OSError:
        stat_result = None
    kind = "file"
    size = None
    mtime = None
    if stat_result is not None:
        kind = _kind_from_mode(stat_result.st_mode)
        if stat.S_ISREG(stat_result.st_mode):
            size = stat_result.st_size
        mtime = _mtime_seconds(stat_result)
    return {
        "name": entry.name,
        "kind": kind,
        "size": size,
        "mtime": mtime,
        "ext": _extension(Path(entry.name)) if kind != "dir" else "",
    }


def list_directory(raw_path: str, *, show_hidden: bool = False) -> dict[str, Any]:
    target = _require_directory(raw_path)
    entries: list[dict[str, Any]] = []
    scanned_entries = 0
    truncated = False
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                scanned_entries += 1
                if not show_hidden and entry.name.startswith("."):
                    if scanned_entries > MAX_LIST_ENTRIES:
                        truncated = True
                        break
                    continue
                payload = _entry_payload(entry)
                if payload is not None:
                    entries.append(payload)
                if len(entries) > MAX_LIST_ENTRIES:
                    entries.pop()
                    truncated = True
                    break
                if scanned_entries > MAX_LIST_ENTRIES:
                    truncated = True
                    break
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc

    entries.sort(key=lambda item: (0 if item["kind"] == "dir" else 1, str(item["name"]).lower(), str(item["name"])))
    parent = None if target.parent == target else str(target.parent)
    payload = {"ok": True, "path": str(target), "parent": parent, "entries": entries, "truncated": truncated}
    if truncated:
        payload["limit"] = MAX_LIST_ENTRIES
    return payload


def metadata(raw_path: str) -> dict[str, Any]:
    path = _resolve_existing_entry_path(raw_path)
    stat_result = _stat_existing(path, follow_symlinks=False)
    kind = _kind_from_mode(stat_result.st_mode)
    size = stat_result.st_size if stat.S_ISREG(stat_result.st_mode) else None
    mime = _guess_mime(path) if kind == "file" else None
    # Content sniff so the editor can open extensionless / unknown-type text files instead of forcing
    # a download. Name/extension alone can't tell a text `LICENSE` from a binary blob.
    text = kind == "file" and _looks_like_text(path)
    return {
        "ok": True,
        "name": path.name,
        "ext": _extension(path),
        "kind": kind,
        "size": size,
        "mtime": _mtime_seconds(stat_result),
        "mime": mime,
        "text": text,
    }


def file_content(raw_path: str, *, download: bool = False) -> FileContent:
    path = _require_regular_file(raw_path)
    stat_result = _stat_existing(path)
    if stat_result.st_size > MAX_FILE_BYTES:
        raise FileBrowserError("too_large", "File is too large", 413)
    mime = _guess_mime(path)
    base_mime = mime.split(";", 1)[0].strip().lower()
    disposition = "attachment" if download or base_mime not in INLINE_SAFE_CONTENT_TYPES else "inline"
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            stat_result = os.fstat(fd)
            if not stat.S_ISREG(stat_result.st_mode):
                raise FileBrowserError("not_file", "Path is not a regular file", 400)
            if stat_result.st_size > MAX_FILE_BYTES:
                raise FileBrowserError("too_large", "File is too large", 413)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
    except FileNotFoundError as exc:
        raise NotFoundError() from exc
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc
    if len(data) > MAX_FILE_BYTES:
        raise FileBrowserError("too_large", "File is too large", 413)
    return FileContent(path=path, mime=mime, disposition=disposition, data=data)


def _audit_mutation(op: str, path: Path, **extra: Any) -> None:
    if extra:
        logger.info("file_browser.%s path=%s extra=%s", op, path, extra)
    else:
        logger.info("file_browser.%s path=%s", op, path)


def _run_mutation(op: str, path: Path, func, **audit_extra: Any):
    _audit_mutation(op, path, **audit_extra)
    return func()


def _fsync_dir(path: Path) -> None:
    # Persist the directory entry change (e.g. an os.replace rename) so the new
    # name survives a crash, not just the file's contents. Best-effort: some
    # platforms (Windows) lack O_DIRECTORY or can't fsync a directory.
    flags = getattr(os, "O_DIRECTORY", None)
    if flags is None:
        return
    try:
        dir_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write_lock_for(path: Path) -> threading.Lock:
    # Retained for the process lifetime; this keeps compare-and-replace cheap and
    # per-path without a cleanup protocol that could race active worker threads.
    key = str(path)
    with _WRITE_LOCKS_MUTEX:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def write_file(
    raw_path: str, content: str, *, expected_mtime: float | None = None, create_only: bool = False
) -> dict[str, Any]:
    if not isinstance(content, str):
        raise FileBrowserError("invalid_content", "Content must be UTF-8 text", 400)
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise FileBrowserError("too_large", "Content is too large", 413)

    # Operate on the entry itself (parent resolved, final component NOT
    # symlink-followed) so writing matches the no-follow semantics of
    # list/meta/delete and never silently clobbers a symlink's target.
    target = _resolve_entry_path(raw_path)
    parent = target.parent
    if not parent.is_dir():
        raise FileBrowserError("not_dir", "Parent is not a directory", 400)
    if target.is_symlink():
        raise FileBrowserError("is_symlink", "Refusing to write through a symlink", 400)
    if target.exists() and not target.is_file():
        raise FileBrowserError("not_file", "Path is not a regular file", 400)
    if expected_mtime is not None:
        try:
            current_mtime = _mtime_seconds(target.stat())
        except FileNotFoundError as exc:
            raise ConflictError("conflict", "File was removed before save") from exc
        if abs(current_mtime - float(expected_mtime)) > 1e-6:
            raise ConflictError("conflict", "File changed on disk")

    def _write() -> dict[str, Any]:
        with _write_lock_for(target):
            # Re-check at write time to defeat a file→symlink swap between the
            # checks above and the replace below.
            if target.is_symlink():
                raise FileBrowserError("is_symlink", "Refusing to write through a symlink", 400)
            if target.exists() and not target.is_file():
                raise FileBrowserError("not_file", "Path is not a regular file", 400)
            # create_only ("New File"): create the final path atomically with O_EXCL — no temp +
            # replace, so it can never overwrite a file another writer/move/external process slipped
            # in. O_EXCL fails outright if the path already exists.
            if create_only:
                try:
                    excl_fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                except FileExistsError as exc:
                    raise ConflictError("exists", "Path already exists") from exc
                except PermissionError as exc:
                    raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
                except OSError as exc:
                    raise FileBrowserError("fs_error", str(exc), 400) from exc
                created_ino = os.fstat(excl_fd).st_ino
                try:
                    with os.fdopen(excl_fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as exc:
                    # Remove the partial file we created so a retry isn't blocked by a stale
                    # `exists` — but ONLY if the path still resolves to OUR inode, so we never delete
                    # a file an external writer slipped in between the failure and here (mirrors the
                    # hard-link rollback guard).
                    try:
                        if os.stat(target).st_ino == created_ino:
                            os.unlink(target)
                    except OSError:
                        pass
                    raise FileBrowserError("fs_error", str(exc), 400) from exc
                _fsync_dir(parent)
                return {"ok": True, "mtime": _mtime_seconds(target.stat())}
            fd = -1
            temp_name = ""
            try:
                mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
                fd, temp_name = tempfile.mkstemp(prefix=_WRITE_TEMP_PREFIX, suffix=".tmp", dir=parent)
                if mode is not None:
                    os.fchmod(fd, mode)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if expected_mtime is not None:
                    try:
                        disk_mtime = _mtime_seconds(target.stat())
                    except FileNotFoundError as exc:
                        raise ConflictError("conflict", "File was removed before save") from exc
                    if abs(disk_mtime - float(expected_mtime)) > 1e-6:
                        raise ConflictError("conflict", "File changed on disk")
                os.replace(temp_name, target)
                temp_name = ""
                _fsync_dir(parent)
                stat_result = target.stat()
                return {"ok": True, "mtime": _mtime_seconds(stat_result)}
            except PermissionError as exc:
                raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
            except OSError as exc:
                raise FileBrowserError("fs_error", str(exc), 400) from exc
            finally:
                if fd >= 0:
                    os.close(fd)
                if temp_name:
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass

    return _run_mutation("write", target, _write)


def make_directory(raw_path: str) -> dict[str, Any]:
    # Resolve the entry without following a final symlink, so mkdir on a name that
    # is a (possibly dangling) symlink reports "exists" instead of creating the
    # symlink's target — matching the no-follow semantics of the other mutations.
    target = _resolve_entry_path(raw_path)
    parent = target.parent
    if not parent.is_dir():
        raise FileBrowserError("not_dir", "Parent is not a directory", 400)
    if _exists_no_follow(target):
        raise ConflictError("exists", "Path already exists")

    def _mkdir() -> dict[str, Any]:
        try:
            target.mkdir()
            return {"ok": True}
        except FileExistsError as exc:
            raise ConflictError("exists", "Path already exists") from exc
        except OSError as exc:
            raise FileBrowserError("fs_error", str(exc), 400) from exc

    return _run_mutation("mkdir", target, _mkdir)


def _validate_new_name(new_name: str) -> str:
    if not isinstance(new_name, str) or not new_name.strip():
        raise FileBrowserError("invalid_name", "New name is required", 400)
    name = new_name.strip()
    if "\x00" in name or name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise FileBrowserError("invalid_name", "New name must not contain path separators", 400)
    return name


def _resolve_upload_directory(raw_dir: str) -> Path:
    # Shares the no-follow directory resolver; only remaps its errors to the upload API's
    # historical contract (an unusable destination reads as "not found", not invalid_path).
    try:
        return resolve_existing_directory(raw_dir)
    except NotFoundError as exc:
        raise NotFoundError("Destination directory not found") from exc
    except FileBrowserError as exc:
        if exc.code == "invalid_path":
            raise NotFoundError("Destination directory not found") from exc
        if exc.code == "not_dir":
            raise FileBrowserError("not_dir", "Destination is not a directory", 400) from exc
        raise


def _open_stable_upload_directory(directory: Path) -> int | None:
    """Pin the validated destination directory with an fd where the platform can.

    The fd keeps the whole streaming window (validate → temp → publish) inside the
    directory inode that was validated, so a writable-parent swap cannot redirect the
    upload. On platforms that cannot open a directory fd (native Windows), return
    ``None`` — the upload then operates path-based with no-follow re-checks, the same
    stability level as the sibling list/write/rename mutations there.
    """
    try:
        expected = directory.lstat()
    except FileNotFoundError as exc:
        raise NotFoundError("Destination directory not found") from exc
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc
    if not stat.S_ISDIR(expected.st_mode):
        raise FileBrowserError("not_dir", "Destination is not a directory", 400)

    if os.name != "posix":
        return None

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(directory, flags)
        current = os.fstat(fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise FileBrowserError("not_dir", "Destination is not a directory", 400)
        return fd
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FileBrowserError("not_dir", "Destination is not a directory", 400) from exc
        raise FileBrowserError("fs_error", str(exc), 400) from exc
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise


# Errnos meaning "this filesystem cannot hard-link" (FAT/exFAT, SMB/CIFS, some NFS):
# fall back to the guarded non-atomic publish below. EPERM is included because CIFS
# reports link refusal as EPERM; a genuine permission problem then surfaces from the
# fallback's replace with the right error anyway.
_LINK_UNSUPPORTED_ERRNOS = {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM}


def _upload_entry_ref(dir_fd: int | None, directory: Path, name: str) -> str:
    """Name to pass to os.* alongside ``dir_fd``: relative when pinned, absolute when not."""
    return name if dir_fd is not None else str(directory / name)


def _validate_upload_target_at(dir_fd: int | None, directory: Path, target_name: str, *, overwrite: bool) -> int | None:
    try:
        stat_result = os.stat(_upload_entry_ref(dir_fd, directory, target_name), dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc
    if not overwrite or not stat.S_ISREG(stat_result.st_mode):
        raise ConflictError("exists", "Destination already exists")
    return stat.S_IMODE(stat_result.st_mode)


def _create_upload_temp_at(dir_fd: int | None, directory: Path) -> tuple[int, str]:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(100):
        temp_name = f"{_WRITE_TEMP_PREFIX}{uuid.uuid4().hex}.tmp"
        try:
            return os.open(_upload_entry_ref(dir_fd, directory, temp_name), flags, 0o666, dir_fd=dir_fd), temp_name
        except FileExistsError:
            continue
        except PermissionError as exc:
            raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
        except OSError as exc:
            raise FileBrowserError("fs_error", str(exc), 400) from exc
    raise FileBrowserError("fs_error", "Could not reserve upload temp file", 400)


def _publish_upload_temp_at(
    dir_fd: int | None, directory: Path, temp_name: str, target_name: str, *, overwrite: bool
) -> None:
    temp_ref = _upload_entry_ref(dir_fd, directory, temp_name)
    target_ref = _upload_entry_ref(dir_fd, directory, target_name)
    if overwrite:
        os.replace(temp_ref, target_ref, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return
    # Create-only publish ladder, mirroring _os_rename_noreplace's policy: hard link is
    # the atomic no-replace primitive where the filesystem has one; where it doesn't
    # (FAT/exFAT/SMB), degrade to a guarded check+replace with the same warning the
    # rename fallback emits. renameat2(RENAME_NOREPLACE) is not a useful middle rung
    # here: filesystems that refuse link() refuse its flags too.
    try:
        os.link(temp_ref, target_ref, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError as exc:
        raise ConflictError("exists", "Destination already exists") from exc
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
            raise
        _warn_rename_noreplace_fallback()
        try:
            os.stat(target_ref, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ConflictError("exists", "Destination already exists") from exc
        os.replace(temp_ref, target_ref, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return
    _unlink_upload_temp_after_link(dir_fd, directory, temp_name, target_name)


def _unlink_upload_temp_after_link(dir_fd: int | None, directory: Path, temp_name: str, target_name: str) -> None:
    try:
        os.unlink(_upload_entry_ref(dir_fd, directory, temp_name), dir_fd=dir_fd)
    except OSError:
        _remove_created_upload_link_at(dir_fd, directory, temp_name, target_name)
        raise


def _remove_created_upload_link_at(dir_fd: int | None, directory: Path, temp_name: str, target_name: str) -> None:
    try:
        temp_stat = os.stat(_upload_entry_ref(dir_fd, directory, temp_name), dir_fd=dir_fd, follow_symlinks=False)
        target_stat = os.stat(_upload_entry_ref(dir_fd, directory, target_name), dir_fd=dir_fd, follow_symlinks=False)
        if (temp_stat.st_dev, temp_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            os.unlink(_upload_entry_ref(dir_fd, directory, target_name), dir_fd=dir_fd)
    except OSError:
        logger.debug("Failed to remove rollback upload hard link after temp unlink failure", exc_info=True)


def upload_file(
    raw_dir: str,
    source: BinaryIO,
    *,
    filename: str | None = None,
    name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    directory = _resolve_upload_directory(raw_dir)
    target_name = _validate_new_name(name if name is not None and name.strip() else (filename or ""))
    target = directory / target_name
    dir_fd = _open_stable_upload_directory(directory)
    try:
        _validate_upload_target_at(dir_fd, directory, target_name, overwrite=overwrite)

        def _upload() -> dict[str, Any]:
            with _write_lock_for(target):
                current_mode = _validate_upload_target_at(dir_fd, directory, target_name, overwrite=overwrite)
                fd = -1
                temp_name = ""
                size = 0
                try:
                    fd, temp_name = _create_upload_temp_at(dir_fd, directory)
                    if current_mode is not None and hasattr(os, "fchmod"):
                        os.fchmod(fd, current_mode)
                    with os.fdopen(fd, "wb") as handle:
                        fd = -1
                        while True:
                            chunk = source.read(_UPLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            if not isinstance(chunk, bytes):
                                chunk = bytes(chunk)
                            if size + len(chunk) > MAX_FILE_BYTES:
                                raise FileBrowserError("too_large", "File is too large", 413)
                            handle.write(chunk)
                            size += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    _publish_upload_temp_at(dir_fd, directory, temp_name, target_name, overwrite=overwrite)
                    temp_name = ""
                    if dir_fd is not None:
                        try:
                            os.fsync(dir_fd)
                        except OSError:
                            pass
                    else:
                        _fsync_dir(directory)
                    stat_result = os.stat(
                        _upload_entry_ref(dir_fd, directory, target_name), dir_fd=dir_fd, follow_symlinks=False
                    )
                    return {
                        "name": target.name,
                        "path": str(target),
                        "size": stat_result.st_size,
                        "mtime": _mtime_seconds(stat_result),
                    }
                except FileExistsError as exc:
                    raise ConflictError("exists", "Destination already exists") from exc
                except PermissionError as exc:
                    raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
                except OSError as exc:
                    raise FileBrowserError("fs_error", str(exc), 400) from exc
                finally:
                    if fd >= 0:
                        os.close(fd)
                    if temp_name:
                        try:
                            os.unlink(_upload_entry_ref(dir_fd, directory, temp_name), dir_fd=dir_fd)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            logger.debug("Failed to clean up upload temp file", exc_info=True)

        return _run_mutation("upload", target, _upload, overwrite=overwrite)
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def rename_path(raw_path: str, new_name: str) -> dict[str, Any]:
    source = _resolve_existing_entry_path(raw_path)
    name = _validate_new_name(new_name)
    target = source.with_name(name)
    same_entry = _same_entry_no_follow(source, target)
    same_directory_entry = same_entry and _same_directory_entry(source, target)
    if same_directory_entry and name == source.name:
        return {"ok": True, "path": str(source)}
    if _exists_no_follow(target) and not same_directory_entry:
        raise ConflictError("exists", "Destination already exists")

    def _rename() -> dict[str, Any]:
        try:
            if same_directory_entry:
                source.rename(target)
                return {"ok": True, "path": str(target)}
            # Atomic no-replace rename: never clobber a destination that appears between the
            # precheck above and the rename itself.
            _rename_no_replace(source, target)
            return {"ok": True, "path": str(target)}
        except FileExistsError as exc:
            raise ConflictError("exists", "Destination already exists") from exc
        except OSError as exc:
            raise FileBrowserError("fs_error", str(exc), 400) from exc

    return _run_mutation("rename", source, _rename)


def move_path(raw_src: str, raw_dst: str, *, overwrite: bool = False) -> dict[str, Any]:
    source = _resolve_existing_entry_path(raw_src)
    target = _resolve_entry_path(raw_dst)
    if source == target:
        # Moving onto itself is a no-op; never unlink-then-move (it would delete it). Kept
        # before the subtree guard below so an exact self-move stays idempotent rather than
        # erroring as "into itself" — the guard is only meant to reject moves into a descendant.
        return {"ok": True}
    if _is_dir_no_follow(source) and _entry_contains_no_follow(source, target):
        raise FileBrowserError("invalid_path", "Cannot move a folder into itself", 400)
    if source.is_symlink():
        try:
            same_target = source.resolve() == target.resolve()
        except (OSError, RuntimeError):
            same_target = False
        if same_target:
            raise ConflictError("invalid_move", "Cannot move a symlink onto the file it points to")
    parent = target.parent
    if not parent.is_dir():
        raise FileBrowserError("not_dir", "Destination parent is not a directory", 400)
    if _exists_no_follow(target) and not overwrite:
        raise ConflictError("exists", "Destination already exists")
    if _is_dir_no_follow(target) and not _is_dir_no_follow(source):
        # No-follow: only a real directory may overwrite a directory. is_file() follows
        # symlinks and is False for a symlink/broken link, which would let a symlink replace
        # (and thus erase) a directory under overwrite=True.
        raise ConflictError("exists", "Cannot overwrite a directory with a non-directory")

    def _move() -> dict[str, Any]:
        backup: Path | None = None
        source_was_dir = _is_dir_no_follow(source)
        target_was_placed = False

        def mark_target_placed() -> None:
            nonlocal target_was_placed
            target_was_placed = True

        try:
            if not overwrite:
                _move_to_absent_target(source, target)
                return {"ok": True}
            # Re-check at move time for the overwrite path so file-vs-directory
            # conflicts are caught before the destination is moved aside.
            if _exists_no_follow(target):
                if _is_dir_no_follow(target) and not _is_dir_no_follow(source):
                    raise ConflictError("exists", "Cannot overwrite a directory with a non-directory")
                backup = _reserve_backup_path(target)
                target.rename(backup)
            try:
                _move_to_absent_target(source, target, on_target_placed=mark_target_placed)
            except Exception:
                if backup is not None:
                    _handle_failed_overwrite_move(
                        target,
                        backup,
                        source_was_dir=source_was_dir,
                        target_was_placed=target_was_placed,
                    )
                raise
            if backup is not None:
                _remove_backup_path(backup)
            return {"ok": True}
        except OSError as exc:
            raise FileBrowserError("fs_error", str(exc), 400) from exc

    return _run_mutation("move", source, _move, dst=str(target), overwrite=overwrite)


def copy_path(raw_src: str, raw_dst: str, *, overwrite: bool = False) -> dict[str, Any]:
    """Copy one entry while preserving modes; copied mtimes are not guaranteed.

    Sources are measured before staging and copied without following symlinks.
    The 2 GB default cap is configurable through
    ``AVIBE_FILES_COPY_SIZE_CAP_BYTES``. The completed copy is published from a
    sibling temporary entry, so the destination never exposes a partial tree.
    """
    source = _resolve_existing_entry_path(raw_src)
    target = _resolve_entry_path(raw_dst)
    _validate_new_name(_raw_final_component(raw_dst))
    source_stat = _stat_existing(source, follow_symlinks=False)
    source_is_dir = stat.S_ISDIR(source_stat.st_mode)
    source_is_file = stat.S_ISREG(source_stat.st_mode)
    source_is_symlink = stat.S_ISLNK(source_stat.st_mode)
    if not (source_is_dir or source_is_file or source_is_symlink):
        raise FileBrowserError("fs_error", "Unsupported source type", 400)
    if source_is_dir and _entry_contains_no_follow(source, target):
        raise FileBrowserError("invalid_path", "Cannot copy a folder into itself", 400)
    if source_is_symlink:
        try:
            same_target = source.resolve() == target.resolve()
        except (OSError, RuntimeError):
            same_target = False
        if same_target:
            raise ConflictError("invalid_copy", "Cannot copy a symlink onto the file it points to")
    if not target.parent.is_dir():
        raise FileBrowserError("not_dir", "Destination parent is not a directory", 400)
    _validate_copy_target(target, source_is_dir=source_is_dir, overwrite=overwrite)

    source_fd: int | None = None
    source_link: str | None = None
    size_cap = _copy_total_size_cap_bytes()
    try:
        if source_is_dir and os.name == "posix":
            source_fd = _open_copy_directory(source, source_stat)
            scan_fd = _open_copy_directory(".", source_stat, dir_fd=source_fd)
            try:
                _measure_copy_directory_fd(scan_fd, size_cap)
            finally:
                os.close(scan_fd)
        elif source_is_dir:
            _measure_copy_directory_path(source, source_stat, size_cap)
        elif source_is_file:
            _add_copy_size(0, source_stat.st_size, size_cap)
            source_fd = _open_copy_file(source, source_stat)
        else:
            source_link = os.readlink(source)

        def _copy() -> dict[str, Any]:
            with _write_lock_for(target):
                _validate_copy_target(target, source_is_dir=source_is_dir, overwrite=overwrite)
                stage: Path | None = None
                try:
                    if source_is_dir:
                        stage = _new_copy_stage_path(target)
                        budget = [0]
                        if source_fd is not None:
                            copy_fd = _open_copy_directory(".", source_stat, dir_fd=source_fd)
                            try:
                                _copy_directory_from_fd(copy_fd, stage, budget, size_cap)
                            finally:
                                os.close(copy_fd)
                        else:
                            _copy_directory_from_path(source, source_stat, stage, budget, size_cap)
                    elif source_is_file:
                        stage = _new_copy_stage_path(target)
                        assert source_fd is not None
                        os.lseek(source_fd, 0, os.SEEK_SET)
                        _copy_file_from_fd(source_fd, stage, source_stat, budget=[0], size_cap=size_cap)
                    else:
                        assert source_link is not None
                        stage = _stage_copy_symlink(target, source_link)
                    _publish_staged_copy(stage, target, source_is_dir=source_is_dir, overwrite=overwrite)
                    stage = None
                    _fsync_dir(target.parent)
                    return {"ok": True, "path": str(target)}
                finally:
                    if stage is not None:
                        _remove_copy_stage_best_effort(stage)

        return _run_mutation("copy", source, _copy, dst=str(target), overwrite=overwrite)
    except FileBrowserError:
        raise
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)


def _move_to_absent_target(
    source: Path,
    target: Path,
    *,
    on_target_placed: Callable[[], None] | None = None,
) -> None:
    try:
        _os_rename_noreplace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_cross_fs_move_to_absent_target(source, target, on_target_placed=on_target_placed)
    else:
        if on_target_placed is not None:
            on_target_placed()


def _copy_cross_fs_move_to_absent_target(
    source: Path,
    target: Path,
    *,
    on_target_placed: Callable[[], None] | None = None,
) -> None:
    temp_target: Path | None = _reserve_backup_path(target)
    try:
        if _is_dir_no_follow(source):
            shutil.copytree(source, temp_target, symlinks=True)
        else:
            shutil.copy2(source, temp_target, follow_symlinks=False)
        _os_rename_noreplace(temp_target, target)
        temp_target = None
        created_target_stat = target.lstat()
        if on_target_placed is not None:
            on_target_placed()
        try:
            _remove_backup_path(source)
        except OSError:
            if _can_delete_placed_target_after_source_removal_failure(source):
                _remove_created_cross_fs_move_target(target, source, created_target_stat)
            raise
    except Exception:
        if temp_target is not None:
            _remove_path_if_exists(temp_target)
        raise


def _reserve_backup_path(target: Path) -> Path:
    for _ in range(100):
        candidate = target.with_name(f".avibe-overwrite-{uuid.uuid4().hex}")
        if not _exists_no_follow(candidate):
            return candidate
    raise FileBrowserError("fs_error", "Could not reserve overwrite backup path", 400)


def _restore_move_backup(backup: Path, target: Path, *, replace_target: bool) -> None:
    if not _exists_no_follow(backup):
        return
    if replace_target and _exists_no_follow(target):
        _remove_backup_path(target)
    try:
        _os_rename_noreplace(backup, target)
    except ConflictError:
        if not replace_target:
            logger.debug("Leaving overwrite backup in place because target is occupied during restore")
            return
        raise


def _handle_failed_overwrite_move(
    target: Path,
    backup: Path,
    *,
    source_was_dir: bool,
    target_was_placed: bool,
) -> None:
    if source_was_dir and target_was_placed and _exists_no_follow(target):
        _remove_path_if_exists_best_effort(backup)
        return
    _restore_move_backup(backup, target, replace_target=target_was_placed)


def _can_delete_placed_target_after_source_removal_failure(source: Path) -> bool:
    # Non-directory source cleanup uses unlink, which is atomic: if it failed and
    # the source still exists, the source copy is intact. Directory cleanup uses
    # rmtree, which can partially delete children before raising.
    return _exists_no_follow(source) and not _is_dir_no_follow(source)


def _remove_created_cross_fs_move_target(target: Path, source: Path, created_target_stat: os.stat_result) -> None:
    try:
        target_stat = target.lstat()
        if (
            _exists_no_follow(source)
            and (target_stat.st_dev, target_stat.st_ino)
            == (created_target_stat.st_dev, created_target_stat.st_ino)
        ):
            _remove_backup_path(target)
    except OSError:
        logger.debug("Failed to remove rollback target after cross-filesystem move failure", exc_info=True)


def _remove_backup_path(path: Path) -> None:
    if _is_dir_no_follow(path):
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_path_if_exists(path: Path) -> None:
    try:
        _remove_backup_path(path)
    except FileNotFoundError:
        pass


def _remove_path_if_exists_best_effort(path: Path) -> None:
    try:
        _remove_path_if_exists(path)
    except OSError:
        logger.debug("Failed to remove move backup after preserving placed target", exc_info=True)


def _copy_total_size_cap_bytes() -> int:
    return _env_int(_COPY_TOTAL_SIZE_CAP_ENV, COPY_TOTAL_SIZE_CAP_BYTES)


def _validate_copy_target(target: Path, *, source_is_dir: bool, overwrite: bool) -> None:
    if not _exists_no_follow(target):
        return
    if not overwrite:
        raise ConflictError("exists", "Destination already exists")
    if _is_dir_no_follow(target) and not source_is_dir:
        raise ConflictError("exists", "Cannot overwrite a directory with a non-directory")


def _same_copy_entry(expected: os.stat_result, current: os.stat_result) -> bool:
    return (expected.st_dev, expected.st_ino, stat.S_IFMT(expected.st_mode)) == (
        current.st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
    )


def _open_copy_directory(path: str | Path, expected: os.stat_result, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, dir_fd=dir_fd)
    current = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode) or not _same_copy_entry(expected, current):
        os.close(fd)
        raise FileBrowserError("fs_error", "Source changed during copy", 400)
    return fd


def _open_copy_file(path: str | Path, expected: os.stat_result, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, dir_fd=dir_fd)
    current = os.fstat(fd)
    if not stat.S_ISREG(current.st_mode) or not _same_copy_entry(expected, current):
        os.close(fd)
        raise FileBrowserError("fs_error", "Source changed during copy", 400)
    return fd


def _add_copy_size(total: int, size: int, cap: int) -> int:
    total += size
    if total > cap:
        raise FileBrowserError("too_large", "Copy is too large", 413)
    return total


def _measure_copy_directory_fd(dir_fd: int, cap: int) -> int:
    total = 0
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(entry_stat.st_mode):
                total = _add_copy_size(total, entry_stat.st_size, cap)
            elif stat.S_ISDIR(entry_stat.st_mode):
                child_fd = _open_copy_directory(entry.name, entry_stat, dir_fd=dir_fd)
                try:
                    total = _add_copy_size(total, _measure_copy_directory_fd(child_fd, cap - total), cap)
                finally:
                    os.close(child_fd)
            elif not stat.S_ISLNK(entry_stat.st_mode):
                raise FileBrowserError("fs_error", "Unsupported source type", 400)
    return total


def _measure_copy_directory_path(source: Path, expected: os.stat_result, cap: int) -> int:
    current = source.lstat()
    if not stat.S_ISDIR(current.st_mode) or not _same_copy_entry(expected, current):
        raise FileBrowserError("fs_error", "Source changed during copy", 400)
    total = 0
    with os.scandir(source) as entries:
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            child = source / entry.name
            if stat.S_ISREG(entry_stat.st_mode):
                total = _add_copy_size(total, entry_stat.st_size, cap)
            elif stat.S_ISDIR(entry_stat.st_mode):
                total = _add_copy_size(
                    total,
                    _measure_copy_directory_path(child, entry_stat, cap - total),
                    cap,
                )
            elif not stat.S_ISLNK(entry_stat.st_mode):
                raise FileBrowserError("fs_error", "Unsupported source type", 400)
    return total


def _new_copy_stage_path(target: Path) -> Path:
    for _ in range(100):
        candidate = target.with_name(f"{_COPY_TEMP_PREFIX}{uuid.uuid4().hex}")
        if not _exists_no_follow(candidate):
            return candidate
    raise FileBrowserError("fs_error", "Could not reserve copy temp path", 400)


def _copy_file_from_fd(
    source_fd: int,
    target: Path,
    source_stat: os.stat_result,
    *,
    budget: list[int] | None = None,
    size_cap: int | None = None,
) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    target_fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(target_fd, "wb") as handle:
            target_fd = -1
            while True:
                chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if budget is not None and size_cap is not None:
                    budget[0] = _add_copy_size(budget[0], len(chunk), size_cap)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
            mode = stat.S_IMODE(source_stat.st_mode)
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            else:
                target.chmod(mode)
    finally:
        if target_fd >= 0:
            os.close(target_fd)


def _copy_symlink_from_fd(source_dir_fd: int, name: str, expected: os.stat_result, target: Path) -> None:
    link_target = os.readlink(name, dir_fd=source_dir_fd)
    current = os.stat(name, dir_fd=source_dir_fd, follow_symlinks=False)
    if not stat.S_ISLNK(current.st_mode) or not _same_copy_entry(expected, current):
        raise FileBrowserError("fs_error", "Source changed during copy", 400)
    os.symlink(link_target, target)


def _copy_directory_from_fd(dir_fd: int, target: Path, budget: list[int], size_cap: int) -> None:
    directory_stat = os.fstat(dir_fd)
    target.mkdir(mode=0o700)
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            child_target = target / entry.name
            if stat.S_ISDIR(entry_stat.st_mode):
                child_fd = _open_copy_directory(entry.name, entry_stat, dir_fd=dir_fd)
                try:
                    _copy_directory_from_fd(child_fd, child_target, budget, size_cap)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry_stat.st_mode):
                child_fd = _open_copy_file(entry.name, entry_stat, dir_fd=dir_fd)
                try:
                    _copy_file_from_fd(child_fd, child_target, entry_stat, budget=budget, size_cap=size_cap)
                finally:
                    os.close(child_fd)
            elif stat.S_ISLNK(entry_stat.st_mode):
                _copy_symlink_from_fd(dir_fd, entry.name, entry_stat, child_target)
            else:
                raise FileBrowserError("fs_error", "Unsupported source type", 400)
    target.chmod(stat.S_IMODE(directory_stat.st_mode))


def _copy_directory_from_path(
    source: Path,
    expected: os.stat_result,
    target: Path,
    budget: list[int],
    size_cap: int,
) -> None:
    directory_stat = source.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode) or not _same_copy_entry(expected, directory_stat):
        raise FileBrowserError("fs_error", "Source changed during copy", 400)
    target.mkdir(mode=0o700)
    with os.scandir(source) as entries:
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            child_source = source / entry.name
            child_target = target / entry.name
            if stat.S_ISDIR(entry_stat.st_mode):
                _copy_directory_from_path(child_source, entry_stat, child_target, budget, size_cap)
            elif stat.S_ISREG(entry_stat.st_mode):
                child_fd = _open_copy_file(child_source, entry_stat)
                try:
                    _copy_file_from_fd(child_fd, child_target, entry_stat, budget=budget, size_cap=size_cap)
                finally:
                    os.close(child_fd)
            elif stat.S_ISLNK(entry_stat.st_mode):
                link_target = os.readlink(child_source)
                current_link = child_source.lstat()
                if not stat.S_ISLNK(current_link.st_mode) or not _same_copy_entry(entry_stat, current_link):
                    raise FileBrowserError("fs_error", "Source changed during copy", 400)
                os.symlink(link_target, child_target)
            else:
                raise FileBrowserError("fs_error", "Unsupported source type", 400)
    target.chmod(stat.S_IMODE(directory_stat.st_mode))


def _stage_copy_symlink(target: Path, link_target: str) -> Path:
    for _ in range(100):
        stage = _new_copy_stage_path(target)
        try:
            os.symlink(link_target, stage)
            return stage
        except FileExistsError:
            continue
    raise FileBrowserError("fs_error", "Could not reserve copy temp path", 400)


def _publish_staged_copy(stage: Path, target: Path, *, source_is_dir: bool, overwrite: bool) -> None:
    if not overwrite:
        _os_rename_noreplace(stage, target)
        return
    if not _exists_no_follow(target):
        _os_rename_noreplace(stage, target)
        return
    if not source_is_dir:
        if _is_dir_no_follow(target):
            raise ConflictError("exists", "Cannot overwrite a directory with a non-directory")
        try:
            os.replace(stage, target)
        except (IsADirectoryError, NotADirectoryError) as exc:
            raise ConflictError("exists", "Destination type changed during copy") from exc
        return

    backup = _reserve_backup_path(target)
    _os_rename_noreplace(target, backup)
    published = False
    try:
        _os_rename_noreplace(stage, target)
        published = True
    except Exception:
        if not _exists_no_follow(target):
            try:
                _os_rename_noreplace(backup, target)
                backup = None
            except Exception:
                logger.exception("Failed to restore copy overwrite backup")
        raise
    finally:
        if published and backup is not None:
            _remove_copy_stage_best_effort(backup)


def _remove_copy_stage_best_effort(path: Path) -> None:
    try:
        if _is_dir_no_follow(path):
            _make_copy_tree_removable(path)
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Failed to remove copy staging path", exc_info=True)


def _make_copy_tree_removable(root: Path) -> None:
    stack = [(root, root.lstat())]
    while stack:
        directory, expected = stack.pop()
        _make_copy_directory_writable(directory, expected)
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(entry_stat.st_mode):
                    stack.append((directory / entry.name, entry_stat))


def _make_copy_directory_writable(path: Path, expected: os.stat_result) -> None:
    mode = stat.S_IMODE(expected.st_mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    if os.name == "posix":
        fd = _open_copy_directory(path, expected)
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        return
    current = path.lstat()
    if not stat.S_ISDIR(current.st_mode) or not _same_copy_entry(expected, current):
        raise FileBrowserError("fs_error", "Copy staging tree changed during cleanup", 400)
    path.chmod(mode)


def _delete_undo_remove_path(path: Path) -> bool:
    try:
        _remove_path_if_exists(path)
        return True
    except OSError:
        logger.debug("Failed to remove delete undo staging path", exc_info=True)
        return False


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Ignoring invalid integer value for %s", name)
        return default


def _delete_undo_ttl_seconds() -> int:
    return _env_int(_DELETE_UNDO_TTL_ENV, DELETE_UNDO_TTL_SECONDS)


def _delete_undo_max_entries() -> int:
    return _env_int(_DELETE_UNDO_MAX_ENTRIES_ENV, DELETE_UNDO_MAX_ENTRIES)


def _delete_undo_entry_size_cap_bytes() -> int:
    return _env_int(_DELETE_UNDO_ENTRY_SIZE_CAP_ENV, DELETE_UNDO_ENTRY_SIZE_CAP_BYTES)


def _delete_undo_total_size_cap_bytes() -> int:
    return _env_int(_DELETE_UNDO_TOTAL_SIZE_CAP_ENV, DELETE_UNDO_TOTAL_SIZE_CAP_BYTES)


def _delete_undo_root() -> Path:
    from config import paths

    return paths.get_runtime_dir() / _DELETE_UNDO_DIR_NAME


def _ensure_delete_undo_root() -> Path:
    root = _delete_undo_root()
    root.mkdir(parents=True, exist_ok=True)
    stat_result = root.lstat()
    if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISDIR(stat_result.st_mode):
        raise OSError(errno.ENOTDIR, "delete undo staging root is not a directory", str(root))
    return root


def _entry_size_no_follow(path: Path, *, cap: int) -> int | None:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            stat_result = current.lstat()
        except OSError:
            return None
        total += max(0, stat_result.st_size)
        if total > cap:
            return total
        if not stat.S_ISDIR(stat_result.st_mode):
            continue
        try:
            with os.scandir(current) as iterator:
                stack.extend(Path(entry.path) for entry in iterator)
        except OSError:
            return None
    return total


def _directory_empty_no_follow(path: Path) -> bool | None:
    try:
        with os.scandir(path) as iterator:
            return next(iterator, None) is None
    except OSError:
        return None


def _same_staged_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
    )


def _delete_staging_type_matches(original: os.stat_result, current: os.stat_result) -> bool:
    original_is_dir = stat.S_ISDIR(original.st_mode)
    current_is_dir = stat.S_ISDIR(current.st_mode)
    if original_is_dir != current_is_dir:
        return False
    return True


def _write_delete_undo_metadata(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_delete_undo_metadata(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_delete_undo_metadata(temp_path, payload)
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    except OSError:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _delete_undo_entry_from_dir(directory: Path) -> DeleteUndoEntry | None:
    if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        return None
    if not directory.is_dir() or directory.is_symlink():
        return None
    metadata_path = directory / _DELETE_UNDO_METADATA_NAME
    entry_path = directory / _DELETE_UNDO_ENTRY_NAME
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        original_path = Path(str(payload["original_path"]))
        parent_dev = int(payload["parent_dev"])
        parent_ino = int(payload["parent_ino"])
        deleted_at = float(payload["deleted_at"])
        metadata_size = int(payload["size"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not original_path.is_absolute()
        or "\x00" in str(original_path)
        or deleted_at < 0
        or metadata_size < 0
        or parent_dev < 0
        or parent_ino < 0
    ):
        return None
    if not _exists_no_follow(entry_path):
        return None
    effective_size_cap = min(_delete_undo_entry_size_cap_bytes(), _delete_undo_total_size_cap_bytes())
    if effective_size_cap <= 0:
        return None
    actual_size = _entry_size_no_follow(entry_path, cap=effective_size_cap)
    if actual_size is None:
        return None
    size = max(metadata_size, actual_size)
    return DeleteUndoEntry(
        token=directory.name,
        directory=directory,
        entry_path=entry_path,
        metadata_path=metadata_path,
        original_path=original_path,
        parent_dev=parent_dev,
        parent_ino=parent_ino,
        deleted_at=deleted_at,
        size=size,
    )


def _cancel_delete_undo_expiry_timer_locked() -> None:
    global _DELETE_UNDO_EXPIRY_TIMER
    global _DELETE_UNDO_EXPIRY_TIMER_DEADLINE
    global _DELETE_UNDO_EXPIRY_TIMER_ROOT
    if _DELETE_UNDO_EXPIRY_TIMER is not None:
        _DELETE_UNDO_EXPIRY_TIMER.cancel()
    _DELETE_UNDO_EXPIRY_TIMER = None
    _DELETE_UNDO_EXPIRY_TIMER_DEADLINE = None
    _DELETE_UNDO_EXPIRY_TIMER_ROOT = None


def _schedule_delete_undo_expiry_locked(root: Path, entries: list[DeleteUndoEntry], *, now: float) -> None:
    global _DELETE_UNDO_EXPIRY_TIMER
    global _DELETE_UNDO_EXPIRY_TIMER_DEADLINE
    global _DELETE_UNDO_EXPIRY_TIMER_ROOT
    ttl = _delete_undo_ttl_seconds()
    future_deadlines = [entry.deleted_at + ttl for entry in entries if entry.deleted_at + ttl > now]
    if not future_deadlines:
        _cancel_delete_undo_expiry_timer_locked()
        return
    deadline = min(future_deadlines)
    if (
        _DELETE_UNDO_EXPIRY_TIMER is not None
        and _DELETE_UNDO_EXPIRY_TIMER.is_alive()
        and _DELETE_UNDO_EXPIRY_TIMER_DEADLINE == deadline
        and _DELETE_UNDO_EXPIRY_TIMER_ROOT == root
    ):
        return
    _cancel_delete_undo_expiry_timer_locked()
    timer = threading.Timer(max(0.0, deadline - now), _run_delete_undo_expiry_timer, args=(root,))
    timer.daemon = True
    _DELETE_UNDO_EXPIRY_TIMER = timer
    _DELETE_UNDO_EXPIRY_TIMER_DEADLINE = deadline
    _DELETE_UNDO_EXPIRY_TIMER_ROOT = root
    timer.start()


def _run_delete_undo_expiry_timer(root: Path) -> None:
    with _DELETE_UNDO_LOCK:
        try:
            root_stat = root.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                _cancel_delete_undo_expiry_timer_locked()
                return
            _purge_delete_undo_store_locked(root)
        except OSError:
            _cancel_delete_undo_expiry_timer_locked()
            logger.debug("Delete undo expiry purge failed", exc_info=True)


def _purge_delete_undo_store_locked(
    root: Path, *, now: float | None = None, schedule_expiry: bool = True
) -> list[DeleteUndoEntry]:
    current_time = time.time() if now is None else now
    ttl = _delete_undo_ttl_seconds()
    entries: list[DeleteUndoEntry] = []
    try:
        children = list(root.iterdir())
    except FileNotFoundError:
        if schedule_expiry:
            _cancel_delete_undo_expiry_timer_locked()
        return []
    for child in children:
        entry = _delete_undo_entry_from_dir(child)
        if entry is None or current_time - entry.deleted_at > ttl:
            if _delete_undo_remove_path(child):
                _fsync_dir(root)
                continue
            if entry is None:
                continue
        elif entry.size > min(_delete_undo_entry_size_cap_bytes(), _delete_undo_total_size_cap_bytes()):
            if _delete_undo_remove_path(child):
                _fsync_dir(root)
                continue
        entries.append(entry)

    entries.sort(key=lambda item: (item.deleted_at, item.token))
    max_entries = _delete_undo_max_entries()
    while len(entries) > max_entries:
        if not _evict_one_delete_undo_entry(root, entries):
            break

    total_cap = _delete_undo_total_size_cap_bytes()
    total_size = sum(entry.size for entry in entries)
    while entries and total_size > total_cap:
        if not _evict_one_delete_undo_entry(root, entries):
            break
        total_size = sum(entry.size for entry in entries)
    if schedule_expiry:
        _schedule_delete_undo_expiry_locked(root, entries, now=current_time)
    return entries


def _evict_one_delete_undo_entry(root: Path, entries: list[DeleteUndoEntry]) -> bool:
    for index, victim in enumerate(entries):
        if _delete_undo_remove_path(victim.directory):
            entries.pop(index)
            _fsync_dir(root)
            return True
    return False


def _delete_undo_store_within_caps(entries: list[DeleteUndoEntry]) -> bool:
    return (
        len(entries) <= _delete_undo_max_entries()
        and sum(entry.size for entry in entries) <= _delete_undo_total_size_cap_bytes()
    )


def _discard_delete_undo_stage_or_raise(stage_dir: Path, root: Path, *, target_parent: Path | None = None) -> None:
    if not _delete_undo_remove_path(stage_dir):
        raise FileBrowserError("fs_error", "Failed to remove delete undo staging entry", 400)
    if target_parent is not None:
        _fsync_dir(target_parent)
    _fsync_dir(root)


def _restore_staged_directory_after_not_empty(staged_entry: Path, target: Path, stage_dir: Path, root: Path) -> None:
    try:
        _rename_no_replace(staged_entry, target)
    except ConflictError as exc:
        raise FileBrowserError("fs_error", "Failed to restore non-empty directory after staging", 400) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc
    _discard_delete_undo_stage_or_raise(stage_dir, root, target_parent=target.parent)


def _delete_undo_parent_matches(entry: DeleteUndoEntry) -> bool:
    try:
        parent_stat = entry.original_path.parent.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(parent_stat.st_mode)
        and not stat.S_ISLNK(parent_stat.st_mode)
        and (parent_stat.st_dev, parent_stat.st_ino) == (entry.parent_dev, entry.parent_ino)
    )


def _open_verified_delete_undo_parent(entry: DeleteUndoEntry) -> int | None:
    if os.name != "posix":
        if _delete_undo_parent_matches(entry):
            return None
        raise FileBrowserError("expired", "Undo token expired or unavailable", 410)

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(entry.original_path.parent, flags)
        parent_stat = os.fstat(fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_ISLNK(parent_stat.st_mode)
            or (parent_stat.st_dev, parent_stat.st_ino) != (entry.parent_dev, entry.parent_ino)
        ):
            raise FileBrowserError("expired", "Undo token expired or unavailable", 410)
        return fd
    except FileBrowserError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            raise FileBrowserError("expired", "Undo token expired or unavailable", 410) from exc
        if exc.errno in {errno.EACCES, errno.EPERM} or isinstance(exc, PermissionError):
            raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
        raise FileBrowserError("fs_error", str(exc), 400) from exc


def _delete_undo_target_exists(parent_fd: int | None, target: Path) -> bool:
    if parent_fd is None:
        return _exists_no_follow(target)
    try:
        os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc


def _fsync_delete_undo_parent(parent_fd: int | None, parent: Path) -> None:
    if parent_fd is None:
        _fsync_dir(parent)
        return
    try:
        os.fsync(parent_fd)
    except OSError:
        pass


def _ensure_delete_undo_initialized() -> None:
    global _DELETE_UNDO_INITIALIZED
    if _DELETE_UNDO_INITIALIZED:
        return
    with _DELETE_UNDO_LOCK:
        if _DELETE_UNDO_INITIALIZED:
            return
        try:
            root = _ensure_delete_undo_root()
            _purge_delete_undo_store_locked(root)
        except OSError:
            logger.debug("Delete undo staging initialization failed", exc_info=True)
            return
        _DELETE_UNDO_INITIALIZED = True


def _purge_delete_undo_store_best_effort() -> None:
    _ensure_delete_undo_initialized()
    with _DELETE_UNDO_LOCK:
        try:
            root = _ensure_delete_undo_root()
            _purge_delete_undo_store_locked(root)
        except OSError:
            logger.debug("Delete undo staging purge failed", exc_info=True)


def _delete_permanently(target: Path, target_stat: os.stat_result, *, recursive: bool) -> dict[str, Any]:
    try:
        if stat.S_ISDIR(target_stat.st_mode):
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
        return {"ok": True, "undo_token": None, "undo_expires_seconds": None}
    except OSError as exc:
        if stat.S_ISDIR(target_stat.st_mode) and not recursive and exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            raise ConflictError("not_empty", "Directory is not empty") from exc
        if isinstance(exc, FileNotFoundError):
            raise NotFoundError() from exc
        if isinstance(exc, PermissionError):
            raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
        raise FileBrowserError("fs_error", str(exc), 400) from exc


def _stage_delete_for_undo(target: Path, target_stat: os.stat_result, *, recursive: bool) -> dict[str, Any] | None:
    entry_size_cap = _delete_undo_entry_size_cap_bytes()
    total_size_cap = _delete_undo_total_size_cap_bytes()
    effective_size_cap = min(entry_size_cap, total_size_cap)
    if effective_size_cap <= 0:
        return None

    _ensure_delete_undo_initialized()
    ttl = _delete_undo_ttl_seconds()
    now = time.time()
    token = uuid.uuid4().hex

    with _DELETE_UNDO_LOCK:
        stage_dir: Path | None = None
        renamed_to_stage = False
        try:
            root = _ensure_delete_undo_root()
            existing_entries = _purge_delete_undo_store_locked(root, now=now)
            if not _delete_undo_store_within_caps(existing_entries):
                return None
            current_stat = target.lstat()
            if not _delete_staging_type_matches(target_stat, current_stat):
                return None
            current_is_dir = stat.S_ISDIR(current_stat.st_mode)
            if current_is_dir and not recursive and _directory_empty_no_follow(target) is not True:
                return None
            parent_stat = target.parent.lstat()
            if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
                return None
            size = _entry_size_no_follow(target, cap=effective_size_cap)
            if size is None or size > effective_size_cap:
                return None
            post_size_stat = target.lstat()
            if not _same_staged_entry(current_stat, post_size_stat):
                return None
            stage_dir = root / token
            stage_dir.mkdir(mode=0o700)
            _fsync_dir(root)
            metadata_path = stage_dir / _DELETE_UNDO_METADATA_NAME
            staged_entry = stage_dir / _DELETE_UNDO_ENTRY_NAME
            metadata_payload = {
                "original_path": str(target),
                "parent_dev": parent_stat.st_dev,
                "parent_ino": parent_stat.st_ino,
                "deleted_at": now,
                "size": size,
            }
            _write_delete_undo_metadata(metadata_path, metadata_payload)
            _fsync_dir(stage_dir)
            pre_rename_stat = target.lstat()
            if not _same_staged_entry(post_size_stat, pre_rename_stat):
                _delete_undo_remove_path(stage_dir)
                _fsync_dir(root)
                return None
            if stat.S_ISDIR(pre_rename_stat.st_mode) and not recursive and _directory_empty_no_follow(target) is not True:
                _delete_undo_remove_path(stage_dir)
                _fsync_dir(root)
                return None
            _os_rename_noreplace(target, staged_entry)
            renamed_to_stage = True
            if current_is_dir and not recursive and _directory_empty_no_follow(staged_entry) is not True:
                _restore_staged_directory_after_not_empty(staged_entry, target, stage_dir, root)
                raise ConflictError("not_empty", "Directory is not empty")
            staged_size = _entry_size_no_follow(staged_entry, cap=effective_size_cap)
            if staged_size is None or staged_size > effective_size_cap:
                _discard_delete_undo_stage_or_raise(stage_dir, root, target_parent=target.parent)
                return {"ok": True, "undo_token": None, "undo_expires_seconds": None}
            if staged_size != size:
                metadata_payload["size"] = staged_size
                _replace_delete_undo_metadata(metadata_path, metadata_payload)
        except OSError as exc:
            if stage_dir is not None:
                _discard_delete_undo_stage_or_raise(
                    stage_dir,
                    root,
                    target_parent=target.parent if renamed_to_stage else None,
                )
            if renamed_to_stage:
                logger.debug("Delete undo staging failed after rename; treating delete as permanent: %s", exc)
                return {"ok": True, "undo_token": None, "undo_expires_seconds": None}
            logger.debug("Delete undo staging failed; falling back to permanent delete: %s", exc)
            return None
        _fsync_dir(stage_dir)
        _fsync_dir(target.parent)
        try:
            entries = _purge_delete_undo_store_locked(root, now=now)
            staged_retained = any(entry.token == token for entry in entries) and _delete_undo_store_within_caps(entries)
        except OSError:
            logger.debug("Delete undo staging cap purge failed after staging", exc_info=True)
            staged_retained = True
        if not staged_retained:
            _discard_delete_undo_stage_or_raise(stage_dir, root, target_parent=target.parent)
            return {"ok": True, "undo_token": None, "undo_expires_seconds": None}
    return {"ok": True, "undo_token": token, "undo_expires_seconds": ttl}


def _validate_delete_undo_token(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise FileBrowserError("expired", "Undo token expired or unavailable", 410)
    return token


def delete_path(raw_path: str, *, recursive: bool = False) -> dict[str, Any]:
    target = _resolve_existing_entry_path(raw_path)
    if target == target.parent:
        # A filesystem root (/, or a drive root on Windows). Never deletable — a recursive
        # delete here would try to wipe everything the process can reach.
        raise FileBrowserError("invalid_path", "Refusing to delete a filesystem root", 400)
    target_stat = _stat_existing(target, follow_symlinks=False)

    def _delete() -> dict[str, Any]:
        _purge_delete_undo_store_best_effort()
        staged = _stage_delete_for_undo(target, target_stat, recursive=recursive)
        if staged is not None:
            return staged
        return _delete_permanently(target, target_stat, recursive=recursive)

    return _run_mutation("delete", target, _delete, recursive=recursive)


def undo_delete_path(token: str) -> dict[str, Any]:
    token = _validate_delete_undo_token(token)
    _ensure_delete_undo_initialized()
    with _DELETE_UNDO_LOCK:
        now = time.time()
        try:
            root = _ensure_delete_undo_root()
            _purge_delete_undo_store_locked(root, now=now)
        except OSError as exc:
            raise FileBrowserError("expired", "Undo token expired or unavailable", 410) from exc
        stage_dir = root / token
        entry = _delete_undo_entry_from_dir(stage_dir)
        if entry is None or now - entry.deleted_at > _delete_undo_ttl_seconds():
            _delete_undo_remove_path(stage_dir)
            raise FileBrowserError("expired", "Undo token expired or unavailable", 410)
        try:
            parent_fd = _open_verified_delete_undo_parent(entry)
        except FileBrowserError as exc:
            if exc.code == "expired":
                _delete_undo_remove_path(stage_dir)
                _fsync_dir(root)
            raise
        try:
            if _delete_undo_target_exists(parent_fd, entry.original_path):
                raise ConflictError("exists", "Original path already exists")

            def _restore() -> dict[str, Any]:
                try:
                    _rename_no_replace_into_dir(
                        entry.entry_path,
                        parent_fd,
                        entry.original_path.parent,
                        entry.original_path.name,
                    )
                except ConflictError:
                    raise
                except OSError as exc:
                    raise FileBrowserError("fs_error", str(exc), 400) from exc
                _fsync_delete_undo_parent(parent_fd, entry.original_path.parent)
                _fsync_dir(entry.directory)
                try:
                    entry.metadata_path.unlink()
                    entry.directory.rmdir()
                    _fsync_dir(root)
                except OSError:
                    logger.debug("Failed to clean up delete undo staging directory after restore", exc_info=True)
                return {"restored_path": str(entry.original_path)}

            return _run_mutation("delete_undo", entry.original_path, _restore)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)


# ---------------------------------------------------------------------------
# Cross-file search + replace
#
# Search walks an absolute root directory (the folder open in the editor) and
# scans text files line-by-line. Matching is unified through a single compiled
# regex so search, replace, and the UI preview all agree on what a "match" is.
# Replace is per-line (same semantics as search/preview) and snapshots the
# original text so the whole batch can be undone with one click.
# ---------------------------------------------------------------------------

SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
SEARCH_MAX_MATCHES = 1000
SEARCH_MAX_FILES = 200
SEARCH_LINE_PREVIEW_CHARS = 400
SEARCH_PREVIEW_CONTEXT = 60
SEARCH_NAME_MAX_RESULTS = 500
_BINARY_SNIFF_BYTES = 8192

# Directories never worth searching — VCS metadata, dependency/build output,
# caches. Pruned during the walk so we never descend into huge noise trees.
SEARCH_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", "dist", "build",
        ".next", ".turbo", ".parcel-cache", ".gradle", "target", ".idea", ".tox",
    }
)

_UNDO_TTL_SECONDS = 600.0
_UNDO_MAX_TOKENS = 32
_UNDO_MAX_BYTES = 64 * 1024 * 1024
_UNDO_STORE: dict[str, dict[str, Any]] = {}
_UNDO_LOCK = threading.Lock()


def _is_word_char(ch: str) -> bool:
    return re.match(r"\w", ch) is not None


def _compile_search_matcher(query: str, *, regex: bool, case_sensitive: bool, whole_word: bool) -> "re.Pattern[str]":
    if not isinstance(query, str) or query == "":
        raise FileBrowserError("invalid_query", "Search query is required", 400)
    pattern = query if regex else re.escape(query)
    if whole_word:
        # Guard an edge with \b only when the query's edge char is itself a word char. A symbol query
        # like "C++" or "foo()" ends in a non-word char, and \b needs a word/non-word transition, so
        # a both-sides \b would make it never match. (For regex queries the edge is taken from the raw
        # query text — a heuristic, but it fixes the common literal symbol case.)
        prefix = r"\b" if _is_word_char(query[0]) else ""
        suffix = r"\b" if _is_word_char(query[-1]) else ""
        pattern = f"{prefix}(?:{pattern}){suffix}"
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise FileBrowserError("invalid_regex", f"Invalid regular expression: {exc}", 400) from exc


def _split_globs(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [piece.strip() for piece in str(raw).replace("\n", ",").split(",") if piece.strip()]


def _match_globs(rel_posix: str, patterns: list[str]) -> bool:
    name = rel_posix.rsplit("/", 1)[-1]
    for pat in patterns:
        # A leading `**/` should also match at the root: fnmatch's `**/` needs a real slash, so
        # `**/*.py` would miss root `foo.py` and `**/dist/**` would miss root `dist/a.ts`. Also try
        # the pattern with the `**/` prefix stripped.
        forms = [pat, pat[3:]] if pat.startswith("**/") else [pat]
        if any(fnmatch.fnmatch(rel_posix, form) for form in forms) or fnmatch.fnmatch(name, pat):
            return True
    return False


def _looks_like_text(path: Path) -> bool:
    """Sniff whether a regular file is text (editable) by CONTENT, independent of name/extension.

    Mirrors the search reader's binary check: a NUL byte in the first chunk, or a head that decodes
    to mostly replacement characters, marks it binary. This lets extensionless text files (LICENSE,
    README, notes, config) open in the editor while true binaries (images, executables, archives)
    stay on the download path. Reads at most the sniff window, so it's cheap even for huge files.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with open(path, "rb") as handle:
            head = handle.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False
    if not head:
        return True  # an empty file is editable
    if b"\x00" in head:
        return False
    # Decode leniently and require almost no replacement characters: real text has none (or a single
    # multibyte char split at the sniff boundary), while binary content produces many.
    decoded = head.decode("utf-8", errors="replace")
    return decoded.count("�") <= max(1, len(decoded) // 100)


def _read_file_text(path: Path, *, lossy: bool) -> tuple[str | None, float | None]:
    """Read a regular text file for search/replace; returns (text, mtime) or (None, None) to skip.

    Skips directories, oversized files, and binaries (NUL in the first chunk). ``lossy`` controls
    undecodable bytes: search previews tolerate replacement characters; replace must not, so it
    returns None and leaves the file alone. The mtime is captured from the stat taken BEFORE the
    read, so it corresponds to (at most) the content returned — search hands that mtime back as the
    replace baseline, and a change during the read window then fails the later mtime check.
    """
    # The rest of the file browser treats symlinks as no-follow / non-editable; mirror that here so
    # search/replace can't follow a link out of the opened root (e.g. a planted
    # ``secret -> ~/.ssh/id_rsa``) and surface or rewrite content outside the tree.
    if path.is_symlink():
        return None, None
    try:
        st = path.stat()
    except OSError:
        return None, None
    if not stat.S_ISREG(st.st_mode) or st.st_size > SEARCH_MAX_FILE_BYTES:
        return None, None
    mtime = _mtime_seconds(st)
    try:
        with open(path, "rb") as handle:
            head = handle.read(_BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return None, None
            # Bound the read to the size limit even if the file grew since the stat above (TOCTOU, or
            # a growing special file), so one read can't pull unbounded bytes into memory. For
            # replace, a file that grew also fails the later mtime check, so no truncated write lands.
            data = head + handle.read(max(0, SEARCH_MAX_FILE_BYTES - len(head)))
    except OSError:
        return None, None
    try:
        return data.decode("utf-8"), mtime
    except UnicodeDecodeError:
        if not lossy:
            return None, None
        return data.decode("utf-8", errors="replace"), mtime


def _iter_search_files(root: Path, include: list[str], exclude: list[str]):
    """Yield (path, rel_posix) for every candidate file under ``root``."""
    exclude_names = [pat for pat in exclude if "/" not in pat]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SEARCH_SKIP_DIRS and not _match_globs(d, exclude_names)
        )
        base = Path(dirpath)
        for name in sorted(filenames):
            fpath = base / name
            rel = fpath.relative_to(root).as_posix()
            if include and not _match_globs(rel, include):
                continue
            if exclude and _match_globs(rel, exclude):
                continue
            yield fpath, rel


def _utf16_len(s: str) -> int:
    # Python indexes strings by code point; the React preview (String.slice) and Monaco columns
    # count UTF-16 code units. Convert so a non-BMP char (emoji, etc.) before a match doesn't shift
    # the highlight / jump selection.
    return len(s.encode("utf-16-le")) // 2


def _preview_for_match(line: str, start: int, end: int) -> tuple[str, int, int]:
    """Build a preview snippet that always contains the match, with UTF-16 col/end into it.

    Short lines are returned whole. For a long line (e.g. minified code) the snippet is windowed
    around the match — otherwise a hit past the first 400 chars would be sliced off and the row
    would show no highlight. A leading '…' marks a left-truncated window.
    """
    if len(line) <= SEARCH_LINE_PREVIEW_CHARS:
        col = _utf16_len(line[:start])
        return line, col, col + _utf16_len(line[start:end])
    win_start = max(0, start - SEARCH_PREVIEW_CONTEXT)
    snippet = line[win_start : win_start + SEARCH_LINE_PREVIEW_CHARS]
    prefix = "…" if win_start > 0 else ""
    text = prefix + snippet
    rel_start = len(prefix) + (start - win_start)
    rel_end = len(prefix) + min(end - win_start, len(snippet))
    return text, _utf16_len(text[:rel_start]), _utf16_len(text[:rel_end])


def _scan_lines(text: str, matcher: "re.Pattern[str]", remaining: int) -> tuple[list[dict[str, Any]], bool]:
    matches: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(text.split("\n"), start=1):
        # Drop a trailing CR so line-end anchors ('foo$') and previews behave on CRLF files; the CR
        # sits after any match, so offsets are unaffected.
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        for m in matcher.finditer(line):
            # Check the cap BEFORE recording: returning True only once we see a match BEYOND the
            # limit means a result set with exactly `remaining` matches isn't falsely flagged
            # truncated (which would needlessly disable Replace All).
            if len(matches) >= remaining:
                return matches, True
            # col/end are FULL-LINE UTF-16 offsets — the editor jump selects against the whole line.
            # preview_col/preview_end index into the (possibly windowed) preview `text` for the row
            # highlight; the two differ once a long line is truncated.
            full_col = _utf16_len(line[: m.start()])
            full_end = full_col + _utf16_len(line[m.start() : m.end()])
            preview, preview_col, preview_end = _preview_for_match(line, m.start(), m.end())
            matches.append(
                {
                    "line": line_no,
                    "col": full_col,
                    "end": full_end,
                    "text": preview,
                    "preview_col": preview_col,
                    "preview_end": preview_end,
                    "line_truncated": len(line) > SEARCH_LINE_PREVIEW_CHARS,
                }
            )
    return matches, False


def search(
    raw_root: str,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    whole_word: bool = False,
    include: str = "",
    exclude: str = "",
    max_matches: int = SEARCH_MAX_MATCHES,
    max_files: int = SEARCH_MAX_FILES,
) -> dict[str, Any]:
    root = _require_directory(raw_root)
    matcher = _compile_search_matcher(query, regex=regex, case_sensitive=case_sensitive, whole_word=whole_word)
    include_globs = _split_globs(include)
    exclude_globs = _split_globs(exclude)

    results: list[dict[str, Any]] = []
    total_matches = 0
    truncated = False
    reason: str | None = None
    for fpath, rel in _iter_search_files(root, include_globs, exclude_globs):
        text, file_mtime = _read_file_text(fpath, lossy=True)
        if text is None:
            continue
        file_matches, overflowed = _scan_lines(text, matcher, max_matches - total_matches)
        if not file_matches:
            # `remaining` was already 0 and this file still holds a match → a genuine hidden match.
            if overflowed:
                truncated = True
                reason = "matches"
                break
            continue
        if len(results) >= max_files:
            # This matching file is the (max_files + 1)th — more files match than we display.
            truncated = True
            reason = "files"
            break
        results.append({"path": str(fpath), "rel": rel, "mtime": file_mtime, "match_count": len(file_matches), "matches": file_matches})
        total_matches += len(file_matches)
        if overflowed:
            truncated = True
            reason = "matches"
            break
    return {
        "root": str(root),
        "query": query,
        "results": results,
        "total_matches": total_matches,
        "total_files": len(results),
        "truncated": truncated,
        "truncated_reason": reason,
    }


def _name_search_hit(path: Path, root: Path) -> dict[str, Any] | None:
    """Build one name-search result row for ``path`` (no-follow stat), or None if it vanished.

    Mirrors ``_entry_payload`` but adds the absolute ``path`` and the ``rel`` path from ``root`` so
    the UI can render the containing folder and navigate/open the hit directly.
    """
    try:
        st = path.lstat()
    except OSError:
        return None
    kind = _kind_from_mode(st.st_mode)
    size = st.st_size if stat.S_ISREG(st.st_mode) else None
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    return {
        "name": path.name,
        "kind": kind,
        "size": size,
        "mtime": _mtime_seconds(st),
        "ext": _extension(path) if kind != "dir" else "",
        "path": str(path),
        "rel": rel,
    }


def search_names(
    raw_root: str,
    query: str,
    *,
    show_hidden: bool = False,
    max_results: int = SEARCH_NAME_MAX_RESULTS,
) -> dict[str, Any]:
    """Recursively find entries under ``root`` whose NAME matches ``query``.

    Case-insensitive substring match over both file AND directory names — the file-browser
    counterpart to the content ``search`` above (which greps file *contents* and never returns
    directories). Prunes the same heavy noise trees (.git / node_modules / build output) so a search
    can't stall descending into them, and skips dotfiles/dirs unless ``show_hidden``. Results come
    back in walk order (shallow first, a rough relevance signal) and are bounded by ``max_results``;
    ``truncated`` is set when the cap is hit so the UI can say results were limited.
    """
    root = _require_directory(raw_root)
    needle = query.strip().lower()
    if not needle:
        raise FileBrowserError("invalid_query", "Search query is required", 400)

    results: list[dict[str, Any]] = []
    truncated = False
    # os.walk swallows per-directory errors by default, so an unreadable subtree is silently skipped
    # (the search keeps going) rather than aborting the whole walk.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in place: drop noise trees and (unless show_hidden) hidden dirs so os.walk never
        # descends into them. The retained dir names are still candidates to MATCH below.
        dirnames[:] = sorted(
            d for d in dirnames if d not in SEARCH_SKIP_DIRS and (show_hidden or not d.startswith("."))
        )
        base = Path(dirpath)
        # Directories first, then files, each alphabetically — mirrors the listing's grouping.
        for name in list(dirnames) + sorted(filenames):
            if not show_hidden and name.startswith("."):
                continue
            if needle not in name.lower():
                continue
            # Check the cap BEFORE recording (mirrors the content search): flagging truncated only on
            # a match BEYOND the cap means a result set of exactly max_results isn't falsely marked
            # truncated (which would make the UI claim results were limited when they weren't).
            if len(results) >= max_results:
                truncated = True
                break
            hit = _name_search_hit(base / name, root)
            if hit is None:
                continue
            results.append(hit)
        if truncated:
            break

    return {
        "ok": True,
        "root": str(root),
        "query": query,
        "results": results,
        "truncated": truncated,
        "limit": max_results if truncated else None,
    }


def _apply_replacement(text: str, matcher: "re.Pattern[str]", replacement: str, *, regex: bool) -> tuple[str, int]:
    # Per-line replace so the count and behavior match the per-line search/preview.
    # Literal mode treats the replacement verbatim (a lambda avoids backref expansion);
    # regex mode keeps Python's \1-style backreferences.
    repl: Any = replacement if regex else (lambda _m: replacement)
    out: list[str] = []
    total = 0
    for raw_line in text.split("\n"):
        # Match the same CR-stripped content search did, then re-attach the terminator so CRLF files
        # keep their line endings.
        cr = "\r" if raw_line.endswith("\r") else ""
        core = raw_line[: -len(cr)] if cr else raw_line
        try:
            new_core, count = matcher.subn(repl, core)
        except (re.error, IndexError) as exc:
            # A bad replacement template (out-of-range \1 or unknown named group \g<x>) raises
            # re.error or IndexError; surface a clean 400 instead of a 500.
            raise FileBrowserError("invalid_regex", f"Invalid replacement: {exc}", 400) from exc
        out.append(new_core + cr)
        total += count
    return "\n".join(out), total


def _store_undo(token: str, files: dict[str, Any]) -> None:
    now = time.monotonic()
    size = sum(len(snap["original"].encode("utf-8")) for snap in files.values())
    with _UNDO_LOCK:
        for stale in [key for key, value in _UNDO_STORE.items() if now - value["created"] > _UNDO_TTL_SECONDS]:
            _UNDO_STORE.pop(stale, None)
        # Bound the store by BOTH token count and total snapshot bytes so a few large Replace All
        # batches can't pin unbounded memory. Evict oldest-first until the new entry fits; a single
        # batch larger than the budget still gets stored (it's the user's latest, undoable action)
        # but evicts everything else.
        def total_bytes() -> int:
            return sum(value["bytes"] for value in _UNDO_STORE.values())

        while _UNDO_STORE and (len(_UNDO_STORE) >= _UNDO_MAX_TOKENS or total_bytes() + size > _UNDO_MAX_BYTES):
            oldest = min(_UNDO_STORE, key=lambda key: _UNDO_STORE[key]["created"])
            _UNDO_STORE.pop(oldest, None)
        _UNDO_STORE[token] = {"created": now, "files": files, "bytes": size}


def replace(
    raw_root: str,
    query: str,
    replacement: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    whole_word: bool = False,
    include: str = "",
    exclude: str = "",
    paths: list[str] | None = None,
    expected_mtimes: dict[str, float] | None = None,
    max_files: int = SEARCH_MAX_FILES,
) -> dict[str, Any]:
    if not isinstance(replacement, str):
        raise FileBrowserError("invalid_content", "Replacement must be text", 400)
    root = _require_directory(raw_root)
    matcher = _compile_search_matcher(query, regex=regex, case_sensitive=case_sensitive, whole_word=whole_word)

    pre_skipped: list[dict[str, Any]] = []
    # `paths is not None` (not truthiness) marks explicit mode: an empty list means "replace none of
    # the shown files" (a no-op), never "walk and rewrite the whole root".
    if paths is not None:
        candidates: list[tuple[Path, str]] = []
        for raw in paths:
            try:
                resolved = _require_regular_file(raw)
            except FileBrowserError as exc:
                # A displayed file deleted/renamed/replaced since the search must be reported as
                # skipped, not abort the whole batch (the other shown files should still apply).
                pre_skipped.append({"path": raw, "rel": raw, "reason": exc.code})
                continue
            # Search skips symlinks; an explicit path that is now a symlink (e.g. swapped in for a
            # shown file after the search) must not be followed, or replace could write through it to
            # a target outside the root.
            if _expanded_absolute_path(raw).is_symlink():
                pre_skipped.append({"path": raw, "rel": raw, "reason": "symlink"})
                continue
            if resolved != root and root not in resolved.parents:
                raise FileBrowserError("invalid_path", "Path is outside the search root", 400)
            candidates.append((resolved, resolved.relative_to(root).as_posix()))
    else:
        # Lazy: iterate the walk and stop at the file cap (the loop breaks) rather than
        # materializing every candidate under the root up front.
        candidates = _iter_search_files(root, _split_globs(include), _split_globs(exclude))

    # In paths mode the caller is replacing the exact files shown in the search results, so a file
    # we now can't process (vanished, or not strict-UTF-8 while search saw it via a lossy decode of
    # an ASCII match) is reported as skipped rather than silently dropped. In walk mode a None just
    # means "not a candidate", so stay quiet.
    explicit = paths is not None
    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = list(pre_skipped)
    snapshots: dict[str, Any] = {}
    total = 0
    truncated = False
    for fpath, rel in candidates:
        if len(changed) >= max_files:
            # Stop after the file cap, but tell the caller the batch was bounded so the UI doesn't
            # claim the whole root was rewritten.
            truncated = True
            break
        # Capture the mtime BEFORE reading, and hand it to write_file as expected_mtime. A
        # concurrent edit landing after this read makes the on-disk mtime diverge, so write_file
        # raises a conflict instead of overwriting the other writer with a replacement computed
        # from stale bytes.
        try:
            before_mtime = _mtime_seconds(fpath.stat())
        except OSError:
            if explicit:
                skipped.append({"path": str(fpath), "rel": rel, "reason": "missing"})
            continue
        # Reject a file that changed between the search the user previewed and this replace, so the
        # batch never rewrites matches they never saw (expected_mtimes carries the search-time mtime).
        if explicit and expected_mtimes is not None:
            expected = expected_mtimes.get(str(fpath))
            if expected is not None and abs(before_mtime - float(expected)) > 1e-6:
                skipped.append({"path": str(fpath), "rel": rel, "reason": "modified"})
                continue
        text, _ = _read_file_text(fpath, lossy=False)
        if text is None:
            if explicit:
                skipped.append({"path": str(fpath), "rel": rel, "reason": "unreadable"})
            continue
        new_text, count = _apply_replacement(text, matcher, replacement, regex=regex)
        if count == 0 or new_text == text:
            continue
        try:
            write_file(str(fpath), new_text, expected_mtime=before_mtime)
        except FileBrowserError as exc:
            # One file failing (read-only, vanished, concurrent edit) must NOT abort the whole batch
            # and strip undo from the files already replaced. Record it and keep going; the token
            # below still covers every file that did change.
            skipped.append({"path": str(fpath), "rel": rel, "reason": exc.code})
            continue
        snapshots[str(fpath)] = {
            "original": text,
            "after_sha": hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
        }
        changed.append({"path": str(fpath), "rel": rel, "replacements": count})
        total += count

    token: str | None = None
    if snapshots:
        token = uuid.uuid4().hex
        _store_undo(token, snapshots)
    return {
        "changed": changed,
        "skipped": skipped,
        "total_replacements": total,
        "files_changed": len(changed),
        "truncated": truncated,
        "undo_token": token,
    }


def undo_replace(token: str) -> dict[str, Any]:
    with _UNDO_LOCK:
        entry = _UNDO_STORE.pop(token, None)
    if not entry or time.monotonic() - entry["created"] > _UNDO_TTL_SECONDS:
        raise FileBrowserError("undo_unavailable", "Nothing to undo — it expired or was already undone", 404)

    restored: list[str] = []
    skipped: list[dict[str, str]] = []
    for path, snap in entry["files"].items():
        target = Path(path)
        # Read the mtime from the SAME window as the bytes we verify, and hand it to write_file as
        # expected_mtime. Otherwise a re-stat after hashing opens a race where an edit landing
        # between the hash check and the write would be silently clobbered by the restore.
        try:
            before_mtime = _mtime_seconds(target.stat())
            current = target.read_bytes()
        except FileNotFoundError:
            skipped.append({"path": path, "reason": "missing"})
            continue
        except OSError:
            skipped.append({"path": path, "reason": "unreadable"})
            continue
        # Only revert files still holding exactly what the replace wrote — never
        # clobber edits the user (or anything else) made after the replace.
        if hashlib.sha256(current).hexdigest() != snap["after_sha"]:
            skipped.append({"path": path, "reason": "modified"})
            continue
        try:
            write_file(path, snap["original"], expected_mtime=before_mtime)
        except FileBrowserError:
            skipped.append({"path": path, "reason": "write_failed"})
            continue
        restored.append(path)
    return {"restored": restored, "skipped": skipped}
