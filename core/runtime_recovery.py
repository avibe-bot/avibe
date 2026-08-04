"""PR3 supervisor handlers that re-enter existing guarded durable owners."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from core.runtime_work import RuntimeWorkItem

if TYPE_CHECKING:
    from core.session_turns import RuntimeDeliveryObservation, SessionTurnManager
    from storage.background import SQLiteBackgroundTaskStore


class SessionDeliveryRecoveryHandler:
    def __init__(self, manager: "SessionTurnManager") -> None:
        self.manager = manager

    def scan(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None = None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        observations, has_more = self.manager.scan_runtime_delivery_recovery(
            limit=limit,
            occupied=occupied,
            cursor=cursor,
        )
        return [
            RuntimeWorkItem(observation.session_id, observation)
            for observation in observations
        ], has_more

    async def process(self, item: RuntimeWorkItem) -> bool:
        return await self.manager.recover_runtime_delivery_observation(
            item.observation
        )


class FallbackRequestRecoveryHandler:
    """Recover only claimed pre-execution fallback Runs; never admit queued Runs."""

    def __init__(
        self,
        store: "SQLiteBackgroundTaskStore",
        *,
        live_claims: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self.store = store
        self._live_claims = live_claims or frozenset

    def scan(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None = None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        live_claims = self._live_claims()
        rows, has_more = self.store.scan_claimed_pre_execution_runs(
            limit=limit,
            occupied=occupied | live_claims,
            cursor=cursor,
        )
        return [
            RuntimeWorkItem(str(row["id"]), row)
            for row in rows
        ], has_more

    async def process(self, item: RuntimeWorkItem) -> bool:
        return await asyncio.to_thread(self.process_sync, item)

    def process_sync(self, item: RuntimeWorkItem) -> bool:
        row = item.observation
        if str(row["id"]) in self._live_claims():
            return True
        return self.store.recover_claimed_pre_execution_run(
            run_id=str(row["id"]),
            expected_status=str(row["status"]),
            expected_updated_at=str(row["updated_at"]),
        )
