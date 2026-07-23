"""Normalize native IM events into platform-agnostic inbound message facts."""

from __future__ import annotations

from typing import Any, Optional

from .base import FileAttachment


def is_ordinary_slack_text(event: dict[str, Any], files: Optional[list[FileAttachment]]) -> bool:
    subtype = event.get("subtype")
    return not any(
        (
            files,
            event.get("files"),
            event.get("attachments"),
            event.get("edited"),
            event.get("bot_id"),
            event.get("blocks"),
            event.get("rich_text"),
            event.get("forwarded"),
            event.get("is_system"),
            event.get("system"),
            event.get("type") in {"system", "system_message"},
            subtype
            in {
                "bot_message",
                "file_share",
                "message_changed",
                "message_deleted",
                "message_replied",
                "channel_join",
                "channel_leave",
            },
        )
    )


def is_ordinary_discord_text(message: Any, files: Optional[list[FileAttachment]]) -> bool:
    try:
        is_system = message.is_system() if callable(getattr(message, "is_system", None)) else False
    except Exception:
        return False
    flags = getattr(message, "flags", None)
    return not any(
        (
            files,
            bool(getattr(getattr(message, "author", None), "bot", False)),
            getattr(message, "edited_at", None) is not None,
            getattr(message, "attachments", None),
            getattr(message, "embeds", None),
            bool(getattr(flags, "forwarded", False)),
            getattr(message, "message_snapshots", None),
            is_system,
        )
    )


def is_ordinary_telegram_text(message: dict[str, Any], files: list[FileAttachment]) -> bool:
    sender = message.get("from") or {}
    return not any(
        (
            files,
            sender.get("is_bot") is True,
            message.get("edit_date"),
            message.get("forward_origin"),
            message.get("forward_from"),
            message.get("is_system"),
            message.get("system"),
            message.get("type") in {"system", "system_message"},
        )
    )


def is_ordinary_feishu_text(
    event: dict[str, Any],
    files: Optional[list[FileAttachment]],
    *,
    shared_text: Optional[str],
) -> bool:
    sender = event.get("sender") or {}
    message = event.get("message") or {}
    return (
        sender.get("sender_type") != "app"
        and message.get("message_type") == "text"
        and not files
        and not shared_text
        and not any(message.get(key) for key in ("file", "image", "media", "edited", "forwarded"))
    )


def is_ordinary_wechat_text(msg: dict[str, Any], files: Optional[list[FileAttachment]]) -> bool:
    items = msg.get("item_list") or []
    return (
        bool(items)
        and all(
            isinstance(item, dict)
            and item.get("type") in (1, "TEXT", "text")
            and not item.get("ref_msg")
            for item in items
        )
        and not files
        and not any(msg.get(key) for key in ("is_system", "system", "edited", "forwarded"))
    )
