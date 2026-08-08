"""Shared presentation aggregates for Memory status surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class MemoryStatusBuckets:
    """The six counts every Memory status surface renders.

    Carried on ``MemoryStatus`` so the UI reads the same buckets the CLI does
    instead of re-deriving the rule per language.
    """

    syncing: int = 0
    succeeded: int = 0
    unknown: int = 0
    failed: int = 0
    dead: int = 0
    missed: int = 0


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
