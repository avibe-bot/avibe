from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path

import pytest

from core.memory.confined_filesystem import ConfinedFilesystemError
from core.memory.process import (
    _MemoryChildRole,
    _ProcessIdentity,
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
    RecordedSidecarReaper,
    RecordedSyncReaper,
    legacy_sync_record_path,
)


class _ReleasedSyncHost:
    def __init__(
        self,
        children: dict[int, _ProcessIdentity] | None = None,
        *,
        candidates: dict[int, float] | None = None,
        parent: _ProcessIdentity | None = None,
    ) -> None:
        self.children = children or {}
        self.candidates = candidates or {}
        self.parent = parent
        self.signals: list[tuple[dict[int, float], int | None]] = []

    def inspect_identity(self, pid: int):
        return self.parent if pid == 99 else self.children.get(pid)

    def process_group(self, pid: int) -> int | None:
        return pid

    def recorded_group_members(self, _group, *, role=None, **_kwargs):
        assert role is _MemoryChildRole.CASCADE_SYNC
        return {
            pid: float(identity.stamp)
            for pid, identity in self.children.items()
            if identity.stamp is not None
        }, []

    def find_syncs(self, **_kwargs):
        return dict(self.candidates)

    def live(self, identities):
        return {
            pid: created_at
            for pid, created_at in identities.items()
            if (
                (identity := self.children.get(pid)) is not None
                and identity.stamp == created_at
            )
        }

    def signal(self, identities, _signum, *, process_group=None, **_kwargs) -> None:
        self.signals.append((dict(identities), process_group))
        for pid in identities:
            self.children.pop(pid, None)

    async def wait_for_exit(self, identities, _timeout, **_kwargs) -> bool:
        return not self.live(identities)


def _released_sync_record(home: Path, *, state: str) -> tuple[Path, dict[str, object]]:
    provider_root = home / "memory" / "everos-root"
    path = legacy_sync_record_path(provider_root)
    python = home / "memory" / "runtime" / "bin" / "python"
    record: dict[str, object] = {
        "state": state,
        "nonce": "a" * 64,
        "pid": 451 if state == "finalized" else None,
        "create_time": 10.5 if state == "finalized" else None,
        "process_group": 451 if state == "finalized" else None,
        "parent_pid": 99,
        "parent_create_time": 8.25,
        "parent_uid": os.getuid() if hasattr(os, "getuid") else None,
        "provider_root": str(provider_root),
        "socket_path": str(home / "memory" / ".rt" / "everos.sock"),
        "role": "cascade_sync",
        "argv": [
            str(python),
            "-I",
            "-m",
            "everos.entrypoints.cli.main",
            "cascade",
            "sync",
        ],
    }
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    return path, record


def _released_sync_environment(
    home: Path,
    record: dict[str, object],
) -> dict[str, str]:
    uid = record["parent_uid"]
    return {
        "EVEROS_ROOT": str(home / "memory" / "everos-root"),
        "AVIBE_MEMORY_CHILD_ROLE": "cascade_sync",
        "AVIBE_MEMORY_SYNC_NONCE": str(record["nonce"]),
        "AVIBE_MEMORY_SYNC_PARENT_PID": str(record["parent_pid"]),
        "AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME": float(
            record["parent_create_time"]
        ).hex(),
        "AVIBE_MEMORY_SYNC_PARENT_UID": "" if uid is None else str(uid),
    }


@pytest.mark.asyncio
async def test_fake_sidecar_start_and_stop_expose_proven_lifecycle() -> None:
    ready = 0
    reaped = 0

    async def on_ready() -> None:
        nonlocal ready
        ready += 1

    async def on_reaped() -> None:
        nonlocal reaped
        reaped += 1

    process = FakeEverOSProcess(
        start_results=deque([True]),
        on_ready=on_ready,
        on_reaped=on_reaped,
    )

    assert await process.start() is True
    assert process.running is True
    await process.stop()

    assert process.running is False
    assert process.stopped is True
    assert ready == 1
    assert reaped == 1


@pytest.mark.asyncio
async def test_sidecar_stop_failure_retains_process_tree_proof() -> None:
    process = FakeEverOSProcess(stop_failure=RuntimeError("still alive"))

    with pytest.raises(RuntimeError, match="still alive"):
        await process.stop()

    assert process.retains_active_config is True


def test_process_factory_keeps_secrets_out_of_repr(tmp_path: Path) -> None:
    factory = FakeEverOSProcessFactory()
    settings = EverOSProcessSettings(
        llm_api_key="llm-secret",
        embedding_api_key="embedding-secret",
    )

    process = factory(
        "/usr/bin/python3",
        provider_root=tmp_path / "memory" / "everos-root",
        effective_home=tmp_path,
        settings=settings,
        on_ready=lambda: None,
    )

    assert "llm-secret" not in repr(settings)
    assert "embedding-secret" not in repr(settings)
    assert process.settings is settings
    assert factory.supervised == [process]


def test_recorded_sidecar_reaper_confines_provider_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    outside = tmp_path / "foreign-root"

    with pytest.raises(ConfinedFilesystemError):
        RecordedSidecarReaper(
            provider_root=outside,
            effective_home=home,
        )


@pytest.mark.asyncio
async def test_recorded_sidecar_reaper_accepts_empty_owned_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    reaper = RecordedSidecarReaper(
        provider_root=provider_root,
        effective_home=home,
    )

    await reaper.reconcile_orphan()

    assert provider_root.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "finalized"])
async def test_released_sync_reaper_retires_gone_ownership(
    tmp_path: Path,
    state: str,
) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, _record = _released_sync_record(home, state=state)
    reaper = RecordedSyncReaper(
        provider_root=provider_root,
        effective_home=home,
        _host=_ReleasedSyncHost(),
    )

    await reaper.reconcile_orphan()

    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "finalized"])
async def test_released_sync_reaper_stops_exact_live_child(
    tmp_path: Path,
    state: str,
) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, record = _released_sync_record(home, state=state)
    identity = _ProcessIdentity(
        stamp=10.5,
        cmdline=tuple(record["argv"]),
        uid=record["parent_uid"],
        environment=_released_sync_environment(home, record),
        wall_create_time=10.5,
    )
    host = _ReleasedSyncHost(
        {451: identity},
        candidates={451: 10.5} if state == "pending" else None,
    )
    reaper = RecordedSyncReaper(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    await reaper.reconcile_orphan()

    assert host.signals[0] == ({451: 10.5}, 451)
    assert not path.exists()


@pytest.mark.asyncio
async def test_released_sync_reaper_sweeps_exact_helper_after_leader_exits(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, record = _released_sync_record(home, state="finalized")
    helper = _ProcessIdentity(
        stamp=11.5,
        cmdline=("/runtime/bin/python", "-c", "everos-helper"),
        uid=record["parent_uid"],
        environment=_released_sync_environment(home, record),
    )
    host = _ReleasedSyncHost({777: helper})
    reaper = RecordedSyncReaper(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    await reaper.reconcile_orphan()

    assert host.signals[0] == ({777: 11.5}, 451)
    assert not path.exists()


@pytest.mark.asyncio
async def test_released_sync_reaper_preserves_record_on_nonce_mismatch(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, record = _released_sync_record(home, state="finalized")
    environment = _released_sync_environment(home, record)
    environment["AVIBE_MEMORY_SYNC_NONCE"] = "b" * 64
    identity = _ProcessIdentity(
        stamp=10.5,
        cmdline=tuple(record["argv"]),
        uid=record["parent_uid"],
        environment=environment,
        wall_create_time=10.5,
    )
    host = _ReleasedSyncHost({451: identity})
    reaper = RecordedSyncReaper(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    with pytest.raises(RuntimeError, match="identity is unavailable"):
        await reaper.reconcile_orphan()

    assert host.signals == []
    assert path.exists()
