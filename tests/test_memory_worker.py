from __future__ import annotations

import asyncio

from core.memory.everos import FakeMemoryProvider
from core.memory.store import BootRecovery
from core.memory.worker import MemoryWorker


def test_new_lease_activation_recovers_before_claiming() -> None:
    calls: list[tuple[str, str]] = []

    class Store:
        def recover_after_boot(self, *, lease_owner: str, clock) -> BootRecovery:
            del clock
            calls.append(("recover", lease_owner))
            return BootRecovery(reclaimed=0, interrupted_flushes=0)

        def next_processing_action(self):
            return None

        def list_flush_candidates(self, *, now: str, limit: int):
            del now, limit
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
