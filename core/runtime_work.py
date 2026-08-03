"""Controller-owned scheduling for passive durable-work recovery lanes."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from core.inbox_events import QUEUE_UPDATED_EVENT, RUNS_UPDATED_EVENT, bus
from vibe import runtime

logger = logging.getLogger(__name__)

_DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0
_DEFAULT_LANE_CAPACITY = 4
_DEFAULT_SCAN_PAGE_SIZE = 32
_DEFAULT_RETRY_BACKOFF_SECONDS = 1.0


class RuntimeWorkLane(str, Enum):
    SESSION_DELIVERIES = "session_deliveries"
    TASK_DEFINITIONS = "task_definitions"
    WATCH_DEFINITIONS = "watch_definitions"
    REQUESTS = "requests"
    RUN_CALLBACKS = "run_callbacks"
    VAULT_CALLBACKS = "vault_callbacks"
    ACTIVITY_OUTPUTS = "activity_outputs"
    FAILURE_NOTICES = "failure_notices"
    STALE_RUNS = "stale_runs"


@dataclass(frozen=True)
class RuntimeWorkItem:
    partition_key: str
    observation: Any


class RuntimeWorkHandler(Protocol):
    """A lane re-reader and its existing guarded owner entry point.

    ``scan`` returns a bounded page ordered by stable partition key, strictly
    after the exclusive cursor when one is supplied.
    """

    def scan(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ) -> tuple[list[RuntimeWorkItem], bool]: ...

    async def process(self, item: RuntimeWorkItem) -> bool | None: ...


@dataclass(frozen=True)
class RuntimeWorkRegistrationToken:
    lane: RuntimeWorkLane
    generation: int


@dataclass
class _Registration:
    token: RuntimeWorkRegistrationToken
    handler: RuntimeWorkHandler
    event: asyncio.Event = field(default_factory=asyncio.Event)
    coordinator: asyncio.Task[None] | None = None
    workers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    scan_cursor: str | None = None
    unregister_task: asyncio.Task[None] | None = None
    live: bool = True


_EVENT_LANES = {
    QUEUE_UPDATED_EVENT: (RuntimeWorkLane.SESSION_DELIVERIES,),
    RUNS_UPDATED_EVENT: (RuntimeWorkLane.REQUESTS,),
}


class RuntimeWorkSupervisor:
    """Coalesce hints while durable stores retain all item authority."""

    def __init__(
        self,
        *,
        reconcile_interval: float = _DEFAULT_RECONCILE_INTERVAL_SECONDS,
        lane_capacity: int = _DEFAULT_LANE_CAPACITY,
        scan_page_size: int = _DEFAULT_SCAN_PAGE_SIZE,
        retry_backoff: float = _DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._reconcile_interval = max(0.001, float(reconcile_interval))
        self._lane_capacity = max(1, int(lane_capacity))
        self._scan_page_size = max(1, int(scan_page_size))
        self._retry_backoff = max(0.001, float(retry_backoff))
        self._registrations: dict[RuntimeWorkLane, _Registration] = {}
        self._generations: dict[RuntimeWorkLane, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active = False
        self._stopping = False
        self._subscription_id: int | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._pending_lock = threading.Lock()
        self._pending_lanes: set[RuntimeWorkLane] = set()
        self._requires_service_lease = runtime.service_instance_lock_attached_to_process()

    def register(
        self,
        lane: RuntimeWorkLane,
        handler: RuntimeWorkHandler,
    ) -> RuntimeWorkRegistrationToken:
        existing = self._registrations.get(lane)
        if existing is not None:
            raise RuntimeError(f"runtime work lane already registered: {lane.value}")
        generation = self._generations.get(lane, 0) + 1
        self._generations[lane] = generation
        token = RuntimeWorkRegistrationToken(lane=lane, generation=generation)
        registration = _Registration(token=token, handler=handler)
        self._registrations[lane] = registration
        if self._active:
            self._start_registration(registration)
            registration.event.set()
        return token

    async def unregister(self, token: RuntimeWorkRegistrationToken) -> None:
        await self.begin_unregister(token)

    def begin_unregister(
        self,
        token: RuntimeWorkRegistrationToken,
    ) -> asyncio.Task[None]:
        """Invalidate a registration synchronously, then join it asynchronously."""

        registration = self._registrations.get(token.lane)
        if registration is None or registration.token != token:
            return asyncio.create_task(asyncio.sleep(0))
        if registration.unregister_task is not None:
            return registration.unregister_task
        # Invalidate the generation before joining it. Worker completion can no
        # longer re-arm this lane or a later replacement registration.
        registration.live = False
        registration.event.set()
        registration.unregister_task = asyncio.create_task(
            self._finish_unregister(registration),
            name=f"runtime-work-unregister:{token.lane.value}",
        )
        return registration.unregister_task

    async def _finish_unregister(self, registration: _Registration) -> None:
        coordinator = registration.coordinator
        if coordinator is not None:
            await asyncio.gather(coordinator, return_exceptions=True)
        workers = tuple(registration.workers.values())
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if self._registrations.get(registration.token.lane) is registration:
            self._registrations.pop(registration.token.lane, None)

    async def activate(self) -> None:
        if self._active:
            return
        if self._stopping:
            raise RuntimeError("runtime work supervisor is stopping")
        self._loop = asyncio.get_running_loop()
        self._active = True
        self._subscription_id = bus.subscribe_callback(self._on_bus_event)
        for registration in tuple(self._registrations.values()):
            self._start_registration(registration)
            registration.event.set()
        with self._pending_lock:
            pending = tuple(self._pending_lanes)
            self._pending_lanes.clear()
        self._notify_on_loop(pending)
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(),
            name="runtime-work-reconcile",
        )

    def notify(self, *lanes: RuntimeWorkLane) -> None:
        if not lanes:
            return
        loop = self._loop
        if not self._active or loop is None or loop.is_closed():
            with self._pending_lock:
                self._pending_lanes.update(lanes)
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            self._notify_on_loop(lanes)
            return
        try:
            loop.call_soon_threadsafe(self._notify_on_loop, tuple(lanes))
        except RuntimeError:
            # A closing generation may lose a hint; the next controller's
            # startup reconciliation is the durable recovery boundary.
            pass

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._active = False
        if self._subscription_id is not None:
            bus.unsubscribe(self._subscription_id)
            self._subscription_id = None
        reconcile = self._reconcile_task
        self._reconcile_task = None
        if reconcile is not None:
            reconcile.cancel()
            await asyncio.gather(reconcile, return_exceptions=True)
        tokens = [registration.token for registration in self._registrations.values()]
        for token in tokens:
            await self.unregister(token)
        self._loop = None

    def _start_registration(self, registration: _Registration) -> None:
        if registration.coordinator is not None:
            return
        registration.coordinator = asyncio.create_task(
            self._coordinate(registration),
            name=f"runtime-work:{registration.token.lane.value}",
        )

    def _on_bus_event(self, event_type: str, _payload: Any) -> None:
        lanes = _EVENT_LANES.get(event_type)
        if not lanes:
            return
        loop = self._loop
        if loop is None or loop.is_closed() or not self._active:
            return
        try:
            loop.call_soon_threadsafe(self._notify_on_loop, lanes)
        except RuntimeError:
            pass

    def _notify_on_loop(self, lanes: tuple[RuntimeWorkLane, ...] | list[RuntimeWorkLane]) -> None:
        if not self._active or self._stopping:
            return
        for lane in lanes:
            registration = self._registrations.get(lane)
            if registration is not None and registration.live:
                registration.event.set()

    def _owns_service_instance(self) -> bool:
        return (
            not self._requires_service_lease
            or runtime.current_process_owns_service_instance()
        )

    async def _coordinate(self, registration: _Registration) -> None:
        while registration.live and not self._stopping:
            await registration.event.wait()
            registration.event.clear()
            if (
                not registration.live
                or self._stopping
                or not self._active
                or not self._owns_service_instance()
            ):
                continue
            capacity = self._lane_capacity - len(registration.workers)
            if capacity <= 0:
                continue
            occupied = frozenset(registration.workers)
            try:
                items, has_more = await asyncio.to_thread(
                    registration.handler.scan,
                    limit=min(capacity, self._scan_page_size),
                    occupied=occupied,
                    cursor=registration.scan_cursor,
                )
            except Exception:
                logger.exception(
                    "Runtime work scan failed for lane=%s generation=%s",
                    registration.token.lane.value,
                    registration.token.generation,
                )
                continue
            if not registration.live or self._stopping or not self._owns_service_instance():
                continue
            if items:
                last_partition = str(items[-1].partition_key or "").strip()
                if last_partition:
                    registration.scan_cursor = last_partition
            elif registration.scan_cursor is not None:
                # The durable keyset reached its end. Wrap exactly once so old
                # stale observations can be retried without pinning later keys.
                registration.scan_cursor = None
                registration.event.set()
            for item in items:
                partition = str(item.partition_key or "").strip()
                if not partition or partition in registration.workers:
                    continue
                task = asyncio.create_task(
                    self._run_item(registration, item),
                    name=(
                        f"runtime-work:{registration.token.lane.value}:"
                        f"{partition}"
                    ),
                )
                registration.workers[partition] = task
            if has_more and len(registration.workers) < self._lane_capacity:
                registration.event.set()

    async def _run_item(
        self,
        registration: _Registration,
        item: RuntimeWorkItem,
    ) -> None:
        partition = item.partition_key
        should_backoff = False
        try:
            if registration.live and self._owns_service_instance():
                processed = await registration.handler.process(item)
                should_backoff = processed is False
        except asyncio.CancelledError:
            raise
        except Exception:
            should_backoff = True
            logger.exception(
                "Runtime work item failed for lane=%s partition=%s generation=%s",
                registration.token.lane.value,
                partition,
                registration.token.generation,
            )
        finally:
            try:
                if should_backoff and registration.live and not self._stopping:
                    await asyncio.sleep(self._retry_backoff)
            finally:
                current = registration.workers.get(partition)
                if current is asyncio.current_task():
                    registration.workers.pop(partition, None)
                if registration.live and self._active and not self._stopping:
                    registration.event.set()

    async def _reconcile_loop(self) -> None:
        try:
            while self._active and not self._stopping:
                await asyncio.sleep(self._reconcile_interval)
                self._notify_on_loop(tuple(self._registrations))
        except asyncio.CancelledError:
            raise
