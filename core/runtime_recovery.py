"""PR3 supervisor handlers that re-enter existing guarded durable owners."""

from __future__ import annotations

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

    def __init__(self, store: "SQLiteBackgroundTaskStore") -> None:
        self.store = store

    def scan(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None = None,
    ) -> tuple[list[RuntimeWorkItem], bool]:
        rows, has_more = self.store.scan_claimed_pre_execution_runs(
            limit=limit,
            occupied=occupied,
            cursor=cursor,
        )
        return [
            RuntimeWorkItem(str(row["id"]), row)
            for row in rows
        ], has_more

    async def process(self, item: RuntimeWorkItem) -> bool:
        row = item.observation
        return self.store.recover_claimed_pre_execution_run(
            run_id=str(row["id"]),
            expected_status=str(row["status"]),
            expected_updated_at=str(row["updated_at"]),
        )
