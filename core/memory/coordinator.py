"""Durable per-session add serialization and flush coordination."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
import weakref
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from core.memory.attachments import (
    AttachmentPinError,
    AttachmentPinStore,
    decode_pinned_bundle,
)
from core.memory.blocking import run_blocking
from core.memory.everos import (
    AddAck,
    AddRejected,
    FlushRejected,
    FlushResult,
    FlushRetryable,
    FlushSucceeded,
    FlushUnknown,
    MemoryProviderFailure,
    MemoryProviderPort,
    MemoryProviderSystemFailure,
    ProviderCapture,
)
from core.memory.store import (
    AmbiguousAdd,
    FlushLease,
    MemoryStore,
    MessageFailure,
    ProcessingHealthProbe,
    ProcessingNotification,
    QueueRow,
    SystemOutage,
)
from core.memory.types import MemoryErrorCode, ProviderSessionRef, is_memory_error_code


logger = logging.getLogger(__name__)


IDLE_FLUSH_TIMEOUT = timedelta(minutes=5)
MAX_UNFLUSHED_AGE = timedelta(minutes=30)
MAX_UNFLUSHED_MESSAGES = 100
ADD_TIMEOUT_SECONDS = 30.0
FLUSH_TIMEOUT_SECONDS = 300.0
MAX_CONCURRENT_PROVIDER_WRITES = 4
SYSTEM_OUTAGE_RETRY_SECONDS = 5.0
PROCESSING_ACTION_RETRY_SECONDS = 5.0
MAX_PROCESSING_ACTIONS_PER_PASS = 3

AttachmentRelease = Callable[[str], Awaitable[None] | None]
ProcessingFaultKind = Literal["credential", "engine"]
ProcessingEvent = Callable[
    [Literal["fault", "recovered"], ProcessingFaultKind | None, str, int],
    Awaitable[bool],
]


class _ProviderSubmissionAttempt:
    def __init__(self) -> None:
        self.provider_entered = False


class SessionFlushCoordinator:
    """Own the exact session fence from add admission through flush settlement."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        provider: MemoryProviderPort,
        enabled: Callable[[], bool],
        now: Callable[[], datetime] | None = None,
        release_attachment: AttachmentRelease | None = None,
        attachment_store: AttachmentPinStore | None = None,
        attachment_admission_lock: asyncio.Lock | None = None,
        add_timeout_seconds: float = ADD_TIMEOUT_SECONDS,
        flush_timeout_seconds: float = FLUSH_TIMEOUT_SECONDS,
        max_concurrent_writes: int = MAX_CONCURRENT_PROVIDER_WRITES,
        processing_event: ProcessingEvent | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._release_attachment = release_attachment
        self._attachment_store = attachment_store
        self._attachment_admission_lock = attachment_admission_lock
        self._add_timeout_seconds = _positive_timeout(add_timeout_seconds)
        self._flush_timeout_seconds = _positive_timeout(flush_timeout_seconds)
        self._write_slots = asyncio.Semaphore(max(1, int(max_concurrent_writes)))
        self._processing_event = processing_event
        self._processing_fault_lock = asyncio.Lock()
        self._processing_notification_lock = asyncio.Lock()
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._processing_task: asyncio.Task[None] | None = None
        self._add_outage_until: datetime | None = None
        self._processing_retry_at: datetime | None = None
        self._paused = False

    def replace_provider(self, provider: MemoryProviderPort) -> None:
        """Replace the provider while the runtime lifecycle fence is held."""

        self._provider = provider

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def add_claims_available(self) -> bool:
        """Keep a proven provider outage from retrying on every drain tick."""

        if self._add_outage_until is None:
            return True
        if self._current_time() < self._add_outage_until:
            return False
        self._add_outage_until = None
        return True

    async def recover(self, *, lease_owner: str) -> None:
        """Perform the one durable boot recovery pass before new claims."""

        await self._store_call(
            self._store.recover_after_boot,
            lease_owner=lease_owner,
            clock=self._current_time,
        )
        if self._attachment_store is not None:
            if self._attachment_admission_lock is None:
                await self._reconcile_attachments()
            else:
                async with self._attachment_admission_lock:
                    await self._reconcile_attachments()

    async def _reconcile_attachments(self) -> None:
        """Reconcile one DB reference snapshot inside the admission fence."""

        if self._attachment_store is None:
            return
        referenced, releasing = await self._store_call(
            self._store.attachment_bundle_sets
        )
        try:
            await self._store_call(
                self._attachment_store.reconcile,
                referenced,
                releasing,
            )
        except AttachmentPinError:
            logger.warning("Memory attachment reconciliation was deferred")
        else:
            for bundle_id in releasing:
                await self._store_call(
                    self._store.finalize_attachment_release,
                    bundle_id,
                )

    async def deliver(self, row: QueueRow, *, lease_owner: str) -> bool:
        """Deliver one claimed add under the exact canonical session lock."""

        key = row.provider_session_ref.serialize()
        submission = _ProviderSubmissionAttempt()
        try:
            async with self._session_lock(key):
                if await self._store_call(
                    self._store.return_claim_if_fenced,
                    row,
                    lease_owner=lease_owner,
                ):
                    return False
                if self._paused or not self._enabled():
                    await self._settle_failure(
                        row,
                        lease_owner=lease_owner,
                        outcome=SystemOutage(error="memory_disabled"),
                    )
                    return False
                return await self._deliver_locked(
                    row,
                    lease_owner=lease_owner,
                    submission=submission,
                )
        except asyncio.CancelledError:
            await self._return_unsubmitted_claim(
                row,
                lease_owner=lease_owner,
                submission=submission,
            )
            raise

    async def run_due(self, *, max_sessions: int = 8) -> int:
        """Schedule due sessions without waiting for long-running provider flushes."""

        self._prune_tasks()
        if self._paused or not self._enabled():
            return 0
        current_time = self._current_time()
        self._schedule_processing_actions()
        now = _iso(current_time)
        refs = await self._store_call(
            self._store.list_flush_candidates,
            now=now,
            limit=max_sessions,
        )
        scheduled = 0
        for ref in refs:
            if self._schedule(ref, force=False) is not None:
                scheduled += 1
        return scheduled

    async def final_flush(
        self,
        provider_session_ref: ProviderSessionRef,
        *,
        deadline_seconds: float = 5.0,
    ) -> bool:
        """Drain exact-session generations to an atomic quiescent point by deadline."""

        if self._paused or not self._enabled():
            return False
        task = self._schedule(provider_session_ref, force=True)
        if task is None:
            key = provider_session_ref.serialize()
            task = self._flush_tasks.get(key)
        deadline = (
            asyncio.get_running_loop().time()
            + _positive_timeout(deadline_seconds)
        )
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            if task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return False

            disposition = await self._store_call(
                self._store.final_flush_disposition,
                provider_session_ref,
            )
            if disposition == "complete":
                return True
            if self._paused or not self._enabled():
                return False

            key = provider_session_ref.serialize()
            active = self._flush_tasks.get(key)
            if active is not None and not active.done() and active is not task:
                task = active
                continue
            if disposition == "blocked" or not self.add_claims_available():
                return False

            task = self._schedule(provider_session_ref, force=True)
            if task is None:
                task = self._flush_tasks.get(key)
            if task is None:
                return False

    async def pause_and_wait(self, *, timeout_seconds: float = 5.0) -> bool:
        """Stop scheduling and wait a bounded time for current session operations."""

        self.pause()
        tasks = tuple(task for task in self._flush_tasks.values() if not task.done())
        processing_task = self._processing_task
        if processing_task is not None and not processing_task.done():
            processing_task.cancel()
            tasks = (*tasks, processing_task)
        if not tasks:
            return True
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(asyncio.shield(task) for task in tasks),
                    return_exceptions=True,
                ),
                timeout=_positive_timeout(timeout_seconds),
            )
        except asyncio.TimeoutError:
            return False
        for task, result in zip(tasks, results, strict=True):
            if task is processing_task and isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                raise result
        return True

    async def prepare_shutdown(self, *, timeout_seconds: float = 2.0) -> None:
        """Cancel bounded in-process work without initiating a provider write."""

        self.pause()
        tasks = tuple(task for task in self._flush_tasks.values() if not task.done())
        processing_task = self._processing_task
        if processing_task is not None and not processing_task.done():
            tasks = (*tasks, processing_task)
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_positive_timeout(timeout_seconds),
            )
        except asyncio.TimeoutError:
            return

    def _schedule(
        self,
        provider_session_ref: ProviderSessionRef,
        *,
        force: bool,
    ) -> asyncio.Task[None] | None:
        key = provider_session_ref.serialize()
        current = self._flush_tasks.get(key)
        if current is not None and not current.done():
            return None
        task = asyncio.create_task(
            self._run_session_flush(provider_session_ref, force=force),
            name=f"memory-flush-{provider_session_ref.session_id[-16:]}",
        )
        self._flush_tasks[key] = task

        def remove(completed: asyncio.Task[None]) -> None:
            if self._flush_tasks.get(key) is completed:
                self._flush_tasks.pop(key, None)

        task.add_done_callback(remove)
        return task

    def _schedule_processing_actions(self) -> None:
        current = self._processing_task
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._reconcile_processing_events(),
            name="memory-processing-actions",
        )
        self._processing_task = task

        def remove(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                logger.exception("Memory processing action execution failed")

        task.add_done_callback(remove)

    async def _run_session_flush(
        self,
        provider_session_ref: ProviderSessionRef,
        *,
        force: bool,
    ) -> None:
        key = provider_session_ref.serialize()
        async with self._session_lock(key):
            if (
                self._paused
                or not self._enabled()
                or not self.add_claims_available()
            ):
                return
            lease = await self._store_call(
                self._store.begin_flush_attempt,
                now=_iso(self._current_time()),
                provider_session_ref=provider_session_ref,
                force=force,
            )
            if lease is None:
                return
            while not self._paused and self._enabled():
                row = await self._claim_fenced_generation(
                    lease,
                    lease_owner=lease.fence_token,
                    now=_iso(self._current_time()),
                )
                if row is None:
                    break
                submission = _ProviderSubmissionAttempt()
                try:
                    delivered = await self._deliver_locked(
                        row,
                        lease_owner=lease.fence_token,
                        submission=submission,
                    )
                except asyncio.CancelledError:
                    await self._return_unsubmitted_claim(
                        row,
                        lease_owner=lease.fence_token,
                        submission=submission,
                    )
                    raise
                state = await self._store_call(
                    self._store.get_session_flush_state,
                    provider_session_ref,
                )
                if state is None or state.state != "due":
                    return
                if not delivered:
                    return
            if self._paused or not self._enabled():
                return
            result: FlushResult
            async with self._write_slots:
                submission = _ProviderSubmissionAttempt()
                try:
                    submitted_at = _iso(self._current_time())
                    if not await self._store_call(
                        self._store.begin_flush_submission,
                        lease,
                        now=submitted_at,
                    ):
                        return
                    try:
                        result = await asyncio.wait_for(
                            _submit_provider_write(
                                submission,
                                lambda: self._provider.flush(
                                    lease.provider_session_ref
                                ),
                            ),
                            timeout=self._flush_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        result = FlushUnknown(reason="timeout")
                    except MemoryProviderSystemFailure as failure:
                        result = (
                            FlushUnknown(reason="transport")
                            if failure.ambiguous
                            else FlushRetryable()
                        )
                    except MemoryProviderFailure as failure:
                        result = (
                            FlushUnknown(
                                reason=(
                                    "timeout"
                                    if failure.error == "memory_provider_timeout"
                                    else "transport"
                                )
                            )
                            if failure.ambiguous
                            else FlushRetryable()
                        )
                    except Exception:
                        result = FlushUnknown(reason="transport")
                except asyncio.CancelledError:
                    if submission.provider_entered:
                        await self._finalize_flush_outcome(
                            lease,
                            FlushUnknown(reason="transport"),
                        )
                    else:
                        await self._store_call(
                            self._store.return_unsubmitted_flush,
                            lease,
                            now=_iso(self._current_time()),
                        )
                    raise

            if isinstance(result, FlushRetryable):
                async with self._processing_fault_lock:
                    settled_at = self._current_time()
                    await self._store_call(
                        self._store.retry_unsubmitted_flush,
                        lease,
                        now=settled_at,
                    )
            else:
                await self._finalize_flush_outcome(lease, result)

    async def _deliver_locked(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        submission: _ProviderSubmissionAttempt,
    ) -> bool:
        if row.payload_text is None:
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=MessageFailure(error="memory_invalid_input", retryable=False),
            )
            return False
        attachment_payload = row.payload_attachments
        if (row.attachment_bundle_id is None) != (attachment_payload is None):
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=MessageFailure(
                    error="memory_store_unavailable",
                    retryable=False,
                ),
            )
            return False
        if row.attachment_bundle_id is None:
            attachments = ()
        else:
            assert attachment_payload is not None
            if self._attachment_store is None:
                await self._settle_failure(
                    row,
                    lease_owner=lease_owner,
                    outcome=SystemOutage(error="memory_store_unavailable"),
                )
                return False
            try:
                bundle = decode_pinned_bundle(
                    row.attachment_bundle_id,
                    attachment_payload,
                )
            except AttachmentPinError as failure:
                await self._settle_attachment_preflight_failure(
                    row,
                    lease_owner=lease_owner,
                    failure=failure,
                    retryable=False,
                    downgrade_allowed=True,
                )
                return False
            try:
                attachments = await asyncio.to_thread(
                    self._attachment_store.provider_attachments,
                    bundle,
                )
            except AttachmentPinError as failure:
                await self._settle_attachment_preflight_failure(
                    row,
                    lease_owner=lease_owner,
                    failure=failure,
                    retryable=True,
                    downgrade_allowed=not failure.retryable,
                )
                return False
        capture = ProviderCapture(
            session_ref=row.provider_session_ref,
            text=row.payload_text,
            provider_timestamp_ms=row.provider_timestamp_ms,
            attachments=attachments,
        )
        try:
            async with self._write_slots:
                try:
                    claim_is_current = await self._store_call(
                        self._store.claim_is_current,
                        row,
                        lease_owner=lease_owner,
                    )
                except Exception:
                    await self._return_unsubmitted_claim(
                        row,
                        lease_owner=lease_owner,
                        submission=submission,
                    )
                    return False
                if not claim_is_current:
                    return False
                ack = await asyncio.wait_for(
                    _submit_provider_write(
                        submission,
                        lambda: self._provider.add(capture),
                    ),
                    timeout=self._add_timeout_seconds,
                )
        except asyncio.TimeoutError:
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=AmbiguousAdd(error="memory_provider_timeout"),
            )
            return False
        except MemoryProviderSystemFailure as failure:
            outcome: AmbiguousAdd | SystemOutage = (
                AmbiguousAdd(error=_provider_error(failure, "memory_sidecar_unavailable"))
                if failure.ambiguous
                else SystemOutage(error=_provider_error(failure, "memory_sidecar_unavailable"))
            )
            await self._settle_failure(row, lease_owner=lease_owner, outcome=outcome)
            return False
        except MemoryProviderFailure as failure:
            error = _provider_error(failure, "memory_processing_failed")
            if failure.ambiguous:
                outcome = AmbiguousAdd(error=error)
            elif failure.retryable:
                outcome = SystemOutage(error=error)
            else:
                outcome = MessageFailure(error=error, retryable=False)
            await self._settle_failure(row, lease_owner=lease_owner, outcome=outcome)
            return False
        except Exception:
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=AmbiguousAdd(error="memory_provider_response_invalid"),
            )
            return False

        if isinstance(ack, AddRejected):
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=ack,
            )
            return False
        if (
            not isinstance(ack, AddAck)
            or ack.status not in {"accumulated", "extracted"}
            or not _valid_receipt(ack.request_id)
        ):
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=AmbiguousAdd(
                    add_request_id=ack.request_id if isinstance(ack, AddAck) else None,
                    error="memory_provider_response_invalid",
                ),
            )
            return False
        async with self._processing_notification_lock:
            settled = await self._store_call(
                self._store.settle_add_ack,
                row,
                ack,
                lease_owner=lease_owner,
                now=self._current_time(),
                idle_timeout=IDLE_FLUSH_TIMEOUT,
                max_unflushed_age=MAX_UNFLUSHED_AGE,
                message_bound=MAX_UNFLUSHED_MESSAGES,
            )
        if settled.attachment_release_id is not None:
            await self._release_bundle(settled.attachment_release_id)
        return settled.settled

    async def _finalize_flush_outcome(
        self,
        lease: FlushLease,
        result: FlushSucceeded | FlushRejected | FlushUnknown,
    ) -> None:
        _settled, cancellation = await self._drain_local_flush_outcome(
            lease,
            result,
        )
        if cancellation is not None:
            raise cancellation

    async def _drain_local_flush_outcome(
        self,
        lease: FlushLease,
        result: FlushSucceeded | FlushRejected | FlushUnknown,
    ) -> tuple[bool, asyncio.CancelledError | None]:
        """Finish local commits, deferring cancellation before remote classification."""

        local_phase = asyncio.create_task(
            self._persist_local_flush_outcome(lease, result)
        )
        cancellation: asyncio.CancelledError | None = None
        while not local_phase.done():
            try:
                await asyncio.shield(local_phase)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        return local_phase.result(), cancellation

    async def _persist_local_flush_outcome(
        self,
        lease: FlushLease,
        result: FlushSucceeded | FlushRejected | FlushUnknown,
    ) -> bool:
        """Persist the exact outcome and its fault edge under the same local phase."""

        async with self._processing_notification_lock:
            settled = await self._store_call(
                self._store.settle_flush,
                lease,
                result,
                now=_iso(self._current_time()),
            )
        return settled.settled

    async def _return_unsubmitted_claim(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        submission: _ProviderSubmissionAttempt,
    ) -> None:
        if submission.provider_entered:
            return
        await self._store_call(
            self._store.return_unsubmitted_claim,
            row,
            lease_owner=lease_owner,
        )

    async def _settle_attachment_preflight_failure(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        failure: AttachmentPinError,
        retryable: bool,
        downgrade_allowed: bool,
    ) -> None:
        bundle_id = None
        if downgrade_allowed:
            bundle_id = await self._store_call(
                self._store._downgrade_claimed_attachment_to_text,
                row,
                lease_owner=lease_owner,
                now=self._current_time(),
            )
        if bundle_id is None:
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=MessageFailure(
                    error=failure.error,
                    retryable=retryable,
                ),
            )
            return
        await self._release_bundle(bundle_id)

    async def _settle_failure(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        outcome: AddRejected | AmbiguousAdd | SystemOutage | MessageFailure,
    ) -> None:
        opens_processing_fault = isinstance(outcome, AmbiguousAdd) or (
            isinstance(outcome, AddRejected) and outcome.server_fault
        ) or (
            isinstance(outcome, SystemOutage)
            and outcome.error == "memory_processing_failed"
        )
        if opens_processing_fault:
            async with self._processing_fault_lock:
                settled_at = self._current_time()
                settled = await self._store_call(
                    self._store.settle,
                    row,
                    outcome,
                    lease_owner=lease_owner,
                    now=settled_at,
                )
                if settled.settled:
                    if isinstance(outcome, SystemOutage):
                        self._add_outage_until = self._current_time() + timedelta(
                            seconds=SYSTEM_OUTAGE_RETRY_SECONDS
                        )
            if settled.attachment_release_id is not None:
                await self._release_bundle(settled.attachment_release_id)
            return

        settled = await self._store_call(
            self._store.settle,
            row,
            outcome,
            lease_owner=lease_owner,
            now=self._current_time(),
        )
        if settled.attachment_release_id is not None:
            await self._release_bundle(settled.attachment_release_id)
        if settled.settled and isinstance(outcome, SystemOutage):
            self._add_outage_until = self._current_time() + timedelta(
                seconds=SYSTEM_OUTAGE_RETRY_SECONDS
            )

    async def _reconcile_processing_events(self) -> None:
        if self._processing_retry_at is not None:
            if self._current_time() < self._processing_retry_at:
                return
            self._processing_retry_at = None
        for _ in range(MAX_PROCESSING_ACTIONS_PER_PASS):
            async with self._processing_fault_lock:
                action = await self._store_call(self._store.next_processing_action)
                if action is None:
                    return
                if isinstance(action, ProcessingNotification):
                    async with self._processing_notification_lock:
                        current = await self._store_call(
                            self._store.next_processing_action
                        )
                        if current != action:
                            return
                        if not await self._emit_processing_event(
                            action.event,
                            action.kind,
                            action.occurred_at,
                        ):
                            self._defer_processing_retry()
                            return
                        acknowledged = await self._store_call(
                            self._store.acknowledge_processing_notification,
                            action,
                        )
                        if not acknowledged:
                            return
                    continue
            if not isinstance(action, ProcessingHealthProbe):
                return
            try:
                healthy = bool(await self._provider.processing_healthy())
            except Exception:
                self._defer_processing_retry()
                return
            async with self._processing_fault_lock:
                committed = await self._store_call(
                    self._store.record_processing_health,
                    action,
                    healthy=healthy,
                )
                if not committed.committed:
                    return

    def _defer_processing_retry(self) -> None:
        self._processing_retry_at = self._current_time() + timedelta(
            seconds=PROCESSING_ACTION_RETRY_SECONDS
        )

    async def _emit_processing_event(
        self,
        event: Literal["fault", "recovered"],
        kind: ProcessingFaultKind | None,
        occurred_at: str,
    ) -> bool:
        if self._processing_event is None:
            return True
        try:
            stats = await self._store_call(self._store.queue_stats)
            return bool(
                await self._processing_event(
                    event,
                    kind,
                    occurred_at,
                    stats.pending + stats.processing,
                )
            )
        except Exception:
            return False

    async def _release_bundle(self, bundle_id: str) -> None:
        try:
            if self._attachment_store is not None:
                await asyncio.to_thread(self._attachment_store.release, bundle_id)
                await self._store_call(
                    self._store.finalize_attachment_release,
                    bundle_id,
                )
            if self._release_attachment is not None:
                result = self._release_attachment(bundle_id)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            # The durable `releasing` row makes cleanup a boot-reconcilable task.
            return

    def _session_lock(self, key: str) -> asyncio.Lock:
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    async def _claim_fenced_generation(
        self,
        lease: FlushLease,
        *,
        lease_owner: str,
        now: str,
    ) -> QueueRow | None:
        task = asyncio.create_task(
            asyncio.to_thread(
                self._store.claim_fenced_generation,
                lease,
                lease_owner=lease_owner,
                now=now,
            )
        )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        try:
            row = task.result()
        except Exception:
            if cancellation is not None:
                raise cancellation
            raise
        if cancellation is None:
            return row
        if row is not None:
            try:
                await self._store_call(
                    self._store.return_unsubmitted_claim,
                    row,
                    lease_owner=lease_owner,
                )
            except asyncio.CancelledError as error:
                cancellation = error
        raise cancellation

    def _prune_tasks(self) -> None:
        for key, task in tuple(self._flush_tasks.items()):
            if task.done():
                self._flush_tasks.pop(key, None)
        if self._processing_task is not None and self._processing_task.done():
            self._processing_task = None

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _store_call(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await run_blocking(operation, *args, **kwargs)


def _positive_timeout(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1.0
    return parsed if parsed > 0 else 1.0


async def _submit_provider_write(
    submission: _ProviderSubmissionAttempt,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Mark the exact point where a provider coroutine may begin executing."""

    submission.provider_entered = True
    return await operation()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _provider_error(
    failure: MemoryProviderFailure,
    fallback: MemoryErrorCode,
) -> MemoryErrorCode:
    return failure.error if is_memory_error_code(failure.error) else fallback


def _valid_receipt(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 128
