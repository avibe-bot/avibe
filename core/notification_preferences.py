"""Account-wide Inbox notification preferences stored in ``state_meta``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.chat_discovery import get_state_meta, set_state_meta
from core.notification_categories import CATEGORY_BOT, CATEGORY_SESSION

PREFERENCES_KEY_PREFIX = "notification_preferences:"
PREFERENCE_FIELDS = ("enabled", "bots", "sessions")


def preferences_key(user_key: str) -> str:
    """Return the durable state key for one trusted notification user key."""

    if not isinstance(user_key, str) or not user_key.strip():
        raise ValueError("user_key must be a non-empty string")
    return f"{PREFERENCES_KEY_PREFIX}{user_key.strip()}"


def _normalized_document(raw: Any) -> dict[str, Any]:
    document = dict(raw) if isinstance(raw, dict) else {}
    for field in PREFERENCE_FIELDS:
        value = document.get(field)
        document[field] = value if isinstance(value, bool) else True
    return document


def get_notification_preferences(
    user_key: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Read preferences; missing or malformed known fields default to on."""

    raw = get_state_meta(preferences_key(user_key), db_path=db_path)
    return _normalized_document(raw)


def set_notification_preferences(
    user_key: str,
    updates: Mapping[str, Any],
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Merge known boolean fields while preserving unknown stored fields."""

    if not isinstance(updates, Mapping):
        raise ValueError("preferences must be an object")
    document = get_notification_preferences(user_key, db_path=db_path)
    for field in PREFERENCE_FIELDS:
        if field not in updates:
            continue
        value = updates[field]
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be a JSON boolean")
        document[field] = value
    set_state_meta(preferences_key(user_key), document, db_path=db_path)
    return document


def public_notification_preferences(preferences: Mapping[str, Any]) -> dict[str, bool]:
    """Return the stable API/SSE shape, excluding reserved future fields."""

    return {
        field: bool(preferences.get(field, True))
        for field in PREFERENCE_FIELDS
    }


def category_is_loud(preferences: Mapping[str, Any], category: str) -> bool:
    """Whether a category contributes badges and may trigger Web Push."""

    if category == CATEGORY_BOT:
        field = "bots"
    elif category == CATEGORY_SESSION:
        field = "sessions"
    else:
        raise ValueError(f"unknown notification category: {category}")
    return bool(preferences.get("enabled", True)) and bool(preferences.get(field, True))


def badge_total(
    unread_by_category: Mapping[str, int],
    preferences: Mapping[str, Any],
) -> int:
    """Sum unread counts only for loud categories."""

    return sum(
        int(unread_by_category.get(category, 0))
        for category in (CATEGORY_BOT, CATEGORY_SESSION)
        if category_is_loud(preferences, category)
    )
