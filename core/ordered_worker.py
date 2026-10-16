"""One daemon thread that runs submitted work in submission order.

For work the controller loop hands off and must never wait on: each job is
started only after the one before it has finished, and its outcome is delivered
through the ``Future`` that ``submit`` returns. The thread is a daemon, so a job
blocked on a dead disk or a hung subprocess cannot hold process exit; ``drain``
waits for the queue with a bound and abandons what is left.
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable

logger = logging.getLogger(__name__)


class OrderedWorker:
    """Runs submitted jobs one at a time, in submission order, on a daemon thread."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._jobs: queue.SimpleQueue[tuple[Future[None], Callable[..., object], tuple[Any, ...]]] = (
            queue.SimpleQueue()
        )
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, fn: Callable[..., object], *args: Any) -> Future[None]:
        future: Future[None] = Future()
        self._jobs.put((future, fn, args))
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
        return future

    def _run(self) -> None:
        while True:
            future, fn, args = self._jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                fn(*args)
            except BaseException as error:  # noqa: BLE001 - delivered through the future
                future.set_exception(error)
            else:
                future.set_result(None)

    def drain(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for every queued job; on timeout discard the rest and return False."""

        drained = self.submit(lambda: None)
        try:
            drained.result(timeout=timeout)
        except FuturesTimeoutError:
            # Python 3.10's futures timeout is not the builtin TimeoutError.
            discarded = 0
            while True:
                try:
                    future, _fn, _args = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if future.cancel() and future is not drained:
                    discarded += 1
            logger.warning(
                "%s work abandoned at shutdown after %.1fs: %d discarded",
                self._name,
                timeout,
                discarded,
            )
            return False
        return True
