"""Bounded, best-effort process-local Memory capture delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from avibe_memory.attachments import AttachmentPinError, AttachmentPinStore, PinnedBundle
from core.blocking import run_blocking
from avibe_memory.everos import (
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
from avibe_memory.observations import AddResult, FlushResult
from avibe_memory.store import MemoryStore, VolatileAdmission
from avibe_memory.types import CaptureAttachment, ProviderSessionRef


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
CaptureOfferOutcome = Literal["queued", "full", "disabled", "unavailable"]
AmbiguousStop = Callable[[bool], Awaitable[bool] | bool]


@dataclass(slots=True)
class _CaptureItem:
    digest: str
    capture: ProviderCapture
    bundle: PinnedBundle | None
    reservation: "WriterReservation"
    raw_session_id: str


@dataclass(slots=True)
class _BarrierItem:
    raw_session_id: str | None = None
    scheduled_key: str | None = None
    owns_permit: bool = False


@dataclass(slots=True)
class _PendingSession:
    ref: ProviderSessionRef
    raw_session_id: str
    message_ids: deque[str]
    first_at: float
    last_ack_at: float
    scheduled: bool = False
    retry_after: float = 0.0


class WriterReservation:
    """A permit and duplicate-LRU claim held until terminal handling."""

    def __init__(self, writer: "BestEffortMemoryWriter", digest: str | None) -> None:
        self._writer = writer
        self.digest = digest
        self.active = True
        self.handed_off = False

    def release(self) -> None:
        if self.active:
            self.active = False
            self._writer._release_reservation(self.digest)

    def abandon(self) -> None:
        """Release work that never entered the writer queue."""

        if self.active:
            self.active = False
            self._writer._abandon_reservation(self.digest)

    def bind_digest(
        self,
        digest: str,
    ) -> Literal["bound", "duplicate", "disabled", "unavailable"]:
        """Bind the final source digest after the pending work is admitted."""

        return self._writer.bind_reservation(self, digest)


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
        monotonic: Callable[[], float] | None = None,
        processing_event: Callable[..., Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._attachment_store = attachment_store
        self._ambiguous_stop_reap = ambiguous_stop_reap
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._processing_event = processing_event
        self._queue: asyncio.Queue[_CaptureItem | _BarrierItem] = asyncio.Queue(
            maxsize=MAX_WRITER_PERMITS
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._active_provider_calls = 0
        self._queued_items = 0
        self._permits = MAX_WRITER_PERMITS
        self._duplicate_lru: OrderedDict[str, bool] = OrderedDict()
        self._pending: OrderedDict[str, _PendingSession] = OrderedDict()
        self._intake_paused = False
        self._closed = False
        self._unavailable = False
        self._attachments_disabled = False
        self.dropped = 0

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    @property
    def attachments_enabled(self) -> bool:
        return not self._attachments_disabled

    def dropped_count(self) -> int:
        """Return the process-local count of queue insertions discarded as full."""

        return self.dropped

    def disable_attachment_intake(self) -> None:
        self._attachments_disabled = True

    def enable_attachment_intake(self) -> None:
        self._attachments_disabled = self._attachment_store is None

    def _handle_cancelled_attachment_projection(self, error: BaseException) -> None:
        if isinstance(error, AttachmentPinError):
            self.disable_attachment_intake()

    def replace_provider(self, provider: MemoryProviderPort) -> None:
        self._provider = provider
        self._unavailable = False

    def pause_intake(self) -> None:
        self._intake_paused = True

    def resume_intake(self) -> None:
        # ``_closed`` independently fences a settling cleanup. Clearing the
        # pause records recovery intent without reopening that old work early.
        if not self._unavailable:
            self._intake_paused = False

    def reset_duplicate_generation(self) -> None:
        """Forget process-local duplicate claims after a completed Clear."""

        self._duplicate_lru.clear()

    def reserve(
        self,
        digest: str,
    ) -> WriterReservation | Literal["duplicate", "full", "disabled", "unavailable"]:
        """Try to claim one permit before attachment pinning."""

        reservation = self.reserve_pending()
        if isinstance(reservation, str):
            return reservation
        outcome = reservation.bind_digest(digest)
        if outcome == "bound":
            return reservation
        return outcome

    def reserve_pending(
        self,
    ) -> WriterReservation | Literal["full", "disabled", "unavailable"]:
        """Claim capacity before deferred capture work performs I/O."""

        if self._unavailable:
            return "unavailable"
        if self._closed or self._intake_paused or not self._enabled():
            return "disabled"
        if self._permits <= 0:
            return "full"
        self._permits -= 1
        return WriterReservation(self, None)

    def bind_reservation(
        self,
        reservation: WriterReservation,
        digest: str,
    ) -> Literal["bound", "duplicate", "disabled", "unavailable"]:
        """Attach a source digest to an already-counted pending capture."""

        if (
            reservation._writer is not self
            or not reservation.active
            or reservation.digest is not None
        ):
            return "disabled"
        if self._unavailable:
            reservation.abandon()
            return "unavailable"
        if self._closed or self._intake_paused or not self._enabled():
            reservation.abandon()
            return "disabled"
        if digest in self._duplicate_lru:
            self._duplicate_lru.move_to_end(digest)
            reservation.abandon()
            return "duplicate"
        reservation.digest = digest
        self._duplicate_lru[digest] = True
        self._duplicate_lru.move_to_end(digest)
        self._evict_duplicate_entries()
        return "bound"

    def offer_capture(
        self,
        reservation: WriterReservation,
        admission: VolatileAdmission,
        *,
        text: str,
        attachments: tuple[CaptureAttachment, ...],
        bundle: PinnedBundle | None,
        sender_name: str | None = None,
    ) -> CaptureOfferOutcome:
        """Queue one already-admitted capture without waiting for worker space."""

        if (
            not reservation.active
            or reservation.digest is None
            or admission.outcome != "accepted"
        ):
            return "disabled"
        if self._unavailable:
            return "unavailable"
        if self._closed or self._intake_paused or not self._enabled():
            return "disabled"
        assert admission.provider_session_ref is not None
        assert admission.provider_timestamp_ms is not None
        assert admission.raw_session_id is not None
        item = _CaptureItem(
            digest=reservation.digest,
            capture=ProviderCapture(
                session_ref=admission.provider_session_ref,
                text=text,
                provider_timestamp_ms=admission.provider_timestamp_ms,
                attachments=attachments,
                sender_name=sender_name,
            ),
            bundle=bundle,
            reservation=reservation,
            raw_session_id=admission.raw_session_id,
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            return "full"
        reservation.handed_off = True
        self._queued_items += 1
        self._ensure_worker()
        return "queued"

    def offer_barrier(
        self,
        raw_session_id: str,
    ) -> BarrierOutcome:
        """Offer a non-blocking flush barrier; no delivery wait is exposed."""

        if (
            not isinstance(raw_session_id, str)
            or not raw_session_id
            or self._closed
            or self._intake_paused
            or self._unavailable
            or not self._enabled()
        ):
            return "disabled"
        if not self._try_acquire_permit():
            return "full"
        try:
            self._queue.put_nowait(
                _BarrierItem(raw_session_id=raw_session_id, owns_permit=True)
            )
        except asyncio.QueueFull:
            self.dropped += 1
            self._release_permit()
            return "full"
        self._queued_items += 1
        self._ensure_worker()
        return "queued"

    async def quiesce(self, *, timeout_seconds: float = 30.0) -> bool:
        """Join current admissions for authority-changing transitions."""

        self.pause_intake()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(float(timeout_seconds), 0.001)
        try:
            await asyncio.wait_for(
                self.close(),
                timeout=max(deadline - loop.time(), 0.001),
            )
        except asyncio.TimeoutError:
            # ``close`` shields its single cleanup task, so this deadline stops
            # waiting without reopening the old authority or abandoning cleanup.
            return False
        while self._permits < MAX_WRITER_PERMITS:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0)
        return True

    async def close(self) -> None:
        """Drop volatile queued/tracked work during shutdown or replacement."""

        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_once(),
                name="memory-writer-close",
            )
            self._close_task = task
            task.add_done_callback(self._retire_close_task)
        await asyncio.shield(task)

    def _retire_close_task(self, task: asyncio.Task[None]) -> None:
        if self._close_task is task:
            self._close_task = None
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _close_once(self) -> None:
        self._closed = True
        self._intake_paused = True
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        if self._worker_task is not None:
            if not self._worker_task.done():
                self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
            self._worker_task = None
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _CaptureItem):
                await self._cleanup_item(item)
            elif item.owns_permit:
                self._release_permit()
            self._queue.task_done()
            self._queued_items = max(0, self._queued_items - 1)
        self._pending.clear()
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

    def _release_reservation(self, digest: str | None) -> None:
        self._release_permit()
        if digest is not None and digest in self._duplicate_lru:
            self._duplicate_lru[digest] = False
            self._duplicate_lru.move_to_end(digest)
            self._evict_duplicate_entries()

    def _abandon_reservation(self, digest: str | None) -> None:
        self._release_permit()
        if digest is not None:
            self._duplicate_lru.pop(digest, None)

    async def _run(self) -> None:
        while not self._closed:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                if isinstance(item, _CaptureItem):
                    if self._unavailable:
                        await self._cleanup_item(item)
                    else:
                        await self._deliver(item)
                else:
                    if not self._unavailable:
                        await self._flush_barrier(item)
            except asyncio.CancelledError:
                if isinstance(item, _CaptureItem):
                    await self._cleanup_item(item)
                raise
            except Exception:
                logger.exception("Memory writer item failed")
                if isinstance(item, _CaptureItem):
                    await self._cleanup_item(item)
            finally:
                if isinstance(item, _BarrierItem) and item.owns_permit:
                    self._release_permit()
                self._queue.task_done()
                self._queued_items = max(0, self._queued_items - 1)

    async def _deliver(self, item: _CaptureItem) -> None:
        if not self._enabled():
            await self._cleanup_item(item)
            return
        capture = item.capture
        attachments = capture.attachments
        if item.bundle is not None and self._attachment_store is not None:
            try:
                attachments = await run_blocking(
                    self._attachment_store.provider_attachments,
                    item.bundle,
                    on_cancel_error=self._handle_cancelled_attachment_projection,
                )
            except AttachmentPinError:
                self._attachments_disabled = True
                await self._cleanup_item(item)
                return
        if not capture.text.strip() and not attachments:
            await self._cleanup_item(item)
            return
        capture = ProviderCapture(
            session_ref=capture.session_ref,
            text=capture.text,
            provider_timestamp_ms=capture.provider_timestamp_ms,
            attachments=attachments,
            sender_name=capture.sender_name,
        )
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            if not self._enabled():
                await self._cleanup_item(item)
                return
            attempt += 1
            self._active_provider_calls += 1
            try:
                result = await self._provider.add(capture)
            except asyncio.CancelledError:
                await self._ambiguous_outcome(
                    "memory_provider_timeout",
                    recover=False,
                )
                await self._cleanup_item(item)
                raise
            except MemoryProviderSystemFailure as failure:
                if failure.ambiguous:
                    await self._ambiguous_outcome(failure.error)
                    await self._cleanup_item(item)
                    return
                if attempt < MAX_ATTEMPTS:
                    continue
                await self._terminal_failure(item, failure.error)
                return
            except MemoryProviderFailure as failure:
                if failure.ambiguous:
                    await self._ambiguous_outcome(failure.error)
                    await self._cleanup_item(item)
                    return
                if failure.retryable and attempt < MAX_ATTEMPTS:
                    continue
                await self._terminal_failure(item, failure.error)
                return
            except Exception:
                await self._ambiguous_outcome("memory_provider_response_invalid")
                await self._cleanup_item(item)
                return
            finally:
                self._active_provider_calls = max(0, self._active_provider_calls - 1)

            if (
                isinstance(result, AddAck)
                and result.status in {"accumulated", "extracted"}
                and _valid_receipt(result.request_id)
            ):
                await self._success(item, extracted=result.status == "extracted")
                return
            if isinstance(result, AddRejected):
                if (
                    attachments
                    and capture.text.strip()
                    and attachment_add_rejection_proves_no_write(capture, result)
                    and attempt < MAX_ATTEMPTS
                ):
                    attachments = ()
                    capture = ProviderCapture(
                        session_ref=capture.session_ref,
                        text=capture.text,
                        provider_timestamp_ms=capture.provider_timestamp_ms,
                        sender_name=capture.sender_name,
                    )
                    continue
                await self._terminal_failure(item, "memory_processing_failed")
                return
            await self._ambiguous_outcome("memory_provider_response_invalid")
            await self._cleanup_item(item)
            return

    async def _success(self, item: _CaptureItem, *, extracted: bool) -> None:
        if not self._enabled():
            await self._cleanup_item(item)
            return
        try:
            await run_blocking(self._store.mark_capture_success)
        except Exception:
            pass
        ref = item.capture.session_ref
        key = ref.serialize()
        if extracted:
            self._pending.pop(key, None)
            await self._cleanup_item(item)
            return
        now = self._clock_seconds()
        pending = self._pending.get(key)
        if pending is None:
            if len(self._pending) >= MAX_PENDING_SESSIONS:
                await self._cleanup_item(item)
                return
            pending = _PendingSession(ref, item.raw_session_id, deque(), now, now)
            self._pending[key] = pending
        if len(pending.message_ids) < MAX_PENDING_MESSAGE_IDS:
            pending.message_ids.append(item.digest)
        pending.last_ack_at = now
        await self._cleanup_item(item)

    async def _terminal_failure(self, item: _CaptureItem, error: str) -> None:
        if not self._enabled():
            await self._cleanup_item(item)
            return
        try:
            await run_blocking(self._store.set_last_error, error)
        except Exception:
            pass
        await self._cleanup_item(item)

    async def _cleanup_item(self, item: _CaptureItem) -> None:
        if not item.reservation.active:
            return
        try:
            if item.bundle is not None and self._attachment_store is not None:
                try:
                    await run_blocking(
                        self._attachment_store.release,
                        item.bundle.bundle_id,
                        on_cancel_error=lambda _error: self.disable_attachment_intake(),
                    )
                except Exception:
                    self.disable_attachment_intake()
        finally:
            item.reservation.release()

    async def _flush_barrier(self, item: _BarrierItem) -> None:
        scheduled_pending = (
            self._pending.get(item.scheduled_key)
            if item.scheduled_key is not None
            else None
        )
        if scheduled_pending is not None:
            scheduled_pending.scheduled = False
        if item.scheduled_key is not None:
            if scheduled_pending is None:
                return
            sessions = (scheduled_pending,)
        elif item.raw_session_id is not None:
            sessions = tuple(
                session
                for session in self._pending.values()
                if session.raw_session_id == item.raw_session_id
            )
        else:
            return
        for session in sessions:
            ref = session.ref
            key = ref.serialize()
            pending = self._pending.get(key)
            if pending is None or not pending.message_ids:
                continue
            result: FlushResult | None = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if not self._enabled():
                    self._pending.pop(key, None)
                    return
                self._active_provider_calls += 1
                try:
                    result = await self._provider.flush(ref)
                except asyncio.CancelledError:
                    self._pending.pop(key, None)
                    await self._ambiguous_outcome(
                        "memory_provider_timeout",
                        recover=False,
                    )
                    raise
                except MemoryProviderFailure as failure:
                    if failure.ambiguous:
                        self._pending.pop(key, None)
                        await self._ambiguous_outcome(failure.error)
                        return
                    if failure.retryable and attempt < MAX_ATTEMPTS:
                        continue
                    result = FlushRejected(None, failure.error, True)
                except Exception:
                    self._pending.pop(key, None)
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
            if isinstance(result, FlushSucceeded) and (
                result.status not in {"extracted", "no_extraction"}
                or not _valid_receipt(result.request_id)
            ):
                await self._ambiguous_outcome("memory_provider_response_invalid")
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
                    if (
                        pending.scheduled
                        or not pending.message_ids
                        or now < pending.retry_after
                    ):
                        continue
                    if (
                        now - pending.last_ack_at >= IDLE_FLUSH_SECONDS
                        or now - pending.first_at >= MAX_UNFLUSHED_AGE_SECONDS
                        or len(pending.message_ids) >= MAX_UNFLUSHED_MESSAGES
                    ):
                        pending.scheduled = True
                        if not self._try_acquire_permit():
                            pending.scheduled = False
                            pending.retry_after = now + IDLE_FLUSH_SECONDS
                            continue
                        try:
                            self._queue.put_nowait(
                                _BarrierItem(scheduled_key=key, owns_permit=True)
                            )
                            self._queued_items += 1
                        except asyncio.QueueFull:
                            self.dropped += 1
                            self._release_permit()
                            pending.scheduled = False
                            pending.retry_after = now + IDLE_FLUSH_SECONDS
        except asyncio.CancelledError:
            return

    async def _ambiguous_outcome(self, _error: str, *, recover: bool = True) -> None:
        if not self._enabled():
            return
        self._unavailable = True
        self._intake_paused = True
        self._pending.clear()
        if self._ambiguous_stop_reap is not None:
            try:
                proved = self._ambiguous_stop_reap(recover)
                if asyncio.iscoroutine(proved):
                    proved = await proved
                if not proved:
                    self._unavailable = True
            except Exception:
                self._unavailable = True
        if self._processing_event is not None:
            try:
                occurred_at = self._now()
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=timezone.utc)
                await self._processing_event(
                    "fault",
                    "engine",
                    occurred_at.astimezone(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    self._queued_items,
                )
            except Exception:
                pass

    def _try_acquire_permit(self) -> bool:
        if self._permits <= 0:
            return False
        self._permits -= 1
        return True

    def _release_permit(self) -> None:
        self._permits = min(MAX_WRITER_PERMITS, self._permits + 1)

    def _clock_seconds(self) -> float:
        return self._monotonic()


def _valid_receipt(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= 128
    except UnicodeError:
        return False
