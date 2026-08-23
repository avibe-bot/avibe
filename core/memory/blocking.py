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
    on_cancel_result: Callable[[_Result], Any] | None = None,
    on_cancel_error: Callable[[BaseException], Any] | None = None,
    **kwargs: Any,
) -> _Result:
    """Settle synchronous work and report its result or failure after cancellation."""

    def report_cancel_error(error: BaseException) -> None:
        if on_cancel_error is None:
            return
        try:
            on_cancel_error(error)
        except (Exception, asyncio.CancelledError):
            pass

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
            result = task.result()
        except (Exception, asyncio.CancelledError) as error:
            report_cancel_error(error)
        else:
            if on_cancel_result is not None:
                try:
                    await run_blocking(
                        on_cancel_result,
                        result,
                        on_cancel_error=on_cancel_error,
                    )
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    report_cancel_error(error)
        raise cancellation
    return task.result()
