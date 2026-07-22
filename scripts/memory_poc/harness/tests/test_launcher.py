from __future__ import annotations

import os
import socket
import tempfile
from types import SimpleNamespace
from pathlib import Path

import psutil
import pytest

from memory_poc.errors import LaunchError
from memory_poc.launcher import assert_no_tcp_listener, secure_socket, validate_socket_path


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
