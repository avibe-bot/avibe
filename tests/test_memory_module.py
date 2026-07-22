from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.memory.everos import (
    FakeMemoryProvider,
    MemoryProviderMessageFailure,
    MemoryProviderSystemFailure,
)
from core.memory.module import (
    MAX_CAPTURE_IDENTIFIER_BYTES,
    MAX_CAPTURE_TEXT_BYTES,
    MAX_QUERY_BYTES,
    MIN_FREE_DISK_BYTES,
    MemoryModule,
)
from core.memory.store import MemoryStore
from core.memory.types import (
    CaptureAccepted,
    CaptureDuplicate,
    CaptureSkipped,
    ClearCompleted,
    MemoryItem,
    MemoryItems,
    OperationFailed,
)
from core.memory.worker import MemoryWorker


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
    **kwargs,
) -> tuple[MemoryModule, MemoryStore, FakeMemoryProvider]:
    store = MemoryStore(tmp_path / "memory.sqlite")
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
    retry_provider.ingest_failures.append(MemoryProviderMessageFailure())
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
    assert provider.captures[0].text == "secret queue payload"
    assert store.queue_stats().queue_plaintext_bytes == 0


async def test_worker_retries_message_failures_then_marks_dead_and_scrubs(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    provider.ingest_failures.extend(
        [
            MemoryProviderMessageFailure(),
            MemoryProviderMessageFailure(),
            MemoryProviderMessageFailure(),
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
        async def ingest(self, capture):
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

    module, store, provider = _module(tmp_path, clear_provider_data=clear_provider_data)
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

    module, _store, _provider = _module(tmp_path, clear_provider_data=clear_provider_data)
    clear_task = asyncio.create_task(module.clear())
    await entered.wait()
    assert (await module.status()).state == "clearing"
    release.set()
    assert isinstance(await clear_task, ClearCompleted)


async def test_status_precedence(tmp_path: Path) -> None:
    ready, _store, _provider = _module(tmp_path / "ready")
    assert (await ready.status()).state == "ready"

    indexing, _store, _provider = _module(tmp_path / "indexing")
    assert await indexing.capture(_request()) == CaptureAccepted()
    assert (await indexing.status()).state == "indexing"

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
