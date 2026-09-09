"""Actual parent/supervisor consumers with all privileged/process/socket work mocked."""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

import namespace


@pytest.fixture
def envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "allocated-task"
    root.mkdir()
    paths = {}
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env"):
        paths[name] = root / name
        paths[name].mkdir()
    sentinel = tmp_path / "outside-child-state"
    sentinel.write_bytes(b"outside-task-sentinel-private-bytes")
    context = argparse.Namespace(
        root=root, sentinel=sentinel, paths=paths, servers=[], candidate=None,
        candidate_calls=0, launched=False, candidate_started=False,
        probe_result={"isolation_probe": "pass", "original_preflight": True},
        probe_exit=0, probe_bytes=None, closed_channel=False, receipt="envelope.json",
    )
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(os.getuid() or 1000))
    monkeypatch.setenv("SUDO_GID", str(os.getgid() or 1000))
    # No sudo, mount, actual process, socket, privilege or ownership change.
    real_fstat = os.fstat

    def simulated_ownership(fd):
        info = list(real_fstat(fd))
        # Simulate setup's fchown, including hosts whose scratch inherits gid 0.
        info[5] = int(os.environ["SUDO_GID"])
        return os.stat_result(info)

    def no_chown(*_args):
        assert not context.candidate_started, "Ownership change after candidate execution."

    monkeypatch.setattr(os, "fstat", simulated_ownership)
    monkeypatch.setattr(os, "fchown", no_chown)
    monkeypatch.setattr(os, "chown", lambda *_args: pytest.fail("Path-based chown is forbidden."))
    monkeypatch.setattr(namespace, "namespace_ids", lambda: {"net": "outer-net", "mnt": "outer-mnt", "pid": "outer-pid"})

    class FakeSentinel:
        def __init__(self, family):
            self.port = 17000 + len(context.servers)
            self.connections = 0
            self.closed = False
            self.family = family
            context.servers.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(namespace, "Sentinel", FakeSentinel)

    def fake_run(command, **kwargs):
        assert kwargs["close_fds"] is True
        if command[0] == "/usr/bin/unshare":
            context.launched = True
            assert "--kill-child=KILL" in command and kwargs["timeout"] == 900
            assert len(kwargs["pass_fds"]) == 1
            parent_fd = kwargs["pass_fds"][0]
            assert str(parent_fd) == command[command.index("--proof-fd") + 1]
            assert os.fstat(parent_fd).st_nlink == 0
            child_fd = os.dup(parent_fd)
            context.child_fd = child_fd
            inner = argparse.Namespace(
                **paths, proof_fd=child_fd, command=["candidate-test"], receipt=context.receipt,
            )
            result = namespace.run_candidate(inner, ["mock-setpriv"], {})
            return subprocess.CompletedProcess(command, result)
        assert "pass_fds" not in kwargs, "Candidate/probe must not inherit the supervisor channel."
        if command[-1].endswith("isolation_probe.py"):
            assert not context.candidate_started
            assert kwargs["stdout"] == subprocess.PIPE and kwargs["timeout"] == 30
            proof = context.probe_bytes if context.probe_bytes is not None else json.dumps(context.probe_result).encode()
            return subprocess.CompletedProcess(command, context.probe_exit, stdout=proof)
        assert command == ["mock-setpriv", "candidate-test"]
        context.candidate_calls += 1
        context.candidate_started = True
        with pytest.raises(OSError) as closed:
            os.fstat(context.child_fd)
        assert closed.value.errno == errno.EBADF
        context.closed_channel = True
        if context.candidate:
            context.candidate()
        return subprocess.CompletedProcess(command, getattr(context, "candidate_exit", 0))

    monkeypatch.setattr(namespace.subprocess, "run", fake_run)
    argv = ["namespace.py", "--root", str(root)]
    for name, path in paths.items():
        argv += ["--" + name.replace("_", "-"), str(path)]
    argv += ["--network", "loopback", "--receipt", context.receipt, "--", "candidate-test"]
    monkeypatch.setattr(sys, "argv", argv)
    return context


def read_receipt(envelope) -> dict:
    return json.loads((envelope.root / "receipts" / envelope.receipt).read_text())


def assert_cleanup(envelope) -> None:
    assert envelope.servers and all(server.closed for server in envelope.servers)
    assert not list(envelope.root.glob("rootfs-*"))
    assert not list((envelope.root / "receipts").glob("*.preflight"))
    assert envelope.sentinel.read_bytes() == b"outside-task-sentinel-private-bytes"


@pytest.mark.parametrize("artifact", ["symlinks", "hardlinks", "directories", "fifos", "forged-files"])
def test_parent_never_consumes_child_receipt_or_probe_paths(envelope, artifact: str) -> None:
    def candidate():
        for name in (envelope.receipt, envelope.receipt + ".probe.json"):
            path = envelope.paths["state"] / name
            if artifact == "symlinks":
                path.symlink_to(envelope.sentinel)
            elif artifact == "hardlinks":
                os.link(envelope.sentinel, path)
            elif artifact == "directories":
                path.mkdir()
            elif artifact == "fifos":
                os.mkfifo(path)
            else:
                path.write_text('{"isolation_probe":"pass","forged":true}')

    envelope.candidate = candidate
    with pytest.raises(SystemExit) as completed:
        namespace.main()
    assert completed.value.code == 0
    receipt = read_receipt(envelope)
    assert receipt["status"] == "passed" and receipt["probe"] == envelope.probe_result
    assert envelope.closed_channel
    assert "outside-task-sentinel-private-bytes" not in json.dumps(receipt)
    assert "forged" not in json.dumps(receipt)
    assert_cleanup(envelope)


def test_later_probe_tampering_cannot_replace_preflight(envelope) -> None:
    def candidate():
        envelope.probe_result["original_preflight"] = False
        envelope.probe_result["forged"] = True
        (envelope.paths["state"] / (envelope.receipt + ".probe.json")).write_text(
            json.dumps(envelope.probe_result),
        )
        # Candidate stdout is not the preflight channel.
        print('{"isolation_probe":"pass","forged":true}')

    envelope.candidate = candidate
    with pytest.raises(SystemExit):
        namespace.main()
    receipt = read_receipt(envelope)
    assert receipt["probe"] == {"isolation_probe": "pass", "original_preflight": True}
    assert receipt["preflight_custody"] == "unnamed-supervisor-fd-closed-before-candidate"
    assert_cleanup(envelope)


@pytest.mark.parametrize("kind", ["regular", "symlink", "hardlink", "directory", "fifo"])
def test_preexisting_terminal_receipt_collision_preserves_evidence(envelope, kind: str) -> None:
    directory = envelope.root / "receipts"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    path = directory / envelope.receipt
    if kind == "regular":
        path.write_bytes(b"previous evidence")
    elif kind == "symlink":
        path.symlink_to(envelope.sentinel)
    elif kind == "hardlink":
        os.link(envelope.sentinel, path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = path.lstat()
    with pytest.raises(FileExistsError):
        namespace.main()
    assert path.lstat() == before
    assert not envelope.launched and not envelope.servers
    if kind == "regular":
        assert path.read_bytes() == b"previous evidence"
    assert envelope.sentinel.read_bytes() == b"outside-task-sentinel-private-bytes"


def test_preexisting_receipt_directory_symlink_is_not_followed(envelope) -> None:
    (envelope.root / "receipts").symlink_to(envelope.paths["state"], target_is_directory=True)
    with pytest.raises(OSError):
        namespace.main()
    assert not envelope.launched
    assert list(envelope.paths["state"].iterdir()) == []


def test_preexisting_probe_channel_collision_is_not_unlinked(envelope) -> None:
    directory = envelope.root / "receipts"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    path = directory / (envelope.receipt + ".preflight")
    path.symlink_to(envelope.sentinel)
    before = path.lstat()
    with pytest.raises(FileExistsError):
        namespace.main()
    assert path.lstat() == before and not envelope.launched
    assert envelope.sentinel.read_bytes() == b"outside-task-sentinel-private-bytes"


def test_preexisting_writable_receipt_directory_is_rejected(envelope) -> None:
    directory = envelope.root / "receipts"
    directory.mkdir()
    directory.chmod(0o770)
    with pytest.raises(RuntimeError, match="ownership or permissions"):
        namespace.main()
    assert not envelope.launched and list(directory.iterdir()) == []
    assert directory.stat().st_mode & 0o777 == 0o770


@pytest.mark.parametrize("failure", ["candidate-exit", "timeout", "probe-exit", "malformed-probe"])
def test_parent_failure_and_timeout_keep_receipt_and_cleanup(envelope, failure: str) -> None:
    if failure == "candidate-exit":
        envelope.candidate_exit = 7
    elif failure == "timeout":
        def timeout():
            raise subprocess.TimeoutExpired("mock-unshare-already-killed-and-waited", 900)
        envelope.candidate = timeout
    elif failure == "probe-exit":
        envelope.probe_exit = 2
    else:
        envelope.probe_bytes = b"not a preflight JSON"
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired, ValueError)):
        namespace.main()
    receipt = read_receipt(envelope)
    assert receipt["status"] == "failed" and receipt["temporary_rootfs_removed"]
    assert receipt["cleanup_errors"] == []
    if failure in ("probe-exit", "malformed-probe"):
        assert envelope.candidate_calls == 0 and receipt["preflight"] == "missing-or-invalid"
    else:
        assert receipt["preflight"] == "passed" and receipt["launcher_reaped"]
        assert envelope.closed_channel
    if failure == "timeout":
        assert receipt["failure"] == "TimeoutExpired"
    assert_cleanup(envelope)


def test_partial_sentinel_setup_failure_cleans_first_listener(envelope, monkeypatch) -> None:
    original = namespace.Sentinel

    def second_fails(family):
        if family == socket.AF_INET6:
            raise OSError("mock setup failure")
        return original(family)

    monkeypatch.setattr(namespace, "Sentinel", second_fails)
    with pytest.raises(OSError, match="mock setup failure"):
        namespace.main()
    assert read_receipt(envelope)["status"] == "failed"
    assert not envelope.launched
    assert_cleanup(envelope)


def test_cleanup_failure_is_never_reported_as_pass(envelope, monkeypatch) -> None:
    original = namespace.Sentinel.close

    def fails_after_close(server):
        original(server)
        if server.family == socket.AF_INET:
            raise RuntimeError("mock cleanup failure")

    monkeypatch.setattr(namespace.Sentinel, "close", fails_after_close)
    with pytest.raises(RuntimeError, match="acceptance failed"):
        namespace.main()
    receipt = read_receipt(envelope)
    assert receipt["status"] == "failed" and receipt["cleanup_errors"] == ["RuntimeError"]
    assert_cleanup(envelope)
