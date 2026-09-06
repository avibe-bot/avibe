"""Render execution-only metadata without changing canonical message content."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any


# Released wire titles must remain recognizable even if translations change.
_LEGACY_ATTACHMENT_ERROR_TITLES = (
    "Attachment Download Errors",
    "\u9644\u4ef6\u4e0b\u8f7d\u9519\u8bef",
)


def _matches_original_body(text: str, original: str) -> bool:
    if text == original:
        return True
    blocks = tuple(f"[{title}]\n" for title in _LEGACY_ATTACHMENT_ERROR_TITLES)
    if text.startswith(original + "\n\n"):
        return text[len(original) + 2 :].startswith(blocks)
    return not original.strip() and text.startswith(blocks)


def sanitize_identity(value: str) -> str:
    token = (value or "").replace("\n", " ").replace("\r", " ").strip()
    token = token.replace("[", "(").replace("]", ")").replace("<", "(").replace(">", ")")
    return token[:80] or "unknown"


def without_legacy_metadata(text: str, *, original: str, user_id: str) -> str:
    """Remove a released IM prefix only when the immutable body proves its extent."""
    if _matches_original_body(text, original):
        return text
    time_line = r"\[Current Time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC[+-]\d{2}:\d{2}\]\n"
    identity_line = rf"\[[^\[\]<>\n]{{1,80}}<{re.escape(sanitize_identity(user_id))}>\]\n"
    # A user-authored first line may look like metadata. The immutable body,
    # not a greedy match, decides which released prefix was actually added.
    for prefix in (time_line + identity_line, time_line, identity_line):
        match = re.match(prefix, text)
        if match is not None:
            body = text[match.end() :]
            if _matches_original_body(body, original):
                return body
    return text


@dataclass(frozen=True)
class AgentInputMetadata:
    """Stable sender facts; the clock and display switches are evaluated on send."""

    user_id: str | None = None
    user_name: str | None = None
    source_session_id: str | None = None

    def render(self, text: str, config: Any, *, now: datetime | None = None) -> str:
        lines: list[str] = []
        if getattr(config, "include_time_info", True):
            current = now or datetime.now().astimezone()
            if current.tzinfo is None:
                current = current.astimezone()
            offset = current.strftime("%z")
            if len(offset) == 5:
                offset = f"{offset[:3]}:{offset[3:]}"
            lines.append(f"[Now: {current.strftime('%Y-%m-%d %H:%M:%S')} UTC{offset}]")
        if self.source_session_id:
            lines.append(f"From: #{sanitize_identity(self.source_session_id)}")
        if self.user_id and getattr(config, "include_user_info", True):
            name = sanitize_identity(self.user_name or self.user_id)
            lines.append(f"[{name}<{sanitize_identity(self.user_id)}>]")
        return "\n".join([*lines, text]) if lines else text
