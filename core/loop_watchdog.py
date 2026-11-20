"""Report controller-loop stalls with bounded, metadata-only loop attribution.

The watchdog asks the loop to answer every second. After STALL_SECONDS without
an answer it emits one structured warning per independent stall (not every
poll of a prolonged stall), then logs recovery. Only the loop thread's innermost
30 Python frames are inspected, and only code-identity labels are emitted:
no source, locals, arguments, absolute paths or arbitrary names. Labels come from
a fixed vocabulary, or, for Avibe's own packages, from code reached by identity
through the defining module's namespace at a stall (never from a frame's own
filename or name). Unknown code is explicitly redacted. The JSON event is limited to 8 KiB. C calls themselves and
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
    OWN_PACKAGES,
    REDACTED_LABEL,
    _CODE_TARGETS,
    own_code_label,
)

logger = logging.getLogger(__name__)

STALL_SECONDS = 2.0
POLL_SECONDS = 1.0
GC_SLOW_SECONDS = 0.2
_MAX_GC_REPORTS = 64
_MAX_OWN_CODES = 200_000
# Written only at Vibey stream boundaries on the controller loop; sampled without a lock by GC.
_active_requests = 0
# Only protects in-memory publication and the final freeze instruction; never DB or IPC.
transient_admission_lock = threading.Lock()


def model_stream_opened() -> None:
    global _active_requests
    with transient_admission_lock:
        _active_requests += 1


def model_stream_closed() -> None:
    global _active_requests
    _active_requests -= 1


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
        self._indexed_modules: set[str] = set()
        self._gc_started_at: float | None = None
        self._gc_reports: deque[tuple[float, float, int, int, int, int, int, int, tuple[int, ...]]] = deque(maxlen=_MAX_GC_REPORTS)
        self._gc_callback = self._observe_gc
        self._census: Callable[[], None] | None = None
        self._census_due = False
        self._census_claimed = False
        self._watchdog_birth = self._answered_at
        self._warm_since = None
        self._trigger_candidates = -1
        self._freeze_state = "off"
        self._freeze_lock = threading.Lock()
        self._freeze_manager = None
        self._freeze_due = False
        self._freeze_task = None
        self._freeze_since = float("inf")
        self._trigger_uptime = 0.0

    def arm_warm_freeze(self, manager) -> str:
        with self._freeze_lock:
            if self._freeze_state == "off" and not gc.get_freeze_count():
                self._freeze_manager = manager
                self._freeze_since = max(self._watchdog_birth + 7200, self._monotonic())
                self._freeze_state = "armed"
            return self._freeze_state

    def rollback_warm_freeze(self) -> str:
        with self._freeze_lock:
            if self._freeze_state == "frozen":
                gc.unfreeze()  # restore normal collection; do not force a full sweep
            if self._freeze_state in ("armed", "frozen"):
                self._freeze_state = "retired"
                self._freeze_manager = None
            return self._freeze_state

    def _try_warm_freeze(self, manager) -> asyncio.Future:
        # The observer can cancel its receipt; only the executor completion
        # releases the attempt slot. No cancelled Task owns a live DB worker.
        receipt = self._loop.create_future()

        def apply():
            with self._freeze_lock, transient_admission_lock:
                if (self._freeze_state == "armed" and not self._stopped.is_set()
                        and not _active_requests and not manager.in_flight and not gc.get_freeze_count()
                        and not receipt.cancelled()):
                    gc.freeze()
                    self._freeze_state = "frozen"
                    self._freeze_manager = None

        def finished(worker):
            try:
                worker.result()
                if self._freeze_state == "frozen":
                    logger.warning("Warm GC trial frozen: count=%d", gc.get_freeze_count())
            except Exception:
                logger.warning("Warm GC trial: quiescence unavailable; freeze skipped")
            finally:
                self._freeze_task = None
                if not receipt.done():
                    receipt.set_result(None)

        worker = self._loop.run_in_executor(None, manager.with_idle_turn_ledger, apply)
        self._freeze_task = receipt
        worker.add_done_callback(finished)
        return receipt

    def arm_warm_census(self, controller) -> bool:
        # Called on the controller loop; idempotent even after the sample fires.
        if self._census_claimed:
            return False
        from core.gc_census import sample_controller

        self._census_claimed = True
        self._warm_since = max(self._watchdog_birth + 7200, self._monotonic())
        self._census = lambda: sample_controller(controller, trigger_candidates=self._trigger_candidates,
                                                trigger_uptime_s=round(self._trigger_uptime, 3),
                                                sample_uptime_s=round(self._monotonic() - self._watchdog_birth, 3))
        return True

    def _observe_gc(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._gc_started_at = self._monotonic()
        elif phase == "stop":
            started_at, self._gc_started_at = self._gc_started_at, None
            if started_at is None:
                return
            if (self._census is not None and info['generation'] == 2 and _active_requests == 0
                    and self._warm_since is not None and started_at >= self._warm_since):
                self._trigger_candidates = info.get('candidates', -1)
                self._trigger_uptime = self._monotonic() - self._watchdog_birth
                self._census_due = True  # scalar signal only; no object enumeration in a collector callback
            if (self._freeze_state == "armed" and info['generation'] == 2 and started_at >= self._freeze_since
                    and not _active_requests and self._freeze_manager is not None and not self._freeze_manager.in_flight):
                self._freeze_due = True
            duration = self._monotonic() - started_at
            if duration > GC_SLOW_SECONDS:
                self._gc_reports.append((
                    time.time(), duration, info["generation"], info["collected"],
                    info["uncollectable"], threading.get_ident(), info.get("candidates", -1), _active_requests,
                    tuple(item["collections"] for item in gc.get_stats()),
                ))

    def _report_gc(self) -> None:
        while self._gc_reports:
            ended_at, duration, generation, collected, uncollectable, thread_id, candidates, active, counts = self._gc_reports.popleft()
            logger.warning(
                "Slow garbage collection: duration_ms=%.1f generation=%d collected=%d "
                "uncollectable=%d thread_id=%d ended_at_unix=%.6f candidates=%d active_requests=%d collections=%s",
                duration * 1000, generation, collected, uncollectable, thread_id, ended_at, candidates, active, counts,
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
        if self._freeze_due:
            self._freeze_due = False
            if (self._freeze_state == "armed" and self._freeze_task is None and not _active_requests
                    and self._freeze_manager is not None and not self._freeze_manager.in_flight):
                self._try_warm_freeze(self._freeze_manager)
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
                if self._census_due and self._census is not None and not _active_requests:
                    census, self._census = self._census, None
                    self._census_due = False
                    census()
                    del census
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

    def _index_own_modules(self) -> None:
        """At a stall only: label Avibe's own code by identity, newly loaded modules only.

        Code is reached from each own module's namespace: its functions, the methods
        of classes it defines, and code nested in those (closures, lambdas,
        comprehensions). Only code whose filename equals the module's own file is
        taken, so imports and decorator wrappers keep their own attribution. A frame's
        filename or name is never read; a cloned or generated code object stays
        redacted because it is not reachable from a namespace.
        """
        for name, module in list(sys.modules.items()):
            if (type(name) is not str or name in self._indexed_modules or type(module) is not ModuleType
                    or name.partition(".")[0] not in OWN_PACKAGES):
                continue
            self._indexed_modules.add(name)
            namespace = ModuleType.__getattribute__(module, "__dict__")
            source = namespace.get("__file__")
            if type(source) is not str:
                continue
            path = name.replace(".", "/") + ("/__init__.py" if source.endswith("__init__.py") else ".py")
            if not own_code_label(path, "x"):
                continue
            codes: list[CodeType] = []
            owners: list[tuple[object, int]] = [(value, 0) for value in list(namespace.values())]
            seen_classes: set[int] = set()
            while owners:
                owner, depth = owners.pop()
                if type(owner) in (staticmethod, classmethod):
                    owner = owner.__func__
                if type(owner) is property:
                    owners.extend((part, depth) for part in (owner.fget, owner.fset, owner.fdel) if part is not None)
                elif type(owner) is FunctionType:
                    codes.append(owner.__code__)
                elif isinstance(owner, type) and depth < 3 and id(owner) not in seen_classes:
                    seen_classes.add(id(owner))
                    attrs = type.__dict__["__dict__"].__get__(owner)
                    if attrs.get("__module__") == name:
                        owners.extend((value, depth + 1) for value in list(attrs.values()))
            while codes and len(self._code_labels) < _MAX_OWN_CODES:
                code = codes.pop()
                if type(code) is not CodeType or code.co_filename != source or id(code) in self._code_labels:
                    continue
                function = getattr(code, "co_qualname", code.co_name)  # 3.10 has co_name only
                if own_code_label(path, function):
                    self._code_labels[id(code)] = (code, (path, function))
                codes.extend(const for const in code.co_consts if type(const) is CodeType)

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
            self._index_own_modules()  # own modules not yet indexed; stalls only
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
