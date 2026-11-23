"""Bounded, presentation-only terminal facts for a human input."""

from collections.abc import Mapping
from typing import Any

TERMINALS = frozenset({"iphone_app", "mac_app", "browser", "unknown"})
SURFACES = frozenset({"main", "mini", "notification_reply", "ask_vibey"})
IM_PLATFORMS = frozenset({"slack", "discord", "telegram", "feishu", "wechat"})


def source_label(metadata: Any, platform: str | None = None) -> str:
    """Render only enums; neither client prose nor a device identifier can enter the prompt."""
    if platform in IM_PLATFORMS:
        return f"im:{platform}"
    source = metadata.get("terminal_source") if isinstance(metadata, Mapping) else None
    source = source if isinstance(source, Mapping) else {}
    terminal = source.get("type")
    terminal = terminal if isinstance(terminal, str) and terminal in TERMINALS else "unknown"
    parts = [terminal]
    surface = source.get("surface")
    if terminal != "unknown" and isinstance(surface, str) and surface in SURFACES:
        parts.append(f"surface={surface}")
    if terminal != "unknown" and source.get("hint") is True:
        parts.append("evidence=hint")
    return "; ".join(parts)
