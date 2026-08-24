from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from core.memory.confined_filesystem import ConfinedFilesystemError
from core.memory.process import (
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
    RecordedSidecarReaper,
)


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
