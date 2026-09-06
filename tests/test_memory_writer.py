"""Focused contracts for the process-local best-effort Memory writer."""

import asyncio
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from avibe_memory.everos import (
    AddAck,
    AddRejected,
    FakeMemoryProvider,
    FlushRetryable,
    FlushSucceeded,
    MemoryProviderFailure,
)
from avibe_memory.attachments import AttachmentBundleInvalidError
from avibe_memory.store import MemoryStore, VolatileAdmission
from avibe_memory.types import CaptureAttachment, ProviderSessionRef
from avibe_memory.writer import (
    IDLE_FLUSH_SECONDS,
    MAX_ATTEMPTS,
    MAX_DUPLICATE_ENTRIES,
    MAX_PENDING_MESSAGE_IDS,
    MAX_PENDING_SESSIONS,
    MAX_UNFLUSHED_AGE_SECONDS,
    MAX_UNFLUSHED_MESSAGES,
    MAX_WRITER_PERMITS,
    BestEffortMemoryWriter,
    _BarrierItem,
    _PendingSession,
)


PRINCIPAL = "u-" + "1" * 32


def _writer(tmp_path: Path, provider: FakeMemoryProvider, **kwargs: object) -> BestEffortMemoryWriter:
    store = MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite", effective_home=tmp_path)
    enabled = kwargs.pop("enabled", lambda: True)
    return BestEffortMemoryWriter(
        store=store,
        provider=provider,
        enabled=enabled,
        **kwargs,
    )


def _ref(index: int = 0) -> ProviderSessionRef:
    return ProviderSessionRef(PRINCIPAL, 0, "default", f"session-{index}")


def _admission(index: int) -> VolatileAdmission:
    ref = _ref(index)
    return VolatileAdmission(
        "accepted",
        f"digest-{index}",
        ref,
        index + 1,
        f"raw-session-{index}",
    )


def _reserve_and_offer(writer: BestEffortMemoryWriter, index: int) -> None:
    reservation = writer.reserve(f"digest-{index}")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(index),
        text=f"message-{index}",
        attachments=(),
        bundle=None,
    ) == "queued"


@pytest.mark.asyncio
async def test_shared_permit_bound_covers_reservations_until_terminal_release(tmp_path: Path) -> None:
    writer = _writer(tmp_path, FakeMemoryProvider())
    writer._ensure_worker = lambda: None
    reservations = []
    for index in range(MAX_WRITER_PERMITS - 1):
        reservation = writer.reserve(f"digest-{index}")
        assert not isinstance(reservation, str)
        reservations.append(reservation)
    assert writer.offer_barrier("raw-session") == "queued"
    assert writer.reserve("digest-over-bound") == "full"
    assert writer.offer_barrier("raw-session") == "full"

    quiescing = asyncio.create_task(writer.quiesce(timeout_seconds=0.2))
    await asyncio.sleep(0)
    assert not quiescing.done()
    for reservation in reservations:
        reservation.release()
    assert await quiescing
    assert writer._permits == MAX_WRITER_PERMITS


def test_completed_duplicate_entries_are_evictable_behind_pending_claims(tmp_path: Path) -> None:
    writer = _writer(tmp_path, FakeMemoryProvider())
    pending = writer.reserve("digest-0")
    assert not isinstance(pending, str)
    completed = []
    for index in range(1, MAX_DUPLICATE_ENTRIES):
        reservation = writer.reserve(f"digest-{index}")
        assert not isinstance(reservation, str)
        completed.append(reservation)
    for reservation in completed:
        reservation.release()
    for index in range(MAX_DUPLICATE_ENTRIES, MAX_DUPLICATE_ENTRIES + 12):
        reservation = writer.reserve(f"digest-{index}")
        assert not isinstance(reservation, str)
        reservation.release()
    assert len(writer._duplicate_lru) <= MAX_DUPLICATE_ENTRIES
    assert "digest-0" in writer._duplicate_lru
    pending.release()


@pytest.mark.asyncio
async def test_full_queue_discards_increment_process_local_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = 1_000.0
    writer = _writer(tmp_path, FakeMemoryProvider(), monotonic=lambda: current)
    writer._ensure_worker = lambda: None
    writer._queue = asyncio.Queue(maxsize=1)
    writer._queue.put_nowait(_BarrierItem())

    reservation = writer.reserve("capture")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message",
        attachments=(),
        bundle=None,
    ) == "full"
    reservation.release()
    assert writer.offer_barrier("raw-session") == "full"

    ref = _ref()
    key = ref.serialize()
    writer._pending[key] = _PendingSession(
        ref,
        "raw-session",
        deque(["digest"]),
        current,
        current - IDLE_FLUSH_SECONDS,
    )

    sleeps = 0

    async def one_scheduler_tick(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("avibe_memory.writer.asyncio.sleep", one_scheduler_tick)
    await writer._schedule_due_flushes()

    assert writer.dropped_count() == 3
    assert writer._pending[key].retry_after == current + IDLE_FLUSH_SECONDS


@pytest.mark.asyncio
async def test_worker_delivers_captures_in_offer_order(tmp_path: Path) -> None:
    provider = FakeMemoryProvider()
    writer = _writer(tmp_path, provider)
    for index in range(3):
        _reserve_and_offer(writer, index)
    await writer.wait_idle_for_tests()
    assert [capture.text for capture in provider.captures] == [
        "message-0",
        "message-1",
        "message-2",
    ]


@pytest.mark.asyncio
async def test_revoked_runtime_drops_detached_provider_completion(
    tmp_path: Path,
) -> None:
    active = True
    provider = FakeMemoryProvider()
    provider_entered = asyncio.Event()
    provider_release = asyncio.Event()
    successes: list[str] = []

    async def delayed_add(capture):  # noqa: ANN001, ANN202
        provider_entered.set()
        await provider_release.wait()
        return AddAck(request_id="old-runtime", status="accumulated")

    provider.add = delayed_add
    writer = _writer(tmp_path, provider, enabled=lambda: active)
    writer._store.mark_capture_success = lambda: successes.append("published")
    _reserve_and_offer(writer, 0)
    await provider_entered.wait()

    active = False
    provider_release.set()
    await writer.wait_idle_for_tests()

    assert successes == []
    assert writer._pending == {}
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_extracted_add_clears_existing_volatile_session_state(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(
        add_results=deque([AddAck(request_id="add-extracted", status="extracted")])
    )
    writer = _writer(tmp_path, provider)
    ref = _ref()
    key = ref.serialize()
    writer._pending[key] = _PendingSession(
        ref,
        "raw-session-0",
        deque(["previous-digest"]),
        0.0,
        0.0,
    )

    _reserve_and_offer(writer, 0)
    await writer.wait_idle_for_tests()

    assert key not in writer._pending
    assert provider.flushes == []


@pytest.mark.asyncio
async def test_only_proven_pre_execution_failures_retry_three_times(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(
        ingest_failures=deque(
            [MemoryProviderFailure("memory_sidecar_unavailable") for _ in range(MAX_ATTEMPTS)]
        )
    )
    calls = 0
    original_add = provider.add

    async def counted_add(capture):
        nonlocal calls
        calls += 1
        return await original_add(capture)

    provider.add = counted_add
    writer = _writer(tmp_path, provider)
    _reserve_and_offer(writer, 0)
    await writer.wait_idle_for_tests()
    assert calls == MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("downgrade_attachment", [False, True])
async def test_sender_name_survives_provider_retry_and_attachment_downgrade(tmp_path, downgrade_attachment) -> None:
    provider = FakeMemoryProvider()
    attempts = []

    async def add(capture):
        attempts.append(capture)
        if len(attempts) == 1:
            if downgrade_attachment:
                return AddRejected(request_id=None, error_code="UNSUPPORTED_FORMAT", server_fault=False)
            raise MemoryProviderFailure("memory_sidecar_unavailable")
        return AddAck(request_id="synthetic", status="accumulated")

    provider.add = add
    writer = _writer(tmp_path, provider)
    reservation = writer.reserve("named-capture")
    assert not isinstance(reservation, str)
    attachments = (CaptureAttachment("pdf", "原件.pdf", "file:///synthetic.pdf", "pdf"),) if downgrade_attachment else ()
    assert writer.offer_capture(
        reservation, _admission(0), text="原文\nunchanged", attachments=attachments,
        bundle=None, sender_name="小王 🌱",
    ) == "queued"
    await writer.wait_idle_for_tests()
    assert len(attempts) == 2
    assert all(capture.sender_name == "小王 🌱" for capture in attempts)
    assert all(capture.text == "原文\nunchanged" for capture in attempts)
    assert all(capture.session_ref == _ref(0) for capture in attempts)
    assert all(capture.provider_timestamp_ms == 1 for capture in attempts)
    if downgrade_attachment:
        assert attempts[0].attachments == attachments
        assert attempts[1].attachments == ()
    await writer.close()


@pytest.mark.asyncio
async def test_ambiguous_failure_never_replays_and_disables_intake(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(
        ingest_failures=deque([MemoryProviderFailure("memory_provider_timeout", ambiguous=True)])
    )
    calls = 0
    stopped = 0
    original_add = provider.add

    async def counted_add(capture):
        nonlocal calls
        calls += 1
        return await original_add(capture)

    async def stop_reap(_recover: bool) -> bool:
        nonlocal stopped
        stopped += 1
        return True

    provider.add = counted_add
    class AttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def provider_attachments(self, _bundle):
            return ()

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    attachment_store = AttachmentStore()
    writer = _writer(
        tmp_path,
        provider,
        ambiguous_stop_reap=stop_reap,
        attachment_store=attachment_store,
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message-0",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"
    await writer.wait_idle_for_tests()
    assert calls == 1
    assert stopped == 1
    assert attachment_store.released == ["bundle-0"]
    assert writer._permits == MAX_WRITER_PERMITS
    assert writer.unavailable
    assert writer.reserve("later") == "unavailable"
    writer.replace_provider(FakeMemoryProvider())
    writer.resume_intake()
    replacement = writer.reserve("replacement-generation")
    assert not isinstance(replacement, str)
    replacement.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [None, "", "\ud800", "x" * 129])
async def test_invalid_add_receipt_is_ambiguous_and_never_replayed(
    tmp_path: Path,
    request_id: str | None,
) -> None:
    stopped = 0

    async def stop_reap(_recover: bool) -> bool:
        nonlocal stopped
        stopped += 1
        return True

    provider = FakeMemoryProvider(
        add_results=deque([AddAck(request_id=request_id, status="accumulated")])
    )
    writer = _writer(tmp_path, provider, ambiguous_stop_reap=stop_reap)
    _reserve_and_offer(writer, 0)

    await writer.wait_idle_for_tests()

    assert len(provider.captures) == 1
    assert stopped == 1
    assert writer.unavailable
    assert writer._pending == {}
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        FlushSucceeded(request_id=None, status="extracted"),
        FlushSucceeded(request_id="", status="extracted"),
        FlushSucceeded(request_id="\ud800", status="extracted"),
        FlushSucceeded(request_id="x" * 129, status="extracted"),
        FlushSucceeded(request_id="flush-request", status=None),
    ],
)
async def test_invalid_flush_receipt_is_ambiguous_and_never_replayed(
    tmp_path: Path,
    result: FlushSucceeded,
) -> None:
    stopped = 0

    async def stop_reap(_recover: bool) -> bool:
        nonlocal stopped
        stopped += 1
        return True

    provider = FakeMemoryProvider(flush_results=deque([result]))
    writer = _writer(tmp_path, provider, ambiguous_stop_reap=stop_reap)
    ref = _ref()
    writer._pending[ref.serialize()] = _PendingSession(
        ref, "raw-session-0", deque(["digest-0"]), 0.0, 0.0
    )

    await writer._flush_barrier(_BarrierItem(raw_session_id="raw-session-0"))

    assert provider.flushes == [ref]
    assert stopped == 1
    assert writer.unavailable
    assert writer._pending == {}


@pytest.mark.asyncio
async def test_ambiguous_failure_drops_already_queued_captures_when_reap_fails(
    tmp_path: Path,
) -> None:
    current = datetime(2026, 8, 23, 1, 2, 3, tzinfo=timezone.utc)
    processing_events: list[tuple[str, str | None, str, int]] = []

    async def processing_event(
        event: str,
        kind: str | None,
        occurred_at: str,
        queued: int,
    ) -> bool:
        processing_events.append((event, kind, occurred_at, queued))
        return True

    provider = FakeMemoryProvider(
        ingest_failures=deque(
            [MemoryProviderFailure("memory_provider_timeout", ambiguous=True)]
        )
    )
    calls = 0
    original_add = provider.add

    async def counted_add(capture):
        nonlocal calls
        calls += 1
        return await original_add(capture)

    provider.add = counted_add
    writer = _writer(
        tmp_path,
        provider,
        ambiguous_stop_reap=lambda _recover: False,
        now=lambda: current,
        processing_event=processing_event,
    )
    start_worker = writer._ensure_worker
    writer._ensure_worker = lambda: None
    _reserve_and_offer(writer, 0)
    _reserve_and_offer(writer, 1)
    writer._ensure_worker = start_worker
    writer._ensure_worker()

    await writer.wait_idle_for_tests()

    assert calls == 1
    assert provider.captures == []
    assert writer.unavailable
    assert writer._permits == MAX_WRITER_PERMITS
    assert processing_events == [
        ("fault", "engine", "2026-08-23T01:02:03.000Z", 2)
    ]


@pytest.mark.asyncio
async def test_quiesce_drops_queued_capture_instead_of_delivering_it(tmp_path: Path) -> None:
    provider = FakeMemoryProvider()
    writer = _writer(tmp_path, provider)
    writer._ensure_worker = lambda: None
    _reserve_and_offer(writer, 0)

    assert await writer.quiesce(timeout_seconds=1.0)

    assert provider.captures == []
    assert writer._queue.empty()
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_quiesce_handles_asyncio_timeout_on_python_310(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _writer(tmp_path, FakeMemoryProvider())

    async def timed_out_close() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(writer, "close", timed_out_close)

    assert not await writer.quiesce(timeout_seconds=0.001)
    assert writer.reserve("after-timeout") == "disabled"


@pytest.mark.asyncio
async def test_quiesce_deadline_leaves_blocking_cleanup_fenced_in_background(
    tmp_path: Path,
) -> None:
    projection_entered = threading.Event()
    finish_projection = threading.Event()

    class AttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def provider_attachments(self, _bundle):
            projection_entered.set()
            finish_projection.wait(timeout=1.0)
            return ()

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    attachment_store = AttachmentStore()
    writer = _writer(
        tmp_path,
        FakeMemoryProvider(),
        attachment_store=attachment_store,
    )
    reservation = writer.reserve("blocking-projection")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="caption",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"
    assert await asyncio.to_thread(projection_entered.wait, 1.0)

    quiescing = asyncio.create_task(writer.quiesce(timeout_seconds=0.001))
    try:
        assert not await asyncio.wait_for(asyncio.shield(quiescing), timeout=0.2)
        writer.resume_intake()
        assert writer.reserve("while-cleanup-settles") == "disabled"
        assert writer._permits == MAX_WRITER_PERMITS - 1
    finally:
        finish_projection.set()
        await asyncio.gather(quiescing, return_exceptions=True)
        await asyncio.wait_for(writer.close(), timeout=1.0)

    assert attachment_store.released == ["bundle-0"]
    assert writer._permits == MAX_WRITER_PERMITS
    reservation = writer.reserve("after-cleanup-settles")
    assert not isinstance(reservation, str)
    reservation.release()


@pytest.mark.asyncio
async def test_quiesce_cancels_inflight_call_and_releases_volatile_resources(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    stop_calls = 0

    async def block_add(_capture) -> None:
        entered.set()
        await asyncio.Event().wait()

    recoveries: list[bool] = []

    async def stop_reap(recover: bool) -> bool:
        nonlocal stop_calls
        stop_calls += 1
        recoveries.append(recover)
        return True

    class AttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def provider_attachments(self, _bundle):
            return ()

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    provider = FakeMemoryProvider(add_hook=block_add)
    attachment_store = AttachmentStore()
    writer = _writer(
        tmp_path,
        provider,
        ambiguous_stop_reap=stop_reap,
        attachment_store=attachment_store,
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message-0",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"
    await entered.wait()
    writer._pending[_ref(1).serialize()] = _PendingSession(
        _ref(1), "raw-session-1", deque(["digest-1"]), 0.0, 0.0
    )

    assert await writer.quiesce(timeout_seconds=1.0)

    assert stop_calls == 1
    assert recoveries == [False]
    assert attachment_store.released == ["bundle-0"]
    assert writer._permits == MAX_WRITER_PERMITS
    assert writer._pending == {}
    assert writer._worker_task is None


@pytest.mark.asyncio
async def test_cancelled_flush_reaps_sidecar_before_releasing_barrier(
    tmp_path: Path,
) -> None:
    flush_entered = asyncio.Event()
    reap_entered = asyncio.Event()
    finish_reap = asyncio.Event()

    async def block_flush(_session_ref: ProviderSessionRef) -> None:
        flush_entered.set()
        await asyncio.Event().wait()

    recoveries: list[bool] = []

    async def stop_reap(recover: bool) -> bool:
        recoveries.append(recover)
        reap_entered.set()
        await finish_reap.wait()
        return True

    writer = _writer(
        tmp_path,
        FakeMemoryProvider(flush_hook=block_flush),
        ambiguous_stop_reap=stop_reap,
    )
    ref = _ref()
    writer._pending[ref.serialize()] = _PendingSession(
        ref, "raw-session-0", deque(["digest-0"]), 0.0, 0.0
    )
    assert writer.offer_barrier("raw-session-0") == "queued"
    await flush_entered.wait()

    closing = asyncio.create_task(writer.close())
    await reap_entered.wait()

    assert not closing.done()
    assert writer._permits == MAX_WRITER_PERMITS - 1
    finish_reap.set()
    await asyncio.wait_for(closing, timeout=1.0)

    assert writer._permits == MAX_WRITER_PERMITS
    assert writer._pending == {}
    assert recoveries == [False]


@pytest.mark.asyncio
async def test_close_during_attachment_projection_releases_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_entered = asyncio.Event()

    class AttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def provider_attachments(self, _bundle):
            raise AssertionError("projection is controlled by the async test bridge")

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    attachment_store = AttachmentStore()

    async def controlled_run_blocking(operation, *args, **_kwargs):
        if operation == attachment_store.provider_attachments:
            projection_entered.set()
            await asyncio.Event().wait()
        return operation(*args)

    monkeypatch.setattr("avibe_memory.writer.run_blocking", controlled_run_blocking)
    writer = _writer(
        tmp_path,
        FakeMemoryProvider(),
        attachment_store=attachment_store,
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message-0",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"
    await projection_entered.wait()

    await asyncio.wait_for(writer.close(), timeout=1.0)

    assert attachment_store.released == ["bundle-0"]
    assert writer._permits == MAX_WRITER_PERMITS
    assert writer._worker_task is None


@pytest.mark.asyncio
async def test_cancelled_attachment_projection_failure_disables_later_intake(
    tmp_path: Path,
) -> None:
    projection_entered = threading.Event()
    finish_projection = threading.Event()

    class AttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def provider_attachments(self, _bundle):
            projection_entered.set()
            finish_projection.wait(timeout=1.0)
            raise AttachmentBundleInvalidError(
                "memory_store_unavailable",
                "bundle verification failed",
            )

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    attachment_store = AttachmentStore()
    writer = _writer(
        tmp_path,
        FakeMemoryProvider(),
        attachment_store=attachment_store,
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message-0",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"
    assert await asyncio.to_thread(projection_entered.wait, 1.0)

    closing = asyncio.create_task(writer.close())
    try:
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        finish_projection.set()
        await asyncio.wait_for(closing, timeout=1.0)

    assert attachment_store.released == ["bundle-0"]
    assert not writer.attachments_enabled
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_attachment_cleanup_failure_disables_later_attachment_intake(tmp_path: Path) -> None:
    class FailingAttachmentStore:
        def provider_attachments(self, _bundle):
            return ()

        def release(self, _bundle_id: str) -> None:
            raise OSError("cleanup failed")

    writer = _writer(
        tmp_path,
        FakeMemoryProvider(),
        attachment_store=FailingAttachmentStore(),
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message-0",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"

    await writer.wait_idle_for_tests()

    assert not writer.attachments_enabled
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fails", [False, True])
async def test_cancelled_attachment_cleanup_tracks_settled_release_result(
    tmp_path: Path,
    release_fails: bool,
) -> None:
    """MEMORY-IM-ATTACH-013: readiness follows the settled cleanup result."""

    release_entered = threading.Event()
    finish_release = threading.Event()

    class AttachmentStore:
        def provider_attachments(self, _bundle):
            return ()

        def release(self, _bundle_id: str) -> None:
            release_entered.set()
            finish_release.wait(timeout=1.0)
            if release_fails:
                raise OSError("cleanup failed")

    writer = _writer(
        tmp_path,
        FakeMemoryProvider(),
        attachment_store=AttachmentStore(),
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message-0",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"
    assert await asyncio.to_thread(release_entered.wait, 1.0)

    closing = asyncio.create_task(writer.close())
    await asyncio.sleep(0)
    finish_release.set()
    await asyncio.wait_for(closing, timeout=1.0)

    assert writer.attachments_enabled is not release_fails
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_invalid_attachment_bundle_disables_later_attachment_intake(
    tmp_path: Path,
) -> None:
    class InvalidAttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def provider_attachments(self, _bundle):
            raise AttachmentBundleInvalidError(
                "memory_store_unavailable",
                "bundle verification failed",
            )

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    provider = FakeMemoryProvider()
    attachment_store = InvalidAttachmentStore()
    writer = _writer(
        tmp_path,
        provider,
        attachment_store=attachment_store,
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="message-0",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"

    await writer.wait_idle_for_tests()

    assert provider.captures == []
    assert attachment_store.released == ["bundle-0"]
    assert not writer.attachments_enabled
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_empty_attachment_projection_is_dropped_before_provider_add(
    tmp_path: Path,
) -> None:
    class EmptyAttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def provider_attachments(self, _bundle):
            return ()

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    provider = FakeMemoryProvider()
    attachment_store = EmptyAttachmentStore()
    writer = _writer(
        tmp_path,
        provider,
        attachment_store=attachment_store,
    )
    reservation = writer.reserve("digest-0")
    assert not isinstance(reservation, str)
    assert writer.offer_capture(
        reservation,
        _admission(0),
        text="",
        attachments=(),
        bundle=SimpleNamespace(bundle_id="bundle-0"),
    ) == "queued"

    await writer.wait_idle_for_tests()

    assert provider.captures == []
    assert attachment_store.released == ["bundle-0"]
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_scheduled_barrier_is_visible_until_dequeue_and_retry_exhaustion_settles(
    tmp_path: Path,
) -> None:
    provider = FakeMemoryProvider(
        flush_results=deque([FlushRetryable()] * MAX_ATTEMPTS)
    )
    writer = _writer(tmp_path, provider)
    ref = _ref()
    key = ref.serialize()
    now = datetime.now(timezone.utc)
    writer._pending[key] = _PendingSession(
        ref,
        "raw-session-0",
        deque(f"digest-{index}" for index in range(MAX_PENDING_MESSAGE_IDS)),
        now.timestamp() - MAX_UNFLUSHED_AGE_SECONDS,
        now.timestamp() - IDLE_FLUSH_SECONDS,
        scheduled=True,
    )
    assert writer._pending[key].scheduled
    await writer._flush_barrier(_BarrierItem(scheduled_key=key))
    assert len(provider.flushes) == MAX_ATTEMPTS
    assert key not in writer._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", ["idle", "age", "count"])
async def test_each_exact_flush_threshold_queues_a_visible_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threshold: str
) -> None:
    provider = FakeMemoryProvider()
    current = 1_000.0
    writer = _writer(tmp_path, provider, monotonic=lambda: current)
    ref = _ref()
    key = ref.serialize()
    message_count = MAX_UNFLUSHED_MESSAGES if threshold == "count" else 1
    first_at = current
    last_ack_at = current
    if threshold == "age":
        first_at -= MAX_UNFLUSHED_AGE_SECONDS
    if threshold == "idle":
        last_ack_at -= IDLE_FLUSH_SECONDS
    writer._pending[key] = _PendingSession(
        ref,
        "raw-session-0",
        deque(f"digest-{index}" for index in range(message_count)),
        first_at,
        last_ack_at,
    )
    sleeps = 0

    async def one_scheduler_tick(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("avibe_memory.writer.asyncio.sleep", one_scheduler_tick)
    await writer._schedule_due_flushes()
    assert writer._pending[key].scheduled
    assert writer._queued_items == 1
    queued = writer._queue.get_nowait()
    assert isinstance(queued, _BarrierItem)
    assert queued.scheduled_key == key
    writer._queue.task_done()
    writer._queued_items = 0
    writer._release_permit()


@pytest.mark.asyncio
async def test_flush_threshold_near_misses_do_not_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = 1_000.0
    writer = _writer(tmp_path, FakeMemoryProvider(), monotonic=lambda: current)
    ref = _ref()
    key = ref.serialize()
    writer._pending[key] = _PendingSession(
        ref,
        "raw-session-0",
        deque(
            f"digest-{index}" for index in range(MAX_UNFLUSHED_MESSAGES - 1)
        ),
        current - MAX_UNFLUSHED_AGE_SECONDS + 1,
        current - IDLE_FLUSH_SECONDS + 1,
    )
    sleeps = 0

    async def one_scheduler_tick(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("avibe_memory.writer.asyncio.sleep", one_scheduler_tick)
    await writer._schedule_due_flushes()
    assert not writer._pending[key].scheduled
    assert writer._queued_items == 0
    assert writer._queue.empty()


@pytest.mark.asyncio
async def test_wall_clock_rollback_does_not_delay_due_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wall_time = datetime(2026, 8, 23, tzinfo=timezone.utc)
    monotonic_time = 1_000.0
    writer = _writer(
        tmp_path,
        FakeMemoryProvider(),
        now=lambda: wall_time,
        monotonic=lambda: monotonic_time,
    )
    ref = _ref()
    key = ref.serialize()
    writer._pending[key] = _PendingSession(
        ref,
        "raw-session-0",
        deque(["digest"]),
        monotonic_time,
        monotonic_time,
    )
    wall_time -= timedelta(days=1)
    monotonic_time += IDLE_FLUSH_SECONDS
    sleeps = 0

    async def one_scheduler_tick(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("avibe_memory.writer.asyncio.sleep", one_scheduler_tick)
    await writer._schedule_due_flushes()

    assert writer._pending[key].scheduled
    queued = writer._queue.get_nowait()
    assert isinstance(queued, _BarrierItem)
    assert queued.scheduled_key == key
    writer._queue.task_done()
    writer._queued_items = 0
    writer._release_permit()


@pytest.mark.asyncio
async def test_session_barrier_flushes_only_matching_raw_session(tmp_path: Path) -> None:
    """MEMORY-SEARCH-006: lifecycle barriers are raw-session scoped."""

    provider = FakeMemoryProvider()
    writer = _writer(tmp_path, provider)
    first = _ref(1)
    second = _ref(2)
    writer._pending[first.serialize()] = _PendingSession(
        first, "raw-a", deque(["a"]), 0.0, 0.0
    )
    writer._pending[second.serialize()] = _PendingSession(
        second, "raw-b", deque(["b"]), 0.0, 0.0
    )

    await writer._flush_barrier(_BarrierItem(raw_session_id="raw-a"))

    assert provider.flushes == [first]
    assert first.serialize() not in writer._pending
    assert second.serialize() in writer._pending


@pytest.mark.asyncio
async def test_stale_scheduled_barrier_is_a_noop(tmp_path: Path) -> None:
    provider = FakeMemoryProvider()
    writer = _writer(tmp_path, provider)
    retained = _ref(1)
    writer._pending[retained.serialize()] = _PendingSession(
        retained, "raw-a", deque(["a"]), 0.0, 0.0
    )

    await writer._flush_barrier(_BarrierItem(scheduled_key="missing"))

    assert provider.flushes == []
    assert retained.serialize() in writer._pending


@pytest.mark.asyncio
async def test_full_pending_tracker_keeps_existing_sessions(
    tmp_path: Path,
) -> None:
    provider = FakeMemoryProvider()
    writer = _writer(tmp_path, provider)
    retained_keys = []
    for index in range(MAX_PENDING_SESSIONS):
        ref = _ref(index + 1)
        key = ref.serialize()
        retained_keys.append(key)
        writer._pending[key] = _PendingSession(
            ref,
            f"raw-{index}",
            deque([f"digest-{index}"]),
            0.0,
            0.0,
        )
    _reserve_and_offer(writer, MAX_PENDING_SESSIONS + 1)

    await writer.wait_idle_for_tests()

    assert list(writer._pending) == retained_keys
    assert writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_failed_automatic_barrier_offer_defers_for_five_minutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = 1_000.0
    writer = _writer(tmp_path, FakeMemoryProvider(), monotonic=lambda: current)
    ref = _ref()
    key = ref.serialize()
    writer._pending[key] = _PendingSession(
        ref,
        "raw-session-0",
        deque(["digest"]),
        current,
        current - IDLE_FLUSH_SECONDS,
    )
    writer._permits = 0
    sleeps = 0

    async def two_scheduler_ticks(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("avibe_memory.writer.asyncio.sleep", two_scheduler_ticks)
    await writer._schedule_due_flushes()

    assert writer._pending[key].retry_after == current + IDLE_FLUSH_SECONDS
    assert writer._queued_items == 0


def test_writer_bounds_are_fixed_and_not_user_tunable() -> None:
    assert MAX_WRITER_PERMITS == MAX_DUPLICATE_ENTRIES == MAX_PENDING_SESSIONS == 256
    assert MAX_PENDING_MESSAGE_IDS == MAX_UNFLUSHED_MESSAGES == 100
    assert MAX_UNFLUSHED_AGE_SECONDS == 30 * 60
