"""Best-effort evidence for the service's otherwise silent shutdown tail.

The service still uses its existing five-second managed-stop budget. Four
seconds after the first shutdown signal, a daemon records one stack snapshot
if this process is still alive, including while Python joins executor workers
after main returns. It neither signals processes nor keeps this one alive.

One private file per shutdown lives beside the application logs. A dedicated
writer owns it, outside logging's handlers/locks and their shutdown lifecycle.
The signal handler and phase producers only enqueue small records: even a
blocked diagnostic destination must not delay service cleanup. Fast exits can
lose the last queued diagnostic records; these are observations, not leases or
completion acknowledgements. No source text, locals, arguments or environment
are included in a snapshot.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# One second before runtime.stop_service's unchanged five-second timeout.
STACK_DELAY_SECONDS = 4.0
_FRAMES_PER_THREAD = 30


class ShutdownDiagnostics:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir
        self._started_at: float | None = None
        self._records: queue.SimpleQueue[tuple[float, float, str]] = queue.SimpleQueue()
        self.path: Path | None = None

    def start(self) -> None:
        """Arm once, at signal receipt; failures must not prevent shutdown."""
        if self._started_at is not None:
            return
        self._started_at = time.monotonic()
        self.path = self._logs_dir / f"shutdown-{os.getpid()}-{time.time_ns()}.log"
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

    def _run(self) -> None:
        assert self.path is not None and self._started_at is not None
        deadline = self._started_at + STACK_DELAY_SECONDS
        dumped = False
        try:
            # Exclusive creation prevents following/reusing an existing path;
            # this descriptor is non-inheritable and never a logging handler.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
