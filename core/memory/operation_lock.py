"""Cross-process admission for destructive Memory operations."""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path

from config import paths


class MemoryOperationBusy(RuntimeError):
    """Another process or thread owns the Memory operation lease."""


_PROCESS_LEASES: set[Path] = set()
_PROCESS_LEASES_LOCK = threading.Lock()
_PROCESS_LEASES_PID = os.getpid()


def _refresh_process_registry() -> None:
    """Discard process-local reservations inherited across ``fork()``."""

    global _PROCESS_LEASES_PID

    pid = os.getpid()
    if _PROCESS_LEASES_PID != pid:
        _PROCESS_LEASES.clear()
        _PROCESS_LEASES_PID = pid


def memory_operation_lock_path(effective_home: Path | None = None) -> Path:
    """Return the stable lock path for one Avibe home."""

    home = effective_home or paths.get_vibe_remote_dir()
    return home / "runtime" / "memory-operation.lock"


class MemoryOperationLease:
    """A non-blocking, process-wide and cross-process operation lease."""

    def __init__(self, effective_home: Path | None = None) -> None:
        self.path = memory_operation_lock_path(effective_home)
        self._key = Path(os.path.abspath(self.path))
        self._descriptor: int | None = None

    def acquire(self) -> None:
        """Acquire the lease or raise ``MemoryOperationBusy`` immediately."""

        with _PROCESS_LEASES_LOCK:
            _refresh_process_registry()
            if self._key in _PROCESS_LEASES:
                raise MemoryOperationBusy("memory operation already in progress")
            _PROCESS_LEASES.add(self._key)

        descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            os.set_inheritable(descriptor, False)
            _try_lock(descriptor)
            self._descriptor = descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            with _PROCESS_LEASES_LOCK:
                _refresh_process_registry()
                _PROCESS_LEASES.discard(self._key)
            raise

    def release(self) -> None:
        """Release the lease. Repeated calls are harmless."""

        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)
            with _PROCESS_LEASES_LOCK:
                _refresh_process_registry()
                _PROCESS_LEASES.discard(self._key)

    def __enter__(self) -> MemoryOperationLease:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _try_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise MemoryOperationBusy(
                    "memory operation already in progress"
                ) from error
            raise
        return

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise MemoryOperationBusy("memory operation already in progress") from error
        raise


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
