from __future__ import annotations

import asyncio
import gc
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import paths
from core.memory.attachments import (
    AttachmentPinStore,
    PinnedBundle,
    encode_pinned_bundle,
)
from core.memory.coordinator import SessionFlushCoordinator
from core.memory.everos import (
    FakeMemoryProvider,
    MemoryProviderFailure,
    MemoryProviderSystemFailure,
)
from core.memory.observations import (
    AddAck,
    AddRejected,
    FlushRejected,
    FlushRetryable,
    FlushSucceeded,
    FlushUnknown,
)
from core.memory.store import MemoryStore, MessageFailure, QueueRow
from core.memory.types import CaptureAttachment, ProviderSessionRef
from core.memory.worker import MemoryWorker


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "p-22222222222222222222222222222222"
TEST_BUNDLE_ID = "a" * 32


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


def _pin_attachment_bundle() -> tuple[AttachmentPinStore, PinnedBundle, Path]:
    home = paths.get_vibe_remote_dir()
    source_root = home / "attachments" / "avibe"
    source_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    source_root.chmod(0o700)
    source = source_root / "evidence.txt"
    source.write_bytes(b"durable attachment")
    source.chmod(0o600)
    attachment_store = AttachmentPinStore(
        effective_home=home,
        source_root=source_root,
    )
    bundle = attachment_store.pin(
        (
            CaptureAttachment(
                kind="doc",
                name=source.name,
                uri=source.as_uri(),
                ext="txt",
            ),
        )
    )
    pinned_path = home / "memory" / "attachments" / bundle.attachments[0].storage_key
    return attachment_store, bundle, pinned_path


def _enqueue_attachment_bundle(
    store: MemoryStore,
    bundle: PinnedBundle,
    *,
    source: str,
) -> QueueRow:
    result = store.enqueue_request(
        source_message_id=source,
        session_id="attachment-session",
        principal_id=PRINCIPAL,
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="remember the attachment",
        payload_attachments=encode_pinned_bundle(bundle),
        attachment_bundle_id=bundle.bundle_id,
        attachment_bundle_relative_path=bundle.relative_path,
        attachment_file_count=len(bundle.attachments),
        attachment_total_bytes=bundle.total_bytes,
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


def test_final_flush_upgrades_joined_due_flush_after_due_at_shifts(
    tmp_path: Path,
) -> None:
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
        first = _enqueue(store, "first-final-flush-race")
        assert await worker.drain_once() == 1

        current[0] += timedelta(minutes=5)
        session_lock = worker.coordinator._session_lock(
            first.provider_session_ref.serialize()
        )
        await session_lock.acquire()
        assert await worker.coordinator.run_due() == 1
        ordinary_flush = worker.coordinator._flush_tasks[
            first.provider_session_ref.serialize()
        ]
        assert not ordinary_flush.done()

        second = _enqueue(store, "second-final-flush-race")
        claimed = store.claim_due(
            lease_owner="blocked-add",
            now="2026-01-01T00:05:00.000Z",
        )
        assert claimed is not None
        assert claimed.source_message_digest == second.source_message_digest
        assert store.settle_add_ack(
            claimed,
            AddAck("second-add", "accumulated"),
            lease_owner="blocked-add",
            now=current[0],
        ).settled
        state = store.get_session_flush_state(first.provider_session_ref)
        assert state is not None
        assert state.due_at == "2026-01-01T00:10:00.000Z"

        final_flush = asyncio.create_task(
            worker.coordinator.final_flush(
                first.provider_session_ref,
                deadline_seconds=1,
            )
        )
        await asyncio.sleep(0)
        assert (
            worker.coordinator._flush_tasks[first.provider_session_ref.serialize()]
            is ordinary_flush
        )
        session_lock.release()

        assert await final_flush
        assert provider.flushes == [first.provider_session_ref]
        state = store.get_session_flush_state(first.provider_session_ref)
        assert state is not None
        assert (state.state, state.unflushed_count) == ("idle", 0)

    asyncio.run(run())


def test_final_flush_repeats_for_capture_enqueued_during_forced_pass(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        first_flush_entered = asyncio.Event()
        release_first_flush = asyncio.Event()
        second_add_entered = asyncio.Event()
        release_second_add = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def add(self, capture):
                self.captures.append(capture)
                if capture.text == "payload-second-final-flush-generation":
                    second_add_entered.set()
                    await release_second_add.wait()
                return AddAck(
                    request_id=f"add-{capture.text}",
                    status="accumulated",
                )

            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                if len(self.flushes) == 1:
                    first_flush_entered.set()
                    await release_first_flush.wait()
                return FlushSucceeded(f"flush-{len(self.flushes)}", "extracted")

        provider = Provider()
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        first = _enqueue(store, "first-final-flush-generation")
        assert await worker.drain_once() == 1

        final_flush = asyncio.create_task(
            worker.coordinator.final_flush(
                first.provider_session_ref,
                deadline_seconds=2,
            )
        )
        await asyncio.wait_for(first_flush_entered.wait(), timeout=1)
        second = _enqueue(store, "second-final-flush-generation")
        assert second.generation == first.generation + 1
        release_first_flush.set()

        await asyncio.wait_for(second_add_entered.wait(), timeout=1)
        assert not final_flush.done()
        release_second_add.set()

        assert await final_flush
        assert len(provider.flushes) == 2
        assert [capture.text for capture in provider.captures] == [
            "payload-first-final-flush-generation",
            "payload-second-final-flush-generation",
        ]
        state = store.get_session_flush_state(first.provider_session_ref)
        assert state is not None
        assert (state.state, state.unflushed_count) == ("idle", 0)
        assert not [
            row
            for row in store.list_queue_rows()
            if row.provider_session_ref == first.provider_session_ref
            and row.state in {"pending", "processing"}
        ]

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


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "malformed_2xx"])
def test_ambiguous_add_opens_one_durable_fault_without_replay(
    tmp_path: Path,
    failure: str,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        events: list[tuple[str, str | None, str, int]] = []

        class Provider(FakeMemoryProvider):
            async def add(self, capture):
                self.captures.append(capture)
                if failure == "timeout":
                    await asyncio.Event().wait()
                if failure == "disconnect":
                    raise MemoryProviderSystemFailure(
                        "memory_sidecar_unavailable",
                        ambiguous=True,
                    )
                return AddAck(request_id=None, status="accumulated")

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        provider = Provider(processing_healthy_flag=True)
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            ingest_timeout_seconds=0.01,
            processing_event=record_event,
        )
        _enqueue(store, f"ambiguous-{failure}")

        assert await worker.drain_once() == 1
        row = store.list_queue_rows()[0]
        assert row.state == "manual_required"
        assert len(provider.captures) == 1
        meta = store.ensure_meta()
        assert meta.processing_fault_since is not None
        assert meta.processing_fault_kind == "engine"
        assert meta.processing_alert_active is True
        assert [(event, kind) for event, kind, _at, _queued in events] == [
            ("fault", "engine")
        ]

        restarted = MemoryWorker(
            store=MemoryStore(store.path),
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        assert await restarted.drain_once() == 0
        assert await restarted.drain_once() == 0
        assert len(provider.captures) == 1
        assert [(event, kind) for event, kind, _at, _queued in events] == [
            ("fault", "engine")
        ]

    asyncio.run(run())


def test_restart_finishes_ambiguous_add_fault_after_classification_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider(processing_healthy_flag=False)
        provider.add_results.append(AddAck(request_id=None, status="accumulated"))
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        row = _enqueue(store, "ambiguous-classification-interrupt")
        claimed = store.claim_due(
            lease_owner="old-boot",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None

        async def interrupt_classification(_occurred_at: str) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(
            coordinator,
            "_classify_processing_fault_locked",
            interrupt_classification,
        )
        with pytest.raises(asyncio.CancelledError):
            await coordinator.deliver(claimed, lease_owner="old-boot")

        queued = store.list_queue_rows()[0]
        assert queued.state == "manual_required"
        pending_fault = store.ensure_meta()
        assert pending_fault.processing_fault_since is not None
        assert pending_fault.processing_fault_kind is None
        assert pending_fault.processing_alert_active is False
        assert len(provider.captures) == 1

        events: list[tuple[str, str | None, str, int]] = []

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued_count: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued_count))
            return True

        restarted = SessionFlushCoordinator(
            store=MemoryStore(store.path),
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await restarted.recover(lease_owner="new-boot")
        await restarted.recover(lease_owner="same-boot")

        recovered = store.ensure_meta()
        assert recovered.processing_fault_kind == "credential"
        assert recovered.processing_alert_active is True
        assert [(event, kind) for event, kind, _at, _queued in events] == [
            ("fault", "credential")
        ]
        assert len(provider.captures) == 1
        assert store.claim_due(
            lease_owner="new-boot",
            now="2026-01-01T00:00:02.000Z",
        ) is None
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "manual_required"

    asyncio.run(run())


def test_confirmed_client_rejection_does_not_open_processing_fault(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        provider.add_results.append(
            AddRejected(
                request_id="client-rejection",
                error_code="INVALID_ARGUMENT",
                server_fault=False,
            )
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        _enqueue(store, "client-rejection")

        assert await worker.drain_once() == 1
        rejected = store.list_queue_rows()[0]
        assert (rejected.state, rejected.add_request_id) == (
            "dead",
            "client-rejection",
        )
        assert store.ensure_meta().processing_fault_since is None

    asyncio.run(run())


def test_server_rejected_add_is_terminal_but_opens_processing_fault(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider(processing_healthy_flag=False)
        provider.add_results.append(
            AddRejected(
                request_id="server-rejection",
                error_code="INTERNAL_ERROR",
                server_fault=True,
            )
        )
        current = [datetime(2099, 1, 1, tzinfo=UTC)]
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
        _enqueue(store, "server-rejection")

        assert await worker.drain_once() == 1
        rejected = store.list_queue_rows()[0]
        assert (rejected.state, rejected.attempts, rejected.add_request_id) == (
            "dead",
            1,
            "server-rejection",
        )
        assert len(provider.captures) == 1
        assert await worker.drain_once() == 0
        assert len(provider.captures) == 1
        opened = store.get_meta()
        assert opened is not None
        assert opened.processing_fault_kind == "credential"
        assert opened.processing_alert_active is True
        assert [event[:2] for event in events] == [("fault", "credential")]
        failures = store.failure_log()
        assert len(failures) == 1
        assert (failures[0].operation, failures[0].state, failures[0].request_id) == (
            "add",
            "rejected",
            "server-rejection",
        )

        provider.processing_healthy_flag = True
        _enqueue(store, "recovery")
        assert await worker.drain_once() == 1
        closed = store.get_meta()
        assert closed is not None
        assert closed.processing_fault_since is None
        assert [event[:2] for event in events] == [
            ("fault", "credential"),
            ("recovered", None),
        ]

    asyncio.run(run())


def test_cancelled_server_rejection_commit_is_completed_once_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider(processing_healthy_flag=False)
        provider.add_results.append(
            AddRejected(
                request_id="cancelled-server-rejection",
                error_code="INTERNAL_ERROR",
                server_fault=True,
            )
        )
        events: list[tuple[str, str | None, str, int]] = []
        settle_committed = threading.Event()
        release_settle = threading.Event()

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        original_settle = store.settle

        def blocking_settle(*args, **kwargs):
            result = original_settle(*args, **kwargs)
            settle_committed.set()
            release_settle.wait(timeout=2)
            return result

        monkeypatch.setattr(store, "settle", blocking_settle)
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        _enqueue(store, "cancelled-server-rejection")

        draining = asyncio.create_task(worker.drain_once())
        assert await asyncio.to_thread(settle_committed.wait, 1)
        draining.cancel()
        await asyncio.sleep(0)
        assert draining.done() is False
        release_settle.set()
        with pytest.raises(asyncio.CancelledError):
            await draining

        rejected = store.list_queue_rows()[0]
        assert rejected.state == "dead"
        pending_fault = store.ensure_meta()
        assert pending_fault.processing_fault_since is not None
        assert pending_fault.processing_fault_kind is None
        assert pending_fault.processing_alert_active is False
        assert events == []
        assert len(provider.captures) == 1

        reopened_store = MemoryStore(store.path)
        restarted = SessionFlushCoordinator(
            store=reopened_store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await restarted.recover(lease_owner="next-boot")
        await restarted.recover(lease_owner="same-boot")
        assert [event[:2] for event in events] == [("fault", "credential")]
        assert len(provider.captures) == 1

        provider.processing_healthy_flag = True
        _enqueue(reopened_store, "server-rejection-recovery", session="recovery")
        restarted_worker = MemoryWorker(
            store=reopened_store,
            provider=provider,
            enabled=lambda: True,
            coordinator=restarted,
        )
        assert await restarted_worker.drain_once() == 1
        assert len(provider.captures) == 2
        assert reopened_store.ensure_meta().processing_fault_since is None
        assert [event[:2] for event in events] == [
            ("fault", "credential"),
            ("recovered", None),
        ]

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


def test_stale_flush_finalization_does_not_open_processing_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        current = datetime(2026, 1, 1, tzinfo=UTC)
        row = _enqueue(store, "stale-finalizer")
        claimed = store.claim_due(
            lease_owner="worker",
            now="2026-01-01T00:00:00.000Z",
        )
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
        assert store.mark_flush_submission_started(
            lease,
            now="2026-01-01T00:00:01.000Z",
        )
        assert store.settle_flush(
            lease,
            FlushSucceeded("first", "extracted"),
            now="2026-01-01T00:00:02.000Z",
        ).settled

        fault_opens = 0
        original_open = store.open_processing_fault

        def count_open(*, now: str) -> bool:
            nonlocal fault_opens
            fault_opens += 1
            return original_open(now=now)

        monkeypatch.setattr(store, "open_processing_fault", count_open)
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=FakeMemoryProvider(),
            enabled=lambda: True,
        )
        await coordinator._finalize_flush_outcome(
            lease,
            FlushUnknown(reason="transport"),
        )

        assert fault_opens == 0
        meta = store.get_meta()
        assert meta is not None and meta.processing_fault_since is None

    asyncio.run(run())


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


@pytest.mark.parametrize("operation", ["add", "flush"])
def test_boot_recovery_opens_and_emits_processing_fault_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        row = _enqueue(store, f"hard-crash-{operation}")
        claimed = store.claim_due(
            lease_owner="old-boot",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None
        if operation == "flush":
            assert store.settle_add_ack(
                claimed,
                AddAck("add-before-crash", "accumulated"),
                lease_owner="old-boot",
                now=datetime(2026, 1, 1, tzinfo=UTC),
                idle_timeout=timedelta(0),
            ).settled
            lease = store.acquire_flush(
                now="2026-01-01T00:00:01.000Z",
                provider_session_ref=row.provider_session_ref,
            )
            assert lease is not None
            assert store.mark_flush_submission_started(
                lease,
                now="2026-01-01T00:00:02.000Z",
            )

        open_fault = store._open_processing_fault_in_connection

        def fail_open_fault(_conn, *, now: str) -> bool:
            del now
            raise OSError("injected processing fault write failure")

        monkeypatch.setattr(
            store,
            "_open_processing_fault_in_connection",
            fail_open_fault,
        )
        with pytest.raises(OSError, match="injected processing fault write failure"):
            store.recover_after_boot(
                lease_owner="failed-new-boot",
                clock=lambda: datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
            )
        monkeypatch.setattr(
            store,
            "_open_processing_fault_in_connection",
            open_fault,
        )
        assert store.ensure_meta().processing_fault_since is None
        if operation == "add":
            assert store.list_queue_rows()[0].state == "processing"
        else:
            flush_state = store.get_session_flush_state(row.provider_session_ref)
            assert flush_state is not None and flush_state.state == "in_flight"

        recovery = store.recover_after_boot(
            lease_owner="crashed-new-boot",
            clock=lambda: datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
        )
        assert (recovery.reclaimed, recovery.interrupted_flushes) == (
            (1, 0) if operation == "add" else (0, 1)
        )
        recovered_fault_since = store.ensure_meta().processing_fault_since
        assert recovered_fault_since is not None

        events: list[tuple[str, str | None, str, int]] = []

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        coordinator = SessionFlushCoordinator(
            store=MemoryStore(store.path),
            provider=FakeMemoryProvider(processing_healthy_flag=True),
            enabled=lambda: True,
            now=lambda: datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
            processing_event=record_event,
        )

        await coordinator.recover(lease_owner="next-boot")
        await coordinator.recover(lease_owner="same-boot")

        meta = store.get_meta()
        assert meta is not None
        assert meta.processing_fault_since == recovered_fault_since
        assert meta.processing_fault_kind == "engine"
        assert meta.processing_alert_active is True
        assert [(event, kind) for event, kind, _at, _queued in events] == [
            ("fault", "engine")
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    "result",
    (
        FlushUnknown(reason="timeout"),
        FlushRejected(
            request_id="server-rejection",
            error_code="INTERNAL_ERROR",
            server_fault=True,
        ),
        FlushSucceeded(request_id=None, status="extracted"),
    ),
    ids=("unknown", "server-rejection", "malformed-success"),
)
def test_restart_finishes_submitted_flush_fault_notification_once(
    tmp_path: Path,
    result: FlushUnknown | FlushRejected | FlushSucceeded,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider(processing_healthy_flag=True)
        row = _enqueue(store, "submitted-flush-fault")
        claimed = store.claim_due(
            lease_owner="old-boot",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None
        assert store.settle_add_ack(
            claimed,
            AddAck("add-before-flush", "accumulated"),
            lease_owner="old-boot",
            now=datetime(2026, 1, 1, tzinfo=UTC),
        ).settled
        lease = store.acquire_flush(
            now="2026-01-01T00:00:01.000Z",
            provider_session_ref=row.provider_session_ref,
            force=True,
        )
        assert lease is not None
        assert store.mark_flush_submission_started(
            lease,
            now="2026-01-01T00:00:02.000Z",
        )

        # Simulate process loss immediately after the local settlement commit.
        assert store.settle_flush(
            lease,
            result,
            now="2026-01-01T00:00:03.000Z",
        ).settled
        pending = store.ensure_meta()
        assert pending.processing_fault_since == "2026-01-01T00:00:03.000Z"
        assert pending.processing_fault_kind is None
        assert pending.processing_alert_active is False

        events: list[tuple[str, str | None, str, int]] = []

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        restarted = SessionFlushCoordinator(
            store=MemoryStore(store.path),
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await restarted.recover(lease_owner="next-boot")
        await restarted.recover(lease_owner="same-boot")

        assert [event[:3] for event in events] == [
            ("fault", "engine", "2026-01-01T00:00:03.000Z")
        ]
        notified = store.ensure_meta()
        assert notified.processing_fault_kind == "engine"
        assert notified.processing_alert_active is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "success",
    ("add-accumulated", "add-extracted", "flush-succeeded"),
)
def test_restart_finishes_atomic_processing_recovery_notification_once(
    tmp_path: Path,
    success: str,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider(processing_healthy_flag=True)
        if success.startswith("add-"):
            assert store.open_processing_fault(now="2026-01-01T00:00:00.000Z")
            assert store.classify_processing_fault("engine")
            assert store.mark_processing_alert_active()

        row = _enqueue(store, f"recovery-{success}")
        claimed = store.claim_due(
            lease_owner="old-boot",
            now="2026-01-01T00:00:01.000Z",
        )
        assert claimed is not None
        if success.startswith("add-"):
            status = success.removeprefix("add-")
            assert store.settle_add_ack(
                claimed,
                AddAck(request_id=f"{success}-request", status=status),
                lease_owner="old-boot",
                now=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
            ).settled
        else:
            assert store.settle_add_ack(
                claimed,
                AddAck(request_id="add-before-flush", status="accumulated"),
                lease_owner="old-boot",
                now=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
            ).settled
            assert store.open_processing_fault(now="2026-01-01T00:00:02.500Z")
            assert store.classify_processing_fault("engine")
            assert store.mark_processing_alert_active()
            lease = store.acquire_flush(
                now="2026-01-01T00:00:03.000Z",
                provider_session_ref=row.provider_session_ref,
                force=True,
            )
            assert lease is not None
            assert store.mark_flush_submission_started(
                lease,
                now="2026-01-01T00:00:04.000Z",
            )
            assert store.settle_flush(
                lease,
                FlushSucceeded(request_id="flush-success", status="extracted"),
                now="2026-01-01T00:00:05.000Z",
            ).settled

        # The success and close are durable; only the external edge is pending.
        pending = store.ensure_meta()
        assert pending.processing_fault_since is None
        assert pending.processing_alert_active is False
        expected_at = (
            "2026-01-01T00:00:02.000Z"
            if success.startswith("add-")
            else "2026-01-01T00:00:05.000Z"
        )
        assert pending.processing_recovery_pending_at == expected_at

        events: list[tuple[str, str | None, str, int]] = []

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        restarted = SessionFlushCoordinator(
            store=MemoryStore(store.path),
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await restarted.recover(lease_owner="next-boot")
        await restarted.recover(lease_owner="same-boot")

        assert [event[:3] for event in events] == [
            ("recovered", None, expected_at)
        ]
        assert store.ensure_meta().processing_recovery_pending_at is None

    asyncio.run(run())


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
        fault = store.ensure_meta()
        assert fault.processing_fault_kind == "engine"
        assert fault.processing_alert_active is True

    asyncio.run(run())


def test_cancelled_exhausted_flush_retry_is_completed_once_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider(processing_healthy_flag=True)
        provider.flush_results.extend([FlushRetryable()] * 4)
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

        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            processing_event=record_event,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            coordinator=coordinator,
        )
        row = _enqueue(store, "cancelled-exhausted-flush")
        assert await worker.drain_once() == 1
        current[0] += timedelta(minutes=5)
        for delay in (1, 2, 4):
            assert await coordinator.run_due() == 1
            await _wait_for_scheduled_flush(coordinator, row.provider_session_ref)
            current[0] += timedelta(seconds=delay)

        retry_committed = threading.Event()
        release_retry = threading.Event()
        original_retry = store.retry_unsubmitted_flush

        def blocking_retry(*args, **kwargs):
            result = original_retry(*args, **kwargs)
            retry_committed.set()
            release_retry.wait(timeout=2)
            return result

        monkeypatch.setattr(store, "retry_unsubmitted_flush", blocking_retry)
        assert await coordinator.run_due() == 1
        flush_task = coordinator._flush_tasks[row.provider_session_ref.serialize()]
        assert await asyncio.to_thread(retry_committed.wait, 1)
        flush_task.cancel()
        await asyncio.sleep(0)
        assert flush_task.done() is False
        release_retry.set()
        with pytest.raises(asyncio.CancelledError):
            await flush_task

        terminal = store.get_session_flush_state(row.provider_session_ref)
        assert terminal is not None
        assert (terminal.state, terminal.retry_count) == ("manual_required", 4)
        pending_fault = store.ensure_meta()
        assert pending_fault.processing_fault_since is not None
        assert pending_fault.processing_fault_kind is None
        assert pending_fault.processing_alert_active is False
        assert events == []
        assert len(provider.flushes) == 4

        reopened_store = MemoryStore(store.path)
        restarted = SessionFlushCoordinator(
            store=reopened_store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            processing_event=record_event,
        )
        await restarted.recover(lease_owner="next-boot")
        await restarted.recover(lease_owner="same-boot")
        assert [event[:2] for event in events] == [("fault", "engine")]
        assert len(provider.flushes) == 4

        _enqueue(reopened_store, "flush-retry-recovery", session="recovery")
        restarted_worker = MemoryWorker(
            store=reopened_store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            coordinator=restarted,
        )
        assert await restarted_worker.drain_once() == 1
        assert reopened_store.ensure_meta().processing_fault_since is None
        assert [event[:2] for event in events] == [
            ("fault", "engine"),
            ("recovered", None),
        ]

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


def test_cancelled_flush_before_submission_coroutine_entry_remains_retryable(
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
        row = _enqueue(store, "cancelled-flush-before-entry")
        assert await worker.drain_once() == 1

        real_wait_for = asyncio.wait_for

        async def cancel_provider_submission(awaitable, *, timeout: float):
            code = getattr(awaitable, "cr_code", None)
            if code is not None and code.co_name == "_submit_provider_write":
                awaitable.close()
                raise asyncio.CancelledError
            return await real_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", cancel_provider_submission)
        with pytest.raises(asyncio.CancelledError):
            await coordinator.final_flush(
                row.provider_session_ref,
                deadline_seconds=1,
            )

        assert provider.flushes == []
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None
        assert state.state == "due"
        assert state.submission_started_at is None

        monkeypatch.setattr(asyncio, "wait_for", real_wait_for)
        assert await coordinator.final_flush(
            row.provider_session_ref,
            deadline_seconds=1,
        )
        assert provider.flushes == [row.provider_session_ref]

    asyncio.run(run())


def test_cancelled_flush_after_provider_entry_opens_one_fault_and_later_recovers(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        flush_entered = asyncio.Event()
        events: list[tuple[str, str | None, str, int]] = []

        class Provider(FakeMemoryProvider):
            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                if session_ref == cancelled.provider_session_ref:
                    flush_entered.set()
                    await asyncio.Event().wait()
                return FlushSucceeded("flush-recovery", "extracted")

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            coordinator=coordinator,
        )
        cancelled = _enqueue(store, "cancelled-flush", session="cancelled-flush")
        recovery = _enqueue(store, "recovery-flush", session="recovery-flush")
        assert await worker.drain(max_rows=2) == 2

        flush_call = asyncio.create_task(
            coordinator.final_flush(cancelled.provider_session_ref, deadline_seconds=5)
        )
        await asyncio.wait_for(flush_entered.wait(), timeout=1)
        flush_task = coordinator._flush_tasks[cancelled.provider_session_ref.serialize()]
        flush_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flush_call

        state = store.get_session_flush_state(cancelled.provider_session_ref)
        assert state is not None and state.state == "manual_required"
        failures = store.failure_log()
        assert len(failures) == 1
        assert (failures[0].operation, failures[0].state) == (
            "flush",
            "manual_required",
        )
        opened = store.get_meta()
        assert opened is not None and opened.processing_alert_active is True
        assert [event[:2] for event in events] == [("fault", "engine")]

        recovered = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await recovered.recover(lease_owner="next-boot")
        assert [event[:2] for event in events] == [("fault", "engine")]

        assert await recovered.final_flush(
            recovery.provider_session_ref,
            deadline_seconds=1,
        )
        closed = store.get_meta()
        assert closed is not None and closed.processing_fault_since is None
        assert [event[:2] for event in events] == [
            ("fault", "engine"),
            ("recovered", None),
        ]

    asyncio.run(run())


def test_shutdown_joins_post_entry_flush_classification_and_boot_recovers(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        flush_entered = asyncio.Event()
        health_entered = asyncio.Event()
        health_finished = asyncio.Event()
        release_health = asyncio.Event()
        events: list[tuple[str, str | None, str, int]] = []
        health_calls = 0

        class Provider(FakeMemoryProvider):
            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                flush_entered.set()
                await asyncio.Event().wait()

            async def processing_healthy(self) -> bool:
                nonlocal health_calls
                health_calls += 1
                if health_calls == 1:
                    health_entered.set()
                    try:
                        await release_health.wait()
                    finally:
                        health_finished.set()
                return True

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            coordinator=coordinator,
        )
        row = _enqueue(store, "shutdown-finalizer")
        assert await worker.drain_once() == 1
        assert await coordinator.run_due() == 0
        flush_task = coordinator._schedule(row.provider_session_ref, force=True)
        assert flush_task is not None
        await asyncio.wait_for(flush_entered.wait(), timeout=1)

        shutdown = asyncio.create_task(
            coordinator.prepare_shutdown(timeout_seconds=1)
        )
        await asyncio.wait_for(health_entered.wait(), timeout=1)
        assert not shutdown.done()

        # Cancel the blocked classification only after the durable local phase.
        flush_task.cancel()
        await asyncio.wait_for(shutdown, timeout=1)
        assert health_finished.is_set()
        assert flush_task.cancelled()
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "manual_required"
        pending_fault = store.get_meta()
        assert pending_fault is not None
        assert pending_fault.processing_fault_since is not None
        assert pending_fault.processing_fault_kind is None
        assert pending_fault.processing_alert_active is False
        assert events == []

        release_health.set()
        completed_failures = store.failure_log()
        completed_meta = store.get_meta()
        await asyncio.sleep(0)
        assert events == []
        assert store.failure_log() == completed_failures
        assert store.get_meta() == completed_meta

        recovered = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await recovered.recover(lease_owner="next-boot")
        assert [event[:2] for event in events] == [("fault", "engine")]
        recovered_meta = store.get_meta()
        assert recovered_meta is not None
        assert recovered_meta.processing_fault_kind == "engine"
        assert recovered_meta.processing_alert_active is True

    asyncio.run(run())


def test_shutdown_drains_local_flush_commit_then_boot_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        flush_entered = asyncio.Event()
        settle_entered = threading.Event()
        release_settle = threading.Event()
        events: list[tuple[str, str | None, str, int]] = []
        health_calls = 0

        class Provider(FakeMemoryProvider):
            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                flush_entered.set()
                await asyncio.Event().wait()

            async def processing_healthy(self) -> bool:
                nonlocal health_calls
                health_calls += 1
                return True

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        original_settle = store.settle_flush

        def blocking_settle(lease, result, *, now: str):
            settle_entered.set()
            release_settle.wait(timeout=2)
            return original_settle(lease, result, now=now)

        monkeypatch.setattr(store, "settle_flush", blocking_settle)
        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            coordinator=coordinator,
        )
        row = _enqueue(store, "shutdown-local-commit")
        assert await worker.drain_once() == 1
        flush_task = coordinator._schedule(row.provider_session_ref, force=True)
        assert flush_task is not None
        await asyncio.wait_for(flush_entered.wait(), timeout=1)

        shutdown = asyncio.create_task(
            coordinator.prepare_shutdown(timeout_seconds=0.01)
        )
        assert await asyncio.to_thread(settle_entered.wait, 1)

        async def wait_for_second_cancel() -> None:
            while flush_task.cancelling() < 2:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_second_cancel(), timeout=1)
        assert not shutdown.done()
        release_settle.set()
        await asyncio.wait_for(shutdown, timeout=1)

        assert flush_task.cancelled()
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "manual_required"
        pending_fault = store.get_meta()
        assert pending_fault is not None
        assert pending_fault.processing_fault_since is not None
        assert pending_fault.processing_fault_kind is None
        assert pending_fault.processing_alert_active is False
        assert health_calls == 0
        assert events == []

        recovered = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await recovered.recover(lease_owner="next-boot")
        assert health_calls == 1
        assert [event[:2] for event in events] == [("fault", "engine")]

    asyncio.run(run())


def test_shutdown_local_flush_phase_does_not_wait_for_classification_lock(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        flush_entered = asyncio.Event()
        old_health_entered = asyncio.Event()
        release_old_health = asyncio.Event()
        events: list[tuple[str, str | None, str, int]] = []
        health_calls = 0

        class Provider(FakeMemoryProvider):
            async def flush(self, session_ref):
                self.flushes.append(session_ref)
                flush_entered.set()
                await asyncio.Event().wait()

            async def processing_healthy(self) -> bool:
                nonlocal health_calls
                health_calls += 1
                if health_calls == 1:
                    old_health_entered.set()
                    await release_old_health.wait()
                return True

        async def record_event(
            event: str,
            kind: str | None,
            occurred_at: str,
            queued: int,
        ) -> bool:
            events.append((event, kind, occurred_at, queued))
            return True

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        worker = MemoryWorker(
            store=store,
            provider=provider,
            enabled=lambda: True,
            coordinator=coordinator,
        )
        row = _enqueue(store, "shutdown-lock-boundary")
        assert await worker.drain_once() == 1

        old_classification = asyncio.create_task(coordinator._open_processing_fault())
        await asyncio.wait_for(old_health_entered.wait(), timeout=1)
        flush_task = coordinator._schedule(row.provider_session_ref, force=True)
        assert flush_task is not None
        await asyncio.wait_for(flush_entered.wait(), timeout=1)

        started = asyncio.get_running_loop().time()
        await coordinator.prepare_shutdown(timeout_seconds=0.01)
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 0.2
        assert not release_old_health.is_set()
        assert not old_classification.done()
        assert flush_task.cancelled()
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "manual_required"
        pending_fault = store.get_meta()
        assert pending_fault is not None
        assert pending_fault.processing_fault_since is not None
        assert pending_fault.processing_fault_kind is None
        assert pending_fault.processing_alert_active is False
        assert events == []

        old_classification.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old_classification
        assert events == []

        recovered = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            processing_event=record_event,
        )
        await recovered.recover(lease_owner="next-boot")
        await recovered.recover(lease_owner="same-boot")
        assert health_calls == 2
        assert [event[:2] for event in events] == [("fault", "engine")]

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


@pytest.mark.parametrize("fenced", [False, True], ids=["ordinary", "fenced"])
def test_claim_revalidation_error_returns_exact_claim_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fenced: bool,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider = FakeMemoryProvider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        row = _enqueue(store, "claim-revalidation", session=f"fenced-{fenced}")
        original_claim_is_current = store.claim_is_current
        calls = 0

        def fail_once(claimed: QueueRow, *, lease_owner: str) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("transient store read failure")
            return original_claim_is_current(claimed, lease_owner=lease_owner)

        monkeypatch.setattr(store, "claim_is_current", fail_once)
        if fenced:
            assert not await coordinator.final_flush(
                row.provider_session_ref,
                deadline_seconds=1,
            )
            state = store.get_session_flush_state(row.provider_session_ref)
            assert state is not None and state.state == "due"
        else:
            claimed = store.claim_due(
                lease_owner="ordinary-worker",
                now="2026-01-01T00:00:00.000Z",
            )
            assert claimed is not None
            assert not await coordinator.deliver(
                claimed,
                lease_owner="ordinary-worker",
            )

        queued = store.list_queue_rows()[0]
        assert (queued.state, queued.attempts) == ("pending", 0)
        assert provider.captures == []
        assert store.failure_log() == ()
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None
        assert state.state == ("due" if fenced else "idle")

        if fenced:
            assert await coordinator.final_flush(
                row.provider_session_ref,
                deadline_seconds=1,
            )
            assert provider.flushes == [row.provider_session_ref]
        else:
            retry = store.claim_due(
                lease_owner="retry-worker",
                now="2026-01-01T00:00:01.000Z",
            )
            assert retry is not None
            assert await coordinator.deliver(retry, lease_owner="retry-worker")
        assert [capture.text for capture in provider.captures] == [
            "payload-claim-revalidation"
        ]

    asyncio.run(run())


def test_cancelled_add_before_submission_coroutine_entry_returns_exact_claim(
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
        _enqueue(store, "cancelled-before-entry")
        claimed = store.claim_due(
            lease_owner="worker",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None

        wait_for_calls = 0

        async def cancel_before_entry(awaitable, *, timeout: float):
            nonlocal wait_for_calls
            wait_for_calls += 1
            awaitable.close()
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "wait_for", cancel_before_entry)
        with pytest.raises(asyncio.CancelledError):
            await coordinator.deliver(claimed, lease_owner="worker")

        assert wait_for_calls == 1
        assert provider.captures == []
        queued = store.list_queue_rows()[0]
        assert (queued.state, queued.attempts) == ("pending", 0)
        recovery = store.recover_after_boot(
            lease_owner="next-boot",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert recovery.reclaimed == 0

    asyncio.run(run())


def test_cancelled_add_after_provider_entry_remains_ambiguous(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        provider_entered = asyncio.Event()

        class Provider(FakeMemoryProvider):
            async def add(self, capture):
                self.captures.append(capture)
                provider_entered.set()
                await asyncio.Event().wait()

        provider = Provider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        row = _enqueue(store, "cancelled-after-entry")
        claimed = store.claim_due(
            lease_owner="worker",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None

        delivery = asyncio.create_task(
            coordinator.deliver(claimed, lease_owner="worker")
        )
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery

        assert len(provider.captures) == 1
        assert store.list_queue_rows()[0].state == "processing"
        recovery = store.recover_after_boot(
            lease_owner="next-boot",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert recovery.reclaimed == 1
        assert store.list_queue_rows()[0].state == "manual_required"
        state = store.get_session_flush_state(row.provider_session_ref)
        assert state is not None and state.state == "manual_required"

    asyncio.run(run())


def test_attachment_preflight_failure_is_bounded_without_session_fence(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        attachment_store, bundle, pinned_path = _pin_attachment_bundle()
        row = _enqueue_attachment_bundle(
            store,
            bundle,
            source="broken-attachment",
        )
        pinned_path.chmod(0o644)

        provider = FakeMemoryProvider()
        current = [datetime(2026, 1, 1, tzinfo=UTC)]
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            attachment_store=attachment_store,
        )
        await coordinator.recover(lease_owner="initial-boot")

        for expected_attempts, delay in ((1, 30), (2, 120), (3, 0)):
            claimed = store.claim_due(
                lease_owner=f"worker-{expected_attempts}",
                now=current[0].isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
            assert claimed is not None
            assert not await coordinator.deliver(
                claimed,
                lease_owner=f"worker-{expected_attempts}",
            )
            queued = store.list_queue_rows()[0]
            assert queued.attempts == expected_attempts
            expected_state = "dead" if expected_attempts == 3 else "pending"
            assert queued.state == expected_state
            session_state = store.get_session_flush_state(row.provider_session_ref)
            assert session_state is not None and session_state.state == "idle"
            current[0] += timedelta(seconds=delay)

        assert store.attachment_bundle_sets() == (
            frozenset(),
            frozenset({bundle.bundle_id}),
        )
        restarted = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
            now=lambda: current[0],
            attachment_store=attachment_store,
        )
        await restarted.recover(lease_owner="restarted-worker")
        assert store.attachment_bundle_sets() == (
            frozenset(),
            frozenset({bundle.bundle_id}),
        )

        later = _enqueue(
            store,
            "after-broken-attachment",
            session="attachment-session",
        )
        unrelated = _enqueue(
            store,
            "unrelated-after-broken-attachment",
            session="unrelated-session",
        )
        for expected in (later, unrelated):
            claimed = store.claim_due(
                lease_owner="later-worker",
                now=current[0].isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
            assert claimed is not None
            assert await restarted.deliver(claimed, lease_owner="later-worker")
        assert [capture.text for capture in provider.captures] == [
            later.payload_text,
            unrelated.payload_text,
        ]

    asyncio.run(run())


def test_successful_boot_attachment_reconcile_finalizes_release(tmp_path: Path) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        attachment_store, bundle, pinned_path = _pin_attachment_bundle()
        _enqueue_attachment_bundle(
            store,
            bundle,
            source="boot-release",
        )
        claimed = store.claim_due(
            lease_owner="crashed-worker",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None
        settled = store.settle(
            claimed,
            MessageFailure(error="memory_store_unavailable", retryable=False),
            lease_owner="crashed-worker",
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert settled.attachment_release_id == bundle.bundle_id
        assert pinned_path.exists()

        coordinator = SessionFlushCoordinator(
            store=store,
            provider=FakeMemoryProvider(),
            enabled=lambda: True,
            attachment_store=attachment_store,
        )
        await coordinator.recover(lease_owner="next-boot")

        assert store.attachment_bundle_sets() == (frozenset(), frozenset())
        assert not pinned_path.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    (
        "case",
        "payload",
        "bundle_id",
        "bundle_relative_path",
        "file_count",
        "total_bytes",
        "expected_state",
        "expected_attempts",
    ),
    [
        ("manifest_without_bundle", "{}", None, None, 0, 0, "dead", 1),
        (
            "bundle_without_manifest",
            None,
            TEST_BUNDLE_ID,
            f"bundles/{TEST_BUNDLE_ID}",
            1,
            1,
            "dead",
            1,
        ),
        (
            "pin_store_unavailable",
            "{}",
            TEST_BUNDLE_ID,
            f"bundles/{TEST_BUNDLE_ID}",
            1,
            1,
            "pending",
            0,
        ),
    ],
)
def test_attachment_preflight_classifies_local_failures_without_ambiguity(
    tmp_path: Path,
    case: str,
    payload: str | None,
    bundle_id: str | None,
    bundle_relative_path: str | None,
    file_count: int,
    total_bytes: int,
    expected_state: str,
    expected_attempts: int,
) -> None:
    async def run() -> None:
        store = _store(tmp_path)
        result = store.enqueue_request(
            source_message_id=f"attachment-{case}",
            session_id=f"attachment-{case}",
            principal_id=PRINCIPAL,
            project_ref=PROJECT,
            provenance="user_input",
            payload_text="remember the attachment",
            payload_attachments=payload,
            attachment_bundle_id=bundle_id,
            attachment_bundle_relative_path=bundle_relative_path,
            attachment_file_count=file_count,
            attachment_total_bytes=total_bytes,
            occurred_at_ms=1_000,
            max_provider_timestamp_ms=4_102_444_800_000,
        )
        assert result.row is not None
        provider = FakeMemoryProvider()
        coordinator = SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=lambda: True,
        )
        claimed = store.claim_due(
            lease_owner="worker",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None

        assert not await coordinator.deliver(claimed, lease_owner="worker")

        queued = store.list_queue_rows()[0]
        assert (queued.state, queued.attempts) == (
            expected_state,
            expected_attempts,
        )
        assert provider.captures == []
        state = store.get_session_flush_state(result.row.provider_session_ref)
        assert state is not None and state.state == "idle"
        assert store.ensure_meta().processing_fault_since is None

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


def test_cancelled_routine_claim_acquisition_returns_exact_claim(
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
            boot_id="ordinary-worker",
            coordinator=coordinator,
        )
        _enqueue(store, "cancelled-claim")
        claim_committed = threading.Event()
        release_claim = threading.Event()
        original_claim_due = store.claim_due

        def blocked_claim_due(*, lease_owner: str, now: str):
            row = original_claim_due(lease_owner=lease_owner, now=now)
            claim_committed.set()
            release_claim.wait(timeout=2)
            return row

        monkeypatch.setattr(store, "claim_due", blocked_claim_due)
        draining = asyncio.create_task(worker.drain_once())
        assert await asyncio.to_thread(claim_committed.wait, 1)

        draining.cancel()
        await asyncio.sleep(0)
        assert not draining.done()
        release_claim.set()
        with pytest.raises(asyncio.CancelledError):
            await draining

        queued = store.list_queue_rows()
        assert len(queued) == 1
        assert queued[0].state == "pending"
        assert queued[0].lease_owner is None
        assert queued[0].attempts == 0
        assert provider.captures == []

    asyncio.run(run())


def test_cancelled_fenced_claim_acquisition_returns_exact_claim(
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
        row = _enqueue(store, "cancelled-fenced-claim")
        claim_committed = threading.Event()
        release_claim = threading.Event()
        original_claim = store.claim_fenced_generation

        def blocked_claim(lease, *, lease_owner: str, now: str):
            claimed = original_claim(lease, lease_owner=lease_owner, now=now)
            claim_committed.set()
            release_claim.wait(timeout=2)
            return claimed

        monkeypatch.setattr(store, "claim_fenced_generation", blocked_claim)
        flushing = coordinator._schedule(row.provider_session_ref, force=True)
        assert flushing is not None
        assert await asyncio.to_thread(claim_committed.wait, 1)

        flushing.cancel()
        await asyncio.sleep(0)
        assert not flushing.done()
        release_claim.set()
        with pytest.raises(asyncio.CancelledError):
            await flushing

        queued = store.list_queue_rows()
        assert len(queued) == 1
        assert queued[0].state == "pending"
        assert queued[0].lease_owner is None
        assert queued[0].attempts == 0
        assert provider.captures == []

    asyncio.run(run())


def test_session_lock_is_shared_with_waiters_then_reclaimed(tmp_path: Path) -> None:
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
        _enqueue(store, "lock-owner", session="shared-lock")
        _enqueue(store, "lock-waiter", session="shared-lock")
        claimed = [
            row
            for row in (
                store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z"),
                store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z"),
            )
            if row is not None
        ]
        key = claimed[0].provider_session_ref.serialize()

        owner = asyncio.create_task(coordinator.deliver(claimed[0], lease_owner="worker"))
        await asyncio.wait_for(first_add_entered.wait(), timeout=1)
        waiter = asyncio.create_task(coordinator.deliver(claimed[1], lease_owner="worker"))
        await asyncio.sleep(0)
        assert waiter.done() is False
        assert len(coordinator._session_locks) == 1
        shared_lock = coordinator._session_locks[key]

        release_first_add.set()
        assert await asyncio.wait_for(owner, timeout=1)
        assert await asyncio.wait_for(waiter, timeout=1)
        assert [capture.text for capture in provider.captures] == [
            "payload-lock-owner",
            "payload-lock-waiter",
        ]

        del shared_lock, owner, waiter
        await asyncio.sleep(0)
        gc.collect()
        assert key not in coordinator._session_locks

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
