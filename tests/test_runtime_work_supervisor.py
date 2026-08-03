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

    def scan(self, *, limit: int, occupied: frozenset[str]):
        available = [item for item in self.items if item.partition_key not in occupied]
        return available[:limit], len(available) > limit

    async def process(self, item: RuntimeWorkItem) -> None:
        self.started.append(item.partition_key)
        await self.release.wait()


@dataclass
class _RetryHandler(RuntimeWorkHandler):
    attempts: int = 0

    def scan(self, *, limit: int, occupied: frozenset[str]):
        if "session-a" in occupied:
            return [], False
        return [RuntimeWorkItem("session-a", {})], False

    async def process(self, item: RuntimeWorkItem) -> bool:
        self.attempts += 1
        return False


@pytest.mark.anyio
async def test_hfr_151_supervisor_coalesces_wakes_and_keeps_partition_single_flight() -> None:
    """HFR-151: coalesced wakes preserve bounded per-partition single flight."""
    release = asyncio.Event()
    handler = _Handler(
        items=[RuntimeWorkItem("session-a", {"version": 1})],
        started=[],
        release=release,
    )
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600, lane_capacity=2)
    token = supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    for _ in range(20):
        if handler.started:
            break
        await asyncio.sleep(0.01)
    assert handler.started == ["session-a"]

    unregister = asyncio.create_task(supervisor.unregister(token))
    await asyncio.sleep(0)
    release.set()
    await unregister
    await supervisor.stop()
    assert handler.started == ["session-a"]


@pytest.mark.anyio
async def test_hfr_151_guarded_noop_uses_partition_backoff() -> None:
    """HFR-151: a stale guarded no-op cannot become a tight retry loop."""

    handler = _RetryHandler()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=1,
        retry_backoff=0.1,
    )
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()
    for _ in range(20):
        if handler.attempts:
            break
        await asyncio.sleep(0.005)
    assert handler.attempts == 1
    await asyncio.sleep(0.03)
    assert handler.attempts == 1
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_152_supervisor_starts_paused_and_periodic_reconcile_wakes_lane() -> None:
    """HFR-152: startup is paused and periodic reconciliation re-reads work."""
    release = asyncio.Event()
    handler = _Handler(
        items=[RuntimeWorkItem("session-a", {})],
        started=[],
        release=release,
    )
    supervisor = RuntimeWorkSupervisor(reconcile_interval=0.01, lane_capacity=1)
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    supervisor.notify(RuntimeWorkLane.SESSION_DELIVERIES)
    await asyncio.sleep(0.02)
    assert handler.started == []

    await supervisor.activate()
    for _ in range(20):
        if handler.started:
            break
        await asyncio.sleep(0.01)
    assert handler.started == ["session-a"]
    release.set()
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_153_bus_callback_crosses_threads_without_mutating_lane_off_loop() -> None:
    """HFR-153: cross-thread events schedule all lane mutation on its loop."""
    release = asyncio.Event()
    handler = _Handler(
        items=[RuntimeWorkItem("session-a", {})],
        started=[],
        release=release,
    )
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600, lane_capacity=1)
    supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, handler)
    await supervisor.activate()

    publisher = threading.Thread(
        target=lambda: bus.publish(QUEUE_UPDATED_EVENT, {"session_id": "ignored"})
    )
    publisher.start()
    publisher.join()
    for _ in range(20):
        if handler.started:
            break
        await asyncio.sleep(0.01)
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
