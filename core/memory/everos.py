"""Private provider port used by the provider-independent Memory core.

The real EverOS adapter is intentionally deferred to Slice 2.  This module
contains only the stable port, closed failure categories, and a small fake for
the module contract tests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Protocol, runtime_checkable

from core.memory.types import MemoryErrorCode, MemoryItem, is_memory_error_code


@dataclass(frozen=True)
class ProviderCapture:
    principal_id: str
    session_ref: str
    text: str
    provider_timestamp_ms: int


class MemoryProviderFailure(RuntimeError):
    """A redaction-safe failure already classified by the provider adapter."""

    def __init__(
        self,
        error: MemoryErrorCode = "memory_processing_failed",
        *,
        retryable: bool = True,
    ) -> None:
        closed_error: MemoryErrorCode = (
            error if is_memory_error_code(error) else "memory_processing_failed"
        )
        super().__init__(closed_error)
        self.error = closed_error
        self.retryable = bool(retryable)


class MemoryProviderSystemFailure(MemoryProviderFailure):
    """The sidecar or its configured processing dependencies are unavailable."""

    def __init__(
        self,
        error: MemoryErrorCode = "memory_sidecar_unavailable",
    ) -> None:
        closed_error: MemoryErrorCode = (
            error if is_memory_error_code(error) else "memory_sidecar_unavailable"
        )
        super().__init__(closed_error, retryable=True)


class MemoryProviderMessageFailure(MemoryProviderFailure):
    """A healthy provider could not process one capture."""

    def __init__(
        self,
        error: MemoryErrorCode = "memory_processing_failed",
        *,
        retryable: bool = True,
    ) -> None:
        closed_error: MemoryErrorCode = (
            error if is_memory_error_code(error) else "memory_processing_failed"
        )
        super().__init__(closed_error, retryable=retryable)


@runtime_checkable
class MemoryProviderPort(Protocol):
    async def ingest(self, capture: ProviderCapture) -> None: ...

    async def search(
        self,
        principal_id: str,
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]: ...

    async def profile(self, principal_id: str) -> tuple[MemoryItem, ...]: ...

    async def health(self) -> bool: ...


@dataclass
class FakeMemoryProvider:
    """In-memory provider fake for Memory module and worker contract tests."""

    healthy: bool = True
    search_items: tuple[MemoryItem, ...] = ()
    profile_items: tuple[MemoryItem, ...] = ()
    captures: list[ProviderCapture] = field(default_factory=list)
    ingest_failures: Deque[BaseException] = field(default_factory=deque)
    search_failure: BaseException | None = None
    profile_failure: BaseException | None = None
    health_failure: BaseException | None = None

    async def ingest(self, capture: ProviderCapture) -> None:
        if self.ingest_failures:
            raise self.ingest_failures.popleft()
        self.captures.append(capture)

    async def search(
        self,
        principal_id: str,
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]:
        del principal_id, query, limit
        if self.search_failure is not None:
            raise self.search_failure
        return self.search_items

    async def profile(self, principal_id: str) -> tuple[MemoryItem, ...]:
        del principal_id
        if self.profile_failure is not None:
            raise self.profile_failure
        return self.profile_items

    async def health(self) -> bool:
        if self.health_failure is not None:
            raise self.health_failure
        return self.healthy
