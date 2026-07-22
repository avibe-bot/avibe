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

    assert location.actual_path == run_dir / ".uds"
    assert location.connect_path == location.actual_path


def test_darwin_socket_path_stays_under_the_realistic_run_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory(prefix="memory-poc-", dir="/tmp") as directory:
        base = Path(directory)
        direct_suffix = "/runs/stage1/.uds"
        padding = "x" * max(1, 98 - len(os.fsencode(base)) - len(direct_suffix))
        state = base / padding
        state.mkdir()
        run_dir = state / "runs" / "stage1"
        old_nested_path = run_dir / "socket" / "00000000.sock"
        monkeypatch.setattr("memory_poc.launcher.sys.platform", "darwin")

        assert len(os.fsencode(old_nested_path)) + 1 > 104
        assert len(os.fsencode(run_dir / ".uds")) + 1 <= 104
        location = new_socket_path(run_dir, state_root=state)

        assert location.actual_path.is_relative_to(run_dir)
        assert location.connect_path.is_relative_to(run_dir)
        assert len(os.fsencode(location.connect_path)) + 1 <= 104


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


def test_stop_retries_monitor_cleanup_before_releasing_owned_handles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    process.stop()

    assert monitor.calls == 2
    assert process.process is None
    assert process.tcp_monitor is None


def test_stop_removes_the_owned_socket_even_when_monitor_shutdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _process_for_stop(tmp_path)
    child = SimpleNamespace()

    class Monitor:
        process_group = None

        def known_processes(self) -> dict[int, float]:
            return {}

        def stop(self) -> dict[int, float]:
            raise LaunchError("tcp_listener_monitor_shutdown_timeout")

    with tempfile.TemporaryDirectory(prefix="memory-poc-", dir="/tmp") as directory:
        socket_path = Path(directory) / "sidecar.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        try:
            process.process = child  # type: ignore[assignment]
            process.tcp_monitor = Monitor()  # type: ignore[assignment]
            process.socket_path = socket_path
            monkeypatch.setattr("memory_poc.launcher.terminate_owned_process", lambda *_args, **_kwargs: None)

            with pytest.raises(LaunchError, match="tcp_listener_monitor_shutdown_timeout"):
                process.stop()

            assert not socket_path.exists()
        finally:
            listener.close()


@pytest.mark.skipif(os.name != "posix", reason="process cleanup requires POSIX")
def test_stop_retries_after_a_first_termination_failure_and_reaps_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _process_for_stop(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)

    class Monitor:
        process_group = None

        def known_processes(self) -> dict[int, float]:
            return {}

        def stop(self) -> dict[int, float]:
            return {}

    monitor = Monitor()
    calls = [0]
    actual_terminate = terminate_owned_process

    def terminate(*_args: object, **_kwargs: object) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise LaunchError("sidecar_process_termination_failed")
        actual_terminate(child, timeout_seconds=1)

    process.process = child
    process.tcp_monitor = monitor  # type: ignore[assignment]
    monkeypatch.setattr("memory_poc.launcher.terminate_owned_process", terminate)

    try:
        process.stop()
        assert calls[0] >= 2
        assert child.poll() is not None
        assert process.process is None
        assert process.tcp_monitor is None
    finally:
        actual_terminate(child, timeout_seconds=1)


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


def test_stop_terminates_descendant_when_direct_child_already_exited(monkeypatch):
    """Reviewer r3/r4 blocking: a direct child that has already exited while a
    same-group worker is still alive. stop() must terminate the worker, not
    short-circuit to success on the exited child alone."""

    from memory_poc.launcher import EverOSProcess

    # A fake process whose poll() says it has exited (direct child gone) ...
    class _ExitedPopen:
        def poll(self):
            return 0  # exited

        @property
        def pid(self):
            return -1  # no real direct child; descendants live via the group

    # A real short-lived grandchild in its own process group, reported by the
    # monitor as a tracked descendant and discoverable via the group snapshot.
    grandchild = subprocess.Popen(["sleep", "30"], start_new_session=True)
    grandchild_pgid = os.getpgid(grandchild.pid)
    grandchild_create = psutil.Process(grandchild.pid).create_time()

    proc = EverOSProcess.__new__(EverOSProcess)
    proc.process = _ExitedPopen()
    proc.socket_path = None
    proc.socket_location = None
    proc.uds_only_verified = False

    class _FakeMonitor:
        process_group = grandchild_pgid

        def known_processes(self):
            return {grandchild.pid: grandchild_create}

        def stop(self):
            return None

    proc.tcp_monitor = _FakeMonitor()
    try:
        # Before stop, the descendant keeps the tree non-reaped.
        assert proc.child_reaped is False, "live descendant must block child_reaped"
        # stop() must not short-circuit on the exited direct child; it must reap
        # the tracked descendant.
        proc.stop()
        alive = psutil.pid_exists(grandchild.pid) and grandchild.poll() is None
        assert not alive, "stop() left the tracked descendant alive"
    finally:
        if grandchild.poll() is None:
            grandchild.kill()
            grandchild.wait()
