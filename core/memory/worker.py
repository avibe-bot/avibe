"""Bounded, health-gated queue draining for the Memory module."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.memory.everos import (
    MemoryProviderFailure,
    MemoryProviderMessageFailure,
    MemoryProviderPort,
    MemoryProviderSystemFailure,
    ProviderCapture,
)
from core.memory.store import MemoryStore, QueueRow
from core.memory.types import MemoryErrorCode, is_memory_error_code


MAX_DRAIN_BATCH_SIZE = 32
PROVIDER_HEALTH_TIMEOUT_SECONDS = 5.0
PROVIDER_INGEST_TIMEOUT_SECONDS = 20.0
SYSTEM_PAUSE_SECONDS = 5.0


class MemoryWorker:
    """Drain one local queue with at-least-once delivery and fenced completion."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        provider: MemoryProviderPort,
        enabled: Callable[[], bool],
        boot_id: str | None = None,
        now: Callable[[], datetime] | None = None,
        ingest_timeout_seconds: float = PROVIDER_INGEST_TIMEOUT_SECONDS,
        health_timeout_seconds: float = PROVIDER_HEALTH_TIMEOUT_SECONDS,
        system_pause_seconds: float = SYSTEM_PAUSE_SECONDS,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._boot_id = boot_id or uuid.uuid4().hex
        self._now = now or (lambda: datetime.now(UTC))
        self._ingest_timeout_seconds = _positive_timeout(ingest_timeout_seconds)
        self._health_timeout_seconds = _positive_timeout(health_timeout_seconds)
        self._system_pause_seconds = max(float(system_pause_seconds), 0.0)
        self._drain_lock = asyncio.Lock()
        self._claims_paused = False
        self._system_paused = False
        self._system_pause_until: datetime | None = None
        self._recovered = False

    def pause_claims(self) -> None:
        """Prevent future claims while allowing a current provider call to finish."""

        self._claims_paused = True

    def resume_claims(self) -> None:
        """Allow claims again after a completed lifecycle operation."""

        self._claims_paused = False

    async def pause_and_wait(
        self,
        *,
        timeout_seconds: float = PROVIDER_INGEST_TIMEOUT_SECONDS,
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
        """Drain a bounded batch, pausing new claims after infrastructure failures."""

        budget = min(max(int(max_rows), 0), MAX_DRAIN_BATCH_SIZE)
        if budget == 0:
            return 0

        async with self._drain_lock:
            if not self._recovered:
                await self._store_call(self._store.reclaim_processing, lease_owner=self._boot_id)
                self._recovered = True
            if self._claims_paused or not self._enabled():
                return 0
            if not await self._health_gate_allows_claims():
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
                meta = await self._store_call(self._store.get_meta)
                if meta is None or meta.epoch != row.epoch or row.payload_text is None:
                    await self._return_system_failure(row, "memory_processing_failed")
                    break

                capture = ProviderCapture(
                    principal_id=meta.principal_id,
                    session_ref=row.session_id,
                    text=row.payload_text,
                    provider_timestamp_ms=row.provider_timestamp_ms,
                )
                try:
                    await asyncio.wait_for(
                        self._provider.ingest(capture),
                        timeout=self._ingest_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    if await self._ambiguous_failure_is_system_outage(
                        row,
                        "memory_provider_timeout",
                    ):
                        break
                    continue
                except MemoryProviderSystemFailure as failure:
                    await self._return_system_failure(
                        row,
                        _provider_error_code(failure, "memory_sidecar_unavailable"),
                    )
                    break
                except MemoryProviderMessageFailure as failure:
                    await self._record_message_failure(
                        row,
                        _provider_error_code(failure, "memory_processing_failed"),
                        failure.retryable,
                    )
                    continue
                except MemoryProviderFailure as failure:
                    if await self._ambiguous_failure_is_system_outage(
                        row,
                        _provider_error_code(failure, "memory_processing_failed"),
                        retryable=failure.retryable,
                    ):
                        break
                    continue
                except Exception:
                    if await self._ambiguous_failure_is_system_outage(
                        row,
                        "memory_processing_failed",
                    ):
                        break
                    continue

                await self._store_call(
                    self._store.mark_delivered,
                    row,
                    lease_owner=self._boot_id,
                    now=_iso_from_datetime(self._current_time()),
                )
            return processed

    async def drain_once(self) -> int:
        """Run one bounded drain tick for focused lifecycle tests."""

        return await self.drain(max_rows=1)

    async def _health_gate_allows_claims(self) -> bool:
        now = self._current_time()
        if self._system_paused:
            if self._system_pause_until is not None and now < self._system_pause_until:
                return False
            if not await self._provider_healthy():
                self._pause_for_system_failure(now)
                await self._store_call(self._store.set_last_error, "memory_sidecar_unavailable")
                return False
            self._system_paused = False
            self._system_pause_until = None
            await self._store_call(self._store.clear_system_outage_error)
            return True

        if not await self._provider_healthy():
            self._pause_for_system_failure(now)
            await self._store_call(self._store.set_last_error, "memory_sidecar_unavailable")
            return False
        return True

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
        """Probe health once before spending a row's message-failure budget."""

        if not await self._provider_healthy():
            await self._return_system_failure(row, "memory_sidecar_unavailable")
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

    async def _store_call(self, method: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    def _pause_for_system_failure(self, now: datetime) -> None:
        self._system_paused = True
        self._system_pause_until = now + timedelta(seconds=self._system_pause_seconds)

    def _current_time(self) -> datetime:
        return self._now().astimezone(UTC)


def _provider_error_code(error: MemoryProviderFailure, fallback: MemoryErrorCode) -> MemoryErrorCode:
    return error.error if is_memory_error_code(error.error) else fallback


def _positive_timeout(value: float) -> float:
    try:
        return max(float(value), 0.001)
    except (TypeError, ValueError):
        return 0.001


def _iso_from_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
