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

from avibe_memory.confined_filesystem import PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS
from avibe_memory.module import PROVIDER_READ_TIMEOUT_SECONDS
from avibe_memory.everos import PROCESSING_PROBE_MAX_DEADLINE_SECONDS
from avibe_memory.processing_record import (
    PROCESSING_RECORD_TRANSPORT_MARGIN_SECONDS,
    PROCESSING_RECORD_WORK_TIMEOUT_SECONDS,
)
from avibe_memory.process import (
    _PROCESSING_PROBE_TIMEOUT_SECONDS,
    _STARTUP_TIMEOUT_SECONDS,
    _STOP_TIMEOUT_SECONDS,
)
from vibe.memory_contract import (
    MAX_AGENTIC_TIMEOUT_SECONDS,
    PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS,
)
ADD_TIMEOUT_SECONDS = 30.0
from vibe.internal_client import (
    MEMORY_FAILURES_TIMEOUT_SECONDS,
    MEMORY_INSTALL_TIMEOUT_SECONDS,
    MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS,
    MEMORY_READ_TIMEOUT_SECONDS,
    MEMORY_RECONCILE_TIMEOUT_SECONDS,
    MEMORY_SEARCH_TIMEOUT_SECONDS,
    MEMORY_STATUS_TIMEOUT_SECONDS,
    MEMORY_MAINTENANCE_TIMEOUT_SECONDS,
    memory_archive_session,
    memory_delete_data,
    memory_profile,
    memory_failures,
    memory_maintenance,
    memory_processing_record,
    memory_profile_sync,
    memory_repair,
    memory_search,
    memory_search_sync,
    memory_status,
    memory_status_sync,
    memory_wake,
)
from vibe.model_hub_client import MODEL_HUB_RPC_TIMEOUT_SECONDS
from vibe.runtime import SERVICE_SLOW_START_TIMEOUT_SECONDS


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
        memory_processing_record,
        memory_profile,
        memory_search,
        memory_status_sync,
        memory_profile_sync,
        memory_search_sync,
    ):
        assert inspect.signature(read).parameters["timeout"].default > PROVIDER_READ_TIMEOUT_SECONDS


def test_processing_record_client_covers_identity_journals_and_parallel_sources() -> None:
    sqlite_bound = PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS
    identity_lookup_bound = sqlite_bound
    initial_journal_observation_bound = 2 * sqlite_bound
    metadata_and_fresh_observation_bound = sqlite_bound + (2 * sqlite_bound)
    work_bound = (
        identity_lookup_bound
        + initial_journal_observation_bound
        + max(
            PROVIDER_READ_TIMEOUT_SECONDS,
                5 * sqlite_bound,
            metadata_and_fresh_observation_bound,
        )
    )
    assert PROCESSING_RECORD_WORK_TIMEOUT_SECONDS == work_bound
    assert (
        PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS
        == work_bound + PROCESSING_RECORD_TRANSPORT_MARGIN_SECONDS
    )
    assert (
        MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS
        == PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS
    )
    assert (
        inspect.signature(memory_processing_record).parameters["timeout"].default
        == MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS
    )
    assert (
        inspect.signature(memory_failures).parameters["timeout"].default
        == MEMORY_FAILURES_TIMEOUT_SECONDS
        == MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS
    )
    assert (
        inspect.signature(memory_maintenance).parameters["timeout"].default
        == MEMORY_MAINTENANCE_TIMEOUT_SECONDS
        == MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS
    )


def test_search_clients_outlast_capability_probe_and_provider_search() -> None:
    assert (
        MEMORY_SEARCH_TIMEOUT_SECONDS
        > PROVIDER_READ_TIMEOUT_SECONDS + MAX_AGENTIC_TIMEOUT_SECONDS + 1.0
    )
    for search in (memory_search, memory_search_sync):
        assert (
            inspect.signature(search).parameters["timeout"].default
            == MEMORY_SEARCH_TIMEOUT_SECONDS
        )


def test_session_archive_client_has_no_reporting_timeout() -> None:
    # The archive commit includes unbounded local filesystem/SQLite work. The
    # reporting transport must await that terminal write instead of returning a
    # retryable-looking failure mid-commit.
    assert "timeout" not in inspect.signature(memory_archive_session).parameters


def _reconcile_lifecycle_budget_seconds() -> float:
    return (
        _PROCESSING_PROBE_TIMEOUT_SECONDS
        + ADD_TIMEOUT_SECONDS
        + _STOP_TIMEOUT_SECONDS
        + _STARTUP_TIMEOUT_SECONDS
    )


def test_reconcile_client_outlasts_every_bounded_lifecycle_step() -> None:
    assert _PROCESSING_PROBE_TIMEOUT_SECONDS == PROCESSING_PROBE_MAX_DEADLINE_SECONDS
    assert MEMORY_RECONCILE_TIMEOUT_SECONDS > _reconcile_lifecycle_budget_seconds()


def test_memory_operations_have_no_reporting_timeout() -> None:
    for operation in (memory_wake, memory_repair, memory_delete_data):
        assert "timeout" not in inspect.signature(operation).parameters


def test_dependency_poller_outlasts_every_install_transport() -> None:
    # The UI must keep polling past both direct Memory installation and CPA's
    # sequential controller-readiness plus install-RPC bounds.
    budget = _ui_dependency_poll_budget_seconds()
    assert MEMORY_INSTALL_TIMEOUT_SECONDS <= budget
    assert SERVICE_SLOW_START_TIMEOUT_SECONDS + MODEL_HUB_RPC_TIMEOUT_SECONDS < budget
