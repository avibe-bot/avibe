from __future__ import annotations

import asyncio
import gc
import json
import logging
import sys
import threading
import time
import weakref
from types import SimpleNamespace

import pytest

import core.loop_watchdog as watchdog_module
from core.loop_watchdog import EventLoopWatchdog
from core.loop_watchdog_events import EVENT_MARKER, MAX_EVENT_BYTES, MAX_STACK_FRAMES, parse_watchdog_event
from vibe.logging_config import APPLICATION_LOG_FORMAT, RedactingFormatter


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
        assert parse_watchdog_event(RedactingFormatter(APPLICATION_LOG_FORMAT).format(caplog.records[-1])) is None

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
    watchdog = EventLoopWatchdog(loop, stall_seconds=0.15, poll_seconds=0.025)
    released, second = threading.Event(), threading.Event()
    try:
        watchdog.start()
        loop.call_soon_threadsafe(_block_loop_until, released)
        _wait_for(lambda: _messages(caplog, logging.WARNING))
        # A stall keeps being one report, however long it lasts.
        time.sleep(0.5)
        released.set()
        _wait_for(lambda: _messages(caplog, logging.INFO))

        # A second, independent stall inside the former 300s interval gets its
        # own sample; a prolonged stall above still gets only one.
        loop.call_soon_threadsafe(_block_loop_until, second)
        _wait_for(lambda: len(_messages(caplog, logging.WARNING)) == 2)
        second.set()
        _wait_for(lambda: len(_messages(caplog, logging.INFO)) == 2)
    finally:
        released.set()
        second.set()
        watchdog.stop()
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()

    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 2
    for warning in warnings:
        event = json.loads(warning.split(EVENT_MARKER)[1])
        assert event["sample_status"] == "ok"
        assert event["loop_id"] == id(loop)
        assert event["loop_thread_id"] == runner.ident
        assert event["watchdog_thread_id"] != runner.ident
        assert any(frame["function"] == "Event.wait" for frame in event["frames"])
        # Arbitrary application/test function and thread names are not labels.
        assert "_block_loop_until" not in warning and runner.name not in warning
    assert all("Event loop answered again after a" in line for line in _messages(caplog, logging.INFO))


def test_a_turning_loop_never_samples_serializes_or_refreshes_labels(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger="core.loop_watchdog")
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    watchdog = EventLoopWatchdog(loop, stall_seconds=0.3, poll_seconds=0.05)
    try:
        watchdog.start()
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(True)
            raise AssertionError("healthy loop must not sample/serialize/refresh")
        monkeypatch.setattr(watchdog, "_loop_stack", forbidden)
        monkeypatch.setattr(watchdog, "_refresh_code_labels", forbidden)
        monkeypatch.setattr(watchdog_module.json, "dumps", forbidden)
        time.sleep(0.5)
        assert not calls
    finally:
        watchdog.stop()
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()
    assert _messages(caplog, logging.WARNING) == [] and _messages(caplog, logging.INFO) == []


class _Frame:
    """Synthetic metadata only; touching locals or arbitrary frames fails."""
    back_reads = 0

    def __init__(self, code, back=None):
        self.f_code = code
        self.f_lineno = 42
        self.back = back

    @property
    def f_back(self):
        type(self).back_reads += 1
        return self.back

    @property
    def f_locals(self):
        raise AssertionError("locals are private")


def _formatted_event(caplog):
    record = next(record for record in reversed(caplog.records) if EVENT_MARKER in record.getMessage())
    line = RedactingFormatter(APPLICATION_LOG_FORMAT).format(record)
    event = parse_watchdog_event(line)
    assert event is not None
    return line, event


def test_loop_sample_has_identity_only_labels_and_bounded_traversal_and_bytes(caplog, monkeypatch) -> None:
    # A filename/name regex would leak the cloned code's metadata; CodeType
    # equality would incorrectly attribute it. Other threads must not be walked.
    import copy
    caplog.set_level(logging.WARNING, logger="core.loop_watchdog")
    watchdog = EventLoopWatchdog(SimpleNamespace(_thread_id=123))
    watchdog._refresh_code_labels()
    clone = copy.deepcopy.__code__.replace()
    assert clone == copy.deepcopy.__code__ and clone is not copy.deepcopy.__code__
    private = clone.replace(co_filename="/private/customer/secret.py", co_name="秘密_token_123")
    tail = _Frame(private)
    tail.back = tail  # unbounded/cyclic traversal still stops at the fixed depth
    root = _Frame(copy.deepcopy.__code__, _Frame(clone, tail))
    snapshot = {123: root, 456: object()}  # any access to the other frame would fail
    monkeypatch.setattr(sys, "_current_frames", lambda: snapshot)
    monkeypatch.setattr(threading, "enumerate", lambda: pytest.fail("no thread census"))
    _Frame.back_reads = 0
    watchdog._report_stall(3.5, 0.125)
    line, event = _formatted_event(caplog)
    assert snapshot == {}
    assert _Frame.back_reads == MAX_STACK_FRAMES
    assert len(event["frames"]) == MAX_STACK_FRAMES and event["truncated"]
    assert event["frames"][0] == {"file": "copy.py", "function": "deepcopy", "line": 42}
    assert all(frame == {"file": "<redacted>", "function": "<redacted>", "line": 42}
               for frame in event["frames"][1:])
    assert len(line.split(EVENT_MARKER)[1].encode("utf-8")) <= MAX_EVENT_BYTES
    assert all(secret not in line for secret in ("/private", "customer", "秘密", "token_123"))

    # Byte truncation is independently enforced, rather than assuming depth is
    # sufficient. Tighten the budget only in this test, keeping real labels.
    monkeypatch.setattr(watchdog_module, "MAX_EVENT_BYTES", 512)
    monkeypatch.setattr(sys, "_current_frames", lambda: {123: root})
    watchdog._report_stall(4.0, 0.0)
    _, event = _formatted_event(caplog)
    payload = caplog.records[-1].getMessage().split(EVENT_MARKER)[1]
    assert len(payload.encode("utf-8")) <= 512
    assert len(event["frames"]) < MAX_STACK_FRAMES and event["truncated"]


def test_sample_releases_selected_and_unrelated_frame_references(monkeypatch) -> None:
    # Keeping snapshots alive pins coroutine locals. Weakrefs on synthetic
    # frames expose retention without walking or collecting the real heap.
    frame, other = _Frame(test_sample_releases_selected_and_unrelated_frame_references.__code__), _Frame(None)
    selected_ref, other_ref = weakref.ref(frame), weakref.ref(other)
    snapshot = {123: frame, 456: other}
    del frame, other
    monkeypatch.setattr(sys, "_current_frames", lambda: snapshot)
    watchdog = EventLoopWatchdog(SimpleNamespace(_thread_id=123))
    result, status, truncated = watchdog._loop_stack(123)
    assert status == "ok" and len(result) == 1 and not truncated
    assert selected_ref() is None and other_ref() is None


@pytest.mark.parametrize("case", ["unknown_thread", "no_frame", "snapshot_error", "walk_error"])
def test_unavailable_samples_are_explicit_and_do_not_echo_errors(case, caplog, monkeypatch) -> None:
    caplog.set_level(logging.WARNING, logger="core.loop_watchdog")
    watchdog = EventLoopWatchdog(SimpleNamespace(_thread_id=None if case == "unknown_thread" else 123))
    calls = []
    def snapshot():
        calls.append(True)
        if case == "snapshot_error":
            raise RuntimeError("/private/secret")
        return {123: object()} if case == "walk_error" else {}
    monkeypatch.setattr(sys, "_current_frames", snapshot)
    watchdog._report_stall(2.5, 0.0)
    line, event = _formatted_event(caplog)
    assert event["sample_status"] == ("error" if "error" in case else "missing")
    assert event["frames"] == [] and not event["truncated"]
    assert "/private/secret" not in line
    assert bool(calls) is (case != "unknown_thread")


def test_fixed_vocabulary_covers_attribution_paths_and_resolves_late_modules(caplog, monkeypatch) -> None:
    # Distinct risk: startup may precede these imports. Refresh only unresolved
    # fixed targets at sampling, and export those labels through the real logger.
    import copy
    from aiohttp.compression_utils import ZLibDecompressor
    from core.agent_core.agent.loop import Agent
    from core.agent_core.harness import projection
    from core.agent_core.tools.jobs import LocalJobHost
    from storage.sessions_service import SQLiteSessionsService
    from vibe.runtime import current_process_owns_service_instance

    caplog.set_level(logging.WARNING, logger="core.loop_watchdog")
    monkeypatch.delitem(sys.modules, "core.agent_core.harness.projection")
    watchdog = EventLoopWatchdog(SimpleNamespace(_thread_id=123))
    watchdog._refresh_code_labels()
    assert id(projection.context_view.__code__) not in watchdog._code_labels
    monkeypatch.setitem(sys.modules, "core.agent_core.harness.projection", projection)
    callables = [
        (projection.context_view, "context_view"),
        (projection._borrowed_context_view, "_borrowed_context_view"),
        (projection.project, "project"),
        (Agent._request, "Agent._request"),
        (Agent._built, "Agent._built"),
        (Agent._prepare_conversation, "Agent._prepare_conversation"),
        (SQLiteSessionsService.load_state, "SQLiteSessionsService.load_state"),
        (current_process_owns_service_instance, "current_process_owns_service_instance"),
        (ZLibDecompressor.decompress_sync, "ZLibDecompressor.decompress_sync"),
        (LocalJobHost.wait, "LocalJobHost.wait"),
        (LocalJobHost.enforce_deadline, "LocalJobHost.enforce_deadline"),
        (LocalJobHost.meta, "LocalJobHost.meta"),
        (copy.deepcopy, "deepcopy"),
    ]
    frame = None
    for function, _ in reversed(callables):
        frame = _Frame(function.__code__, frame)
    monkeypatch.setattr(sys, "_current_frames", lambda: {123: frame})
    watchdog._report_stall(3.0, 0.0)
    _, event = _formatted_event(caplog)
    assert [entry["function"] for entry in event["frames"]] == [label for _, label in callables]
    assert not event["truncated"]


def test_gc_flags_and_wake_lateness_precede_reporting_and_exclude_previous_work(caplog, monkeypatch) -> None:
    # Deterministic scalar clock: reporting consumes 100s, but that is not the
    # next wait's scheduler lateness. No real delay, load or collection occurs.
    caplog.set_level(logging.INFO, logger="core.loop_watchdog")
    now = [0.0]
    callbacks = []
    watchdog = EventLoopWatchdog(SimpleNamespace(_thread_id=123, call_soon_threadsafe=callbacks.append),
                                 poll_seconds=1, monotonic=lambda: now[0])
    info = {"generation": 2, "collected": 0, "uncollectable": 0}
    watchdog._observe_gc("start", info)
    steps = iter((2.5, 1.0, 3.0))
    waits = []
    def wait(timeout):
        delay = next(steps, None)
        if delay is None:
            return True
        now[0] += delay
        waits.append(now[0])
        if len(waits) == 2:
            watchdog._observe_gc("stop", info)
            callbacks.pop()()  # recovery before a new independent stall
        return False
    watchdog._stopped = SimpleNamespace(wait=wait)
    original_report_gc = watchdog._report_gc
    def slow_reporting():
        original_report_gc()
        now[0] += 100.0
    monkeypatch.setattr(watchdog, "_report_gc", slow_reporting)
    monkeypatch.setattr(sys, "_current_frames", lambda: {})
    watchdog._run()
    events = [json.loads(message.split(EVENT_MARKER)[1]) for message in _messages(caplog, logging.WARNING)
              if EVENT_MARKER in message]
    assert len(events) == 2
    assert [event["gc_active"] for event in events] == [True, False]
    assert [event["watchdog_wake_lateness_ms"] for event in events] == [1500.0, 2000.0]
    assert [event["lag_ms"] for event in events] == [2500.0, 103000.0]
    assert any("Slow garbage collection: duration_ms=" in msg for msg in _messages(caplog, logging.WARNING))


def test_sampling_and_logging_failures_do_not_terminate_watchdog(caplog, monkeypatch) -> None:
    # An unavailable sampler and a broken handler cannot suppress later
    # independent stall detection or leak exception text in a fallback log.
    caplog.set_level(logging.WARNING, logger="core.loop_watchdog")
    now = [0.0]
    watchdog = EventLoopWatchdog(SimpleNamespace(_thread_id=123, call_soon_threadsafe=lambda cb: None),
                                 poll_seconds=1, monotonic=lambda: now[0])
    steps = iter((3.0, 4.0, 7.0))
    def wait(timeout):
        instant = next(steps, None)
        if instant is None:
            return True
        now[0] = instant
        if instant == 4.0:
            watchdog._answer()
        return False
    watchdog._stopped = SimpleNamespace(wait=wait)
    def broken_snapshot():
        raise RuntimeError("secret snapshot error")
    monkeypatch.setattr(sys, "_current_frames", broken_snapshot)
    warning = watchdog_module.logger.warning
    calls = []
    def flaky_logger(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("secret handler error")
        return warning(*args, stacklevel=2, **kwargs)
    monkeypatch.setattr(watchdog_module.logger, "warning", flaky_logger)
    watchdog._run()
    assert len(calls) == 2
    line, event = _formatted_event(caplog)
    assert event["sample_status"] == "error" and event["frames"] == []
    assert "secret" not in line


def test_offline_export_rejects_unsafe_unknown_malformed_and_legacy_lines(caplog, monkeypatch) -> None:
    # The offline boundary must retain safe attribution, not the raw line or
    # arbitrary JSON fields; it never reads a log or starts a capture.
    caplog.set_level(logging.WARNING, logger="core.loop_watchdog")
    monkeypatch.setattr(sys, "_current_frames", lambda: {})
    EventLoopWatchdog(SimpleNamespace(_thread_id=123))._report_stall(2.5, 0.0)
    line, event = _formatted_event(caplog)
    prefix, payload = line.split(EVENT_MARKER)
    assert parse_watchdog_event(line + "\n") == event
    invalid = [
        line.replace("core.loop_watchdog", "another.logger"),
        line.replace(" - WARNING - ", " - INFO - "),
        "private prefix " + line, line + "\nprivate suffix",
        prefix, prefix + "; thread stacks follow.\n/private/secret.py",
        prefix.replace("has not answered for", "answered again after a"),
        prefix + EVENT_MARKER + "[1]", prefix + EVENT_MARKER + "{",
        prefix + EVENT_MARKER + '{"version":1,' + payload[1:],
        prefix + EVENT_MARKER + "[" * 2000,
        prefix + EVENT_MARKER + " " * (MAX_EVENT_BYTES + 1),
    ]
    for change in (
        {"version": True}, {"version": 2}, {"kind": "unknown"}, {"private": "secret"},
        {"loop_id": True}, {"loop_thread_id": "private name"}, {"lag_ms": float("nan")},
        {"watchdog_wake_lateness_ms": float("inf")}, {"gc_active": "secret"},
        {"sample_status": "unknown"}, {"frames": {}}, {"truncated": 1},
        {"sample_status": "ok", "frames": [{"file": "/private/a.py", "function": "secret", "line": 42}]},
        {"sample_status": "ok", "frames": [{"file": "copy.py", "function": "private", "line": 42}]},
        {"sample_status": "ok", "frames": [{"file": [], "function": "deepcopy", "line": 42}]},
        {"sample_status": "ok", "frames": [{"file": "copy.py", "function": "deepcopy", "line": True}]},
        {"sample_status": "ok", "frames": [{"file": "copy.py", "function": "deepcopy", "line": 42, "locals": "secret"}]},
        {"sample_status": "ok", "frames": [{"file": "copy.py", "function": "deepcopy", "line": 42}] * 31},
    ):
        invalid.append(prefix + EVENT_MARKER + json.dumps(event | change))
    assert all(parse_watchdog_event(candidate) is None for candidate in invalid)
