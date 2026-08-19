from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from config import paths


class MigrationLockTimeout(TimeoutError):
    pass


class MigrationFileLock:
    """Small cross-process lock for startup migrations."""

    def __init__(self, lock_path: Path | None = None, *, timeout_seconds: float = 30.0):
        self.lock_path = lock_path or paths.get_sqlite_migration_lock_path()
        self.timeout_seconds = timeout_seconds
        self._handle = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            handle.seek(0)
            if _try_lock(handle):
                handle.seek(0)
                handle.truncate()
                handle.write(str(os.getpid()))
                handle.flush()
                self._handle = handle
                return
            if time.monotonic() >= deadline:
                handle.close()
                raise MigrationLockTimeout(f"Timed out waiting for migration lock: {self.lock_path}")
            time.sleep(0.1)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "MigrationFileLock":
        self.acquire()
        return self

    def __exit__(
        self,
        _exc_type: Optional[Type[BaseException]],
        _exc: Optional[BaseException],
        _tb: Optional[TracebackType],
    ) -> None:
        self.release()


def _try_lock(handle) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def fcntl_available() -> bool:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return False
    return True


def try_windows_exclusive_lock(fd: int) -> bool:
    """Non-blocking exclusive probe on an already-open Windows lock fd."""
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def unlock_windows_exclusive_lock(fd: int) -> None:
    try:
        import msvcrt
    except ImportError:
        return
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
