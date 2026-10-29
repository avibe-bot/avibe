"""Active work that a host upgrade may interrupt."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

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


def _unsettled(counts: dict[str, Any]) -> int:
    return sum(
        value
        for key in ("queued", "running")
        if isinstance((value := counts.get(key)), int) and not isinstance(value, bool)
    )


async def count_upgrade_interruptions() -> UpgradeInterruptions:
    """Read independent Controller and Harness liveness sources.

    A source that cannot answer sets ``unknown`` instead of being represented as
    zero. Callers can still proceed after an explicit confirmation.
    """

    from storage.background import SQLiteBackgroundTaskStore
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

    store = None
    try:
        store = SQLiteBackgroundTaskStore(include_private_metadata=False)
        watches = _unsettled(store.count_runs_by_status(run_type="watch"))
        tasks = _unsettled(store.count_runs_by_status(run_type="scheduled"))
    except Exception:
        logger.warning("Could not count active Harness work before upgrade", exc_info=True)
        unknown = True
    finally:
        if store is not None:
            store.close()

    return UpgradeInterruptions(
        agents=agents,
        watches=watches,
        tasks=tasks,
        unknown=unknown,
    )
