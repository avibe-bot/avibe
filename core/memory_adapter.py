"""Host-owned capture boundary for optional Memory integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias


@dataclass(frozen=True, slots=True)
class TurnAccepted:
    """One committed human turn offered for best-effort capture."""

    context: object
    text: str
    session_id: str
    lifecycle_snapshot: object
    attachment_lease: object | None = None


@dataclass(frozen=True, slots=True)
class SessionReset:
    """A core session generation was reset successfully."""

    session_id: str


@dataclass(frozen=True, slots=True)
class SessionArchived:
    """A core session was archived successfully."""

    session_id: str


MemoryEvent: TypeAlias = TurnAccepted | SessionReset | SessionArchived


class MemoryCaptureAdapter(Protocol):
    """Accept a best-effort host event without waiting or raising."""

    def offer(self, event: MemoryEvent, /) -> None: ...


class DisabledMemoryAdapter:
    """No-op capture target used while Memory is disabled."""

    def offer(self, event: MemoryEvent, /) -> None:
        del event
