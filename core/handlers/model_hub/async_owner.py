"""Cancellation-safe ownership for finite work delegated to threads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar


_Result = TypeVar("_Result")


async def await_owned_task(task: asyncio.Task[_Result]) -> _Result:
    """Wait through caller cancellation without cancelling owned work."""

    while True:
        if task.cancelled():
            raise asyncio.CancelledError
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


async def run_owned_in_thread(
    function: Callable[..., _Result],
    /,
    *args: Any,
    **kwargs: Any,
) -> _Result:
    """Keep a thread call's resources alive until that call has stopped using them."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancelled:
        try:
            await await_owned_task(worker)
        except BaseException:
            # Cancellation owns the public result; draining owns resource safety.
            pass
        raise cancelled
