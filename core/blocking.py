"""Cancellation-safe coordination for blocking and multi-stage operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar


_Result = TypeVar("_Result")


class CancellationSettlement:
    """Retain caller cancellation across an irreversible multi-stage sequence.

    Callers run every required commit/reconcile stage through this instance,
    then call ``raise_if_cancelled`` at the consistency boundary.
    """

    def __init__(self) -> None:
        self._cancellation: asyncio.CancelledError | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancellation is not None

    async def wait(self, awaitable: Awaitable[_Result]) -> _Result:
        task = asyncio.ensure_future(awaitable)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if task.cancelled():
                    if asyncio.current_task().cancelling():
                        self._cancellation = self._cancellation or error
                        try:
                            return task.result()
                        except asyncio.CancelledError as child_error:
                            self.raise_if_cancelled(child_error)
                    return task.result()
                self._cancellation = self._cancellation or error
            except Exception:
                break
        return task.result()

    async def run_blocking(
        self,
        operation: Callable[..., _Result],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _Result:
        return await self.wait(asyncio.to_thread(operation, *args, **kwargs))

    def raise_if_cancelled(self, cause: BaseException | None = None) -> None:
        if self._cancellation is not None:
            if cause is not None and cause is not self._cancellation:
                raise self._cancellation from cause
            raise self._cancellation


async def run_blocking(
    operation: Callable[..., _Result],
    /,
    *args: Any,
    **kwargs: Any,
) -> _Result:
    """Settle one blocking operation before propagating caller cancellation."""

    settlement = CancellationSettlement()
    try:
        result = await settlement.run_blocking(operation, *args, **kwargs)
    except (Exception, asyncio.CancelledError):
        settlement.raise_if_cancelled()
        raise
    settlement.raise_if_cancelled()
    return result
