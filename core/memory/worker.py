"""Bounded capture-outbox delivery for the Memory module."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from core.memory.attachments import AttachmentPinStore
from core.memory.blocking import run_blocking
from core.memory.coordinator import ProcessingEvent, SessionFlushCoordinator
from core.memory.everos import MemoryProviderPort
from core.memory.store import MemoryStore, QueueRow


MAX_DRAIN_BATCH_SIZE = 32
ADD_TIMEOUT_SECONDS = 30.0

class MemoryWorker:
    """Claim add rows; all session ordering and flush state live in the coordinator."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        provider: MemoryProviderPort,
        enabled: Callable[[], bool],
        boot_id: str | None = None,
        now: Callable[[], datetime] | None = None,
        ingest_timeout_seconds: float = ADD_TIMEOUT_SECONDS,
        processing_event: ProcessingEvent | None = None,
        coordinator: SessionFlushCoordinator | None = None,
        attachment_store: AttachmentPinStore | None = None,
        attachment_admission_lock: asyncio.Lock | None = None,
        **_legacy_options: object,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled = enabled
        self._boot_id = boot_id or uuid.uuid4().hex
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._coordinator = coordinator or SessionFlushCoordinator(
            store=store,
            provider=provider,
            enabled=enabled,
            now=self._now,
            add_timeout_seconds=ingest_timeout_seconds,
            attachment_store=attachment_store,
            attachment_admission_lock=attachment_admission_lock,
            processing_event=processing_event,
        )
        self._drain_lock = asyncio.Lock()
        self._claims_paused = False
        self._activation_pending = True

    @property
    def coordinator(self) -> SessionFlushCoordinator:
        return self._coordinator

    def replace_provider(self, provider: MemoryProviderPort) -> None:
        self._provider = provider
        self._coordinator.replace_provider(provider)

    def begin_activation(self) -> None:
        self._activation_pending = True

    def begin_new_lease_activation(self) -> None:
        self._boot_id = uuid.uuid4().hex
        self.begin_activation()

    def pause_claims(self) -> None:
        self._claims_paused = True
        self._coordinator.pause()

    def resume_claims(self) -> None:
        self._claims_paused = False
        self._coordinator.resume()

    async def pause_and_wait(
        self,
        *,
        timeout_seconds: float = ADD_TIMEOUT_SECONDS,
    ) -> bool:
        """Fence new work and wait a bounded time for add and flush operations."""

        self.pause_claims()
        timeout = _positive_timeout(timeout_seconds)
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(self._drain_lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        self._drain_lock.release()
        remaining = max(timeout - (asyncio.get_running_loop().time() - started), 0.001)
        return await self._coordinator.pause_and_wait(timeout_seconds=remaining)

    async def drain(self, *, max_rows: int = MAX_DRAIN_BATCH_SIZE) -> int:
        """Drain a bounded add batch and schedule due flushes asynchronously."""

        budget = min(max(int(max_rows), 0), MAX_DRAIN_BATCH_SIZE)
        if budget == 0:
            return 0
        async with self._drain_lock:
            if self._activation_pending:
                await self._coordinator.recover(lease_owner=self._boot_id)
                self._activation_pending = False
            if self._claims_paused or not self._enabled():
                return 0

            await self._coordinator.run_due()
            processed = 0
            for _ in range(budget):
                if (
                    self._claims_paused
                    or not self._enabled()
                    or not self._coordinator.add_claims_available()
                ):
                    break
                row = await self._claim_due(
                    lease_owner=self._boot_id,
                    now=_iso(self._current_time()),
                )
                if row is None:
                    break
                processed += 1
                if not await self._coordinator.deliver(row, lease_owner=self._boot_id):
                    break
                await self._coordinator.run_due()
            await self._coordinator.run_due()
            return processed

    async def drain_once(self) -> int:
        return await self.drain(max_rows=1)

    async def prepare_shutdown(self) -> None:
        self.pause_claims()
        await self._coordinator.prepare_shutdown()

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _claim_due(self, *, lease_owner: str, now: str) -> QueueRow | None:
        task = asyncio.create_task(
            asyncio.to_thread(
                self._store.claim_due,
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

    async def _store_call(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await run_blocking(operation, *args, **kwargs)


def _positive_timeout(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1.0
    return parsed if parsed > 0 else 1.0


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
