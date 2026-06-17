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
