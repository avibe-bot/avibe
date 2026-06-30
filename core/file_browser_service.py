from __future__ import annotations

import ctypes
import errno
import fnmatch
import hashlib
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
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_LIST_ENTRIES = 5000

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


def _unlink_source_after_hard_link(source: Path, target: Path) -> None:
    try:
        os.unlink(source)
    except OSError:
        _remove_created_hard_link(target, source)
        raise


def _remove_created_hard_link(target: Path, source: Path) -> None:
    try:
        source_stat = source.lstat()
        target_stat = target.lstat()
        if (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            os.unlink(target)
    except OSError:
        logger.debug("Failed to remove rollback hard link after no-replace rename failure", exc_info=True)


def _glibc_renameat2_noreplace(source: Path, target: Path) -> None:
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
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), str(target))
        raise OSError(err, os.strerror(err), str(target))


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
    if name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise FileBrowserError("invalid_name", "New name must not contain path separators", 400)
    return name


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


def delete_path(raw_path: str, *, recursive: bool = False) -> dict[str, Any]:
    target = _resolve_existing_entry_path(raw_path)
    if target == target.parent:
        # A filesystem root (/, or a drive root on Windows). Never deletable — a recursive
        # delete here would try to wipe everything the process can reach.
        raise FileBrowserError("invalid_path", "Refusing to delete a filesystem root", 400)
    target_stat = _stat_existing(target, follow_symlinks=False)

    def _delete() -> dict[str, Any]:
        try:
            if stat.S_ISDIR(target_stat.st_mode):
                if recursive:
                    shutil.rmtree(target)
                else:
                    target.rmdir()
            else:
                target.unlink()
            return {"ok": True}
        except OSError as exc:
            if stat.S_ISDIR(target_stat.st_mode) and not recursive and exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                raise ConflictError("not_empty", "Directory is not empty") from exc
            if isinstance(exc, FileNotFoundError):
                raise NotFoundError() from exc
            if isinstance(exc, PermissionError):
                raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
            raise FileBrowserError("fs_error", str(exc), 400) from exc

    return _run_mutation("delete", target, _delete, recursive=recursive)


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
