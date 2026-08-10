"""Resolve the concrete outbound destination carried by a message context."""

from __future__ import annotations

from modules.im import MessageContext


def routed_delivery_context(context: MessageContext) -> MessageContext:
    """Apply a scheduled/Harness delivery override without losing Turn lineage."""

    payload = dict(context.platform_specific or {})
    override = payload.get("delivery_override")
    if not isinstance(override, dict):
        return context

    payload["is_dm"] = override.get("is_dm", payload.get("is_dm", False))
    return MessageContext(
        user_id=str(override.get("user_id") or context.user_id),
        channel_id=str(override.get("channel_id") or context.channel_id),
        platform=override.get("platform") or context.platform,
        thread_id=override.get("thread_id"),
        message_id=context.message_id,
        platform_specific=payload,
        files=context.files,
        is_ordinary_text=context.is_ordinary_text,
    )
