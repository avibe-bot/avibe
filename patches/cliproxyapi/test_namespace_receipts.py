"""Actual parent/supervisor consumers with all privileged/process/socket work mocked."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

import namespace
from budgets import PHASES
from isolation import (
    CAPABILITY_FIELDS, NAMESPACE_NAMES, STORAGE_ENV, StorageContext,
    keyring_identity, require_namespaces, validate_state_root,
)


def kernel_proof():
    return {
        "outer_namespaces": {name: f"{name}:[{index + 1}]" for index, name in enumerate(NAMESPACE_NAMES)},
        "namespaces": {name: f"{name}:[{index + 11}]" for index, name in enumerate(NAMESPACE_NAMES)},
        "keyring_boundary": keyring_identity(),
        "process_status": {**dict.fromkeys(CAPABILITY_FIELDS, "0000000000000000"),
                           "NoNewPrivs": "1", "Seccomp": "2"},
    }


def stat_fields(info, **changes):
    """Preserve extended fields (especially st_rdev), unlike stat_result(tuple)."""
    return SimpleNamespace(**{**{name: getattr(info, name) for name in dir(info) if name.startswith("st_")},
                              **changes})


@pytest.fixture
def envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = StorageContext.capture()
    root = tmp_path / "allocated-task"
    root.mkdir(mode=0o700)
    paths = {}
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env"):
        paths[name] = root / name
        paths[name].mkdir()
    paths["go_archive"] = root / "go.tar.gz"
    paths["go_archive"].write_bytes(b"test-archive-not-executed")
    for name in ("budgets", "isolation", "namespace"):
        (paths["recipe"] / (name + ".py")).write_bytes(
            (Path(namespace.__file__).parent / (name + ".py")).read_bytes(),
        )
    sentinel = tmp_path / "outside-child-state"
    sentinel.write_bytes(b"outside-task-sentinel-private-bytes")
    context = argparse.Namespace(
        root=root, sentinel=sentinel, paths=paths, servers=[], candidate=None,
        candidate_calls=0, launched=False, candidate_started=False,
        probe_result={"isolation_probe": "pass", "original_preflight": True, **kernel_proof()},
        probe_exit=0, probe_bytes=None, closed_channel=False, receipt="envelope.json",
        storage_reader=namespace.sudo_storage_context,
        system_pin=namespace.pin_system_resources,
    )
    monkeypatch.setattr(sys, "platform", "linux")
    # The macOS pure runner's actual tmp_path is not a Linux /tmp path. Test
    # the real domain validator separately; only this receipt fixture adapts it.
    monkeypatch.setattr(namespace, "validate_temporary_root", lambda path, **kwargs: validate_state_root(path, **kwargs))
    monkeypatch.setattr(namespace, "__file__", str(paths["recipe"] / "namespace.py"))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(os.getuid() or 1000))
    monkeypatch.setenv("SUDO_GID", str(os.getgid() or 1000))
    # Caller provenance has dedicated consuming tests with fake proc metadata.
    # Receipt/lifecycle cases receive the already-admitted immutable context.
    monkeypatch.setattr(namespace, "sudo_storage_context", lambda uid, expected: storage)
    # No sudo, mount, actual process, socket, privilege or ownership change.
    real_stat, real_lstat, real_fstat = os.stat, os.lstat, os.fstat
    ownership, ownership_effects, admitted_inodes = {}, [], set()

    def admit_ownership_target(path):
        assert path.is_relative_to(tmp_path) and path == path.resolve(strict=True)
        info = real_lstat(path)
        assert stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        admitted_inodes.add((info.st_dev, info.st_ino))

    for path in (root, *paths.values(), sentinel):
        admit_ownership_target(path)

    def project(info):
        changed = ownership.get((info.st_dev, info.st_ino))
        return stat_fields(info, **changed) if changed is not None else info

    def modeled_fchown(fd, uid, gid):
        assert not context.candidate_started, "Ownership change after candidate execution."
        info = real_fstat(fd)
        key = info.st_dev, info.st_ino
        # The four production allocation targets are the only implicit
        # additions. Preexisting test prerequisites are admitted explicitly.
        # No recursive scan, proc-FD lookup, or real ownership change.
        if key not in admitted_inodes:
            for path in (root / "receipts", root / "runs",
                         root / "receipts" / context.receipt, root / "runs" / context.receipt):
                try:
                    target = real_lstat(path)
                except FileNotFoundError:
                    continue
                if (target.st_dev, target.st_ino) == key:
                    admit_ownership_target(path)
        assert key in admitted_inodes, "Ownership effect outside task fixture."
        current = project(info)
        ownership[key] = {
            "st_uid": current.st_uid if uid == -1 else uid,
            "st_gid": current.st_gid if gid == -1 else gid,
        }
        ownership_effects.append((key, uid, gid))

    def parent_owned(path):
        """Explicit already-valid prerequisite, never repair by actual main."""
        admit_ownership_target(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            os.fchown(fd, -1, int(os.environ["SUDO_GID"]))
        finally:
            os.close(fd)

    context.ownership = ownership
    context.ownership_effects = ownership_effects
    context.parent_owned = parent_owned
    context.raw_stat, context.raw_lstat, context.raw_fstat = real_stat, real_lstat, real_fstat
    monkeypatch.setattr(os, "stat", lambda *a, **kw: project(real_stat(*a, **kw)))
    monkeypatch.setattr(os, "lstat", lambda *a, **kw: project(real_lstat(*a, **kw)))
    monkeypatch.setattr(os, "fstat", lambda fd: project(real_fstat(fd)))
    monkeypatch.setattr(os, "fchown", modeled_fchown)
    monkeypatch.setattr(os, "chown", lambda *_args: pytest.fail("Path-based chown is forbidden."))
    monkeypatch.setattr(namespace, "namespace_ids", lambda: kernel_proof()["outer_namespaces"])
    monkeypatch.setattr(namespace, "pin_system_resources", lambda _stack: ([], {}))

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
            assert "--kill-child=KILL" in command and kwargs["timeout"] == PHASES[getattr(context, "phase", "test")].namespace_seconds
            assert "-I" in command and command[command.index("-c") + 1] == namespace.CHILD_TRAMPOLINE
            assert "--parent-namespaces" not in command and "--proof-fd" not in command
            assert "--ipc" in command
            control_fd = int(command[-1])
            control = json.loads(os.pread(control_fd, 65536, 0))
            parent_fd = control["proof_fd"]
            expected = {parent_fd, control_fd, *(record["fd"] for record in control["handles"].values()),
                        *(item["resource"]["fd"] for item in control["system_mounts"]),
                        *(record["fd"] for record in control["bootstrap"].values())}
            assert set(kwargs["pass_fds"]) == expected
            assert os.fstat(parent_fd).st_nlink == 0
            assert os.fstat(control_fd).st_nlink == 0
            assert control["outer_namespaces"] == namespace.namespace_ids()
            assert control["output"] == str(root / "runs" / context.receipt)
            if getattr(context, "launch", None):
                return context.launch(command, control, kwargs)
            child_fd = os.dup(parent_fd)
            context.child_fd = child_fd
            inner = argparse.Namespace(
                **paths, proof_fd=child_fd, command=["candidate-test"], receipt=context.receipt, phase="test",
                outer_namespaces=control["outer_namespaces"],
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
    argv += ["--phase", "test", "--network", "loopback", "--receipt", context.receipt,
             "--caller-storage-sha256", storage.fingerprint(), "--", "candidate-test"]
    monkeypatch.setattr(sys, "argv", argv)
    return context


@pytest.fixture
def installed_system(envelope, tmp_path, monkeypatch):
    """Actual finite pin owner over task files; only host metadata/open effects modeled."""
    root = tmp_path / "installed-system-系统"
    root.mkdir()
    directories = [root]
    for relative in ("usr", "usr/bin", "usr/sbin", "usr/lib", "usr/share", "usr/lib64", "dev"):
        path = root / relative
        path.mkdir()
        directories.append(path)
    devices = {}
    for name, minor in (("null", 3), ("zero", 5)):
        path = root / "dev" / name
        path.write_bytes(b"regular task backing; no host device I/O")
        info = envelope.raw_lstat(path)
        devices[(info.st_dev, info.st_ino)] = (name, minor)
    trusted = {(info.st_dev, info.st_ino) for info in map(envelope.raw_lstat, directories)}
    original_open, original_stat, original_fstat = os.open, os.stat, os.fstat
    context = SimpleNamespace(root=root, opened=[], device_opens=[], mounts=None, aliases=None,
                              fault=None, pinning=False, calls=0)

    def layout(shape, overrides=None):
        assert shape in ("directories", "relative-aliases", "absolute-aliases")
        for name in ("bin", "sbin", "lib", "lib64"):
            path = root / name
            if shape == "directories":
                path.mkdir()
            else:
                target = (overrides or {}).get(name, ("/" if shape == "absolute-aliases" else "") + "usr/" + name)
                path.symlink_to(target)
            info = envelope.raw_lstat(path)
            trusted.add((info.st_dev, info.st_ino))

    def project(info):
        key = info.st_dev, info.st_ino
        if key in devices:
            name, minor = devices[key]
            changes = {"st_mode": stat.S_IFCHR | 0o666, "st_rdev": os.makedev(1, minor), "st_uid": 0}
            if context.fault and context.fault[0] == name:
                changes.update(context.fault[1])
            return stat_fields(info, **changes)
        if key in trusted:
            return stat_fields(info, st_uid=0)
        return info

    def opening(path, flags, *args, **kwargs):
        is_device = bool(flags & 0o10000000)
        if context.pinning:
            if path == "/":
                assert flags == os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                path = root
            if is_device:
                assert path in ("null", "zero"), "RNG or another host device must not be acquired."
                assert flags == 0o10000000 | os.O_NOFOLLOW | os.O_CLOEXEC
                parent = envelope.raw_fstat(kwargs["dir_fd"])
                actual_dev = envelope.raw_lstat(root / "dev")
                assert (parent.st_dev, parent.st_ino) == (actual_dev.st_dev, actual_dev.st_ino)
                context.device_opens.append(path)
                # O_PATH is modeled only for the exact regular task backing.
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            fd = original_open(path, flags, *args, **kwargs)
            context.opened.append(fd)
            return fd
        assert not is_device, "No unmodeled device acquisition."
        return original_open(path, flags, *args, **kwargs)

    def pin(stack):
        context.calls += 1
        context.pinning = True
        try:
            result = envelope.system_pin(stack)
        finally:
            context.pinning = False
        context.mounts, context.aliases = result
        return result

    context.layout = layout
    monkeypatch.setattr(namespace, "require_native_abi", lambda: None)
    monkeypatch.setattr(namespace, "pin_system_resources", pin)
    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "stat", lambda *a, **kw: project(original_stat(*a, **kw)))
    monkeypatch.setattr(os, "fstat", lambda fd: project(original_fstat(fd)))
    return context


@pytest.mark.parametrize("device", ["null", "zero"])
@pytest.mark.parametrize("failure", ["type", "major", "minor", "owner"])
def test_actual_system_device_admission_refuses_before_parent_effects(envelope, installed_system, device, failure):
    installed_system.layout("relative-aliases")
    changes = {
        "type": {"st_mode": stat.S_IFREG | 0o600},
        "major": {"st_rdev": os.makedev(2, 3 if device == "null" else 5)},
        "minor": {"st_rdev": os.makedev(1, 8 if device == "null" else 9)},
        "owner": {"st_uid": os.getuid() or 1000},
    }
    installed_system.fault = device, changes[failure]
    with pytest.raises((ValueError, RuntimeError), match="wrong type|device identity|trusted ownership"):
        namespace.main()
    assert installed_system.calls == 1 and installed_system.mounts is None
    assert installed_system.device_opens == (["null"] if device == "null" else ["null", "zero"])
    for fd in installed_system.opened:
        with pytest.raises(OSError) as closed:
            envelope.raw_fstat(fd)
        assert closed.value.errno == errno.EBADF
    assert not envelope.launched and not envelope.servers and envelope.candidate_calls == 0
    assert not (envelope.root / "receipts").exists() and not (envelope.root / "runs").exists()


@pytest.mark.parametrize("alias", ["bin", "sbin", "lib", "lib64"])
@pytest.mark.parametrize("target", ["../outside", "/dev/urandom", "usr/lib/../lib"])
def test_actual_system_alias_admission_refuses_before_parent_effects(envelope, installed_system, alias, target):
    installed_system.layout("relative-aliases", {alias: target})
    with pytest.raises(RuntimeError, match="Unexpected installed system alias"):
        namespace.main()
    assert installed_system.calls == 1 and installed_system.mounts is None
    assert not installed_system.device_opens and not envelope.launched and not envelope.servers
    for fd in installed_system.opened:
        with pytest.raises(OSError) as closed:
            envelope.raw_fstat(fd)
        assert closed.value.errno == errno.EBADF
    assert not (envelope.root / "receipts").exists() and not (envelope.root / "runs").exists()


def read_receipt(envelope) -> dict:
    return json.loads((envelope.root / "receipts" / envelope.receipt).read_text())


def assert_cleanup(envelope) -> None:
    assert envelope.servers and all(server.closed for server in envelope.servers)
    assert not list((envelope.root / "receipts").glob("rootfs-*"))
    assert not list((envelope.root / "receipts").glob("*.preflight"))
    assert not list((envelope.root / "receipts").glob("*.control"))
    assert envelope.sentinel.read_bytes() == b"outside-task-sentinel-private-bytes"


def test_envelope_ownership_effect_is_consistent_and_preserves_raw_metadata(envelope):
    source = envelope.paths["source"]
    raw = envelope.raw_lstat(source)
    gid = raw.st_gid + 1
    assert gid > 0 and gid != raw.st_gid
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    root_fd = os.open(envelope.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert source.stat() == source.lstat() == os.fstat(source_fd) == raw
        assert envelope.ownership == {}
        os.fchown(source_fd, -1, gid)
        for info in (source.stat(), source.lstat(), os.stat(source_fd),
                     os.stat("source", dir_fd=root_fd), os.lstat("source", dir_fd=root_fd),
                     os.stat("source", dir_fd=root_fd, follow_symlinks=False), os.fstat(source_fd)):
            assert info.st_uid == raw.st_uid and info.st_gid == gid
            for name in (name for name in dir(raw) if name.startswith("st_") and name != "st_gid"):
                assert getattr(info, name) == getattr(raw, name)
        assert envelope.raw_stat(source) == envelope.raw_lstat(source) == envelope.raw_fstat(source_fd) == raw
        os.fchown(source_fd, raw.st_uid + 10000, -1)
        assert source.stat().st_uid == raw.st_uid + 10000 and os.fstat(source_fd).st_gid == gid
        previous = namespace.descriptor_identity(source.stat())
        os.fchown(source_fd, -1, -1)
        assert namespace.descriptor_identity(os.fstat(source_fd)) == previous
        assert envelope.raw_lstat(source) == raw
        before = dict(envelope.ownership)
        envelope.candidate_started = True
        with pytest.raises(AssertionError, match="after candidate"):
            os.fchown(source_fd, -1, gid + 1)
        assert envelope.ownership == before
        envelope.candidate_started = False
    finally:
        os.close(source_fd)
        os.close(root_fd)
    with pytest.raises(OSError) as closed:
        os.fstat(source_fd)
    assert closed.value.errno == errno.EBADF
    with pytest.raises(OSError) as closed:
        os.fchown(source_fd, -1, gid)
    assert closed.value.errno == errno.EBADF


def test_envelope_ownership_follows_inode_not_names_aliases_or_reused_fds(envelope, tmp_path):
    source = envelope.paths["source"]
    saved = source.with_name("preserved-source")
    raw = envelope.raw_lstat(source)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    root_fd = os.open(envelope.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fchown(source_fd, -1, raw.st_gid + 1)
        source.rename(saved)
        source.mkdir()
        replacement = envelope.raw_lstat(source)
        assert replacement.st_ino != raw.st_ino
        assert source.stat() == replacement
        assert saved.stat().st_gid == os.fstat(source_fd).st_gid == raw.st_gid + 1
        alias = envelope.root / "source-alias"
        alias.symlink_to(saved)
        alias_raw = envelope.raw_lstat(alias)
        assert alias.lstat() == os.lstat(alias) == alias_raw
        assert os.stat(alias, follow_symlinks=False) == alias_raw
        assert os.stat("source-alias", dir_fd=root_fd, follow_symlinks=False) == alias_raw
        assert alias.stat().st_gid == os.stat("source-alias", dir_fd=root_fd).st_gid == raw.st_gid + 1
        assert envelope.raw_lstat(saved).st_gid == raw.st_gid
        with pytest.raises(FileNotFoundError):
            os.stat("absent", dir_fd=root_fd, follow_symlinks=False)
        with pytest.raises(FileNotFoundError):
            os.lstat("absent", dir_fd=root_fd)
        replacement_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.dup2(replacement_fd, source_fd)
            assert os.fstat(source_fd) == replacement
            assert saved.stat().st_gid == raw.st_gid + 1
        finally:
            os.close(replacement_fd)
        # An unnamed file is not an admitted linked fixture object, even when
        # its numeric FD happens to be one that previously carried an effect.
        with tempfile.TemporaryFile(dir=tmp_path) as unnamed:
            before = dict(envelope.ownership)
            with pytest.raises(AssertionError, match="outside task fixture"):
                os.fchown(unnamed.fileno(), -1, raw.st_gid + 2)
            assert envelope.ownership == before
    finally:
        os.close(source_fd)
        os.close(root_fd)


@pytest.mark.parametrize("field", ["dev", "ino", "mode", "uid", "gid", "rdev"])
def test_actual_parent_rejects_single_input_identity_drift_before_allocation(envelope, monkeypatch, field):
    source = envelope.paths["source"]
    before = namespace.descriptor_identity(source.lstat())
    raw = envelope.raw_lstat(source)
    original_open, original_fstat = os.open, os.fstat
    reached = []

    def opening(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if path == "source":
            reached.append(fd)
            if field in ("uid", "gid"):
                os.fchown(fd, before["uid"] + 1 if field == "uid" else -1,
                          before["gid"] + 1 if field == "gid" else -1)
        return fd

    def changed(fd):
        info = original_fstat(fd)
        if fd in reached and field not in ("uid", "gid"):
            return stat_fields(info, **{"st_" + field: before[field] + (0o010 if field == "mode" else 1)})
        return info

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "fstat", changed)
    monkeypatch.setattr(namespace, "open_receipts", lambda *_: pytest.fail("Allocation after input identity drift."))
    with pytest.raises(ValueError, match="Pinned resource changed after admission"):
        namespace.main()
    assert len(reached) == 1 and not envelope.launched and not envelope.servers
    assert not (envelope.root / "receipts").exists() and not (envelope.root / "runs").exists()
    assert envelope.raw_lstat(source) == raw
    for fd in reached:
        with pytest.raises(OSError):
            os.fstat(fd)
    if field in ("uid", "gid"):
        after = namespace.descriptor_identity(source.lstat())
        assert {name for name in before if after[name] != before[name]} == {field}


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
    assert receipt["probe"] == {"isolation_probe": "pass", "original_preflight": True, **kernel_proof()}
    assert receipt["preflight_custody"] == "unnamed-supervisor-fd-closed-before-candidate"
    assert_cleanup(envelope)


@pytest.mark.parametrize("kind", ["regular", "symlink", "hardlink", "directory", "fifo"])
def test_preexisting_terminal_receipt_collision_preserves_evidence(envelope, kind: str) -> None:
    directory = envelope.root / "receipts"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    envelope.parent_owned(directory)
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
    envelope.parent_owned(directory)
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
    envelope.parent_owned(directory)
    with pytest.raises(RuntimeError, match="ownership or permissions"):
        namespace.main()
    assert not envelope.launched and list(directory.iterdir()) == []
    assert directory.stat().st_mode & 0o777 == 0o770


@pytest.mark.parametrize("collection", ["receipts", "runs"])
@pytest.mark.parametrize("failure", ["uid", "gid", "mode", "alias"])
def test_preexisting_collection_refuses_its_own_defect_without_ownership_repair(envelope, collection, failure):
    directory = envelope.root / collection
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    envelope.parent_owned(directory)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if failure in ("uid", "gid"):
            info = os.fstat(fd)
            os.fchown(fd, info.st_uid + 10000 if failure == "uid" else -1,
                      info.st_gid + 1 if failure == "gid" else -1)
        elif failure == "mode":
            directory.chmod(0o770)
        else:
            preserved = directory.with_name(collection + "-preserved")
            directory.rename(preserved)
            directory.symlink_to(preserved, target_is_directory=True)
        key = envelope.raw_fstat(fd).st_dev, envelope.raw_fstat(fd).st_ino
        before = namespace.descriptor_identity(os.fstat(fd))
        effects = list(envelope.ownership_effects)
        if failure == "alias" and collection == "runs":
            with pytest.raises(ValueError, match="Environment directory escapes"):
                namespace.main()
            assert not (envelope.root / "receipts").exists()
        elif failure == "alias":
            with pytest.raises(OSError):
                namespace.main()
        else:
            message = "ownership or permissions" if collection == "receipts" else "Run collection"
            with pytest.raises(RuntimeError, match=message):
                namespace.main()
        assert namespace.descriptor_identity(os.fstat(fd)) == before
        assert [effect for effect in envelope.ownership_effects if effect[0] == key] == [
            effect for effect in effects if effect[0] == key
        ]
        assert not envelope.launched and not envelope.servers
        if collection == "runs" and failure != "alias":
            assert read_receipt(envelope)["status"] == "failed"
    finally:
        os.close(fd)


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
    import builtins

    with tempfile.TemporaryFile(dir=tmp_path) as control:
        values = {name: str(tmp_path / name) for name in (
            "root", "rootfs", "source", "fixture", "state", "recipe", "toolchain",
            "python_env", "go_archive", "output",
        )}
        outer = kernel_proof()["outer_namespaces"]
        values.update(build=None, outer_namespaces=outer)
        fd = os.dup(control.fileno())
        original = os.fstat

        def root_owned(number):
            return stat_fields(original(number), st_uid=0) if number == fd else original(number)

        monkeypatch.setattr(os, "fstat", root_owned)

        def consume(body):
            with pytest.raises(OSError):
                os.fstat(fd)
            for record in values["bootstrap"].values():
                with pytest.raises(OSError):
                    os.fstat(record["fd"])
            namespace.child_from_control(body)

        observed = []
        monkeypatch.setattr(namespace, "inside", lambda args, ids: observed.append((args.root, ids)))
        monkeypatch.setattr(builtins, "_bootstrap_consumer", consume, raising=False)
        with ExitStack() as stack:
            bootstrap = {}
            for name in ("budgets", "isolation", "namespace"):
                path = tmp_path / (name + ".py")
                path.write_text("from builtins import _bootstrap_consumer as child_from_control\n"
                                if name == "namespace" else "# finite inspected test module\n")
                record = namespace.pin_beneath(
                    stack, _directory_fd(stack, tmp_path), Path(path.name), kind="file",
                )
                bootstrap[name] = {"fd": os.dup(record["fd"]), "identity": record["identity"],
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            values["bootstrap"] = bootstrap
            control.write(json.dumps(values).encode())
            control.flush()
            control.seek(0)
            monkeypatch.setattr(sys, "argv", ["-c", str(fd)])
            for name in bootstrap:
                monkeypatch.setitem(sys.modules, name, sys.modules[name])
            exec(compile(namespace.CHILD_TRAMPOLINE, "<actual fixed trampoline>", "exec"), {})
        assert observed == [(Path(values["root"]), outer)]


def test_named_handoff_is_rejected_before_inside(tmp_path, monkeypatch):
    path = tmp_path / "caller-controlled"
    path.write_bytes(b"{}")
    monkeypatch.setattr(namespace, "inside", lambda *_: pytest.fail("Untrusted handoff reached setup."))
    fd = os.open(path, os.O_RDONLY)
    monkeypatch.setattr(sys, "argv", ["-c", str(fd)])
    closures = []
    monkeypatch.setattr(os, "closerange", lambda start, end: closures.append((start, end)))
    with pytest.raises(RuntimeError, match="parent handoff"):
        exec(compile(namespace.CHILD_TRAMPOLINE, "<actual fixed trampoline>", "exec"), {})
    with pytest.raises(OSError):
        os.fstat(fd)
    assert path.read_bytes() == b"{}"
    assert closures and closures[0][0] == 3


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
    envelope.parent_owned(runs)
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
    envelope.parent_owned(directory)
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
    if kind in ("failed", "wrong-phase", "wrong-output"):
        envelope.parent_owned(path)
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


def _directory_fd(stack, path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    stack.callback(os.close, fd)
    return fd


@pytest.fixture
def private_setup(tmp_path, monkeypatch):
    """Task-only directory replacement models views; it is NOT a mount proof."""
    root = tmp_path / "task"
    root.mkdir(mode=0o700)
    paths = {}
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env", "output", "build",
                 "control", "runs"):
        paths[name] = root / name
        paths[name].mkdir(mode=0o750)
        paths[name].chmod(0o750)
    paths["rootfs"] = paths["control"] / "rootfs-test"
    paths["rootfs"].mkdir(mode=0o700)
    paths["go_archive"] = root / "go.tar.gz"
    paths["go_archive"].write_bytes(b"not a compiler")
    outer, inner = kernel_proof()["outer_namespaces"], kernel_proof()["namespaces"]
    calls, closed = [], []
    descriptors = []
    for name, path in {"root": root, **paths}.items():
        fd = os.open(path, os.O_RDONLY | (0 if name == "go_archive" else os.O_DIRECTORY))
        descriptors.append((name, namespace.retained_handle(fd, "file" if name == "go_archive" else "directory")))
    with tempfile.TemporaryFile(dir=tmp_path) as proof:
        args = argparse.Namespace(
            **paths, root=root, network="loopback", phase="wire", receipt="test-wire.json",
            selected_build={"output": str(paths["build"])}, uid=os.getuid(), gid=os.getgid(), proof_fd=os.dup(proof.fileno()),
            sentinel_ports=[17000, 17001], command=["never-executed"],
            storage_context=StorageContext.capture().record(), handles=dict(descriptors),
            system_mounts=[], system_aliases={},
        )
    saved = paths["rootfs"].with_name("preserved-underlay")
    context = SimpleNamespace(args=args, paths=paths, calls=calls, closed=closed, outer=outer, inner=inner,
                              saved=saved, descriptors=descriptors, bind_observer=None)

    def mount(source, target, filesystem, flags, data=None):
        calls.append(("mount", source, target, filesystem, flags, data))
        if filesystem == "tmpfs":
            paths["rootfs"].rename(saved)
            paths["rootfs"].mkdir(mode=0o755)
            paths["rootfs"].chmod(0o755)
        if flags == namespace.MS_BIND:
            marker = paths["rootfs"] / "run/avibe-engine-test-isolation.json"
            assert marker.exists() and marker.read_bytes() == b""
            if context.bind_observer:
                context.bind_observer(source, target)

    monkeypatch.setattr(namespace, "namespace_ids", lambda: inner)
    monkeypatch.setattr(namespace, "run", lambda *command: calls.append(command))
    monkeypatch.setattr(namespace, "linux_mount", mount)
    monkeypatch.setattr(namespace, "install_keyring_boundary", lambda: calls.append(("filter",)) or keyring_identity())
    monkeypatch.setattr(namespace.subprocess, "check_output", lambda _command, **_kw: b'[{"ifname":"lo"}]')
    monkeypatch.setattr(os, "fchdir", lambda fd: calls.append(("fchdir", namespace.descriptor_identity(os.fstat(fd)))))
    monkeypatch.setattr(os, "chroot", lambda path: calls.append(("mock-chroot", str(path))))
    monkeypatch.setattr(os, "chdir", lambda path: calls.append(("chdir", path)))
    monkeypatch.setattr(os, "closerange", lambda first, last: closed.append((first, last)))
    monkeypatch.setattr(os, "set_inheritable", lambda fd, value: calls.append(("inherit", fd, value)))
    monkeypatch.setattr(namespace.resource, "getrlimit", lambda _name: (4096, 4096))
    yield context
    for fd in {args.proof_fd, *(record["fd"] for _, record in descriptors)}:
        try:
            os.close(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF


def test_actual_private_setup_keeps_prior_build_readonly_and_only_current_output_writable(private_setup, monkeypatch):
    state = private_setup
    args, paths, calls, closed = state.args, state.paths, state.calls, state.closed

    def candidate(actual_args, drop, env):
        assert actual_args is args and closed == [(3, args.proof_fd), (args.proof_fd + 1, 4096)]
        assert "--clear-groups" in drop and "--no-new-privs" in drop
        assert all(flag in drop for flag in ("--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all"))
        assert env["HOME"] == str(paths["output"] / "home")
        marker = json.loads((paths["rootfs"] / "run/avibe-engine-test-isolation.json").read_text())
        assert marker["outer_namespaces"] == state.outer and marker["namespaces"] == state.inner
        assert marker["keyring_boundary"] == keyring_identity()
        assert marker["selected_build"] == args.selected_build and marker["phase"] == "wire"
        assert marker["budget"] == PHASES["wire"].receipt()
        for _, record in state.descriptors:
            with pytest.raises(OSError):
                os.fstat(record["fd"])
        os.close(args.proof_fd)
        return 0

    monkeypatch.setattr(namespace, "run_candidate", candidate)
    with pytest.raises(SystemExit) as finished:
        namespace.inside(args, state.outer)
    assert finished.value.code == 0
    assert calls[0] == ("mount", None, "/", None, namespace.MS_REC | namespace.MS_PRIVATE, None)
    for name in ("source", "fixture", "recipe", "toolchain", "python_env", "go_archive", "build"):
        bind = next(call for call in calls if call[:2] == ("mount", namespace.fd_path(args.handles[name]["fd"])))
        assert ("mount", None, bind[2], None, namespace.MS_BIND | namespace.MS_REMOUNT |
                namespace.MS_NOSUID | namespace.MS_NODEV | namespace.MS_RDONLY, None) in calls
    for name in ("state", "output"):
        bind = next(call for call in calls if call[:2] == ("mount", namespace.fd_path(args.handles[name]["fd"])))
        assert ("mount", None, bind[2], None, namespace.MS_BIND | namespace.MS_REMOUNT |
                namespace.MS_NOSUID | namespace.MS_NODEV, None) in calls
    assert calls.index(("filter",)) < next(i for i, call in enumerate(calls)
                                           if call[0] == "mount" and call[4] == 39)
    view = next(call[1] for call in calls if call[0] == "fchdir")
    assert view["ino"] == paths["rootfs"].stat().st_ino != state.saved.stat().st_ino
    assert ("mock-chroot", ".") in calls and ("chdir", "/") in calls


@pytest.mark.parametrize("variable", STORAGE_ENV)
@pytest.mark.parametrize("role", ["root", "state", "output", "generated-cache"])
def test_namespace_configured_storage_refuses_before_first_parent_write(envelope, monkeypatch, variable, role):
    boundary = {
        "root": envelope.root, "state": envelope.paths["state"],
        "output": envelope.root / "runs" / envelope.receipt,
        "generated-cache": envelope.paths["state"] / "cache",
    }[role]
    context = StorageContext.capture(environment={variable: str(boundary)})
    monkeypatch.setattr(namespace, "sudo_storage_context", lambda *_: context)
    monkeypatch.setattr(namespace, "open_receipts", lambda *_: pytest.fail("First parent write reached."))
    with pytest.raises(ValueError):
        namespace.main()
    assert not envelope.launched and not envelope.servers
    assert not (envelope.root / "receipts").exists()
    assert not (envelope.root / "runs").exists()


@pytest.fixture
def sudo_process(tmp_path, monkeypatch):
    """Real bounded proc-file reader, but every proc/identity surface is fake."""
    import isolation

    caller_home, root_home = tmp_path / "caller-home", tmp_path / "root-home"
    caller_home.mkdir()
    root_home.mkdir()
    uid = os.getuid()
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda number: SimpleNamespace(
        pw_dir=str(caller_home if number == uid else root_home),
    ))
    selected = {name: str(tmp_path / ("original-" + name)) for name in STORAGE_ENV}
    for name, value in selected.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)
    context = StorageContext.capture()
    proc = tmp_path / "fake-proc"
    proc.mkdir()
    executable = tmp_path / "fake-sudo"
    executable.write_bytes(b"never executed")
    (proc / "exe").symlink_to(executable)
    values = ["S", *(["0"] * 18), "123456"]
    (proc / "stat").write_text("765432 (sudo) " + " ".join(values))
    raw = b"\0".join(name.encode() + b"=" + value.encode() for name, value in selected.items()) + b"\0"
    (proc / "environ").write_bytes(raw)
    real_open, real_stat = os.open, os.stat
    sudo_info = list(executable.stat())
    sudo_info[4] = 0

    def open_proc(path, flags, *args, **kwargs):
        return real_open(proc if path == "/proc/765432" else path, flags, *args, **kwargs)

    def stat_sudo(path, *args, **kwargs):
        return os.stat_result(sudo_info) if path == "/usr/bin/sudo" else real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "getppid", lambda: 765432)
    monkeypatch.setattr(os, "open", open_proc)
    monkeypatch.setattr(os, "stat", stat_sudo)
    return SimpleNamespace(uid=uid, proc=proc, context=context, selected=selected, raw=raw,
                           sudo_info=sudo_info, root_home=root_home)


def test_parent_uses_original_sudo_environment_not_its_own_sanitized_home(sudo_process, monkeypatch):
    original = sudo_process
    for name in STORAGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(original.root_home))
    context = namespace.sudo_storage_context(original.uid, original.context.fingerprint())
    assert context.uid == original.uid
    for name, path in original.selected.items():
        target = Path(path) / (".avibe" if name == "HOME" else "task")
        with pytest.raises(ValueError):
            context.validate(target)
    with pytest.raises(ValueError):
        context.validate(original.root_home / ".codex")


@pytest.mark.parametrize("failure", [
    "missing-environment", "sanitized-environment", "forged-digest", "invalid-digest",
    "different-executable", "writable-executable", "oversized-environment",
    "changed-parent", "duplicate-storage-key", "invalid-encoding",
])
def test_sudo_context_loss_or_forgery_fails_closed(sudo_process, monkeypatch, failure):
    original = sudo_process
    expected = original.context.fingerprint()
    if failure == "missing-environment":
        (original.proc / "environ").unlink()
    elif failure == "sanitized-environment":
        (original.proc / "environ").write_bytes(b"HOME=/root\0")
    elif failure == "forged-digest":
        expected = "0" * 64
    elif failure == "invalid-digest":
        expected = '{"protected":[]}'
    elif failure == "different-executable":
        original.sudo_info[1] += 1
    elif failure == "writable-executable":
        original.sudo_info[0] |= 0o002
    elif failure == "oversized-environment":
        (original.proc / "environ").write_bytes(b"x" * (1024 * 1024 + 1))
    elif failure == "changed-parent":
        parents = iter((765432, 765433))
        monkeypatch.setattr(os, "getppid", lambda: next(parents))
    elif failure == "duplicate-storage-key":
        (original.proc / "environ").write_bytes(original.raw + b"HOME=/different\0")
    else:
        (original.proc / "environ").write_bytes(b"HOME=/invalid-\xff\0")
    with pytest.raises((ValueError, RuntimeError, OSError)):
        namespace.sudo_storage_context(original.uid, expected)


def test_changed_sudo_environment_during_read_fails(sudo_process, monkeypatch):
    read = namespace._proc_field
    reads = 0

    def changing(fd, name, limit):
        nonlocal reads
        value = read(fd, name, limit)
        if name == "environ":
            reads += 1
            if reads == 2:
                return b"HOME=/changed\0"
        return value

    monkeypatch.setattr(namespace, "_proc_field", changing)
    with pytest.raises(RuntimeError, match="lost or changed"):
        namespace.sudo_storage_context(sudo_process.uid, sudo_process.context.fingerprint())


@pytest.mark.parametrize("mutation", ["missing-digest", "claimed-root-map", "allow-flag"])
def test_public_cli_has_no_protection_bypass(envelope, monkeypatch, mutation):
    argv = list(sys.argv)
    if mutation == "missing-digest":
        index = argv.index("--caller-storage-sha256")
        del argv[index:index + 2]
    else:
        index = argv.index("--")
        argv[index:index] = (["--storage-context", '{"protected":[]}'] if mutation == "claimed-root-map"
                             else ["--allow-task-root", str(envelope.root)])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as refused:
        namespace.main()
    assert refused.value.code == 2
    assert not envelope.launched and not (envelope.root / "receipts").exists()


def test_documented_caller_binds_original_context_and_actual_parent_admission(
        envelope, sudo_process, monkeypatch):
    # Execute the maintained caller itself. Only sudo/process/privilege metadata
    # are simulated; the parent's storage and path admission remain real.
    readme = (Path(__file__).parent / "README.md").read_text()
    caller = readme.split("run_phase() {", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY\n}", 1)[0]
    monkeypatch.setattr(namespace, "sudo_storage_context", envelope.storage_reader)
    monkeypatch.setattr(sys, "argv", [
        "-", str(envelope.root), str(envelope.paths["fixture"]), "test", "loopback", envelope.receipt, "",
    ])
    # The README uses this documented archive layout.
    (envelope.root / "downloads").mkdir()
    (envelope.root / "downloads/go.tar.gz").write_bytes(b"not executed")
    (envelope.root / "venv").mkdir()
    reached = []

    class BeforeFirstWrite(Exception):
        pass

    def first_write(args, _stack, root_fd):
        context = StorageContext.from_parent(args.storage_context)
        assert set(sudo_process.context.protected).issubset(context.protected)
        assert args.root == envelope.root
        assert os.fstat(root_fd).st_ino == envelope.root.stat().st_ino
        reached.append(context.fingerprint())
        raise BeforeFirstWrite

    def sudo(command, **kwargs):
        assert command[:6] == ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c"]
        assert command[7] == str(envelope.paths["recipe"])
        assert kwargs["timeout"] == PHASES["test"].driver_seconds and kwargs["close_fds"]
        assert command[command.index("--caller-storage-sha256") + 1] == sudo_process.context.fingerprint()
        # Mimic sudo env_reset after the original executable's environment was
        # captured in fake proc. No actual sudo or subordinate process runs.
        for name in STORAGE_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HOME", str(sudo_process.root_home))
        monkeypatch.setenv("SUDO_UID", str(sudo_process.uid))
        monkeypatch.setenv("SUDO_GID", str(os.getgid() or 1000))
        monkeypatch.setattr(sys, "argv", ["-c", *command[7:]])
        with pytest.raises(BeforeFirstWrite):
            exec(compile(command[6], "<maintained privileged entry>", "exec"), {})
        return subprocess.CompletedProcess(command, 0)

    # The outer caller is genuinely non-root; simulate root only inside sudo.
    monkeypatch.setattr(os, "geteuid", lambda: sudo_process.uid)
    monkeypatch.setattr(sys, "path", list(sys.path))
    def call_sudo(command, **kwargs):
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        return sudo(command, **kwargs)

    monkeypatch.setattr(namespace, "open_receipts", first_write)
    monkeypatch.setattr(subprocess, "run", call_sudo)
    exec(compile(caller, "<maintained original-context caller>", "exec"), {})
    assert len(reached) == 1 and not (envelope.root / "receipts").exists()
    assert not envelope.launched and not envelope.servers


@pytest.mark.parametrize("failure", ["missing", "file", "alias", "foreign", "unreadable", "0755", "0777", "1700"])
def test_actual_parent_refuses_root_custody_before_first_effect(envelope, monkeypatch, failure):
    root = envelope.root
    previous = root / "previous-evidence"
    previous.write_bytes(b"preserve original evidence")
    preserved = root
    if failure in ("missing", "file"):
        preserved = root.with_name("preserved-root")
        root.rename(preserved)
        if failure == "file":
            root.write_bytes(b"not a directory")
    elif failure == "alias":
        alias = root.with_name("alias")
        alias.symlink_to(root, target_is_directory=True)
        argv = list(sys.argv)
        argv[argv.index("--root") + 1] = str(alias)
        monkeypatch.setattr(sys, "argv", argv)
    elif failure == "foreign":
        original = StorageContext.capture()
        caller = original.uid + 10000
        context = StorageContext(caller, original.homes, original.protected, original.environment_sha256)
        monkeypatch.setenv("SUDO_UID", str(caller))
        monkeypatch.setattr(namespace, "sudo_storage_context", lambda *_: context)
    elif failure[0].isdigit():
        root.chmod(int(failure, 8))
    before = preserved.stat()
    opened = []
    real_open = os.open

    def opening(path, *args, **kwargs):
        if path == root and failure == "unreadable":
            raise PermissionError("finite root-open denial")
        fd = real_open(path, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "mkdir", lambda *_a, **_kw: pytest.fail("First allocation reached."))
    monkeypatch.setattr(os, "fchmod", lambda *_: pytest.fail("Supplied root repaired."))
    with pytest.raises((ValueError, OSError)):
        namespace.main()
    for fd in opened:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF
    after = preserved.stat()
    assert (before.st_ino, before.st_uid, before.st_mode) == (after.st_ino, after.st_uid, after.st_mode)
    assert (preserved / "previous-evidence").read_bytes() == b"preserve original evidence"
    assert not (preserved / "receipts").exists() and not (preserved / "runs").exists()
    assert not envelope.launched and not envelope.servers


@pytest.mark.parametrize("seam", ["root-open", "after-receipts", "after-output", "before-launch"])
def test_root_replacement_never_redirects_allocation_or_cleanup(envelope, monkeypatch, seam):
    root = envelope.root
    preserved = root.with_name("preserved-root")
    (root / "previous-evidence").write_bytes(b"original")
    opened = []
    real_open = os.open

    def replace():
        root.rename(preserved)
        root.mkdir(mode=0o700)
        (root / "replacement-evidence").write_bytes(b"replacement untouched")

    def opening(path, *args, **kwargs):
        if path == root and seam == "root-open":
            replace()
        fd = real_open(path, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", opening)
    receipts, output, sentinel = namespace.open_receipts, namespace.allocate_output, namespace.Sentinel

    def receive(args, stack, fd):
        result = receipts(args, stack, fd)
        if seam == "after-receipts":
            replace()
        return result

    def allocate(args, stack, fd):
        result = output(args, stack, fd)
        if seam == "after-output":
            replace()
        return result

    def listener(family):
        result = sentinel(family)
        if seam == "before-launch" and family == socket.AF_INET6:
            replace()
        return result

    monkeypatch.setattr(namespace, "open_receipts", receive)
    monkeypatch.setattr(namespace, "allocate_output", allocate)
    monkeypatch.setattr(namespace, "Sentinel", listener)
    with pytest.raises(ValueError, match="scratch"):
        namespace.main()
    for fd in opened:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF
    assert list(root.iterdir()) == [root / "replacement-evidence"]
    assert (root / "replacement-evidence").read_bytes() == b"replacement untouched"
    assert (preserved / "previous-evidence").read_bytes() == b"original"
    assert not list(preserved.glob("rootfs-*"))
    assert not envelope.launched and all(server.closed for server in envelope.servers)
    if seam == "root-open":
        assert not (preserved / "receipts").exists()
    else:
        result = json.loads((preserved / "receipts" / envelope.receipt).read_text())
        assert result["status"] == "failed" and result["failure"] == "ValueError"
        assert result["temporary_rootfs_removed"] and not result["cleanup_errors"]
        assert not list((preserved / "receipts").glob("*.control"))
        assert not list((preserved / "receipts").glob("*.preflight"))
    if seam in ("root-open", "after-receipts"):
        assert not (preserved / "runs").exists()


def test_successful_parent_holds_one_caller_root_descriptor_for_all_allocations(envelope, monkeypatch):
    real_open, real_mkdir = os.open, os.mkdir
    root_fds, effects = [], []
    identity = envelope.root.stat()

    def opening(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if path == envelope.root:
            root_fds.append(fd)
        return fd

    def mkdir(path, *args, **kwargs):
        if path in ("receipts", "runs") or str(path).startswith("rootfs-"):
            fd = kwargs["dir_fd"]
            info = os.fstat(fd)
            if str(path).startswith("rootfs-"):
                assert fd != root_fds[0]
                control = (envelope.root / "receipts").stat()
                assert (info.st_dev, info.st_ino) == (control.st_dev, control.st_ino)
                assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o750
            else:
                assert root_fds == [fd]
                assert (info.st_dev, info.st_ino, info.st_uid, info.st_mode) == (
                    identity.st_dev, identity.st_ino, identity.st_uid, identity.st_mode,
                )
            effects.append(str(path).split("-")[0])
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "mkdir", mkdir)
    with pytest.raises(SystemExit) as result:
        namespace.main()
    assert result.value.code == 0 and effects == ["receipts", "runs", "rootfs"]
    with pytest.raises(OSError) as closed:
        os.fstat(root_fds[0])
    assert closed.value.errno == errno.EBADF
    assert read_receipt(envelope)["status"] == "passed"
    assert_cleanup(envelope)


def test_actual_emitted_filter_decode_excludes_foreign_abi_and_three_keyring_syscalls():
    """Decode the actual bytes, not an independent production policy predicate."""
    raw = namespace.keyring_program()
    assert len(raw) == 88 and hashlib.sha256(raw).hexdigest() == keyring_identity()["program_sha256"]
    instructions = tuple(struct.iter_unpack("<HBBI", raw))

    def evaluate(arch, number):
        pc, accumulator = 0, 0
        for _ in range(len(instructions)):
            code, yes, no, value = instructions[pc]
            if code == 0x20:
                accumulator = {0: number & 0xFFFFFFFF, 4: arch}[value]
                pc += 1
            elif code in (0x15, 0x35):
                condition = accumulator == value if code == 0x15 else accumulator >= value
                pc += 1 + (yes if condition else no)
            elif code == 0x06:
                return value
            else:
                pytest.fail("Unexpected emitted opcode.")
        pytest.fail("Emitted filter did not terminate.")

    count = 0
    for arch in (0xC00000B7, 0x40000028, 0xC000003E, 0, 0xFFFFFFFF):
        for number in (*range(1024), 0x3FFFFFFF, 0x40000000, 0xFFFFFFFF, -1):
            expected = (0x80000000 if arch != 0xC00000B7 or (number & 0xFFFFFFFF) >= 0x40000000
                        else 0x00050001 if number in (217, 218, 219) else 0x7FFF0000)
            assert evaluate(arch, number) == expected
            count += 1
    assert count == 5140


@pytest.mark.parametrize("stage", ["success", "nnp-error", "filter-error", "nnp-readback", "filter-readback",
                                  "bool-result", "bad-program"])
def test_actual_typed_filter_adapter_and_order(monkeypatch, stage):
    calls = []
    monkeypatch.setattr(namespace, "require_native_abi", lambda: None)

    class Prctl:
        def __call__(self, option, a2, a3, a4, a5):
            assert self.argtypes == [ctypes.c_int, *([ctypes.c_ulong] * 4)]
            assert self.restype is ctypes.c_int and (a4, a5) == (0, 0)
            calls.append(option)
            if option == 22:
                program = ctypes.cast(a3, ctypes.POINTER(namespace.SockFprog)).contents
                assert a2 == 2 and program.len == 11
                assert ctypes.string_at(program.filter, 88) == namespace.keyring_program()
            if (stage == "nnp-error" and option == 38) or (stage == "filter-error" and option == 22):
                ctypes.set_errno(errno.EPERM)
                return -1
            if stage == "nnp-readback" and option == 39:
                return 0
            if stage == "filter-readback" and option == 21:
                return 1
            if stage == "bool-result":
                return False
            return {38: 0, 22: 0, 39: 1, 21: 2}[option]

    adapter = Prctl()
    monkeypatch.setattr(ctypes, "CDLL", lambda path, **kwargs: (
        pytest.fail("Unexpected library selection.") if path is not None or kwargs != {"use_errno": True}
        else SimpleNamespace(prctl=adapter)
    ))
    if stage == "bad-program":
        monkeypatch.setattr(namespace, "keyring_program", lambda: b"\0" * 88)
    if stage == "success":
        assert namespace.install_keyring_boundary() == keyring_identity()
        assert calls == [38, 22, 39, 21]
    else:
        with pytest.raises((RuntimeError, OSError)):
            namespace.install_keyring_boundary()
        assert calls == {"nnp-error": [38], "filter-error": [38, 22], "nnp-readback": [38, 22, 39],
                         "filter-readback": [38, 22, 39, 21], "bool-result": [38], "bad-program": []}[stage]


@pytest.mark.parametrize("platform_name,machine,byteorder,width", [
    ("darwin", "arm64", "little", 8), ("linux", "x86_64", "little", 8),
    ("linux", "aarch64", "big", 8), ("linux", "aarch64", "little", 4),
])
def test_unsupported_abi_refuses_before_any_adapter(monkeypatch, platform_name, machine, byteorder, width):
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(namespace.platform, "machine", lambda: machine)
    monkeypatch.setattr(sys, "byteorder", byteorder)
    monkeypatch.setattr(ctypes, "sizeof", lambda _: width)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_kw: pytest.fail("Unsupported ABI reached libc."))
    with pytest.raises(RuntimeError, match="Unsupported"):
        namespace.install_keyring_boundary()
    with pytest.raises(RuntimeError, match="Unsupported"):
        namespace.linux_mount(None, "/", None, 0)


@pytest.mark.parametrize("failure", [False, True])
def test_actual_direct_mount_adapter_signature_and_errno(monkeypatch, failure):
    monkeypatch.setattr(namespace, "require_native_abi", lambda: None)
    calls = []

    class Mount:
        def __call__(self, source, target, filesystem, flags, data):
            assert self.argtypes == [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                     ctypes.c_ulong, ctypes.c_void_p]
            assert self.restype is ctypes.c_int
            calls.append((source, target, filesystem, flags, ctypes.string_at(data)))
            ctypes.set_errno(errno.EACCES if failure else 0)
            return -1 if failure else 0

    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(mount=Mount()))
    if failure:
        with pytest.raises(OSError) as error:
            namespace.linux_mount("tmpfs", "/proc/self/fd/7/唯一", "tmpfs", 6, "mode=0755")
        assert error.value.errno == errno.EACCES
    else:
        namespace.linux_mount("tmpfs", "/proc/self/fd/7/唯一", "tmpfs", 6, "mode=0755")
    assert calls == [(b"tmpfs", "/proc/self/fd/7/唯一".encode(), b"tmpfs", 6, b"mode=0755")]


@pytest.mark.parametrize("name", NAMESPACE_NAMES)
@pytest.mark.parametrize("side", ["outer_namespaces", "namespaces"])
@pytest.mark.parametrize("failure", ["missing", "extra", "same", "integer", "wrong-prefix", "empty", "zero", "suffix"])
def test_complete_namespace_identity_schema_at_actual_consumer(name, side, failure):
    proof = kernel_proof()
    values = proof[side]
    if failure == "missing":
        values.pop(name)
    elif failure == "extra":
        values["user"] = "user:[22]"
    elif failure == "same":
        values[name] = proof["namespaces" if side == "outer_namespaces" else "outer_namespaces"][name]
    else:
        values[name] = {"integer": 2, "wrong-prefix": "user:[2]", "empty": "", "zero": f"{name}:[0]",
                        "suffix": f"{name}:[12]oops"}[failure]
    with pytest.raises(RuntimeError):
        require_namespaces(proof["outer_namespaces"], proof["namespaces"])


@pytest.mark.parametrize("failure", ["missing", "extra", "prefix", "type"])
def test_parent_bad_outer_capture_refuses_before_allocations(envelope, monkeypatch, failure):
    outer = kernel_proof()["outer_namespaces"]
    if failure == "missing":
        outer.pop("ipc")
    elif failure == "extra":
        outer["user"] = "user:[99]"
    else:
        outer["ipc"] = "net:[4]" if failure == "prefix" else 4
    monkeypatch.setattr(namespace, "namespace_ids", lambda: outer)
    monkeypatch.setattr(namespace, "open_receipts", lambda *_: pytest.fail("Allocation before outer validation."))
    with pytest.raises(RuntimeError):
        namespace.main()
    assert not envelope.servers and not (envelope.root / "receipts").exists()


@pytest.mark.parametrize("name", [*namespace.INPUT_NAMES, "output", "build"])
def test_actual_bind_consumes_pin_after_child_and_ancestor_replacement(private_setup, monkeypatch, name):
    state = private_setup
    path = state.paths[name]
    original = state.args.handles[name]["identity"]
    path.rename(path.with_name(path.name + "-preserved"))
    if name == "go_archive":
        path.write_bytes(b"replacement archive")
    else:
        path.mkdir()
        (path / "replacement").write_bytes(b"untouched")
    ancestor = state.args.root
    preserved = ancestor.with_name("preserved-task")
    ancestor.rename(preserved)
    ancestor.mkdir()
    (ancestor / "replacement-evidence").write_bytes(b"untouched ancestor")
    # The HOST view simulator must follow its own task-only preserved directory.
    state.paths["rootfs"] = preserved / "control/rootfs-test"
    state.saved = preserved / "control/preserved-underlay"
    # This test stops at the exact source-consuming bind, before any real mount.
    def mount(source, target, filesystem, flags, data=None):
        if filesystem == "tmpfs":
            state.paths["rootfs"].rename(state.saved)
            state.paths["rootfs"].mkdir(mode=0o755)
            state.paths["rootfs"].chmod(0o755)
        if source == namespace.fd_path(state.args.handles[name]["fd"]) and flags == namespace.MS_BIND:
            assert namespace.descriptor_identity(os.fstat(state.args.handles[name]["fd"])) == original
            assert target.startswith("/proc/self/fd/") and target.endswith("/" + path.name)
            raise LookupError("finite stop at actual retained bind source")
    monkeypatch.setattr(namespace, "linux_mount", mount)
    with pytest.raises(LookupError, match="retained bind source"):
        namespace.inside(state.args, state.outer)
    assert (ancestor / "replacement-evidence").read_bytes() == b"untouched ancestor"
    replaced = preserved / path.name
    assert (replaced.read_bytes() if name == "go_archive" else (replaced / "replacement").read_bytes()) == (
        b"replacement archive" if name == "go_archive" else b"untouched"
    )
    for _, record in state.descriptors:
        with pytest.raises(OSError):
            os.fstat(record["fd"])
    with pytest.raises(OSError):
        os.fstat(state.args.proof_fd)


@pytest.mark.parametrize("stage", ["namespace", "context", "missing-handle", "aliased-handle", "identity",
                                  "mount", "overmount", "filter"])
def test_actual_inside_partial_failure_closes_all_received_handles(private_setup, monkeypatch, stage):
    state = private_setup
    if stage == "namespace":
        state.outer.pop("ipc")
    elif stage == "context":
        state.args.storage_context["uid"] += 10000
    elif stage == "missing-handle":
        # Preserve the FD in another received record so closure remains observable.
        state.args.handles["unexpected"] = state.args.handles.pop("state")
    elif stage == "aliased-handle":
        state.args.system_mounts = [{"resource": state.args.handles["state"]}]
    elif stage == "identity":
        state.args.handles["source"]["identity"]["ino"] += 1
    elif stage == "mount":
        monkeypatch.setattr(namespace, "linux_mount", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("finite mount failure")))
    elif stage == "overmount":
        monkeypatch.setattr(namespace, "linux_mount", lambda *_a, **_kw: None)
    else:
        monkeypatch.setattr(namespace, "install_keyring_boundary",
                            lambda: (_ for _ in ()).throw(RuntimeError("finite filter failure")))
    monkeypatch.setattr(namespace, "run_candidate", lambda *_: pytest.fail("Candidate after setup failure."))
    with pytest.raises((RuntimeError, ValueError, OSError)):
        namespace.inside(state.args, state.outer)
    for _, record in state.descriptors:
        with pytest.raises(OSError):
            os.fstat(record["fd"])
    with pytest.raises(OSError):
        os.fstat(state.args.proof_fd)
    marker = state.paths["rootfs"] / "run/avibe-engine-test-isolation.json"
    if marker.exists():
        assert marker.read_bytes() == b""


@pytest.mark.parametrize("kind", ["directory", "file", "device"])
@pytest.mark.parametrize("failure", ["none", "wrong-type", "symlink", "missing", "unreadable", "ancestor-alias"])
def test_actual_component_pins_type_flags_and_partial_closure(tmp_path, monkeypatch, kind, failure):
    root = tmp_path / "root"
    parent = root / "a"
    parent.mkdir(parents=True)
    target = parent / "resource"
    if kind == "directory" and failure != "wrong-type":
        target.mkdir()
    else:
        target.write_bytes(b"synthetic regular backing, never a device")
    if failure == "symlink":
        original = target.with_name("preserved")
        target.rename(original)
        target.symlink_to(original)
    elif failure == "missing":
        target.rename(target.with_name("preserved"))
    elif failure == "ancestor-alias":
        parent.rename(root / "preserved")
        parent.symlink_to(root / "preserved")
    real_open, real_fstat = os.open, os.fstat
    opened, devices = [], set()
    def opening(path, flags, *args, **kwargs):
        assert flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC
        if path == "resource" and failure == "unreadable":
            raise PermissionError("finite consuming open failure")
        is_device = bool(flags & 0o10000000)
        if is_device:
            assert kind == "device"
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        if is_device and failure != "wrong-type":
            devices.add(fd)
        return fd
    def info(fd):
        value = real_fstat(fd)
        return stat_fields(value, st_mode=stat.S_IFCHR | 0o600, st_rdev=os.makedev(1, 3)) if fd in devices else value
    with ExitStack() as stack:
        root_fd = _directory_fd(stack, root)
        monkeypatch.setattr(namespace, "require_native_abi", lambda: None)
        monkeypatch.setattr(os, "open", opening)
        monkeypatch.setattr(os, "fstat", info)
        if failure == "none":
            record = namespace.pin_beneath(stack, root_fd, Path("a/resource"), kind=kind)
            assert namespace.check_handle(record) == record["fd"]
        else:
            # A regular directory supplies the wrong type for a regular-file pin.
            if kind == "file" and failure == "wrong-type":
                target.unlink()
                target.mkdir()
            with pytest.raises((OSError, ValueError)):
                namespace.pin_beneath(stack, root_fd, Path("a/resource"), kind=kind)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("different_inherited_gid", [False, True])
def test_rootfs_pin_failure_cleans_only_the_original_empty_control_child(envelope, monkeypatch, different_inherited_gid):
    raw = envelope.raw_lstat(envelope.paths["source"])
    if different_inherited_gid:
        monkeypatch.setenv("SUDO_GID", str(raw.st_gid + 1))
        assert int(os.environ["SUDO_GID"]) > 0 and int(os.environ["SUDO_GID"]) != raw.st_gid
    reached = []
    original = namespace.pin_beneath
    def pin(stack, fd, path, **kwargs):
        if path.name.startswith("rootfs-"):
            reached.append(path.name)
            raise PermissionError("finite rootfs pin denial")
        return original(stack, fd, path, **kwargs)
    monkeypatch.setattr(namespace, "pin_beneath", pin)
    with pytest.raises(PermissionError, match="rootfs pin denial"):
        namespace.main()
    result = read_receipt(envelope)
    assert result["status"] == "failed" and result["temporary_rootfs_removed"] and not result["cleanup_errors"]
    assert len(reached) == 1 and result["failure"] == "PermissionError"
    assert envelope.paths["source"].lstat() == raw
    assert envelope.raw_lstat(envelope.paths["source"]) == raw
    assert not list((envelope.root / "receipts").glob("rootfs-*"))
    assert not envelope.servers and not envelope.launched


@pytest.mark.parametrize("failure", ["none", "wrong-identity", "replacement", "nonempty"])
def test_exact_rootfs_cleanup_preserves_replacements_and_nonempty_evidence(tmp_path, failure):
    root = tmp_path / "control"
    root.mkdir()
    target = root / "rootfs-test"
    target.mkdir(mode=0o700)
    with ExitStack() as stack:
        fd = _directory_fd(stack, root)
        record = namespace.pin_beneath(stack, fd, Path(target.name), kind="directory")
        if failure == "wrong-identity":
            record["identity"]["ino"] += 1
        elif failure == "replacement":
            target.rename(root / "preserved")
            target.mkdir()
        if failure in ("replacement", "nonempty"):
            (target / "evidence").write_bytes(b"preserve")
        if failure == "none":
            namespace.remove_rootfs(namespace.retained_handle(fd, "directory"), record, target.name)
            assert not target.exists()
        else:
            with pytest.raises((RuntimeError, OSError)):
                namespace.remove_rootfs(namespace.retained_handle(fd, "directory"), record, target.name)
            assert target.exists()
            if failure in ("replacement", "nonempty"):
                assert (target / "evidence").read_bytes() == b"preserve"


@pytest.mark.parametrize("failure", [False, True])
def test_selected_build_consumes_exact_output_identity_from_parent_receipt(envelope, failure):
    control = envelope.root / "receipts"
    control.mkdir(mode=0o750)
    envelope.parent_owned(control)
    build = envelope.root / "runs" / "old-build.json"
    build.mkdir(parents=True)
    with ExitStack() as stack:
        fd = _directory_fd(stack, control)
        build_fd = _directory_fd(stack, build)
        record = namespace.retained_handle(build_fd, "directory")
        value = {"status": "passed", "phase": "build", "output": str(build),
                 "output_identity": {**record["identity"], "ino": record["identity"]["ino"] + int(failure)}}
        receipt = control / build.name
        receipt.write_text(json.dumps(value))
        receipt.chmod(0o640)
        envelope.parent_owned(receipt)
        if failure:
            with pytest.raises(RuntimeError, match="directory does not match"):
                namespace.selected_build_receipt(SimpleNamespace(build=build), fd, record)
        else:
            assert namespace.selected_build_receipt(SimpleNamespace(build=build), fd, record)["output_identity"] == record["identity"]


@pytest.mark.parametrize("case", ["success", "pathname-replacement", "same-inode-write", "wrong-hash",
                                 "missing-fd", "missing-module", "named-control", "oversized-control"])
def test_actual_host_exec_inheritance_and_fixed_trampoline_bytes(tmp_path, case):
    """Real unprivileged HOST exec only; finite control-UID metadata adaptation."""
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    source = {
        "budgets": "VALUE = 'trusted-budget'\n",
        "isolation": "from budgets import VALUE\n",
    }
    source["namespace"] = (
        "import json, os\nfrom isolation import VALUE\n"
        "def child_from_control(value):\n"
        " for fd in value['closed_sources']:\n"
        "  try: os.fstat(fd)\n"
        "  except OSError: pass\n"
        "  else: raise RuntimeError('bootstrap descriptor survived import')\n"
        " print(json.dumps({'value': VALUE, 'fixed_import': True}))\n"
    )
    for name, text in source.items():
        (recipe / (name + ".py")).write_text(text)
    with ExitStack() as stack:
        recipe_fd = _directory_fd(stack, recipe)
        pinned = namespace.pin_bootstrap(stack, namespace.retained_handle(recipe_fd, "directory"))
        control = stack.enter_context(
            (tmp_path / "named-control").open("w+b") if case == "named-control"
            else tempfile.TemporaryFile(dir=tmp_path),
        )
        os.fchmod(control.fileno(), 0o600)
        values = {"recipe": str(recipe), "bootstrap": pinned, "closed_sources": [item["fd"] for item in pinned.values()]}
        if case == "pathname-replacement":
            (recipe / "budgets.py").rename(recipe / "preserved.py")
            (recipe / "budgets.py").write_text("raise RuntimeError('replacement must not import')\n")
        elif case == "same-inode-write":
            (recipe / "budgets.py").write_text("raise RuntimeError('changed content must not import')\n")
        elif case == "wrong-hash":
            pinned["isolation"]["sha256"] = "0" * 64
        elif case == "missing-module":
            pinned.pop("isolation")
        data = json.dumps(values).encode() if case != "oversized-control" else b"x" * 65537
        control.write(data)
        control.flush()
        control.seek(0)
        # No privilege change: the trusted HOST test shim changes only the
        # returned UID metadata for this one task-owned unnamed control file.
        adaptation = (
            "import os, sys, types\n"
            "original_fstat=os.fstat\n"
            "def host_fstat(fd):\n"
            " info=original_fstat(fd)\n"
            " if fd!=int(sys.argv[1]): return info\n"
            " fields={key:getattr(info,key) for key in dir(info) if key.startswith('st_')}\n"
            " fields['st_uid']=0\n"
            " return types.SimpleNamespace(**fields)\n"
            "os.fstat=host_fstat\n"
        )
        passed = [control.fileno(), *(item["fd"] for item in pinned.values())]
        if case == "missing-fd":
            passed.remove(pinned["isolation"]["fd"])
        command = [sys.executable, "-I", "-B", "-c", adaptation + namespace.CHILD_TRAMPOLINE, str(control.fileno())]
        environment = {name: str(tmp_path / ("child-" + name)) for name in (*STORAGE_ENV, "TMPDIR")}
        for value in environment.values():
            Path(value).mkdir()
        environment.update(PATH="/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(command, cwd=tmp_path, env=environment, close_fds=True,
                                pass_fds=tuple(passed), capture_output=True, timeout=10)
        (tmp_path / "child-command.json").write_text(json.dumps({
            "argv": command, "cwd": str(tmp_path), "environment": environment, "pass_fds": passed,
            "timeout": 10, "close_fds": True, "qualification": "HOST-only control UID metadata seam; no Linux effect",
        }, indent=2))
        (tmp_path / "child.stdout").write_bytes(result.stdout)
        (tmp_path / "child.stderr").write_bytes(result.stderr)
        (tmp_path / "child.exit").write_text(str(result.returncode) + "\n")
    if case in ("success", "pathname-replacement"):
        assert result.returncode == 0 and result.stderr == b""
        assert json.loads(result.stdout) == {"value": "trusted-budget", "fixed_import": True}
    else:
        assert result.returncode != 0 and result.stdout == b""
        assert b"replacement must not import" not in result.stderr
        assert b"changed content must not import" not in result.stderr


@pytest.mark.parametrize("shape", ["directories", "relative-aliases", "absolute-aliases"])
@pytest.mark.parametrize("failure", [None, "zero-bind"])
def test_connected_parent_fixed_bootstrap_inside_probe_and_public_receipt(
        envelope, installed_system, monkeypatch, shape, failure):
    """Actual producer/consumers, task-only FD/view adapters; no OS acceptance."""
    import builtins
    from contextlib import redirect_stdout
    import io
    import isolation_probe

    installed_system.layout(shape)
    envelope.phase = "build"
    argv = list(sys.argv)
    argv[argv.index("--phase") + 1] = "build"
    argv[argv.index("--network") + 1] = "none"
    if shape == "absolute-aliases":
        # Non-ASCII input and output labels reach the same parent/private seam.
        source = envelope.paths["source"].with_name("source-输入")
        envelope.paths["source"].rename(source)
        envelope.paths["source"] = source
        argv[argv.index("--source") + 1] = str(source)
        envelope.receipt = "运行-envelope.json"
        argv[argv.index("--receipt") + 1] = envelope.receipt
    monkeypatch.setattr(sys, "argv", argv)
    events, child_fds = [], []
    real_exec, real_fstat = builtins.exec, os.fstat
    real_exists, real_read_text, real_write = Path.exists, Path.read_text, Path.write_bytes
    actual_uid = os.getuid()

    def launch(command, control, kwargs):
        assert installed_system.calls == 1
        assert control["system_mounts"] == installed_system.mounts
        assert control["system_aliases"] == installed_system.aliases
        expected_directories = ["/usr/bin", "/usr/sbin", "/usr/lib", "/usr/share"]
        expected_aliases = {}
        if shape == "directories":
            expected_directories += ["/bin", "/sbin", "/lib", "/lib64"]
        else:
            expected_aliases = {name: ("/" if shape == "absolute-aliases" else "") + "usr/" + name
                                for name in ("bin", "sbin", "lib", "lib64")}
        assert control["system_aliases"] == expected_aliases
        assert [item["path"] for item in control["system_mounts"]] == [*expected_directories, "/dev/null", "/dev/zero"]
        assert installed_system.device_opens == ["null", "zero"]
        for item in control["system_mounts"]:
            resource = item["resource"]
            assert resource["identity"]["uid"] == 0
            if item["path"] in ("/dev/null", "/dev/zero"):
                assert resource["kind"] == "device" and item["writable"] is True
                assert stat.S_ISCHR(resource["identity"]["mode"])
                assert (os.major(resource["identity"]["rdev"]), os.minor(resource["identity"]["rdev"])) == (
                    1, 3 if item["path"] == "/dev/null" else 5,
                )
            else:
                assert resource["kind"] == "directory" and item["writable"] is False
                assert stat.S_ISDIR(resource["identity"]["mode"])
        # In-process HOST simulation duplicates the exact passed handles to
        # preserve the real parent's ownership. Numeric-FD translation is the
        # only handoff adaptation; real exec inheritance is tested separately.
        translated = json.loads(json.dumps(control))
        mapping = {fd: os.dup(fd) for fd in kwargs["pass_fds"] if fd != int(command[-1])}
        child_fds.extend(mapping.values())
        for record in (*translated["handles"].values(), *translated["bootstrap"].values(),
                       *(item["resource"] for item in translated["system_mounts"])):
            record["fd"] = mapping[record["fd"]]
        translated["proof_fd"] = mapping[translated["proof_fd"]]
        rootfs = Path(control["rootfs"])
        underlay = rootfs.with_name(rootfs.name + "-underlay")
        marker = rootfs / "run/avibe-engine-test-isolation.json"
        expected_mounts = [*translated["system_mounts"], *(
            {"path": control[name], "resource": translated["handles"][name],
             "writable": name in ("state", "output")}
            for name in (*namespace.INPUT_NAMES, "output")
        )]
        bound, remounted = [], []
        with tempfile.TemporaryFile(dir=envelope.root) as handoff, monkeypatch.context() as child:
            handoff.write(json.dumps(translated).encode())
            handoff.flush()
            handoff.seek(0)
            control_fd = os.dup(handoff.fileno())
            control_identity = real_fstat(control_fd).st_ino
            child.setattr(sys, "argv", ["-c", str(control_fd)])
            for name in ("budgets", "isolation", "namespace"):
                child.setitem(sys.modules, name, sys.modules[name])

            def fstat(fd):
                info = real_fstat(fd)
                if info.st_ino == control_identity or (marker.exists() and info.st_ino == marker.stat().st_ino):
                    return stat_fields(info, st_uid=0)
                return info

            def mount(source, target, filesystem, flags, data=None):
                events.append(("mount", source, target, flags))
                if filesystem == "tmpfs":
                    rootfs.rename(underlay)
                    rootfs.mkdir(mode=0o755)
                    rootfs.chmod(0o755)
                if flags == namespace.MS_BIND:
                    assert marker.exists() and marker.read_bytes() == b""
                    source_fd = int(source.rsplit("/", 1)[1])
                    assert source_fd in mapping.values()
                    os.fstat(source_fd)
                    item = expected_mounts[len(bound)]
                    assert source_fd == item["resource"]["fd"]
                    relative = Path(item["path"]).relative_to("/")
                    parent_fd = int(target.split("/")[-2])
                    parent_info = os.fstat(parent_fd)
                    expected_parent = (rootfs / relative.parent).stat()
                    assert (parent_info.st_dev, parent_info.st_ino) == (expected_parent.st_dev, expected_parent.st_ino)
                    assert target.endswith("/" + relative.name)
                    bound.append((item, target))
                    if failure == "zero-bind" and item["path"] == "/dev/zero":
                        raise OSError(errno.EPERM, "finite task-only zero bind refusal")
                elif flags & namespace.MS_BIND and flags & namespace.MS_REMOUNT:
                    item, expected_target = bound[len(remounted)]
                    expected_flags = namespace.MS_BIND | namespace.MS_REMOUNT | namespace.MS_NOSUID
                    if item["resource"]["kind"] != "device":
                        expected_flags |= namespace.MS_NODEV
                    if not item["writable"]:
                        expected_flags |= namespace.MS_RDONLY
                    assert source is None and filesystem is None and target == expected_target
                    assert flags == expected_flags
                    remounted.append(item)
                if flags == namespace.MS_REMOUNT | namespace.MS_RDONLY | namespace.MS_NOSUID | namespace.MS_NODEV:
                    assert json.loads(marker.read_text())["keyring_boundary"] == keyring_identity()
                    events.append(("immutable-marker",))

            def execute(code, globals=None, locals=None, **options):
                result = real_exec(code, globals, locals, **options)
                if globals is not None and globals.get("__name__") == "namespace":
                    module = sys.modules["namespace"]
                    child.setattr(module, "namespace_ids", lambda: kernel_proof()["namespaces"])
                    child.setattr(module, "linux_mount", mount)
                    child.setattr(module, "install_keyring_boundary",
                                  lambda: events.append(("filter",)) or keyring_identity())
                    child.setattr(sys.modules["isolation"], "PROOF_PATH", marker)
                return result

            def read_text(path, *args, **kwargs):
                if str(path) == "/proc/self/status":
                    return "\n".join(f"{name}: {value}" for name, value in kernel_proof()["process_status"].items())
                if str(path) == "/proc/self/mountinfo":
                    return ""
                return real_read_text(path, *args, **kwargs)

            def exists(path):
                if str(path) in ("/Users", "/home", "/root", "/sys", "/run/netns"):
                    return False
                return real_exists(path)

            def write(path, value):
                if path.name == ".isolation-write-probe":
                    raise PermissionError("finite readonly bind seam")
                return real_write(path, value)

            def run(command, **options):
                assert options["close_fds"] and "pass_fds" not in options
                assert command[0] == "/usr/bin/setpriv"
                assert "--clear-groups" in command and "--bounding-set=-all" in command
                for fd in mapping.values():
                    if fd != translated["proof_fd"]:
                        with pytest.raises(OSError):
                            os.fstat(fd)
                if command[-1].endswith("isolation_probe.py"):
                    events.append(("preflight",))
                    child.setattr(isolation_probe, "namespace_receipt", sys.modules["isolation"].namespace_receipt)
                    output = io.StringIO()
                    with redirect_stdout(output):
                        isolation_probe.main()
                    return subprocess.CompletedProcess(command, 0, stdout=output.getvalue().encode())
                assert command[-1] == "candidate-test"
                with pytest.raises(OSError):
                    os.fstat(translated["proof_fd"])
                events.append(("candidate",))
                return subprocess.CompletedProcess(command, 0)

            child.setattr(os, "fstat", fstat)
            child.setattr(builtins, "exec", execute)
            child.setattr(Path, "read_text", read_text)
            child.setattr(Path, "exists", exists)
            child.setattr(Path, "write_bytes", write)
            original_readlink = os.readlink
            child.setattr(os, "readlink", lambda path, *a, **kw: (
                kernel_proof()["namespaces"][Path(path).name] if str(path).startswith("/proc/self/ns/")
                else original_readlink(path, *a, **kw)
            ))
            child.setattr(namespace.subprocess, "check_output", lambda *_a, **_kw: b'[{"ifname":"lo"}]')
            child.setattr(namespace.subprocess, "run", run)
            child.setattr(os, "fchdir", lambda fd: events.append(("new-view", os.fstat(fd).st_ino)))
            child.setattr(os, "chroot", lambda path: (events.append(("chroot", path)),
                                                     child.setattr(os, "geteuid", lambda: actual_uid)))
            child.setattr(os, "chdir", lambda path: events.append(("chdir", path)))
            child.setattr(os, "closerange", lambda *_: None)
            child.setattr(isolation_probe, "probe_blocked_connection", lambda network, host, port, kind: {
                "host": host, "kind": kind, "blocked": True, "errno": errno.ECONNREFUSED,
            })
            if failure is None:
                with pytest.raises(SystemExit) as result:
                    real_exec(compile(namespace.CHILD_TRAMPOLINE, "<actual connected trampoline>", "exec"), {})
                assert result.value.code == 0
                assert events.index(("filter",)) < events.index(("immutable-marker",)) < events.index(("preflight",))
                assert events.index(("preflight",)) < events.index(("candidate",))
                assert next(event[1] for event in events if event[0] == "new-view") != underlay.stat().st_ino
                assert remounted == expected_mounts
                assert [item["path"] for item in remounted if item["writable"] and item["resource"]["kind"] == "directory"] == [
                    control["state"], control["output"],
                ]
            else:
                with pytest.raises(OSError, match="finite task-only zero bind refusal") as refused:
                    real_exec(compile(namespace.CHILD_TRAMPOLINE, "<actual connected trampoline>", "exec"), {})
                assert refused.value.errno == errno.EPERM
                assert [item["path"] for item, _ in bound] == [*expected_directories, "/dev/null", "/dev/zero"]
                assert [item["path"] for item in remounted] == [*expected_directories, "/dev/null"]
                assert not any(event[0] in ("filter", "immutable-marker", "preflight", "candidate") for event in events)
            for fd in mapping.values():
                with pytest.raises(OSError) as closed:
                    os.fstat(fd)
                assert closed.value.errno == errno.EBADF
            for name, target in expected_aliases.items():
                assert os.readlink(rootfs / name) == target
            assert not (rootfs / "dev/random").exists() and not (rootfs / "dev/urandom").exists()
            rootfs.rename(rootfs.with_name(rootfs.name + "-preserved-view"))
            underlay.rename(rootfs)
        return subprocess.CompletedProcess(command, 0 if failure is None else 1)

    envelope.launch = launch
    if failure is None:
        with pytest.raises(SystemExit) as result:
            namespace.main()
        assert result.value.code == 0
    else:
        with pytest.raises(RuntimeError, match="Namespace execution or preflight/cleanup/sentinel acceptance failed"):
            namespace.main()
    receipt = read_receipt(envelope)
    assert receipt["status"] == ("passed" if failure is None else "failed")
    assert receipt["temporary_rootfs_removed"] and not receipt["cleanup_errors"]
    if failure is None:
        assert receipt["probe"]["keyring_boundary"] == keyring_identity()
        assert receipt["probe"]["namespaces"] == kernel_proof()["namespaces"]
        assert receipt["probe"]["outer_namespaces"] == kernel_proof()["outer_namespaces"]
        assert receipt["probe"]["isolation_probe"] == "pass"
        assert "storage_context" not in receipt["probe"]
    else:
        assert receipt["failure"] is None and receipt["exit_code"] == 1
        assert receipt["preflight"] == "missing-or-invalid" and "probe" not in receipt
    assert receipt["output_identity"]["ino"] == (envelope.root / "runs" / envelope.receipt).stat().st_ino
    for fd in child_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    for fd in installed_system.opened:
        with pytest.raises(OSError) as closed:
            envelope.raw_fstat(fd)
        assert closed.value.errno == errno.EBADF
