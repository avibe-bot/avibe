"""Scrub-safe Memory attachment telemetry."""

from __future__ import annotations

import logging


# Preserve the existing observability channel while keeping this helper free of
# admission and IM dependencies.
logger = logging.getLogger("core.memory.admission")


def log_attachment_skip(platform: str, count: int, reason: str) -> None:
    """Record one aggregate attachment drop without native attachment detail."""

    logger.info(
        "memory_attachment_capture_skipped platform=%s count=%d reason=%s",
        platform,
        count,
        reason,
    )
