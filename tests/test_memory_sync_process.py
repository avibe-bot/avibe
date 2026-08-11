from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

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
            pid: float(identity.create_time)
            for pid, identity in self.identities.items()
            if identity.create_time is not None
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


async def test_pending_sync_record_fails_closed_without_touching_sidecar(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    host = _Host()
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(_record(root, state="pending"))
    sidecar = memory_dir / ".rt" / "everos.sidecar.json"
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


async def test_finalized_gone_sync_record_is_retired_independently(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=_Host())
    ownership.write(_record(root, state="finalized", pid=451))

    await ownership.reconcile()

    assert not ownership.path.exists()


async def test_live_parent_keeps_singleton_sync_record_and_child_untouched(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    root = memory_dir / "everos-root"
    uid = os.getuid() if hasattr(os, "getuid") else None
    host = _Host(
        {
            99: _ProcessIdentity(
                create_time=8.25,
                cmdline=("avibe",),
                uid=uid,
                environment={},
            )
        }
    )
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(_record(root, state="finalized", pid=451))

    with pytest.raises(SyncOwnershipError, match="live parent"):
        await ownership.reconcile()

    assert ownership.path.exists()
    assert host.signals == []


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
                create_time=10.5,
                cmdline=tuple(record["argv"]),
                uid=uid,
                environment=environment,
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
        create_time=11.5,
        cmdline=tuple(record["argv"]),
        uid=uid,
        environment=helper_env,
    )
    host = _Host({777: helper})
    ownership = SyncOwnership(sync_record_path(memory_dir), provider_root=root, host=host)
    ownership.write(record)

    await ownership.reconcile()

    assert not ownership.path.exists()
    assert host.signals[0][0] == {777: 11.5}


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

        async def wait_for_stopped(self, pid, timeout_seconds):
            del timeout_seconds
            assert pid == process.pid
            events.append("stopped")
            return True

        def inspect_identity(self, pid):
            assert pid == process.pid
            events.append("validated")
            return _ProcessIdentity(
                create_time=10.5,
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
