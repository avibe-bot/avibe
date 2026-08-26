"""Authorization-scoped adapter for native EverOS Processing Records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from core.memory.native_processing_record import NativeProcessingRecordReader
from core.memory.processing_record import ProcessingSourceObservations

MemoryReadScope: TypeAlias = tuple[str, str]


@dataclass(frozen=True, slots=True)
class MemoryInsightPaths:
    everos_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "everos_root", Path(self.everos_root))


class MemoryInsightReader:
    """Expose the bounded native Processing Record projection."""

    def __init__(
        self,
        paths: MemoryInsightPaths,
        *,
        provider_base_urls: tuple[str, ...] | list[str] = (),
        exact_redaction_values: tuple[str, ...] | list[str] = (),
    ) -> None:
        if isinstance(provider_base_urls, str) or any(
            not isinstance(value, str) for value in provider_base_urls
        ):
            raise TypeError("provider_base_urls must be a sequence of strings")
        if isinstance(exact_redaction_values, str) or any(
            not isinstance(value, str) for value in exact_redaction_values
        ):
            raise TypeError("exact_redaction_values must be a sequence of strings")
        self._native = NativeProcessingRecordReader(
            paths.everos_root,
            provider_base_urls=tuple(value.rstrip("/") for value in provider_base_urls if value),
            exact_redaction_values=tuple(value for value in exact_redaction_values if value),
        )

    def source_observation(self) -> ProcessingSourceObservations:
        return self._native.source_observation()

    def list_processing_records(
        self, scope: MemoryReadScope, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        return self._native.list_records(scope, cursor, limit)

    def processing_record_detail(
        self, scope: MemoryReadScope, memcell_id: str
    ) -> dict[str, Any]:
        return self._native.record_detail(scope, memcell_id)
