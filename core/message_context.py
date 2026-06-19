"""Shared helpers for MessageContext-derived metadata."""

from __future__ import annotations

from typing import Optional

from modules.im import MessageContext


def resolve_context_platform(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
    default: str = "",
) -> str:
    """Resolve a MessageContext platform using the common precedence order."""
    platform = fallback_platform or default
    if context is not None:
        payload = context.platform_specific or {}
        platform = context.platform or payload.get("platform") or platform
    return str(platform or default)


def resolve_context_settings_key(context: MessageContext) -> str:
    payload = context.platform_specific or {}
    value = context.user_id if payload.get("is_dm", False) else context.channel_id
    return str(value or "")


def requires_typed_user_session_key(context: MessageContext) -> bool:
    payload = context.platform_specific or {}
    return bool(payload.get("is_dm", False) and context.user_id and context.channel_id == context.user_id)


def build_context_session_key(
    context: MessageContext,
    *,
    platform: Optional[str] = None,
    settings_key: Optional[str] = None,
    fallback_platform: Optional[str] = None,
) -> str:
    resolved_platform = platform or resolve_context_platform(context, fallback_platform=fallback_platform)
    resolved_settings_key = settings_key if settings_key is not None else resolve_context_settings_key(context)
    if requires_typed_user_session_key(context):
        return f"{resolved_platform}::user::{resolved_settings_key}"
    return f"{resolved_platform}::{resolved_settings_key}"
