"""Cancellation-safe execution for blocking Memory operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar


_Result = TypeVar("_Result")


async def run_blocking(
    operation: Callable[..., _Result],
    /,
    *args: Any,
    **kwargs: Any,
) -> _Result:
    """Settle one synchronous Memory operation before propagating cancellation."""

    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except Exception:
            break
    if cancellation is not None:
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            pass
        raise cancellation
    return task.result()
