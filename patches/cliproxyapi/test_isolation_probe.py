"""Consume all negative probe transports without processes, sockets or privileges."""

from __future__ import annotations

import errno
import subprocess

import pytest

import isolation_probe


@pytest.fixture
def connection_outcome(monkeypatch):
    outcome = {"errno": errno.ECONNREFUSED, "calls": []}

    def connect(*_args, **_kwargs):
        outcome["calls"].append("connection")
        if outcome["errno"] is not None:
            raise OSError(outcome["errno"], "mock connection error")
        return Connection()

    class Connection:
        def request(self, *_args, **_kwargs):
            connect()

        def close(self):
            pass

        def sendall(self, _value):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def child(command, **kwargs):
        assert kwargs["close_fds"] and kwargs["capture_output"] and kwargs["timeout"] == 3
        outcome["calls"].append("subprocess")
        number = outcome["errno"]
        return subprocess.CompletedProcess(
            command, 3 if number is None else 0,
            stdout=b"" if number is None else str(number).encode(), stderr=b"",
        )

    monkeypatch.setattr(isolation_probe.socket, "create_connection", connect)
    monkeypatch.setattr(isolation_probe.http.client, "HTTPConnection", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(isolation_probe.subprocess, "run", child)
    return outcome


@pytest.mark.parametrize("network", ["none", "loopback"])
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("kind", ["http", "websocket", "http-proxy", "connect-proxy", "direct-ip", "subprocess"])
@pytest.mark.parametrize("number", [
    errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES,
    errno.EPERM, errno.EADDRNOTAVAIL, errno.ETIMEDOUT, None,
])
def test_actual_negative_probe_classification(connection_outcome, network, host, kind, number):
    connection_outcome["errno"] = number
    permitted = (
        number in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM)
        or (number == errno.EADDRNOTAVAIL and network == "none" and host == "::1")
    )
    if permitted:
        assert isolation_probe.probe_blocked_connection(network, host, 12345, kind) == {
            "host": host, "kind": kind, "blocked": True, "errno": number,
        }
    else:
        with pytest.raises(AssertionError):
            isolation_probe.probe_blocked_connection(network, host, 12345, kind)
    assert connection_outcome["calls"] == ["subprocess" if kind == "subprocess" else "connection"]
