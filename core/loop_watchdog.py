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
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from importlib.machinery import ModuleSpec
from types import CodeType, FunctionType, ModuleType

from core.loop_watchdog_events import (
    EVENT_MARKER,
    MAX_EVENT_BYTES,
    MAX_STACK_FRAMES,
    OWN_PACKAGES,
    PACKAGE_ROOT,
    REDACTED_LABEL,
    _CODE_TARGETS,
    own_code_label,
    shipped_vocabulary,
)

logger = logging.getLogger(__name__)

STALL_SECONDS = 2.0
POLL_SECONDS = 1.0
GC_SLOW_SECONDS = 0.2
_MAX_GC_REPORTS = 64
_MAX_OWN_CODES = 200_000
# Written only at Vibey stream boundaries on the controller loop; sampled without a lock by GC.
_active_requests = 0


def model_stream_opened() -> None:
    global _active_requests
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
        self._own_queue: list[str] = []
        self._own_seen_count = 0
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
        self._freeze_due = False
        self._freeze_since = float("inf")
        self._trigger_uptime = 0.0

    def arm_warm_freeze(self) -> str:
        """Arm the one-shot freeze of the warmed heap; idempotent, never resets the warm-up.

        It fires at the first natural full collection after two hours of uptime,
        whatever is running or streaming then: on the owner's machine moments with no
        open model stream were 2 in 1,827 slow collections, so any such condition is
        no condition. Cost: objects frozen then are never collected by the cycle
        collector until unfreeze or restart (reference counting still frees acyclic
        ones, such as request bodies and stream buffers, as each stream ends), and an
        unreachable cycle that passes through a frozen object also keeps whatever it
        references later. That is not bounded by the heap at freeze time; the field
        trial's memory guard and the unfreeze command are the safeguard. The number
        of Vibey streams open at the freeze is logged, so what they left can be read.
        """
        with self._freeze_lock:
            if self._freeze_state == "off" and not gc.get_freeze_count():
                self._freeze_since = max(self._watchdog_birth + 7200, self._monotonic())
                self._freeze_state = "armed"
            return self._freeze_state

    def rollback_warm_freeze(self) -> str:
        with self._freeze_lock:
            was = self._freeze_state
            if was == "frozen":
                gc.unfreeze()  # restore normal collection; do not force a full sweep
            if was in ("armed", "frozen"):
                self._freeze_state = "retired"
        if was in ("armed", "frozen"):
            # The boundary a field comparison needs; the freeze itself logs "frozen".
            logger.warning("Warm GC trial retired: was=%s", was)
        return self._freeze_state

    def _apply_warm_freeze(self) -> None:
        # On the loop thread. The count before freezing is cheap (empty unless another
        # component froze first, which this trial must not take over); after freezing
        # it would walk the whole permanent generation, so it is not logged.
        with self._freeze_lock:
            if self._freeze_state != "armed" or self._stopped.is_set() or gc.get_freeze_count():
                return
            gc.freeze()
            self._freeze_state = "frozen"
            streams = _active_requests
        logger.warning("Warm GC trial frozen: open_streams=%d", streams)

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
            if self._freeze_state == "armed" and info['generation'] == 2 and started_at >= self._freeze_since:
                self._freeze_due = True  # scalar signal only; applied by the loop at its next answer
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
            self._apply_warm_freeze()
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
                if not self._stall_reported and now - answered_at < 2 * self._poll_seconds:
                    self._index_own_step()  # healthy loop only; one module per poll
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

    def _index_own_step(self) -> None:
        """Index at most one newly loaded Avibe module per healthy watchdog poll.

        Labels come by identity: code reached from the module's namespace (its
        functions, methods of classes it defines, code nested in those), only when
        the module's file is the shipped file for its name and the code was compiled
        from that file. Names are the namespace path, not a frame's strings; each is
        kept only if the installed build's shipped source defines that name on the
code's first line (hash-checked RECORD file, parsed here, never at a stall). Paced to one module
        per poll and skipped while the loop is late, so it never delays a stall sample.
        """
        if not self._own_queue:
            count = len(sys.modules)
            if count != self._own_seen_count:
                self._own_seen_count = count
                self._own_queue = [
                    name for name in list(sys.modules)
                    if type(name) is str and name not in self._indexed_modules and name.partition(".")[0] in OWN_PACKAGES
                ]
            return
        name = self._own_queue.pop()
        module = sys.modules.get(name)
        if type(module) is not ModuleType:
            return
        namespace = ModuleType.__getattribute__(module, "__dict__")
        spec = namespace.get("__spec__")
        if type(spec) is ModuleSpec and object.__getattribute__(spec, "__dict__").get("_initializing"):
            self._own_seen_count = -1  # still importing; look again later
            return
        self._indexed_modules.add(name)
        source = namespace.get("__file__")
        if type(source) is not str:
            return
        path = name.replace(".", "/") + ("/__init__.py" if os.path.basename(source) == "__init__.py" else ".py")
        if os.path.realpath(source) != os.path.realpath(PACKAGE_ROOT / path):
            return  # an alias or a module loaded from elsewhere is not shipped Avibe code
        codes: list[tuple[CodeType, str]] = []
        owners = [(value, key) for key, value in list(namespace.items()) if type(key) is str]
        seen_classes: set[int] = set()
        while owners:
            owner, qualname = owners.pop()
            kind = type(owner)
            if kind in (staticmethod, classmethod):
                owner = owner.__func__
                kind = type(owner)
            if kind is property:
                owners.extend((part, qualname) for part in (owner.fget, owner.fset, owner.fdel) if part is not None)
            elif kind is FunctionType:
                code = owner.__code__
                if code.co_filename == source and code.co_name == qualname.rpartition(".")[2]:
                    codes.append((code, qualname))
            elif issubclass(kind, type) and qualname.count(".") < 3 and id(owner) not in seen_classes:
                seen_classes.add(id(owner))
                attrs = type.__dict__["__dict__"].__get__(owner)
                if attrs.get("__module__") == name:
                    owners.extend((value, f"{qualname}.{key}") for key, value in list(attrs.items()) if type(key) is str)
        if not shipped_vocabulary(path):
            return  # not a file of the installed build, or modified since
        while codes and len(self._code_labels) < _MAX_OWN_CODES:
            code, qualname = codes.pop()
            # Fixed labels keep precedence. The name must be defined in the shipped
            # file, starting on this code's first line. This is source-location
            # attribution: a label always names real shipped source, but in-process
            # code that deliberately recompiles a method at the same file, name and
            # line would be attributed to that method.
            if id(code) not in self._code_labels and own_code_label(path, qualname, code.co_firstlineno):
                self._code_labels[id(code)] = (code, (path, qualname))
            codes.extend(
                (const, f"{qualname}.<locals>.{const.co_name}")
                for const in code.co_consts if type(const) is CodeType and const.co_filename == source
            )

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
