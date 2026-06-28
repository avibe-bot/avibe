from __future__ import annotations

import ctypes
import errno
import logging
import mimetypes
import os
import shutil
import stat
import sys
import tempfile
import threading
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
    return {
        "ok": True,
        "name": path.name,
        "ext": _extension(path),
        "kind": kind,
        "size": size,
        "mtime": _mtime_seconds(stat_result),
        "mime": mime,
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
