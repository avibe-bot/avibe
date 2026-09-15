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


class _TestReference:
    def __init__(self, host, pid):
        self.host, self.pid = host, pid
        self.generation = host.children.get(pid)

    def is_running(self):
        return self.generation is not None and self.host.children.get(self.pid) is self.generation

    def status(self):
        return "running"


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

    def capture(self, pid: int):
        return _TestReference(self, pid) if pid in self.children else None

    def inspect_identity(self, pid: int):
        return self.parent if pid == 99 else self.children.get(pid)

    def process_group(self, pid: int) -> int | None:
        return pid

    def recorded_group_members(self, _group, *, role=None, **_kwargs):
        assert role == "cascade_sync"
        return {
            pid: self.capture(pid)
            for pid, identity in self.children.items()
            if identity.stamp is not None
        }, []

    def find_syncs(self, **_kwargs):
        return {pid: self.capture(pid) for pid in self.candidates}

    def find_sidecars(self, **_kwargs):
        return {}

    def find_sidecars_by_root(self, **_kwargs):
        return {}

    def live(self, identities):
        return {
            pid: created_at
            for pid, created_at in identities.items()
            if (
created_at is not None and created_at.is_running()
            )
        }

    def signal(self, identities, _signum, *, process_group=None, **_kwargs) -> None:
        self.signals.append(({pid: ref.generation.stamp for pid, ref in identities.items()}, process_group))
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

    def capture(self, pid: int):
        return _TestReference(self, pid) if pid in self.children else None

    def inspect_identity(self, pid: int):
        return self.children.get(pid)

    def process_group(self, pid: int) -> int | None:
        return pid

    def snapshot_tree(self, pid: int, _group: int | None, owned=None):
        identity = self.children.get(pid)
        return dict(owned) if owned is not None else ({} if identity is None else {pid: self.capture(pid)})

    def recorded_group_members(self, _group, **_kwargs):
        return {pid: self.capture(pid) for pid in self.group_owned}, list(self.group_foreign)

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
created_at is not None and created_at.is_running()
            )
        }

    def signal(self, identities, _signum, **_kwargs) -> None:
        self.signals.append({pid: ref.generation.stamp for pid, ref in identities.items()})
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

@pytest.mark.parametrize("profile_enabled", [True, False])
def test_generated_ome_profile_strategies_follow_switch(tmp_path: Path, profile_enabled: bool) -> None:
    from avibe_memory.process import _write_memory_child_config
    import tomllib
    memory_dir = tmp_path / "memory"
    provider_root = tmp_path / "provider"
    attachments = tmp_path / "attachments"
    memory_dir.mkdir(parents=True); (memory_dir / "generated").mkdir(); provider_root.mkdir(parents=True); attachments.mkdir(parents=True)
    _write_memory_child_config(memory_dir=memory_dir, provider_root=provider_root, attachments_root=attachments, settings=EverOSProcessSettings(profile_enabled=profile_enabled))
    ome = tomllib.loads((provider_root / "ome.toml").read_text())
    strategies = ome["strategies"]
    assert strategies["trigger_profile_clustering"]["enabled"] is profile_enabled
    assert strategies["extract_user_profile"]["enabled"] is profile_enabled
    assert strategies["reflect_episodes"]["enabled"] is False
    assert strategies["extract_foresight"]["enabled"] is False


@pytest.mark.asyncio
async def test_orphan_record_survives_shift_until_classified_execution_exits(tmp_path):
    """MEMORY-WAKE-204: classification, signals, wait and retirement share one reference."""
    from dataclasses import replace
    from avibe_memory.process import SidecarOwnership

    home = tmp_path / "home"
    root = home / "memory" / "everos-root"
    root.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    root.parent.chmod(0o700)
    record_path, record = _sidecar_record(home)
    identity = _sidecar_identity(home, record)

    class ShiftHost(_SidecarHost):
        shifted = False
        rounds = 0
        captured = None

        def capture(self, pid):
            self.captured = super().capture(pid)
            return self.captured

        def inspect_identity(self, pid):
            value = super().inspect_identity(pid)
            return replace(value, stamp=value.stamp + 1) if self.shifted and value else value

        def signal(self, identities, signum, **kwargs):
            self.rounds += 1
            assert identities[451] is self.captured
            self.shifted = True
            if self.rounds == 2:
                super().signal(identities, signum, **kwargs)

        async def wait_for_exit(self, identities, timeout, **kwargs):
            assert record_path.exists()
            assert identities[451] is self.captured
            if self.rounds == 1:
                assert self.inspect_identity(451).stamp == 11.5
                assert self.live(identities)
                return False
            assert not self.live(identities)
            return True

    host = ShiftHost({451: identity})
    ownership = SidecarOwnership(record_path=record_path,
                                socket_path=Path(record["socket_path"]),
                                provider_root=root, _host=host)
    await ownership.reap()
    assert host.rounds == 2
    assert not record_path.exists()


def test_record_retirement_rejects_a_late_claimed_survivor(tmp_path):
    from avibe_memory.process import SidecarOwnership
    home = tmp_path / "home"
    record_path, record = _sidecar_record(home)
    host = _SidecarHost(group_owned={452: 10.5})
    ownership = SidecarOwnership(record_path=record_path,
                                socket_path=Path(record["socket_path"]),
                                provider_root=Path(record["provider_root"]), _host=host)
    with pytest.raises(RuntimeError, match="did not exit"):
        ownership.retire_if_group_is_clear(451, 451)
    assert record_path.exists()
