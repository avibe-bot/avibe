"""Shared, deliberately small grammar for direct Memory read commands."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal


MAX_MEMORY_COMMAND_QUERY_BYTES = 8 * 1024
# Discord's plain-message limit is the smallest relevant private-IM ceiling.
# Count UTF-8 bytes here: a 1900-byte string is also at most 1900 code points,
# leaving room below its 2000-character maximum on every supported transport.
MAX_INERT_MEMORY_REPLY_BYTES = 1900


MemoryCommandAction = Literal["help", "status", "profile", "search", "invalid"]


@dataclass(frozen=True)
class MemoryCommand:
    """A parsed ``/memory`` command with no transport-specific behavior."""

    action: MemoryCommandAction
    query: str | None = None
    error: str | None = None


def is_memory_command_candidate(text: object) -> bool:
    """Return whether text starts with the exact ``/memory`` command token."""

    normalized = _normalize_memory_command_text(text)
    if normalized is None:
        return False
    stripped = normalized.strip()
    return stripped == "/memory" or (
        stripped.startswith("/memory")
        and len(stripped) > len("/memory")
        and stripped[len("/memory")].isspace()
    )


def parse_memory_command(text: object) -> MemoryCommand | None:
    """Parse the supported, text-only Memory read grammar.

    Supported forms are deliberately closed: ``/memory``, ``/memory help``,
    ``/memory status``, ``/memory profile``, and ``/memory search <query>``.
    Unknown verbs are returned as invalid rather than delegated to an agent.
    """

    normalized = _normalize_memory_command_text(text)
    if normalized is None or not is_memory_command_candidate(normalized):
        return None

    stripped = normalized.strip()
    tokens = stripped.split(maxsplit=2)
    if len(tokens) == 1:
        return MemoryCommand("help")

    verb = tokens[1].lower()
    if verb in {"help", "status", "profile"}:
        if len(tokens) != 2:
            return MemoryCommand("invalid", error="memory_invalid_input")
        return MemoryCommand(verb)
    if verb == "search":
        if len(tokens) != 3 or not tokens[2].strip() or len(tokens[2].encode("utf-8")) > MAX_MEMORY_COMMAND_QUERY_BYTES:
            return MemoryCommand("invalid", error="memory_invalid_input")
        return MemoryCommand("search", query=tokens[2].strip())
    return MemoryCommand("invalid", error="memory_invalid_input")


def bounded_inert_text(value: object, *, max_bytes: int = MAX_INERT_MEMORY_REPLY_BYTES) -> str:
    """Produce bounded plain text safe for an inert IM command response."""

    text = value if isinstance(value, str) else str(value)
    try:
        text = unicodedata.normalize("NFC", text)
    except (TypeError, UnicodeError, ValueError):
        text = ""
    # Preserve only layout controls required for plain text. In particular,
    # discard bidi/format controls and terminal escapes before any adapter sees
    # provider-derived content.
    text = "".join(
        char
        for char in text
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    text = text.replace("@", "[at]").replace("<", "[").replace(">", "]")
    text = text.replace("://", ": //")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return text
    suffix = b"..."
    clipped = encoded[: max(0, max_bytes - len(suffix))].decode("utf-8", "ignore").rstrip()
    return f"{clipped}..."


def _normalize_memory_command_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    except (TypeError, UnicodeError, ValueError):
        return None
