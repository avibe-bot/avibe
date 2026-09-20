"""Host-owned value boundary for optional Memory capture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias
import unicodedata


def normalize_memory_sender_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cs"}
        and (unicodedata.category(character) != "Cf" or character in {"\u200c", "\u200d"})
    ).strip()[:128].rstrip()
    return name or None


@dataclass(frozen=True, slots=True)
class MemoryFile:
    """Immutable copy of one platform-neutral attachment descriptor."""

    name: str
    mimetype: str
    url: str | None = None
    content: bytes | None = None
    local_path: str | None = None
    size: int | None = None


def snapshot_memory_files(files: object) -> tuple[MemoryFile, ...]:
    """Copy already-normalized descriptors without consulting external state."""

    if not isinstance(files, (list, tuple)):
        return ()
    copied: list[MemoryFile] = []
    for item in files:
        name = getattr(item, "name", None)
        mimetype = getattr(item, "mimetype", None)
        if not isinstance(name, str) or not isinstance(mimetype, str):
            continue
        copied.append(
            MemoryFile(
                name=name,
                mimetype=mimetype,
                url=getattr(item, "url", None),
                content=getattr(item, "content", None),
                local_path=getattr(item, "local_path", None),
                size=getattr(item, "size", None),
            )
        )
    return tuple(copied)


@dataclass(frozen=True, slots=True)
class TurnAccepted:
    """Closed snapshot of one committed human turn."""

    platform: str | None
    user_id: str | None
    message_id: str | None
    session_id: str
    text: str
    files: tuple[MemoryFile, ...]
    is_dm: bool
    is_ordinary_text: bool | None
    is_ordinary_attachment: bool | None
    lifecycle_snapshot: object
    attachment_lease: object | None = None
    sender_name: str | None = None


@dataclass(frozen=True, slots=True)
class SessionReset:
    """A core session generation was reset successfully."""

    session_id: str


@dataclass(frozen=True, slots=True)
class SessionArchived:
    """A core session was archived successfully."""

    session_id: str


@dataclass(frozen=True, slots=True, eq=False)
class DisabledCaptureReceipt:
    """Host-owned skipped result returned without importing Memory types."""

    reason: str = "memory_disabled"
    status: str = "skipped"

    def __eq__(self, other: object) -> bool:
        return (
            getattr(other, "status", None) == self.status
            and getattr(other, "reason", None) == self.reason
        )


MemoryEvent: TypeAlias = TurnAccepted | SessionReset | SessionArchived


class MemoryCaptureAdapter(Protocol):
    """Accept a best-effort host event without waiting or raising."""

    def offer(self, event: MemoryEvent, /) -> None: ...


class DisabledMemoryAdapter:
    """No-op capture target used while Memory is disabled or unavailable."""

    def offer(self, event: MemoryEvent, /) -> None:
        del event
