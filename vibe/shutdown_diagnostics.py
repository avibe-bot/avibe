"""Best-effort evidence for the service's otherwise silent shutdown tail.

The service still uses its existing five-second managed-stop budget. Four
seconds after the first shutdown signal, a daemon records one stack snapshot
if this process is still alive, including while Python joins executor workers
after main returns. It neither signals processes nor keeps this one alive.

One private file per shutdown lives beside the application logs. The normal
phase writer owns it, outside logging's handlers/locks and their shutdown
lifecycle. The signal handler and ordinary phase producers only enqueue small
records: even a blocked diagnostic destination must not delay service cleanup.
Exit-boundary markers synchronously attempt a direct append-and-flush through a
short-lived daemon I/O helper with a bounded wait, so they remain visible after
the queue writer or logging listener has stopped without making a blocked
filesystem an owner of process lifetime. Fast exits can lose the last queued
records; these are observations, not leases or completion acknowledgements. No source text, locals, arguments or environment
are included in a snapshot.
"""
from __future__ import annotations

import atexit
import functools
import importlib
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# One second before runtime.stop_service's unchanged five-second timeout.
STACK_DELAY_SECONDS = 4.0
_PRIVATE_OPEN = os.open
_SYNC_PHASE_WRITE_TIMEOUT_SECONDS = 0.1
_FRAMES_PER_THREAD = 30


_ACTIVE_DIAGNOSTICS: "ShutdownDiagnostics | None" = None
_EXECUTOR_EXIT_HOOKS_INSTALLED = False


def _record_active_phase(name: str) -> None:
    diagnostics = _ACTIVE_DIAGNOSTICS
    if diagnostics is not None:
        diagnostics.phase_sync(name)


def register_shutdown_hook(name: str, func: Callable[..., object], *args: object, **kwargs: object) -> object:
    """Register an Avibe-owned exit hook with private synchronous diagnostics."""

    @functools.wraps(func)
    def wrapped() -> object:
        _record_active_phase(f"atexit_{name}_start")
        try:
            return func(*args, **kwargs)
        finally:
            _record_active_phase(f"atexit_{name}_return")

    return atexit.register(wrapped)


def _wrap_threading_exit_hook(callback: Callable[[], object], name: str) -> Callable[[], object]:
    @functools.wraps(callback)
    def wrapped() -> object:
        _record_active_phase(f"{name}_start")
        try:
            return callback()
        finally:
            _record_active_phase(f"{name}_return")

    return wrapped


def _callback_targets(callback: object, target: object) -> bool:
    if getattr(callback, "func", None) is target:
        return True
    closure = getattr(callback, "__closure__", None) or ()
    for cell in closure:
        try:
            if cell.cell_contents is target:
                return True
        except ValueError:
            continue
    return False


def install_executor_exit_diagnostics() -> None:
    """Wrap CPython's pre-thread-shutdown executor joins, when available."""

    global _EXECUTOR_EXIT_HOOKS_INSTALLED
    if _EXECUTOR_EXIT_HOOKS_INSTALLED:
        return
    callbacks = getattr(threading, "_threading_atexits", None)
    if not isinstance(callbacks, list):
        return
    wrapped_any = False
    for module_name, label in (
        ("concurrent.futures.thread", "thread_executor_join"),
        ("concurrent.futures.process", "process_executor_join"),
    ):
        try:
            module = importlib.import_module(module_name)
            target = getattr(module, "_python_exit", None)
        except Exception:
            continue
        if not callable(target):
            continue
        for index, callback in enumerate(callbacks):
            if getattr(callback, "_avibe_shutdown_diagnostic", False):
                continue
            if not _callback_targets(callback, target):
                continue
            wrapped = _wrap_threading_exit_hook(callback, label)
            wrapped._avibe_shutdown_diagnostic = True  # type: ignore[attr-defined]
            callbacks[index] = wrapped
            wrapped_any = True
    _EXECUTOR_EXIT_HOOKS_INSTALLED = wrapped_any


class ShutdownDiagnostics:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir
        self._started_at: float | None = None
        self._records: queue.SimpleQueue[tuple[float, float, str]] = queue.SimpleQueue()
        self.path: Path | None = None
        install_executor_exit_diagnostics()

    def start(self) -> None:
        """Arm once, at signal receipt; failures must not prevent shutdown."""
        if self._started_at is not None:
            return
        self._started_at = time.monotonic()
        self.path = self._logs_dir / f"shutdown-{os.getpid()}-{time.time_ns()}.log"
        global _ACTIVE_DIAGNOSTICS
        _ACTIVE_DIAGNOSTICS = self
        self.phase("shutdown_requested")
        try:
            threading.Thread(target=self._run, name="shutdown-diagnostics", daemon=True).start()
        except Exception:
            # Diagnostics are never an owner of service lifetime.
            pass

    def phase(self, name: str) -> None:
        """Record a fixed phase name without doing I/O on the cleanup thread."""
        if self._started_at is not None:
            self._records.put((time.time(), time.monotonic(), name))

    def phase_sync(self, name: str) -> None:
        """Synchronously attempt one bounded append-and-flush without owning exit."""
        if self._started_at is None or self.path is None:
            return
        line = self._header(time.time(), time.monotonic(), name)

        def write() -> None:
            try:
                fd = _PRIVATE_OPEN(self.path, os.O_WRONLY | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace", buffering=1) as sink:
                    sink.write(line)
                    sink.flush()
            except Exception:
                # Exit diagnostics must never own process lifetime.
                return

        try:
            writer = threading.Thread(target=write, name="shutdown-diagnostics-phase", daemon=True)
            writer.start()
            writer.join(_SYNC_PHASE_WRITE_TIMEOUT_SECONDS)
        except Exception:
            # Thread creation/finalization can be unavailable during interpreter exit.
            return

    def _run(self) -> None:
        assert self.path is not None and self._started_at is not None
        deadline = self._started_at + STACK_DELAY_SECONDS
        dumped = False
        try:
            # Exclusive creation prevents following/reusing an existing path;
            # this descriptor is non-inheritable and never a logging handler.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace", buffering=1) as sink:
                while True:
                    if not dumped and time.monotonic() >= deadline:
                        sink.write(self._header(time.time(), time.monotonic(), "thread_snapshot"))
                        sink.write(_thread_stacks())
                        sink.flush()
                        dumped = True
                    try:
                        record = self._records.get(
                            timeout=None if dumped else max(0.0, deadline - time.monotonic())
                        )
                    except queue.Empty:
                        continue
                    sink.write(self._header(*record))
        except Exception:
            # Do not use logging here: its drain may be the very waiter we
            # need to diagnose, or its handlers may already have been closed.
            return

    def _header(self, wall_time: float, observed_at: float, phase: str) -> str:
        stamp = datetime.fromtimestamp(wall_time, timezone.utc).isoformat(timespec="milliseconds")
        elapsed_ms = round((observed_at - self._started_at) * 1000)
        return f"{stamp} pid={os.getpid()} elapsed_ms={elapsed_ms} Shutdown phase={phase}\n"


def _thread_stacks() -> str:
    """Names and frame coordinates only, with no linecache/source lookup."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    frames = sys._current_frames()
    lines: list[str] = []
    try:
        for ident, frame in sorted(frames.items()):
            lines.append(f"--- thread {ident} name={names.get(ident, 'unknown')!r} ---\n")
            depth = 0
            while frame is not None and depth < _FRAMES_PER_THREAD:
                code = frame.f_code
                lines.append(f"  File {code.co_filename!r}, line {frame.f_lineno}, in {code.co_name}\n")
                frame = frame.f_back
                depth += 1
            if frame is not None:
                lines.append("  ... older frames omitted\n")
        return "".join(lines)
    finally:
        frames.clear()
