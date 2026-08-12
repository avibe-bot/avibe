from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol

from core.reply_enhancer import strip_quick_reply_buttons
from .types import BackendSessionTitle, NativeResumeSession

logger = logging.getLogger(__name__)
EDGE_SYMBOLS = " \t\r\n`*_~'\"“”‘’.,!?！？。，、：:;；-—…()（）[]【】<>《》「」『』{}|/\\"


class NativeSessionProvider(Protocol):
    agent_name: str

    def list_metadata(self, working_path: str) -> list[NativeResumeSession]:
        """Return lightweight session metadata for a working path."""

    def hydrate_preview(self, item: NativeResumeSession) -> NativeResumeSession:
        """Fill in the assistant preview text for one item."""

    def get_title(
        self,
        *,
        native_session_id: str,
        working_path: str,
        first_user_message: str = "",
    ) -> BackendSessionTitle | None:
        """Return a backfillable title for a native session, if available."""


def normalize_preview_text(text: str, *, strip_quick_replies: bool = True) -> str:
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    if strip_quick_replies:
        cleaned = strip_quick_reply_buttons(cleaned)
    if "\n---\n" in cleaned:
        cleaned = cleaned.split("\n---\n", 1)[0]
    lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
    return " ".join(lines).strip()


def normalize_title_text(text: str, *, limit: int | None = None) -> str:
    # Titles come from user prompts; button-like lines are content there, not
    # assistant controls to strip.
    cleaned = normalize_preview_text(text, strip_quick_replies=False)
    if not cleaned:
        return ""
    return cleaned[:limit] if limit is not None else cleaned


def normalize_multiline_preview_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = strip_quick_reply_buttons(cleaned)
    if "\n---\n" in cleaned:
        cleaned = cleaned.split("\n---\n", 1)[0]

    normalized_lines: list[str] = []
    previous_blank = False
    for raw_line in cleaned.split("\n"):
        line = raw_line.strip()
        if not line:
            if normalized_lines and not previous_blank:
                normalized_lines.append("")
            previous_blank = True
            continue
        normalized_lines.append(line)
        previous_blank = False

    while normalized_lines and not normalized_lines[0]:
        normalized_lines.pop(0)
    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()

    return "\n".join(normalized_lines).strip()


def trim_edge_symbols(text: str) -> str:
    return (text or "").strip(EDGE_SYMBOLS)


def build_trailing_excerpt(
    text: str,
    limit: int,
    *,
    prefix_ellipsis: bool = True,
    strip_quick_replies: bool = True,
) -> str:
    cleaned = normalize_preview_text(text, strip_quick_replies=strip_quick_replies)
    if not cleaned:
        return ""
    excerpt = cleaned if len(cleaned) <= limit else cleaned[-limit:]
    excerpt = trim_edge_symbols(excerpt) or excerpt.strip()
    if not excerpt:
        return ""
    if len(cleaned) <= limit:
        return excerpt
    return f"...{excerpt}" if prefix_ellipsis else excerpt


def build_tail_preview(
    text: str,
    limit: int = 15,
    *,
    strip_quick_replies: bool = True,
) -> str:
    return build_trailing_excerpt(
        text,
        limit,
        prefix_ellipsis=True,
        strip_quick_replies=strip_quick_replies,
    )


def build_resume_preview(text: str, limit: int = 200) -> str:
    cleaned = normalize_multiline_preview_text(text)
    if not cleaned:
        return ""
    excerpt = cleaned if len(cleaned) <= limit else cleaned[-limit:]
    excerpt = excerpt.strip()
    if len(cleaned) > limit:
        excerpt = excerpt.lstrip()
        return f"...{excerpt}" if excerpt else ""
    return excerpt


def parse_json_blob(blob: str) -> dict:
    try:
        value = json.loads(blob)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def ts_seconds(value: int | float | str | None, *, millis: bool = False) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000.0 if millis else number


def dt_from_ts(value: int | float | str | None, *, millis: bool = False) -> datetime | None:
    seconds = ts_seconds(value, millis=millis)
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds)


def read_json_lines(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except Exception:
                continue
            if isinstance(value, dict):
                rows.append(value)
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("Failed to read jsonl %s: %s", path, exc)
    return rows
