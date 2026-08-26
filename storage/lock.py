from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Callable, IO, Optional, Type


logger = logging.getLogger(__name__)

#: How long a blocked acquire waits before it starts saying so. A migration that
#: takes minutes is legitimate, and the process waiting behind it looks hung from
#: the outside; naming the holder is what turns that into a diagnosable wait.
_WAIT_LOG_INTERVAL_SECONDS = 15.0


class MigrationLockTimeout(TimeoutError):
    pass


def migration_lock_path_for(db_path: Path) -> Path:
    """The lock guarding migrations of one SQLite state database.

    Derived from the database rather than from the state directory, and stated
    once here, because the thing being protected is that file: two callers that
    disagree about which directory is "the" state directory would otherwise
    take two different locks over one database and both proceed.
    """

    return db_path.expanduser().resolve().with_name("migration.lock")


class _PathLockState:
    """The one in-process owner of a lock path.

    The OS lock alone cannot express re-entrance. ``fcntl.flock`` attaches to an
    open file description, so a second ``open`` of the same path -- which is what
    a nested acquire does -- is a *different* description and blocks against the
    first, in the very thread that holds it. That is a self-deadlock, and it is
    reachable from ordinary nesting: ``ensure_sqlite_state`` holds the migration
    lock across the call to ``run_migrations``, which takes it too.

    Holding the depth here rather than on the lock object is what makes
    re-entrance a property of the path instead of one caller's instance. Nested
    callers do not share objects; they only share the file they are protecting.
    """

    def __init__(self) -> None:
        self.gate = threading.RLock()
        self.depth = 0
        self.handle = None
        self.users = 0


_registry_lock = threading.Lock()
_states: dict[str, _PathLockState] = {}


def _state_key(lock_path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(lock_path)))


def _checkout_state(key: str) -> _PathLockState:
    with _registry_lock:
        state = _states.get(key)
        if state is None:
            state = _PathLockState()
            _states[key] = state
        state.users += 1
        return state


def _return_state(key: str) -> None:
    with _registry_lock:
        state = _states.get(key)
        if state is None:
            return
        state.users -= 1
        if state.users <= 0:
            _states.pop(key, None)


class MigrationFileLock:
    """A cross-process lock for one path, re-entrant within a thread.

    Two guarantees, needed together by the same callers: at most one process on
    the machine holds the path, and at most one thread inside each process holds
    it -- while a thread that already holds it may take it again.

    The second guarantee makes this thread-owned, so the thread that acquires is
    the thread that releases. Re-entrance is what forces that: a lock that any
    thread may hand to any other has no holder to compare a nested acquire
    against. A caller that waits on one thread and works on another must keep
    the whole acquire-release pair on the waiting thread.

    ``timeout_seconds`` bounds the whole acquire, gate and file lock together.
    ``0`` makes it a try-lock that never blocks. ``None`` waits indefinitely,
    which is the right answer whenever the work behind the lock has no upper
    bound: the OS drops a file lock when its holder dies, so the only way to
    wait forever is for a live process to still be working.

    Security-sensitive callers may supply the private opener and validator so
    the same no-follow descriptor is checked before and after acquisition.
    Default callers retain the ordinary append-open behavior.
    """

    def __init__(
        self,
        lock_path: Path,
        *,
        timeout_seconds: float | None = 30.0,
        _handle_opener: Callable[[Path], IO[str]] | None = None,
        _handle_validator: Callable[[IO[str]], bool] | None = None,
    ):
        self.lock_path = lock_path
        self.timeout_seconds = timeout_seconds
        self._handle_opener = _handle_opener
        self._handle_validator = _handle_validator
        self._key = _state_key(self.lock_path)
        self._state: _PathLockState | None = None
        self._entries = 0

    def acquire(self) -> None:
        deadline = None if self.timeout_seconds is None else time.monotonic() + self.timeout_seconds
        state = _checkout_state(self._key)
        if not _acquire_gate(state.gate, deadline):
            _return_state(self._key)
            raise MigrationLockTimeout(f"Timed out waiting for migration lock: {self.lock_path}")
        try:
            if state.depth == 0:
                state.handle = _acquire_file_lock(
                    self.lock_path,
                    deadline,
                    handle_opener=self._handle_opener,
                    handle_validator=self._handle_validator,
                )
            elif self._handle_validator is not None and (
                state.handle is None or not self._handle_validator(state.handle)
            ):
                raise OSError(f"Lock path failed identity validation: {self.lock_path}")
        except BaseException:
            state.gate.release()
            _return_state(self._key)
            raise
        state.depth += 1
        self._state = state
        self._entries += 1

    def release(self) -> None:
        state = self._state
        if state is None or self._entries == 0:
            return
        self._entries -= 1
        if self._entries == 0:
            self._state = None
        state.depth -= 1
        try:
            if state.depth == 0 and state.handle is not None:
                handle, state.handle = state.handle, None
                _release_file_lock(handle)
        finally:
            state.gate.release()
            _return_state(self._key)

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


def _acquire_gate(gate, deadline: float | None) -> bool:
    if deadline is None:
        gate.acquire()
        return True
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # Still one real attempt: a zero timeout is a try-lock, not a refusal,
        # and a thread already holding the gate re-enters it here.
        return gate.acquire(blocking=False)
    return gate.acquire(timeout=remaining)


def _acquire_file_lock(
    lock_path: Path,
    deadline: float | None,
    *,
    handle_opener: Callable[[Path], IO[str]] | None = None,
    handle_validator: Callable[[IO[str]], bool] | None = None,
):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = handle_opener(lock_path) if handle_opener is not None else open(lock_path, "a+", encoding="utf-8")
    next_log = time.monotonic() + _WAIT_LOG_INTERVAL_SECONDS
    locked = False
    try:
        if handle_validator is not None and not handle_validator(handle):
            raise OSError(f"Lock path failed identity validation: {lock_path}")
        while True:
            # Every attempt locks the same byte. Windows locks a range starting at
            # the current position, and both the append-mode open and the read below
            # leave it at the end of the file, so seeking is what keeps two waiters
            # contending for one byte instead of two disjoint ones.
            handle.seek(0)
            if _try_lock(handle):
                locked = True
                if handle_validator is not None and not handle_validator(handle):
                    raise OSError(f"Lock path failed identity validation after acquisition: {lock_path}")
                handle.seek(0)
                handle.truncate()
                handle.write(str(os.getpid()))
                handle.flush()
                return handle
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise MigrationLockTimeout(f"Timed out waiting for migration lock: {lock_path}")
            if now >= next_log:
                next_log = now + _WAIT_LOG_INTERVAL_SECONDS
                logger.info(
                    "Waiting for the migration lock at %s, held by pid %s",
                    lock_path,
                    _recorded_holder(handle),
                )
            time.sleep(0.1)
    except BaseException:
        if locked:
            _release_file_lock(handle)
        else:
            handle.close()
        raise


def _release_file_lock(handle) -> None:
    try:
        _unlock(handle)
    finally:
        handle.close()


def _recorded_holder(handle) -> str:
    """Whoever last wrote the lock file, for the waiting message only.

    Advisory: a holder writes its pid just after taking the lock, so a reader
    can catch the previous holder's pid for an instant. Nothing decides anything
    on this value.
    """

    try:
        handle.seek(0)
        return handle.read().strip() or "unknown"
    except OSError:
        return "unknown"


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
