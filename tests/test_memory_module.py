from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import paths
from core.memory.everos import (
    FakeMemoryProvider,
    FlushRejected,
    FlushSucceeded,
    FlushUnknown,
    MemoryProviderFailure,
    MemoryProviderSystemFailure,
)
from core.memory.module import (
    MAX_CAPTURE_IDENTIFIER_BYTES,
    MAX_CAPTURE_TEXT_BYTES,
    MAX_QUERY_BYTES,
    MIN_FREE_DISK_BYTES,
    MemoryModule,
)
from core.memory.store import MemoryStore, TERMINAL_TOMBSTONE_RETENTION
from core.memory.types import (
    CaptureAccepted,
    CaptureDuplicate,
    CaptureSkipped,
    ClearCompleted,
    CLOSED_MEMORY_ERROR_CODES,
    MemoryItem,
    MemoryFailureLogEntry,
    MemoryItems,
    OperationFailed,
)
from core.memory.worker import BREAKER_RETRY_SECONDS, MemoryWorker, SYSTEM_PAUSE_SECONDS


ROOT_SENTINEL_FILENAME = ".avibe-memory-root.json"


def _store_path(scope: Path) -> Path:
    return paths.get_state_dir() / "memory-tests" / scope.name / "memory.sqlite"


def _write_owned_provider_root(root: Path, store: MemoryStore) -> None:
    meta = store.ensure_meta()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    sentinel = root / ROOT_SENTINEL_FILENAME
    sentinel.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider_root_id": meta.provider_root_id,
                "provider_id": "everos",
                "provider_root_format": "slice1",
                "created_by_artifact_fingerprint": "slice1-core",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    sentinel.chmod(0o600)


def _request(
    *,
    source: str = "source-1",
    session: str = "conversation-1",
    text: str = "remember this",
    occurred_at_ms: int = 1_000,
):
    from core.memory.types import CaptureRequest

    return CaptureRequest(
        source_message_id=source,
        session_id=session,
        text=text,
        occurred_at_ms=occurred_at_ms,
    )


def _module(
    tmp_path: Path,
    *,
    provider: FakeMemoryProvider | None = None,
    enabled=True,
    disk_free_bytes=None,
    owned_provider_root: bool = False,
    **kwargs,
) -> tuple[MemoryModule, MemoryStore, FakeMemoryProvider]:
    store = MemoryStore(_store_path(tmp_path))
    if owned_provider_root:
        provider_root = kwargs.pop("provider_root", tmp_path / "provider-root")
        _write_owned_provider_root(provider_root, store)
        kwargs["provider_root"] = provider_root
    fake = provider or FakeMemoryProvider()
    module = MemoryModule(
        store,
        fake,
        enabled=enabled,
        disk_free_bytes=disk_free_bytes or (lambda: MIN_FREE_DISK_BYTES),
        **kwargs,
    )
    return module, store, fake


async def test_disabled_methods_are_closed_and_status_remains_readable(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path, enabled=False)

    capture = await module.capture(_request())
    search = await module.search("query")
    profile = await module.profile()
    status = await module.status()

    assert capture == CaptureSkipped(reason="memory_disabled")
    assert search == OperationFailed(error="memory_disabled")
    assert profile == OperationFailed(error="memory_disabled")
    assert status.state == "disabled"
    assert store.get_meta() is None


async def test_capture_excludes_active_clear_and_status_prioritizes_clearing(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    store.begin_clear()

    receipt = await module.capture(_request())
    status = await module.status()

    assert receipt == CaptureSkipped(reason="memory_clear_failed")
    assert status.state == "clearing"
    assert store.list_queue_rows() == ()


async def test_capture_normalizes_deduplicates_and_never_persists_raw_ids(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    request = _request(
        source="raw-source-id-canary",
        session="raw-session-id-canary",
        text="Cafe\u0301\r\nmessage",
        occurred_at_ms=5_000,
    )

    first = await module.capture(request)
    duplicate = await module.capture(request)
    rows = store.list_queue_rows()

    assert first == CaptureAccepted()
    assert duplicate == CaptureDuplicate()
    assert len(rows) == 1
    assert rows[0].payload_text == "Café\nmessage"
    assert rows[0].session_id.startswith("src--")
    assert "raw-session-id-canary" not in rows[0].session_id
    with sqlite3.connect(store.path) as conn:
        dump = "\n".join(str(value) for row in conn.execute("SELECT * FROM memory_capture_queue") for value in row)
    assert "raw-source-id-canary" not in dump
    assert "raw-session-id-canary" not in dump


async def test_capture_validation_and_disk_rejections_increment_only_missed(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path, disk_free_bytes=lambda: 0)

    blank = await module.capture(_request(text="\r\n  \r"))
    command = await module.capture(_request(source="source-2", text="/memory search private"))
    too_large = await module.capture(_request(source="source-3", text="x" * (MAX_CAPTURE_TEXT_BYTES + 1)))
    oversized_id = await module.capture(
        _request(source="x" * (MAX_CAPTURE_IDENTIFIER_BYTES + 1), text="content")
    )
    disk = await module.capture(_request(source="source-4", text="content"))

    assert blank == CaptureSkipped(reason="memory_invalid_input")
    assert command == CaptureSkipped(reason="memory_invalid_input")
    assert too_large == CaptureSkipped(reason="memory_input_too_large")
    assert oversized_id == CaptureSkipped(reason="memory_invalid_input")
    assert disk == CaptureSkipped(reason="memory_low_disk_space")
    assert store.ensure_meta().missed_count == 5
    assert store.ensure_meta().last_error == "memory_low_disk_space"
    assert store.list_queue_rows() == ()


async def test_provider_timestamp_is_allocated_once_and_reused_after_restart(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    first_request = _request(source="first", occurred_at_ms=5_000)
    second_request = _request(source="second", occurred_at_ms=5_000)
    assert await module.capture(first_request) == CaptureAccepted()
    assert await module.capture(second_request) == CaptureAccepted()
    assert await module.capture(first_request) == CaptureDuplicate()
    rows = store.list_queue_rows()
    assert [row.provider_timestamp_ms for row in rows] == [5_000, 5_001]

    retry_module, retry_store, retry_provider = _module(tmp_path / "retry")
    assert await retry_module.capture(_request(source="retry", occurred_at_ms=8_000)) == CaptureAccepted()
    original = retry_store.list_queue_rows()[0].provider_timestamp_ms
    retry_provider.ingest_failures.append(MemoryProviderFailure())
    current = datetime(2026, 1, 1, tzinfo=UTC)
    first_worker = MemoryWorker(
        store=retry_store,
        provider=retry_provider,
        enabled=lambda: True,
        boot_id="first-boot",
        now=lambda: current,
    )
    assert await first_worker.drain_once() == 1
    current += timedelta(seconds=31)
    restarted_worker = MemoryWorker(
        store=retry_store,
        provider=retry_provider,
        enabled=lambda: True,
        boot_id="second-boot",
        now=lambda: current,
    )
    assert await restarted_worker.drain_once() == 1
    assert retry_provider.captures[-1].provider_timestamp_ms == original


async def test_worker_delivers_and_scrubs_payload(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(text="secret queue payload")) == CaptureAccepted()
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="boot")

    assert await worker.drain_once() == 1
    row = store.list_queue_rows()[0]

    assert row.state == "delivered"
    assert row.payload_text is None
    assert row.flush_observation == "succeeded"
    assert provider.captures[0].text == "secret queue payload"
    assert provider.flushes == [row.session_id]
    assert store.queue_stats().queue_plaintext_bytes == 0
    assert store.ensure_meta().last_success_at is not None


async def test_flush_unknown_keeps_delivery_terminal_and_opens_processing_breaker(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="one")) == CaptureAccepted()
    assert await module.capture(_request(source="two")) == CaptureAccepted()
    provider.flush_results.append(FlushUnknown("timeout"))
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="boot",
        now=lambda: current,
    )

    assert await worker.drain(max_rows=2) == 1
    first, second = store.list_queue_rows()
    assert (first.state, first.attempts, first.flush_observation) == ("delivered", 0, "unknown")
    assert second.state == "pending"
    fault = store.ensure_meta()
    assert fault.processing_fault_kind == "engine"
    assert fault.processing_fault_since == "2026-01-01T00:00:00.000Z"
    assert fault.processing_alert_active is True


async def test_processing_breaker_survives_restart_and_half_open_admits_one_capture(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    for source in ("one", "two", "three"):
        assert await module.capture(_request(source=source)) == CaptureAccepted()
    provider.flush_results.extend(
        [
            FlushRejected("failed", "INTERNAL_ERROR", server_fault=True),
            FlushSucceeded("recovered", "extracted"),
            FlushSucceeded("normal", "extracted"),
        ]
    )
    current = datetime(2026, 1, 1, tzinfo=UTC)
    first_worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="first",
        now=lambda: current,
    )
    assert await first_worker.drain(max_rows=3) == 1

    restarted = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="second",
        now=lambda: current,
    )
    assert await restarted.drain(max_rows=3) == 0
    assert len(provider.captures) == 1

    current += timedelta(seconds=BREAKER_RETRY_SECONDS + 1)
    assert await restarted.drain(max_rows=3) == 1
    assert len(provider.captures) == 2
    assert store.ensure_meta().processing_fault_since is None
    assert store.list_queue_rows()[2].state == "pending"

    assert await restarted.drain(max_rows=3) == 1
    assert store.list_queue_rows()[2].state == "delivered"


async def test_processing_breaker_alerts_once_and_emits_recovery_edge(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    for source in ("one", "two", "three"):
        assert await module.capture(_request(source=source)) == CaptureAccepted()
    provider.flush_results.extend(
        [
            FlushRejected("first", "INTERNAL_ERROR", server_fault=True),
            FlushUnknown("transport"),
            FlushSucceeded("recovered", "extracted"),
        ]
    )
    current = datetime(2026, 1, 1, tzinfo=UTC)
    events: list[tuple[str, str | None, str, int]] = []

    async def record_event(event: str, kind: str | None, occurred_at: str, queued: int) -> bool:
        events.append((event, kind, occurred_at, queued))
        return True

    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="boot",
        now=lambda: current,
        processing_event=record_event,
    )

    assert await worker.drain(max_rows=3) == 1
    current += timedelta(seconds=BREAKER_RETRY_SECONDS + 1)
    assert await worker.drain(max_rows=3) == 1
    current += timedelta(seconds=BREAKER_RETRY_SECONDS + 1)
    assert await worker.drain(max_rows=3) == 1

    assert [event[:2] for event in events] == [("fault", "engine"), ("recovered", None)]
    assert events[0][3] == 2
    assert events[1][3] == 0


async def test_failed_processing_alert_is_retried_on_next_activation(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="one")) == CaptureAccepted()
    provider.flush_results.append(FlushUnknown("transport"))
    current = datetime(2026, 1, 1, tzinfo=UTC)
    attempts: list[str] = []

    async def fail_alert(_event: str, _kind: str | None, occurred_at: str, _queued: int) -> bool:
        attempts.append(occurred_at)
        return False

    first = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        now=lambda: current,
        processing_event=fail_alert,
    )
    assert await first.drain_once() == 1
    assert store.ensure_meta().processing_alert_active is False

    async def deliver_alert(_event: str, _kind: str | None, occurred_at: str, _queued: int) -> bool:
        attempts.append(occurred_at)
        return True

    restarted = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        now=lambda: current,
        processing_event=deliver_alert,
    )
    assert await restarted.drain_once() == 0
    assert store.ensure_meta().processing_alert_active is True
    assert attempts == ["2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"]


async def test_half_open_4xx_does_not_refresh_breaker_anchor(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="one")) == CaptureAccepted()
    assert await module.capture(_request(source="two")) == CaptureAccepted()
    provider.flush_results.extend(
        [
            FlushRejected("server", "INTERNAL_ERROR", server_fault=True),
            FlushRejected("content", "INVALID_INPUT", server_fault=False),
        ]
    )
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, now=lambda: current)

    assert await worker.drain(max_rows=2) == 1
    opened_at = store.ensure_meta().processing_fault_since
    current += timedelta(seconds=BREAKER_RETRY_SECONDS + 1)
    assert await worker.drain(max_rows=2) == 1

    assert store.ensure_meta().processing_fault_since == opened_at
    assert store.list_queue_rows()[1].flush_observation == "rejected"


async def test_activation_recovery_flushes_not_attempted_without_readding_capture(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    row = store.claim_due(lease_owner="old", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.mark_delivered(
        row,
        lease_owner="old",
        now="2026-01-01T00:00:01.000Z",
        add_request_id="ack",
    )

    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="new")
    assert await worker.drain_once() == 0
    recovered = store.list_queue_rows()[0]
    assert recovered.flush_observation == "succeeded"
    assert provider.captures == []
    assert provider.flushes == [recovered.session_id]


async def test_activation_recovery_turns_interrupted_flush_unknown_and_opens_breaker(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    row = store.claim_due(lease_owner="old", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.mark_delivered(row, lease_owner="old", now="2026-01-01T00:00:01.000Z")
    assert store.mark_flush_in_flight(row.session_id) == 1

    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="new",
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert await worker.drain_once() == 0
    recovered = store.list_queue_rows()[0]
    assert recovered.flush_observation == "unknown"
    assert store.ensure_meta().processing_fault_since is not None


async def test_activation_finishes_interrupted_breaker_classification_before_claiming(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    opened_at = "2026-01-01T00:00:00.000Z"
    assert store.open_processing_fault(now=opened_at)
    events: list[tuple[str, str | None, str, int]] = []

    async def record_event(event: str, kind: str | None, occurred_at: str, queued: int) -> bool:
        events.append((event, kind, occurred_at, queued))
        return True

    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        processing_event=record_event,
    )

    assert await worker.drain_once() == 0
    meta = store.ensure_meta()
    assert meta.processing_fault_kind == "engine"
    assert meta.processing_alert_active is True
    assert events == [("fault", "engine", opened_at, 1)]
    assert provider.captures == []


async def test_worker_retries_message_failures_then_marks_dead_and_scrubs(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    provider.ingest_failures.extend(
        [
            MemoryProviderFailure(),
            MemoryProviderFailure(),
            MemoryProviderFailure(),
        ]
    )
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="boot",
        now=lambda: current,
    )

    await worker.drain_once()
    first = store.list_queue_rows()[0]
    assert (first.state, first.attempts, first.payload_text) == ("pending", 1, "remember this")
    current += timedelta(seconds=31)
    await worker.drain_once()
    second = store.list_queue_rows()[0]
    assert (second.state, second.attempts, second.payload_text) == ("pending", 2, "remember this")
    current += timedelta(minutes=2, seconds=1)
    await worker.drain_once()
    dead = store.list_queue_rows()[0]
    assert (dead.state, dead.attempts, dead.payload_text) == ("dead", 3, None)
    assert dead.last_error == "memory_processing_failed"


async def test_system_outage_pauses_claims_without_consuming_attempts(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="one")) == CaptureAccepted()
    assert await module.capture(_request(source="two")) == CaptureAccepted()
    provider.ingest_failures.append(MemoryProviderSystemFailure())
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="boot")

    assert await worker.drain(max_rows=2) == 1
    rows = store.list_queue_rows()
    assert [row.state for row in rows] == ["pending", "pending"]
    assert [row.attempts for row in rows] == [0, 0]
    assert store.ensure_meta().last_error == "memory_sidecar_unavailable"


async def test_ambiguous_failure_uses_health_to_preserve_attempt_budget(tmp_path: Path) -> None:
    class AmbiguousOutage(FakeMemoryProvider):
        async def add(self, capture):
            del capture
            self.healthy = False
            raise RuntimeError("provider-body-canary")

    provider = AmbiguousOutage()
    module, store, _provider = _module(tmp_path, provider=provider)
    assert await module.capture(_request()) == CaptureAccepted()
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="boot")

    await worker.drain_once()
    row = store.list_queue_rows()[0]

    assert row.state == "pending"
    assert row.attempts == 0
    assert row.last_error == "memory_sidecar_unavailable"


async def test_processing_endpoint_outage_pauses_claims_without_consuming_attempts(tmp_path: Path) -> None:
    """Sidecar is healthy but the processing (LLM/embedding) endpoint is down.

    This is the r3 #1 case: a reachable sidecar whose model endpoint never responds
    must be treated as a SYSTEM outage (pause + preserve attempts), not a poison row
    that burns its retry budget.
    """
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="one")) == CaptureAccepted()
    # An ambiguous failure (plain MemoryProviderFailure) routes through the
    # disambiguation probe; with the processing endpoint down it must be classified
    # as a system outage, preserving attempts.
    provider.ingest_failures.append(MemoryProviderFailure("memory_processing_failed"))
    provider.processing_healthy_flag = False  # sidecar up, endpoint down
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="boot")

    # The claim happens (sidecar health gate passes), then ingest fails and the
    # disambiguation probe finds the processing endpoint down -> system pause. The
    # row returns to pending with attempts preserved.
    await worker.drain(max_rows=1)
    row = store.list_queue_rows()[0]
    assert row.state == "pending"
    assert row.attempts == 0  # not consumed by the endpoint outage


async def test_message_failure_when_endpoints_healthy_consumes_attempt(tmp_path: Path) -> None:
    """Both sidecar and processing endpoints healthy, but this row fails to ingest.

    This is the disambiguation's positive side: a genuine message failure (system
    healthy) must increment attempts so a poison row eventually deads and unblocks
    the queue (tech §10.3).
    """
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="one")) == CaptureAccepted()
    provider.ingest_failures.append(MemoryProviderFailure("memory_processing_failed"))
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="boot")

    await worker.drain_once()
    row = store.list_queue_rows()[0]
    assert row.state == "pending"
    assert row.attempts == 1  # message failure charged to the row
    assert row.last_error == "memory_processing_failed"


async def test_old_boot_processing_row_is_reclaimed_for_at_least_once_delivery(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    claimed = store.claim_due(lease_owner="old-boot", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None and claimed.state == "processing"
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="new-boot")

    assert await worker.drain_once() == 1
    row = store.list_queue_rows()[0]
    assert row.state == "delivered"
    assert len(provider.captures) == 1


async def test_search_and_profile_enforce_bounds_and_return_closed_errors(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(
        search_items=(MemoryItem(kind="fact", text="bounded fact", date="2026-01-01"),),
        profile_items=(MemoryItem(kind="profile", text="bounded profile"),),
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.search("query") == MemoryItems(items=provider.search_items)
    assert await module.profile() == MemoryItems(items=provider.profile_items)
    assert await module.search("x" * (MAX_QUERY_BYTES + 1)) == OperationFailed(error="memory_input_too_large")
    provider.search_items = tuple(MemoryItem(kind="fact", text=str(index)) for index in range(9))
    assert await module.search("query") == OperationFailed(error="memory_provider_response_invalid")
    provider.search_items = ()
    provider.search_failure = RuntimeError("provider-search-body-canary")
    result = await module.search("query")
    assert result == OperationFailed(error="memory_processing_failed")
    assert "provider-search-body-canary" not in repr(result)


async def test_clear_is_idempotent_and_interrupted_clear_recovers_on_next_module(tmp_path: Path) -> None:
    calls = 0

    async def clear_provider_data() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("clear-body-canary")

    provider_root = tmp_path / "provider-root"
    module, store, provider = _module(
        tmp_path,
        clear_provider_data=clear_provider_data,
        owned_provider_root=True,
        provider_root=provider_root,
    )
    assert await module.capture(_request()) == CaptureAccepted()
    assert await module.clear() == OperationFailed(error="memory_clear_failed")
    assert store.ensure_meta().clear_in_progress is True
    assert len(store.list_queue_rows()) == 1

    recovered = MemoryModule(
        store,
        provider,
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        clear_provider_data=clear_provider_data,
        provider_root=provider_root,
    )
    status = await recovered.status()
    assert calls == 2
    assert status.state == "ready"
    assert store.ensure_meta().clear_in_progress is False
    assert store.list_queue_rows() == ()

    first = await recovered.clear()
    second = await recovered.clear()
    assert isinstance(first, ClearCompleted)
    assert isinstance(second, ClearCompleted)
    assert store.list_queue_rows() == ()


async def test_status_reports_clearing_while_clear_waits_on_provider_data(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def clear_provider_data() -> None:
        entered.set()
        await release.wait()

    module, _store, _provider = _module(
        tmp_path,
        clear_provider_data=clear_provider_data,
        owned_provider_root=True,
    )
    clear_task = asyncio.create_task(module.clear())
    await entered.wait()
    assert (await module.status()).state == "clearing"
    release.set()
    assert isinstance(await clear_task, ClearCompleted)


async def test_status_precedence(tmp_path: Path) -> None:
    ready, _store, _provider = _module(tmp_path / "ready")
    assert (await ready.status()).state == "ready"

    syncing, syncing_store, _provider = _module(tmp_path / "syncing")
    assert await syncing.capture(_request()) == CaptureAccepted()
    syncing_status = await syncing.status()
    assert syncing_status.state == "syncing"
    assert syncing_status.pending == 1
    assert syncing_status.awaiting_receipt == 0

    syncing_worker = MemoryWorker(
        store=syncing_store,
        provider=_provider,
        enabled=lambda: True,
        boot_id="syncing-worker",
    )
    assert await syncing_worker.drain_once() == 1
    ready_status = await syncing.status()
    assert ready_status.state == "ready"
    assert ready_status.succeeded == 1
    assert ready_status.last_flush_observation == "succeeded"
    assert ready_status.last_flush_status == "extracted"

    degraded, degraded_store, _provider = _module(tmp_path / "degraded")
    degraded_store.set_last_error("memory_processing_failed")
    assert (await degraded.status()).state == "degraded"

    down, _store, _provider = _module(tmp_path / "down", provider=FakeMemoryProvider(healthy=False))
    assert (await down.status()).state == "down"

    starting, _store, _provider = _module(tmp_path / "starting", starting=True)
    assert (await starting.status()).state == "starting"

    runtime_error, _store, _provider = _module(
        tmp_path / "runtime-error",
        runtime_error="memory_runtime_missing",
    )
    assert (await runtime_error.status()).state == "error"

    disabled, _store, _provider = _module(tmp_path / "disabled", enabled=False)
    assert (await disabled.status()).state == "disabled"

    clearing, clearing_store, _provider = _module(tmp_path / "clearing")
    clearing_store.begin_clear()
    assert (await clearing.status()).state == "clearing"


async def test_latest_flush_success_closes_stale_timeout_but_keeps_dead_history(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="failed", text="failed delivery")) == CaptureAccepted()
    provider.ingest_failures.extend(
        [
            MemoryProviderFailure("memory_provider_timeout"),
            MemoryProviderFailure("memory_provider_timeout"),
            MemoryProviderFailure("memory_provider_timeout"),
        ]
    )
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="latest-status-worker",
        now=lambda: current,
    )

    await worker.drain_once()
    current += timedelta(seconds=31)
    await worker.drain_once()
    current += timedelta(minutes=2, seconds=1)
    await worker.drain_once()
    assert store.list_queue_rows()[0].state == "dead"

    assert await module.capture(_request(source="succeeded", text="successful delivery")) == CaptureAccepted()
    assert await worker.drain_once() == 1

    status = await module.status()
    assert status.state == "ready"
    assert status.error is None
    assert status.dead == 1
    assert status.succeeded == 1
    assert status.last_flush_observation == "succeeded"


async def test_status_treats_newer_persisted_flush_as_current_after_upgrade(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    row = store.claim_due(lease_owner="upgrade", now="2026-07-01T00:00:00.000Z")
    assert row is not None
    assert store.mark_delivered(
        row,
        lease_owner="upgrade",
        now="2026-07-01T00:00:01.000Z",
        add_request_id="add",
    )
    assert store.mark_flush_in_flight(row.session_id) == 1
    assert store.record_flush_verdict(
        row.session_id,
        FlushSucceeded("flush", "extracted"),
        now="2026-07-01T00:00:03.000Z",
    ) == 1
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """
                UPDATE memory_meta
                SET last_error = 'memory_provider_timeout',
                    last_error_at = '2026-07-01T00:00:02.000Z',
                    updated_at = '2026-07-01T00:00:02.000Z'
            WHERE singleton = 1
            """
        )

    status = await module.status()
    assert status.state == "ready"
    assert status.error is None
    assert status.last_flush_observation == "succeeded"
    assert store.ensure_meta().last_error is None

    assert await module.capture(_request(source="after-upgrade")) == CaptureAccepted()
    syncing = await module.status()
    assert syncing.state == "syncing"
    assert syncing.error is None


@pytest.mark.parametrize(
    ("verdict", "expected_observation"),
    [
        (FlushRejected("rejected", "INVALID_INPUT", server_fault=False), "rejected"),
        (FlushUnknown("timeout"), "unknown"),
    ],
)
async def test_latest_flush_observation_supersedes_stale_timeout_after_delivery(
    tmp_path: Path,
    verdict,
    expected_observation: str,
) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="failed")) == CaptureAccepted()
    provider.ingest_failures.extend(
        [MemoryProviderFailure("memory_provider_timeout")] * 3
    )
    current = datetime.now(UTC).replace(microsecond=0)
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, now=lambda: current)
    await worker.drain_once()
    current += timedelta(seconds=31)
    await worker.drain_once()
    current += timedelta(minutes=2, seconds=1)
    await worker.drain_once()

    assert await module.capture(_request(source="latest")) == CaptureAccepted()
    row = store.claim_due(lease_owner="latest", now=current.isoformat())
    assert row is not None
    assert store.mark_delivered(
        row,
        lease_owner="latest",
        now=current.isoformat(),
        add_request_id="latest-add",
    )
    delivered_status = await module.status()
    assert delivered_status.state == "degraded"
    assert delivered_status.error == "memory_provider_timeout"

    assert store.mark_flush_in_flight(row.session_id) == 1
    assert store.record_flush_verdict(
        row.session_id,
        verdict,
        now=(current + timedelta(seconds=1)).isoformat(),
    ) == 1
    status = await module.status()
    assert status.state == "ready"
    assert status.error is None
    assert status.last_flush_observation == expected_observation


async def test_failure_log_keeps_sanitized_delivery_failures(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request(source="failed", session="private-session", text="private text")) == CaptureAccepted()
    provider.ingest_failures.extend(
        [
            MemoryProviderFailure("memory_provider_timeout"),
            MemoryProviderFailure("memory_provider_timeout"),
            MemoryProviderFailure("memory_provider_timeout"),
        ]
    )
    current = datetime.now(UTC).replace(microsecond=0)
    expected_at = (current + timedelta(minutes=2, seconds=32)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="failure-log-worker",
        now=lambda: current,
    )

    await worker.drain_once()
    current += timedelta(seconds=31)
    await worker.drain_once()
    current += timedelta(minutes=2, seconds=1)
    await worker.drain_once()

    assert await module.failure_log() == (
        MemoryFailureLogEntry(
            kind="delivery_abandoned",
            occurred_at=expected_at,
            error_code="memory_provider_timeout",
            request_id=None,
            attempts=3,
        ),
    )


async def test_failure_log_includes_provider_rejections_and_unknown_results_newest_first(
    tmp_path: Path,
) -> None:
    module, store, _provider = _module(tmp_path)

    base = datetime.now(UTC).replace(microsecond=0)
    base_iso = base.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    delivered_iso = (base + timedelta(seconds=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    async def deliver(source: str, session: str, request_id: str) -> None:
        assert await module.capture(_request(source=source, session=session)) == CaptureAccepted()
        row = store.claim_due(lease_owner=source, now=base_iso)
        assert row is not None
        assert store.mark_delivered(
            row,
            lease_owner=source,
            now=delivered_iso,
            add_request_id=request_id,
        )
        assert store.mark_flush_in_flight(row.session_id) == 1

    await deliver("rejected", "rejected-session", "add-rejected")
    rejected_session = store.list_queue_rows()[0].session_id
    assert store.record_flush_verdict(
        rejected_session,
        FlushRejected("flush-rejected", "INVALID_INPUT", server_fault=False),
        now=(base + timedelta(seconds=3)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    ) == 1

    await deliver("unknown", "unknown-session", "add-unknown")
    unknown_session = store.list_queue_rows()[1].session_id
    assert store.record_flush_verdict(
        unknown_session,
        FlushUnknown("timeout"),
        now=(base + timedelta(seconds=4)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    ) == 1

    assert await module.failure_log() == (
        MemoryFailureLogEntry(
            kind="result_unknown",
            occurred_at=(base + timedelta(seconds=4)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            attempts=0,
        ),
        MemoryFailureLogEntry(
            kind="distillation_rejected",
            occurred_at=(base + timedelta(seconds=3)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            error_code="INVALID_INPUT",
            request_id="flush-rejected",
            attempts=0,
        ),
    )


async def test_failure_log_collapses_one_session_flush_into_one_entry(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    for source in ("one", "two"):
        assert await module.capture(
            _request(source=source, session="shared-session")
        ) == CaptureAccepted()
        row = store.claim_due(lease_owner=source, now="2026-07-01T00:00:00.000Z")
        assert row is not None
        assert store.mark_delivered(
            row,
            lease_owner=source,
            now="2026-07-01T00:00:01.000Z",
            add_request_id=f"add-{source}",
        )

    session_id = store.list_queue_rows()[0].session_id
    assert store.mark_flush_in_flight(session_id) == 2
    assert store.record_flush_verdict(
        session_id,
        FlushRejected("shared-flush", "INVALID_INPUT", server_fault=False),
        now="2026-07-01T00:00:03.000Z",
    ) == 2

    assert await module.failure_log() == (
        MemoryFailureLogEntry(
            kind="distillation_rejected",
            occurred_at="2026-07-01T00:00:03.000Z",
            error_code="INVALID_INPUT",
            request_id="shared-flush",
            attempts=0,
        ),
    )


async def test_failure_log_excludes_entries_older_than_retention(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    assert await module.capture(_request(source="expired")) == CaptureAccepted()
    old = datetime.now(UTC) - TERMINAL_TOMBSTONE_RETENTION - timedelta(days=1)
    old_iso = old.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    row = store.claim_due(lease_owner="expired", now=old_iso)
    assert row is not None
    assert store.mark_delivered(
        row,
        lease_owner="expired",
        now=old_iso,
        add_request_id="old-add",
    )
    assert store.mark_flush_in_flight(row.session_id) == 1
    assert store.record_flush_verdict(
        row.session_id,
        FlushUnknown("timeout"),
        now=old_iso,
    ) == 1

    assert await module.failure_log() == ()


async def test_failure_log_retention_uses_latest_flush_observation_time(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    assert await module.capture(_request(source="late-flush")) == CaptureAccepted()
    old = datetime.now(UTC) - TERMINAL_TOMBSTONE_RETENTION - timedelta(days=1)
    old_iso = old.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    observed = datetime.now(UTC).replace(microsecond=0)
    observed_iso = observed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    row = store.claim_due(lease_owner="late-flush", now=old_iso)
    assert row is not None
    assert store.mark_delivered(
        row,
        lease_owner="late-flush",
        now=old_iso,
        add_request_id="old-add",
    )
    assert store.mark_flush_in_flight(row.session_id) == 1
    assert store.record_flush_verdict(
        row.session_id,
        FlushRejected("fresh-rejection", "INVALID_INPUT", server_fault=False),
        now=observed_iso,
    ) == 1

    assert await module.failure_log() == (
        MemoryFailureLogEntry(
            kind="distillation_rejected",
            occurred_at=observed_iso,
            error_code="INVALID_INPUT",
            request_id="fresh-rejection",
            attempts=0,
        ),
    )


async def test_memory_never_logs_or_serializes_capture_or_provider_canaries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "very-secret-user-text-canary"
    provider = FakeMemoryProvider(search_failure=RuntimeError("provider-error-body-canary"))
    module, store, _provider = _module(tmp_path, provider=provider)

    assert await module.capture(_request(text=canary)) == CaptureAccepted()
    result = await module.search("query-canary")
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="boot")
    await worker.drain_once()

    rendered = f"{result!r}\n{caplog.text}"
    assert canary not in rendered
    assert "provider-error-body-canary" not in rendered


async def test_provider_failures_are_closed_codes_and_never_persist_provider_text(tmp_path: Path) -> None:
    canary = "https://provider.invalid/v1?api_key=memory-key-canary"
    provider = FakeMemoryProvider(search_failure=MemoryProviderFailure(canary))  # type: ignore[arg-type]
    module, store, _provider = _module(tmp_path, provider=provider)

    result = await module.search("query")

    assert isinstance(result, OperationFailed)
    assert result.error in CLOSED_MEMORY_ERROR_CODES
    assert canary not in repr(result)

    provider.ingest_failures.append(MemoryProviderFailure(canary))  # type: ignore[arg-type]
    assert await module.capture(_request()) == CaptureAccepted()
    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="boot")
    assert await worker.drain_once() == 1
    row = store.list_queue_rows()[0]
    assert row.last_error in CLOSED_MEMORY_ERROR_CODES
    with sqlite3.connect(store.path) as conn:
        serialized = "\n".join(
            str(value)
            for query in ("SELECT * FROM memory_meta", "SELECT * FROM memory_capture_queue")
            for item in conn.execute(query)
            for value in item
        )
    assert canary not in serialized


async def test_malformed_unicode_returns_closed_capture_and_search_errors(tmp_path: Path) -> None:
    module, _store, _provider = _module(tmp_path)

    capture = await module.capture(_request(text="\ud800"))
    search = await module.search("\ud800")

    assert capture == CaptureSkipped(reason="memory_invalid_input")
    assert search == OperationFailed(error="memory_invalid_input")


async def test_capture_happy_path_uses_one_local_queue_transaction(tmp_path: Path) -> None:
    class CountingStore(MemoryStore):
        def __init__(self, path: Path) -> None:
            self.transactions = 0
            super().__init__(path)

        @contextmanager
        def _transaction(self):
            self.transactions += 1
            with super()._transaction() as conn:
                yield conn

    store = CountingStore(_store_path(tmp_path))
    module = MemoryModule(
        store,
        FakeMemoryProvider(),
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
    )
    store.transactions = 0

    assert await module.capture(_request()) == CaptureAccepted()
    assert store.transactions == 1


async def test_capture_does_not_wait_or_admit_during_an_active_clear(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def clear_provider_data() -> None:
        entered.set()
        await release.wait()

    module, store, _provider = _module(
        tmp_path,
        clear_provider_data=clear_provider_data,
        owned_provider_root=True,
    )
    clear_task = asyncio.create_task(module.clear())
    await entered.wait()

    receipt = await asyncio.wait_for(module.capture(_request()), timeout=0.2)

    assert receipt == CaptureSkipped(reason="memory_clear_failed")
    assert store.list_queue_rows() == ()
    release.set()
    assert isinstance(await clear_task, ClearCompleted)


async def test_system_failure_globally_pauses_claims_until_health_gate_recovers(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    provider.ingest_failures.append(MemoryProviderSystemFailure())
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="boot",
        now=lambda: current,
    )

    assert await worker.drain_once() == 1
    assert await worker.drain_once() == 0
    paused = store.list_queue_rows()[0]
    assert (paused.state, paused.attempts) == ("pending", 0)

    current += timedelta(seconds=SYSTEM_PAUSE_SECONDS + 1)
    assert await worker.drain_once() == 1
    assert store.list_queue_rows()[0].state == "delivered"


async def test_pause_does_not_resume_until_processing_endpoint_recovers(tmp_path: Path) -> None:
    """Sidecar health alone must not reopen claims after a processing-endpoint outage.

    The resume gate must require BOTH health() and processing_healthy(); otherwise an
    endpoint that is still down gets re-probed on every tick after the pause interval,
    prematurely reopening the claim fence (tech §10 resume-on-global-health).
    """
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    provider.ingest_failures.append(MemoryProviderFailure("memory_processing_failed"))
    provider.processing_healthy_flag = False  # endpoint stays down throughout
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="boot",
        now=lambda: current,
    )

    # First drain: ambiguous failure + endpoint-down probe => system pause, attempts 0.
    await worker.drain_once()
    paused = store.list_queue_rows()[0]
    assert (paused.state, paused.attempts) == ("pending", 0)

    # Past the pause interval but processing endpoint STILL down: the row must NOT
    # be delivered (resume gate requires processing_healthy, not just sidecar health).
    current += timedelta(seconds=SYSTEM_PAUSE_SECONDS + 1)
    for _ in range(3):
        await worker.drain_once()
    still_paused = store.list_queue_rows()[0]
    assert still_paused.state == "pending"
    assert still_paused.attempts == 0

    # Endpoint recovers => advance past the pause window and claims resume.
    provider.processing_healthy_flag = True
    current += timedelta(seconds=SYSTEM_PAUSE_SECONDS + 1)
    await worker.drain_once()
    assert store.list_queue_rows()[0].state == "delivered"


async def test_clear_has_bounded_provider_cleanup_and_drain_waits(tmp_path: Path) -> None:
    cleanup_wait = asyncio.Event()

    async def never_finish_cleanup() -> None:
        await cleanup_wait.wait()

    module, store, _provider = _module(
        tmp_path / "cleanup-timeout",
        clear_provider_data=never_finish_cleanup,
        clear_cleanup_timeout_seconds=0.01,
        owned_provider_root=True,
    )

    assert await asyncio.wait_for(module.clear(), timeout=0.5) == OperationFailed(error="memory_clear_failed")
    assert store.ensure_meta().clear_in_progress is True
    cleanup_wait.set()
    await asyncio.sleep(0)

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeMemoryProvider):
        async def add(self, capture):
            del capture
            entered.set()
            await release.wait()

    provider = BlockingProvider()
    store = MemoryStore(_store_path(tmp_path / "drain-timeout"))
    provider_root = tmp_path / "drain-timeout" / "provider-root"
    _write_owned_provider_root(provider_root, store)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        ingest_timeout_seconds=1.0,
    )
    module = MemoryModule(
        store,
        provider,
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        clear_provider_data=lambda: None,
        clear_drain_timeout_seconds=0.01,
        provider_root=provider_root,
        worker=worker,
    )
    assert await module.capture(_request()) == CaptureAccepted()
    drain_task = asyncio.create_task(worker.drain_once())
    await entered.wait()

    assert await module.clear() == OperationFailed(error="memory_clear_failed")
    assert store.ensure_meta().clear_in_progress is True
    release.set()
    assert await drain_task == 1


async def test_interrupted_clear_rechecks_after_a_transient_store_read_failure(tmp_path: Path) -> None:
    class FailingFirstReadStore(MemoryStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.fail_next_read = False

        def get_meta(self):
            if self.fail_next_read:
                self.fail_next_read = False
                raise sqlite3.OperationalError("transient store read failure")
            return super().get_meta()

    calls = 0

    async def clear_provider_data() -> None:
        nonlocal calls
        calls += 1

    store = FailingFirstReadStore(_store_path(tmp_path))
    store.begin_clear()
    provider_root = tmp_path / "provider-root"
    _write_owned_provider_root(provider_root, store)
    store.fail_next_read = True
    module = MemoryModule(
        store,
        FakeMemoryProvider(),
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        clear_provider_data=clear_provider_data,
        provider_root=provider_root,
    )

    first = await module.status()
    second = await module.status()

    assert first.state == "clearing"
    assert second.state == "ready"
    assert calls == 1
    assert store.ensure_meta().clear_in_progress is False


async def test_disabled_clear_resumes_worker_after_reenable(tmp_path: Path) -> None:
    enabled = {"value": True}
    module, store, _provider = _module(
        tmp_path,
        enabled=lambda: enabled["value"],
        clear_provider_data=lambda: None,
        owned_provider_root=True,
    )
    assert await module.capture(_request(source="before-clear")) == CaptureAccepted()
    enabled["value"] = False
    assert isinstance(await module.clear(), ClearCompleted)

    enabled["value"] = True
    assert await module.capture(_request(source="after-clear")) == CaptureAccepted()
    assert await module._worker.drain_once() == 1
    assert store.list_queue_rows()[0].state == "delivered"


async def test_clear_never_reports_completed_without_provider_cleanup(tmp_path: Path) -> None:
    no_cleanup, no_cleanup_store, _provider = _module(tmp_path / "no-cleanup")
    assert await no_cleanup.clear() == OperationFailed(error="memory_clear_failed")
    assert no_cleanup_store.ensure_meta().clear_in_progress is True

    provider_root = tmp_path / "provider-root"
    provider_root.mkdir()
    retained = provider_root / "retained-provider-data"
    retained.write_text("provider state", encoding="utf-8")
    module, store, _provider = _module(
        tmp_path / "root-remains",
        provider_root=provider_root,
        clear_provider_data=lambda: None,
    )

    assert await module.clear() == OperationFailed(error="memory_clear_failed")
    assert retained.exists()
    assert store.ensure_meta().clear_in_progress is True


async def test_capture_capacity_and_disk_pauses_are_visible_as_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    low_disk, _store, _provider = _module(tmp_path / "low-disk", disk_free_bytes=lambda: 0)
    assert await low_disk.capture(_request()) == CaptureSkipped(reason="memory_low_disk_space")
    assert (await low_disk.status()).state == "degraded"

    monkeypatch.setattr("core.memory.module.MAX_NONTERMINAL_QUEUE_ROWS", 1)
    full, _store, _provider = _module(tmp_path / "queue-full")
    assert await full.capture(_request(source="one")) == CaptureAccepted()
    assert await full.capture(_request(source="two")) == CaptureSkipped(reason="memory_queue_full")
    assert (await full.status()).state == "degraded"


async def test_whitespace_only_capture_identifiers_are_invalid(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)

    assert await module.capture(_request(source="   ")) == CaptureSkipped(reason="memory_invalid_input")
    assert await module.capture(_request(session="\t\n")) == CaptureSkipped(reason="memory_invalid_input")
    assert store.ensure_meta().missed_count == 2


def test_provider_port_is_not_part_of_the_public_memory_package() -> None:
    import core.memory as memory

    assert "MemoryProviderPort" not in memory.__all__
    assert "ProviderCapture" not in memory.__all__


async def test_healthy_timeout_poison_row_spends_attempts_then_unblocks_later_work(tmp_path: Path) -> None:
    class PoisonProvider(FakeMemoryProvider):
        async def add(self, capture):
            if capture.text == "poison":
                await asyncio.Event().wait()
            return await super().add(capture)

    provider = PoisonProvider()
    module, store, _provider = _module(tmp_path, provider=provider)
    assert await module.capture(_request(source="poison", text="poison")) == CaptureAccepted()
    assert await module.capture(_request(source="later", text="later")) == CaptureAccepted()
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="poison-worker",
        now=lambda: current,
        ingest_timeout_seconds=0.01,
    )

    assert await worker.drain_once() == 1
    assert store.list_queue_rows()[0].attempts == 1
    current += timedelta(seconds=31)
    assert await worker.drain_once() == 1
    assert store.list_queue_rows()[0].attempts == 2
    current += timedelta(minutes=2, seconds=1)
    assert await worker.drain_once() == 1
    poison = store.list_queue_rows()[0]
    assert (poison.state, poison.attempts, poison.payload_text) == ("dead", 3, None)

    assert await worker.drain_once() == 1
    later = store.list_queue_rows()[1]
    assert later.state == "delivered"
    assert [capture.text for capture in provider.captures] == ["later"]


async def test_clear_rejects_a_missing_or_unsentinelized_provider_root(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-root"
    missing, missing_store, _provider = _module(
        tmp_path / "missing",
        provider_root=missing_root,
        clear_provider_data=lambda: None,
    )
    assert await missing.clear() == OperationFailed(error="memory_clear_failed")
    assert missing_store.ensure_meta().clear_in_progress is True

    root_without_sentinel = tmp_path / "root-without-sentinel"
    root_without_sentinel.mkdir()
    unsentinelized, unsentinelized_store, _provider = _module(
        tmp_path / "unsentinelized",
        provider_root=root_without_sentinel,
        clear_provider_data=lambda: None,
    )
    assert await unsentinelized.clear() == OperationFailed(error="memory_clear_failed")
    assert unsentinelized_store.ensure_meta().clear_in_progress is True


async def test_clear_removes_provider_children_and_recreates_the_owned_sentinel(tmp_path: Path) -> None:
    provider_root = tmp_path / "provider-root"
    module, store, _provider = _module(
        tmp_path,
        provider_root=provider_root,
        clear_provider_data=lambda: None,
        owned_provider_root=True,
    )
    (provider_root / "derived-state").write_text("derived", encoding="utf-8")
    nested = provider_root / "nested"
    nested.mkdir()
    (nested / "more-state").write_text("derived", encoding="utf-8")

    assert isinstance(await module.clear(), ClearCompleted)
    assert [entry.name for entry in provider_root.iterdir()] == [ROOT_SENTINEL_FILENAME]
    sentinel = json.loads((provider_root / ROOT_SENTINEL_FILENAME).read_text(encoding="utf-8"))
    assert sentinel["provider_root_id"] == store.ensure_meta().provider_root_id
    assert store.ensure_meta().clear_in_progress is False


async def test_timed_out_sync_cleanup_remains_lifecycle_owned_without_overlap(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0

    def blocking_cleanup() -> None:
        nonlocal active, calls, max_active
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        started.set()
        release.wait(timeout=1.0)
        with lock:
            active -= 1

    module, _store, _provider = _module(
        tmp_path,
        clear_provider_data=blocking_cleanup,
        clear_cleanup_timeout_seconds=0.01,
        owned_provider_root=True,
    )
    try:
        assert await module.clear() == OperationFailed(error="memory_clear_failed")
        assert await asyncio.to_thread(started.wait, 0.5)

        status = await asyncio.wait_for(module.status(), timeout=0.2)
        assert status.state == "clearing"
        with lock:
            assert (calls, max_active) == (1, 1)
    finally:
        release.set()
        await asyncio.sleep(0.05)


async def test_status_rechecks_persistent_low_disk_after_an_older_row_delivers(tmp_path: Path) -> None:
    disk = {"free": MIN_FREE_DISK_BYTES}
    module, store, provider = _module(tmp_path, disk_free_bytes=lambda: disk["free"])
    assert await module.capture(_request(source="older")) == CaptureAccepted()
    disk["free"] = 0
    assert await module.capture(_request(source="low-disk")) == CaptureSkipped(reason="memory_low_disk_space")

    worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True, boot_id="disk-worker")
    assert await worker.drain_once() == 1

    status = await module.status()
    assert status.state == "degraded"
    assert status.error == "memory_low_disk_space"


async def test_provider_outage_after_disk_recovery_reports_sidecar_error(tmp_path: Path) -> None:
    """A resolved low-disk condition must not mask a later active provider outage.

    Sequence: low-disk skipped -> disk recovers (ready) -> provider goes down ->
    status must be 'down' with error 'memory_sidecar_unavailable', NOT the stale
    'memory_low_disk_space' (tech §15 precedence: active outage over resolved disk).
    """
    disk = {"free": MIN_FREE_DISK_BYTES}
    module, store, provider = _module(tmp_path, disk_free_bytes=lambda: disk["free"])
    disk["free"] = 0
    assert await module.capture(_request(source="low-disk")) == CaptureSkipped(reason="memory_low_disk_space")

    # Disk recovers -> ready, error None.
    disk["free"] = MIN_FREE_DISK_BYTES
    status = await module.status()
    assert status.state == "ready"
    assert status.error is None

    # Provider goes down -> down with the active sidecar error, not stale disk.
    provider.healthy = False
    status = await module.status()
    assert status.state == "down"
    assert status.error == "memory_sidecar_unavailable"


async def test_health_recovery_clears_only_the_persisted_system_outage_error(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(healthy=False)
    module, store, _provider = _module(tmp_path, provider=provider)
    current = datetime(2026, 1, 1, tzinfo=UTC)
    worker = MemoryWorker(
        store=store,
        provider=provider,
        enabled=lambda: True,
        boot_id="recovery-worker",
        now=lambda: current,
    )

    assert await worker.drain_once() == 0
    assert store.ensure_meta().last_error == "memory_sidecar_unavailable"
    provider.healthy = True
    current += timedelta(seconds=SYSTEM_PAUSE_SECONDS + 1)
    assert await worker.drain_once() == 0
    assert store.ensure_meta().last_error is None
    assert (await module.status()).state == "ready"


def test_slice2_runtime_types_remain_internal_to_the_memory_package() -> None:
    import core.memory.artifact as artifact
    import core.memory.process as process
    import core.memory as memory

    assert hasattr(artifact, "MemoryArtifactManager")
    assert hasattr(process, "EverOSProcess")
    assert "MemoryArtifactManager" not in memory.__all__
    assert "EverOSProcess" not in memory.__all__
