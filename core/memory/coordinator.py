"""Durable per-session add serialization and flush coordination."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from core.memory.attachments import (
    AttachmentPinError,
    AttachmentPinStore,
    decode_pinned_bundle,
)
from core.memory.everos import (
    AddAck,
    FlushResult,
    FlushRetryable,
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
    QueueRow,
    SystemOutage,
)
from core.memory.types import MemoryErrorCode, ProviderSessionRef, is_memory_error_code


IDLE_FLUSH_TIMEOUT = timedelta(minutes=5)
MAX_UNFLUSHED_AGE = timedelta(minutes=30)
MAX_UNFLUSHED_MESSAGES = 100
ADD_TIMEOUT_SECONDS = 30.0
FLUSH_TIMEOUT_SECONDS = 300.0
MAX_CONCURRENT_PROVIDER_WRITES = 4

AttachmentRelease = Callable[[str], Awaitable[None] | None]


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
        add_timeout_seconds: float = ADD_TIMEOUT_SECONDS,
        flush_timeout_seconds: float = FLUSH_TIMEOUT_SECONDS,
        max_concurrent_writes: int = MAX_CONCURRENT_PROVIDER_WRITES,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._release_attachment = release_attachment
        self._attachment_store = attachment_store
        self._add_timeout_seconds = _positive_timeout(add_timeout_seconds)
        self._flush_timeout_seconds = _positive_timeout(flush_timeout_seconds)
        self._write_slots = asyncio.Semaphore(max(1, int(max_concurrent_writes)))
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._paused = False

    def replace_provider(self, provider: MemoryProviderPort) -> None:
        """Replace the provider while the runtime lifecycle fence is held."""

        self._provider = provider

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def recover(self, *, lease_owner: str) -> None:
        """Perform the one durable boot recovery pass before new claims."""

        await self._store_call(
            self._store.recover_after_boot,
            lease_owner=lease_owner,
            clock=self._current_time,
        )
        if self._attachment_store is not None:
            referenced, releasing = await self._store_call(
                self._store.attachment_bundle_sets
            )
            await asyncio.to_thread(
                self._attachment_store.reconcile,
                referenced,
                releasing,
            )
            for bundle_id in releasing:
                await self._store_call(
                    self._store.finalize_attachment_release,
                    bundle_id,
                )

    async def deliver(self, row: QueueRow, *, lease_owner: str) -> bool:
        """Deliver one claimed add under the exact canonical session lock."""

        key = row.provider_session_ref.serialize()
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
            return await self._deliver_locked(row, lease_owner=lease_owner)

    async def run_due(self, *, max_sessions: int = 8) -> int:
        """Schedule due sessions without waiting for long-running provider flushes."""

        self._prune_tasks()
        if self._paused or not self._enabled():
            return 0
        now = _iso(self._current_time())
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
        """Fence and join a trusted explicit final flush for a bounded deadline."""

        if self._paused or not self._enabled():
            return False
        task = self._schedule(provider_session_ref, force=True)
        if task is None:
            key = provider_session_ref.serialize()
            task = self._flush_tasks.get(key)
        if task is None:
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_positive_timeout(deadline_seconds),
            )
        except asyncio.TimeoutError:
            return False
        state = await self._store_call(
            self._store.get_session_flush_state,
            provider_session_ref,
        )
        return state is None or state.state == "idle"

    async def pause_and_wait(self, *, timeout_seconds: float = 5.0) -> bool:
        """Stop scheduling and wait a bounded time for current session operations."""

        self.pause()
        tasks = tuple(task for task in self._flush_tasks.values() if not task.done())
        if not tasks:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in tasks)),
                timeout=_positive_timeout(timeout_seconds),
            )
        except asyncio.TimeoutError:
            return False
        return True

    async def prepare_shutdown(self, *, timeout_seconds: float = 2.0) -> None:
        """Cancel bounded in-process work without initiating a provider write."""

        self.pause()
        tasks = tuple(task for task in self._flush_tasks.values() if not task.done())
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

    async def _run_session_flush(
        self,
        provider_session_ref: ProviderSessionRef,
        *,
        force: bool,
    ) -> None:
        key = provider_session_ref.serialize()
        async with self._session_lock(key):
            if self._paused or not self._enabled():
                return
            lease = await self._store_call(
                self._store.acquire_flush,
                now=_iso(self._current_time()),
                provider_session_ref=provider_session_ref,
                force=force,
            )
            if lease is None:
                return
            await self._store_call(self._store.reclaim_fenced_generation_claims, lease)
            while not self._paused and self._enabled():
                row = await self._store_call(
                    self._store.claim_fenced_generation,
                    lease,
                    lease_owner=lease.fence_token,
                    now=_iso(self._current_time()),
                )
                if row is None:
                    break
                delivered = await self._deliver_locked(
                    row,
                    lease_owner=lease.fence_token,
                )
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
            pending, processing = await self._store_call(
                self._store.target_generation_counts,
                lease,
            )
            if pending or processing:
                return
            submitted_at = _iso(self._current_time())
            if not await self._store_call(
                self._store.mark_flush_submission_started,
                lease,
                now=submitted_at,
            ):
                return
            result: FlushResult
            try:
                async with self._write_slots:
                    result = await asyncio.wait_for(
                        self._provider.flush(lease.provider_session_ref),
                        timeout=self._flush_timeout_seconds,
                    )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._store_call(
                        self._store.settle_flush,
                        lease,
                        FlushUnknown(reason="transport"),
                        now=_iso(self._current_time()),
                    )
                )
                raise
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

            if isinstance(result, FlushRetryable):
                await self._store_call(
                    self._store.retry_unsubmitted_flush,
                    lease,
                    now=self._current_time(),
                )
            else:
                await self._store_call(
                    self._store.settle_flush,
                    lease,
                    result,
                    now=_iso(self._current_time()),
                )

    async def _deliver_locked(self, row: QueueRow, *, lease_owner: str) -> bool:
        if row.payload_text is None:
            await self._settle_failure(
                row,
                lease_owner=lease_owner,
                outcome=MessageFailure(error="memory_invalid_input", retryable=False),
            )
            return False
        if row.attachment_bundle_id is None:
            if row.payload_attachments is not None:
                await self._settle_failure(
                    row,
                    lease_owner=lease_owner,
                    outcome=AmbiguousAdd(error="memory_store_unavailable"),
                )
                return False
            attachments = ()
        else:
            if self._attachment_store is None or row.payload_attachments is None:
                await self._settle_failure(
                    row,
                    lease_owner=lease_owner,
                    outcome=AmbiguousAdd(error="memory_store_unavailable"),
                )
                return False
            try:
                bundle = decode_pinned_bundle(
                    row.attachment_bundle_id,
                    row.payload_attachments,
                )
                attachments = await asyncio.to_thread(
                    self._attachment_store.provider_attachments,
                    bundle,
                )
            except AttachmentPinError:
                await self._settle_failure(
                    row,
                    lease_owner=lease_owner,
                    outcome=AmbiguousAdd(error="memory_store_unavailable"),
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
                ack = await asyncio.wait_for(
                    self._provider.add(capture),
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

    async def _settle_failure(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        outcome: AmbiguousAdd | SystemOutage | MessageFailure,
    ) -> None:
        settled = await self._store_call(
            self._store.settle,
            row,
            outcome,
            lease_owner=lease_owner,
            now=self._current_time(),
        )
        if settled.attachment_release_id is not None:
            await self._release_bundle(settled.attachment_release_id)

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

    def _prune_tasks(self) -> None:
        for key, task in tuple(self._flush_tasks.items()):
            if task.done():
                self._flush_tasks.pop(key, None)

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _store_call(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        if cancellation is not None:
            try:
                task.result()
            except (Exception, asyncio.CancelledError):
                pass
            raise cancellation
        return task.result()


def _positive_timeout(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1.0
    return parsed if parsed > 0 else 1.0


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _provider_error(
    failure: MemoryProviderFailure,
    fallback: MemoryErrorCode,
) -> MemoryErrorCode:
    return failure.error if is_memory_error_code(failure.error) else fallback


def _valid_receipt(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 128
