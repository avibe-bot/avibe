"""Cancellation shared by provider streams, the loop, and tools."""

from __future__ import annotations

import asyncio
from typing import Optional


class CancelToken:
    """A one-way cancellation flag that async code can poll or await."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: Optional[str] = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    def cancel(self, reason: str = "aborted") -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    async def wait(self) -> None:
        await self._event.wait()
