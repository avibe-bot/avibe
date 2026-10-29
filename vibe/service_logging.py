"""Bounded, service-owned logging. See docs/plans/nonblocking-service-logging.md.

Producers prepare records and enqueue without waiting for destination I/O. Full
queues drop the newest record; accepted records are not evicted. Only the final
service-exit path may wait for drain (and thus for slow/hung destinations).
"""
from __future__ import annotations

import atexit
import logging
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from queue import Full, Queue

QUEUE_CAPACITY = 8192
REPORT_INTERVAL_SECONDS = 30.0


class _RaiseHandlerErrors:
    def handleError(self, record: logging.LogRecord) -> None:
        # Stdlib stream/file emit() normally prints to stderr and swallows these.
        # Let our listener count them instead, without recursively logging them.
        raise


class ServiceFileHandler(_RaiseHandlerErrors, RotatingFileHandler):
    pass


class ServiceStreamHandler(_RaiseHandlerErrors, logging.StreamHandler):
    pass


class _Losses:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: Counter[str] = Counter()

    def add(self, reason: str) -> None:
        with self._lock:
            self._counts[reason] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


class _QueueHandler(QueueHandler):
    def __init__(self, records: Queue, losses: _Losses, context: threading.local) -> None:
        super().__init__(records)
        self.losses = losses
        self.context = context
        self.accepting = True

    def emit(self, record: logging.LogRecord) -> None:
        if not self.accepting:
            return
        # A Python signal can log while this thread holds Queue's non-reentrant
        # mutex. Handler/formatter logging can recurse too. Never re-enter either.
        if getattr(self.context, "logging", False):
            self.losses.add("reentrant")
            return
        self.context.logging = True
        try:
            super().emit(record)
        finally:
            self.context.logging = False

    def handleError(self, record: logging.LogRecord) -> None:
        # QueueHandler calls this for full queues and failed record preparation.
        self.losses.add("queue_full" if isinstance(sys.exc_info()[1], Full) else "prepare_error")

    def close(self) -> None:
        # Serialize the admission boundary with emit, not with listener I/O.
        self.acquire()
        try:
            self.accepting = False
        finally:
            self.release()
        super().close()


class _Listener(QueueListener):
    # QueueListener's stubs expose only a put_nowait queue protocol. This
    # service owns a concrete bounded Queue and a sentinel that cannot be a log.
    queue: Queue
    _sentinel = None

    def __init__(self, records: Queue, handlers: list[logging.Handler], losses: _Losses, context: threading.local) -> None:
        super().__init__(records, *handlers, respect_handler_level=True)
        self.losses = losses
        self.context = context
        self._reported: dict[str, int] = {}
        self._last_report = time.monotonic()

    def _deliver(self, record: logging.LogRecord) -> bool:
        delivered = False
        for handler in self.handlers:
            if record.levelno < handler.level:
                continue
            try:
                if handler.handle(record):
                    delivered = True
            except Exception:
                self.losses.add("handler_error")
        return delivered

    def _report_losses(self, *, final: bool = False) -> None:
        now = time.monotonic()
        if not final and now - self._last_report < REPORT_INTERVAL_SECONDS:
            return
        counts = self.losses.snapshot()
        if counts == self._reported:
            return
        self._last_report = now
        self._reported = counts
        delivered = self._deliver(logging.LogRecord(
            __name__, logging.WARNING, __file__, 0,
            "Service logging losses (cumulative): %s", (counts,), None,
        ))
        if not delivered:
            # The background service has only the file sink. Do not send its
            # failure notice solely back to that same failed destination. This
            # best-effort escape path runs only on the listener, shares the
            # summary rate limit, and never contains record payloads.
            try:
                stderr = sys.__stderr__
                if stderr is not None:
                    message = f"Service logging destinations unavailable; losses={counts}"
                    stderr.write(message[:1023] + "\n")
                    stderr.flush()
            except Exception:
                # stderr can be unavailable too. No recursion, retries, or
                # producer-side fallback; a blocked stderr only holds this worker.
                pass

    def handle(self, record: logging.LogRecord) -> None:
        self._deliver(record)
        self._report_losses()

    def enqueue_sentinel(self) -> None:
        # Called only after admission closes, at service exit, off the live loop.
        # put_nowait (the stdlib default) would fail if the bounded queue is full.
        self.queue.put(self._sentinel)

    def _monitor(self) -> None:
        # Destination callbacks must not feed records back into their own queue.
        self.context.logging = True
        try:
            # The stdlib worker hook exists on every supported Python version,
            # but is deliberately omitted from typeshed's public interface.
            super()._monitor()  # type: ignore[misc]
        finally:
            self._report_losses(final=True)
            for handler in self.handlers:
                try:
                    handler.close()
                except Exception:
                    self.losses.add("close_error")
            self.context.logging = False


class _ServiceLogging:
    def __init__(self, handlers: list[logging.Handler]) -> None:
        self.root = logging.getLogger()
        self.previous_handlers = self.root.handlers[:]
        self.previous_level = self.root.level
        records: Queue = Queue(maxsize=QUEUE_CAPACITY)
        losses = _Losses()
        context = threading.local()
        self.handler = _QueueHandler(records, losses, context)
        self.listener = _Listener(records, handlers, losses, context)

    def stop(self) -> None:
        self.handler.close()
        self.listener.stop()
        # Keep the closed queue handler installed throughout drain so late
        # module-level logging cannot basicConfig() a synchronous stderr handler.
        self.root.handlers = self.previous_handlers
        self.root.setLevel(self.previous_level)


_lifecycle_lock = threading.RLock()
_runtime: _ServiceLogging | None = None


def configure_service_logging(
    build_handlers: Callable[[], list[logging.Handler]], level: int, formatter: logging.Formatter,
) -> None:
    """Replace the previous setup, opening new files only after its final rotation."""
    global _runtime
    with _lifecycle_lock:
        handlers: list[logging.Handler] = []
        runtime = None
        try:
            if not isinstance(level, int):
                raise ValueError("Service log level must be an integer")
            shutdown_service_logging()
            handlers = build_handlers()
            for handler in handlers:
                handler.setFormatter(formatter)
            runtime = _ServiceLogging(handlers)
            runtime.listener.start()
        except BaseException:
            # Thread.start can be interrupted after the worker actually started.
            thread = getattr(runtime.listener, "_thread", None) if runtime else None
            if runtime is not None and thread is not None and thread.is_alive():
                runtime.stop()
            else:
                if runtime is not None:
                    runtime.handler.close()
                for handler in handlers:
                    handler.close()
            raise
        runtime.root.handlers = [runtime.handler]
        runtime.root.setLevel(level)
        _runtime = runtime


def shutdown_service_logging() -> None:
    """Drain after service producers stop; safe before setup and on repeated calls.

    Accepted records are attempted in FIFO order on every destination. This waits
    for destination I/O, with no timeout: a permanently hung destination requires
    process termination, which forfeits the remaining queued records.
    """
    global _runtime
    with _lifecycle_lock:
        if _runtime is not None:
            _runtime.stop()
            _runtime = None


# Fallback for callers using setup outside main; main itself drains explicitly.
# Registered after logging's hook so drain precedes logging.shutdown at exit.
atexit.register(shutdown_service_logging)
