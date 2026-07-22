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
from memory_poc.launcher import assert_no_tcp_listener, secure_socket, terminate_owned_process, validate_socket_path


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
        assert_no_tcp_listener(1, connection_provider=lambda _pid: [listener])


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_termination_reaps_a_sidecar_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    program = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
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

        terminate_owned_process(parent, timeout_seconds=5)

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
        terminate_owned_process(parent, timeout_seconds=5)
