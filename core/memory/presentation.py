"""Shared presentation aggregates for Memory status surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class MemoryStatusBuckets:
    syncing: int
    succeeded: int
    unknown: int
    failed: int
    dead: int
    missed: int


def memory_status_buckets(payload: Mapping[str, object]) -> MemoryStatusBuckets:
    """Derive the six user-facing buckets from the status payload facts."""

    return MemoryStatusBuckets(
        syncing=(
            _count(payload, "pending")
            + _count(payload, "processing")
            + _count(payload, "awaiting_receipt")
        ),
        succeeded=_count(payload, "succeeded"),
        unknown=_count(payload, "receipt_unknown"),
        failed=_count(payload, "distill_failed"),
        dead=_count(payload, "dead"),
        missed=_count(payload, "missed"),
    )


def _count(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
