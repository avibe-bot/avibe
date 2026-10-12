from __future__ import annotations

import asyncio
import logging
import threading
import time

from core.loop_watchdog import EventLoopWatchdog


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


def _block_loop_until(released: threading.Event) -> None:
    # The name is what the report has to show.
    released.wait(10)


def _messages(caplog, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level and record.name == "core.loop_watchdog"]


def test_a_blocked_loop_is_reported_once_with_the_blocking_stack_and_its_length(caplog) -> None:
    caplog.set_level(logging.INFO, logger="core.loop_watchdog")
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    watchdog = EventLoopWatchdog(loop, stall_seconds=0.3, poll_seconds=0.05, report_interval_seconds=60)
    try:
        watchdog.start()
        released = threading.Event()
        loop.call_soon_threadsafe(_block_loop_until, released)
        _wait_for(lambda: _messages(caplog, logging.WARNING))
        # A stall keeps being one report, however long it lasts.
        time.sleep(0.5)
        released.set()
        _wait_for(lambda: _messages(caplog, logging.INFO))

        # A second stall inside the report interval adds only its recovery line.
        second = threading.Event()
        loop.call_soon_threadsafe(_block_loop_until, second)
        time.sleep(0.6)
        second.set()
        _wait_for(lambda: len(_messages(caplog, logging.INFO)) == 2)
    finally:
        watchdog.stop()
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()

    (warning,) = _messages(caplog, logging.WARNING)
    assert "Event loop has not answered for" in warning
    assert "(event loop)" in warning and "_block_loop_until" in warning
    assert all("Event loop answered again after a" in line for line in _messages(caplog, logging.INFO))


def test_a_turning_loop_is_never_reported(caplog) -> None:
    caplog.set_level(logging.INFO, logger="core.loop_watchdog")
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    watchdog = EventLoopWatchdog(loop, stall_seconds=0.3, poll_seconds=0.05)
    try:
        watchdog.start()
        time.sleep(1.0)
    finally:
        watchdog.stop()
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()
    assert _messages(caplog, logging.WARNING) == [] and _messages(caplog, logging.INFO) == []
