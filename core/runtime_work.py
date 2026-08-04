"""Controller-owned scheduling for passive durable-work recovery lanes."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from typing import Any, Protocol, TypeVar

from core.inbox_events import (
    DEFINITIONS_UPDATED_EVENT,
    QUEUE_UPDATED_EVENT,
    RUNS_UPDATED_EVENT,
    VAULTS_UPDATED_EVENT,
    bus,
)
from vibe import runtime

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0
_DEFAULT_LANE_CAPACITY = 4
_DEFAULT_SCAN_PAGE_SIZE = 32
_DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
_DEFAULT_OVERDUE_SECONDS = 30.0


class RuntimeWorkLane(str, Enum):
    SESSION_DELIVERIES = "delivery_queue_recovery"
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
    cursor_key: str | None = None
    rearm_after_process: bool = True


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
    workers: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    worker_started_at: dict[str, float] = field(default_factory=dict)
    scan_started_at: float | None = None
    scan_cursor: str | None = None
    unregister_task: asyncio.Task[None] | None = None
    live: bool = True


_EVENT_LANES = {
    QUEUE_UPDATED_EVENT: (RuntimeWorkLane.SESSION_DELIVERIES,),
    RUNS_UPDATED_EVENT: (
        RuntimeWorkLane.REQUESTS,
        RuntimeWorkLane.RUN_CALLBACKS,
        RuntimeWorkLane.FAILURE_NOTICES,
    ),
    VAULTS_UPDATED_EVENT: (RuntimeWorkLane.VAULT_CALLBACKS,),
    DEFINITIONS_UPDATED_EVENT: (
        RuntimeWorkLane.TASK_DEFINITIONS,
        RuntimeWorkLane.WATCH_DEFINITIONS,
    ),
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
        retry_wait: Callable[[float], Awaitable[None]] | None = None,
        reconcile_wait: Callable[[float], Awaitable[None]] | None = None,
        overdue_seconds: float = _DEFAULT_OVERDUE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reconcile_interval = max(0.001, float(reconcile_interval))
        self._lane_capacity = max(1, int(lane_capacity))
        self._scan_page_size = max(1, int(scan_page_size))
        self._retry_backoff = max(0.001, float(retry_backoff))
        self._retry_wait = retry_wait or asyncio.sleep
        self._reconcile_wait = reconcile_wait or asyncio.sleep
        self._overdue_seconds = max(0.001, float(overdue_seconds))
        self._monotonic = monotonic
        self._registrations: dict[RuntimeWorkLane, _Registration] = {}
        self._generations: dict[RuntimeWorkLane, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active = False
        self._stopping = False
        self._subscription_id: int | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._delayed_wakes: dict[
            RuntimeWorkRegistrationToken,
            asyncio.TimerHandle,
        ] = {}
        self._pending_lock = threading.Lock()
        self._pending_lanes: set[RuntimeWorkLane] = set()
        self._requires_service_lease = runtime.service_instance_lock_attached_to_process()
        self._sync_executor = ThreadPoolExecutor(
            max_workers=max(8, self._lane_capacity * len(RuntimeWorkLane)),
            thread_name_prefix="runtime-work",
        )
        self._sync_futures: set[asyncio.Future[Any]] = set()
        self._sync_executor_stopped = False

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
        delayed = self._delayed_wakes.pop(token, None)
        if delayed is not None:
            delayed.cancel()
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

    def notify_after(
        self,
        token: RuntimeWorkRegistrationToken,
        delay: float,
    ) -> None:
        """Coalesce a generation-scoped future eligibility wake."""

        loop = self._loop
        if not self._active or loop is None or loop.is_closed():
            return
        bounded_delay = max(0.0, float(delay))
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            self._notify_after_on_loop(token, bounded_delay)
            return
        try:
            loop.call_soon_threadsafe(
                self._notify_after_on_loop,
                token,
                bounded_delay,
            )
        except RuntimeError:
            pass

    def _notify_after_on_loop(
        self,
        token: RuntimeWorkRegistrationToken,
        delay: float,
    ) -> None:
        registration = self._registrations.get(token.lane)
        if (
            registration is None
            or registration.token != token
            or not registration.live
            or not self._active
            or self._stopping
        ):
            return
        if delay <= 0:
            registration.event.set()
            return
        loop = self._loop
        if loop is None:
            return
        deadline = loop.time() + delay
        existing = self._delayed_wakes.get(token)
        if existing is not None and not existing.cancelled():
            if existing.when() <= deadline:
                return
            existing.cancel()
        self._delayed_wakes[token] = loop.call_at(
            deadline,
            self._fire_delayed_wake,
            token,
        )

    def _fire_delayed_wake(self, token: RuntimeWorkRegistrationToken) -> None:
        self._delayed_wakes.pop(token, None)
        registration = self._registrations.get(token.lane)
        if (
            registration is not None
            and registration.token == token
            and registration.live
            and self._active
            and not self._stopping
        ):
            registration.event.set()

    async def run_in_partition(
        self,
        lane: RuntimeWorkLane,
        partition_key: str,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Run a live domain owner under the same lease as recovered work.

        Activity output has both a live consumer and a restart consumer.  The
        durable Registry remains authoritative; this lease only prevents those
        two consumers from claiming or emitting for one runtime concurrently.
        """

        partition = str(partition_key or "").strip()
        registration = self._registrations.get(lane)
        loop = self._loop
        if (
            not partition
            or registration is None
            or not registration.live
            or not self._active
            or self._stopping
            or loop is None
        ):
            return await operation()
        if asyncio.get_running_loop() is not loop:
            raise RuntimeError("runtime work partitions belong to the controller loop")

        current = asyncio.current_task()
        assert current is not None
        owner_task: asyncio.Task[_T] | None = None
        while True:
            owner = registration.workers.get(partition)
            if owner is current:
                return await operation()
            if owner is None:
                owner_task = asyncio.create_task(
                    operation(),
                    name=f"runtime-work-live:{lane.value}:{partition}",
                )
                registration.workers[partition] = owner_task
                registration.worker_started_at[partition] = self._monotonic()
                owner_task.add_done_callback(
                    partial(
                        self._release_live_partition,
                        registration,
                        partition,
                    )
                )
                break
            await asyncio.gather(asyncio.shield(owner), return_exceptions=True)
            if not registration.live or self._stopping:
                raise RuntimeError("runtime work partition is stopping")
            if owner.done() and registration.workers.get(partition) is owner:
                registration.workers.pop(partition, None)
                registration.worker_started_at.pop(partition, None)

        assert owner_task is not None
        return await asyncio.shield(owner_task)

    def _release_live_partition(
        self,
        registration: _Registration,
        partition: str,
        owner: asyncio.Task[Any],
    ) -> None:
        if registration.workers.get(partition) is owner:
            registration.workers.pop(partition, None)
            registration.worker_started_at.pop(partition, None)
        if registration.live and self._active and not self._stopping:
            registration.event.set()

    async def run_sync(self, operation: Callable[[], _T]) -> _T:
        """Run blocking store work without abandoning its exact worker future."""

        if self._stopping or self._sync_executor_stopped:
            raise RuntimeError("runtime work executor is stopped")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._sync_executor, operation)
        self._sync_futures.add(future)
        cancelled = False
        try:
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError:
                    # Python threads are not safely cancellable. Consume every
                    # cancellation until the exact store operation exits; the
                    # partition task remains the owner throughout.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            if future.done():
                self._sync_futures.discard(future)

    async def stop(self) -> None:
        stop_task = self._begin_stop()
        await asyncio.shield(stop_task)

    def _begin_stop(self) -> asyncio.Task[None]:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_and_join(),
                name="runtime-work-stop",
            )
        return self._stop_task

    def _stop_for_lost_lease(self) -> None:
        if self._stopping or self._stop_task is not None:
            return
        logger.error(
            "Runtime work supervisor stopping because this process no longer "
            "owns the service lock"
        )
        self._begin_stop()

    async def _stop_and_join(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._active = False
        for handle in self._delayed_wakes.values():
            handle.cancel()
        self._delayed_wakes.clear()
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
        remaining = tuple(self._sync_futures)
        if remaining:
            await asyncio.gather(
                *(asyncio.shield(future) for future in remaining),
                return_exceptions=True,
            )
        self._sync_executor_stopped = True
        await asyncio.to_thread(
            self._sync_executor.shutdown,
            wait=True,
            cancel_futures=False,
        )
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
            ):
                continue
            if not self._owns_service_instance():
                self._stop_for_lost_lease()
                return
            capacity = self._lane_capacity - len(registration.workers)
            if capacity <= 0:
                continue
            occupied = frozenset(registration.workers)
            try:
                registration.scan_started_at = self._monotonic()
                try:
                    items, has_more = await self.run_sync(
                        partial(
                            registration.handler.scan,
                            limit=min(capacity, self._scan_page_size),
                            occupied=occupied,
                            cursor=registration.scan_cursor,
                        )
                    )
                finally:
                    registration.scan_started_at = None
            except Exception:
                logger.exception(
                    "Runtime work scan failed for lane=%s generation=%s",
                    registration.token.lane.value,
                    registration.token.generation,
                )
                if registration.live and not self._stopping:
                    await self._retry_wait(self._retry_backoff)
                    if registration.live and self._active and not self._stopping:
                        registration.event.set()
                continue
            if not registration.live or self._stopping:
                continue
            if not self._owns_service_instance():
                self._stop_for_lost_lease()
                return
            if items:
                last_cursor = str(
                    items[-1].cursor_key or items[-1].partition_key or ""
                ).strip()
                if last_cursor:
                    registration.scan_cursor = last_cursor
            elif registration.scan_cursor is not None and not has_more:
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
                registration.worker_started_at[partition] = self._monotonic()
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
            elif registration.live:
                self._stop_for_lost_lease()
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
                    await self._retry_wait(self._retry_backoff)
            finally:
                current = registration.workers.get(partition)
                if current is asyncio.current_task():
                    registration.workers.pop(partition, None)
                    registration.worker_started_at.pop(partition, None)
                if (
                    registration.live
                    and self._active
                    and not self._stopping
                    and (should_backoff or item.rearm_after_process)
                ):
                    registration.event.set()

    async def _reconcile_loop(self) -> None:
        try:
            while self._active and not self._stopping:
                await self._reconcile_wait(self._reconcile_interval)
                if not self._owns_service_instance():
                    self._stop_for_lost_lease()
                    return
                self._report_overdue_workers()
                self._notify_on_loop(tuple(self._registrations))
        except asyncio.CancelledError:
            raise

    def _report_overdue_workers(self) -> None:
        now = self._monotonic()
        for lane, registration in tuple(self._registrations.items()):
            if not registration.live:
                continue
            if registration.scan_started_at is not None:
                elapsed = now - registration.scan_started_at
                if elapsed >= self._overdue_seconds:
                    logger.warning(
                        "Runtime work item overdue lane=%s partition=%s "
                        "generation=%s elapsed=%.3fs",
                        lane.value,
                        "<scan>",
                        registration.token.generation,
                        elapsed,
                    )
            for partition, started_at in tuple(registration.worker_started_at.items()):
                elapsed = now - started_at
                if elapsed < self._overdue_seconds:
                    continue
                logger.warning(
                    "Runtime work item overdue lane=%s partition=%s generation=%s "
                    "elapsed=%.3fs",
                    lane.value,
                    partition,
                    registration.token.generation,
                    elapsed,
                )
