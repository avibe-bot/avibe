from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import sys

import pytest

from avibe_memory.confined_filesystem import ConfinedFilesystemError
from avibe_memory.process import (
    _ProcessIdentity,
    _cmdline_is_sidecar,
    _memory_child_environment,
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
    ReleasedEverOSOrphanReconciler,
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
        assert role == "cascade_sync"
        return {
            pid: float(identity.stamp)
            for pid, identity in self.children.items()
            if identity.stamp is not None
        }, []

    def find_syncs(self, **_kwargs):
        return dict(self.candidates)

    def find_sidecars(self, **_kwargs):
        return {}

    def find_sidecars_by_root(self, **_kwargs):
        return {}

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


class _SidecarHost:
    def __init__(
        self,
        children: dict[int, _ProcessIdentity] | None = None,
        *,
        group_owned: dict[int, float] | None = None,
        group_foreign: list[int] | None = None,
    ) -> None:
        self.children = children or {}
        self.group_owned = group_owned or {}
        self.group_foreign = group_foreign or []
        self.signals: list[dict[int, float]] = []

    def inspect_identity(self, pid: int):
        return self.children.get(pid)

    def process_group(self, pid: int) -> int | None:
        return pid

    def snapshot_tree(self, pid: int, _group: int | None):
        identity = self.children.get(pid)
        return {} if identity is None else {pid: float(identity.stamp)}

    def recorded_group_members(self, _group, **_kwargs):
        return dict(self.group_owned), list(self.group_foreign)

    def find_sidecars(self, **_kwargs):
        return {}

    def find_sidecars_by_root(self, **_kwargs):
        return {}

    def find_syncs(self, **_kwargs):
        return {}

    def live(self, identities):
        return {
            pid: created_at
            for pid, created_at in identities.items()
            if (
                (identity := self.children.get(pid)) is not None
                and identity.stamp == created_at
            )
        }

    def signal(self, identities, _signum, **_kwargs) -> None:
        self.signals.append(dict(identities))
        for pid in identities:
            self.children.pop(pid, None)
            self.group_owned.pop(pid, None)

    async def wait_for_exit(self, identities, _timeout, **_kwargs) -> bool:
        return not self.live(identities)


def _sidecar_record(home: Path) -> tuple[Path, dict[str, object]]:
    provider_root = home / "memory" / "everos-root"
    socket_path = home / "memory" / ".rt" / "everos.sock"
    python = home / "memory" / "runtime" / "bin" / "python"
    record: dict[str, object] = {
        "pid": 451,
        "create_time": 10.5,
        "starttime_ticks": 10.5,
        "process_group": 451,
        "provider_root": str(provider_root),
        "socket_path": str(socket_path),
        "role": "sidecar",
        "python": str(python),
    }
    path = home / "memory" / ".rt" / "everos.sidecar.json"
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    return path, record


def _sidecar_identity(
    home: Path,
    record: dict[str, object],
    *,
    entrypoint: str = "avibe_memory.sidecar",
) -> _ProcessIdentity:
    return _ProcessIdentity(
        stamp=10.5,
        cmdline=(
            str(record["python"]),
            "-m",
            entrypoint,
            "--uds",
            str(record["socket_path"]),
        ),
        uid=os.getuid() if hasattr(os, "getuid") else None,
        environment={
            "EVEROS_ROOT": str(home / "memory" / "everos-root"),
            "AVIBE_MEMORY_CHILD_ROLE": "sidecar",
        },
        wall_create_time=10.5,
    )


def _released_rebuild_record(home: Path) -> tuple[Path, dict[str, object]]:
    path, record = _sidecar_record(home)
    record["role"] = "cascade_rebuild"
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    return path, record


def _released_rebuild_identity(
    home: Path,
    record: dict[str, object],
) -> _ProcessIdentity:
    return _ProcessIdentity(
        stamp=10.5,
        cmdline=(
            str(record["python"]),
            "-m",
            "core.memory.rebuild_child",
            "cascade",
            "rebuild",
            "--yes",
        ),
        uid=os.getuid() if hasattr(os, "getuid") else None,
        environment={
            "EVEROS_ROOT": str(home / "memory" / "everos-root"),
            "AVIBE_MEMORY_CHILD_ROLE": "cascade_rebuild",
        },
        wall_create_time=10.5,
    )


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

    async def on_ready() -> None:
        nonlocal ready
        ready += 1

    process = FakeEverOSProcess(
        start_results=deque([True]),
        on_ready=on_ready,
    )

    assert await process.start() is True
    assert process.running is True
    await process.stop()

    assert process.running is False
    assert process.stopped is True
    assert ready == 1


@pytest.mark.asyncio
async def test_sidecar_stop_failure_retains_process_tree_proof() -> None:
    process = FakeEverOSProcess(stop_failure=RuntimeError("still alive"))
    assert await process.start() is True

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


def test_memory_child_environment_exposes_top_level_package_root(
    tmp_path: Path,
) -> None:
    environment = _memory_child_environment(
        python=Path(sys.executable),
        memory_dir=tmp_path / "memory",
        provider_root=tmp_path / "memory" / "everos-root",
        attachments_root=tmp_path / "memory" / "attachments",
        settings=EverOSProcessSettings(),
        role=None,
    )

    package_root = Path(__file__).resolve().parents[1]
    assert environment["PYTHONPATH"] == str(package_root)
    assert (package_root / "avibe_memory").is_dir()


def test_recorded_sidecar_reaper_confines_provider_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    outside = tmp_path / "foreign-root"

    with pytest.raises(ConfinedFilesystemError):
        ReleasedEverOSOrphanReconciler(
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
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
    )

    await reaper.reconcile_orphans()

    assert provider_root.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrypoint", "legacy_record"),
    [
        ("avibe_memory.sidecar", False),
        ("core.memory.sidecar", False),
        ("core.memory.sidecar", True),
    ],
)
async def test_sidecar_reaper_verifies_identity_before_signalling(
    tmp_path: Path,
    entrypoint: str,
    legacy_record: bool,
) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, record = _sidecar_record(home)
    if legacy_record:
        record.pop("role")
        path.write_text(json.dumps(record), encoding="utf-8")
    identity = _sidecar_identity(home, record, entrypoint=entrypoint)
    host = _SidecarHost({451: identity})
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    await reaper.reconcile_orphans()

    assert host.signals[0] == {451: 10.5}
    assert not path.exists()


@pytest.mark.parametrize(
    "entrypoint",
    ["avibe_memory.sidecar", "core.memory.sidecar"],
)
def test_sidecar_discovery_accepts_current_and_released_entrypoints(
    entrypoint: str,
) -> None:
    assert _cmdline_is_sidecar(
        ("/runtime/bin/python", "-m", entrypoint, "--uds", "/memory.sock")
    )


@pytest.mark.asyncio
async def test_reaper_consumes_released_rebuild_ownership(tmp_path: Path) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, record = _released_rebuild_record(home)
    host = _SidecarHost({451: _released_rebuild_identity(home, record)})
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    await reaper.reconcile_orphans()

    assert host.signals[0] == {451: 10.5}
    assert not path.exists()


@pytest.mark.asyncio
async def test_sidecar_reaper_does_not_signal_unverifiable_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, record = _sidecar_record(home)
    identity = _sidecar_identity(home, record)
    identity = _ProcessIdentity(
        stamp=identity.stamp,
        cmdline=identity.cmdline,
        uid=identity.uid,
        environment=None,
        wall_create_time=identity.wall_create_time,
    )
    host = _SidecarHost({451: identity})
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    with pytest.raises(RuntimeError, match="identity could not be verified"):
        await reaper.reconcile_orphans()

    assert host.signals == []
    assert path.exists()


@pytest.mark.asyncio
async def test_sidecar_reaper_fails_closed_on_unverifiable_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    provider_root = home / "memory" / "everos-root"
    provider_root.mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "memory").chmod(0o700)
    path, _record = _sidecar_record(home)
    host = _SidecarHost(group_foreign=[777])
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    with pytest.raises(RuntimeError, match="group could not be verified"):
        await reaper.reconcile_orphans()

    assert host.signals == []
    assert path.exists()


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
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=_ReleasedSyncHost(),
    )

    await reaper.reconcile_orphans()

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
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    await reaper.reconcile_orphans()

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
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    await reaper.reconcile_orphans()

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
    reaper = ReleasedEverOSOrphanReconciler(
        provider_root=provider_root,
        effective_home=home,
        _host=host,
    )

    with pytest.raises(RuntimeError, match="identity is unavailable"):
        await reaper.reconcile_orphans()

    assert host.signals == []
    assert path.exists()
