from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pytest

from core.inbox_events import QUEUE_UPDATED_EVENT, bus
from core.runtime_work import (
    RuntimeWorkHandler,
    RuntimeWorkItem,
    RuntimeWorkLane,
    RuntimeWorkSupervisor,
)


@dataclass
class _Handler(RuntimeWorkHandler):
    items: list[RuntimeWorkItem]
    started: list[str]
    release: asyncio.Event
    started_event: asyncio.Event | None = None

    def scan(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ):
        available = [
            item
            for item in self.items
            if item.partition_key not in occupied
            and (cursor is None or item.partition_key > cursor)
        ]
        return available[:limit], len(available) > limit

    async def process(self, item: RuntimeWorkItem) -> None:
        self.started.append(item.partition_key)
        if self.started_event is not None:
            self.started_event.set()
        await self.release.wait()


@dataclass
class _RetryHandler(RuntimeWorkHandler):
    attempts: int = 0
    attempted: asyncio.Event | None = None

    def scan(
        self,
        *,
        limit: int,
        occupied: frozenset[str],
        cursor: str | None,
    ):
        if "session-a" in occupied:
            return [], False
        if cursor is not None:
            return [], False
        return [RuntimeWorkItem("session-a", {})], False

    async def process(self, item: RuntimeWorkItem) -> bool:
        self.attempts += 1
        if self.attempted is not None:
            self.attempted.set()
        return False


@pytest.mark.anyio
async def test_hfr_151_supervisor_coalesces_wakes_and_keeps_partition_single_flight() -> None:
    """HFR-151: coalesced wakes preserve bounded per-partition single flight."""
    release = asyncio.Event()
    started = asyncio.Event()
    handler = _Handler(
        items=[RuntimeWorkItem("session-a", {"version": 1})],
        started=[],
        release=release,
        started_event=started,
    )
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600, lane_capacity=2)
    token = supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert handler.started == ["session-a"]

    unregister = supervisor.begin_unregister(token)
    release.set()
    await unregister
    await supervisor.stop()
    assert handler.started == ["session-a"]


@pytest.mark.anyio
async def test_hfr_151_guarded_noop_uses_partition_backoff() -> None:
    """HFR-151: a stale guarded no-op cannot become a tight retry loop."""

    attempted = asyncio.Event()
    backoff_entered = asyncio.Event()
    release_backoff = asyncio.Event()

    async def controlled_backoff(delay: float) -> None:
        assert delay == 0.1
        backoff_entered.set()
        await release_backoff.wait()

    handler = _RetryHandler(attempted=attempted)
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=1,
        retry_backoff=0.1,
        retry_wait=controlled_backoff,
    )
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()
    await asyncio.wait_for(attempted.wait(), timeout=1)
    await asyncio.wait_for(backoff_entered.wait(), timeout=1)
    assert handler.attempts == 1
    release_backoff.set()
    await supervisor.stop()
    assert handler.attempts == 1


@pytest.mark.anyio
async def test_hfr_151_stale_first_page_does_not_starve_later_partition() -> None:
    """HFR-151: the bounded scan cursor advances past stale partitions."""

    class _StalePageHandler(RuntimeWorkHandler):
        def __init__(self) -> None:
            self.items = [
                RuntimeWorkItem(f"session-{index}", {})
                for index in range(1, 7)
            ]
            self.started: list[str] = []
            self.scan_limits: list[int] = []
            self.later_partition_started = asyncio.Event()

        def scan(
            self,
            *,
            limit: int,
            occupied: frozenset[str],
            cursor: str | None,
        ):
            self.scan_limits.append(limit)
            available = [
                item
                for item in self.items
                if item.partition_key not in occupied
                and (cursor is None or item.partition_key > cursor)
            ]
            return available[:limit], len(available) > limit

        async def process(self, item: RuntimeWorkItem) -> bool:
            self.started.append(item.partition_key)
            if item.partition_key == "session-5":
                self.later_partition_started.set()
            return False

    handler = _StalePageHandler()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=4,
        scan_page_size=4,
        retry_backoff=0.01,
    )
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()
    await asyncio.wait_for(handler.later_partition_started.wait(), timeout=1)
    await supervisor.stop()

    assert "session-5" in handler.started
    assert max(handler.scan_limits) == 4


@pytest.mark.anyio
async def test_hfr_152_supervisor_starts_paused_and_periodic_reconcile_wakes_lane() -> None:
    """HFR-152: startup is paused and periodic reconciliation re-reads work."""
    first_scan = asyncio.Event()
    tick_waiting = asyncio.Event()
    release_tick = asyncio.Event()
    started = asyncio.Event()
    release_worker = asyncio.Event()

    class _PeriodicHandler(RuntimeWorkHandler):
        ready = False

        def scan(
            self,
            *,
            limit: int,
            occupied: frozenset[str],
            cursor: str | None,
        ):
            del limit, cursor
            if not self.ready or "session-a" in occupied:
                first_scan.set()
                return [], False
            return [RuntimeWorkItem("session-a", {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            assert item.partition_key == "session-a"
            started.set()
            await release_worker.wait()

    async def controlled_reconcile_wait(delay: float) -> None:
        assert delay == 3600
        tick_waiting.set()
        await release_tick.wait()
        release_tick.clear()

    handler = _PeriodicHandler()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=1,
        reconcile_wait=controlled_reconcile_wait,
    )
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    assert not started.is_set()

    await supervisor.activate()
    await asyncio.wait_for(first_scan.wait(), timeout=1)
    await asyncio.wait_for(tick_waiting.wait(), timeout=1)
    handler.ready = True
    release_tick.set()
    await asyncio.wait_for(started.wait(), timeout=1)
    release_worker.set()
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_153_bus_callback_crosses_threads_without_mutating_lane_off_loop() -> None:
    """HFR-153: cross-thread events schedule all lane mutation on its loop."""
    release = asyncio.Event()
    started = asyncio.Event()
    handler = _Handler(
        items=[RuntimeWorkItem("session-a", {})],
        started=[],
        release=release,
        started_event=started,
    )
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600, lane_capacity=1)
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()

    publisher = threading.Thread(
        target=lambda: bus.publish(QUEUE_UPDATED_EVENT, {"session_id": "ignored"})
    )
    publisher.start()
    publisher.join()
    await asyncio.wait_for(started.wait(), timeout=1)
    assert handler.started == ["session-a"]
    release.set()
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_154_replacing_live_registration_requires_generation_handoff() -> None:
    """HFR-154: replacement requires exact registration-generation handoff."""
    first = _Handler([], [], asyncio.Event())
    second = _Handler([], [], asyncio.Event())
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    token = supervisor.register(RuntimeWorkLane.REQUESTS, first)
    with pytest.raises(RuntimeError, match="already registered"):
        supervisor.register(RuntimeWorkLane.REQUESTS, second)
    await supervisor.unregister(token)
    replacement = supervisor.register(RuntimeWorkLane.REQUESTS, second)
    assert replacement.generation > token.generation
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_154_begin_unregister_invalidates_before_async_join() -> None:
    """HFR-154: stop invalidates before an in-flight scan can admit work."""

    class _BlockingScanHandler(RuntimeWorkHandler):
        def __init__(self) -> None:
            self.scan_started = threading.Event()
            self.release_scan = threading.Event()
            self.scans = 0
            self.processed: list[str] = []

        def scan(
            self,
            *,
            limit: int,
            occupied: frozenset[str],
            cursor: str | None,
        ):
            self.scans += 1
            self.scan_started.set()
            assert self.release_scan.wait(timeout=1)
            return [RuntimeWorkItem("session-a", {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            self.processed.append(item.partition_key)

    handler = _BlockingScanHandler()
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    token = supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()
    assert await asyncio.to_thread(handler.scan_started.wait, 1)

    unregister = supervisor.begin_unregister(token)
    handler.release_scan.set()
    await unregister
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)

    assert handler.scans == 1
    assert handler.processed == []
    await supervisor.stop()


@pytest.mark.anyio
async def test_concurrent_stop_callers_join_the_same_blocked_scan() -> None:
    """Every stop caller waits for the exact underlying synchronous owner."""

    class _BlockingScanHandler(RuntimeWorkHandler):
        def __init__(self) -> None:
            self.scan_started = threading.Event()
            self.release_scan = threading.Event()

        def scan(
            self,
            *,
            limit: int,
            occupied: frozenset[str],
            cursor: str | None,
        ):
            del limit, occupied, cursor
            self.scan_started.set()
            assert self.release_scan.wait(timeout=2)
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    handler = _BlockingScanHandler()
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()
    assert await asyncio.to_thread(handler.scan_started.wait, 1)

    first = asyncio.create_task(supervisor.stop())
    second = asyncio.create_task(supervisor.stop())
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()

    handler.release_scan.set()
    await asyncio.gather(first, second)
