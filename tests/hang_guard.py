"""One generous bound for test gates that wait on events, not on time.

A gate that waits for a worker, a thread, a process or a clock reading ends on a
state change, never on a guess of how long the work takes. When that state never
comes, because a regression hangs, the gate still fails loudly inside its test
instead of leaving the CI per-file watchdog to kill the whole file.
``HANG_GUARD_SECONDS`` is far longer than any loaded runner needs, so it never
decides a passing outcome.

Invariant: when a gate fails, no worker it owns keeps running. Every stuck
process is killed before the failure. A stuck thread cannot be stopped, and one
left running would act inside the tests that follow, so the run ends instead
(``pytest.exit``): that process can no longer isolate tests.
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
    """``worker.join()`` for a daemon Thread or a Process; see ``join_all``."""
    __tracebackhide__ = True
    join_all([worker], what)


def join_all(workers: Iterable[Any], what: str) -> None:
    """Join every worker within one shared bound before failing for any of them.

    Every stuck process is killed first, so none outlives its test. A stuck thread
    ends the run (see the module invariant); it must be a daemon, so that it cannot
    hold the interpreter open at exit either.
    """
    __tracebackhide__ = True
    workers = list(workers)
    for worker in workers:
        if isinstance(worker, threading.Thread) and not worker.daemon:
            raise ValueError(f"hang_guard needs a daemon thread, so a hang cannot block exit: {worker!r}")
    give_up = time.monotonic() + HANG_GUARD_SECONDS
    for worker in workers:
        worker.join(max(0.0, give_up - time.monotonic()))
    stuck = [worker for worker in workers if worker.is_alive()]
    for worker in stuck:
        if not isinstance(worker, threading.Thread):
            worker.kill()
            worker.join()
    stuck_threads = [worker for worker in stuck if isinstance(worker, threading.Thread)]
    if stuck_threads:
        pytest.exit(
            f"hung for {HANG_GUARD_SECONDS:.0f}s waiting for {what}: {len(stuck_threads)} thread(s) "
            "still running, and a thread cannot be stopped, so this process can no longer "
            "isolate tests",
            returncode=pytest.ExitCode.TESTS_FAILED,
        )
    if stuck:
        _hung(f"{what} ({len(stuck)} of {len(workers)} still running)")


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
