"""Transport deadlines must outlast the operations they wait on.

A client that gives up first turns a slow success into a reported failure while
the controller keeps working, and frees the caller to retry into the unfinished
operation. These assert the ordering against the real sources rather than
against copies of the numbers, so raising one bound without the other fails
here instead of in production.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.memory.module import PROVIDER_READ_TIMEOUT_SECONDS
from vibe.internal_client import (
    MEMORY_INSTALL_TIMEOUT_SECONDS,
    MEMORY_STATUS_TIMEOUT_SECONDS,
)


def _ui_dependency_poll_budget_seconds() -> float:
    """Read the Dependencies poll budget out of the UI client."""

    source = (Path(__file__).resolve().parents[1] / "ui" / "src" / "context" / "ApiContext.tsx").read_text(
        encoding="utf-8"
    )
    match = re.search(r"startAndPollDependencyInstall[\s\S]{0,600}?Date\.now\(\) \+ ([\d_]+)", source)
    assert match, "could not locate the dependency install poll deadline in ApiContext.tsx"
    return int(match.group(1).replace("_", "")) / 1000


def test_status_client_outlasts_the_controller_health_probe() -> None:
    # MemoryModule.status bounds its provider health check at
    # PROVIDER_READ_TIMEOUT_SECONDS. Abandoning the request earlier replaces the
    # structured "down" status with a generic transport failure, precisely
    # during the outage the Settings page exists to diagnose.
    assert MEMORY_STATUS_TIMEOUT_SECONDS > PROVIDER_READ_TIMEOUT_SECONDS


def test_install_client_covers_the_dependency_job_budget() -> None:
    # The UI polls the install job for its full budget. A shorter transport
    # deadline marks the job failed while the controller is still downloading,
    # extracting, and activating.
    budget = _ui_dependency_poll_budget_seconds()
    assert MEMORY_INSTALL_TIMEOUT_SECONDS >= budget - 30
    # ...but not beyond it: outliving the poller only moves the false failure to
    # the UI side.
    assert MEMORY_INSTALL_TIMEOUT_SECONDS <= budget
