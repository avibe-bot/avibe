from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import memory_legacy_cleanup as cleanup


def _sidecar_record(home: Path) -> tuple[Path, dict[str, object]]:
    provider_root = home / "memory" / "everos-root"
    socket_path = home / "memory" / ".rt" / "everos.sock"
    record: dict[str, object] = {
        "pid": 451,
        "create_time": 10.5,
        "starttime_ticks": 10.5,
        "process_group": None,
        "provider_root": str(provider_root),
        "socket_path": str(socket_path),
        "role": "sidecar",
        "python": "/runtime/bin/python",
    }
    path = home / "memory" / ".rt" / "everos.sidecar.json"
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    return path, record


def _identity(
    home: Path,
    *,
    command: tuple[str, ...] | None = None,
) -> cleanup._Identity:
    return cleanup._Identity(
        pid=451,
        stamp=10.5,
        wall_create_time=10.5,
        uid=os.getuid() if hasattr(os, "getuid") else None,
        command=command
        or (
            "/runtime/bin/python",
            "-m",
            "core.memory.sidecar",
            "--uds",
            str(home / "memory" / ".rt" / "everos.sock"),
        ),
        environment={
            "EVEROS_ROOT": str(home / "memory" / "everos-root"),
            "AVIBE_MEMORY_CHILD_ROLE": "sidecar",
        },
        process_group=None,
    )


def _sync_record(home: Path) -> Path:
    provider_root = home / "memory" / "everos-root"
    path = cleanup._root_artifact(provider_root, cleanup._SYNC_PREFIX, ".json")
    record = {
        "state": "finalized",
        "nonce": "a" * 64,
        "pid": 451,
        "create_time": 10.5,
        "process_group": 451,
        "parent_pid": 99999999,
        "parent_create_time": 8.25,
        "parent_uid": os.getuid() if hasattr(os, "getuid") else None,
        "provider_root": str(provider_root),
        "socket_path": str(home / "memory" / ".rt" / "everos.sock"),
        "role": "cascade_sync",
        "argv": [
            "/runtime/bin/python",
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
    return path


@pytest.mark.asyncio
async def test_core_reaper_retires_gone_released_sidecar(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path, _record = _sidecar_record(home)
    reaper = cleanup.ReleasedEverOSOrphanReconciler(
        provider_root=home / "memory" / "everos-root",
        effective_home=home,
    )

    await reaper.reconcile_orphans()

    assert not path.exists()


@pytest.mark.asyncio
async def test_core_reaper_retires_gone_released_sync(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = _sync_record(home)
    reaper = cleanup.ReleasedEverOSOrphanReconciler(
        provider_root=home / "memory" / "everos-root",
        effective_home=home,
    )

    await reaper.reconcile_orphans()

    assert not path.exists()


@pytest.mark.asyncio
async def test_core_reaper_signals_only_an_exact_released_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path, _record = _sidecar_record(home)
    observed: list[list[cleanup._Identity]] = []
    monkeypatch.setattr(cleanup, "_inspect", lambda _pid: _identity(home))
    monkeypatch.setattr(
        cleanup,
        "_signal_and_wait",
        lambda identities, _timeout: observed.append(identities),
    )
    reaper = cleanup.ReleasedEverOSOrphanReconciler(
        provider_root=home / "memory" / "everos-root",
        effective_home=home,
    )

    await reaper.reconcile_orphans()

    assert [[identity.pid for identity in batch] for batch in observed] == [[451]]
    assert not path.exists()


@pytest.mark.asyncio
async def test_core_reaper_preserves_record_when_identity_does_not_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path, _record = _sidecar_record(home)
    monkeypatch.setattr(
        cleanup,
        "_inspect",
        lambda _pid: _identity(home, command=("/usr/bin/python", "foreign.py")),
    )
    monkeypatch.setattr(
        cleanup,
        "_signal_and_wait",
        lambda *_args, **_kwargs: pytest.fail("foreign process must not be signalled"),
    )
    reaper = cleanup.ReleasedEverOSOrphanReconciler(
        provider_root=home / "memory" / "everos-root",
        effective_home=home,
    )

    with pytest.raises(RuntimeError, match="identity is unavailable"):
        await reaper.reconcile_orphans()

    assert path.exists()


def test_core_reaper_confines_released_provider_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="escaped its home"):
        cleanup.ReleasedEverOSOrphanReconciler(
            provider_root=tmp_path / "foreign",
            effective_home=home,
        )
