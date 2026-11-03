"""Account-wide Workbench home preferences stored in ``state_meta``.

What the home remembers per account (the same account key as the notification
preferences): when "Getting started" was put off until the next day, which of
its items are not needed at all, when the tip of the day was hidden until the
next day, and whether tips show at all. Days are the reader's local calendar
dates, sent by the browser, because "until tomorrow" is the reader's tomorrow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from core.chat_discovery import get_state_meta, update_state_meta

PREFERENCES_KEY_PREFIX = "home_preferences:"
# The "Getting started" items, as the Web UI names them.
SETUP_ITEMS = ("model", "notifications", "computer_use", "chat_apps", "phone")
DATE_FIELDS = ("setup_later_on", "tip_hidden_on")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def preferences_key(user_key: str) -> str:
    """Return the durable state key for one trusted account key."""

    if not isinstance(user_key, str) or not user_key.strip():
        raise ValueError("user_key must be a non-empty string")
    return f"{PREFERENCES_KEY_PREFIX}{user_key.strip()}"


def _normalized_document(raw: Any) -> dict[str, Any]:
    """Known fields normalized to their shape; unknown stored fields kept."""

    document = dict(raw) if isinstance(raw, dict) else {}
    for field in DATE_FIELDS:
        value = document.get(field)
        document[field] = value if isinstance(value, str) and _DATE.match(value) else None
    not_needed = document.get("setup_not_needed")
    document["setup_not_needed"] = (
        [item for item in SETUP_ITEMS if item in not_needed] if isinstance(not_needed, list) else []
    )
    tips = document.get("tips_enabled")
    document["tips_enabled"] = tips if isinstance(tips, bool) else True
    return document


def get_home_preferences(user_key: str, *, db_path: Path | None = None) -> dict[str, Any]:
    """Read preferences; missing or malformed known fields read as their defaults."""

    return _normalized_document(get_state_meta(preferences_key(user_key), db_path=db_path))


def set_home_preferences(
    user_key: str,
    updates: Mapping[str, Any],
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Merge the given known fields, preserving the rest.

    ``setup_not_needed`` only ever adds: an item one device marks as not needed
    stays so whatever another device sends at the same time.
    """

    if not isinstance(updates, Mapping):
        raise ValueError("preferences must be an object")
    for field in DATE_FIELDS:
        if field in updates and updates[field] is not None:
            value = updates[field]
            if not isinstance(value, str) or not _DATE.match(value):
                raise ValueError(f"{field} must be a YYYY-MM-DD date or null")
    if "setup_not_needed" in updates:
        items = updates["setup_not_needed"]
        if not isinstance(items, list) or any(item not in SETUP_ITEMS for item in items):
            raise ValueError(f"setup_not_needed must list items from {', '.join(SETUP_ITEMS)}")
    if "tips_enabled" in updates and not isinstance(updates["tips_enabled"], bool):
        raise ValueError("tips_enabled must be a JSON boolean")

    def merge(raw: Any) -> dict[str, Any]:
        document = _normalized_document(raw)
        for field in DATE_FIELDS:
            if field in updates:
                document[field] = updates[field]
        if "setup_not_needed" in updates:
            added = set(document["setup_not_needed"]) | set(updates["setup_not_needed"])
            document["setup_not_needed"] = [item for item in SETUP_ITEMS if item in added]
        if "tips_enabled" in updates:
            document["tips_enabled"] = updates["tips_enabled"]
        return document

    return update_state_meta(preferences_key(user_key), merge, db_path=db_path)


def public_home_preferences(preferences: Mapping[str, Any]) -> dict[str, Any]:
    """The stable API shape, without reserved future fields."""

    document = _normalized_document(dict(preferences))
    return {
        "setup_later_on": document["setup_later_on"],
        "setup_not_needed": document["setup_not_needed"],
        "tip_hidden_on": document["tip_hidden_on"],
        "tips_enabled": document["tips_enabled"],
    }
