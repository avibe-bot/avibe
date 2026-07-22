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

from memory_poc.errors import LaunchError
from memory_poc.launcher import _TcpListenerMonitor, assert_no_tcp_listener, secure_socket, terminate_owned_process, validate_socket_path
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
