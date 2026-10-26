"""Frozen reader boundary from core.handlers.model_hub.usage at b83d62de60f23e0894f321348400a1b9207f0011 (gh-v3.2.3rc4).

Exact source fragments, not a simulated old reader. Unchanged supporting types,
validation and I/O helpers come from the current module; the released vocabulary,
projection and identity functions below own the downgrade behavior under test.
Do not update this fixture when current readers change.
"""
from __future__ import annotations

from typing import Optional
from core.handlers.model_hub.usage import (
    _calendar_day, _is_service, _normalize_hours, _repair_service_counters,
    _text, _timestamp, persisted_ledger_key,
)

def _normalize_service_row(row: dict) -> Optional[dict]:
    """Project one persisted search-service row, repaired to its relations, or drop it.

    A row naming both a model and a service is neither; the same day, Source and
    key rules as a model row apply otherwise.
    """

    if "model_id" in row:
        return None
    day = _text(row.get("day"))
    source_id = persisted_ledger_key(row.get("source_id"))
    service_id = persisted_ledger_key(row.get("service_id"))
    if day is None or source_id is None or service_id is None:
        return None
    calendar_day = _calendar_day(day)
    if calendar_day is None:
        return None
    repaired = _repair_service_counters(row)
    if repaired is None:
        return None
    counters, broken = repaired
    normalized = {
        "day": calendar_day.isoformat(),
        "source_id": source_id,
        "service_id": service_id,
        **counters,
        # A broken relation leaves a smaller true statement, so what it values is
        # a floor; so is a request whose billing is unknown.
        "lower_bound": row.get("lower_bound") is True or broken or counters["uncertain_requests"] > 0,
    }
    if row.get("history_degraded") is True:
        normalized["history_degraded"] = True
    normalized["last_metered_at"] = _timestamp(row.get("last_metered_at"))
    # Repaired counts no longer match the slices that were written beside them,
    # so where in the day the remainder fell is unknown.
    return _normalize_hours(normalized, row, invalid=broken)


def _row_key(row: dict) -> tuple[str, str, str, int, bool]:
    """A row is one day of one model on one Source, in one context tier.

    Or of one search service on one Source. The last element keeps the two apart,
    so a model whose ID spells a catalog service id is never folded into it.
    """

    if _is_service(row):
        return (row["day"], row["source_id"], row["service_id"], 0, True)
    return (row["day"], row["source_id"], row["model_id"], row.get("context_tier", 0), False)
