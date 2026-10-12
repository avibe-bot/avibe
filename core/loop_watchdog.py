"""Report an event loop that stops turning, with the stack that stopped it.

A synchronous call on the service's loop -- a lock wait, a subprocess, a large
file rewrite -- stops every coroutine with it, and nothing on that loop can say
so while it happens: its own timers fire late, afterwards, with no trace of what
held them. A thread outside the loop can. It asks the loop to answer once a
second and, when the loop has not answered for ``STALL_SECONDS``, logs one
WARNING with the stack of every thread, so the blocking call is named in the
log while it is still blocking. When the loop answers again it logs how long
the stall lasted.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable

logger = logging.getLogger(__name__)

STALL_SECONDS = 5.0
POLL_SECONDS = 1.0
# At most one stack report per this long; a loop that keeps stalling still gets
# its recovery lines, which carry each stall's length.
REPORT_INTERVAL_SECONDS = 300.0
# Innermost frames kept per thread, and the report's overall size.
_FRAMES_PER_THREAD = 30
_MAX_REPORT_CHARS = 64 * 1024


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

    def start(self) -> None:
        if self._thread is not None:
            return
        self._answered_at = self._monotonic()
        self._thread = threading.Thread(target=self._run, name="event-loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
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
