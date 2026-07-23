"""Provider-neutral acknowledgements and processing observations for Memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


@dataclass(frozen=True)
class AddAck:
    request_id: str | None
    status: Literal["accumulated", "extracted"] | None


@dataclass(frozen=True)
class FlushSucceeded:
    request_id: str | None
    status: Literal["extracted", "no_extraction"] | None


@dataclass(frozen=True)
class FlushRejected:
    request_id: str | None
    error_code: str | None
    server_fault: bool


@dataclass(frozen=True)
class FlushUnknown:
    reason: Literal["timeout", "transport"]


FlushResult: TypeAlias = FlushSucceeded | FlushRejected | FlushUnknown
