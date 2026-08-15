"""Scrub-safe Memory attachment telemetry.

This leaf stays above the eager ``core.memory`` package because importing that
package loads SQLite-backed modules and breaks native-session lightweight imports.
"""

from __future__ import annotations

import logging


# Preserve the existing observability channel while keeping this helper free of
# admission and IM dependencies.
logger = logging.getLogger("core.memory.admission")


def log_attachment_capture(
    platform: str,
    total: int,
    captured: int,
) -> None:
    """Record one finalized attachment set without native attachment detail."""

    dropped = total - captured
    logger.info(
        "memory_attachment_capture platform=%s total=%d captured=%d dropped=%d",
        platform,
        total,
        captured,
        dropped,
    )
