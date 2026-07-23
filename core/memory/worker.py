"""Bounded, health-gated queue draining for the Memory module."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from core.memory.everos import (
    AddAck,
    FlushRejected,
    FlushResult,
    FlushSucceeded,
    FlushUnknown,
    MemoryProviderFailure,
    MemoryProviderPort,
    MemoryProviderSystemFailure,
    ProviderCapture,
)
from core.memory.store import MemoryStore, QueueRow
from core.memory.types import MemoryErrorCode, is_memory_error_code


MAX_DRAIN_BATCH_SIZE = 32
PROVIDER_HEALTH_TIMEOUT_SECONDS = 5.0
ADD_TIMEOUT_SECONDS = 30.0
FLUSH_TIMEOUT_SECONDS = 300.0
SYSTEM_PAUSE_SECONDS = 5.0
BREAKER_RETRY_SECONDS = 5 * 60.0

ProcessingFaultKind = Literal["credential", "engine"]
ProcessingEvent = Callable[
    [Literal["fault", "recovered"], ProcessingFaultKind | None, str, int],
    Awaitable[bool],
]


class MemoryWorker:
    """Drain one local queue with delivery fencing and provider observations."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        provider: MemoryProviderPort,
        enabled: Callable[[], bool],
        boot_id: str | None = None,
        now: Callable[[], datetime] | None = None,
        ingest_timeout_seconds: float = ADD_TIMEOUT_SECONDS,
        flush_timeout_seconds: float = FLUSH_TIMEOUT_SECONDS,
        health_timeout_seconds: float = PROVIDER_HEALTH_TIMEOUT_SECONDS,
        system_pause_seconds: float = SYSTEM_PAUSE_SECONDS,
        breaker_retry_seconds: float = BREAKER_RETRY_SECONDS,
        processing_event: ProcessingEvent | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._boot_id = boot_id or uuid.uuid4().hex
        self._now = now or (lambda: datetime.now(UTC))
        self._add_timeout_seconds = _positive_timeout(ingest_timeout_seconds)
        self._flush_timeout_seconds = _positive_timeout(flush_timeout_seconds)
        self._health_timeout_seconds = _positive_timeout(health_timeout_seconds)
        self._system_pause_seconds = max(float(system_pause_seconds), 0.0)
        self._breaker_retry_seconds = max(float(breaker_retry_seconds), 0.0)
        self._processing_event = processing_event
        self._drain_lock = asyncio.Lock()
        self._claims_paused = False
        self._system_paused = False
        self._system_pause_until: datetime | None = None
        self._activation_pending = True
        self._recovery_sessions: list[str] = []

    def begin_activation(self) -> None:
        """Require durable recovery before a recreated drain task can claim."""

        self._activation_pending = True
        self._recovery_sessions = []

    def pause_claims(self) -> None:
        """Prevent future claims while allowing a current provider call to finish."""

        self._claims_paused = True

    def resume_claims(self) -> None:
        """Allow claims again after a completed lifecycle operation."""

        self._claims_paused = False

    async def pause_and_wait(
        self,
        *,
        timeout_seconds: float = ADD_TIMEOUT_SECONDS,
    ) -> bool:
        """Fence claims and wait only a bounded time for a current drain tick."""

        self.pause_claims()
        try:
            await asyncio.wait_for(
                self._drain_lock.acquire(),
                timeout=_positive_timeout(timeout_seconds),
            )
        except asyncio.TimeoutError:
            return False
        self._drain_lock.release()
        return True

    async def drain(self, *, max_rows: int = MAX_DRAIN_BATCH_SIZE) -> int:
        """Drain a bounded batch, stopping when infrastructure becomes unsafe."""

        budget = min(max(int(max_rows), 0), MAX_DRAIN_BATCH_SIZE)
        if budget == 0:
            return 0

        async with self._drain_lock:
            if self._activation_pending:
                await self._recover_activation()
            if self._claims_paused or not self._enabled():
                return 0

            half_open = await self._health_gate_allows_claims()
            if half_open is None:
                return 0
            if self._recovery_sessions:
                recovered = await self._drain_recovery_sessions(half_open=half_open)
                if not recovered or half_open:
                    return 0
                half_open = await self._health_gate_allows_claims()
                if half_open is None:
                    return 0

            processed = 0
            for _ in range(budget):
                if self._claims_paused or not self._enabled():
                    break
                now = self._current_time()
                row = await self._store_call(
                    self._store.claim_due,
                    lease_owner=self._boot_id,
                    now=_iso_from_datetime(now),
                )
                if row is None:
                    break
                processed += 1
                delivered = await self._deliver_row(row)
                if not delivered:
                    if half_open:
                        await self._reopen_processing_fault()
                        break
                    if self._system_paused:
                        break
                    continue

                result = await self._flush_session(row.session_id)
                if _opens_breaker(result):
                    await self._open_processing_fault()
                    break
                if half_open:
                    if isinstance(result, FlushSucceeded):
                        await self._close_processing_fault()
                    elif _opens_breaker(result):
                        await self._reopen_processing_fault()
                    break
            return processed

    async def drain_once(self) -> int:
        """Run one bounded drain tick for focused lifecycle tests."""

        return await self.drain(max_rows=1)

    async def _recover_activation(self) -> None:
        await self._store_call(self._store.reclaim_processing, lease_owner=self._boot_id)
        now = _iso_from_datetime(self._current_time())
        interrupted = await self._store_call(self._store.recover_in_flight_flushes, now=now)
        self._recovery_sessions = list(await self._store_call(self._store.list_not_attempted_sessions))
        self._activation_pending = False
        if interrupted:
            await self._open_processing_fault()
            return
        meta = await self._store_call(self._store.get_meta)
        if (
            meta is not None
            and meta.processing_fault_since is not None
            and (meta.processing_fault_kind is None or not meta.processing_alert_active)
        ):
            await self._classify_processing_fault(meta.processing_fault_since)

    async def _drain_recovery_sessions(self, *, half_open: bool) -> bool:
        while self._recovery_sessions:
            session_id = self._recovery_sessions[0]
            result = await self._flush_session(session_id)
            self._recovery_sessions.pop(0)
            if _opens_breaker(result):
                await self._open_processing_fault()
                return False
            if half_open:
                if isinstance(result, FlushSucceeded):
                    await self._close_processing_fault()
                    return True
                if _opens_breaker(result):
                    await self._reopen_processing_fault()
                return False
        return True

    async def _deliver_row(self, row: QueueRow) -> bool:
        meta = await self._store_call(self._store.get_meta)
        if meta is None or meta.epoch != row.epoch or row.payload_text is None:
            await self._return_system_failure(row, "memory_processing_failed")
            return False

        capture = ProviderCapture(
            principal_id=meta.principal_id,
            session_ref=row.session_id,
            text=row.payload_text,
            provider_timestamp_ms=row.provider_timestamp_ms,
        )
        try:
            ack = await asyncio.wait_for(
                self._provider.add(capture),
                timeout=self._add_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._ambiguous_failure_is_system_outage(
                row,
                "memory_provider_timeout",
            )
            return False
        except MemoryProviderSystemFailure as failure:
            await self._return_system_failure(
                row,
                _provider_error_code(failure, "memory_sidecar_unavailable"),
            )
            return False
        except MemoryProviderFailure as failure:
            await self._ambiguous_failure_is_system_outage(
                row,
                _provider_error_code(failure, "memory_processing_failed"),
                retryable=failure.retryable,
            )
            return False
        except Exception:
            await self._ambiguous_failure_is_system_outage(row, "memory_processing_failed")
            return False

        if not isinstance(ack, AddAck):
            ack = AddAck(request_id=None, status=None)
        return bool(
            await self._store_call(
                self._store.mark_delivered,
                row,
                lease_owner=self._boot_id,
                now=_iso_from_datetime(self._current_time()),
                add_request_id=ack.request_id,
            )
        )

    async def _flush_session(self, session_id: str) -> FlushResult:
        marked = await self._store_call(self._store.mark_flush_in_flight, session_id)
        if not marked:
            return FlushUnknown(reason="transport")
        try:
            result = await asyncio.wait_for(
                self._provider.flush(session_id),
                timeout=self._flush_timeout_seconds,
            )
        except asyncio.TimeoutError:
            result = FlushUnknown(reason="timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            result = FlushUnknown(reason="transport")
        if not isinstance(result, (FlushSucceeded, FlushRejected, FlushUnknown)):
            result = FlushUnknown(reason="transport")
        await self._store_call(
            self._store.record_flush_verdict,
            session_id,
            result,
            now=_iso_from_datetime(self._current_time()),
        )
        return result

    async def _health_gate_allows_claims(self) -> bool | None:
        now = self._current_time()
        if self._system_paused:
            if self._system_pause_until is not None and now < self._system_pause_until:
                return None
            if not await self._provider_healthy():
                self._pause_for_system_failure(now)
                await self._store_call(self._store.set_last_error, "memory_sidecar_unavailable")
                return None
            if not await self._provider_processing_healthy():
                self._pause_for_system_failure(now)
                await self._store_call(self._store.set_last_error, "memory_processing_failed")
                return None
            self._system_paused = False
            self._system_pause_until = None
            await self._store_call(self._store.clear_system_outage_error)

        meta = await self._store_call(self._store.get_meta)
        if meta is not None and meta.processing_fault_since is not None:
            opened_at = _datetime_from_iso(meta.processing_fault_since)
            if opened_at is None or (now - opened_at).total_seconds() < self._breaker_retry_seconds:
                return None
            sidecar_healthy = await self._provider_healthy()
            processing_healthy = sidecar_healthy and await self._provider_processing_healthy()
            if not processing_healthy:
                await self._reopen_processing_fault()
                return None
            return True

        if not await self._provider_healthy():
            self._pause_for_system_failure(now)
            await self._store_call(self._store.set_last_error, "memory_sidecar_unavailable")
            return None
        return False

    async def _open_processing_fault(self) -> None:
        now = _iso_from_datetime(self._current_time())
        await self._store_call(self._store.open_processing_fault, now=now)
        await self._classify_processing_fault(now)

    async def _classify_processing_fault(self, occurred_at: str) -> None:
        processing_healthy = await self._provider_processing_healthy()
        kind: ProcessingFaultKind = "engine" if processing_healthy else "credential"
        should_alert = await self._store_call(self._store.classify_processing_fault, kind)
        if should_alert and await self._emit_processing_event("fault", kind, occurred_at):
            await self._store_call(self._store.mark_processing_alert_active)

    async def _reopen_processing_fault(self) -> None:
        now = _iso_from_datetime(self._current_time())
        await self._store_call(self._store.open_processing_fault, now=now)
        kind: ProcessingFaultKind = "engine" if await self._provider_processing_healthy() else "credential"
        await self._store_call(self._store.classify_processing_fault, kind)

    async def _close_processing_fault(self) -> None:
        now = _iso_from_datetime(self._current_time())
        if await self._store_call(self._store.close_processing_fault, now=now):
            await self._emit_processing_event("recovered", None, now)

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

    async def _return_system_failure(self, row: QueueRow, error: MemoryErrorCode) -> None:
        self._pause_for_system_failure(self._current_time())
        await self._store_call(
            self._store.return_system_failure,
            row,
            lease_owner=self._boot_id,
            error=error,
            now=_iso_from_datetime(self._current_time()),
        )

    async def _ambiguous_failure_is_system_outage(
        self,
        row: QueueRow,
        error: MemoryErrorCode,
        *,
        retryable: bool = True,
    ) -> bool:
        if not await self._provider_healthy():
            await self._return_system_failure(row, "memory_sidecar_unavailable")
            return True
        if not await self._provider_processing_healthy():
            await self._return_system_failure(row, "memory_processing_failed")
            return True
        await self._record_message_failure(row, error, retryable)
        return False

    async def _record_message_failure(
        self,
        row: QueueRow,
        error: MemoryErrorCode,
        retryable: bool,
    ) -> None:
        await self._store_call(
            self._store.record_message_failure,
            row,
            lease_owner=self._boot_id,
            error=error,
            retryable=retryable,
            now=self._current_time(),
        )

    async def _provider_healthy(self) -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    self._provider.health(),
                    timeout=self._health_timeout_seconds,
                )
            )
        except Exception:
            return False

    async def _provider_processing_healthy(self) -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    self._provider.processing_healthy(),
                    timeout=self._health_timeout_seconds,
                )
            )
        except Exception:
            return False

    async def _store_call(self, method: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    def _pause_for_system_failure(self, now: datetime) -> None:
        self._system_paused = True
        self._system_pause_until = now + timedelta(seconds=self._system_pause_seconds)

    def _current_time(self) -> datetime:
        return self._now().astimezone(UTC)


def _opens_breaker(result: FlushResult) -> bool:
    return isinstance(result, FlushUnknown) or (
        isinstance(result, FlushRejected) and result.server_fault
    )


def _provider_error_code(error: MemoryProviderFailure, fallback: MemoryErrorCode) -> MemoryErrorCode:
    return error.error if is_memory_error_code(error.error) else fallback


def _positive_timeout(value: float) -> float:
    try:
        return max(float(value), 0.001)
    except (TypeError, ValueError):
        return 0.001


def _iso_from_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime_from_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None
