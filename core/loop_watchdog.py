"""Report an event loop that stops turning, with the stack that stopped it.

A synchronous call on the service's loop -- a lock wait, a subprocess, a large
file rewrite -- stops every coroutine with it, and nothing on that loop can say
so while it happens: its own timers fire late, afterwards, with no trace of what
held them. A thread outside the loop can. It asks the loop to answer once a
second and, when the loop has not answered for ``STALL_SECONDS``, logs one
WARNING with the stack of every thread, so the blocking call is named in the
log while it is still blocking. When the loop answers again it logs how long
the stall lasted.

While running, it also times process-wide garbage collections. Collections
longer than 200 ms are queued for this thread to report, with their end time,
generation, collected/uncollectable counts (not a heap census), and collector
thread id. The callback never walks the heap, logs, or changes GC policy. The
queue retains the newest 64 slow collections if reporting falls behind; this
is a diagnostic, not a lossless GC trace. End times, rather than the delayed
log timestamps, can be correlated with loop stalls; overlap is not causality.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)

STALL_SECONDS = 2.0
POLL_SECONDS = 1.0
# At most one stack report per this long; a loop that keeps stalling still gets
# its recovery lines, which carry each stall's length.
REPORT_INTERVAL_SECONDS = 300.0
# Innermost frames kept per thread, and the report's overall size.
_FRAMES_PER_THREAD = 30
_MAX_REPORT_CHARS = 64 * 1024
# Observe collections without walking the heap or changing GC policy. Reports
# are emitted by the watchdog thread, never by the thread doing the collection.
GC_SLOW_SECONDS = 0.2
_MAX_GC_REPORTS = 64


class EventLoopWatchdog:
    """A daemon thread that logs the stacks of a loop that has not answered for ``stall_seconds``."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        stall_seconds: float = STALL_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        report_interval_seconds: float = REPORT_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop = loop
        self._stall_seconds = stall_seconds
        self._poll_seconds = poll_seconds
        self._report_interval_seconds = report_interval_seconds
        self._monotonic = monotonic
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._answered_at = monotonic()
        self._asking = False
        self._loop_thread_id: int | None = None
        self._last_report_at: float | None = None
        self._stall_reported = False
        self._gc_started_at: float | None = None
        self._gc_reports: deque[tuple[float, float, int, int, int, int]] = deque(maxlen=_MAX_GC_REPORTS)
        self._gc_callback = self._observe_gc

    def _observe_gc(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._gc_started_at = self._monotonic()
        elif phase == "stop":
            started_at, self._gc_started_at = self._gc_started_at, None
            if started_at is None:
                return
            duration = self._monotonic() - started_at
            if duration > GC_SLOW_SECONDS:
                # Bounded even if the reporter cannot run. Under severe pressure
                # the oldest pending reports are discarded, not heap-retained.
                self._gc_reports.append((
                    time.time(), duration, info["generation"], info["collected"],
                    info["uncollectable"], threading.get_ident(),
                ))

    def _report_gc(self) -> None:
        while self._gc_reports:
            ended_at, duration, generation, collected, uncollectable, thread_id = self._gc_reports.popleft()
            logger.warning(
                "Slow garbage collection: duration_ms=%.1f generation=%d collected=%d "
                "uncollectable=%d thread_id=%d ended_at_unix=%.6f",
                duration * 1000, generation, collected, uncollectable, thread_id, ended_at,
            )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopped.clear()
        self._answered_at = self._monotonic()
        gc.callbacks.append(self._gc_callback)
        self._thread = threading.Thread(target=self._run, name="event-loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._gc_callback in gc.callbacks:
            gc.callbacks.remove(self._gc_callback)
        self._gc_started_at = None
        self._stopped.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._poll_seconds * 2)

    def _answer(self) -> None:
        # Runs on the loop: the only work it adds there is two assignments.
        self._loop_thread_id = threading.get_ident()
        self._answered_at = self._monotonic()
        self._asking = False

    def _run(self) -> None:
        silent_since = self._answered_at
        while not self._stopped.wait(self._poll_seconds):
            self._report_gc()
            answered_at = self._answered_at
            if self._stall_reported and answered_at > silent_since:
                logger.info("Event loop answered again after a %.1fs stall", answered_at - silent_since)
                self._stall_reported = False
            silent_since = answered_at
            if not self._asking:
                self._asking = True
                try:
                    self._loop.call_soon_threadsafe(self._answer)
                except RuntimeError:
                    return  # the loop is closed
            now = self._monotonic()
            if self._stall_reported or now - answered_at < self._stall_seconds:
                continue
            self._stall_reported = True
            if self._last_report_at is not None and now - self._last_report_at < self._report_interval_seconds:
                continue
            self._last_report_at = now
            logger.warning(
                "Event loop has not answered for %.1fs; thread stacks follow.\n%s",
                now - answered_at,
                self._thread_stacks(),
            )
        self._report_gc()

    def _thread_stacks(self) -> str:
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        frames = sys._current_frames()
        # A loop that blocks before its first answer is still named: asyncio records its running thread.
        loop_thread = self._loop_thread_id or getattr(self._loop, "_thread_id", None)
        order = sorted(frames, key=lambda ident: (ident != loop_thread, names.get(ident, "")))
        sections = []
        for ident in order:
            if ident == threading.get_ident():
                continue
            label = "event loop" if ident == loop_thread else names.get(ident, "unnamed")
            stack = "".join(traceback.format_stack(frames[ident])[-_FRAMES_PER_THREAD:])
            sections.append(f"--- thread {ident} ({label}) ---\n{stack}")
        report = "".join(sections)
        if len(report) > _MAX_REPORT_CHARS:
            report = report[:_MAX_REPORT_CHARS] + "\n... (truncated)"
        return report
