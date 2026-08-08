from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import paths
from core.memory.coordinator import SessionFlushCoordinator
from core.memory.everos import (
    FakeMemoryProvider,
    MemoryProviderFailure,
    MemoryProviderSystemFailure,
)
from core.memory.observations import AddAck, FlushRetryable, FlushSucceeded
from core.memory.store import MemoryStore
from core.memory.types import ProviderSessionRef
from core.memory.worker import MemoryWorker


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "p-22222222222222222222222222222222"


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(paths.get_state_dir() / "coordinator-tests" / tmp_path.name / "memory.sqlite")


def _enqueue(store: MemoryStore, source: str, *, session: str = "session"):
    result = store.enqueue_request(
        source_message_id=source,
        session_id=session,
        principal_id=PRINCIPAL,
        project_ref=PROJECT,
        provenance="user_input",
        payload_text=f"payload-{source}",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert result.row is not None
    return result.row


async def _wait_for_scheduled_flush(
    coordinator: SessionFlushCoordinator,
    session_ref: ProviderSessionRef,
) -> None:
    task = coordinator._flush_tasks.get(session_ref.serialize())
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=2)


def test_accumulated_add_waits_for_idle_flush(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
        )
        row = _enqueue(store, "one")

        assert await worker.drain_once() == 1
        assert provider.flushes == []
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None
        assert state.first_unflushed_at == "2026-01-01T00:00:00.000Z"
        assert state.due_at == "2026-01-01T00:05:00.000Z"

        current[0] += timedelta(minutes=5)
        assert await worker.coordinator.run_due() == 1
        await _wait_for_scheduled_flush(worker.coordinator, row.provider_session_ref)

        assert provider.flushes == [row.provider_session_ref]
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "idle"
        assert state.open_generation == 2

    asyncio.run(run())


def test_extracted_add_is_a_natural_boundary_without_flush(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        provider.add_results = deque([AddAck("natural", "extracted")])
        worker = MemoryWorker(store=store, provider=provider, enabled=lambda: True)
        row = _enqueue(store, "natural")

        assert await worker.drain_once() == 1

        assert provider.flushes == []
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None
        assert (state.state, state.open_generation, state.unflushed_count) == ("idle", 2, 0)

    asyncio.run(run())


def test_system_outage_backs_off_add_claims_between_drain_ticks(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        provider.ingest_failures.extend(
            [MemoryProviderSystemFailure(), MemoryProviderSystemFailure()]
        )
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
        )
        _enqueue(store, "outage")

        assert await worker.drain_once() == 1
        assert len(provider.ingest_failures) == 1
        assert await worker.drain_once() == 0
        assert len(provider.ingest_failures) == 1

        current[0] += timedelta(seconds=5)
        assert await worker.drain_once() == 1
        assert len(provider.ingest_failures) == 0
        row = store.list_queue_rows()[0]
        assert (row.state, row.attempts) == ("pending", 0)

    asyncio.run(run())


def test_system_outage_backs_off_fenced_generation_adds(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        provider.ingest_failures.extend(
            [MemoryProviderSystemFailure(), MemoryProviderSystemFailure()]
        )
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
        )
        row = _enqueue(store, "fenced-outage")

        assert not await coordinator.final_flush(row.provider_session_ref)
        assert len(provider.ingest_failures) == 1
        assert not await coordinator.final_flush(row.provider_session_ref)
        assert len(provider.ingest_failures) == 1

        current[0] += timedelta(seconds=5)
        assert not await coordinator.final_flush(row.provider_session_ref)
        assert len(provider.ingest_failures) == 0

    asyncio.run(run())


def test_processing_fault_emits_one_fault_and_recovery_edge(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider(processing_healthy_flag=False)
        provider.ingest_failures.append(
            MemoryProviderFailure("memory_processing_failed", retryable=True)
        )
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        events: list[tuple[str, str | None, str, int]] = []

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            processing_event=record_event,
        )
        _enqueue(store, "processing-fault")

        assert await worker.drain_once() == 1
        opened = store.get_meta()
        assert opened is not None
        assert opened.processing_fault_kind == "credential"
        assert opened.processing_alert_active is True
        assert [event[:2] for event in events] == [("fault", "credential")]
        assert events[0][3] == 1

        provider.processing_healthy_flag = True
        current[0] += timedelta(seconds=5)
        assert await worker.drain_once() == 1
        closed = store.get_meta()
        assert closed is not None
        assert closed.processing_fault_since is None
        assert [event[:2] for event in events] == [
            ("fault", "credential"),
            ("recovered", None),
        ]
        assert events[1][3] == 0

    asyncio.run(run())


def test_fence_routes_new_capture_to_next_generation(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                entered.set()
                await release.wait()
                return FlushSucceeded("flush", "extracted")

        provider = Provider()
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
        )
        first = _enqueue(store, "first")
        assert await worker.drain_once() == 1
        current[0] += timedelta(minutes=5)
        assert await worker.coordinator.run_due() == 1
        await asyncio.wait_for(entered.wait(), timeout=1)

        second = _enqueue(store, "second")
        assert second.generation == first.generation + 1
        assert store.claim_due(lease_owner="raced", now="2026-01-01T00:05:01.000Z") is None

        release.set()
        await _wait_for_scheduled_flush(worker.coordinator, first.provider_session_ref)
        claimed = store.claim_due(lease_owner="next", now="2026-01-01T00:05:01.000Z")
        assert claimed is not None and claimed.source_message_digest == second.source_message_digest

    asyncio.run(run())


def test_stale_flush_settlement_cannot_clear_newer_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    current = datetime(2026, 1, 1, tzinfo=UTC)
    row = _enqueue(store, "stale")
    claimed = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None
    assert store.settle_add_ack(
        claimed,
        AddAck("add", "accumulated"),
        lease_owner="worker",
        now=current,
        idle_timeout=timedelta(0),
    ).settled
    lease = store.acquire_flush(
        now="2026-01-01T00:00:00.000Z",
        provider_session_ref=row.provider_session_ref,
    )
    assert lease is not None
    assert store.mark_flush_submission_started(lease, now="2026-01-01T00:00:01.000Z")
    assert store.settle_flush(
        lease,
        FlushSucceeded("first", "extracted"),
        now="2026-01-01T00:00:02.000Z",
    ).settled

    assert not store.settle_flush(
        lease,
        FlushSucceeded("stale", "extracted"),
        now="2026-01-01T00:00:03.000Z",
    ).settled
    state = store.get_session_flush_state(row.provider_session_ref)
    assert state is not None and state.open_generation == 2 and state.state == "idle"


def test_boot_recovery_never_replays_submitted_flush(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = _enqueue(store, "boot")
    claimed = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None
    assert store.settle_add_ack(
        claimed,
        AddAck("add", "accumulated"),
        lease_owner="worker",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        idle_timeout=timedelta(0),
    ).settled
    lease = store.acquire_flush(
        now="2026-01-01T00:00:00.000Z",
        provider_session_ref=row.provider_session_ref,
    )
    assert lease is not None
    assert store.mark_flush_submission_started(lease, now="2026-01-01T00:00:01.000Z")

    recovered = MemoryStore(store.path)
    evidence = recovered.recover_after_boot(
        lease_owner="new-boot",
        clock=lambda: datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
    )

    assert evidence.interrupted_flushes == 1
    state = recovered.get_session_flush_state(row.provider_session_ref)
    assert state is not None and state.state == "manual_required"
    assert recovered.acquire_flush(
        now="2026-01-01T00:10:00.000Z",
        provider_session_ref=row.provider_session_ref,
        force=True,
    ) is None


def test_proven_pre_submission_flush_failure_uses_bounded_retry(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        provider.flush_results.extend([FlushRetryable()] * 4)
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            coordinator=coordinator,
        )
        row = _enqueue(store, "retry")
        assert await worker.drain_once() == 1
        expected_delays = (1, 2, 4)
        current[0] += timedelta(minutes=5)
        for retry_count, delay in enumerate(expected_delays, start=1):
            assert await coordinator.run_due() == 1
            await _wait_for_scheduled_flush(coordinator, row.provider_session_ref)

            state = store.get_session_flush_state(row.provider_session_ref)
            assert state is not None
            assert state.state == "due"
            assert state.retry_count == retry_count
            assert state.next_attempt_at == (
                current[0] + timedelta(seconds=delay)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            assert state.submission_started_at is None
            current[0] += timedelta(seconds=delay)

        assert await coordinator.run_due() == 1
        await _wait_for_scheduled_flush(coordinator, row.provider_session_ref)
        terminal = store.get_session_flush_state(row.provider_session_ref)
        assert terminal is not None
        assert terminal.state == "manual_required"
        assert terminal.retry_count == 4
        assert terminal.next_attempt_at is None
        assert len(provider.flushes) == 4

    asyncio.run(run())


def test_continuous_activity_cannot_extend_flush_past_max_age(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        current = [start]
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
        )
        session_ref = None

        # Every add arrives before the five-minute idle deadline. The absolute
        # age bound still pins the generation to the first add plus 30 minutes.
        for index, minute in enumerate(range(0, 29, 4)):
            current[0] = start + timedelta(minutes=minute)
            queued = _enqueue(store, f"continuous-{index}")
            session_ref = queued.provider_session_ref
            claimed = store.claim_due(
                lease_owner="continuous",
                now=current[0].isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
            assert claimed is not None
            assert await coordinator.deliver(claimed, lease_owner="continuous")

        assert session_ref is not None
        state = store.get_session_flush_state(session_ref)
        assert state is not None
        assert state.first_unflushed_at == "2026-01-01T00:00:00.000Z"
        assert state.last_add_ack_at == "2026-01-01T00:28:00.000Z"
        assert state.due_at == "2026-01-01T00:30:00.000Z"

        current[0] = start + timedelta(minutes=29, seconds=59)
        assert await coordinator.run_due() == 0
        current[0] = start + timedelta(minutes=30)
        assert await coordinator.run_due() == 1
        await _wait_for_scheduled_flush(coordinator, session_ref)

        assert provider.flushes == [session_ref]
        state = store.get_session_flush_state(session_ref)
        assert state is not None and state.state == "idle"

    asyncio.run(run())


def test_message_bound_makes_generation_immediately_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr("core.memory.coordinator.MAX_UNFLUSHED_MESSAGES", 3)
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        current = datetime(2026, 1, 1, tzinfo=UTC)
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current,
        )
        rows = [_enqueue(store, f"bounded-{index}") for index in range(3)]

        assert await worker.drain(max_rows=3) == 3
        await _wait_for_scheduled_flush(worker.coordinator, rows[0].provider_session_ref)

        assert provider.flushes == [rows[0].provider_session_ref]
        state = store.get_session_flush_state(rows[0].provider_session_ref)
        assert state is not None
        assert (state.state, state.open_generation, state.unflushed_count) == ("idle", 2, 0)

    asyncio.run(run())


def test_same_session_serializes_while_another_session_continues(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def add(self, capture):
                self.captures.append(capture)
                if capture.text == "payload-same-first":
                    first_entered.set()
                    await release_first.wait()
                return AddAck(
                    request_id=f"add-{capture.text}",
                    status="accumulated",
                )

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        _enqueue(store, "same-first", session="same")
        _enqueue(store, "same-second", session="same")
        _enqueue(store, "other", session="other")
        claimed = {}
        for _ in range(3):
            row = store.claim_due(lease_owner="parallel", now="2026-01-01T00:00:00.000Z")
            assert row is not None and row.payload_text is not None
            claimed[row.payload_text] = row

        first = asyncio.create_task(
            coordinator.deliver(
                claimed["payload-same-first"],
                lease_owner="parallel",
            )
        )
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        same_second = asyncio.create_task(
            coordinator.deliver(
                claimed["payload-same-second"],
                lease_owner="parallel",
            )
        )
        other = asyncio.create_task(
            coordinator.deliver(
                claimed["payload-other"],
                lease_owner="parallel",
            )
        )

        assert await asyncio.wait_for(other, timeout=1)
        assert not same_second.done()
        assert [capture.text for capture in provider.captures] == [
            "payload-same-first",
            "payload-other",
        ]

        release_first.set()
        assert await asyncio.wait_for(first, timeout=1)
        assert await asyncio.wait_for(same_second, timeout=1)
        assert [capture.text for capture in provider.captures] == [
            "payload-same-first",
            "payload-other",
            "payload-same-second",
        ]

    asyncio.run(run())


def test_shutdown_does_not_initiate_a_provider_flush(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
        )
        row = _enqueue(store, "shutdown")
        assert await worker.drain_once() == 1
        assert len(provider.captures) == 1

        current[0] += timedelta(minutes=5)
        await worker.prepare_shutdown()
        assert await worker.coordinator.run_due() == 0
        assert not await worker.coordinator.final_flush(row.provider_session_ref)

        assert len(provider.captures) == 1
        assert provider.flushes == []
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "idle"

    asyncio.run(run())


def test_cancelled_flush_waiting_for_write_slot_remains_retryable(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        first_flush_entered = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                if session_ref == first.provider_session_ref:
                    first_flush_entered.set()
                    await asyncio.Event().wait()
                return FlushSucceeded("flush", "extracted")

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            max_concurrent_writes=1,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            coordinator=coordinator,
        )
        first = _enqueue(store, "first", session="first")
        waiting = _enqueue(store, "waiting", session="waiting")
        assert await worker.drain(max_rows=2) == 2

        first_call = asyncio.create_task(
            coordinator.final_flush(first.provider_session_ref, deadline_seconds=5)
        )
        await asyncio.wait_for(first_flush_entered.wait(), timeout=1)
        waiting_call = asyncio.create_task(
            coordinator.final_flush(waiting.provider_session_ref, deadline_seconds=5)
        )

        async def wait_for_queued_slot() -> None:
            while not getattr(coordinator._write_slots, "_waiters", None):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_queued_slot(), timeout=1)
        await coordinator.prepare_shutdown()

        with pytest.raises(asyncio.CancelledError):
            await first_call
        with pytest.raises(asyncio.CancelledError):
            await waiting_call

        first_state = store.get_session_flush_state(first.provider_session_ref)
        assert first_state is not None and first_state.state == "manual_required"
        waiting_state = store.get_session_flush_state(waiting.provider_session_ref)
        assert waiting_state is not None
        assert waiting_state.state == "due"
        assert waiting_state.submission_started_at is None
        assert provider.flushes == [first.provider_session_ref]

        coordinator.resume()
        assert await coordinator.final_flush(
            waiting.provider_session_ref,
            deadline_seconds=1,
        )
        assert provider.flushes == [
            first.provider_session_ref,
            waiting.provider_session_ref,
        ]

    asyncio.run(run())


def test_cancelled_flush_while_submission_marker_commits_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            coordinator=coordinator,
        )
        row = _enqueue(store, "cancelled-marker")
        assert await worker.drain_once() == 1

        marker_committed = threading.Event()
        release_marker = threading.Event()
        original_mark = store.mark_flush_submission_started

        def blocking_mark(lease, *, now: str) -> bool:
            marked = original_mark(lease, now=now)
            marker_committed.set()
            release_marker.wait(timeout=2)
            return marked

        monkeypatch.setattr(store, "mark_flush_submission_started", blocking_mark)
        flush_call = asyncio.create_task(
            coordinator.final_flush(row.provider_session_ref, deadline_seconds=5)
        )
        assert await asyncio.to_thread(marker_committed.wait, 1)
        flush_task = coordinator._flush_tasks[row.provider_session_ref.serialize()]
        flush_task.cancel()
        await asyncio.sleep(0)
        assert flush_task.done() is False
        release_marker.set()

        with pytest.raises(asyncio.CancelledError):
            await flush_call
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None
        assert state.state == "due"
        assert state.submission_started_at is None
        assert provider.flushes == []

        assert await coordinator.final_flush(
            row.provider_session_ref,
            deadline_seconds=1,
        )
        assert provider.flushes == [row.provider_session_ref]

    asyncio.run(run())


def test_cancelled_add_waiting_for_write_slot_returns_exact_claim(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        first_add_entered = asyncio.Event()
        release_first_add = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def add(self, capture):
                self.captures.append(capture)
                if capture.text == "payload-first-add":
                    first_add_entered.set()
                    await release_first_add.wait()
                return AddAck(f"add-{len(self.captures)}", "accumulated")

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            max_concurrent_writes=1,
        )
        _enqueue(store, "first-add", session="first-add")
        _enqueue(store, "waiting-add", session="waiting-add")
        claimed = {
            row.payload_text: row
            for row in (
                store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z"),
                store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z"),
            )
            if row is not None
        }

        first_call = asyncio.create_task(
            coordinator.deliver(claimed["payload-first-add"], lease_owner="worker")
        )
        await asyncio.wait_for(first_add_entered.wait(), timeout=1)
        waiting_call = asyncio.create_task(
            coordinator.deliver(claimed["payload-waiting-add"], lease_owner="worker")
        )

        async def wait_for_queued_slot() -> None:
            while not getattr(coordinator._write_slots, "_waiters", None):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_queued_slot(), timeout=1)
        waiting_call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_call

        waiting_row = next(
            row for row in store.list_queue_rows() if row.payload_text == "payload-waiting-add"
        )
        assert waiting_row.state == "pending"
        assert waiting_row.attempts == 0
        assert [capture.text for capture in provider.captures] == ["payload-first-add"]

        release_first_add.set()
        assert await asyncio.wait_for(first_call, timeout=1)
        recovery = store.recover_after_boot(
            lease_owner="next-boot",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert recovery.reclaimed == 0
        retry = store.claim_due(
            lease_owner="next-boot",
            now="2026-01-01T00:00:01.000Z",
        )
        assert retry is not None and retry.payload_text == "payload-waiting-add"

        assert await coordinator.deliver(retry, lease_owner="next-boot")
        assert [capture.text for capture in provider.captures] == [
            "payload-first-add",
            "payload-waiting-add",
        ]

    asyncio.run(run())


def test_stale_add_waiter_does_not_resubmit_after_flush_reclaims_it(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        first_add_entered = asyncio.Event()
        release_first_add = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def add(self, capture):
                self.captures.append(capture)
                if len(self.captures) == 1:
                    first_add_entered.set()
                    await release_first_add.wait()
                return AddAck(f"add-{len(self.captures)}", "accumulated")

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        row = _enqueue(store, "stale-waiter")
        stale_claim = store.claim_due(
            lease_owner="ordinary-worker",
            now="2026-01-01T00:00:00.000Z",
        )
        assert stale_claim is not None

        flush_call = asyncio.create_task(
            coordinator.final_flush(row.provider_session_ref, deadline_seconds=2)
        )
        await asyncio.wait_for(first_add_entered.wait(), timeout=1)
        stale_call = asyncio.create_task(
            coordinator.deliver(stale_claim, lease_owner="ordinary-worker")
        )
        await asyncio.sleep(0)
        assert stale_call.done() is False

        release_first_add.set()
        assert await flush_call
        assert await stale_call is False
        assert [capture.text for capture in provider.captures] == [
            "payload-stale-waiter"
        ]
        assert provider.flushes == [row.provider_session_ref]

    asyncio.run(run())


def test_cancelled_add_waiting_for_session_lock_returns_exact_claim(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        first_add_entered = asyncio.Event()
        release_first_add = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def add(self, capture):
                self.captures.append(capture)
                if capture.text == "payload-first-locked":
                    first_add_entered.set()
                    await release_first_add.wait()
                return AddAck(f"add-{len(self.captures)}", "accumulated")

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        _enqueue(store, "first-locked", session="same-session")
        _enqueue(store, "waiting-locked", session="same-session")
        claimed = {
            row.payload_text: row
            for row in (
                store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z"),
                store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z"),
            )
            if row is not None
        }

        first_call = asyncio.create_task(
            coordinator.deliver(claimed["payload-first-locked"], lease_owner="worker")
        )
        await asyncio.wait_for(first_add_entered.wait(), timeout=1)
        waiting_call = asyncio.create_task(
            coordinator.deliver(claimed["payload-waiting-locked"], lease_owner="worker")
        )
        await asyncio.sleep(0)
        assert waiting_call.done() is False

        waiting_call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_call

        waiting_row = next(
            row
            for row in store.list_queue_rows()
            if row.payload_text == "payload-waiting-locked"
        )
        assert waiting_row.state == "pending"
        assert waiting_row.attempts == 0
        assert [capture.text for capture in provider.captures] == ["payload-first-locked"]

        release_first_add.set()
        assert await asyncio.wait_for(first_call, timeout=1)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("malformed", "memory_provider_response_invalid"),
        ("timeout", "memory_provider_timeout"),
    ],
)
def test_submitted_malformed_or_timeout_flush_is_terminal(
    tmp_path: Path,
    failure: str,
    expected_error: str,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)

        class Provider(FakeMemoryProvider):
            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                if failure == "timeout":
                    await asyncio.Event().wait()
                return FlushSucceeded(request_id=None, status="extracted")

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            flush_timeout_seconds=0.01,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            coordinator=coordinator,
        )
        row = _enqueue(store, failure)
        assert await worker.drain_once() == 1

        assert not await coordinator.final_flush(
            row.provider_session_ref,
            deadline_seconds=1,
        )

        assert provider.flushes == [row.provider_session_ref]
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "manual_required"
        failures = store.failure_log()
        assert failures[0].operation == "flush"
        assert failures[0].state == "manual_required"
        assert failures[0].error_code == expected_error

    asyncio.run(run())
