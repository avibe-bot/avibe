from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time

from core.loop_watchdog import EventLoopWatchdog


def test_gc_observation_is_thresholded_bounded_and_removed_on_stop(caplog, monkeypatch) -> None:
    # Existing loop tests cannot see GC callbacks. This protects the diagnostic
    # contract without a heap load or an actual slow collection on the host.
    caplog.set_level(logging.WARNING, logger="core.loop_watchdog")
    unrelated_callback = lambda phase, info: None
    callbacks = [unrelated_callback]
    monkeypatch.setattr(gc, "callbacks", callbacks)
    now = [0.0]
    loop = asyncio.new_event_loop()
    watchdog = EventLoopWatchdog(loop, poll_seconds=60, monotonic=lambda: now[0])
    info = {"generation": 2, "collected": 17, "uncollectable": 3}
    try:
        watchdog.start()
        watchdog.start()
        assert len(callbacks) == 2
        callback = callbacks[1]
        callback("stop", info)  # attached during a collection: no start to time
        for duration in (0.1, 0.2):
            now[0] = 0.0
            callback("start", info)
            now[0] = duration
            callback("stop", info)
        watchdog._report_gc()
        assert not _messages(caplog, logging.WARNING)

        callback("start", info)
        now[0] = 0.45
        callback("stop", info)
        assert not _messages(caplog, logging.WARNING)  # no logging on collector thread
        watchdog._report_gc()
        (message,) = _messages(caplog, logging.WARNING)
        assert "duration_ms=250.0 generation=2 collected=17 uncollectable=3" in message
        assert f"thread_id={threading.get_ident()} ended_at_unix=" in message

        for _ in range(100):
            callback("start", info)
            now[0] += 0.3
            callback("stop", info)
        assert len(watchdog._gc_reports) == 64
    finally:
        watchdog.stop()
        watchdog.stop()
        loop.close()
    assert callbacks == [unrelated_callback]


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
