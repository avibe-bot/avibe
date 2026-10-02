"""Output caps for tool results (C-7 section 1).

Two independent limits, whichever is hit first: ``MAX_LINES`` lines and
``MAX_BYTES`` UTF-8 bytes. ``read`` keeps the head, ``bash`` keeps the tail.

Ported from Pi ``packages/coding-agent/src/core/tools/truncate.ts`` (MIT,
Copyright (c) 2025 Mario Zechner).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Optional

from core.agent_core.tools.base import MAX_BYTES, MAX_LINES

TruncatedBy = Literal["lines", "bytes"]


@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: Optional[TruncatedBy]
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    # Tail truncation only: the kept text starts inside the last line.
    last_line_partial: bool
    # Head truncation only: the first line alone is over the byte limit.
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int

    def to_details(self) -> dict[str, Any]:
        """The display form for ``ToolResult.details``, without the content itself."""
        out = asdict(self)
        del out["content"]
        return out


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8", "surrogatepass"))


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def split_lines_for_counting(content: str) -> list[str]:
    """Lines of ``content``; a trailing newline does not start another line."""
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def _untruncated(content: str, total_lines: int, total_bytes: int, max_lines: int, max_bytes: int) -> TruncationResult:
    return TruncationResult(
        content=content,
        truncated=False,
        truncated_by=None,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=total_lines,
        output_bytes=total_bytes,
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_head(content: str, *, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> TruncationResult:
    """Keep the first lines that fit. Never returns a partial line."""
    total_bytes = utf8_len(content)
    lines = split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _untruncated(content, total_lines, total_bytes, max_lines, max_bytes)

    if utf8_len(lines[0]) > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    kept: list[str] = []
    kept_bytes = 0
    truncated_by: TruncatedBy = "lines"
    for index, line in enumerate(lines[:max_lines]):
        line_bytes = utf8_len(line) + (1 if index > 0 else 0)
        if kept_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        kept.append(line)
        kept_bytes += line_bytes
    if len(kept) >= max_lines and kept_bytes <= max_bytes:
        truncated_by = "lines"

    output = "\n".join(kept)
    return TruncationResult(
        content=output,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(kept),
        output_bytes=utf8_len(output),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(content: str, *, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> TruncationResult:
    """Keep the last lines that fit; only a single over-long last line is cut mid-line."""
    total_bytes = utf8_len(content)
    lines = split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _untruncated(content, total_lines, total_bytes, max_lines, max_bytes)

    kept: list[str] = []
    kept_bytes = 0
    truncated_by: TruncatedBy = "lines"
    last_line_partial = False
    for line in reversed(lines):
        if len(kept) >= max_lines:
            break
        line_bytes = utf8_len(line) + (1 if kept else 0)
        if kept_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not kept:
                partial = truncate_string_to_bytes_from_end(line, max_bytes)
                kept.insert(0, partial)
                kept_bytes = utf8_len(partial)
                last_line_partial = True
            break
        kept.insert(0, line)
        kept_bytes += line_bytes
    if len(kept) >= max_lines and kept_bytes <= max_bytes:
        truncated_by = "lines"

    output = "\n".join(kept)
    return TruncationResult(
        content=output,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(kept),
        output_bytes=utf8_len(output),
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_string_to_bytes_from_end(value: str, max_bytes: int) -> str:
    """The longest suffix of ``value`` within ``max_bytes``, cut at a character boundary."""
    data = value.encode("utf-8", "surrogatepass")
    if len(data) <= max_bytes:
        return value
    start = len(data) - max_bytes
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:].decode("utf-8", "replace")
