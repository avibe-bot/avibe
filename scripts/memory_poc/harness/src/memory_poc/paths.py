from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from .errors import HarnessError


def workspace_root() -> Path:
    """Locate the checkout from the installed editable harness source."""
    current = Path(__file__).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "scripts" / "memory_poc" / "CONTRACT.md").is_file():
            return candidate
    raise HarnessError("workspace_root_not_found")


def harness_root() -> Path:
    return workspace_root() / "scripts" / "memory_poc" / "harness"


def runtime_root(root: Path | None = None) -> Path:
    return (root or workspace_root()) / ".runtime" / "memory-poc"


def _assert_owner(info: os.stat_result, *, directory: bool) -> None:
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if stat.S_ISLNK(info.st_mode) or not expected_kind(info.st_mode):
        raise HarnessError("unsafe_runtime_directory" if directory else "unsafe_runtime_file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise HarnessError("runtime_directory_owner_mismatch" if directory else "runtime_file_owner_mismatch")


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_owner_directory(path: Path) -> int:
    try:
        descriptor = os.open(path, _directory_flags())
    except OSError as exc:
        raise HarnessError("unsafe_runtime_directory") from exc
    try:
        _assert_owner(os.fstat(descriptor), directory=True)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _directory_anchor(path: Path, anchor: Path | None) -> tuple[Path, tuple[str, ...], Path]:
    target = Path(os.path.abspath(path))
    if anchor is not None:
        base = Path(os.path.abspath(anchor))
        try:
            relative = target.relative_to(base)
        except ValueError as exc:
            raise HarnessError("runtime_path_outside_anchor") from exc
        _assert_owner(base.lstat(), directory=True)
        return base, relative.parts, target

    cursor = target
    missing: list[str] = []
    while True:
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor.name)
            if cursor.parent == cursor:
                raise HarnessError("runtime_directory_anchor_missing")
            cursor = cursor.parent
            continue
        _assert_owner(info, directory=True)
        return cursor, tuple(reversed(missing)), target


def ensure_owner_directory(path: Path, *, anchor: Path | None = None) -> Path:
    """Create an owner-only directory without traversing POC-owned symlinks."""
    base, components, target = _directory_anchor(path, anchor)
    descriptor = _open_owner_directory(base)
    current = base
    try:
        if not components and anchor is None:
            os.fchmod(descriptor, 0o700)
        for component in components:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise HarnessError("runtime_directory_create_failed") from exc
            try:
                next_descriptor = os.open(component, _directory_flags(), dir_fd=descriptor)
            except OSError as exc:
                raise HarnessError("unsafe_runtime_directory") from exc
            try:
                _assert_owner(os.fstat(next_descriptor), directory=True)
                current = current / component
                path_info = current.lstat()
                _assert_owner(path_info, directory=True)
                os.fchmod(next_descriptor, 0o700)
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return target
    finally:
        os.close(descriptor)


def ensure_regular_file_mode(path: Path, mode: int = 0o600) -> None:
    info = path.lstat()
    _assert_owner(info, directory=False)
    os.chmod(path, mode)


def write_private_text(path: Path, text: str, *, anchor: Path | None = None) -> None:
    """Atomically replace a POC-owned text file through an opened safe directory."""
    parent = ensure_owner_directory(path.parent, anchor=anchor)
    descriptor = _open_owner_directory(parent)
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(temporary_name, flags, 0o600, dir_fd=descriptor)
        except OSError as exc:
            raise HarnessError("runtime_file_create_failed") from exc
        temporary_created = True
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        temporary_created = False
        verified_descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
        try:
            _assert_owner(os.fstat(verified_descriptor), directory=False)
            os.fchmod(verified_descriptor, 0o600)
        finally:
            os.close(verified_descriptor)
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        os.close(descriptor)


def read_private_text(path: Path) -> str:
    """Read a regular POC-owned text file without following a final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise HarnessError("unsafe_runtime_file") from exc
    try:
        _assert_owner(os.fstat(descriptor), directory=False)
        handle = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = -1
        with handle:
            return handle.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)
