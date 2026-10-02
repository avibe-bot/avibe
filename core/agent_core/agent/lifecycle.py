"""One run's admission and child-operation ownership (Python 3.10 asyncio).

Factories, never pre-created awaitables, cross this boundary. Interruptible work
is cancelled and joined before it leaves the scope. Once admitted, a store
transaction is joined without interruption so its caller receives the commit.
Cleanup runs outside this scope, after admission has closed.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from core.agent_core.cancel import CancelToken

T = TypeVar("T")


class RunAborted(Exception):
    """The run no longer admits ordinary work."""


class RunScope:
    def __init__(self, cancel: CancelToken) -> None:
        self.cancel = cancel
        self._closed = False
        self._operations: dict[asyncio.Future, bool] = {}

    def check(self) -> None:
        if self._closed or self.cancel.cancelled:
            raise RunAborted()

    def stop(self) -> None:
        self._closed = True

    async def call(self, factory: Callable[[], Awaitable[T]], *, interruptible: bool = True) -> T:
        self.check()

        async def invoke() -> T:
            # Cancellation may arrive between admission and task scheduling.
            self.check()
            return await factory()

        operation = asyncio.create_task(invoke())
        self._operations[operation] = interruptible
        cancelled = None
        try:
            if not interruptible:
                # A consumer closing during a commit still waits for the
                # transaction's outcome; the next admission sees cancellation.
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    return await operation
            cancelled = asyncio.create_task(self.cancel.wait())
            ready, _ = await asyncio.wait({operation, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in ready:
                raise RunAborted()
            return await operation
        finally:
            if interruptible and not operation.done():
                operation.cancel()
            if cancelled is not None and not cancelled.done():
                cancelled.cancel()
            joined = asyncio.gather(operation, *([cancelled] if cancelled is not None else []), return_exceptions=True)
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                await joined
                raise
            finally:
                self._operations.pop(operation, None)

    async def close(self) -> None:
        self.stop()
        for task, interruptible in tuple(self._operations.items()):
            if interruptible and not task.done():
                task.cancel()
        if self._operations:
            await asyncio.gather(*self._operations, return_exceptions=True)
