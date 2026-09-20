"""Shared validation for the agent trace retention window.

The value is persisted in configuration but consumed by storage and the CLI.
Keeping the range here gives those boundaries one contract and prevents a
malformed window from reaching ``datetime - timedelta``.
"""

from __future__ import annotations

from typing import Any


MIN_RETENTION_DAYS = 1
# A thousand years is intentionally generous while remaining well inside the
# representable datetime range for current runtime timestamps.
MAX_RETENTION_DAYS = 365_000


def validate_retention_days(value: Any, *, field: str = "retention window") -> int:
    """Return a valid integer window or raise a descriptive ``ValueError``."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS
    ):
        raise ValueError(
            f"{field} must be an integer between {MIN_RETENTION_DAYS} "
            f"and {MAX_RETENTION_DAYS}"
        )
    return value
