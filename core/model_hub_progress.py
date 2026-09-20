"""Read-only presentation of live Model Hub recovery; never owns a retry."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from core.handlers.model_hub.resolver import parse_model_hub_timestamp
from vibe.i18n import t


def recovery_snapshot(controller: Any, turn_id: str | None) -> list[dict]:
    if not isinstance(turn_id, str) or not turn_id.strip():
        return []
    gateway = getattr(controller, "model_hub_turn_gateway", None)
    read = getattr(getattr(gateway, "correlation", None), "recovery_snapshot", None)
    if not callable(read):
        return []
    return read(turn_id.strip())


def publish_recovery_changed(controller: Any, turn_id: str) -> None:
    """Invalidate the existing Session read projection, without chat delivery."""

    from core.inbox_events import bus

    manager = getattr(controller, "session_turns", None)
    for session_id, entry in tuple(getattr(manager, "in_flight", {}).items()):
        if entry.task.done():
            continue
        payload = getattr(entry.context, "platform_specific", None) or {}
        if payload.get("turn_token") == turn_id:
            bus.publish(
                "session.activity",
                {"session_id": session_id, "event": "model_recovery"},
            )


def recovery_status_text(
    controller: Any,
    turn_id: str | None,
    language: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Debounce a status label for an existing IM bubble; emit no messages."""

    observed_at = now or datetime.now(timezone.utc)
    for snapshot in recovery_snapshot(controller, turn_id):
        started_at = snapshot.get("started_at")
        if not isinstance(started_at, str):
            continue
        try:
            age = (observed_at - parse_model_hub_timestamp(started_at)).total_seconds()
        except (ValueError, OverflowError):
            continue
        if age < 5:
            continue
        phase = snapshot.get("phase")
        if phase == "attempting":
            return t("modelHub.recovery.attempting", language)
        if phase != "waiting":
            continue
        retry_at = snapshot.get("next_eligible_at")
        if isinstance(retry_at, str):
            try:
                remaining = (parse_model_hub_timestamp(retry_at) - observed_at).total_seconds()
            except (ValueError, OverflowError):
                remaining = 0
            if remaining > 0:
                return t("modelHub.recovery.waiting", language, seconds=math.ceil(remaining))
        # A read-side clock can establish eligibility, never actual recovery.
        return t("modelHub.recovery.pending", language)
    return None
