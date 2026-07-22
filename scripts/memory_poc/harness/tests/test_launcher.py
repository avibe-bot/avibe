from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path

import psutil
import pytest

from memory_poc.environment import ProviderSettings
from memory_poc.errors import LaunchError
from memory_poc.launcher import (
    EverOSProcess,
    _TcpListenerMonitor,
    assert_no_tcp_listener,
    new_socket_path,
    secure_socket,
    terminate_owned_process,
    validate_socket_path,
)
from memory_poc.provider import EverOSClient


def test_secure_socket_enforces_owner_only_mode() -> None:
    # pytest's generated directory can itself overflow Darwin's short UDS path.
    with tempfile.TemporaryDirectory(prefix="mp-") as directory:
        path = Path(directory) / "everos.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            os.chmod(path, 0o666)
            secure_socket(path)

            assert path.stat().st_mode & 0o777 == 0o600
        finally:
            listener.close()
            path.unlink(missing_ok=True)


def test_socket_path_overflow_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / ("a" * 200)

    with pytest.raises(LaunchError, match="uds_path_too_long"):
        validate_socket_path(path)


def test_socket_location_resolves_inside_the_per_run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    run_dir = state / "runs" / "r1"
    monkeypatch.setattr("memory_poc.launcher._socket_path_limit", lambda: 4096)

    location = new_socket_path(run_dir, state_root=state)

    assert location.actual_path.parent == run_dir / "socket"
    assert location.connect_path.parent.resolve() == location.actual_path.parent
    assert location.actual_path.name.endswith(".sock")


def test_short_socket_alias_still_targets_the_per_run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    run_dir = state / "runs" / "r1"
    alias_candidate = state / "s" / "a" / "00000000.sock"
    monkeypatch.setattr("memory_poc.launcher._socket_path_limit", lambda: len(os.fsencode(alias_candidate)) + 1)

    location = new_socket_path(run_dir, state_root=state)

    assert location.alias_path is not None
    assert location.actual_path.parent == run_dir / "socket"
    assert location.connect_path.parent.resolve() == location.actual_path.parent


def _process_for_stop(tmp_path: Path) -> EverOSProcess:
    return EverOSProcess(
        python=Path(sys.executable),
        everos_root=tmp_path / "everos-root",
        child_home=tmp_path / "child-home",
        state_root=tmp_path,
        settings=ProviderSettings(
            llm_base_url="http://127.0.0.1",
            llm_model="test-llm",
            llm_api_key="test-key",
            embedding_base_url="http://127.0.0.1",
            embedding_model="test-embedding",
            embedding_api_key="test-key",
            source=tmp_path / ".env.poc",
        ),
        metrics_path=tmp_path / "request-counts.jsonl",
        owner_id="00000000-0000-4000-8000-000000000001",
    )


def test_stop_keeps_owned_handles_when_monitor_cleanup_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _process_for_stop(tmp_path)
    child = SimpleNamespace()

    class Monitor:
        process_group = None

        def __init__(self) -> None:
            self.calls = 0

        def known_processes(self) -> dict[int, float]:
            return {}

        def stop(self) -> dict[int, float]:
            self.calls += 1
            if self.calls == 1:
                raise LaunchError("tcp_listener_monitor_shutdown_timeout")
            return {}

    monitor = Monitor()
    process.process = child  # type: ignore[assignment]
    process.tcp_monitor = monitor  # type: ignore[assignment]
    monkeypatch.setattr("memory_poc.launcher.terminate_owned_process", lambda *_args, **_kwargs: None)

    with pytest.raises(LaunchError, match="tcp_listener_monitor_shutdown_timeout"):
        process.stop()

    assert process.process is child
    assert process.tcp_monitor is monitor

    process.stop()

    assert process.process is None
    assert process.tcp_monitor is None


def test_stop_keeps_owned_handles_when_termination_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _process_for_stop(tmp_path)
    child = SimpleNamespace()

    class Monitor:
        process_group = None

        def known_processes(self) -> dict[int, float]:
            return {}

        def stop(self) -> dict[int, float]:
            return {}

    monitor = Monitor()
    calls = [0]

    def terminate(*_args: object, **_kwargs: object) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise LaunchError("sidecar_process_termination_failed")

    process.process = child  # type: ignore[assignment]
    process.tcp_monitor = monitor  # type: ignore[assignment]
    monkeypatch.setattr("memory_poc.launcher.terminate_owned_process", terminate)

    with pytest.raises(LaunchError, match="sidecar_process_termination_failed"):
        process.stop()

    assert process.process is child
    assert process.tcp_monitor is monitor

    process.stop()

    assert process.process is None
    assert process.tcp_monitor is None


def test_sidecar_startup_and_request_timeouts_are_separate(tmp_path: Path) -> None:
    process = EverOSProcess(
        python=Path(sys.executable),
        everos_root=tmp_path / "everos-root",
        child_home=tmp_path / "child-home",
        state_root=tmp_path,
        settings=ProviderSettings(
            llm_base_url="http://127.0.0.1",
            llm_model="test-llm",
            llm_api_key="test-key",
            embedding_base_url="http://127.0.0.1",
            embedding_model="test-embedding",
            embedding_api_key="test-key",
            source=tmp_path / ".env.poc",
        ),
        metrics_path=tmp_path / "request-counts.jsonl",
        owner_id="00000000-0000-4000-8000-000000000001",
    )
    process.socket_path = tmp_path / "everos.sock"
    process.tcp_monitor = SimpleNamespace(assert_safe=lambda: None)  # type: ignore[assignment]

    client = process._client()

    assert process.startup_timeout_seconds == 30.0
    assert client.timeout_seconds == 390.0


def test_tcp_listener_is_rejected() -> None:
    listener = SimpleNamespace(status=psutil.CONN_LISTEN)

    with pytest.raises(LaunchError, match="tcp_listener_detected"):
        assert_no_tcp_listener(1, connection_provider=lambda _pid: [listener], process_ids_provider=lambda _pid: (1,))


def test_tcp_listener_is_rejected_for_a_descendant() -> None:
    listener = SimpleNamespace(status=psutil.CONN_LISTEN)

    with pytest.raises(LaunchError, match="tcp_listener_detected"):
        assert_no_tcp_listener(
            1,
            connection_provider=lambda pid: [] if pid == 1 else [listener],
            process_ids_provider=lambda _pid: (1, 2),
        )


def test_pinned_factory_serves_health_over_uds_without_tcp(tmp_path: Path) -> None:
    fake_factory = tmp_path / "fake" / "everos" / "entrypoints" / "api"
    fake_factory.mkdir(parents=True)
    for package in (
        fake_factory / "__init__.py",
        fake_factory.parent / "__init__.py",
        fake_factory.parent.parent / "__init__.py",
    ):
        package.write_text("", encoding="utf-8")
    (fake_factory / "app.py").write_text(
        "\n".join(
            (
                "from fastapi import FastAPI",
                "",
                "app = FastAPI()",
                "",
                "@app.get('/health')",
                "async def health():",
                "    return {'ok': True}",
                "",
                "def create_app():",
                "    return app",
                "",
            )
        ),
        encoding="utf-8",
    )

    with tempfile.TemporaryDirectory(prefix="mp-") as directory:
        socket_path = Path(directory) / "everos.sock"
        environment = {
            "MEMORY_POC_OWNER_ID": "00000000-0000-4000-8000-000000000001",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(tmp_path / "fake"),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "memory_poc.sidecar", "--uds", str(socket_path)],
            env=environment,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert process.poll() is None, "pinned factory child exited before binding its UDS"
            assert socket_path.exists(), "pinned factory child did not bind its UDS"
            secure_socket(socket_path)
            EverOSClient(socket_path, timeout_seconds=2).health()
            assert_no_tcp_listener(process.pid)
        finally:
            terminate_owned_process(process, timeout_seconds=1)


@pytest.mark.skipif(os.name != "posix", reason="process monitoring requires POSIX")
def test_tcp_monitor_rejects_a_delayed_listener() -> None:
    program = (
        "import socket, time; "
        "time.sleep(0.15); "
        "listener = socket.socket(socket.AF_INET); "
        "listener.bind(('127.0.0.1', 0)); "
        "listener.listen(); "
        "time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", program], start_new_session=True)
    monitor = _TcpListenerMonitor(process, interval_seconds=0.02)
    try:
        monitor.start()
        deadline = time.monotonic() + 3
        detected = False
        while time.monotonic() < deadline:
            try:
                monitor.assert_safe()
            except LaunchError as exc:
                assert "tcp_listener_detected" in str(exc)
                detected = True
                break
            time.sleep(0.02)
        assert detected, "delayed TCP listener was not detected"
    finally:
        try:
            monitor.stop()
        except LaunchError:
            pass
        terminate_owned_process(process, timeout_seconds=1)


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_tcp_monitor_rejects_a_reparented_group_descendant() -> None:
    program = (
        "import os, socket, time; "
        "child = os.fork(); "
        "os._exit(0) if child else None; "
        "time.sleep(0.15); "
        "listener = socket.socket(socket.AF_INET); "
        "listener.bind(('127.0.0.1', 0)); "
        "listener.listen(); "
        "time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", program], start_new_session=True)
    process_group = os.getpgid(process.pid)
    monitor = _TcpListenerMonitor(process, process_group=process_group, interval_seconds=0.02)
    try:
        monitor.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                monitor.assert_safe()
            except LaunchError as exc:
                assert "tcp_listener_detected" in str(exc)
                break
            time.sleep(0.02)
        else:
            pytest.fail("reparented TCP listener was not detected")
    finally:
        known_processes = monitor.known_processes()
        try:
            monitor.stop()
        except LaunchError:
            pass
        terminate_owned_process(
            process,
            timeout_seconds=1,
            known_processes=known_processes,
            process_group=process_group,
        )


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_termination_reaps_a_sidecar_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    program = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', \"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\"]); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", program, str(child_pid_path)], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))

        terminate_owned_process(parent, timeout_seconds=0.25)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and psutil.pid_exists(descendant_pid):
            try:
                if psutil.Process(descendant_pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.02)
        assert not psutil.pid_exists(descendant_pid) or psutil.Process(descendant_pid).status() == psutil.STATUS_ZOMBIE
    finally:
        terminate_owned_process(parent, timeout_seconds=1)
