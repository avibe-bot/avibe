"""Focused runtime lifecycle tests for volatile Memory delivery."""

import asyncio
from pathlib import Path

import pytest

from core.memory.runtime import MemoryConfig, MemoryRuntime
from core.memory.store import MemoryStore


def _runtime(tmp_path: Path) -> MemoryRuntime:
    store = MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite", effective_home=tmp_path)
    return MemoryRuntime(
        MemoryConfig(enabled=True),
        store=store,
        effective_home=tmp_path,
    )


@pytest.mark.asyncio
async def test_session_lifecycle_offers_without_waiting_for_capture(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    started = asyncio.Event()

    async def operation() -> str:
        started.set()
        return "reset"

    result = await asyncio.wait_for(
        runtime.run_session_lifecycle(
            principal_id="u-" + "a" * 32,
            project_id="default",
            raw_session_id="session",
            operation=operation,
        ),
        timeout=1.0,
    )
    assert result == "reset"
    assert started.is_set()
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_drops_volatile_work(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    await runtime.close()
    assert runtime.closed


def test_runtime_store_is_identity_only(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with runtime._store._connection() as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not str(row[0]).startswith("sqlite_")
        }
    assert tables == {"memory_meta", "memory_projects"}
