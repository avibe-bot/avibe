"""Service logging acceptance, backpressure and exit contracts (issue #2748).

Existing handler tests cannot detect a producer waiting for a destination, a
listener dying on an emit error, or final records abandoned behind a sentinel.
These tests hold real stream writes, inspect delivered content, and release/join
all test threads even when an assertion fails.
"""
from __future__ import annotations

import asyncio
import importlib
import io
import logging
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import main
from vibe import service_logging


@pytest.fixture(autouse=True)
def logging_lifecycle():
    service_logging.shutdown_service_logging()
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    service_logging.shutdown_service_logging()
    assert root.handlers == handlers
    assert root.level == level


class HeldStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writer_threads = []

    def write(self, value):
        self.writer_threads.append(threading.get_ident())
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release destination")
        return super().write(value)


def install_stream(monkeypatch, stream):
    handler = main.ServiceStreamHandler(stream)
    monkeypatch.setattr(main, "_build_logging_handlers", lambda _: [handler])
    main.setup_logging()
    return handler


def test_controller_produces_while_destination_is_blocked(monkeypatch):
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers, root.level
    root.handlers = []  # Also exercise the original basicConfig path in the regression replay.
    existing_threads = set(threading.enumerate())
    stream = HeldStream()
    handler = install_stream(monkeypatch, stream)
    listener_threads = set(threading.enumerate()) - existing_threads
    produced = threading.Event()
    producer_ids = []

    async def turn():
        producer_ids.append(threading.get_ident())
        logging.getLogger("controller-test").info("slow destination")
        await asyncio.sleep(0)
        logging.getLogger("controller-test").info("next turn")
        produced.set()

    producer = threading.Thread(target=lambda: asyncio.run(turn()), daemon=True)
    producer.start()
    try:
        assert stream.entered.wait(2)
        assert produced.wait(1), "controller waited for stream.write()"
        assert stream.getvalue() == ""
    finally:
        stream.release.set()
        producer.join(3)
        service_logging.shutdown_service_logging()
        handler.close()
        root.handlers, root.level = previous_handlers, previous_level
    assert not producer.is_alive()
    assert all(not thread.is_alive() for thread in listener_threads)
    assert set(stream.writer_threads).isdisjoint(producer_ids)
    assert stream.getvalue().count("slow destination") == 1
    assert stream.getvalue().count("next turn") == 1
    assert handler._closed


def test_full_queue_drops_newest_and_shutdown_delivers_every_accepted_record(monkeypatch, capsys):
    monkeypatch.setattr(service_logging, "QUEUE_CAPACITY", 2)
    stream = HeldStream()
    install_stream(monkeypatch, stream)
    runtime = service_logging._runtime
    logger = logging.getLogger("drain-test")
    stopped = threading.Event()
    admission_closed = threading.Event()
    original_close = runtime.handler.close

    def close():
        original_close()
        admission_closed.set()

    monkeypatch.setattr(runtime.handler, "close", close)
    stopper = threading.Thread(target=lambda: (service_logging.shutdown_service_logging(), stopped.set()), daemon=True)
    try:
        logger.info("in-flight")
        assert stream.entered.wait(2)
        logger.info("final census")
        logger.warning("final watchdog")
        for _ in range(10):
            logger.error("overflow")
        assert runtime.handler.queue.qsize() == 2
        assert capsys.readouterr().err == ""
        stopper.start()
        assert admission_closed.wait(2)
        logger.warning("after admission closed")
        assert not stopped.wait(0.05), "stop returned without delivering accepted records"
    finally:
        stream.release.set()
        if stopper.ident is not None:
            stopper.join(3)
    assert stopped.is_set()
    lines = stream.getvalue().splitlines()
    assert [line.rsplit(" - ", 1)[-1] for line in lines[:3]] == ["in-flight", "final census", "final watchdog"]
    assert len(lines) == 4
    assert "'queue_full': 10" in lines[3]
    assert "overflow" not in stream.getvalue()
    assert "after admission closed" not in stream.getvalue()
    service_logging.shutdown_service_logging()


@pytest.mark.parametrize("failure", ["stream", "handler"])
def test_destination_and_prepare_errors_are_counted_without_recursion(monkeypatch, capsys, failure):
    class BrokenStream(io.StringIO):
        def write(self, value):
            raise OSError("broken destination")

    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler failure")

    class ReentrantStream(io.StringIO):
        def write(self, value):
            logging.getLogger("recursive-destination").warning("must not enqueue recursively")
            return super().write(value)

    broken = main.ServiceStreamHandler(BrokenStream()) if failure == "stream" else BrokenHandler()
    output = ReentrantStream()
    healthy = main.ServiceStreamHandler(output)
    monkeypatch.setattr(main, "_build_logging_handlers", lambda _: [broken, healthy])
    main.setup_logging()
    logger = logging.getLogger("error-test")
    logger.info("bad formatting %d", "not-a-number")
    for index in range(3):
        logger.info("record %s", index)
    service_logging._runtime.handler.queue.join()
    service_logging.shutdown_service_logging()
    lines = output.getvalue().splitlines()
    assert len(lines) == 4
    assert [line.rsplit(" - ", 1)[-1] for line in lines[:3]] == ["record 0", "record 1", "record 2"]
    assert "'handler_error': 3" in lines[3]
    assert "'prepare_error': 1" in lines[3]
    assert "'reentrant': 3" in lines[3]
    assert capsys.readouterr().err == ""
    assert broken._closed and healthy._closed


def test_reentrant_signal_logging_cannot_deadlock_queue_mutex(monkeypatch):
    output = io.StringIO()
    install_stream(monkeypatch, output)
    records = service_logging._runtime.handler.queue
    original_put = records._put

    def interrupted_put(record):
        # Exactly the state of a Python signal handler interrupting Queue.put:
        # Queue's non-reentrant mutex is held by the producer thread.
        logging.getLogger("signal-test").info("nested signal record")
        original_put(record)

    monkeypatch.setattr(records, "_put", interrupted_put)
    produced = threading.Event()
    producer = threading.Thread(target=lambda: (logging.info("outer record"), produced.set()), daemon=True)
    producer.start()
    assert produced.wait(2)
    producer.join(2)
    service_logging.shutdown_service_logging()
    assert "outer record" in output.getvalue()
    assert "nested signal record" not in output.getvalue()
    assert "'reentrant': 1" in output.getvalue()


def test_repeated_setup_reload_and_shutdown_preserve_format_levels_and_ownership(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(main, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setenv("VIBE_DISABLE_STDOUT_LOGGING", "1")
    # Early records must not prevent setup from taking ownership of root.
    logging.warning("before service setup")
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    threads, handlers = [], []
    for index in range(3):
        if index == 1:
            importlib.reload(main)
            monkeypatch.setattr(main, "get_logs_dir", lambda: tmp_path)
        main.setup_logging("WARNING")
        threads.append(service_logging._runtime.listener._thread)
        handlers.extend(service_logging._runtime.listener.handlers)
        assert len(root.handlers) == 1
        assert root.handlers[0] is service_logging._runtime.handler
        logging.getLogger("format-test").info("filtered record")
        logging.getLogger("format-test").warning("visible record %s", index)
    try:
        raise ValueError("traceback preserved")
    except ValueError:
        logging.getLogger("format-test").exception("final exception")
    service_logging.shutdown_service_logging()
    service_logging.shutdown_service_logging()
    assert all(not thread.is_alive() for thread in threads)
    assert all(handler.stream is None and handler._closed for handler in handlers)
    assert root.handlers == original_handlers and root.level == original_level
    text = (tmp_path / "vibe_remote.log").read_text()
    assert text.count("visible record") == 3
    assert "filtered record" not in text
    assert " - format-test - WARNING - [test_service_logging.py:" in text
    assert "test_repeated_setup_reload_and_shutdown_preserve_format_levels_and_ownership()" in text
    assert text.count("Traceback (most recent call last):") == 1
    assert "ValueError: traceback preserved" in text
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("disabled", [False, True])
def test_listener_keeps_stdout_policy_and_file_rotation(monkeypatch, tmp_path, capsys, disabled):
    monkeypatch.setattr(main, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "APPLICATION_LOG_MAX_BYTES", 256)
    monkeypatch.setattr(main, "APPLICATION_LOG_BACKUP_COUNT", 2)
    if disabled:
        monkeypatch.setenv("VIBE_DISABLE_STDOUT_LOGGING", "1")
    else:
        monkeypatch.delenv("VIBE_DISABLE_STDOUT_LOGGING", raising=False)
    main.setup_logging()
    for index in range(10):
        logging.getLogger("rotation-test").info("rotation-record-%s %s", index, "x" * 80)
    service_logging.shutdown_service_logging()
    files = sorted(tmp_path.glob("vibe_remote.log*"))
    assert [path.name for path in files] == ["vibe_remote.log", "vibe_remote.log.1", "vibe_remote.log.2"]
    assert "rotation-record-9" in files[0].read_text()
    assert "rotation-record-8" in files[1].read_text()
    assert "rotation-record-7" in files[2].read_text()
    stdout = capsys.readouterr().out
    assert stdout.count("rotation-record-") == (0 if disabled else 10)


def test_loss_reports_are_rate_limited_and_flush_final_totals(monkeypatch):
    from types import SimpleNamespace

    class BrokenStream(io.StringIO):
        def write(self, value):
            raise OSError("destination unavailable")

    now = [0.0]
    monkeypatch.setattr(service_logging, "time", SimpleNamespace(monotonic=lambda: now[0]))
    output = io.StringIO()
    handlers = [main.ServiceStreamHandler(BrokenStream()), main.ServiceStreamHandler(output)]
    monkeypatch.setattr(main, "_build_logging_handlers", lambda _: handlers)
    main.setup_logging()
    records = service_logging._runtime.handler.queue
    logger = logging.getLogger("rate-test")
    for index in range(20):
        logger.info("initial %s", index)
    records.join()
    assert "Service logging losses" not in output.getvalue()
    now[0] = 31.0
    for index in range(20):
        logger.info("burst %s", index)
    records.join()
    assert output.getvalue().count("Service logging losses") == 1
    now[0] = 62.0
    logger.info("after interval")
    records.join()
    assert output.getvalue().count("Service logging losses") == 2
    service_logging.shutdown_service_logging()
    # Two earlier summary writes also failed at the broken destination.
    assert output.getvalue().count("Service logging losses") == 3
    assert "'handler_error': 43" in output.getvalue().splitlines()[-1]


def break_file_destination(monkeypatch, handler, failure):
    def fail(*args):
        raise OSError("destination unavailable")

    if failure == "write":
        monkeypatch.setattr(handler.stream, "write", fail)
    else:
        monkeypatch.setattr(handler, "shouldRollover", lambda record: True)
        monkeypatch.setattr(handler, "doRollover", fail)


@pytest.mark.parametrize("file_failure", ["write", "rotation"])
@pytest.mark.parametrize("stderr_failure", [None, "write", "flush", "missing"])
def test_file_only_failure_has_bounded_listener_stderr_fallback(monkeypatch, tmp_path, file_failure, stderr_failure):
    from types import SimpleNamespace

    class Stderr(io.StringIO):
        def __init__(self):
            super().__init__()
            self.attempts = []

        def write(self, value):
            self.attempts.append((threading.get_ident(), value))
            if stderr_failure == "write":
                raise OSError("stderr unavailable")
            return super().write(value)

        def flush(self):
            if stderr_failure == "flush":
                raise OSError("stderr flush unavailable")
            return super().flush()

    now = [0.0]
    monkeypatch.setattr(service_logging, "time", SimpleNamespace(monotonic=lambda: now[0]))
    stderr = Stderr()
    monkeypatch.setattr(sys, "__stderr__", None if stderr_failure == "missing" else stderr)
    monkeypatch.setenv("VIBE_DISABLE_STDOUT_LOGGING", "1")
    monkeypatch.setattr(main, "get_logs_dir", lambda: tmp_path)
    main.setup_logging()
    runtime = service_logging._runtime
    assert len(runtime.listener.handlers) == 1
    file_handler = runtime.listener.handlers[0]
    listener = runtime.listener._thread
    break_file_destination(monkeypatch, file_handler, file_failure)
    logger = logging.getLogger("file-failure")
    for _ in range(20):
        logger.info("private_payload_never_in_stderr")
    runtime.handler.queue.join()
    assert stderr.attempts == []
    now[0] = 31.0
    for _ in range(20):
        logger.info("private_payload_never_in_stderr")
    runtime.handler.queue.join()
    assert len(stderr.attempts) == (0 if stderr_failure == "missing" else 1)
    now[0] = 62.0
    logger.info("private_payload_never_in_stderr")
    runtime.handler.queue.join()
    assert len(stderr.attempts) == (0 if stderr_failure == "missing" else 2)
    service_logging.shutdown_service_logging()
    assert not listener.is_alive()
    assert file_handler._closed and file_handler.stream is None
    assert len(stderr.attempts) == (0 if stderr_failure == "missing" else 3)
    if stderr_failure != "missing":
        for thread_id, text in stderr.attempts:
            assert thread_id == listener.ident
            assert len(text.encode("utf-8")) <= 1024
            assert "private_payload_never_in_stderr" not in text
            assert text.startswith("Service logging destinations unavailable; losses=")
        assert "'handler_error': 43" in stderr.attempts[-1][1]
        if stderr_failure != "write":
            assert stderr.getvalue().count("Service logging destinations unavailable") == 3
    assert (tmp_path / "vibe_remote.log").read_text() == ""


def test_blocked_stderr_fallback_never_blocks_producer(monkeypatch, tmp_path):
    from types import SimpleNamespace

    now = [0.0]
    monkeypatch.setattr(service_logging, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(service_logging, "QUEUE_CAPACITY", 2)
    stderr = HeldStream()
    monkeypatch.setattr(sys, "__stderr__", stderr)
    monkeypatch.setenv("VIBE_DISABLE_STDOUT_LOGGING", "1")
    monkeypatch.setattr(main, "get_logs_dir", lambda: tmp_path)
    main.setup_logging()
    runtime = service_logging._runtime
    listener = runtime.listener._thread
    break_file_destination(monkeypatch, runtime.listener.handlers[0], "write")
    now[0] = 31.0
    first_returned = threading.Event()
    continue_producing = threading.Event()
    produced = threading.Event()

    def produce():
        logger = logging.getLogger("blocked-stderr")
        logger.error("trigger fallback")
        first_returned.set()
        if not continue_producing.wait(3):
            return
        for _ in range(100):
            logger.error("during blocked stderr")
        produced.set()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    try:
        assert stderr.entered.wait(2)
        assert first_returned.wait(1), "initial fallback ran on producer"
        continue_producing.set()
        assert produced.wait(1), "producer waited on blocked stderr/queue space"
        assert runtime.handler.queue.qsize() == 2
        assert stderr.getvalue() == ""
    finally:
        continue_producing.set()
        stderr.release.set()
        producer.join(3)
        service_logging.shutdown_service_logging()
    assert not producer.is_alive() and not listener.is_alive()
    assert set(stderr.writer_threads) == {listener.ident}
    assert "'queue_full': 98" in stderr.getvalue()
    assert stderr.getvalue().count("Service logging destinations unavailable") == 2


def test_partial_handler_construction_closes_open_file(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBE_DISABLE_STDOUT_LOGGING", raising=False)
    file_handler = main.ServiceFileHandler(tmp_path / "vibe_remote.log")
    monkeypatch.setattr(main, "ServiceFileHandler", lambda *args, **kwargs: file_handler)

    def fail_stream(*args, **kwargs):
        raise RuntimeError("stdout handler unavailable")

    monkeypatch.setattr(main, "ServiceStreamHandler", fail_stream)
    with pytest.raises(RuntimeError, match="stdout handler unavailable"):
        main._build_logging_handlers(str(tmp_path))
    assert file_handler.stream is None and file_handler._closed
    service_logging.shutdown_service_logging()


def test_repeated_setup_opens_new_log_only_after_previous_drain_rotation(monkeypatch, tmp_path):
    held = HeldStream()
    built_again = threading.Event()
    setup_started = threading.Event()
    setup_done = threading.Event()
    build_count = 0

    def build(_):
        nonlocal build_count
        build_count += 1
        if build_count == 2:
            built_again.set()
        return [
            main.ServiceStreamHandler(held if build_count == 1 else io.StringIO()),
            main.ServiceFileHandler(tmp_path / "vibe_remote.log", maxBytes=256, backupCount=2),
        ]

    monkeypatch.setattr(main, "_build_logging_handlers", build)
    main.setup_logging()
    logger = logging.getLogger("replace-test")
    logger.info("old record %s", "x" * 120)

    def reconfigure():
        setup_started.set()
        main.setup_logging()
        setup_done.set()

    replacing = threading.Thread(target=reconfigure, daemon=True)
    try:
        assert held.entered.wait(2)
        logger.info("old drain rotates %s", "x" * 120)
        replacing.start()
        assert setup_started.wait(2)
        assert not built_again.wait(0.05), "opened new inode before previous drain/rotation"
    finally:
        held.release.set()
        if replacing.ident is not None:
            replacing.join(3)
    assert setup_done.is_set()
    logger.info("new current file")
    service_logging.shutdown_service_logging()
    assert "new current file" in (tmp_path / "vibe_remote.log").read_text()
    assert "new current file" not in (tmp_path / "vibe_remote.log.1").read_text()


@pytest.mark.parametrize("start_then_fail", [False, True])
def test_listener_start_failure_closes_handlers_and_allows_retry(monkeypatch, tmp_path, start_then_fail):
    monkeypatch.setenv("VIBE_DISABLE_STDOUT_LOGGING", "1")
    handler = main._build_logging_handlers(str(tmp_path))[0]
    monkeypatch.setattr(main, "_build_logging_handlers", lambda _: [handler])
    original_handlers = logging.getLogger().handlers[:]
    original_start = service_logging._Listener.start
    workers = []
    with monkeypatch.context() as patch:
        def fail_start(self):
            if start_then_fail:
                original_start(self)
                workers.append(self._thread)
            raise RuntimeError("cannot start listener")

        patch.setattr(service_logging._Listener, "start", fail_start)
        with pytest.raises(RuntimeError, match="cannot start listener"):
            main.setup_logging()
    assert handler.stream is None and handler._closed
    assert all(not thread.is_alive() for thread in workers)
    assert logging.getLogger().handlers == original_handlers
    service_logging.shutdown_service_logging()
    output = io.StringIO()
    install_stream(monkeypatch, output)
    logging.warning("retry works")
    service_logging.shutdown_service_logging()
    assert "retry works" in output.getvalue()


def test_atexit_fallback_drains_before_logging_closes_destinations(tmp_path):
    code = """
import logging
from pathlib import Path
import sys
import main
main.get_logs_dir = lambda: Path(sys.argv[1])
main.setup_logging()
for index in range(50):
    logging.getLogger('exit-test').warning('exit record %s', index)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "vibe_remote.log").read_text()
    assert text.count("exit record") == 50
    assert "exit record 49" in text


@pytest.mark.parametrize("release_fails", [False, True])
def test_startup_failure_drains_error_and_cleanup_even_when_cleanup_raises(monkeypatch, release_fails):
    output = io.StringIO()
    handler = main.ServiceStreamHandler(output)
    monkeypatch.setattr(main, "_build_logging_handlers", lambda _: [handler])
    monkeypatch.setattr(main, "acquire_service_instance_lock", lambda: None)
    from types import SimpleNamespace

    monkeypatch.setattr(main, "load_config", lambda: SimpleNamespace(runtime=SimpleNamespace(log_level="INFO")))

    def fail_startup():
        raise RuntimeError("startup failure after logging setup")

    def release():
        # No successor may acquire the same log path until this runtime's last
        # write/rotation and handler close have completed.
        assert service_logging._runtime is None
        assert handler._closed
        assert "final service diagnostics cleanup" in output.getvalue()
        if release_fails:
            raise RuntimeError("cleanup failure")

    monkeypatch.setattr(main, "_stop_macos_session_diagnostics", lambda _: logging.warning("final service diagnostics cleanup"))
    monkeypatch.setattr(main, "apply_claude_sdk_patches", fail_startup)
    monkeypatch.setattr(main, "release_service_instance_lock", release)
    import core.managed_skills

    monkeypatch.setattr(core.managed_skills, "prepare_builtin_skills", lambda: None)
    with pytest.raises(RuntimeError if release_fails else SystemExit):
        main.main()
    assert "Failed to start: startup failure after logging setup" in output.getvalue()
    assert "final service diagnostics cleanup" in output.getvalue()
    assert service_logging._runtime is None
    assert handler._closed
