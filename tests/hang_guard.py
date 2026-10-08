"""One generous bound for test gates that wait on events, not on time.

A gate that waits for a worker, a thread, a process or a clock reading ends on a
state change, never on a guess of how long the work takes. When that state never
comes, because a regression hangs, the gate still fails loudly inside its test
instead of leaving the CI per-file watchdog to kill the whole file.
``HANG_GUARD_SECONDS`` is far longer than any loaded runner needs, so it never
decides a passing outcome.
"""

from __future__ import annotations

import asyncio
import multiprocessing.connection
import queue
import threading
import time
from typing import Any, Awaitable, Callable, Iterable

import pytest

HANG_GUARD_SECONDS = 120.0


def _hung(what: str) -> None:
    __tracebackhide__ = True
    pytest.fail(f"hung for {HANG_GUARD_SECONDS:.0f}s waiting for {what}")


def wait(event: Any, what: str) -> None:
    """``event.wait()`` for a threading or multiprocessing Event."""
    __tracebackhide__ = True
    if not event.wait(HANG_GUARD_SECONDS):
        _hung(what)


def join(worker: Any, what: str) -> None:
    """``worker.join()`` for a daemon Thread or a Process.

    A thread that hangs cannot be stopped, so it must be a daemon: otherwise the
    interpreter waits for it at exit and the watchdog kills the file after all. A
    process that hangs is killed before the test fails.
    """
    __tracebackhide__ = True
    if isinstance(worker, threading.Thread) and not worker.daemon:
        raise ValueError(f"hang_guard.join needs a daemon thread, so a hang cannot block exit: {worker!r}")
    worker.join(HANG_GUARD_SECONDS)
    if worker.is_alive():
        if not isinstance(worker, threading.Thread):
            worker.kill()
            worker.join()
        _hung(what)


def get(items: queue.SimpleQueue | queue.Queue, what: str) -> Any:
    """``items.get()``."""
    __tracebackhide__ = True
    try:
        return items.get(timeout=HANG_GUARD_SECONDS)
    except queue.Empty:
        _hung(what)


def ready(waitables: Iterable[Any], what: str) -> list[Any]:
    """``multiprocessing.connection.wait(waitables)``: the ones that became ready."""
    __tracebackhide__ = True
    woke = multiprocessing.connection.wait(list(waitables), timeout=HANG_GUARD_SECONDS)
    if not woke:
        _hung(what)
    return woke


async def within(awaitable: Awaitable[Any], what: str) -> Any:
    """``await awaitable``."""
    __tracebackhide__ = True
    try:
        return await asyncio.wait_for(awaitable, HANG_GUARD_SECONDS)
    except asyncio.TimeoutError:
        _hung(what)


async def until(predicate: Callable[[], Any], what: str, *, interval: float = 0.01) -> None:
    """Poll ``predicate`` until it holds; for state with no event to wait on."""
    __tracebackhide__ = True
    give_up = time.monotonic() + HANG_GUARD_SECONDS
    while not predicate():
        if time.monotonic() >= give_up:
            _hung(what)
        await asyncio.sleep(interval)
