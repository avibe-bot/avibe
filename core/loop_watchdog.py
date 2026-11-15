"""Report controller-loop stalls with bounded, metadata-only loop attribution.

The watchdog asks the loop to answer every second. After STALL_SECONDS without
an answer it emits one structured warning per independent stall (not every
poll of a prolonged stall), then logs recovery. Only the loop thread's innermost
30 Python frames are inspected, and only fixed code-identity labels are emitted:
no source, locals, arguments, absolute paths or arbitrary names. Unknown code is
explicitly redacted. The JSON event is limited to 8 KiB. C calls themselves and
unlisted or dynamically generated Python code are deliberate blind spots.

GC-active at sampling and watchdog wake lateness are signals, not causality.
``lag_ms`` is observed at wake; ``sampled_at_unix`` is taken after bounded label
resolution, adjacent to the GC-state/frame snapshot, for offline correlation.
Wake lateness measures only the timed wait, before any reporting work; it cannot
distinguish OS scheduling, GIL contention or GC. Process-wide GC timing retains
the newest 64 collections longer than 200 ms, without heap census or GC policy
changes. Collection end times, not delayed log timestamps, support correlation.

For later, explicitly authorized offline observation, pass already-selected
application log lines to core.loop_watchdog_events.parse_watchdog_event. It
preserves safe structured attribution; old numeric-only parsers do not. No
capture, log discovery or automatic observation is enabled here.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from types import CodeType, FunctionType, ModuleType

from core.loop_watchdog_events import (
    EVENT_MARKER,
    MAX_EVENT_BYTES,
    MAX_STACK_FRAMES,
    REDACTED_LABEL,
    _CODE_TARGETS,
)

logger = logging.getLogger(__name__)

STALL_SECONDS = 2.0
POLL_SECONDS = 1.0
GC_SLOW_SECONDS = 0.2
_MAX_GC_REPORTS = 64


class EventLoopWatchdog:
    """A daemon thread reporting each independently detected loop stall once."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        stall_seconds: float = STALL_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop = loop
        self._stall_seconds = stall_seconds
        self._poll_seconds = poll_seconds
        self._monotonic = monotonic
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._answered_at = monotonic()
        self._asking = False
        self._loop_thread_id: int | None = None
        self._stall_reported = False
        # Index by id, NOT CodeType equality: replaced/dynamic code can compare
        # equal to static code. Retain code objects (never frames) to prevent id reuse.
        self._code_labels: dict[int, tuple[CodeType, tuple[str, str]]] = {}
        self._unresolved_targets = [(module, attribute) for module, attrs in _CODE_TARGETS for attribute in attrs]
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
        self._refresh_code_labels()
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
        # No stack work on the loop, just scalar heartbeat state.
        self._loop_thread_id = threading.get_ident()
        self._answered_at = self._monotonic()
        self._asking = False

    def _run(self) -> None:
        silent_since = self._answered_at
        while True:
            # Start the deadline AFTER the previous report, not at its wake.
            wake_due = self._monotonic() + self._poll_seconds
            if self._stopped.wait(self._poll_seconds):
                break
            now = self._monotonic()
            wake_lateness = max(0.0, now - wake_due)
            answered_at = self._answered_at
            recovered = self._stall_reported and answered_at > silent_since
            if recovered:
                self._stall_reported = False
            previous_silent_since, silent_since = silent_since, answered_at
            if not self._asking:
                self._asking = True
                try:
                    self._loop.call_soon_threadsafe(self._answer)
                except RuntimeError:
                    break  # the loop is closed; still drain queued GC reports below
            try:
                if not self._stall_reported and now - answered_at >= self._stall_seconds:
                    self._stall_reported = True
                    self._report_stall(now - answered_at, wake_lateness)
                if recovered:
                    logger.info("Event loop answered again after a %.1fs stall", answered_at - previous_silent_since)
                self._report_gc()
            except Exception:
                # Diagnostics must not end the watchdog or echo exception text,
                # which can contain private filenames or values.
                pass
        try:
            self._report_gc()
        except Exception:
            pass

    def _refresh_code_labels(self) -> None:
        unresolved = []
        for module, attribute in self._unresolved_targets:
            owner = sys.modules.get(module)
            for part in attribute.split("."):
                # No getattr, descriptors, imports, module scan or source I/O.
                if type(owner) is ModuleType:
                    owner = ModuleType.__getattribute__(owner, "__dict__").get(part)
                elif isinstance(owner, type):
                    owner = type.__dict__["__dict__"].__get__(owner).get(part)
                else:
                    owner = None
                    break
            if type(owner) in (staticmethod, classmethod):
                owner = owner.__func__
            if type(owner) is FunctionType:
                code = owner.__code__
                label = (module.replace(".", "/") + ".py", attribute)
                self._code_labels[id(code)] = (code, label)
            else:
                unresolved.append((module, attribute))
        self._unresolved_targets = unresolved

    def _loop_stack(self, loop_thread: int | None) -> tuple[list[dict], str, bool]:
        frame = all_frames = code = None
        try:
            if loop_thread is None:
                return [], "missing", False
            # CPython exposes only an all-thread snapshot. Drop every other
            # reference immediately; never enumerate threads or walk their stacks.
            all_frames = sys._current_frames()
            frame = all_frames.get(loop_thread)
            all_frames.clear()
            all_frames = None
            if frame is None:
                return [], "missing", False
            result = []
            for _ in range(MAX_STACK_FRAMES):
                code = frame.f_code
                known = self._code_labels.get(id(code))
                file, function = known[1] if known is not None and known[0] is code else REDACTED_LABEL
                result.append({"file": file, "function": function, "line": min(max(frame.f_lineno, 0), 2**31 - 1)})
                frame = frame.f_back
                if frame is None:
                    break
            return result, "ok", frame is not None
        except Exception:
            return [], "error", False
        finally:
            if all_frames is not None:
                all_frames.clear()
            frame = all_frames = code = None

    def _report_stall(self, lag: float, wake_lateness: float) -> None:
        try:
            self._refresh_code_labels()  # fixed unresolved entries only; no healthy-tick work
        except Exception:
            # Existing fixed labels remain safe; unresolved frames stay redacted.
            pass
        loop_thread = self._loop_thread_id or getattr(self._loop, "_thread_id", None)
        sampled_at_unix = time.time()
        gc_active = self._gc_started_at is not None
        frames, status, truncated = self._loop_stack(loop_thread)
        event = {
            "version": 1, "kind": "loop_stall", "loop_id": id(self._loop),
            "loop_thread_id": loop_thread, "watchdog_thread_id": threading.get_ident(),
            "lag_ms": round(lag * 1000, 3), "sampled_at_unix": sampled_at_unix, "gc_active": gc_active,
            "watchdog_wake_lateness_ms": round(wake_lateness * 1000, 3),
            "sample_status": status, "frames": frames, "truncated": truncated,
        }
        payload = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        while len(payload.encode("utf-8")) > MAX_EVENT_BYTES and frames:
            frames.pop()
            event["truncated"] = True
            payload = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        # Preserve the numeric-only warning prefix for existing offline parsers.
        logger.warning("Event loop has not answered for %.1fs%s%s", lag, EVENT_MARKER, payload)
