from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    expanded = Path(os.path.expanduser(raw.strip()))
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
    resolve_safe_path(raw)
    expanded = _expanded_absolute_path(raw)
    try:
        parent = expanded.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError("Parent directory not found") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise FileBrowserError("invalid_path", str(exc), 400) from exc
    return parent / expanded.name


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


def _is_dir_no_follow(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


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
    truncated = False
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                if not show_hidden and entry.name.startswith("."):
                    continue
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                payload = _entry_payload(entry)
                if payload is not None:
                    entries.append(payload)
    except PermissionError as exc:
        raise FileBrowserError("permission_denied", "Permission denied", 403) from exc
    except OSError as exc:
        raise FileBrowserError("fs_error", str(exc), 400) from exc

    entries.sort(key=lambda item: (0 if item["kind"] == "dir" else 1, str(item["name"]).lower(), str(item["name"])))
    parent = None if target.parent == target else str(target.parent)
    return {"ok": True, "path": str(target), "parent": parent, "entries": entries, "truncated": truncated}


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
    # name survives a crash, not just the file's contents.
    try:
        dir_fd = os.open(path, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def write_file(raw_path: str, content: str, *, expected_mtime: float | None = None) -> dict[str, Any]:
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
        # Re-check at write time to defeat a file→symlink swap between the
        # checks above and the replace below.
        if target.is_symlink():
            raise FileBrowserError("is_symlink", "Refusing to write through a symlink", 400)
        if target.exists() and not target.is_file():
            raise FileBrowserError("not_file", "Path is not a regular file", 400)
        fd = -1
        temp_name = ""
        try:
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            temp_name = ""
            _fsync_dir(parent)
            stat_result = target.stat()
            return {"ok": True, "mtime": _mtime_seconds(stat_result)}
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
    target = resolve_safe_path(raw_path)
    try:
        parent = target.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError("Parent directory not found") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise FileBrowserError("invalid_path", str(exc), 400) from exc
    if not parent.is_dir():
        raise FileBrowserError("not_dir", "Parent is not a directory", 400)
    if target.exists():
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
    if _exists_no_follow(target):
        raise ConflictError("exists", "Destination already exists")

    def _rename() -> dict[str, Any]:
        try:
            source.rename(target)
            return {"ok": True, "path": str(target)}
        except FileExistsError as exc:
            raise ConflictError("exists", "Destination already exists") from exc
        except OSError as exc:
            raise FileBrowserError("fs_error", str(exc), 400) from exc

    return _run_mutation("rename", source, _rename)


def move_path(raw_src: str, raw_dst: str, *, overwrite: bool = False) -> dict[str, Any]:
    source = _resolve_existing_entry_path(raw_src)
    target = _resolve_entry_path(raw_dst)
    parent = target.parent
    if not parent.is_dir():
        raise FileBrowserError("not_dir", "Destination parent is not a directory", 400)
    if _exists_no_follow(target) and not overwrite:
        raise ConflictError("exists", "Destination already exists")
    if _is_dir_no_follow(target) and source.is_file():
        raise ConflictError("exists", "Cannot overwrite a directory with a file")

    def _move() -> dict[str, Any]:
        try:
            if _exists_no_follow(target) and overwrite:
                if _is_dir_no_follow(target):
                    target.rmdir()
                else:
                    target.unlink()
            shutil.move(str(source), str(target))
            return {"ok": True}
        except OSError as exc:
            raise FileBrowserError("fs_error", str(exc), 400) from exc

    return _run_mutation("move", source, _move, dst=str(target), overwrite=overwrite)


def delete_path(raw_path: str, *, recursive: bool = False) -> dict[str, Any]:
    target = _resolve_existing_entry_path(raw_path)
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
            if stat.S_ISDIR(target_stat.st_mode) and not recursive:
                raise ConflictError("not_empty", "Directory is not empty") from exc
            raise FileBrowserError("fs_error", str(exc), 400) from exc

    return _run_mutation("delete", target, _delete, recursive=recursive)
