"""Host-owned capture boundary for optional Memory integration."""

from __future__ import annotations

from typing import Protocol, TypeAlias


MemoryEvent: TypeAlias = object


class MemoryCaptureAdapter(Protocol):
    """Accept a best-effort host event without waiting or raising."""

    def offer(self, event: MemoryEvent, /) -> None: ...


class DisabledMemoryAdapter:
    """No-op capture target used while Memory is disabled."""

    def offer(self, event: MemoryEvent, /) -> None:
        del event
