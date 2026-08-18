from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

import core.memory.process as memory_process
import core.memory.sync_process as memory_sync_process
from core.memory.process import _MemoryChildRole, _ProcessIdentity, _ProcessKind, _SystemProcessHost
from core.memory.sync_process import (
    SYNC_ARGV,
    SYNC_NONCE_ENV,
    SYNC_PARENT_CREATE_TIME_ENV,
    SYNC_PARENT_PID_ENV,
    SYNC_PARENT_UID_ENV,
    SYNC_ROLE,
    SyncOwnership,
    SyncOwnershipError,
    EverOSSyncProcess,
    SyncProcessResult,
    _ParentIdentity,
    _sync_environment,
    sync_record_path,
)


class _Host:
    def __init__(self, identities: dict[int, _ProcessIdentity] | None = None) -> None:
        self.identities = identities or {}
        self.signals: list[tuple[dict[int, float], int, int | None]] = []

    def inspect_identity(self, pid: int):
        return self.identities.get(pid)

    def live(self, identities):
        return {pid: created for pid, created in identities.items() if pid in self.identities}

    def recorded_group_members(self, process_group, *, socket_path, provider_root, role=None):
        del socket_path, provider_root
        assert role is _MemoryChildRole.CASCADE_SYNC
        return {
            pid: float(identity.stamp)
            for pid, identity in self.identities.items()
            if identity.stamp is not None
        }, []

    def signal(self, identities, signum, *, process_group=None, process=None):
        del process
        self.signals.append((dict(identities), signum, process_group))
        for pid in identities:
            self.identities.pop(pid, None)

    async def wait_for_exit(self, identities, timeout_seconds, **_kwargs):
        del timeout_seconds
        return not self.live(identities)


def _record(root: Path, *, state: str, pid: int | None = None) -> dict[str, object]:
    uid = os.getuid() if hasattr(os, "getuid") else None
    python = root.parent / "runtime" / "bin" / "python"
    return {
        "state": state,
        "nonce": "a" * 64,
        "pid": pid,
        "create_time": 10.5 if pid is not None else None,
        "process_group": pid,
        "parent_pid": 99,
        "parent_create_time": 8.25,
        "parent_uid": uid,
        "provider_root": str(root),
        "socket_path": str(root.parent / ".rt" / "everos.sock"),
        "role": SYNC_ROLE,
        "argv": [str(python), *SYNC_ARGV],
    }


async def _hold_rebuild_lock(provider_root: Path) -> asyncio.subprocess.Process:
    lock_path = memory_process._provider_rebuild_lock_path(provider_root=provider_root)
    lock_path.parent.mkdir(mode=0o700)
    script = "\n".join(
        (
            "import fcntl",
            "import os",
            "import sys",
            "descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)",
            "fcntl.flock(descriptor, fcntl.LOCK_EX)",
            "print('locked', flush=True)",
            "sys.stdin.read(1)",
            "fcntl.flock(descriptor, fcntl.LOCK_UN)",
            "os.close(descriptor)",
        )
    )
    locker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(lock_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert locker.stdout is not None
    assert await locker.stdout.readline() == b"locked\n"
    return locker


async def _release_rebuild_lock(locker: asyncio.subprocess.Process) -> None:
    assert locker.stdin is not None
    locker.stdin.write(b"\n")
    await locker.stdin.drain()
    locker.stdin.close()
    assert await locker.wait() == 0


async def test_pending_sync_record_fails_closed_without_touching_sidecar(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    host = _Host()
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(_record(root, state="pending"))
    sidecar = memory_dir / ".rt" / "everos.sidecar.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("sidecar-owned", encoding="ascii")

    with pytest.raises(SyncOwnershipError, match="pending"):
        await ownership.reconcile()

    assert ownership.path.exists()
    assert sidecar.read_text(encoding="ascii") == "sidecar-owned"


def test_two_launchers_have_one_atomic_nonce_winner(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=_Host())
    first = _record(root, state="pending")
    second = {**first, "nonce": "b" * 64}

    ownership.claim(first)
    with pytest.raises(SyncOwnershipError, match="claimed"):
        ownership.claim(second)
    assert ownership.read()["nonce"] == first["nonce"]


def test_nonce_clobber_is_rejected_for_update_and_remove(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=_Host())
    first = _record(root, state="pending")
    ownership.claim(first)

    with pytest.raises(SyncOwnershipError, match="claimed"):
        ownership.write({**first, "nonce": "b" * 64})
    with pytest.raises(SyncOwnershipError, match="changed"):
        ownership.remove(nonce="b" * 64)
    assert ownership.read()["nonce"] == first["nonce"]


async def test_pending_record_is_retired_only_after_exact_discovery_finds_no_child(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"

    class DiscoveryHost(_Host):
        def find_syncs(self, *, provider_root, python, nonce):
            assert provider_root == root
            assert python.is_absolute()
            assert nonce == "a" * 64
            return {}

    ownership = SyncOwnership(
        sync_record_path(memory_dir),
        provider_root=root,
        host=DiscoveryHost(),
    )
    ownership.write(_record(root, state="pending"))

    await ownership.reconcile()

    assert not ownership.path.exists()


async def test_pending_record_is_preserved_when_discovery_cannot_inspect_a_child(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"

    class UnverifiableDiscoveryHost(_Host):
        def find_syncs(self, *, provider_root, python, nonce):
            del provider_root, python, nonce
            raise RuntimeError("sync child command line could not be verified")

    ownership = SyncOwnership(
        sync_record_path(memory_dir),
        provider_root=root,
        host=UnverifiableDiscoveryHost(),
    )
    ownership.write(_record(root, state="pending"))

    with pytest.raises(SyncOwnershipError, match="could not be verified"):
        await ownership.reconcile()

    assert ownership.path.exists()


@pytest.mark.parametrize("socket_path", [None, 451])
async def test_sync_run_fails_closed_for_a_malformed_record_socket_path(
    tmp_path: Path,
    socket_path: object,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    sync = EverOSSyncProcess(python, effective_home=tmp_path / "home", _host=_Host())
    record = _record(sync.provider_root, state="finalized", pid=451)
    if socket_path is None:
        record.pop("socket_path")
    else:
        record["socket_path"] = socket_path
    sync._ownership.write(record)

    assert await sync.run() is SyncProcessResult.ALREADY_RUNNING
    assert sync._ownership.path.exists()


async def test_finalized_gone_sync_record_is_retired_independently(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=_Host())
    ownership.write(_record(root, state="finalized", pid=451))

    await ownership.reconcile()

    assert not ownership.path.exists()


async def test_recycled_sync_leader_retires_stale_finalized_record(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    record = _record(root, state="finalized", pid=451)
    record["starttime_ticks"] = 10.5
    uid = os.getuid() if hasattr(os, "getuid") else None
    host = _Host(
        {
            451: _ProcessIdentity(
                stamp=99.5,
                cmdline=tuple(record["argv"]),
                uid=uid,
                environment={},
            )
        }
    )
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    await ownership.reconcile()

    assert not ownership.path.exists()
    assert host.signals == []


async def test_live_parent_keeps_singleton_sync_record_and_child_untouched(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None
    host = _Host(
        {
            99: _ProcessIdentity(
                stamp=8.25,
                cmdline=("avibe",),
                uid=uid,
                environment={},
                wall_create_time=8.25,
            )
        }
    )
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(_record(root, state="finalized", pid=451))

    with pytest.raises(SyncOwnershipError, match="live parent"):
        await ownership.reconcile()

    assert ownership.path.exists()
    assert host.signals == []


async def test_live_parent_keeps_record_when_identity_stamp_is_linux_ticks(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None
    host = _Host(
        {
            99: _ProcessIdentity(
                stamp=424_242.0,
                cmdline=("avibe",),
                uid=uid,
                environment={},
                wall_create_time=8.25,
            )
        }
    )
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(_record(root, state="finalized", pid=451))

    with pytest.raises(SyncOwnershipError, match="live parent"):
        await ownership.reconcile()

    assert ownership.path.exists()
    assert host.signals == []


async def test_legacy_sync_child_with_wall_clock_record_is_not_removed_as_recycled(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None
    record = _record(root, state="finalized", pid=451)
    environment = {
        "EVEROS_ROOT": str(root),
        "AVIBE_MEMORY_CHILD_ROLE": SYNC_ROLE,
        SYNC_NONCE_ENV: "a" * 64,
        SYNC_PARENT_PID_ENV: "99",
        SYNC_PARENT_CREATE_TIME_ENV: float(8.25).hex(),
        SYNC_PARENT_UID_ENV: "" if uid is None else str(uid),
    }
    host = _Host(
        {
            451: _ProcessIdentity(
                stamp=424_242.0,
                cmdline=tuple(record["argv"]),
                uid=uid,
                environment=environment,
                wall_create_time=10.5,
            )
        }
    )
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    await ownership.reconcile()

    assert host.signals
    assert host.signals[0][0] == {451: 424_242.0}
    assert not ownership.path.exists()


async def test_legacy_sync_child_without_wall_time_keeps_record(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None
    record = _record(root, state="finalized", pid=451)
    host = _Host(
        {
            451: _ProcessIdentity(
                stamp=424_242.0,
                cmdline=tuple(record["argv"]),
                uid=uid,
                environment={},
                wall_create_time=None,
            )
        }
    )
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    with pytest.raises(SyncOwnershipError, match="creation time is unavailable"):
        await ownership.reconcile()

    assert ownership.path.exists()
    assert host.signals == []


async def test_retained_failed_cleanup_reconciles_while_its_parent_is_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    root = tmp_path / "home" / "memory" / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None

    class RetainedCleanupHost(_Host):
        def recorded_group_members(self, process_group, *, socket_path, provider_root, role=None):
            del process_group, socket_path, provider_root
            assert role is _MemoryChildRole.CASCADE_SYNC
            return {}, []

    host = RetainedCleanupHost(
        {
            99: _ProcessIdentity(
                stamp=8.25,
                cmdline=("avibe",),
                uid=uid,
                environment={},
                wall_create_time=8.25,
            )
        }
    )
    sync = EverOSSyncProcess(
        python,
        effective_home=tmp_path / "home",
        _host=host,
    )
    record = _record(root, state="finalized", pid=451)
    sync._ownership.write(record)

    async def cleanup_cannot_be_proven(*_args, **_kwargs) -> None:
        raise SyncOwnershipError("sync process group death is unproven")

    monkeypatch.setattr(sync, "_terminate_owned_sync_tree", cleanup_cannot_be_proven)

    class Process:
        pid = 451

    await sync._cleanup_failed_launch(Process(), 451, {451: 10.5}, str(record["nonce"]))

    assert sync._ownership.read()["cleanup_failed"] is True
    await sync._ownership.reconcile()
    assert not sync._ownership.path.exists()


async def test_retained_pending_cleanup_reconciles_while_its_parent_is_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    root = tmp_path / "home" / "memory" / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None

    class RetainedPendingHost(_Host):
        def find_syncs(self, *, provider_root, python, nonce):
            del provider_root, python, nonce
            return {}

    host = RetainedPendingHost(
        {
            99: _ProcessIdentity(
                stamp=8.25,
                cmdline=("avibe",),
                uid=uid,
                environment={},
                wall_create_time=8.25,
            )
        }
    )
    sync = EverOSSyncProcess(
        python,
        effective_home=tmp_path / "home",
        _host=host,
    )
    record = _record(root, state="pending")
    sync._ownership.write(record)

    async def cleanup_cannot_be_proven(*_args, **_kwargs) -> None:
        raise RuntimeError("EverOS child process tree did not exit")

    monkeypatch.setattr(sync, "_terminate_owned_sync_tree", cleanup_cannot_be_proven)

    class Process:
        pid = 451

    await sync._cleanup_failed_launch(Process(), 451, {451: 10.5}, str(record["nonce"]))

    assert sync._ownership.read()["cleanup_failed"] is True
    await sync._ownership.reconcile()
    assert not sync._ownership.path.exists()


async def test_handleless_spawn_failure_marks_discovered_pending_record_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    uid = os.getuid() if hasattr(os, "getuid") else None
    parent = _ParentIdentity(pid=99, create_time=8.25, uid=uid)

    class HandlelessSpawnHost(_Host):
        child_is_present = True

        async def spawn(self, *_args, **_kwargs):
            raise OSError("child was created before the spawn handoff failed")

        def find_syncs(self, *, provider_root, python, nonce):
            del provider_root, python
            assert len(nonce) == 64
            return {451: 10.5} if self.child_is_present else {}

    host = HandlelessSpawnHost(
        {
            parent.pid: _ProcessIdentity(
                stamp=parent.create_time,
                cmdline=("avibe",),
                uid=uid,
                environment={},
                wall_create_time=parent.create_time,
            )
        }
    )
    sync = EverOSSyncProcess(python, effective_home=tmp_path / "home", _host=host)
    monkeypatch.setattr(memory_sync_process, "_parent_identity", lambda: parent)

    assert await sync.run() is SyncProcessResult.FAILED
    record = sync._ownership.read()
    assert record is not None
    assert record["state"] == "pending"
    assert record["cleanup_failed"] is True

    host.child_is_present = False
    await sync._ownership.reconcile()
    assert not sync._ownership.path.exists()


async def test_handleless_spawn_failure_marks_uncertain_pending_record_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    uid = os.getuid() if hasattr(os, "getuid") else None
    parent = _ParentIdentity(pid=99, create_time=8.25, uid=uid)

    class UncertainHandlelessSpawnHost(_Host):
        discovery_is_uncertain = True

        async def spawn(self, *_args, **_kwargs):
            raise OSError("child was created before the spawn handoff failed")

        def find_syncs(self, *, provider_root, python, nonce):
            del provider_root, python
            assert len(nonce) == 64
            if self.discovery_is_uncertain:
                raise RuntimeError("sync child identity is temporarily unavailable")
            return {}

    host = UncertainHandlelessSpawnHost(
        {
            parent.pid: _ProcessIdentity(
                stamp=parent.create_time,
                cmdline=("avibe",),
                uid=uid,
                environment={},
                wall_create_time=parent.create_time,
            )
        }
    )
    sync = EverOSSyncProcess(python, effective_home=tmp_path / "home", _host=host)
    monkeypatch.setattr(memory_sync_process, "_parent_identity", lambda: parent)

    assert await sync.run() is SyncProcessResult.FAILED
    record = sync._ownership.read()
    assert record is not None
    assert record["state"] == "pending"
    assert record["cleanup_failed"] is True

    host.discovery_is_uncertain = False
    await sync._ownership.reconcile()
    assert not sync._ownership.path.exists()


async def test_sync_cleanup_runtime_error_marks_the_finalized_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")

    class Process:
        pid = 451
        returncode = None

        def send_signal(self, signum):
            assert signum is signal.SIGCONT

        async def wait(self):
            self.returncode = 0
            return 0

    process = Process()

    class Host:
        environment: dict[str, str] = {}

        async def spawn(self, kind, artifact_python, *, cwd, env, socket_path=None):
            del cwd, socket_path
            assert kind is _ProcessKind.CASCADE_SYNC
            assert artifact_python == python
            self.environment = dict(env)
            return process

        def process_group(self, pid):
            assert pid == process.pid
            return pid

        def snapshot_tree(self, pid, process_group):
            assert (pid, process_group) == (process.pid, process.pid)
            return {pid: 10.5}

        async def wait_for_stopped(self, pid, timeout_seconds):
            del timeout_seconds
            assert pid == process.pid
            return True

        def inspect_identity(self, pid):
            assert pid == process.pid
            return _ProcessIdentity(
                stamp=10.5,
                cmdline=(str(python), *SYNC_ARGV),
                uid=os.getuid() if hasattr(os, "getuid") else None,
                environment=self.environment,
            )

    sync = EverOSSyncProcess(
        python,
        effective_home=tmp_path / "home",
        _host=Host(),
    )
    cleanup_calls = 0

    async def cleanup_fails(*_args, **_kwargs) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("EverOS child process tree did not exit")

    monkeypatch.setattr(sync, "_terminate_owned_sync_tree", cleanup_fails)

    assert await sync.run() is SyncProcessResult.FAILED
    record = sync._ownership.read()
    assert cleanup_calls == 2
    assert record is not None
    assert record["state"] == "finalized"
    assert record["cleanup_failed"] is True


async def test_sync_cleanup_survives_a_second_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    handshake_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class Process:
        pid = 451

    class Host:
        async def spawn(self, *_args, **_kwargs):
            return Process()

        def process_group(self, pid):
            assert pid == 451
            return pid

        def snapshot_tree(self, pid, process_group):
            assert (pid, process_group) == (451, 451)
            return {pid: 10.5}

        async def wait_for_stopped(self, pid, timeout_seconds):
            del pid, timeout_seconds
            handshake_started.set()
            await asyncio.Future()

    sync = EverOSSyncProcess(python, effective_home=tmp_path / "home", _host=Host())

    async def block_cleanup(*_args, **_kwargs) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    monkeypatch.setattr(sync, "_terminate_owned_sync_tree", block_cleanup)
    task = asyncio.create_task(sync.run())
    await asyncio.wait_for(handshake_started.wait(), timeout=0.5)

    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_cleanup.set()
    assert await task is SyncProcessResult.INTERRUPTED
    assert not sync._ownership.path.exists()


async def test_sync_ownership_is_shared_across_homes_for_one_provider_root(
    tmp_path: Path,
) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    provider_root = tmp_path / "shared" / "everos-root"
    provider_root.parent.mkdir(mode=0o700)
    owner = EverOSSyncProcess(
        python,
        effective_home=tmp_path / "owner-home",
        provider_root=provider_root,
        _host=_Host(),
    )
    contender = EverOSSyncProcess(
        python,
        effective_home=tmp_path / "contender-home",
        provider_root=provider_root,
        _host=_Host(),
    )
    owner._ownership.claim(_record(provider_root, state="pending"))

    assert owner._ownership.path == contender._ownership.path
    assert await contender.run() is SyncProcessResult.ALREADY_RUNNING


async def test_sync_serializes_with_a_held_rebuild_lock(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    provider_root = tmp_path / "shared" / "everos-root"
    provider_root.parent.mkdir(mode=0o700)
    provider_root.mkdir(mode=0o700)

    class NoSpawnHost(_Host):
        async def spawn(self, *_args, **_kwargs):
            pytest.fail("sync must not spawn while rebuild owns the provider root")

    locker = await _hold_rebuild_lock(provider_root)
    sync = EverOSSyncProcess(
        python,
        effective_home=tmp_path / "sync-home",
        provider_root=provider_root,
        _host=NoSpawnHost(),
    )
    try:
        with pytest.raises(memory_process._ProviderRootBusy):
            await sync.reconcile_orphan()
        assert await sync.run() is SyncProcessResult.ALREADY_RUNNING
        assert not sync._ownership.path.exists()
    finally:
        if locker.returncode is None:
            await _release_rebuild_lock(locker)


async def test_sync_rejects_a_symlinked_provider_root_without_touching_target(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(mode=0o700)
    target = tmp_path / "provider-target"
    target.mkdir(mode=0o700)
    sentinel = target / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    provider_root = memory_dir / "everos-root"
    provider_root.symlink_to(target, target_is_directory=True)
    sync = EverOSSyncProcess(
        sys.executable,
        effective_home=tmp_path,
        provider_root=provider_root,
        _host=_Host(),
    )

    assert await sync.run() is SyncProcessResult.FAILED
    assert provider_root.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in target.iterdir()) == ["sentinel"]
    assert not sync._ownership.path.exists()


async def test_finalized_sync_reconciliation_cleans_exact_recorded_group(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None
    record = _record(root, state="finalized", pid=451)
    environment = {
        "EVEROS_ROOT": str(root),
        "AVIBE_MEMORY_CHILD_ROLE": SYNC_ROLE,
        SYNC_NONCE_ENV: "a" * 64,
        SYNC_PARENT_PID_ENV: "99",
        SYNC_PARENT_CREATE_TIME_ENV: float(8.25).hex(),
        SYNC_PARENT_UID_ENV: "" if uid is None else str(uid),
    }
    host = _Host(
        {
            451: _ProcessIdentity(
                stamp=10.5,
                cmdline=tuple(record["argv"]),
                uid=uid,
                environment=environment,
                wall_create_time=10.5,
            )
        }
    )
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    await ownership.reconcile()

    assert not ownership.path.exists()
    assert host.signals[0][0] == {451: 10.5}
    assert host.signals[0][2] == 451


async def test_gone_leader_with_live_helper_is_swept_before_retirement(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    record = _record(root, state="finalized", pid=451)
    uid = os.getuid() if hasattr(os, "getuid") else None
    helper_env = {
        "EVEROS_ROOT": str(root),
        "AVIBE_MEMORY_CHILD_ROLE": SYNC_ROLE,
        SYNC_NONCE_ENV: record["nonce"],
        SYNC_PARENT_PID_ENV: str(record["parent_pid"]),
        SYNC_PARENT_CREATE_TIME_ENV: float(record["parent_create_time"]).hex(),
        SYNC_PARENT_UID_ENV: "" if uid is None else str(uid),
    }
    helper = _ProcessIdentity(
        stamp=11.5,
        cmdline=("/runtime/bin/python", "-c", "everos-helper"),
        uid=uid,
        environment=helper_env,
    )
    host = _Host({777: helper})
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    await ownership.reconcile()

    assert not ownership.path.exists()
    assert host.signals[0][0] == {777: 11.5}


async def test_orphan_reconciliation_rescans_group_before_retiring_record(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    record = _record(root, state="finalized", pid=451)
    uid = os.getuid() if hasattr(os, "getuid") else None
    helper_env = {
        "EVEROS_ROOT": str(root),
        "AVIBE_MEMORY_CHILD_ROLE": SYNC_ROLE,
        SYNC_NONCE_ENV: record["nonce"],
        SYNC_PARENT_PID_ENV: str(record["parent_pid"]),
        SYNC_PARENT_CREATE_TIME_ENV: float(record["parent_create_time"]).hex(),
        SYNC_PARENT_UID_ENV: "" if uid is None else str(uid),
    }

    class ReplacingHelperHost(_Host):
        scans = 0

        def recorded_group_members(self, process_group, **kwargs):
            del process_group, kwargs
            self.scans += 1
            if self.scans == 1:
                return {777: 11.5}, []
            self.identities[778] = _ProcessIdentity(
                stamp=12.5,
                cmdline=("/runtime/bin/python", "-c", "replacement-helper"),
                uid=uid,
                environment=helper_env,
            )
            return {778: 12.5}, []

    host = ReplacingHelperHost()
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    with pytest.raises(SyncOwnershipError, match="group death is unproven"):
        await ownership.reconcile()

    assert host.scans == 2
    assert ownership.path.exists()
    assert host.signals == []


async def test_uncertain_group_member_preserves_record(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    record = _record(root, state="finalized", pid=451)

    class UncertainHost(_Host):
        def recorded_group_members(self, process_group, **kwargs):
            del process_group, kwargs
            return {}, [777]

    host = UncertainHost()
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    with pytest.raises(SyncOwnershipError, match="unverifiable"):
        await ownership.reconcile()
    assert ownership.path.exists()
    assert host.signals == []


async def test_spawn_failure_without_process_retires_exact_pending_nonce(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")

    class SpawnFailureHost(_Host):
        async def spawn(self, *_args, **_kwargs):
            raise OSError("spawn failed")

        def find_syncs(self, *, provider_root, python, nonce):
            del provider_root, python, nonce
            return {}

    process = EverOSSyncProcess(
        python,
        effective_home=tmp_path / "home",
        _host=SpawnFailureHost(),
    )
    assert await process.run() is SyncProcessResult.FAILED
    assert not sync_record_path(tmp_path / "home" / "memory").exists()

    # A retry can claim a fresh nonce immediately; the failed caller did not
    # leave a tombstone that blocks admission.
    assert await process.run() is SyncProcessResult.FAILED
    assert not sync_record_path(tmp_path / "home" / "memory").exists()


async def test_system_host_uses_exact_direct_sync_argv(monkeypatch, tmp_path: Path) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def create_subprocess(*arguments, **options):
        captured.append((arguments, options))
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    await _SystemProcessHost().spawn(
        _ProcessKind.CASCADE_SYNC,
        Path("/artifact/bin/python"),
        cwd=tmp_path,
        env={"EVEROS_ROOT": str(tmp_path / "root")},
    )

    assert captured[0][0] == (
        "/artifact/bin/python",
        "-I",
        "-m",
        "everos.entrypoints.cli.main",
        "cascade",
        "sync",
    )
    assert captured[0][1]["start_new_session"] is True


def test_sync_environment_has_no_host_source_path(tmp_path: Path) -> None:
    from core.memory.process import EverOSProcessSettings

    env = _sync_environment(
        Path("/artifact/bin/python"),
        tmp_path / "memory",
        tmp_path / "memory" / "everos-root",
        _ParentIdentity(pid=99, create_time=8.25, uid=501),
        "a" * 64,
        settings=EverOSProcessSettings(),
    )

    assert "PYTHONPATH" not in env
    assert all("SOURCE_ROOT" not in key for key in env)


async def test_sync_finalizes_ownership_before_sigcont_and_cleans_group(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    memory_dir = tmp_path / "home" / "memory"
    record_path = sync_record_path(memory_dir)
    events: list[str] = []

    class Process:
        pid = 451
        returncode = None

        def send_signal(self, signum):
            assert signum.name == "SIGCONT"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            assert record["state"] == "finalized"
            events.append("continued")

        async def wait(self):
            self.returncode = 0
            return 0

    process = Process()

    class Host:
        environment: dict[str, str] = {}
        alive = True

        async def spawn(self, kind, artifact_python, *, cwd, env, socket_path=None):
            del cwd, socket_path
            assert kind is _ProcessKind.CASCADE_SYNC
            assert artifact_python == python
            self.environment = dict(env)
            events.append("spawned")
            return process

        def process_group(self, pid):
            return pid

        def snapshot_tree(self, pid, process_group):
            assert process_group == pid
            return {pid: 10.5} if self.alive else {}

        def recorded_group_members(self, process_group, *, socket_path, provider_root, role=None):
            del process_group, socket_path, provider_root, role
            return {}, []

        async def wait_for_stopped(self, pid, timeout_seconds):
            del timeout_seconds
            assert pid == process.pid
            events.append("stopped")
            return True

        def inspect_identity(self, pid):
            assert pid == process.pid
            events.append("validated")
            return _ProcessIdentity(
                stamp=10.5,
                cmdline=(str(python), *SYNC_ARGV),
                uid=os.getuid() if hasattr(os, "getuid") else None,
                environment=self.environment,
            )

        def live(self, identities):
            return dict(identities) if self.alive else {}

        def signal(self, identities, signum, *, process_group=None, process=None):
            del identities, signum, process_group, process
            events.append("cleaned")
            self.alive = False

        async def wait_for_exit(self, identities, timeout_seconds, **_kwargs):
            del identities, timeout_seconds
            return not self.alive

    result = await EverOSSyncProcess(
        python,
        effective_home=tmp_path / "home",
        _host=Host(),
    ).run()

    assert result is SyncProcessResult.COMPLETED
    assert events[:4] == ["spawned", "stopped", "validated", "continued"]
    assert events[-1] == "cleaned"
    assert not record_path.exists()


async def test_sync_spawn_handoff_finishes_before_honoring_cancellation(tmp_path: Path) -> None:
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    memory_dir = tmp_path / "home" / "memory"
    record_path = sync_record_path(memory_dir)
    events: list[str] = []
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    class Process:
        pid = 451
        returncode = None

        def send_signal(self, signum):
            assert signum.name == "SIGCONT"
            assert json.loads(record_path.read_text(encoding="utf-8"))["state"] == "finalized"
            events.append("continued")

        async def wait(self):
            events.append("waited")
            self.returncode = 0
            return 0

    process = Process()

    class Host:
        environment: dict[str, str] = {}
        alive = True

        async def spawn(self, kind, artifact_python, *, cwd, env, socket_path=None):
            del cwd, socket_path
            assert kind is _ProcessKind.CASCADE_SYNC
            assert artifact_python == python
            self.environment = dict(env)
            events.append("spawn-start")
            spawn_started.set()
            await release_spawn.wait()
            events.append("spawn-return")
            return process

        def process_group(self, pid):
            return pid

        def snapshot_tree(self, pid, process_group):
            assert process_group == pid
            return {pid: 10.5} if self.alive else {}

        def recorded_group_members(self, process_group, *, socket_path, provider_root, role=None):
            del process_group, socket_path, provider_root, role
            return {}, []

        async def wait_for_stopped(self, pid, timeout_seconds):
            del timeout_seconds
            assert pid == process.pid
            return True

        def inspect_identity(self, pid):
            assert pid == process.pid
            return _ProcessIdentity(
                stamp=10.5,
                cmdline=(str(python), *SYNC_ARGV),
                uid=os.getuid() if hasattr(os, "getuid") else None,
                environment=self.environment,
            )

        def live(self, identities):
            return dict(identities) if self.alive else {}

        def signal(self, identities, signum, *, process_group=None, process=None):
            del identities, signum, process_group, process
            events.append("cleaned")
            self.alive = False

        async def wait_for_exit(self, identities, timeout_seconds, **_kwargs):
            del identities, timeout_seconds
            return not self.alive

    task = asyncio.create_task(
        EverOSSyncProcess(
            python,
            effective_home=tmp_path / "home",
            _host=Host(),
        ).run()
    )
    await spawn_started.wait()
    task.cancel()
    release_spawn.set()

    assert await task is SyncProcessResult.INTERRUPTED
    assert events.index("spawn-return") < events.index("continued")
    assert "waited" not in events
    assert "cleaned" in events
    assert not record_path.exists()


async def test_sync_cleanup_rescans_a_group_after_the_leader_exits(tmp_path: Path) -> None:
    helper = _ProcessIdentity(
        stamp=11.5,
        cmdline=("/runtime/bin/python", *SYNC_ARGV),
        uid=os.getuid() if hasattr(os, "getuid") else None,
        environment={},
    )
    host = _Host({777: helper})
    sync = EverOSSyncProcess(
        "/runtime/bin/python",
        effective_home=tmp_path / "home",
        _host=host,
    )

    class ExitedProcess:
        pid = 451
        returncode = 0

    await sync._terminate_owned_sync_tree(
        ExitedProcess(),
        process_group=451,
        owned_processes={451: 10.5},
        stop_timeout_seconds=1.0,
    )

    assert host.signals[0][0] == {451: 10.5, 777: 11.5}
