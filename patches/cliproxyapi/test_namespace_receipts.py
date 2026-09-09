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
import tempfile
from types import SimpleNamespace

import pytest

import namespace
from budgets import PHASES


@pytest.fixture
def envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "allocated-task"
    root.mkdir()
    paths = {}
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env"):
        paths[name] = root / name
        paths[name].mkdir()
    paths["go_archive"] = root / "go.tar.gz"
    paths["go_archive"].write_bytes(b"test-archive-not-executed")
    sentinel = tmp_path / "outside-child-state"
    sentinel.write_bytes(b"outside-task-sentinel-private-bytes")
    context = argparse.Namespace(
        root=root, sentinel=sentinel, paths=paths, servers=[], candidate=None,
        candidate_calls=0, launched=False, candidate_started=False,
        probe_result={"isolation_probe": "pass", "original_preflight": True},
        probe_exit=0, probe_bytes=None, closed_channel=False, receipt="envelope.json",
    )
    monkeypatch.setattr(sys, "platform", "linux")
    # The macOS pure runner's actual tmp_path is not a Linux /tmp path. Test
    # the real domain validator separately; only this receipt fixture adapts it.
    monkeypatch.setattr(namespace, "validate_temporary_root", lambda path, **_kw: path.resolve())
    monkeypatch.setattr(namespace, "__file__", str(paths["recipe"] / "namespace.py"))
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
            assert "--kill-child=KILL" in command and kwargs["timeout"] == PHASES["test"].namespace_seconds
            assert "-I" in command and command[command.index("-c") + 1] == namespace.CHILD_TRAMPOLINE
            assert "--parent-namespaces" not in command and "--proof-fd" not in command
            assert len(kwargs["pass_fds"]) == 2
            parent_fd, control_fd = kwargs["pass_fds"]
            assert str(control_fd) == command[-1]
            assert os.fstat(parent_fd).st_nlink == 0
            assert os.fstat(control_fd).st_nlink == 0
            control = json.loads(os.pread(control_fd, 65536, 0))
            assert control["outer_namespaces"] == namespace.namespace_ids()
            assert control["output"] == str(root / "runs" / context.receipt)
            child_fd = os.dup(parent_fd)
            context.child_fd = child_fd
            inner = argparse.Namespace(
                **paths, proof_fd=child_fd, command=["candidate-test"], receipt=context.receipt, phase="test",
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
        assert kwargs["timeout"] == PHASES["test"].candidate_seconds
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
    argv += ["--phase", "test", "--network", "loopback", "--receipt", context.receipt, "--", "candidate-test"]
    monkeypatch.setattr(sys, "argv", argv)
    return context


def read_receipt(envelope) -> dict:
    return json.loads((envelope.root / "receipts" / envelope.receipt).read_text())


def assert_cleanup(envelope) -> None:
    assert envelope.servers and all(server.closed for server in envelope.servers)
    assert not list(envelope.root.glob("rootfs-*"))
    assert not list((envelope.root / "receipts").glob("*.preflight"))
    assert not list((envelope.root / "receipts").glob("*.control"))
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
            raise subprocess.TimeoutExpired("mock-candidate-already-killed-and-waited", PHASES["test"].candidate_seconds)
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
        assert receipt["exit_code"] == 124 and receipt["failure"] is None
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


@pytest.mark.parametrize("mapping", ["{}", '{"mnt":"fake"}',
                                   '{"mnt":"mnt:[1]","net":"net:[2]","pid":"pid:[3]"}'])
def test_public_cli_cannot_select_private_reentry(envelope, monkeypatch, mapping):
    argv = list(sys.argv)
    index = argv.index("--")
    argv[index:index] = ["--parent-namespaces", mapping, "--proof-fd", "7"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as refused:
        namespace.main()
    assert refused.value.code == 2
    assert not envelope.launched and not envelope.servers
    assert not (envelope.root / "receipts").exists()
    assert not list(envelope.root.glob("rootfs-*"))


@pytest.mark.parametrize("outer", [
    {}, {"net": "net:[1]"}, {"net": "net:[1]", "mnt": "mnt:[2]"},
    {"net": "net:[1]", "mnt": "mnt:[2]", "pid": "pid:[3]"},
])
def test_all_kernel_identities_required_before_first_mutation(monkeypatch, outer):
    monkeypatch.setattr(namespace, "namespace_ids", lambda: {"net": "net:[1]", "mnt": "mnt:[2]", "pid": "pid:[3]"})
    monkeypatch.setattr(namespace, "run", lambda *_: pytest.fail("Kernel mutation reached before namespace proof."))
    with pytest.raises(RuntimeError, match="outside new"):
        namespace.inside(SimpleNamespace(), outer)


def test_private_handoff_is_closed_before_inside(tmp_path, monkeypatch):
    with tempfile.TemporaryFile(dir=tmp_path) as control:
        values = {name: str(tmp_path / name) for name in (
            "root", "rootfs", "source", "fixture", "state", "recipe", "toolchain",
            "python_env", "go_archive", "output",
        )}
        outer = {"mnt": "mnt:[11]", "net": "net:[12]", "pid": "pid:[13]"}
        values.update(build=None, outer_namespaces=outer)
        control.write(json.dumps(values).encode())
        control.seek(0)
        fd = os.dup(control.fileno())
        original = os.fstat

        def root_owned(number):
            info = list(original(number))
            if number == fd:
                info[4] = 0
            return os.stat_result(info)

        monkeypatch.setattr(os, "fstat", root_owned)

        def consume(args, actual_outer):
            with pytest.raises(OSError):
                os.fstat(fd)
            assert actual_outer == outer and args.root == Path(values["root"])

        monkeypatch.setattr(namespace, "inside", consume)
        namespace.child_from_control(fd)


def test_named_handoff_is_rejected_before_inside(tmp_path, monkeypatch):
    path = tmp_path / "caller-controlled"
    path.write_bytes(b"{}")
    monkeypatch.setattr(namespace, "inside", lambda *_: pytest.fail("Untrusted handoff reached setup."))
    fd = os.open(path, os.O_RDONLY)
    with pytest.raises(RuntimeError, match="parent handoff"):
        namespace.child_from_control(fd)
    assert path.read_bytes() == b"{}"


def test_parent_deadline_failure_has_receipt_and_cleanup(envelope, monkeypatch):
    def deadline(command, **kwargs):
        assert command[0] == "/usr/bin/unshare"
        assert kwargs["timeout"] == PHASES["test"].namespace_seconds
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(namespace.subprocess, "run", deadline)
    with pytest.raises(subprocess.TimeoutExpired):
        namespace.main()
    receipt = read_receipt(envelope)
    assert receipt["status"] == "failed" and receipt["failure"] == "TimeoutExpired"
    assert receipt["launcher_reaped"] and receipt["budget"] == PHASES["test"].receipt()
    assert_cleanup(envelope)


def test_orphan_output_collision_preserves_all_previous_files(envelope):
    runs = envelope.root / "runs"
    runs.mkdir(mode=0o750)
    runs.chmod(0o750)
    output = runs / envelope.receipt
    output.mkdir()
    prior = output / "prior-evidence"
    prior.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError):
        namespace.main()
    assert not envelope.launched and not envelope.servers
    assert prior.read_bytes() == b"do not overwrite"
    assert read_receipt(envelope)["status"] == "failed"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "failed", "wrong-phase", "wrong-output"])
def test_prior_build_requires_real_matching_parent_receipt(envelope, kind):
    directory = envelope.root / "receipts"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    root = envelope.root / "runs" / "build-old.json"
    root.mkdir(parents=True)
    path = directory / root.name
    if kind == "symlink":
        path.symlink_to(envelope.sentinel)
    elif kind == "hardlink":
        os.link(envelope.sentinel, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        value = {"status": "failed" if kind == "failed" else "passed",
                 "phase": "test" if kind == "wrong-phase" else "build",
                 "output": str(envelope.paths["state"] if kind == "wrong-output" else root)}
        path.write_text(json.dumps(value))
        path.chmod(0o640)
    # Only the task-owned parent receipt directory is opened; no candidate
    # artifact consumer or privileged operation is involved.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((RuntimeError, OSError)):
            namespace.selected_build_receipt(SimpleNamespace(build=root), fd)
    finally:
        os.close(fd)
    assert envelope.sentinel.read_bytes() == b"outside-task-sentinel-private-bytes"


def test_phase_network_mismatch_refuses_before_parent_setup(envelope, monkeypatch):
    argv = list(sys.argv)
    argv[argv.index("--phase") + 1] = "build"
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(ValueError, match="network=none"):
        namespace.main()
    assert not envelope.servers and not (envelope.root / "receipts").exists()


def test_actual_private_setup_keeps_prior_build_readonly_and_only_current_output_writable(tmp_path, monkeypatch):
    """Exercise the setup consumer; every kernel/process operation is replaced."""
    root = tmp_path / "task"
    root.mkdir()
    paths = {}
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env", "output", "build", "rootfs"):
        paths[name] = root / name
        paths[name].mkdir()
    paths["go_archive"] = root / "go.tar.gz"
    paths["go_archive"].write_bytes(b"not a compiler")
    outer = {"mnt": "mnt:[1]", "net": "net:[2]", "pid": "pid:[3]"}
    inner = {"mnt": "mnt:[11]", "net": "net:[12]", "pid": "pid:[13]"}
    calls, closed = [], []
    args = argparse.Namespace(
        **paths, root=root, network="loopback", phase="wire", receipt="test-wire.json",
        selected_build={"output": str(paths["build"])}, uid=501, gid=1000, proof_fd=7,
        sentinel_ports=[17000, 17001], command=["never-executed"],
    )
    monkeypatch.setattr(namespace, "namespace_ids", lambda: inner)
    monkeypatch.setattr(namespace, "run", lambda *command: calls.append(command))
    monkeypatch.setattr(namespace.subprocess, "check_output", lambda _command: b'[{"ifname":"lo"}]')
    monkeypatch.setattr(os, "chroot", lambda path: calls.append(("mock-chroot", str(path))))
    monkeypatch.setattr(os, "chdir", lambda _path: None)
    monkeypatch.setattr(os, "closerange", lambda first, last: closed.append((first, last)))
    monkeypatch.setattr(os, "set_inheritable", lambda fd, value: calls.append(("inherit", fd, value)))
    monkeypatch.setattr(namespace.resource, "getrlimit", lambda _name: (64, 64))

    def candidate(actual_args, drop, env):
        assert actual_args is args and closed == [(3, 7), (8, 64)]
        assert "--clear-groups" in drop and "--no-new-privs" in drop
        assert all(flag in drop for flag in ("--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all"))
        assert env["HOME"] == str(paths["output"] / "home")
        marker = json.loads((paths["rootfs"] / "run/avibe-engine-test-isolation.json").read_text())
        assert marker["outer_namespaces"] == outer and marker["namespaces"] == inner
        assert marker["selected_build"] == args.selected_build and marker["phase"] == "wire"
        assert marker["budget"] == PHASES["wire"].receipt()
        return 0

    monkeypatch.setattr(namespace, "run_candidate", candidate)
    with pytest.raises(SystemExit) as finished:
        namespace.inside(args, outer)
    assert finished.value.code == 0
    assert calls[0] == ("/usr/bin/mount", "--make-rprivate", "/")
    for name in ("source", "fixture", "recipe", "toolchain", "python_env", "go_archive", "build"):
        target = str(paths["rootfs"] / str(paths[name]).lstrip("/"))
        assert ("/usr/bin/mount", "-o", "remount,bind,nosuid,ro", target) in calls
    for name in ("state", "output"):
        target = str(paths["rootfs"] / str(paths[name]).lstrip("/"))
        assert ("/usr/bin/mount", "-o", "remount,bind,nosuid", target) in calls
