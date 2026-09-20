"""Resolve the concrete outbound destination carried by a message context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from modules.im import MessageContext


MessageKind = Literal[
    "original",
    "quick_reply",
    "forwarded",
    "edited",
    "system",
    "unknown",
]
MESSAGE_KINDS = frozenset(
    {"original", "quick_reply", "forwarded", "edited", "system", "unknown"}
)


def normalize_message_kind(value: object) -> MessageKind:
    """Normalize untrusted or legacy message-kind input fail closed."""

    normalized = str(value or "").strip()
    return cast(MessageKind, normalized if normalized in MESSAGE_KINDS else "unknown")


def routed_delivery_context(context: MessageContext) -> MessageContext:
    """Apply a scheduled/Harness delivery override without losing Turn lineage."""

    payload = dict(context.platform_specific or {})
    override = payload.get("delivery_override")
    if not isinstance(override, dict):
        return context

    payload["is_dm"] = override.get("is_dm", payload.get("is_dm", False))
    from modules.im import MessageContext

    return MessageContext(
        user_id=str(override.get("user_id") or context.user_id),
        channel_id=str(override.get("channel_id") or context.channel_id),
        platform=override.get("platform") or context.platform,
        thread_id=override.get("thread_id"),
        message_id=context.message_id,
        platform_specific=payload,
        files=context.files,
        is_original_human_text=context.is_original_human_text,
        is_original_human_attachment=context.is_original_human_attachment,
        message_kind=context.message_kind,
    )
