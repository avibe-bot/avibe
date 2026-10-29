"""Active work that a host upgrade may interrupt."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpgradeInterruptions:
    agents: int
    watches: int
    tasks: int
    unknown: bool = False

    @property
    def total(self) -> int:
        return self.agents + self.watches + self.tasks

    @property
    def requires_confirmation(self) -> bool:
        return self.unknown or self.total > 0

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "agents": self.agents,
            "watches": self.watches,
            "tasks": self.tasks,
            "total": self.total,
            "unknown": self.unknown,
        }


def _count_harness_interruptions() -> tuple[int, int]:
    """Count only active Harness rows on a worker thread."""

    from storage.background import SQLiteBackgroundTaskStore

    store = SQLiteBackgroundTaskStore(include_private_metadata=False)
    try:
        def active(run_type: str) -> int:
            return sum(
                store.count_runs(status=status, run_type=run_type)
                for status in ("queued", "running")
            )

        return active("watch"), active("scheduled")
    finally:
        store.close()


async def count_upgrade_interruptions() -> UpgradeInterruptions:
    """Read independent Controller and Harness liveness sources.

    A source that cannot answer sets ``unknown`` instead of being represented as
    zero. Callers can still proceed after an explicit confirmation.
    """

    from vibe import internal_client

    agents = watches = tasks = 0
    unknown = False
    try:
        snapshot = await internal_client.list_running_agents()
        body = snapshot.get("body") if isinstance(snapshot, dict) else None
        rows = body.get("agents") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            unknown = True
        else:
            agents = sum(
                1
                for row in rows
                if isinstance(row, dict) and row.get("state") == "active"
            )
    except (
        internal_client.InternalServerUnavailable,
        internal_client.InternalServerTimeout,
    ):
        unknown = True
    except Exception:
        logger.warning("Could not count active Agents before upgrade", exc_info=True)
        unknown = True

    try:
        watches, tasks = await asyncio.to_thread(_count_harness_interruptions)
    except Exception:
        logger.warning("Could not count active Harness work before upgrade", exc_info=True)
        unknown = True

    return UpgradeInterruptions(
        agents=agents,
        watches=watches,
        tasks=tasks,
        unknown=unknown,
    )
