from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.memory.clear_intent import ClearIntentStore, DEFAULT_CLEAR_SURFACES
from core.memory.maintenance import MemoryMaintenance
from core.memory.store import MemoryStore


class _Port:
    def __init__(self, home: Path) -> None:
        self.deleted: list[tuple[str, int]] = []
        self.resumed = 0
        self.entered = 0
        self.left = 0
        self.home = home

    def exclusive_fence(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fence():
            yield

        return fence()

    def boot_recovery_fence(self):
        return self.exclusive_fence()

    def state(self):
        return SimpleNamespace(artifact_installing=False)

    def enter_maintenance(self):
        self.entered += 1

    def leave_maintenance(self):
        self.left += 1

    async def pause_claims(self):
        return None

    def resume_claims(self):
        return None

    async def quiesce(self, _strict: bool):
        return None

    async def resume(self):
        self.resumed += 1

    async def delete_surface(self, surface, target_epoch: int):
        self.deleted.append((surface.surface, target_epoch))

    def restore_completed(self):
        return None


def _maintenance(tmp_path: Path) -> tuple[MemoryMaintenance, _Port]:
    port = _Port(tmp_path)
    maintenance = MemoryMaintenance(
        MemoryStore(),
        effective_home=tmp_path,
        runtime=port,
    )
    return maintenance, port


@pytest.mark.asyncio
async def test_clear_writes_marker_and_repeats_four_surfaces(tmp_path: Path):
    maintenance, port = _maintenance(tmp_path)

    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "completed"
    assert [surface for surface, _epoch in port.deleted] == [
        surface.surface for surface in DEFAULT_CLEAR_SURFACES
    ]
    assert len({epoch for _surface, epoch in port.deleted}) == 1
    assert ClearIntentStore(tmp_path).load() is None
    assert port.resumed == 1


@pytest.mark.asyncio
async def test_failed_clear_persists_failed_projection_and_boot_retries(tmp_path: Path):
    maintenance, port = _maintenance(tmp_path)

    async def fail(surface, target_epoch: int):
        port.deleted.append((surface.surface, target_epoch))
        if surface.surface == "provider":
            raise RuntimeError("provider unavailable")

    port.delete_surface = fail
    result = await maintenance.clear(operator_ref="user-1")

    assert result.status == "failed"
    intent = ClearIntentStore(tmp_path).load()
    assert intent is not None
    assert intent.state == "failed"
    assert result.clear_in_progress is not None
    assert result.clear_in_progress.state == "failed"

    port.delete_surface = _Port.delete_surface.__get__(port, _Port)
    assert await maintenance.reconcile_pending() is True
    assert ClearIntentStore(tmp_path).load() is None


def test_corrupt_marker_is_fail_closed(tmp_path: Path):
    marker = tmp_path / "state/memory/clear-intent.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("not json", encoding="utf-8")
    maintenance, _port = _maintenance(tmp_path)

    observation = maintenance._read_projection()

    assert observation is not None
    assert observation.state == "failed"
    assert observation.error_code == "memory_clear_marker_unreadable"
