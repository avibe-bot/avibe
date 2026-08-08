"""Transport deadlines must outlast the operations they wait on.

A client that gives up first turns a slow success into a reported failure while
the controller keeps working, and frees the caller to retry into the unfinished
operation. These assert the ordering against the real sources rather than
against copies of the numbers, so raising one bound without the other fails
here instead of in production.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from core.memory.module import (
    CLEAR_CLEANUP_TIMEOUT_SECONDS,
    CLEAR_DRAIN_TIMEOUT_SECONDS,
    PROVIDER_READ_TIMEOUT_SECONDS,
)
from core.memory.process import (
    _PROCESSING_PROBE_TIMEOUT_SECONDS,
    _STARTUP_TIMEOUT_SECONDS,
    _STOP_TIMEOUT_SECONDS,
)
from core.memory.worker import ADD_TIMEOUT_SECONDS
from vibe.internal_client import (
    MEMORY_CLEAR_TIMEOUT_SECONDS,
    MEMORY_FINAL_FLUSH_TIMEOUT_SECONDS,
    MEMORY_INSTALL_TIMEOUT_SECONDS,
    MEMORY_READ_TIMEOUT_SECONDS,
    MEMORY_RECONCILE_TIMEOUT_SECONDS,
    MEMORY_SEARCH_TIMEOUT_SECONDS,
    MEMORY_STATUS_TIMEOUT_SECONDS,
    memory_profile,
    memory_final_flush,
    memory_profile_sync,
    memory_restart,
    memory_search,
    memory_search_sync,
    memory_status,
    memory_status_sync,
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


def test_all_memory_read_clients_outlast_provider_reads() -> None:
    assert MEMORY_READ_TIMEOUT_SECONDS > PROVIDER_READ_TIMEOUT_SECONDS
    for read in (
        memory_status,
        memory_profile,
        memory_search,
        memory_status_sync,
        memory_profile_sync,
        memory_search_sync,
    ):
        assert inspect.signature(read).parameters["timeout"].default > PROVIDER_READ_TIMEOUT_SECONDS


def test_search_clients_outlast_capability_probe_and_provider_search() -> None:
    assert MEMORY_SEARCH_TIMEOUT_SECONDS > 2 * PROVIDER_READ_TIMEOUT_SECONDS
    for search in (memory_search, memory_search_sync):
        assert (
            inspect.signature(search).parameters["timeout"].default
            == MEMORY_SEARCH_TIMEOUT_SECONDS
        )


def test_final_flush_client_outlasts_the_controller_deadline() -> None:
    assert MEMORY_FINAL_FLUSH_TIMEOUT_SECONDS > 5.0
    assert (
        inspect.signature(memory_final_flush).parameters["timeout"].default
        == MEMORY_FINAL_FLUSH_TIMEOUT_SECONDS
    )


def _reconcile_lifecycle_budget_seconds() -> float:
    return (
        _PROCESSING_PROBE_TIMEOUT_SECONDS
        + ADD_TIMEOUT_SECONDS
        + _STOP_TIMEOUT_SECONDS
        + _STARTUP_TIMEOUT_SECONDS
    )


def test_reconcile_client_outlasts_every_bounded_lifecycle_step() -> None:
    assert MEMORY_RECONCILE_TIMEOUT_SECONDS > _reconcile_lifecycle_budget_seconds()


def test_restart_client_has_no_reporting_timeout() -> None:
    assert "timeout" not in inspect.signature(memory_restart).parameters


def test_clear_client_outlasts_clear_and_enabled_reconciliation() -> None:
    clear_lifecycle_budget = (
        CLEAR_DRAIN_TIMEOUT_SECONDS
        + CLEAR_CLEANUP_TIMEOUT_SECONDS
        + _reconcile_lifecycle_budget_seconds()
    )
    assert MEMORY_CLEAR_TIMEOUT_SECONDS > clear_lifecycle_budget


def test_install_client_covers_the_dependency_job_budget() -> None:
    # The UI polls the install job for its full budget. A shorter transport
    # deadline marks the job failed while the controller is still downloading,
    # extracting, and activating.
    budget = _ui_dependency_poll_budget_seconds()
    assert MEMORY_INSTALL_TIMEOUT_SECONDS >= budget - 30
    # ...but not beyond it: outliving the poller only moves the false failure to
    # the UI side.
    assert MEMORY_INSTALL_TIMEOUT_SECONDS <= budget
