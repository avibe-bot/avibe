"""Bounded, health-gated queue draining for the Memory module."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.memory.everos import (
    MemoryProviderFailure,
    MemoryProviderMessageFailure,
    MemoryProviderPort,
    MemoryProviderSystemFailure,
    ProviderCapture,
)
from core.memory.store import MemoryStore
from core.memory.types import MemoryErrorCode


MAX_DRAIN_BATCH_SIZE = 32
PROVIDER_HEALTH_TIMEOUT_SECONDS = 5.0


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
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._boot_id = boot_id or uuid.uuid4().hex
        self._now = now or (lambda: datetime.now(UTC))
        self._drain_lock = asyncio.Lock()
        self._claims_paused = False
        self._recovered = False

    def pause_claims(self) -> None:
        """Prevent future claims while allowing a current provider call to finish."""

        self._claims_paused = True

    def resume_claims(self) -> None:
        """Allow claims again after a completed lifecycle operation."""

        self._claims_paused = False

    async def pause_and_wait(self) -> None:
        """Fence future claims and wait for the current bounded drain to become idle."""

        self.pause_claims()
        async with self._drain_lock:
            return

    async def drain(self, *, max_rows: int = MAX_DRAIN_BATCH_SIZE) -> int:
        """Drain at most ``max_rows`` due rows, stopping immediately on a system outage."""

        budget = min(max(int(max_rows), 0), MAX_DRAIN_BATCH_SIZE)
        if budget == 0:
            return 0

        async with self._drain_lock:
            if not self._recovered:
                await self._store_call(self._store.reclaim_processing, lease_owner=self._boot_id)
                self._recovered = True
            if self._claims_paused or not self._enabled():
                return 0
            if not await self._provider_healthy():
                await self._store_call(self._store.set_last_error, "memory_sidecar_unavailable")
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
                    await self._store_call(
                        self._store.return_system_failure,
                        row,
                        lease_owner=self._boot_id,
                        error="memory_processing_failed",
                        now=_iso_from_datetime(now),
                    )
                    break

                capture = ProviderCapture(
                    principal_id=meta.principal_id,
                    session_ref=row.session_id,
                    text=row.payload_text,
                    provider_timestamp_ms=row.provider_timestamp_ms,
                )
                try:
                    await self._provider.ingest(capture)
                except MemoryProviderSystemFailure as failure:
                    await self._return_system_failure(row, failure.error)
                    break
                except MemoryProviderMessageFailure as failure:
                    await self._record_message_failure(row, failure.error, failure.retryable)
                    continue
                except MemoryProviderFailure as failure:
                    if not await self._provider_healthy():
                        await self._return_system_failure(row, failure.error)
                        break
                    await self._record_message_failure(row, failure.error, failure.retryable)
                    continue
                except Exception:
                    if not await self._provider_healthy():
                        await self._return_system_failure(row, "memory_sidecar_unavailable")
                        break
                    await self._record_message_failure(row, "memory_processing_failed", True)
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

    async def _return_system_failure(self, row, error: MemoryErrorCode) -> None:
        await self._store_call(
            self._store.return_system_failure,
            row,
            lease_owner=self._boot_id,
            error=error,
            now=_iso_from_datetime(self._current_time()),
        )

    async def _record_message_failure(
        self,
        row,
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
                    timeout=PROVIDER_HEALTH_TIMEOUT_SECONDS,
                )
            )
        except Exception:
            return False

    async def _store_call(self, method: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    def _current_time(self) -> datetime:
        return self._now().astimezone(UTC)


def _iso_from_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
