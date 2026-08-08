from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from core.memory.everos import FakeMemoryProvider
from core.memory.worker import MemoryWorker


def test_new_lease_activation_recovers_before_claiming() -> None:
    calls: list[tuple[str, str]] = []

    class Store:
        def recover_after_boot(self, *, lease_owner: str, clock) -> SimpleNamespace:
            del clock
            calls.append(("recover", lease_owner))
            return SimpleNamespace(not_attempted_sessions=(), interrupted_flushes=0)

        def get_meta(self):
            return None

        def list_due_flush_sessions(self, *, now: str):
            del now
            return ()

        def claim_due(self, *, lease_owner: str, now: str):
            del now
            calls.append(("claim", lease_owner))
            return None

    worker = MemoryWorker(
        store=Store(),
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        boot_id="old-lease",
    )
    worker.begin_new_lease_activation()

    assert asyncio.run(worker.drain_once()) == 0
    assert [kind for kind, _lease in calls] == ["recover", "claim"]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != "old-lease"


def test_cancelled_worker_waits_for_exact_store_call() -> None:
    entered = threading.Event()
    release = threading.Event()

    class Store:
        def recover_after_boot(self, *, lease_owner: str, clock) -> SimpleNamespace:
            del lease_owner, clock
            entered.set()
            release.wait(timeout=2.0)
            return SimpleNamespace(not_attempted_sessions=(), interrupted_flushes=0)

    worker = MemoryWorker(
        store=Store(),
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        boot_id="lease",
    )

    async def run() -> None:
        draining = asyncio.create_task(worker.drain_once())
        assert await asyncio.to_thread(entered.wait, 1.0)
        draining.cancel()
        await asyncio.sleep(0)
        assert draining.done() is False
        draining.cancel()
        await asyncio.sleep(0)
        assert draining.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await draining

    asyncio.run(run())
