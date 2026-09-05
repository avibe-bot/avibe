from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pytest

from core.inbox_events import (
    DEFINITIONS_UPDATED_EVENT,
    QUEUE_UPDATED_EVENT,
    RUNS_UPDATED_EVENT,
    VAULTS_UPDATED_EVENT,
    bus,
)
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
async def test_hfr_170_guarded_noop_uses_partition_backoff() -> None:
    """HFR-151: a stale guarded no-op cannot become a tight retry loop."""

    attempted = asyncio.Event()
    backoff_entered = asyncio.Event()
    release_backoff = asyncio.Event()

    async def controlled_backoff(delay: float) -> None:
        assert delay == pytest.approx(0.1, abs=0.01)
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
    await supervisor.stop()
    assert handler.attempts == 1


@pytest.mark.anyio
async def test_hfr_162_backoff_partitions_release_capacity_and_rewind_for_retry() -> None:
    backoff_entered = asyncio.Event()
    release_backoff = asyncio.Event()
    later_started = asyncio.Event()
    retried_head = asyncio.Event()

    async def controlled_backoff(_delay: float) -> None:
        backoff_entered.set()
        await release_backoff.wait()

    class _BackoffHead(RuntimeWorkHandler):
        def __init__(self) -> None:
            self.attempts: dict[str, int] = {}

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            rows = [
                RuntimeWorkItem(
                    f"partition-{index}",
                    {},
                    cursor_key=str(index),
                )
                for index in range(5)
                if f"partition-{index}" not in occupied
                and (cursor is None or str(index) > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> bool:
            count = self.attempts.get(item.partition_key, 0) + 1
            self.attempts[item.partition_key] = count
            if item.partition_key == "partition-4":
                later_started.set()
                return True
            if count == 1:
                return False
            if item.partition_key == "partition-0":
                retried_head.set()
            return True

    handler = _BackoffHead()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=4,
        scan_page_size=4,
        retry_wait=controlled_backoff,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, handler)
    await supervisor.activate()

    await asyncio.wait_for(backoff_entered.wait(), 1)
    await asyncio.wait_for(later_started.wait(), 1)
    assert handler.attempts["partition-4"] == 1

    release_backoff.set()
    await asyncio.wait_for(retried_head.wait(), 1)
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_162_partition_backoff_state_is_lane_bounded() -> None:
    retry_entered = asyncio.Event()
    release_retry = asyncio.Event()
    six_partitions_attempted = asyncio.Event()
    later_partition_started = asyncio.Event()
    later_partition_retried = asyncio.Event()
    attempted: list[str] = []
    attempt_counts: dict[str, int] = {}

    async def controlled_retry(_delay: float) -> None:
        retry_entered.set()
        await release_retry.wait()

    class _UnavailablePartitions(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            rows = [
                RuntimeWorkItem(
                    f"partition-{index:02d}",
                    {},
                    cursor_key=f"{index:02d}",
                )
                for index in range(9)
                if f"partition-{index:02d}" not in occupied
                and (cursor is None or f"{index:02d}" > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> bool:
            attempted.append(item.partition_key)
            if len(attempted) == 6:
                six_partitions_attempted.set()
            attempt_counts[item.partition_key] = (
                attempt_counts.get(item.partition_key, 0) + 1
            )
            if item.partition_key == "partition-08":
                later_partition_started.set()
                return True
            if attempt_counts[item.partition_key] == 1:
                return False
            if item.partition_key == "partition-06":
                later_partition_retried.set()
            return True

    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
        scan_continuation_pages=3,
        retry_wait=controlled_retry,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, _UnavailablePartitions())
    await supervisor.activate()

    await asyncio.wait_for(retry_entered.wait(), 1)
    await asyncio.wait_for(six_partitions_attempted.wait(), 1)
    assert attempted == [f"partition-{index:02d}" for index in range(6)]
    registration = supervisor._registrations[RuntimeWorkLane.REQUESTS]
    assert len(registration.backoff_deadlines) == 6
    assert registration.backoff_task is not None
    assert not registration.backoff_task.done()
    assert not later_partition_started.is_set()

    supervisor.notify(RuntimeWorkLane.REQUESTS, reset_cursor=True)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not later_partition_started.is_set()

    release_retry.set()
    await asyncio.wait_for(later_partition_started.wait(), 1)
    await asyncio.wait_for(later_partition_retried.wait(), 1)
    assert attempt_counts["partition-06"] == 2
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_288_expired_backoff_preserves_forward_scan_before_retry() -> None:
    retry_entered = asyncio.Event()
    release_retry = asyncio.Event()
    first_window_attempted = asyncio.Event()
    later_partition_started = asyncio.Event()
    attempts: list[str] = []

    async def controlled_retry(_delay: float) -> None:
        retry_entered.set()
        await release_retry.wait()

    class _UnavailablePrefix(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            rows = [
                RuntimeWorkItem(
                    f"partition-{index:02d}",
                    {},
                    cursor_key=f"{index:02d}",
                )
                for index in range(7)
                if f"partition-{index:02d}" not in occupied
                and (cursor is None or f"{index:02d}" > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> bool:
            attempts.append(item.partition_key)
            if len(attempts) == 6:
                first_window_attempted.set()
            if item.partition_key == "partition-06":
                later_partition_started.set()
                return True
            return later_partition_started.is_set()

    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
        scan_continuation_pages=3,
        retry_wait=controlled_retry,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, _UnavailablePrefix())
    await supervisor.activate()

    await asyncio.wait_for(retry_entered.wait(), 1)
    await asyncio.wait_for(first_window_attempted.wait(), 1)
    assert attempts == [f"partition-{index:02d}" for index in range(6)]

    release_retry.set()
    await asyncio.wait_for(later_partition_started.wait(), 1)
    assert attempts[6] == "partition-06"
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_162_backoff_budget_preserves_exact_partition_deadline() -> None:
    now = 0.0
    release_retry = asyncio.Event()
    retry_entered = asyncio.Event()
    retry_delays: list[float] = []
    exact_retried = asyncio.Event()
    later_started = asyncio.Event()
    attempts: dict[str, int] = {}

    async def controlled_retry(delay: float) -> None:
        retry_delays.append(delay)
        retry_entered.set()
        await release_retry.wait()

    class _TwoPartitions(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            rows = [
                RuntimeWorkItem(partition, {}, cursor_key=partition)
                for partition in ("exact", "later")
                if partition not in occupied
                and (cursor is None or partition > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> bool:
            attempts[item.partition_key] = attempts.get(item.partition_key, 0) + 1
            if item.partition_key == "exact" and attempts[item.partition_key] == 1:
                return False
            if item.partition_key == "exact":
                exact_retried.set()
            else:
                later_started.set()
            return True

    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=1,
        scan_page_size=1,
        scan_continuation_pages=1,
        retry_backoff=10,
        retry_wait=controlled_retry,
        monotonic=lambda: now,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, _TwoPartitions())
    await supervisor.activate()
    try:
        await asyncio.wait_for(retry_entered.wait(), 1)
        assert retry_delays == [10.0]
        assert attempts == {"exact": 1}

        now = 5.0
        supervisor.notify(RuntimeWorkLane.REQUESTS, reset_cursor=True)
        for _ in range(10):
            await asyncio.sleep(0)
        assert attempts == {"exact": 1}
        assert not later_started.is_set()

        now = 10.0
        release_retry.set()
        await asyncio.wait_for(exact_retried.wait(), 1)
        await asyncio.wait_for(later_started.wait(), 1)
        assert attempts == {"exact": 2, "later": 1}
    finally:
        await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_170_live_partition_admission_is_rejected_after_shutdown_starts() -> None:
    invoked = False

    class _Idle(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    async def operation() -> None:
        nonlocal invoked
        invoked = True

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, _Idle())
    await supervisor.activate()
    stopping = asyncio.create_task(supervisor.stop())
    while not supervisor._stopping:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="partition is stopping"):
        await supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            operation,
        )
    await stopping
    assert invoked is False


@pytest.mark.anyio
async def test_hfr_170_quiesce_keeps_executor_for_service_finalization() -> None:
    class _Idle(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, _Idle())
    await supervisor.activate()
    supervisor.quiesce()

    assert await supervisor.run_sync(lambda: "persisted") == "persisted"
    with pytest.raises(RuntimeError, match="partition is stopping"):
        await supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            lambda: asyncio.sleep(0),
        )
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_170_waiter_cannot_reacquire_after_quiesce() -> None:
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    waiter_invoked = False

    class _Idle(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    async def owner() -> None:
        owner_entered.set()
        await release_owner.wait()

    async def waiter_operation() -> None:
        nonlocal waiter_invoked
        waiter_invoked = True

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, _Idle())
    await supervisor.activate()
    first = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            owner,
        )
    )
    await asyncio.wait_for(owner_entered.wait(), 1)
    waiter = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            waiter_operation,
        )
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    supervisor.quiesce()
    release_owner.set()
    await first
    with pytest.raises(RuntimeError, match="partition is stopping"):
        await waiter
    assert waiter_invoked is False
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_175_lease_loss_quiesces_before_controller_callback() -> None:
    callbacks: list[str] = []
    invoked = False

    class _Idle(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    async def operation() -> None:
        nonlocal invoked
        invoked = True

    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        on_lease_lost=lambda: callbacks.append("lost"),
    )
    supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, _Idle())
    await supervisor.activate()
    supervisor._stop_for_lost_lease()

    assert callbacks == ["lost"]
    assert supervisor._quiescing is True
    with pytest.raises(RuntimeError, match="partition is stopping"):
        await supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            operation,
        )
    assert invoked is False
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_175_waiter_rechecks_service_lease_before_reacquiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owns_lease = True
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    callbacks: list[str] = []
    waiter_invoked = False

    class _Idle(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    async def owner() -> None:
        owner_entered.set()
        await release_owner.wait()

    async def waiter_operation() -> None:
        nonlocal waiter_invoked
        waiter_invoked = True

    monkeypatch.setattr(
        "core.runtime_work.runtime.current_process_owns_service_instance",
        lambda: owns_lease,
    )
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        on_lease_lost=lambda: callbacks.append("lost"),
    )
    supervisor._requires_service_lease = True
    supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, _Idle())
    await supervisor.activate()
    first = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            owner,
        )
    )
    await asyncio.wait_for(owner_entered.wait(), 1)
    waiter = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            waiter_operation,
        )
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    owns_lease = False
    release_owner.set()
    await first
    with pytest.raises(RuntimeError, match="partition is stopping"):
        await waiter
    assert callbacks == ["lost"]
    assert supervisor._quiescing is True
    assert waiter_invoked is False
    await supervisor.stop()


@pytest.mark.anyio
async def test_completed_maintenance_item_does_not_self_rearm() -> None:
    scans = 0
    processed = asyncio.Event()

    class _Maintenance(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            nonlocal scans
            del limit, occupied, cursor
            scans += 1
            return [
                RuntimeWorkItem(
                    "maintenance",
                    {},
                    rearm_after_process=False,
                )
            ], False

        async def process(self, item: RuntimeWorkItem) -> None:
            del item
            processed.set()

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    supervisor.register(RuntimeWorkLane.TASK_DEFINITIONS, _Maintenance())
    await supervisor.activate()
    await asyncio.wait_for(processed.wait(), 1)
    for _ in range(10):
        await asyncio.sleep(0)
    assert scans == 1

    processed.clear()
    supervisor.notify(RuntimeWorkLane.TASK_DEFINITIONS)
    await asyncio.wait_for(processed.wait(), 1)
    assert scans == 2
    await supervisor.stop()


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (QUEUE_UPDATED_EVENT, {RuntimeWorkLane.SESSION_DELIVERIES}),
        (
            RUNS_UPDATED_EVENT,
            {
                RuntimeWorkLane.REQUESTS,
                RuntimeWorkLane.RUN_CALLBACKS,
                RuntimeWorkLane.FAILURE_NOTICES,
                RuntimeWorkLane.STALE_RUNS,
            },
        ),
        (VAULTS_UPDATED_EVENT, {RuntimeWorkLane.VAULT_CALLBACKS}),
        (
            DEFINITIONS_UPDATED_EVENT,
            {
                RuntimeWorkLane.TASK_DEFINITIONS,
                RuntimeWorkLane.WATCH_DEFINITIONS,
            },
        ),
    ],
)
async def test_hfr_155_events_wake_only_their_durable_lanes(
    event_type: str,
    expected: set[RuntimeWorkLane],
) -> None:
    started: set[RuntimeWorkLane] = set()
    events = {lane: asyncio.Event() for lane in RuntimeWorkLane}

    class _OneShot(RuntimeWorkHandler):
        def __init__(self, lane: RuntimeWorkLane) -> None:
            self.lane = lane
            self.ready = False
            self.initial_scan = threading.Event()

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            self.initial_scan.set()
            if not self.ready:
                return [], False
            self.ready = False
            return [RuntimeWorkItem(self.lane.value, {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            del item
            started.add(self.lane)
            events[self.lane].set()

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    handlers = {lane: _OneShot(lane) for lane in RuntimeWorkLane}
    for lane, handler in handlers.items():
        supervisor.register(lane, handler)
    await supervisor.activate()
    await asyncio.gather(
        *(asyncio.to_thread(handler.initial_scan.wait, 1) for handler in handlers.values())
    )
    for handler in handlers.values():
        handler.ready = True
    bus.publish(event_type, {})
    await asyncio.gather(
        *(asyncio.wait_for(events[lane].wait(), 1) for lane in expected)
    )
    await asyncio.sleep(0)
    await supervisor.stop()
    assert started == expected


@pytest.mark.anyio
async def test_hfr_162_failed_scan_rearms_after_bounded_backoff() -> None:
    attempted = asyncio.Event()
    release_backoff = asyncio.Event()
    processed = asyncio.Event()

    class _FailsOnce(RuntimeWorkHandler):
        scans = 0

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            self.scans += 1
            attempted.set()
            if self.scans == 1:
                raise RuntimeError("transient")
            return [RuntimeWorkItem("partition-a", {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            del item
            processed.set()

    async def backoff(delay: float) -> None:
        assert delay == 0.25
        await release_backoff.wait()

    handler = _FailsOnce()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        retry_backoff=0.25,
        retry_wait=backoff,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, handler)
    await supervisor.activate()
    await asyncio.wait_for(attempted.wait(), 1)
    assert handler.scans == 1
    release_backoff.set()
    await asyncio.wait_for(processed.wait(), 1)
    await supervisor.stop()
    assert handler.scans >= 2


@pytest.mark.anyio
async def test_hfr_162_wake_during_final_empty_scan_is_not_lost() -> None:
    processed = asyncio.Event()
    holder: dict[str, RuntimeWorkSupervisor] = {}

    class _WakeDuringEmpty(RuntimeWorkHandler):
        scans = 0

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            self.scans += 1
            if self.scans == 1:
                holder["supervisor"].notify(RuntimeWorkLane.REQUESTS)
                return [], False
            return [RuntimeWorkItem("run-a", {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            assert item.partition_key == "run-a"
            processed.set()

    handler = _WakeDuringEmpty()
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    holder["supervisor"] = supervisor
    supervisor.register(RuntimeWorkLane.REQUESTS, handler)
    await supervisor.activate()
    await asyncio.wait_for(processed.wait(), 1)
    await supervisor.stop()
    assert handler.scans >= 2


@pytest.mark.anyio
async def test_hfr_159_blocked_store_scan_does_not_block_another_lane() -> None:
    scan_entered = threading.Event()
    release_scan = threading.Event()
    other_processed = asyncio.Event()

    class _BlockedScan(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            scan_entered.set()
            assert release_scan.wait(timeout=1)
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    class _Ready(RuntimeWorkHandler):
        ready = True

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            if not self.ready:
                return [], False
            self.ready = False
            return [RuntimeWorkItem("other", {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            assert item.partition_key == "other"
            other_processed.set()

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    supervisor.register(RuntimeWorkLane.TASK_DEFINITIONS, _BlockedScan())
    supervisor.register(RuntimeWorkLane.STALE_RUNS, _Ready())
    await supervisor.activate()
    assert await asyncio.to_thread(scan_entered.wait, 1)
    await asyncio.wait_for(other_processed.wait(), 1)
    stopping = asyncio.create_task(supervisor.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release_scan.set()
    await stopping


@pytest.mark.anyio
async def test_hfr_163_lane_capacity_bounds_distinct_partition_workers() -> None:
    release = asyncio.Event()
    two_started = asyncio.Event()
    all_completed = asyncio.Event()
    active = 0
    peak = 0
    completed = 0
    done: set[str] = set()

    class _Backlog(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            rows = [
                RuntimeWorkItem(f"partition-{index}", {}, cursor_key=str(index))
                for index in range(5)
                if f"partition-{index}" not in occupied
                and f"partition-{index}" not in done
                and (cursor is None or str(index) > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> None:
            nonlocal active, peak, completed
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1
            done.add(item.partition_key)
            completed += 1
            if completed == 5:
                all_completed.set()

    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=5,
    )
    supervisor.register(RuntimeWorkLane.RUN_CALLBACKS, _Backlog())
    await supervisor.activate()
    try:
        await asyncio.wait_for(two_started.wait(), 1)
        assert peak == 2
        release.set()
        await asyncio.wait_for(all_completed.wait(), 1)
    finally:
        release.set()
        await supervisor.stop()
    assert completed == 5
    assert peak == 2


@pytest.mark.anyio
async def test_hfr_171_overdue_report_names_lane_partition_and_generation(
    caplog,
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    tick = asyncio.Event()
    clock = iter((0.0, 10.0, 45.0))

    async def reconcile_wait(delay: float) -> None:
        assert delay == 1
        await tick.wait()

    handler = _Handler(
        [RuntimeWorkItem("session-a", {})],
        [],
        release,
        started,
    )
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=1,
        overdue_seconds=30,
        reconcile_wait=reconcile_wait,
        monotonic=lambda: next(clock),
    )
    token = supervisor.register(RuntimeWorkLane.REQUESTS, handler)
    await supervisor.activate()
    await asyncio.wait_for(started.wait(), 1)
    tick.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if "Runtime work item overdue" in caplog.text:
            break
    unregister = supervisor.begin_unregister(token)
    release.set()
    await unregister
    await supervisor.stop()
    assert "lane=requests partition=session-a generation=1" in caplog.text


@pytest.mark.anyio
async def test_hfr_158_occupied_page_cannot_hide_a_later_partition() -> None:
    """HFR-158: keyset paging advances through followers of an occupied owner."""

    live_entered = asyncio.Event()
    release_live = asyncio.Event()
    later_started = asyncio.Event()
    scans = 0

    class _OccupiedPage(RuntimeWorkHandler):
        ready = False

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            nonlocal scans
            del occupied
            scans += 1
            if not self.ready:
                return [], False
            rows = [
                RuntimeWorkItem(
                    "session-a",
                    index,
                    cursor_key=f"{index:02d}",
                )
                for index in range(8)
            ] + [RuntimeWorkItem("session-b", 8, cursor_key="08")]
            rows = [row for row in rows if cursor is None or row.cursor_key > cursor]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> None:
            if item.partition_key == "session-b":
                later_started.set()

    async def live_owner() -> None:
        live_entered.set()
        await release_live.wait()

    handler = _OccupiedPage()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
        scan_continuation_pages=16,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, handler)
    await supervisor.activate()
    live = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.REQUESTS,
            "session-a",
            live_owner,
        )
    )
    await asyncio.wait_for(live_entered.wait(), 1)
    handler.ready = True
    supervisor.notify(RuntimeWorkLane.REQUESTS)
    await asyncio.wait_for(later_started.wait(), 1)
    assert scans >= 9
    release_live.set()
    await live
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_158_occupied_page_without_cursor_progress_does_not_spin() -> None:
    live_entered = asyncio.Event()
    release_live = asyncio.Event()
    scanned_twice = threading.Event()
    scans = 0

    class _RepeatedOccupiedPage(RuntimeWorkHandler):
        ready = False

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            nonlocal scans
            del limit, occupied, cursor
            if not self.ready:
                return [], False
            scans += 1
            if scans >= 2:
                scanned_twice.set()
            return [
                RuntimeWorkItem("session-a", 0, cursor_key="00"),
                RuntimeWorkItem("session-a", 1, cursor_key="01"),
            ], True

        async def process(self, item: RuntimeWorkItem) -> None:
            raise AssertionError(item)

    async def live_owner() -> None:
        live_entered.set()
        await release_live.wait()

    handler = _RepeatedOccupiedPage()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, handler)
    await supervisor.activate()
    live = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.REQUESTS,
            "session-a",
            live_owner,
        )
    )
    await asyncio.wait_for(live_entered.wait(), 1)
    handler.ready = True
    supervisor.notify(RuntimeWorkLane.REQUESTS)
    assert await asyncio.to_thread(scanned_twice.wait, 1)
    for _ in range(10):
        await asyncio.sleep(0)
    assert scans == 2

    release_live.set()
    await live
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_158_duplicate_pages_continue_only_after_bounded_backoff() -> None:
    live_entered = asyncio.Event()
    release_live = asyncio.Event()
    later_started = asyncio.Event()
    retry_started: asyncio.Queue[None] = asyncio.Queue()
    retry_release: asyncio.Queue[None] = asyncio.Queue()
    scans = 0

    async def controlled_retry(_delay: float) -> None:
        retry_started.put_nowait(None)
        await retry_release.get()

    class _LongOccupiedTail(RuntimeWorkHandler):
        ready = False

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            nonlocal scans
            del occupied
            if not self.ready:
                return [], False
            scans += 1
            rows = [
                RuntimeWorkItem("session-a", index, cursor_key=f"{index:02d}")
                for index in range(4)
            ] + [RuntimeWorkItem("session-b", 4, cursor_key="04")]
            rows = [row for row in rows if cursor is None or row.cursor_key > cursor]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> None:
            if item.partition_key == "session-b":
                later_started.set()

    async def live_owner() -> None:
        live_entered.set()
        await release_live.wait()

    handler = _LongOccupiedTail()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
        scan_continuation_pages=2,
        retry_wait=controlled_retry,
    )
    supervisor.register(RuntimeWorkLane.REQUESTS, handler)
    await supervisor.activate()
    live = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.REQUESTS,
            "session-a",
            live_owner,
        )
    )
    await asyncio.wait_for(live_entered.wait(), 1)
    handler.ready = True
    supervisor.notify(RuntimeWorkLane.REQUESTS)

    await asyncio.wait_for(retry_started.get(), 1)
    assert scans == 2
    assert not later_started.is_set()
    retry_release.put_nowait(None)
    await asyncio.wait_for(retry_started.get(), 1)
    assert scans == 4
    assert not later_started.is_set()
    retry_release.put_nowait(None)
    await asyncio.wait_for(later_started.wait(), 1)

    release_live.set()
    await live
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_158_skipped_partition_rewinds_when_its_owner_releases() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    completed: set[int] = set()
    scans = 0

    class _SamePartitionCallbacks(RuntimeWorkHandler):
        ready = False

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            nonlocal scans
            del occupied
            if not self.ready:
                return [], False
            scans += 1
            rows = [
                RuntimeWorkItem(
                    "session-a",
                    index,
                    cursor_key=f"{index:02d}",
                    rearm_after_process=False,
                )
                for index in (1, 2)
                if index not in completed
                and (cursor is None or f"{index:02d}" > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> None:
            index = int(item.observation)
            if index == 1:
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
            completed.add(index)

    handler = _SamePartitionCallbacks()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
    )
    supervisor.register(RuntimeWorkLane.RUN_CALLBACKS, handler)
    await supervisor.activate()
    handler.ready = True
    supervisor.notify(RuntimeWorkLane.RUN_CALLBACKS)

    await asyncio.wait_for(first_started.wait(), 1)
    for _ in range(10):
        await asyncio.sleep(0)
    assert scans == 1
    assert not second_started.is_set()

    release_first.set()
    await asyncio.wait_for(second_started.wait(), 1)
    assert completed == {1, 2}
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_158_live_owner_release_rewinds_skipped_partition_rows() -> None:
    live_entered = asyncio.Event()
    release_live = asyncio.Event()
    recovered: list[int] = []
    both_recovered = asyncio.Event()
    active_recoveries = 0
    peak_recoveries = 0

    class _HiddenRecoveryRows(RuntimeWorkHandler):
        ready = False

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del occupied
            if not self.ready:
                return [], False
            rows = [
                RuntimeWorkItem(
                    "session-a",
                    index,
                    cursor_key=f"{index:02d}",
                    rearm_after_process=False,
                )
                for index in (1, 2)
                if index not in recovered
                and (cursor is None or f"{index:02d}" > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> None:
            nonlocal active_recoveries, peak_recoveries
            active_recoveries += 1
            peak_recoveries = max(peak_recoveries, active_recoveries)
            await asyncio.sleep(0)
            recovered.append(int(item.observation))
            active_recoveries -= 1
            if len(recovered) == 2:
                both_recovered.set()

    async def live_owner() -> None:
        live_entered.set()
        await release_live.wait()

    handler = _HiddenRecoveryRows()
    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
    )
    supervisor.register(RuntimeWorkLane.RUN_CALLBACKS, handler)
    await supervisor.activate()
    live = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.RUN_CALLBACKS,
            "session-a",
            live_owner,
        )
    )
    await asyncio.wait_for(live_entered.wait(), 1)
    handler.ready = True
    supervisor.notify(RuntimeWorkLane.RUN_CALLBACKS)
    for _ in range(10):
        await asyncio.sleep(0)
    assert recovered == []

    release_live.set()
    await live
    await asyncio.wait_for(both_recovered.wait(), 1)
    assert sorted(recovered) == [1, 2]
    assert peak_recoveries == 1
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_162_backoff_owns_rewind_for_a_skipped_partition() -> None:
    first_attempt = asyncio.Event()
    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()
    second_recovered = asyncio.Event()
    attempts: dict[int, int] = {}
    completed: set[int] = set()

    async def controlled_retry(_delay: float) -> None:
        retry_waiting.set()
        await release_retry.wait()

    class _RetryingPartition(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del occupied
            rows = [
                RuntimeWorkItem("session-a", index, cursor_key=f"{index:02d}")
                for index in (1, 2)
                if index not in completed
                and (cursor is None or f"{index:02d}" > cursor)
            ]
            return rows[:limit], len(rows) > limit

        async def process(self, item: RuntimeWorkItem) -> bool:
            index = int(item.observation)
            attempts[index] = attempts.get(index, 0) + 1
            if index == 1 and attempts[index] == 1:
                first_attempt.set()
                return False
            completed.add(index)
            if index == 2:
                second_recovered.set()
            return True

    supervisor = RuntimeWorkSupervisor(
        reconcile_interval=3600,
        lane_capacity=2,
        scan_page_size=2,
        retry_wait=controlled_retry,
    )
    supervisor.register(RuntimeWorkLane.RUN_CALLBACKS, _RetryingPartition())
    await supervisor.activate()
    await asyncio.wait_for(first_attempt.wait(), 1)
    await asyncio.wait_for(retry_waiting.wait(), 1)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not second_recovered.is_set()

    release_retry.set()
    await asyncio.wait_for(second_recovered.wait(), 1)
    assert attempts == {1: 2, 2: 1}
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_160_live_partition_owner_blocks_recovery_and_unregisters_by_join() -> None:
    """HFR-160/HFR-166: one exact owner survives wake and registration teardown."""

    live_entered = asyncio.Event()
    release_live = asyncio.Event()
    scan_observed = threading.Event()
    recovered_started = asyncio.Event()

    class _Recovery(RuntimeWorkHandler):
        ready = False

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied
            if not self.ready:
                return [], False
            if cursor is not None:
                return [], False
            scan_observed.set()
            return [RuntimeWorkItem("claude\x1fruntime-a", {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            del item
            recovered_started.set()

    async def live_owner() -> str:
        live_entered.set()
        await release_live.wait()
        return "settled"

    handler = _Recovery()
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    token = supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, handler)
    await supervisor.activate()
    live = asyncio.create_task(
        supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "claude\x1fruntime-a",
            live_owner,
        )
    )
    await asyncio.wait_for(live_entered.wait(), 1)
    handler.ready = True
    supervisor.notify(RuntimeWorkLane.ACTIVITY_OUTPUTS)
    assert await asyncio.to_thread(scan_observed.wait, 1)
    assert not recovered_started.is_set()

    unregister = supervisor.begin_unregister(token)
    await asyncio.sleep(0)
    assert not unregister.done()
    release_live.set()
    assert await live == "settled"
    await unregister
    assert not recovered_started.is_set()
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_160_canceled_sync_worker_remains_owner_until_thread_exits() -> None:
    """HFR-160: cancellation cannot abandon a still-running store thread."""

    started = threading.Event()
    release = threading.Event()

    class _Idle(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            del item

    def blocking_store_call() -> str:
        started.set()
        release.wait()
        return "done"

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    token = supervisor.register(RuntimeWorkLane.REQUESTS, _Idle())
    await supervisor.activate()

    async def operation() -> str:
        return await supervisor.run_sync(blocking_store_call)

    owner = asyncio.create_task(
        supervisor.run_in_partition(RuntimeWorkLane.REQUESTS, "run-a", operation)
    )
    assert await asyncio.to_thread(started.wait, 1)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    unregister = supervisor.begin_unregister(token)
    await asyncio.sleep(0)
    assert not unregister.done()

    release.set()
    await unregister
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_164_unregister_joins_partition_not_outer_caller_task() -> None:
    """HFR-164: lane teardown joins only the exact protected operation."""

    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    caller_continued = asyncio.Event()
    release_caller = asyncio.Event()

    class _Idle(RuntimeWorkHandler):
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            del item

    async def operation() -> None:
        operation_started.set()
        await release_operation.wait()

    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    token = supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, _Idle())
    await supervisor.activate()

    async def caller() -> None:
        await supervisor.run_in_partition(
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
            "runtime-a",
            operation,
        )
        caller_continued.set()
        await release_caller.wait()

    caller_task = asyncio.create_task(caller())
    await asyncio.wait_for(operation_started.wait(), 1)
    unregister = supervisor.begin_unregister(token)
    release_operation.set()
    await asyncio.wait_for(caller_continued.wait(), 1)
    await asyncio.wait_for(unregister, 1)
    assert not caller_task.done()

    release_caller.set()
    await caller_task
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_165_old_generation_delayed_wake_cannot_rearm_replacement() -> None:
    """HFR-165: delayed eligibility belongs to one registration generation."""

    class _Idle(RuntimeWorkHandler):
        def __init__(self) -> None:
            self.scanned = threading.Event()

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            self.scanned.set()
            return [], False

        async def process(self, item: RuntimeWorkItem) -> None:
            del item

    first = _Idle()
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    first_token = supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, first)
    await supervisor.activate()
    assert await asyncio.to_thread(first.scanned.wait, 1)
    supervisor.notify_after(first_token, 60)
    assert first_token in supervisor._delayed_wakes
    await supervisor.unregister(first_token)
    assert first_token not in supervisor._delayed_wakes

    second = _Idle()
    second_token = supervisor.register(RuntimeWorkLane.ACTIVITY_OUTPUTS, second)
    assert await asyncio.to_thread(second.scanned.wait, 1)
    registration = supervisor._registrations[RuntimeWorkLane.ACTIVITY_OUTPUTS]
    registration.event.clear()
    supervisor._fire_delayed_wake(first_token)
    assert registration.token == second_token
    assert not registration.event.is_set()
    await supervisor.stop()


@pytest.mark.anyio
async def test_hfr_175_global_lease_loss_joins_every_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owns_lease = True
    scanned = threading.Event()
    stop_started = asyncio.Event()
    processed: list[str] = []

    class _LeaseHandler(RuntimeWorkHandler):
        ready = False

        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            scanned.set()
            if not self.ready:
                return [], False
            return [RuntimeWorkItem("work", {})], False

        async def process(self, item: RuntimeWorkItem) -> None:
            processed.append(item.partition_key)

    monkeypatch.setattr(
        "core.runtime_work.runtime.current_process_owns_service_instance",
        lambda: owns_lease,
    )
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    supervisor._requires_service_lease = True
    begin_stop = supervisor._begin_stop

    def observe_stop() -> asyncio.Task[None]:
        task = begin_stop()
        stop_started.set()
        return task

    monkeypatch.setattr(supervisor, "_begin_stop", observe_stop)
    handlers = {
        RuntimeWorkLane.SESSION_DELIVERIES: _LeaseHandler(),
        RuntimeWorkLane.REQUESTS: _LeaseHandler(),
    }
    for lane, handler in handlers.items():
        supervisor.register(lane, handler)
    await supervisor.activate()
    try:
        assert await asyncio.to_thread(scanned.wait, 1)

        owns_lease = False
        for handler in handlers.values():
            handler.ready = True
        supervisor.notify(*handlers)
        await asyncio.wait_for(stop_started.wait(), 1)
        assert supervisor._stop_task is not None
        await asyncio.wait_for(asyncio.shield(supervisor._stop_task), 1)
    finally:
        await supervisor.stop()
    assert supervisor._registrations == {}
    assert processed == []
