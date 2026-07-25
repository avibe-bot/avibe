"""Normalize native IM events into platform-agnostic inbound message facts."""

from __future__ import annotations

from typing import Any, Optional

from .base import FileAttachment


# Slack's WYSIWYG composer emits a ``rich_text`` block for every message a human
# sends from a modern client, so the mere presence of ``blocks`` carries no
# signal about whether the event is ordinary text. Classification therefore
# inspects block CONTENT: only composer-shaped rich text is ordinary, and any
# other or unrecognized shape (app-authored layout blocks, media, interactive
# elements, future element types) fails closed.
_SLACK_RICH_TEXT_CONTAINERS = frozenset(
    {
        "rich_text_section",
        "rich_text_list",
        "rich_text_quote",
        "rich_text_preformatted",
    }
)
_SLACK_RICH_TEXT_LEAVES = frozenset(
    {
        "text",
        "link",
        "emoji",
        "user",
        "usergroup",
        "channel",
        "team",
        "date",
        "broadcast",
        "color",
    }
)


def _is_composer_rich_text_element(element: Any) -> bool:
    """Return True when one rich-text node is plain composer output."""

    if not isinstance(element, dict):
        return False
    element_type = element.get("type")
    if element_type in _SLACK_RICH_TEXT_LEAVES:
        return True
    if element_type not in _SLACK_RICH_TEXT_CONTAINERS:
        return False
    children = element.get("elements")
    if not isinstance(children, list):
        return False
    return all(_is_composer_rich_text_element(child) for child in children)


def is_plain_slack_composer_blocks(blocks: Any) -> bool:
    """Return True when ``blocks`` only contains text a human could have typed.

    An absent or empty array is the pre-Block-Kit client shape and stays
    ordinary. Anything else must be exclusively ``rich_text`` blocks whose
    nested elements are all recognized text nodes.
    """

    if not blocks:
        return True
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "rich_text":
            return False
        elements = block.get("elements")
        if not isinstance(elements, list):
            return False
        if not all(_is_composer_rich_text_element(element) for element in elements):
            return False
    return True


def is_ordinary_slack_text(event: dict[str, Any], files: Optional[list[FileAttachment]]) -> bool:
    subtype = event.get("subtype")
    return not any(
        (
            files,
            event.get("files"),
            event.get("attachments"),
            event.get("edited"),
            event.get("bot_id"),
            not is_plain_slack_composer_blocks(event.get("blocks")),
            # Not a documented Slack message field. Retained as a fail-closed
            # catch-all for any payload carrying rich text outside ``blocks``.
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


def is_ordinary_workbench_text(payload: object, quick_reply_for: object) -> bool:
    """Classify a Workbench submit the same way the IM adapters classify events.

    The Workbench is a surface like any other here: a quick-reply click, an
    upload, or forwarded metadata is not ordinary human text.
    """

    if not isinstance(payload, dict) or quick_reply_for or payload.get("files"):
        return False
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and any(
        metadata.get(key)
        for key in ("forwarded", "is_forwarded", "forward_origin", "forwarded_from")
    ):
        return False
    return True


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
