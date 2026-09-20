"""Consume all negative probe transports without processes, sockets or privileges."""

from __future__ import annotations

import errno
import json
from pathlib import Path
import subprocess

import pytest

import isolation_probe
from isolation import CAPABILITY_FIELDS, NAMESPACE_NAMES, keyring_identity


def valid_kernel_proof():
    return {
        "outer_namespaces": {name: f"{name}:[{index + 1}]" for index, name in enumerate(NAMESPACE_NAMES)},
        "namespaces": {name: f"{name}:[{index + 11}]" for index, name in enumerate(NAMESPACE_NAMES)},
        "keyring_boundary": keyring_identity(),
        "process_status": {**dict.fromkeys(CAPABILITY_FIELDS, "0"), "NoNewPrivs": "1", "Seccomp": "2"},
    }


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


@pytest.mark.parametrize("failure", ["missing-ipc", "same-ipc", "extra-ipc", "bad-ipc", "missing-filter",
                                  "wrong-filter", "seccomp-off", "nnp-off", "capability"])
def test_actual_preflight_main_refuses_kernel_evidence_before_probe_effect(monkeypatch, failure):
    proof = valid_kernel_proof()
    if failure == "missing-ipc":
        proof["namespaces"].pop("ipc")
    elif failure == "same-ipc":
        proof["namespaces"]["ipc"] = proof["outer_namespaces"]["ipc"]
    elif failure == "extra-ipc":
        proof["namespaces"]["user"] = "user:[4]"
    elif failure == "bad-ipc":
        proof["namespaces"]["ipc"] = 4
    elif failure == "missing-filter":
        proof.pop("keyring_boundary")
    elif failure == "wrong-filter":
        proof["keyring_boundary"]["program_sha256"] = "0" * 64
    else:
        key, value = {"seccomp-off": ("Seccomp", "0"), "nnp-off": ("NoNewPrivs", "0"),
                      "capability": ("CapEff", "1")}[failure]
        proof["process_status"][key] = value
    monkeypatch.setattr(isolation_probe, "namespace_receipt", lambda: proof)
    monkeypatch.setattr(isolation_probe, "probe_blocked_connection", lambda *_: pytest.fail("Socket probe reached."))
    monkeypatch.setattr(Path, "write_bytes", lambda *_: pytest.fail("File probe reached."))
    with pytest.raises(RuntimeError):
        isolation_probe.main()


def test_actual_preflight_main_emits_complete_kernel_evidence_with_all_effects_intercepted(monkeypatch, capsys):
    proof = valid_kernel_proof()
    proof.update(network="none", sentinel_ports=[17000, 17001], uid=501, gid=20, selected_build=None,
                 **{name: "/synthetic/" + name for name in
                    ("source", "fixture", "recipe", "toolchain", "python_env", "state", "output")})
    effects = []
    def write(path, contents):
        effects.append(("write", str(path), contents))
        if path.name == ".isolation-write-probe":
            raise PermissionError("finite readonly mount seam")
        assert path.name in ("writable-state-probe", "writable-output-probe")
        return len(contents)
    monkeypatch.setattr(isolation_probe, "namespace_receipt", lambda: proof)
    monkeypatch.setattr(isolation_probe, "probe_blocked_connection", lambda network, host, port, kind: {
        "network": network, "host": host, "port": port, "kind": kind, "blocked": True,
    })
    monkeypatch.setattr(Path, "write_bytes", write)
    monkeypatch.setattr(Path, "unlink", lambda path: effects.append(("unlink", str(path))))
    monkeypatch.setattr(Path, "exists", lambda _path: False)
    monkeypatch.setattr(Path, "read_text", lambda path: "" if str(path) == "/proc/self/mountinfo" else pytest.fail("Unexpected read."))
    monkeypatch.setattr(isolation_probe.os, "getuid", lambda: 501)
    monkeypatch.setattr(isolation_probe.os, "getgid", lambda: 20)
    isolation_probe.main()
    result = json.loads(capsys.readouterr().out)
    assert all(result[name] == proof[name] for name in valid_kernel_proof())
    assert result["isolation_probe"] == "pass" and len(result["blocked_attempts"]) == 12
    assert result["positive_listeners"] == [] and result["task_state_write"] and result["exclusive_output_write"]
    assert len(effects) == 9
