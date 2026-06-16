from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import main
import pytest
from vibe import runtime


def test_build_logging_handlers_excludes_stdout_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_DISABLE_STDOUT_LOGGING", "1")

    handlers = main._build_logging_handlers(str(tmp_path))

    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.FileHandler)


def test_build_logging_handlers_keeps_stdout_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBE_DISABLE_STDOUT_LOGGING", raising=False)

    handlers = main._build_logging_handlers(str(tmp_path))

    assert len(handlers) == 2
    assert isinstance(handlers[0], logging.StreamHandler)
    assert isinstance(handlers[1], logging.FileHandler)


def test_start_service_disables_stdout_logging_for_background_process(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    pid_path = tmp_path / "vibe.pid"

    monkeypatch.setattr(runtime.paths, "get_runtime_pid_path", lambda: pid_path)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)
    monkeypatch.setattr(runtime, "get_service_main_path", lambda: Path("/tmp/main.py"))
    monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (True, 0))
    # Stub the real spawn so we never fork a real vibe service, and short-circuit
    # the post-spawn lock wait. This captures the env start_service would launch with.
    monkeypatch.setattr(runtime, "wait_for_service_pid", lambda pid, *args, **kwargs: True)

    def fake_spawn_service_background_process(args, stdout_name, stderr_name, env=None):
        captured["args"] = args
        captured["stdout_name"] = stdout_name
        captured["stderr_name"] = stderr_name
        captured["env"] = env
        return type("Process", (), {"pid": 12345, "poll": lambda self: None})()

    monkeypatch.setattr(runtime, "spawn_service_background_process", fake_spawn_service_background_process)

    pid = runtime.start_service()

    assert pid == 12345
    assert captured["stdout_name"] == "service_stdout.log"
    assert captured["stderr_name"] == "service_stderr.log"
    assert isinstance(captured["env"], dict)
    assert captured["env"]["VIBE_DISABLE_STDOUT_LOGGING"] == "1"


def test_spawn_background_detaches_stdin(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.setattr(runtime.paths, "get_runtime_dir", lambda: tmp_path)

    class FakePopen:
        pid = 12345

        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    process = runtime.spawn_service_background_process(["python3", "service.py"], "stdout.log", "stderr.log")

    assert process.pid == 12345
    stdin = captured["kwargs"]["stdin"]
    assert stdin.name == os.devnull
    assert stdin.closed is True
    assert captured["kwargs"]["start_new_session"] is True


def test_main_import_does_not_load_controller() -> None:
    code = "import sys; import main; raise SystemExit(1 if 'core.controller' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], check=False)

    assert result.returncode == 0


def test_main_acquires_lock_before_loading_config(monkeypatch):
    events = []

    monkeypatch.setattr(main, "acquire_service_instance_lock", lambda: events.append("lock"))
    monkeypatch.setattr(main, "load_config", lambda: events.append("load_config") or (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(main, "release_service_instance_lock", lambda: events.append("release"))

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 1
    assert events == ["lock", "load_config", "release"]


def test_shutdown_intent_missing_is_logged_not_ignored(monkeypatch, caplog):
    monkeypatch.setattr(main, "shutdown_intent_required", lambda: True)
    monkeypatch.setattr(main, "consume_shutdown_intent", lambda pid, signum: None)

    logger = logging.getLogger("test.shutdown")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        main._log_shutdown_intent(logger, signal.SIGTERM)

    assert "honoring signal" in caplog.text


def test_shutdown_signal_logging_is_lightweight(monkeypatch, caplog):
    monkeypatch.setattr(main.os, "getpid", lambda: 123)
    monkeypatch.setattr(main.os, "getppid", lambda: 1)
    monkeypatch.setattr(main.os, "getpgid", lambda pid: 123)
    monkeypatch.setattr(main.os, "getsid", lambda pid: 123)

    logger = logging.getLogger("test.shutdown")
    with caplog.at_level(logging.INFO, logger=logger.name):
        main._log_shutdown_signal(logger, signal.SIGTERM)

    assert "Received signal 15 pid=123 ppid=1 pgid=123 sid=123" in caplog.text
