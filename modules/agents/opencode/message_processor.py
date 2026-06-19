"""Message processing helpers for OpenCode agent."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping

logger = logging.getLogger(__name__)


def extract_opencode_response_text(
    response: Mapping[str, Any],
    *,
    allow_non_text_fallback: bool = False,
) -> str:
    """Extract user-visible assistant text from an OpenCode message."""
    parts = response.get("parts", [])
    text_parts: list[str] = []
    fallback_parts: list[str] = []

    if not isinstance(parts, list):
        return ""

    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        cleaned = text.strip()
        if part.get("type") == "text":
            text_parts.append(cleaned)
        else:
            fallback_parts.append(cleaned)

    if text_parts:
        return "\n\n".join(text_parts).strip()
    if allow_non_text_fallback:
        return "\n\n".join(fallback_parts).strip()
    return ""


def is_empty_terminal_opencode_message(message: Mapping[str, Any]) -> bool:
    """Return True when OpenCode completed without usable text or error."""

    info = message.get("info")
    if not isinstance(info, Mapping):
        return False
    if info.get("role") != "assistant":
        return False
    time_info = info.get("time")
    if not isinstance(time_info, Mapping) or not time_info.get("completed"):
        return False
    if info.get("error"):
        return False
    if info.get("finish") == "tool-calls":
        return False
    if extract_opencode_response_text(message, allow_non_text_fallback=True):
        return False

    parts = message.get("parts")
    if not isinstance(parts, list):
        return True
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        part_type = part.get("type")
        if part_type in {"step-start", "step-finish"}:
            continue
        text = part.get("text")
        if part_type == "text":
            if not isinstance(text, str) or not text.strip():
                continue
            return False
        if isinstance(text, str) and not text.strip():
            continue
        return False
    return True


class OpenCodeMessageProcessorMixin:
    """Pure-ish helpers that depend only on instance config."""

    def _extract_response_text(self, response: Dict[str, Any]) -> str:
        text = extract_opencode_response_text(response)
        parts = response.get("parts", [])

        if not text and isinstance(parts, list) and parts:
            part_types = [p.get("type") for p in parts if isinstance(p, dict)]
            msg_id = response.get("info", {}).get("id", "unknown")
            logger.info(
                "OpenCode message %s has no extractable text; part types: %s",
                msg_id,
                part_types,
            )

        return text


    def _to_relative_path(self, abs_path: str, cwd: str) -> str:
        """Convert absolute file paths to relative paths under cwd."""

        try:
            abs_path = os.path.abspath(os.path.expanduser(abs_path))
            cwd = os.path.abspath(os.path.expanduser(cwd))
            rel_path = os.path.relpath(abs_path, cwd)
            if rel_path.startswith("../.."):  # outside workspace
                return abs_path
            if not rel_path.startswith(".") and rel_path != ".":
                rel_path = "./" + rel_path
            return rel_path
        except Exception:
            return abs_path
