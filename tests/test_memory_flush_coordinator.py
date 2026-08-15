from __future__ import annotations

import asyncio
import errno
import gc
import sqlite3
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import paths
from core.memory import attachments as attachment_module
from core.memory import confined_filesystem as confined_filesystem_module
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
from core.memory.store import (
    AmbiguousAdd,
    Delivered,
    MemoryStore,
    MessageFailure,
    ProcessingHealthCommit,
    ProcessingHealthProbe,
    ProcessingNotification,
    QueueRow,
    SystemOutage,
)
from core.memory.types import CaptureAttachment, ProviderSessionRef
from core.memory.worker import MemoryWorker


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "default"
TEST_BUNDLE_ID = "a" * 32


class _Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._release = asyncio.Event()

    async def __call__(self, *_args: object) -> None:
        self.entered.set()
        await self._release.wait()

    def open(self) -> None:
        self._release.set()


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


def _open_store_processing_fault(
    store: MemoryStore,
    source: str,
    *,
    at: datetime,
) -> QueueRow:
    _enqueue(store, source, session=source)
    claimed = store.claim_due(lease_owner="setup", now=at.isoformat())
    assert claimed is not None
    assert store.settle(
        claimed,
        SystemOutage(error="memory_processing_failed"),
        lease_owner="setup",
        now=at,
    ).settled
    return claimed


def _classify_and_ack_store_processing_fault(store: MemoryStore) -> None:
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    notification = store.next_processing_action()
    assert isinstance(notification, ProcessingNotification)
    assert store.acknowledge_processing_notification(notification)


def _close_store_processing_fault(store: MemoryStore, *, at: datetime) -> None:
    claimed = store.claim_due(lease_owner="setup", now=at.isoformat())
    assert claimed is not None
    assert store.settle(
        claimed,
        Delivered(add_request_id="setup-success"),
        lease_owner="setup",
        now=at,
    ).settled


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
    payload_text: str = "remember the attachment",
) -> QueueRow:
    result = store.enqueue_request(
        source_message_id=source,
        session_id="attachment-session",
        principal_id=PRINCIPAL,
        project_ref=PROJECT,
        provenance="user_input",
        payload_text=payload_text,
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


async def _wait_for_processing_actions(
    coordinator: SessionFlushCoordinator,
) -> None:
    task = coordinator._processing_task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=2)


async def _run_processing_actions(
    coordinator: SessionFlushCoordinator,
) -> None:
    coordinator._schedule_processing_actions()
    await _wait_for_processing_actions(coordinator)


async def _run_due_and_wait(
    coordinator: SessionFlushCoordinator,
    *,
    max_sessions: int = 8,
) -> int:
    scheduled = await coordinator.run_due(max_sessions=max_sessions)
    await _wait_for_processing_actions(coordinator)
    return scheduled


async def test_accumulated_add_waits_for_idle_flush(tmp_path: Path) -> None:
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
    assert await _run_due_and_wait(worker.coordinator) == 1
    await _wait_for_scheduled_flush(worker.coordinator, row.provider_session_ref)

    assert provider.flushes == [row.provider_session_ref]
    state = store.get_session_flush_state(row.provider_session_ref)
    assert state is not None and state.state == "idle"
    assert state.open_generation == 2


async def test_tick_runs_processing_action_without_due_sessions(tmp_path: Path) -> None:
    """MEMORY-IM-ATTACH-001: the periodic owner never skips a durable action."""

    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "tick-only-processing-action",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    health_calls = 0
    events: list[tuple[str, str | None]] = []

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            nonlocal health_calls
            health_calls += 1
            return True

    async def notify(event, kind, _occurred_at, _queued) -> bool:
        events.append((event, kind))
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
        processing_event=notify,
    )

    assert await coordinator.run_due() == 0
    await _wait_for_processing_actions(coordinator)

    assert health_calls == 1
    assert events == [("fault", "engine")]
    assert store.next_processing_action() is None


async def test_processing_probe_runs_after_exact_session_lock_release(tmp_path: Path) -> None:
    """MEMORY-IM-ATTACH-003: provider health is outside the delivery fence."""

    store = _store(tmp_path)
    health_entered = asyncio.Event()
    release_health = asyncio.Event()

    class Provider(FakeMemoryProvider):
        async def add(self, capture):
            self.captures.append(capture)
            raise MemoryProviderFailure("memory_processing_failed", retryable=True)

        async def processing_healthy(self) -> bool:
            health_entered.set()
            await release_health.wait()
            return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
    )
    row = _enqueue(store, "lock-free-processing-probe")
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None

    assert not await coordinator.deliver(claimed, lease_owner="worker")
    session_lock = coordinator._session_lock(row.provider_session_ref.serialize())
    assert not session_lock.locked()
    assert await coordinator.run_due() == 0
    await asyncio.wait_for(health_entered.wait(), timeout=1)
    assert not session_lock.locked()

    release_health.set()
    await _wait_for_processing_actions(coordinator)


async def test_boot_recovery_leaves_processing_action_for_next_tick(tmp_path: Path) -> None:
    """MEMORY-IM-ATTACH-003: recovery restores state without running the probe."""

    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "restart-before-processing-probe",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    health_calls = 0

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            nonlocal health_calls
            health_calls += 1
            return True

    restarted = SessionFlushCoordinator(
        store=MemoryStore(store.path),
        provider=Provider(),
        enabled=lambda: True,
    )

    await restarted.recover(lease_owner="next-boot")
    assert health_calls == 0
    assert isinstance(store.next_processing_action(), ProcessingHealthProbe)

    assert await restarted.run_due() == 0
    await _wait_for_processing_actions(restarted)
    assert health_calls == 1
    assert store.next_processing_action() is None


async def test_stale_processing_probe_cannot_commit_across_generation(
    tmp_path: Path,
) -> None:
    """MEMORY-IM-ATTACH-003: a superseded probe remains fail-closed."""

    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "stale-processing-generation-1",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    health_entered = asyncio.Event()
    release_health = asyncio.Event()
    events: list[str] = []

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            health_entered.set()
            await release_health.wait()
            return True

    async def notify(event, _kind, _occurred_at, _queued) -> bool:
        events.append(event)
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
        processing_event=notify,
    )
    assert await coordinator.run_due() == 0
    await asyncio.wait_for(health_entered.wait(), timeout=1)

    _close_store_processing_fault(
        store,
        at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    _open_store_processing_fault(
        store,
        "stale-processing-generation-2",
        at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
    )
    release_health.set()
    await _wait_for_processing_actions(coordinator)

    current = store.next_processing_action()
    assert isinstance(current, ProcessingHealthProbe)
    assert current.generation == 2
    assert store.ensure_meta().processing_fault_kind is None
    assert events == []


async def test_blocked_processing_probe_does_not_serialize_other_session(
    tmp_path: Path,
) -> None:
    """MEMORY-IM-ATTACH-001: independent failure settlement stays concurrent."""

    store = _store(tmp_path)
    fault_row = _enqueue(store, "blocked-probe-session", session="blocked-probe-session")
    claimed = store.claim_due(
        lease_owner="setup",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed.source_message_digest == fault_row.source_message_digest
    assert store.settle(
        claimed,
        AmbiguousAdd(error="memory_provider_timeout"),
        lease_owner="setup",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ).settled
    _enqueue(store, "independent-session", session="independent-session")
    independent = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:01.000Z",
    )
    assert independent is not None
    health_entered = asyncio.Event()
    release_health = asyncio.Event()

    class Provider(FakeMemoryProvider):
        async def add(self, capture):
            self.captures.append(capture)
            raise MemoryProviderFailure("memory_processing_failed", retryable=True)

        async def processing_healthy(self) -> bool:
            health_entered.set()
            await release_health.wait()
            return True

    provider = Provider()
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
    )
    assert await coordinator.run_due() == 0

    await asyncio.wait_for(health_entered.wait(), timeout=1)
    assert not await asyncio.wait_for(
        coordinator.deliver(independent, lease_owner="worker"),
        timeout=1,
    )
    session_lock = coordinator._session_lock(
        independent.provider_session_ref.serialize()
    )
    assert not session_lock.locked()

    release_health.set()
    await _wait_for_processing_actions(coordinator)


async def test_quiescence_cancels_and_joins_processing_action(tmp_path: Path) -> None:
    """MEMORY-IM-ATTACH-003: lifecycle quiescence retires the active probe."""

    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "quiesce-processing-probe",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    health_entered = asyncio.Event()
    health_finished = asyncio.Event()

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            health_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                health_finished.set()

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
    )
    assert await coordinator.run_due() == 0
    await asyncio.wait_for(health_entered.wait(), timeout=1)

    assert await coordinator.pause_and_wait(timeout_seconds=1)
    assert health_finished.is_set()
    assert isinstance(store.next_processing_action(), ProcessingHealthProbe)


async def test_processing_probe_error_retries_on_tick_after_backoff(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "probe-pending",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    expected = store.next_processing_action()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    health_attempts = 0
    events: list[str] = []

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            nonlocal health_attempts
            health_attempts += 1
            if health_attempts == 1:
                raise RuntimeError("health failed")
            return True

    async def notify(event, _kind, _occurred_at, _queued) -> bool:
        events.append(event)
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
        now=lambda: current[0],
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="next-boot")
    await _run_processing_actions(coordinator)

    assert health_attempts == 1
    assert store.next_processing_action() == expected
    assert await _run_due_and_wait(coordinator) == 0
    current[0] += timedelta(seconds=4)
    assert await _run_due_and_wait(coordinator) == 0
    assert health_attempts == 1
    assert events == []

    current[0] += timedelta(seconds=1)
    assert await _run_due_and_wait(coordinator) == 0
    assert health_attempts == 2
    assert events == ["fault"]
    assert store.next_processing_action() is None


async def test_add_settlement_does_not_bypass_processing_probe_backoff(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "probe-before-add",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    health_attempts = 0
    events: list[str] = []

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            nonlocal health_attempts
            health_attempts += 1
            if health_attempts == 1:
                raise RuntimeError("health failed")
            return True

        async def add(self, capture):
            self.captures.append(capture)
            raise MemoryProviderFailure("memory_processing_failed", retryable=True)

    async def notify(event, _kind, _occurred_at, _queued) -> bool:
        events.append(event)
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
        now=lambda: current[0],
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="next-boot")
    await _run_processing_actions(coordinator)
    _enqueue(store, "activity-before-probe-retry", session="activity")
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:01.000Z",
    )
    assert claimed is not None

    assert not await coordinator.deliver(claimed, lease_owner="worker")
    assert health_attempts == 1
    assert events == []
    assert isinstance(store.next_processing_action(), ProcessingHealthProbe)

    current[0] += timedelta(seconds=5)
    assert await _run_due_and_wait(coordinator) == 0
    assert health_attempts == 2
    assert events == ["fault"]
    assert store.next_processing_action() is None


async def test_processing_probe_cancellation_does_not_schedule_retry(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "probe-cancelled",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    expected = store.next_processing_action()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    health_attempts = 0

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            nonlocal health_attempts
            health_attempts += 1
            raise asyncio.CancelledError

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
        now=lambda: current[0],
    )
    await coordinator.recover(lease_owner="next-boot")
    with pytest.raises(asyncio.CancelledError):
        await _run_due_and_wait(coordinator)

    current[0] += timedelta(minutes=1)
    with pytest.raises(asyncio.CancelledError):
        await _run_due_and_wait(coordinator)
    assert health_attempts == 2
    assert store.next_processing_action() == expected


@pytest.mark.parametrize("failure", ["false", "error"])
async def test_processing_notification_failure_retries_on_tick_after_backoff(
    tmp_path: Path,
    failure: str,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "notify-pending",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    expected = store.next_processing_action()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    attempts = 0

    async def notify(*_args) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return True
        if failure == "error":
            raise RuntimeError("notify failed")
        return False

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        now=lambda: current[0],
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="next-boot")
    await _run_processing_actions(coordinator)

    assert attempts == 1
    assert store.next_processing_action() == expected
    assert await _run_due_and_wait(coordinator) == 0
    assert attempts == 1

    current[0] += timedelta(seconds=5)
    assert await _run_due_and_wait(coordinator) == 0
    assert attempts == 2
    assert store.next_processing_action() is None


async def test_flush_settlement_does_not_bypass_processing_notification_backoff(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    row = _enqueue(store, "flush-before-notification-retry")
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    assert store.settle_add_ack(
        claimed,
        AddAck("before-flush", "accumulated"),
        lease_owner="worker",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ).settled
    lease = store.begin_flush_attempt(
        now="2026-01-01T00:00:01.000Z",
        provider_session_ref=row.provider_session_ref,
        force=True,
    )
    assert lease is not None
    assert store.begin_flush_submission(
        lease,
        now="2026-01-01T00:00:02.000Z",
    )
    _open_store_processing_fault(
        store,
        "notification-before-flush",
        at=datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
    )
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    current = [datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)]
    notification_attempts = 0

    async def notify(*_args) -> bool:
        nonlocal notification_attempts
        notification_attempts += 1
        return notification_attempts > 1

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        now=lambda: current[0],
        processing_event=notify,
    )
    await coordinator._reconcile_processing_events()

    await coordinator._finalize_flush_outcome(
        lease,
        FlushUnknown(reason="transport"),
    )
    assert notification_attempts == 1
    assert isinstance(store.next_processing_action(), ProcessingNotification)

    current[0] += timedelta(seconds=5)
    assert await _run_due_and_wait(coordinator) == 0
    assert notification_attempts == 2
    assert store.next_processing_action() is None


@pytest.mark.parametrize("stop", ["pause", "shutdown"])
async def test_processing_retry_does_not_run_while_paused_or_shutdown(
    tmp_path: Path,
    stop: str,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        f"retry-{stop}",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    health_attempts = 0

    class Provider(FakeMemoryProvider):
        async def processing_healthy(self) -> bool:
            nonlocal health_attempts
            health_attempts += 1
            raise RuntimeError("health failed")

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=Provider(),
        enabled=lambda: True,
        now=lambda: current[0],
    )
    await coordinator.recover(lease_owner="next-boot")
    await _run_processing_actions(coordinator)
    if stop == "pause":
        coordinator.pause()
    else:
        await coordinator.prepare_shutdown()

    current[0] += timedelta(minutes=1)
    assert await _run_due_and_wait(coordinator) == 0
    assert health_attempts == 1
    assert isinstance(store.next_processing_action(), ProcessingHealthProbe)


async def test_processing_notification_cancellation_leaves_action_pending(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "notify-cancelled",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    expected = store.next_processing_action()
    entered = asyncio.Event()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    attempts = 0

    async def notify(*_args) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return True
        entered.set()
        await asyncio.Event().wait()
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        now=lambda: current[0],
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="next-boot")
    assert await coordinator.run_due() == 0
    recovery = coordinator._processing_task
    assert recovery is not None
    await asyncio.wait_for(entered.wait(), timeout=1)
    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery

    current[0] += timedelta(minutes=1)
    assert await _run_due_and_wait(coordinator) == 0
    assert attempts == 2
    assert store.next_processing_action() is None


async def test_processing_notification_is_at_least_once_when_ack_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "at-least-once",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    events: list[tuple[str, str | None, str, int]] = []

    async def notify(event, kind, occurred_at, queued) -> bool:
        events.append((event, kind, occurred_at, queued))
        return True

    original_ack = store.acknowledge_processing_notification

    def fail_ack(_notification: ProcessingNotification) -> bool:
        raise OSError("ack unavailable")

    monkeypatch.setattr(store, "acknowledge_processing_notification", fail_ack)
    first = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        processing_event=notify,
    )
    await first.recover(lease_owner="first-boot")
    with pytest.raises(OSError, match="ack unavailable"):
        await _run_due_and_wait(first)

    monkeypatch.setattr(store, "acknowledge_processing_notification", original_ack)
    restarted = SessionFlushCoordinator(
        store=MemoryStore(store.path),
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        processing_event=notify,
    )
    await restarted.recover(lease_owner="second-boot")
    await _run_processing_actions(restarted)

    assert len(events) == 2
    assert events[0] == events[1]


async def test_processing_notification_ack_suppresses_later_replays(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "ack-suppresses-replay",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    events: list[tuple[str, str | None, str, int]] = []

    async def notify(event, kind, occurred_at, queued) -> bool:
        events.append((event, kind, occurred_at, queued))
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(processing_healthy_flag=True),
        enabled=lambda: True,
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="first-boot")
    await _run_processing_actions(coordinator)
    await coordinator.recover(lease_owner="second-boot")
    await _run_processing_actions(coordinator)

    assert len(events) == 1


async def test_successful_add_waits_for_blocked_fault_notification_ack(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "blocked-fault-close",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:01.000Z",
    )
    assert claimed is not None
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()
    events: list[str] = []

    async def notify(event, _kind, _occurred_at, _queued) -> bool:
        events.append(event)
        if event == "fault":
            callback_entered.set()
            await release_callback.wait()
        return True

    provider = FakeMemoryProvider()
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        processing_event=notify,
    )
    reconciliation = asyncio.create_task(coordinator._reconcile_processing_events())
    await asyncio.wait_for(callback_entered.wait(), timeout=1)
    success = asyncio.create_task(
        coordinator.deliver(claimed, lease_owner="worker")
    )

    async def wait_for_provider_add() -> None:
        while not provider.captures:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_provider_add(), timeout=1)
    await asyncio.sleep(0)
    assert not success.done()
    assert store.ensure_meta().processing_fault_since is not None

    release_callback.set()
    await asyncio.wait_for(reconciliation, timeout=1)
    assert await asyncio.wait_for(success, timeout=1)
    await _run_due_and_wait(coordinator)

    assert events == ["fault", "recovered"]
    assert store.next_processing_action() is None


async def test_successful_flush_waits_for_blocked_fault_notification_ack(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    row = _enqueue(store, "blocked-flush-close")
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    assert store.settle_add_ack(
        claimed,
        AddAck("before-flush", "accumulated"),
        lease_owner="worker",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ).settled
    lease = store.begin_flush_attempt(
        now="2026-01-01T00:00:01.000Z",
        provider_session_ref=row.provider_session_ref,
        force=True,
    )
    assert lease is not None
    assert store.begin_flush_submission(
        lease,
        now="2026-01-01T00:00:02.000Z",
    )
    _open_store_processing_fault(
        store,
        "blocked-flush-fault",
        at=datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
    )
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()
    events: list[str] = []

    async def notify(event, _kind, _occurred_at, _queued) -> bool:
        events.append(event)
        if event == "fault":
            callback_entered.set()
            await release_callback.wait()
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        processing_event=notify,
    )
    reconciliation = asyncio.create_task(coordinator._reconcile_processing_events())
    await asyncio.wait_for(callback_entered.wait(), timeout=1)
    success = asyncio.create_task(
        coordinator._finalize_flush_outcome(
            lease,
            FlushSucceeded("flush-success", "extracted"),
        )
    )
    await asyncio.sleep(0)
    assert not success.done()
    state = store.get_session_flush_state(row.provider_session_ref)
    assert state is not None and state.state == "in_flight"
    assert store.ensure_meta().processing_fault_since is not None

    release_callback.set()
    await asyncio.wait_for(reconciliation, timeout=1)
    await asyncio.wait_for(success, timeout=1)
    await _run_due_and_wait(coordinator)

    assert events == ["fault", "recovered"]
    assert store.next_processing_action() is None


async def test_processing_action_loop_orders_recovery_probe_and_fault(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "ordered-cycle-1",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _classify_and_ack_store_processing_fault(store)
    _close_store_processing_fault(store, at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC))
    _open_store_processing_fault(
        store,
        "ordered-cycle-2",
        at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
    )
    events: list[tuple[str, str | None]] = []

    async def notify(event, kind, _occurred_at, _queued) -> bool:
        events.append((event, kind))
        return True

    provider = FakeMemoryProvider(processing_healthy_flag=True)
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="next-boot")
    await _run_processing_actions(coordinator)

    assert events == [("recovered", None), ("fault", "engine")]
    assert store.next_processing_action() is None
    meta = store.ensure_meta()
    assert meta.processing_fault_generation == 2
    assert meta.processing_alert_active is True


async def test_notification_queue_count_is_live_and_not_durable_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "live-queued-1",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    probe = store.next_processing_action()
    assert isinstance(probe, ProcessingHealthProbe)
    assert store.record_processing_health(probe, healthy=True).committed
    durable_event = store.next_processing_action()
    queued_values: list[int] = []

    async def notify(_event, _kind, _occurred_at, queued) -> bool:
        queued_values.append(queued)
        return len(queued_values) > 1

    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        now=lambda: current[0],
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="first-boot")
    await _run_processing_actions(coordinator)
    _enqueue(store, "live-queued-2", session="live-queued-2")
    assert store.next_processing_action() == durable_event
    await coordinator.recover(lease_owner="second-boot")
    await _run_processing_actions(coordinator)
    assert queued_values == [1]

    current[0] += timedelta(seconds=5)
    assert await _run_due_and_wait(coordinator) == 0

    assert queued_values == [1, 2]
    assert store.next_processing_action() is None


async def test_stale_processing_commit_stops_without_hot_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        "stale-commit",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    commits = 0

    def stale_commit(_probe: ProcessingHealthProbe, *, healthy: bool) -> ProcessingHealthCommit:
        nonlocal commits
        del healthy
        commits += 1
        return ProcessingHealthCommit(committed=False)

    monkeypatch.setattr(store, "record_processing_health", stale_commit)
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(processing_healthy_flag=True),
        enabled=lambda: True,
    )
    await coordinator.recover(lease_owner="next-boot")
    await _run_processing_actions(coordinator)

    assert commits == 1


@pytest.mark.parametrize("operation", ["health", "ack"])
async def test_processing_action_cancellation_drains_started_store_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = _store(tmp_path)
    _open_store_processing_fault(
        store,
        f"cancel-{operation}",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    entered = threading.Event()
    release = threading.Event()
    events: list[str] = []

    if operation == "health":
        original = store.record_processing_health

        def blocking_commit(probe: ProcessingHealthProbe, *, healthy: bool):
            entered.set()
            release.wait(timeout=2)
            return original(probe, healthy=healthy)

        monkeypatch.setattr(store, "record_processing_health", blocking_commit)
    else:
        probe = store.next_processing_action()
        assert isinstance(probe, ProcessingHealthProbe)
        assert store.record_processing_health(probe, healthy=True).committed
        original = store.acknowledge_processing_notification

        def blocking_ack(notification: ProcessingNotification) -> bool:
            entered.set()
            release.wait(timeout=2)
            return original(notification)

        monkeypatch.setattr(store, "acknowledge_processing_notification", blocking_ack)

    async def notify(event, _kind, _occurred_at, _queued) -> bool:
        events.append(event)
        return True

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(processing_healthy_flag=True),
        enabled=lambda: True,
        processing_event=notify,
    )
    await coordinator.recover(lease_owner="next-boot")
    assert await coordinator.run_due() == 0
    recovery = coordinator._processing_task
    assert recovery is not None
    assert await asyncio.to_thread(entered.wait, 1)
    recovery.cancel()
    await asyncio.sleep(0)
    assert not recovery.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await recovery

    action = store.next_processing_action()
    if operation == "health":
        assert isinstance(action, ProcessingNotification)
        assert events == []
    else:
        assert action is None
        assert events == ["fault"]



async def test_final_flush_upgrades_joined_due_flush_after_due_at_shifts(
    tmp_path: Path,
) -> None:
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
    await _wait_for_processing_actions(worker.coordinator)

    current[0] += timedelta(minutes=5)
    session_lock = worker.coordinator._session_lock(
        first.provider_session_ref.serialize()
    )
    await session_lock.acquire()
    assert await _run_due_and_wait(worker.coordinator) == 1
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



async def test_final_flush_repeats_for_capture_enqueued_during_forced_pass(
    tmp_path: Path,
) -> None:
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



async def test_extracted_add_is_a_natural_boundary_without_flush(tmp_path: Path) -> None:
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



async def test_system_outage_backs_off_add_claims_between_drain_ticks(tmp_path: Path) -> None:
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



async def test_system_outage_backs_off_fenced_generation_adds(tmp_path: Path) -> None:
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



async def test_processing_fault_emits_one_fault_and_recovery_edge(tmp_path: Path) -> None:
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
    await _wait_for_processing_actions(worker.coordinator)
    opened = store.get_meta()
    assert opened is not None
    assert opened.processing_fault_kind == "credential"
    assert opened.processing_alert_active is True
    assert [event[:2] for event in events] == [("fault", "credential")]
    assert events[0][3] == 1

    provider.processing_healthy_flag = True
    current[0] += timedelta(seconds=5)
    assert await worker.drain_once() == 1
    await _wait_for_processing_actions(worker.coordinator)
    closed = store.get_meta()
    assert closed is not None
    assert closed.processing_fault_since is None
    assert [event[:2] for event in events] == [
        ("fault", "credential"),
        ("recovered", None),
    ]
    assert events[1][3] == 0



@pytest.mark.parametrize("failure", ["timeout", "disconnect", "malformed_2xx"])
async def test_ambiguous_add_opens_one_durable_fault_without_replay(
    tmp_path: Path,
    failure: str,
) -> None:
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
    await _wait_for_processing_actions(worker.coordinator)
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



async def test_restart_finishes_ambiguous_add_fault_after_classification_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    async def interrupt_classification() -> bool:
        raise asyncio.CancelledError

    original_health = provider.processing_healthy
    monkeypatch.setattr(provider, "processing_healthy", interrupt_classification)
    assert not await coordinator.deliver(claimed, lease_owner="old-boot")
    with pytest.raises(asyncio.CancelledError):
        await _run_due_and_wait(coordinator)

    queued = store.list_queue_rows()[0]
    assert queued.state == "manual_required"
    pending_fault = store.ensure_meta()
    assert pending_fault.processing_fault_since is not None
    assert pending_fault.processing_fault_kind is None
    assert pending_fault.processing_alert_active is False
    assert len(provider.captures) == 1
    monkeypatch.setattr(provider, "processing_healthy", original_health)

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
    await _run_processing_actions(restarted)
    await restarted.recover(lease_owner="same-boot")
    await _run_processing_actions(restarted)

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



async def test_confirmed_client_rejection_does_not_open_processing_fault(
    tmp_path: Path,
) -> None:
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
    await _wait_for_processing_actions(worker.coordinator)
    rejected = store.list_queue_rows()[0]
    assert (rejected.state, rejected.add_request_id) == (
        "dead",
        "client-rejection",
    )
    assert store.ensure_meta().processing_fault_since is None



async def test_server_rejected_add_is_terminal_but_opens_processing_fault(tmp_path: Path) -> None:
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
    await _wait_for_processing_actions(worker.coordinator)
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
    assert (
        failures[0].operation,
        failures[0].state,
        failures[0].request_id,
        failures[0].attempts,
    ) == (
        "add",
        "rejected",
        "server-rejection",
        1,
    )

    provider.processing_healthy_flag = True
    _enqueue(store, "recovery")
    assert await worker.drain_once() == 1
    await _wait_for_processing_actions(worker.coordinator)
    closed = store.get_meta()
    assert closed is not None
    assert closed.processing_fault_since is None
    assert [event[:2] for event in events] == [
        ("fault", "credential"),
        ("recovered", None),
    ]



async def test_cancelled_server_rejection_commit_is_completed_once_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    await _run_processing_actions(restarted)
    await restarted.recover(lease_owner="same-boot")
    await _run_processing_actions(restarted)
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
    await _wait_for_processing_actions(restarted)
    assert len(provider.captures) == 2
    assert reopened_store.ensure_meta().processing_fault_since is None
    assert [event[:2] for event in events] == [
        ("fault", "credential"),
        ("recovered", None),
    ]



async def test_fence_routes_new_capture_to_next_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    flush = _Gate()
    provider = FakeMemoryProvider(flush_hook=flush)
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
    assert await _run_due_and_wait(worker.coordinator) == 1
    await asyncio.wait_for(flush.entered.wait(), timeout=1)

    second = _enqueue(store, "second")
    assert second.generation == first.generation + 1
    assert store.claim_due(lease_owner="raced", now="2026-01-01T00:05:01.000Z") is None

    flush.open()
    await _wait_for_scheduled_flush(worker.coordinator, first.provider_session_ref)
    claimed = store.claim_due(lease_owner="next", now="2026-01-01T00:05:01.000Z")
    assert claimed is not None and claimed.source_message_digest == second.source_message_digest



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
    lease = store.begin_flush_attempt(
        now="2026-01-01T00:00:00.000Z",
        provider_session_ref=row.provider_session_ref,
    )
    assert lease is not None
    assert store.begin_flush_submission(lease, now="2026-01-01T00:00:01.000Z")
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


async def test_stale_flush_finalization_does_not_open_processing_fault(
    tmp_path: Path,
) -> None:
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
    lease = store.begin_flush_attempt(
        now="2026-01-01T00:00:00.000Z",
        provider_session_ref=row.provider_session_ref,
    )
    assert lease is not None
    assert store.begin_flush_submission(
        lease,
        now="2026-01-01T00:00:01.000Z",
    )
    assert store.settle_flush(
        lease,
        FlushSucceeded("first", "extracted"),
        now="2026-01-01T00:00:02.000Z",
    ).settled

    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
    )
    await coordinator._finalize_flush_outcome(
        lease,
        FlushUnknown(reason="transport"),
    )

    meta = store.get_meta()
    assert meta is not None
    assert meta.processing_fault_generation == 0
    assert meta.processing_fault_since is None



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
    lease = store.begin_flush_attempt(
        now="2026-01-01T00:00:00.000Z",
        provider_session_ref=row.provider_session_ref,
    )
    assert lease is not None
    assert store.begin_flush_submission(lease, now="2026-01-01T00:00:01.000Z")

    recovered = MemoryStore(store.path)
    evidence = recovered.recover_after_boot(
        lease_owner="new-boot",
        clock=lambda: datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
    )

    assert evidence.interrupted_flushes == 1
    state = recovered.get_session_flush_state(row.provider_session_ref)
    assert state is not None and state.state == "manual_required"
    assert recovered.begin_flush_attempt(
        now="2026-01-01T00:10:00.000Z",
        provider_session_ref=row.provider_session_ref,
        force=True,
    ) is None


@pytest.mark.parametrize("operation", ["add", "flush"])
async def test_boot_recovery_opens_and_emits_processing_fault_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
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
        lease = store.begin_flush_attempt(
            now="2026-01-01T00:00:01.000Z",
            provider_session_ref=row.provider_session_ref,
        )
        assert lease is not None
        assert store.begin_flush_submission(
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
    await _run_processing_actions(coordinator)
    await coordinator.recover(lease_owner="same-boot")
    await _run_processing_actions(coordinator)

    meta = store.get_meta()
    assert meta is not None
    assert meta.processing_fault_since == recovered_fault_since
    assert meta.processing_fault_kind == "engine"
    assert meta.processing_alert_active is True
    assert [(event, kind) for event, kind, _at, _queued in events] == [
        ("fault", "engine")
    ]



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
async def test_restart_finishes_submitted_flush_fault_notification_once(
    tmp_path: Path,
    result: FlushUnknown | FlushRejected | FlushSucceeded,
) -> None:
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
    lease = store.begin_flush_attempt(
        now="2026-01-01T00:00:01.000Z",
        provider_session_ref=row.provider_session_ref,
        force=True,
    )
    assert lease is not None
    assert store.begin_flush_submission(
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
    await _run_processing_actions(restarted)
    await restarted.recover(lease_owner="same-boot")
    await _run_processing_actions(restarted)

    assert [event[:3] for event in events] == [
        ("fault", "engine", "2026-01-01T00:00:03.000Z")
    ]
    notified = store.ensure_meta()
    assert notified.processing_fault_kind == "engine"
    assert notified.processing_alert_active is True



@pytest.mark.parametrize(
    "success",
    ("add-accumulated", "add-extracted", "flush-succeeded"),
)
async def test_restart_finishes_atomic_processing_recovery_notification_once(
    tmp_path: Path,
    success: str,
) -> None:
    store = _store(tmp_path)
    provider = FakeMemoryProvider(processing_healthy_flag=True)

    row = _enqueue(store, f"recovery-{success}")
    claimed = store.claim_due(
        lease_owner="old-boot",
        now="2026-01-01T00:00:01.000Z",
    )
    assert claimed is not None
    if success.startswith("add-"):
        _open_store_processing_fault(
            store,
            f"recovery-fault-{success}",
            at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        _classify_and_ack_store_processing_fault(store)
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
        _open_store_processing_fault(
            store,
            "recovery-fault-flush",
            at=datetime(2026, 1, 1, 0, 0, 2, 500000, tzinfo=UTC),
        )
        _classify_and_ack_store_processing_fault(store)
        lease = store.begin_flush_attempt(
            now="2026-01-01T00:00:03.000Z",
            provider_session_ref=row.provider_session_ref,
            force=True,
        )
        assert lease is not None
        assert store.begin_flush_submission(
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
    await _run_processing_actions(restarted)
    await restarted.recover(lease_owner="same-boot")
    await _run_processing_actions(restarted)

    assert [event[:3] for event in events] == [
        ("recovered", None, expected_at)
    ]
    assert store.ensure_meta().processing_recovery_pending_at is None



async def test_proven_pre_submission_flush_failure_uses_bounded_retry(tmp_path: Path) -> None:
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
        assert await _run_due_and_wait(coordinator) == 1
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

    assert await _run_due_and_wait(coordinator) == 1
    await _wait_for_scheduled_flush(coordinator, row.provider_session_ref)
    await _run_due_and_wait(coordinator)
    terminal = store.get_session_flush_state(row.provider_session_ref)
    assert terminal is not None
    assert terminal.state == "manual_required"
    assert terminal.retry_count == 4
    assert terminal.next_attempt_at is None
    assert len(provider.flushes) == 4
    fault = store.ensure_meta()
    assert fault.processing_fault_kind == "engine"
    assert fault.processing_alert_active is True



async def test_cancelled_exhausted_flush_retry_is_completed_once_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        assert await _run_due_and_wait(coordinator) == 1
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
    assert await _run_due_and_wait(coordinator) == 1
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
    await _run_processing_actions(restarted)
    await restarted.recover(lease_owner="same-boot")
    await _run_processing_actions(restarted)
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
    await _wait_for_processing_actions(restarted)
    assert reopened_store.ensure_meta().processing_fault_since is None
    assert [event[:2] for event in events] == [
        ("fault", "engine"),
        ("recovered", None),
    ]



async def test_continuous_activity_cannot_extend_flush_past_max_age(tmp_path: Path) -> None:
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
    assert await _run_due_and_wait(coordinator) == 0
    current[0] = start + timedelta(minutes=30)
    assert await _run_due_and_wait(coordinator) == 1
    await _wait_for_scheduled_flush(coordinator, session_ref)

    assert provider.flushes == [session_ref]
    state = store.get_session_flush_state(session_ref)
    assert state is not None and state.state == "idle"



async def test_message_bound_makes_generation_immediately_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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



async def test_same_session_serializes_while_another_session_continues(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_add = _Gate()

    async def block_first(capture) -> None:
        if capture.text == "payload-same-first":
            await first_add(capture)

    provider = FakeMemoryProvider(add_hook=block_first)
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
    await asyncio.wait_for(first_add.entered.wait(), timeout=1)
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

    first_add.open()
    assert await asyncio.wait_for(first, timeout=1)
    assert await asyncio.wait_for(same_second, timeout=1)
    assert [capture.text for capture in provider.captures] == [
        "payload-same-first",
        "payload-other",
        "payload-same-second",
    ]



async def test_shutdown_does_not_initiate_a_provider_flush(tmp_path: Path) -> None:
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
    assert await _run_due_and_wait(worker.coordinator) == 0
    assert not await worker.coordinator.final_flush(row.provider_session_ref)

    assert len(provider.captures) == 1
    assert provider.flushes == []
    state = store.get_session_flush_state(row.provider_session_ref)
    assert state is not None and state.state == "idle"



async def test_cancelled_flush_waiting_for_write_slot_remains_retryable(tmp_path: Path) -> None:
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



async def test_cancelled_flush_while_submission_marker_commits_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    original_mark = store.begin_flush_submission

    def blocking_mark(lease, *, now: str) -> bool:
        marked = original_mark(lease, now=now)
        marker_committed.set()
        release_marker.wait(timeout=2)
        return marked

    monkeypatch.setattr(store, "begin_flush_submission", blocking_mark)
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
    await _run_due_and_wait(coordinator)
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



async def test_cancelled_flush_before_submission_coroutine_entry_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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



async def test_cancelled_flush_after_provider_entry_opens_one_fault_and_later_recovers(
    tmp_path: Path,
) -> None:
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
    await _run_due_and_wait(coordinator)

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
    await _run_processing_actions(recovered)
    assert [event[:2] for event in events] == [("fault", "engine")]

    assert await recovered.final_flush(
        recovery.provider_session_ref,
        deadline_seconds=1,
    )
    await _run_due_and_wait(recovered)
    closed = store.get_meta()
    assert closed is not None and closed.processing_fault_since is None
    assert [event[:2] for event in events] == [
        ("fault", "engine"),
        ("recovered", None),
    ]



async def test_shutdown_joins_post_entry_flush_classification_and_boot_recovers(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    flush_entered = asyncio.Event()
    events: list[tuple[str, str | None, str, int]] = []
    health_calls = 0

    class Provider(FakeMemoryProvider):
        async def flush(self, session_ref):
            self.flushes.append(session_ref)
            flush_entered.set()
            await asyncio.Event().wait()

    async def record_health_probe() -> None:
        nonlocal health_calls
        health_calls += 1

    async def record_event(
        event: str,
        kind: str | None,
        occurred_at: str,
        queued: int,
    ) -> bool:
        events.append((event, kind, occurred_at, queued))
        return True

    provider = Provider(processing_healthy_hook=record_health_probe)
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
    assert await _run_due_and_wait(coordinator) == 0
    flush_task = coordinator._schedule(row.provider_session_ref, force=True)
    assert flush_task is not None
    await asyncio.wait_for(flush_entered.wait(), timeout=1)

    await coordinator.prepare_shutdown(timeout_seconds=1)
    assert flush_task.cancelled()
    state = store.get_session_flush_state(row.provider_session_ref)
    assert state is not None and state.state == "manual_required"
    pending_fault = store.get_meta()
    assert pending_fault is not None
    assert pending_fault.processing_fault_since is not None
    assert pending_fault.processing_fault_kind is None
    assert pending_fault.processing_alert_active is False
    assert events == []
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
    assert health_calls == 0
    await _run_due_and_wait(recovered)
    assert [event[:2] for event in events] == [("fault", "engine")]
    recovered_meta = store.get_meta()
    assert recovered_meta is not None
    assert recovered_meta.processing_fault_kind == "engine"
    assert recovered_meta.processing_alert_active is True



async def test_shutdown_drains_local_flush_commit_then_boot_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    async def record_health_probe() -> None:
        nonlocal health_calls
        health_calls += 1

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
    provider = Provider(processing_healthy_hook=record_health_probe)
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
    await _run_processing_actions(recovered)
    assert health_calls == 1
    assert [event[:2] for event in events] == [("fault", "engine")]



async def test_shutdown_local_flush_phase_does_not_wait_for_classification_lock(
    tmp_path: Path,
) -> None:
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

    async def block_first_health_probe() -> None:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            old_health_entered.set()
            await release_old_health.wait()

    async def record_event(
        event: str,
        kind: str | None,
        occurred_at: str,
        queued: int,
    ) -> bool:
        events.append((event, kind, occurred_at, queued))
        return True

    provider = Provider(processing_healthy_hook=block_first_health_probe)
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

    _open_store_processing_fault(
        store,
        "shutdown-lock-fault",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    old_classification = asyncio.create_task(coordinator._reconcile_processing_events())
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
    await _run_processing_actions(recovered)
    await recovered.recover(lease_owner="same-boot")
    await _run_processing_actions(recovered)
    assert health_calls == 2
    assert [event[:2] for event in events] == [("fault", "engine")]



async def test_cancelled_add_waiting_for_write_slot_returns_exact_claim(tmp_path: Path) -> None:
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



@pytest.mark.parametrize("fenced", [False, True], ids=["ordinary", "fenced"])
async def test_claim_revalidation_error_returns_exact_claim_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fenced: bool,
) -> None:
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



async def test_cancelled_add_before_submission_coroutine_entry_returns_exact_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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



async def test_cancelled_add_after_provider_entry_remains_ambiguous(tmp_path: Path) -> None:
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



def test_claimed_attachment_downgrade_is_atomic_and_lease_fenced(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _attachment_store, bundle, _pinned_path = _pin_attachment_bundle()
    original = _enqueue_attachment_bundle(
        store,
        bundle,
        source="atomic-text-downgrade",
    )
    stale = store.claim_due(
        lease_owner="first-worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert stale is not None

    assert (
        store._downgrade_claimed_attachment_to_text(
            stale,
            lease_owner="wrong-worker",
            now=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        )
        is None
    )
    assert store.return_unsubmitted_claim(stale, lease_owner="first-worker")
    current = store.claim_due(
        lease_owner="second-worker",
        now="2026-01-01T00:00:02.000Z",
    )
    assert current is not None
    assert current.lease_token == stale.lease_token + 1
    assert (
        store._downgrade_claimed_attachment_to_text(
            stale,
            lease_owner="second-worker",
            now=datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
        )
        is None
    )

    assert (
        store._downgrade_claimed_attachment_to_text(
            current,
            lease_owner="second-worker",
            now=datetime(2026, 1, 1, 0, 0, 4, tzinfo=UTC),
        )
        == bundle.bundle_id
    )
    downgraded = store.get_queue_row(original.source_message_digest)
    assert downgraded is not None
    assert (
        downgraded.source_message_digest,
        downgraded.created_at,
        downgraded.payload_text,
        downgraded.state,
        downgraded.attempts,
        downgraded.payload_attachments,
        downgraded.attachment_bundle_id,
        downgraded.lease_owner,
        downgraded.lease_at,
    ) == (
        original.source_message_digest,
        original.created_at,
        "remember the attachment",
        "pending",
        0,
        None,
        None,
        None,
        None,
    )
    assert store.attachment_bundle_sets() == (
        frozenset(),
        frozenset({bundle.bundle_id}),
    )
    assert (
        store._downgrade_claimed_attachment_to_text(
            current,
            lease_owner="second-worker",
            now=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
        )
        is None
    )


def test_claimed_attachment_downgrade_rolls_back_caption_and_bundle_together(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _attachment_store, bundle, _pinned_path = _pin_attachment_bundle()
    _enqueue_attachment_bundle(store, bundle, source="rollback-text-downgrade")
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_attachment_release
            BEFORE UPDATE OF state ON memory_attachment_bundle
            WHEN NEW.state = 'releasing'
            BEGIN
                SELECT RAISE(ABORT, 'forced release failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced release failure"):
        store._downgrade_claimed_attachment_to_text(
            claimed,
            lease_owner="worker",
            now=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        )

    preserved = store.get_queue_row(claimed.source_message_digest)
    assert preserved is not None
    assert (
        preserved.state,
        preserved.payload_text,
        preserved.payload_attachments,
        preserved.attachment_bundle_id,
        preserved.lease_owner,
        preserved.lease_token,
        preserved.attempts,
    ) == (
        "processing",
        claimed.payload_text,
        claimed.payload_attachments,
        bundle.bundle_id,
        "worker",
        claimed.lease_token,
        0,
    )
    assert store.attachment_bundle_sets() == (
        frozenset({bundle.bundle_id}),
        frozenset(),
    )


@pytest.mark.parametrize("failure", ["decode", "missing", "tamper"])
async def test_attachment_preflight_failure_retries_caption_as_text_only(
    tmp_path: Path,
    failure: str,
) -> None:
    store = _store(tmp_path)
    attachment_store, bundle, pinned_path = _pin_attachment_bundle()
    original = _enqueue_attachment_bundle(
        store,
        bundle,
        source=f"{failure}-text-downgrade",
    )
    if failure == "decode":
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                """
                UPDATE memory_capture_queue
                SET payload_attachments = '{}'
                WHERE source_message_digest = ?
                """,
                (original.source_message_digest,),
            )
    elif failure == "missing":
        pinned_path.unlink()
    else:
        pinned_path.write_bytes(b"tampered after enqueue")

    provider = FakeMemoryProvider()
    releases: list[str] = []
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        attachment_store=attachment_store,
        release_attachment=releases.append,
    )
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None

    assert not await coordinator.deliver(claimed, lease_owner="worker")

    downgraded = store.get_queue_row(original.source_message_digest)
    assert downgraded is not None
    assert (
        downgraded.source_message_digest,
        downgraded.created_at,
        downgraded.payload_text,
        downgraded.state,
        downgraded.attempts,
        downgraded.payload_attachments,
        downgraded.attachment_bundle_id,
    ) == (
        original.source_message_digest,
        original.created_at,
        "remember the attachment",
        "pending",
        0,
        None,
        None,
    )
    assert releases == [bundle.bundle_id]
    assert store.attachment_bundle_sets() == (frozenset(), frozenset())
    assert not pinned_path.exists()
    assert provider.captures == []

    retry = store.claim_due(
        lease_owner="retry-worker",
        now="2026-01-01T00:00:01.000Z",
    )
    assert retry is not None
    assert retry.source_message_digest == original.source_message_digest
    assert await coordinator.deliver(retry, lease_owner="retry-worker")
    assert len(provider.captures) == 1
    assert provider.captures[0].text == "remember the attachment"
    assert provider.captures[0].attachments == ()
    assert releases == [bundle.bundle_id]


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [("decode", "dead"), ("revalidation", "pending")],
)
async def test_captionless_attachment_preflight_failure_always_makes_progress(
    tmp_path: Path,
    failure: str,
    expected_state: str,
) -> None:
    store = _store(tmp_path)
    attachment_store, bundle, pinned_path = _pin_attachment_bundle()
    original = _enqueue_attachment_bundle(
        store,
        bundle,
        source=f"captionless-{failure}-failure",
        payload_text="",
    )
    if failure == "decode":
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                """
                UPDATE memory_capture_queue
                SET payload_attachments = '{}'
                WHERE source_message_digest = ?
                """,
                (original.source_message_digest,),
            )
    else:
        pinned_path.write_bytes(b"tampered after enqueue")
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=FakeMemoryProvider(),
        enabled=lambda: True,
        attachment_store=attachment_store,
    )
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None

    assert not await coordinator.deliver(claimed, lease_owner="worker")

    terminal = store.get_queue_row(original.source_message_digest)
    assert terminal is not None
    assert (terminal.state, terminal.attempts) == (expected_state, 1)
    if failure == "decode":
        assert (
            terminal.payload_text,
            terminal.payload_attachments,
            terminal.attachment_bundle_id,
        ) == (None, None, None)
        assert store.attachment_bundle_sets() == (frozenset(), frozenset())
    else:
        assert terminal.payload_text == ""
        assert terminal.payload_attachments == original.payload_attachments
        assert terminal.attachment_bundle_id == bundle.bundle_id
        assert store.attachment_bundle_sets() == (
            frozenset({bundle.bundle_id}),
            frozenset(),
        )


@pytest.mark.parametrize("failure_source", ["file-open", "directory-order"])
async def test_transient_attachment_projection_failure_retries_original_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_source: str,
) -> None:
    store = _store(tmp_path)
    attachment_store, bundle, pinned_path = _pin_attachment_bundle()
    original = _enqueue_attachment_bundle(
        store,
        bundle,
        source="transient-projection-failure",
    )
    provider = FakeMemoryProvider()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        now=lambda: current[0],
        attachment_store=attachment_store,
    )
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    failed = False
    if failure_source == "file-open":
        original_operation = attachment_module.os.open

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError(errno.EMFILE, "temporary descriptor exhaustion")
            return original_operation(*args, **kwargs)

        monkeypatch.setattr(attachment_module.os, "open", fail_once)
    else:
        original_operation = confined_filesystem_module.sqlite3.connect

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed and args and args[0] == "":
                failed = True
                raise sqlite3.OperationalError("temporary ordering unavailable")
            return original_operation(*args, **kwargs)

        monkeypatch.setattr(confined_filesystem_module.sqlite3, "connect", fail_once)
    assert not await coordinator.deliver(claimed, lease_owner="worker")

    retryable = store.get_queue_row(original.source_message_digest)
    assert retryable is not None
    assert (
        retryable.state,
        retryable.attempts,
        retryable.payload_text,
        retryable.payload_attachments,
        retryable.attachment_bundle_id,
    ) == (
        "pending",
        1,
        "remember the attachment",
        original.payload_attachments,
        bundle.bundle_id,
    )
    assert store.attachment_bundle_sets() == (
        frozenset({bundle.bundle_id}),
        frozenset(),
    )
    assert pinned_path.exists()
    assert provider.captures == []

    if failure_source == "file-open":
        monkeypatch.setattr(attachment_module.os, "open", original_operation)
    else:
        monkeypatch.setattr(
            confined_filesystem_module.sqlite3,
            "connect",
            original_operation,
        )
    current[0] += timedelta(seconds=30)
    retry = store.claim_due(
        lease_owner="retry-worker",
        now="2026-01-01T00:00:30.000Z",
    )
    assert retry is not None
    assert await coordinator.deliver(retry, lease_owner="retry-worker")
    assert len(provider.captures) == 1
    assert provider.captures[0].attachments


async def test_cancel_after_downgrade_leaves_releasing_bundle_for_boot_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    attachment_store, bundle, pinned_path = _pin_attachment_bundle()
    original = _enqueue_attachment_bundle(
        store,
        bundle,
        source="cancelled-text-downgrade",
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """
            UPDATE memory_capture_queue
            SET payload_attachments = '{}'
            WHERE source_message_digest = ?
            """,
            (original.source_message_digest,),
        )

    release_entered = threading.Event()
    release_continue = threading.Event()
    release_finished = threading.Event()
    original_release = attachment_store.release

    def blocked_release(bundle_id: str) -> None:
        release_entered.set()
        release_continue.wait(timeout=2)
        original_release(bundle_id)
        release_finished.set()

    monkeypatch.setattr(attachment_store, "release", blocked_release)
    provider = FakeMemoryProvider()
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        attachment_store=attachment_store,
    )
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    delivery = asyncio.create_task(coordinator.deliver(claimed, lease_owner="worker"))
    assert await asyncio.to_thread(release_entered.wait, 1)

    committed = store.get_queue_row(original.source_message_digest)
    assert committed is not None
    assert (
        committed.state,
        committed.payload_text,
        committed.payload_attachments,
        committed.attachment_bundle_id,
        committed.attempts,
    ) == ("pending", "remember the attachment", None, None, 0)
    assert store.attachment_bundle_sets() == (
        frozenset(),
        frozenset({bundle.bundle_id}),
    )

    delivery.cancel()
    release_continue.set()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    assert await asyncio.to_thread(release_finished.wait, 1)
    assert provider.captures == []
    assert store.attachment_bundle_sets() == (
        frozenset(),
        frozenset({bundle.bundle_id}),
    )

    monkeypatch.setattr(attachment_store, "release", original_release)
    restarted = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        attachment_store=attachment_store,
    )
    await restarted.recover(lease_owner="next-boot")
    assert store.attachment_bundle_sets() == (frozenset(), frozenset())
    assert not pinned_path.exists()


async def test_attachment_preflight_downgrade_survives_deferred_bundle_release(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    attachment_store, bundle, pinned_path = _pin_attachment_bundle()
    row = _enqueue_attachment_bundle(
        store,
        bundle,
        source="broken-attachment",
    )
    pinned_path.chmod(0o644)

    provider = FakeMemoryProvider()
    coordinator = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        attachment_store=attachment_store,
    )
    await coordinator.recover(lease_owner="initial-boot")
    await _run_processing_actions(coordinator)

    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    assert not await coordinator.deliver(claimed, lease_owner="worker")
    queued = store.list_queue_rows()[0]
    assert (
        queued.state,
        queued.attempts,
        queued.payload_text,
        queued.payload_attachments,
        queued.attachment_bundle_id,
    ) == ("pending", 0, "remember the attachment", None, None)
    session_state = store.get_session_flush_state(row.provider_session_ref)
    assert session_state is not None and session_state.state == "idle"

    assert store.attachment_bundle_sets() == (
        frozenset(),
        frozenset({bundle.bundle_id}),
    )
    retry = store.claim_due(
        lease_owner="retry-worker",
        now="2026-01-01T00:00:01.000Z",
    )
    assert retry is not None
    assert await coordinator.deliver(retry, lease_owner="retry-worker")
    assert len(provider.captures) == 1
    assert provider.captures[0].text == "remember the attachment"
    assert provider.captures[0].attachments == ()

    restarted = SessionFlushCoordinator(
        store=store,
        provider=provider,
        enabled=lambda: True,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        attachment_store=attachment_store,
    )
    await restarted.recover(lease_owner="restarted-worker")
    await _run_processing_actions(restarted)
    assert store.attachment_bundle_sets() == (
        frozenset(),
        frozenset({bundle.bundle_id}),
    )
    pinned_path.chmod(0o600)
    await restarted.recover(lease_owner="repaired-worker")
    assert store.attachment_bundle_sets() == (frozenset(), frozenset())
    assert not pinned_path.exists()

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
            now="2026-01-01T00:00:02.000Z",
        )
        assert claimed is not None
        assert await restarted.deliver(claimed, lease_owner="later-worker")
    assert [capture.text for capture in provider.captures] == [
        "remember the attachment",
        later.payload_text,
        unrelated.payload_text,
    ]



async def test_successful_boot_attachment_reconcile_finalizes_release(tmp_path: Path) -> None:
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
    await _run_processing_actions(coordinator)

    assert store.attachment_bundle_sets() == (frozenset(), frozenset())
    assert not pinned_path.exists()



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
async def test_attachment_preflight_classifies_local_failures_without_ambiguity(
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



async def test_stale_add_waiter_does_not_resubmit_after_flush_reclaims_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_add = _Gate()
    provider = FakeMemoryProvider(add_hook=first_add)
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
    await asyncio.wait_for(first_add.entered.wait(), timeout=1)
    stale_call = asyncio.create_task(
        coordinator.deliver(stale_claim, lease_owner="ordinary-worker")
    )
    await asyncio.sleep(0)
    assert stale_call.done() is False

    first_add.open()
    assert await flush_call
    assert await stale_call is False
    assert [capture.text for capture in provider.captures] == [
        "payload-stale-waiter"
    ]
    assert provider.flushes == [row.provider_session_ref]



async def test_cancelled_add_waiting_for_session_lock_returns_exact_claim(tmp_path: Path) -> None:
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



async def test_cancelled_routine_claim_acquisition_returns_exact_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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



async def test_cancelled_fenced_claim_acquisition_returns_exact_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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



async def test_session_lock_is_shared_with_waiters_then_reclaimed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_add = _Gate()
    provider = FakeMemoryProvider(add_hook=first_add)
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
    await asyncio.wait_for(first_add.entered.wait(), timeout=1)
    waiter = asyncio.create_task(coordinator.deliver(claimed[1], lease_owner="worker"))
    await asyncio.sleep(0)
    assert waiter.done() is False
    assert len(coordinator._session_locks) == 1
    shared_lock = coordinator._session_locks[key]

    first_add.open()
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



@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("malformed", "memory_provider_response_invalid"),
        ("timeout", "memory_provider_timeout"),
    ],
)
async def test_submitted_malformed_or_timeout_flush_is_terminal(
    tmp_path: Path,
    failure: str,
    expected_error: str,
) -> None:
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
