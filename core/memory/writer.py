"""Bounded, best-effort process-local Memory capture delivery."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from core.memory.attachments import AttachmentPinError, AttachmentPinStore, PinnedBundle
from core.memory.blocking import run_blocking
from core.memory.everos import (
    AddAck,
    AddRejected,
    FlushRejected,
    FlushRetryable,
    FlushSucceeded,
    FlushUnknown,
    MemoryProviderFailure,
    MemoryProviderPort,
    MemoryProviderSystemFailure,
    ProviderCapture,
    attachment_add_rejection_proves_no_write,
)
from core.memory.observations import AddResult, FlushResult
from core.memory.store import MemoryStore, VolatileAdmission
from core.memory.types import CaptureAttachment, ProviderSessionRef


logger = logging.getLogger(__name__)

MAX_WRITER_PERMITS = 256
MAX_DUPLICATE_ENTRIES = 256
MAX_ATTEMPTS = 3
MAX_PENDING_SESSIONS = 256
MAX_PENDING_MESSAGE_IDS = 100
IDLE_FLUSH_SECONDS = 5 * 60
MAX_UNFLUSHED_AGE_SECONDS = 30 * 60
MAX_UNFLUSHED_MESSAGES = 100

BarrierOutcome = Literal["queued", "full", "disabled"]
AmbiguousStop = Callable[[], Awaitable[bool] | bool]


@dataclass(slots=True)
class _CaptureItem:
    digest: str
    capture: ProviderCapture
    bundle: PinnedBundle | None
    reservation: "WriterReservation"


@dataclass(slots=True)
class _BarrierItem:
    refs: tuple[ProviderSessionRef, ...] | None
    scheduled_key: str | None = None


@dataclass(slots=True)
class _PendingSession:
    ref: ProviderSessionRef
    message_ids: deque[str]
    first_at: float
    last_ack_at: float
    scheduled: bool = False


class WriterReservation:
    """A permit and duplicate-LRU claim held until terminal handling."""

    def __init__(self, writer: "BestEffortMemoryWriter", digest: str) -> None:
        self._writer = writer
        self.digest = digest
        self.active = True

    def release(self) -> None:
        if self.active:
            self.active = False
            self._writer._release_reservation(self.digest)


class BestEffortMemoryWriter:
    """One ordered worker for volatile add/flush work.

    The queue, duplicate cache, pending-flush tracker, and all payloads live in
    this process only.  A restart, replacement, shutdown, or saturation may
    intentionally discard them.
    """

    def __init__(
        self,
        *,
        store: MemoryStore,
        provider: MemoryProviderPort,
        enabled: Callable[[], bool],
        attachment_store: AttachmentPinStore | None = None,
        ambiguous_stop_reap: AmbiguousStop | None = None,
        now: Callable[[], datetime] | None = None,
        processing_event: Callable[..., Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._attachment_store = attachment_store
        self._ambiguous_stop_reap = ambiguous_stop_reap
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._processing_event = processing_event
        self._queue: asyncio.Queue[_CaptureItem | _BarrierItem] = asyncio.Queue(
            maxsize=MAX_WRITER_PERMITS
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._active_provider_calls = 0
        self._queued_items = 0
        self._permits = MAX_WRITER_PERMITS
        self._duplicate_lru: OrderedDict[str, bool] = OrderedDict()
        self._pending: OrderedDict[str, _PendingSession] = OrderedDict()
        self._intake_paused = False
        self._closed = False
        self._unavailable = False
        self._attachments_disabled = False

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    @property
    def attachments_enabled(self) -> bool:
        return not self._attachments_disabled

    def replace_provider(self, provider: MemoryProviderPort) -> None:
        self._provider = provider

    def pause_intake(self) -> None:
        self._intake_paused = True

    def resume_intake(self) -> None:
        if not self._closed and not self._unavailable:
            self._intake_paused = False

    def reserve(self, digest: str) -> WriterReservation | Literal["duplicate", "full", "disabled"]:
        """Try to claim one permit before attachment pinning."""

        if self._closed or self._intake_paused or self._unavailable or not self._enabled():
            return "disabled"
        if digest in self._duplicate_lru:
            self._duplicate_lru.move_to_end(digest)
            return "duplicate"
        if self._permits <= 0:
            return "full"
        self._permits -= 1
        self._duplicate_lru[digest] = True
        self._duplicate_lru.move_to_end(digest)
        self._evict_duplicate_entries()
        return WriterReservation(self, digest)

    def offer_capture(
        self,
        reservation: WriterReservation,
        admission: VolatileAdmission,
        *,
        text: str,
        attachments: tuple[CaptureAttachment, ...],
        bundle: PinnedBundle | None,
    ) -> bool:
        """Queue one already-admitted capture without waiting for worker space."""

        if not reservation.active or admission.outcome != "accepted":
            reservation.release()
            return False
        if self._closed or self._unavailable:
            reservation.release()
            return False
        assert admission.provider_session_ref is not None
        assert admission.provider_timestamp_ms is not None
        item = _CaptureItem(
            digest=reservation.digest,
            capture=ProviderCapture(
                session_ref=admission.provider_session_ref,
                text=text,
                provider_timestamp_ms=admission.provider_timestamp_ms,
                attachments=attachments,
            ),
            bundle=bundle,
            reservation=reservation,
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            reservation.release()
            return False
        self._queued_items += 1
        self._ensure_worker()
        return True

    def offer_barrier(
        self,
        refs: tuple[ProviderSessionRef, ...] | None = None,
    ) -> BarrierOutcome:
        """Offer a non-blocking flush barrier; no delivery wait is exposed."""

        if self._closed or self._unavailable:
            return "disabled"
        try:
            self._queue.put_nowait(_BarrierItem(refs=refs))
        except asyncio.QueueFull:
            return "full"
        self._queued_items += 1
        self._ensure_worker()
        return "queued"

    async def quiesce(self, *, timeout_seconds: float = 30.0) -> bool:
        """Join current generation admissions for authority-changing transitions."""

        self.pause_intake()
        deadline = asyncio.get_running_loop().time() + max(float(timeout_seconds), 0.001)
        while (
            self._permits < MAX_WRITER_PERMITS
            or self._active_provider_calls
            or self._queued_items
        ):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0)
        return True

    async def close(self) -> None:
        """Drop volatile queued/tracked work during shutdown or replacement."""

        self._closed = True
        self._intake_paused = True
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _CaptureItem):
                await self._cleanup_item(item)
            self._queue.task_done()
            self._queued_items = max(0, self._queued_items - 1)
        if self._worker_task is not None and self._worker_task.done():
            self._worker_task = None
        # Runtime replacement reuses the writer object after dropping the old
        # generation.  A later authority transition may resume intake.
        self._closed = False

    async def wait_idle_for_tests(self, *, timeout_seconds: float = 5.0) -> None:
        """Deterministic test-only synchronization; no product drain API."""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        remaining = max(deadline - asyncio.get_running_loop().time(), 0.001)
        await asyncio.wait_for(self._queue.join(), timeout=remaining)
        while self._active_provider_calls:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Memory writer did not become idle")
            await asyncio.sleep(0)

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._run(), name="memory-writer")
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(
                self._schedule_due_flushes(), name="memory-writer-scheduler"
            )

    def _evict_duplicate_entries(self) -> None:
        while len(self._duplicate_lru) > MAX_DUPLICATE_ENTRIES:
            for digest, pending in self._duplicate_lru.items():
                if not pending:
                    self._duplicate_lru.pop(digest, None)
                    break
            else:
                # Every retained digest still owns a reservation. The shared
                # permit bound prevents this protected set from growing.
                break

    def _release_reservation(self, digest: str) -> None:
        self._permits = min(MAX_WRITER_PERMITS, self._permits + 1)
        if digest in self._duplicate_lru:
            self._duplicate_lru[digest] = False
            self._duplicate_lru.move_to_end(digest)
            self._evict_duplicate_entries()

    async def _run(self) -> None:
        while not self._closed:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                if isinstance(item, _CaptureItem):
                    await self._deliver(item)
                else:
                    await self._flush_barrier(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Memory writer item failed")
                if isinstance(item, _CaptureItem):
                    await self._cleanup_item(item)
            finally:
                self._queue.task_done()
                self._queued_items = max(0, self._queued_items - 1)

    async def _deliver(self, item: _CaptureItem) -> None:
        capture = item.capture
        attachments = capture.attachments
        if item.bundle is not None and self._attachment_store is not None:
            try:
                attachments = await run_blocking(
                    self._attachment_store.provider_attachments,
                    item.bundle,
                )
            except AttachmentPinError:
                await self._cleanup_item(item)
                return
        capture = ProviderCapture(
            session_ref=capture.session_ref,
            text=capture.text,
            provider_timestamp_ms=capture.provider_timestamp_ms,
            attachments=attachments,
        )
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            self._active_provider_calls += 1
            try:
                result = await self._provider.add(capture)
            except asyncio.CancelledError:
                await self._ambiguous_outcome("memory_provider_timeout")
                return
            except MemoryProviderSystemFailure as failure:
                if failure.ambiguous:
                    await self._ambiguous_outcome(failure.error)
                    return
                if attempt < MAX_ATTEMPTS:
                    continue
                await self._terminal_failure(item, failure.error)
                return
            except MemoryProviderFailure as failure:
                if failure.ambiguous:
                    await self._ambiguous_outcome(failure.error)
                    return
                if failure.retryable and attempt < MAX_ATTEMPTS:
                    continue
                await self._terminal_failure(item, failure.error)
                return
            except Exception:
                await self._ambiguous_outcome("memory_provider_response_invalid")
                return
            finally:
                self._active_provider_calls = max(0, self._active_provider_calls - 1)

            if isinstance(result, AddAck) and result.status in {"accumulated", "extracted"}:
                await self._success(item)
                return
            if isinstance(result, AddRejected):
                if (
                    attachments
                    and attachment_add_rejection_proves_no_write(capture, result)
                    and attempt < MAX_ATTEMPTS
                ):
                    attachments = ()
                    capture = ProviderCapture(
                        session_ref=capture.session_ref,
                        text=capture.text,
                        provider_timestamp_ms=capture.provider_timestamp_ms,
                    )
                    continue
                await self._terminal_failure(item, "memory_processing_failed")
                return
            await self._ambiguous_outcome("memory_provider_response_invalid")
            return

    async def _success(self, item: _CaptureItem) -> None:
        try:
            await run_blocking(self._store.mark_capture_success)
        except Exception:
            pass
        ref = item.capture.session_ref
        now = self._clock_seconds()
        key = ref.serialize()
        pending = self._pending.get(key)
        if pending is None:
            if len(self._pending) >= MAX_PENDING_SESSIONS:
                oldest_key = next(iter(self._pending))
                self._pending.pop(oldest_key, None)
            pending = _PendingSession(ref, deque(), now, now)
            self._pending[key] = pending
        if len(pending.message_ids) < MAX_PENDING_MESSAGE_IDS:
            pending.message_ids.append(item.digest)
        pending.last_ack_at = now
        await self._cleanup_item(item)

    async def _terminal_failure(self, item: _CaptureItem, error: str) -> None:
        try:
            await run_blocking(self._store.set_last_error, error)
        except Exception:
            pass
        await self._cleanup_item(item)

    async def _cleanup_item(self, item: _CaptureItem) -> None:
        if item.bundle is not None and self._attachment_store is not None:
            try:
                await run_blocking(self._attachment_store.release, item.bundle.bundle_id)
            except Exception:
                self._attachments_disabled = True
        item.reservation.release()

    async def _flush_barrier(self, item: _BarrierItem) -> None:
        scheduled_pending = (
            self._pending.get(item.scheduled_key)
            if item.scheduled_key is not None
            else None
        )
        if scheduled_pending is not None:
            scheduled_pending.scheduled = False
        refs = item.refs
        if refs is None:
            refs = (
                (scheduled_pending.ref,)
                if scheduled_pending is not None
                else tuple(session.ref for session in self._pending.values())
            )
        for ref in refs:
            key = ref.serialize()
            pending = self._pending.get(key)
            if pending is None or not pending.message_ids:
                continue
            result: FlushResult | None = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                self._active_provider_calls += 1
                try:
                    result = await self._provider.flush(ref)
                except MemoryProviderFailure as failure:
                    if failure.ambiguous:
                        await self._ambiguous_outcome(failure.error)
                        return
                    if attempt < MAX_ATTEMPTS:
                        continue
                    result = FlushRejected(None, failure.error, True)
                except Exception:
                    await self._ambiguous_outcome("memory_provider_response_invalid")
                    return
                finally:
                    self._active_provider_calls = max(0, self._active_provider_calls - 1)
                if isinstance(result, FlushRetryable) and attempt < MAX_ATTEMPTS:
                    continue
                break
            if isinstance(result, FlushUnknown):
                self._pending.pop(key, None)
                await self._ambiguous_outcome("memory_provider_timeout")
                return
            if isinstance(result, (FlushSucceeded, FlushRejected, FlushRetryable)):
                # A retryable response that survives the fixed attempt budget
                # is settled as volatile loss; never leave a scheduled marker
                # pinning an unbounded pending session.
                pending.message_ids.clear()
                self._pending.pop(key, None)

    async def _schedule_due_flushes(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(1)
                now = self._clock_seconds()
                for key, pending in tuple(self._pending.items()):
                    if pending.scheduled or not pending.message_ids:
                        continue
                    if (
                        now - pending.last_ack_at >= IDLE_FLUSH_SECONDS
                        or now - pending.first_at >= MAX_UNFLUSHED_AGE_SECONDS
                        or len(pending.message_ids) >= MAX_UNFLUSHED_MESSAGES
                    ):
                        pending.scheduled = True
                        try:
                            self._queue.put_nowait(_BarrierItem(None, key))
                            self._queued_items += 1
                        except asyncio.QueueFull:
                            pending.scheduled = False
        except asyncio.CancelledError:
            return

    async def _ambiguous_outcome(self, error: str) -> None:
        self._unavailable = True
        self._intake_paused = True
        if self._ambiguous_stop_reap is None:
            return
        try:
            proved = self._ambiguous_stop_reap()
            if asyncio.iscoroutine(proved):
                proved = await proved
            if not proved:
                self._unavailable = True
        except Exception:
            self._unavailable = True
        if self._processing_event is not None:
            try:
                await self._processing_event("fault", "engine", error, 0)
            except Exception:
                pass

    def _clock_seconds(self) -> float:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
