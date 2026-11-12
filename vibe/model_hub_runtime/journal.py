"""Strict persistence for the engine activation journal, NOT channel history.

A successful return is the durability boundary. A replace followed by a failed
flush is not success, even if a reader can already see the new bytes. Callers
must fence execution until recovery settles such an interrupted operation.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class JournalError(RuntimeError):
    """The engine owner cannot establish a durable recovery decision."""


def flush_file(path: Path) -> None:
    # Windows _commit/FlushFileBuffers requires a write-capable handle.
    with path.open("r+b" if os.name == "nt" else "rb") as handle:
        os.fsync(handle.fileno())
        if sys.platform == "darwin":
            import fcntl

            # No fallback to ordinary fsync: that would weaken the transaction.
            fcntl.fcntl(handle.fileno(), fcntl.F_FULLFSYNC)


def flush_directory(path: Path) -> None:
    if os.name == "nt":
        # Namespace publication on Windows uses MoveFileExW(WRITE_THROUGH).
        # Do not pretend Python's directory fsync is a Windows primitive.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_mkdir(path: Path) -> None:
    if path.is_dir():
        # Also settle an earlier mkdir whose parent flush failed.
        flush_directory(path)
        flush_directory(path.parent)
        return
    durable_mkdir(path.parent)
    if os.name == "nt":
        temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}-", dir=path.parent))
        try:
            _replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.rmdir()
    else:
        path.mkdir(exist_ok=True)
    flush_directory(path)
    flush_directory(path.parent)


def _replace(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return
    from ctypes import wintypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    # REPLACE_EXISTING | WRITE_THROUGH; a reader's sharing violation is bounded.
    deadline = time.monotonic() + 1.0
    while not move(str(source), str(target), 0x9):
        error = ctypes.get_last_error()
        if error not in {5, 32, 33} or time.monotonic() >= deadline:
            raise ctypes.WinError(error)
        time.sleep(0.02)


def publish_directory(source: Path, target: Path) -> None:
    """Same-volume namespace publication, with the platform completion primitive."""
    try:
        durable_mkdir(target.parent)
        _replace(source, target)
        flush_directory(target.parent)
    except (OSError, ValueError, AttributeError) as exc:
        raise JournalError(f"engine_journal_unavailable: {type(exc).__name__}") from exc


def write_durable(path: Path, data: bytes) -> None:
    """Publish one whole file or raise; never downgrade a flush failure."""
    temporary: Path | None = None
    try:
        durable_mkdir(path.parent)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if sys.platform == "darwin":
                import fcntl

                fcntl.fcntl(handle.fileno(), fcntl.F_FULLFSYNC)
        _replace(temporary, path)
        temporary = None
        flush_directory(path.parent)
        if sys.platform == "darwin":
            # The parent fsync orders the rename's metadata before the final
            # native device-cache flush, not only before a pre-rename flush.
            flush_file(path)
    except (OSError, ValueError, AttributeError) as exc:
        raise JournalError(f"engine_journal_unavailable: {type(exc).__name__}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_document(path: Path, value: dict[str, Any]) -> None:
    write_durable(path, json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def read_document(path: Path) -> dict[str, Any] | None:
    """Absent is distinct from damaged. Never silently initialize over damage."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if path.is_symlink():
            raise JournalError("engine_journal_unreadable") from None
        return None
    except OSError as exc:
        raise JournalError("engine_journal_unreadable") from exc
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise JournalError("engine_journal_unreadable") from exc
    if not isinstance(value, dict):
        raise JournalError("engine_journal_unreadable")
    return value
