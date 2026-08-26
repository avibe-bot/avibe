from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.memory.blocking import run_blocking


def test_run_blocking_returns_operation_result() -> None:
    def operation(value: int, *, increment: int) -> int:
        return value + increment

    assert asyncio.run(run_blocking(operation, 40, increment=2)) == 42


def test_run_blocking_settles_work_before_repeated_cancellation_propagates() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def operation() -> None:
        entered.set()
        release.wait(timeout=2)
        finished.set()

    async def run() -> None:
        call = asyncio.create_task(run_blocking(operation))
        assert await asyncio.to_thread(entered.wait, 1)

        call.cancel("first")
        await asyncio.sleep(0)
        assert not call.done()
        call.cancel("second")
        await asyncio.sleep(0)
        assert not call.done()

        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await call
        assert raised.value.args == ("first",)
        assert finished.is_set()

    asyncio.run(run())


def test_run_blocking_reports_later_operation_failure_before_cancellation() -> None:
    entered = threading.Event()
    release = threading.Event()
    failure = RuntimeError("blocking operation failed")
    reported: list[BaseException] = []

    def operation() -> None:
        entered.set()
        release.wait(timeout=2)
        raise failure

    async def run() -> None:
        call = asyncio.create_task(
            run_blocking(operation, on_cancel_error=reported.append)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        call.cancel("caller stopped")
        await asyncio.sleep(0)
        assert not call.done()

        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await call
        assert raised.value.args == ("caller stopped",)
        assert reported == [failure]

    asyncio.run(run())


def test_run_blocking_finishes_queued_work_before_cancellation_propagates() -> None:
    executor_entered = threading.Event()
    release_executor = threading.Event()
    operation_finished = threading.Event()

    def occupy_executor() -> None:
        executor_entered.set()
        release_executor.wait(timeout=2)

    def operation() -> None:
        operation_finished.set()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupying = asyncio.create_task(asyncio.to_thread(occupy_executor))
        await asyncio.sleep(0)
        assert executor_entered.wait(timeout=1)

        call = asyncio.create_task(run_blocking(operation))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        call.cancel("cancelled while queued")
        await asyncio.sleep(0)
        assert not call.done()
        assert not operation_finished.is_set()

        release_executor.set()
        await occupying
        with pytest.raises(asyncio.CancelledError) as raised:
            await call
        assert raised.value.args == ("cancelled while queued",)
        assert operation_finished.is_set()

    asyncio.run(run())


def test_run_blocking_propagates_operation_failure_without_cancellation() -> None:
    def operation() -> None:
        raise RuntimeError("blocking operation failed")

    with pytest.raises(RuntimeError, match="blocking operation failed"):
        asyncio.run(run_blocking(operation))


def test_run_blocking_completed_result_is_not_changed_by_late_cancellation() -> None:
    async def run() -> None:
        call = asyncio.create_task(run_blocking(lambda: "settled"))
        assert await call == "settled"
        assert call.cancel("too late") is False
        assert call.result() == "settled"

    asyncio.run(run())


def test_run_blocking_reclaims_settled_result_before_cancellation_propagates() -> None:
    entered = threading.Event()
    release = threading.Event()
    reclaimed: list[str] = []

    def operation() -> str:
        entered.set()
        release.wait(timeout=1.0)
        return "bundle-1"

    async def run() -> None:
        call = asyncio.create_task(
            run_blocking(operation, on_cancel_result=reclaimed.append)
        )
        assert await asyncio.to_thread(entered.wait, 1.0)
        call.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(run())
    assert reclaimed == ["bundle-1"]


def test_run_blocking_reports_cancelled_result_cleanup_failure() -> None:
    entered = threading.Event()
    release = threading.Event()
    failure = RuntimeError("result cleanup failed")
    reported: list[BaseException] = []

    def operation() -> str:
        entered.set()
        release.wait(timeout=1.0)
        return "bundle-1"

    def reclaim(_result: str) -> None:
        raise failure

    async def run() -> None:
        call = asyncio.create_task(
            run_blocking(
                operation,
                on_cancel_result=reclaim,
                on_cancel_error=reported.append,
            )
        )
        assert await asyncio.to_thread(entered.wait, 1.0)
        call.cancel("caller stopped")
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await call
        assert raised.value.args == ("caller stopped",)

    asyncio.run(run())
    assert reported == [failure]
