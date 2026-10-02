"""The ``read`` tool (C-7 section 2).

Ported from Pi ``packages/coding-agent/src/core/tools/read.ts`` and
``src/utils/mime.ts`` (MIT, Copyright (c) 2025 Mario Zechner); the description
and every model-facing string are Pi's unless marked as Avibe's.

Unlike Pi, the file is streamed: only the lines that can reach the model are
kept in memory, so reading a multi-gigabyte log cannot exhaust the process the
loop shares with the rest of Avibe. The result is the same as Pi's.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import struct
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

from core.agent_core.messages import IMAGE_MIME_TYPES, ImageBlock, TextBlock
from core.agent_core.tools.args import (
    ToolInputError,
    error_result,
    format_number,
    optional_line_arg,
    os_error_text,
    str_arg,
    text_result,
)
from core.agent_core.tools.base import MAX_BYTES, MAX_LINES, ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.paths import resolve_read_path
from core.agent_core.tools.truncate import format_size, truncate_head

#: Stores an image for the transcript and returns its media token: ``(data, mime_type, name)``.
ImageSink = Callable[[bytes, str, str], Union[str, Awaitable[str]]]

# Pi keeps images under 4.5MB of base64 payload, below Anthropic's 5MB limit. Avibe v1 does not
# resize, so the cap applies to the file as it is.
MAX_INLINE_IMAGE_BYTES = int(4.5 * 1024 * 1024) * 3 // 4
_IMAGE_SNIFF_BYTES = 4100
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_READ_CHUNK_BYTES = 1024 * 1024

READ_DESCRIPTION = (
    "Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as "
    f"attachments. For text files, output is truncated to {MAX_LINES} lines or {MAX_BYTES // 1024}KB (whichever is "
    "hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete."
)

READ_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["path"],
    "properties": {
        "path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
        "offset": {"type": "integer", "minimum": 1, "description": "Line number to start reading from (1-indexed)"},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum number of lines to read"},
    },
}


class ReadTool:
    def __init__(self, *, image_sink: Optional[ImageSink] = None) -> None:
        self._image_sink = image_sink
        self._spec = ToolSpec(name="read", description=READ_DESCRIPTION, input_schema=READ_SCHEMA)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.cancel.cancelled:
            return error_result("Operation aborted")
        try:
            path = str_arg(arguments, "path")
            offset = optional_line_arg(arguments, "offset")
            limit = optional_line_arg(arguments, "limit")
        except ToolInputError as exc:
            return error_result(str(exc))

        try:
            absolute = resolve_read_path(path, ctx.cwd)
            mime_type = await asyncio.to_thread(_sniff_image, absolute)
            if mime_type is not None:
                return await self._read_image(absolute, mime_type)
            # Off the event loop, which every Session and surface shares; the scan stops on cancel.
            return await asyncio.to_thread(_read_text, absolute, path, offset, limit, lambda: ctx.cancel.cancelled)
        except _Aborted:
            return error_result("Operation aborted")
        except OSError as exc:
            return error_result(os_error_text(exc))
        except ToolInputError as exc:
            return error_result(str(exc))

    async def _read_image(self, absolute: str, mime_type: str) -> ToolResult:
        header = f"Read image file [{mime_type}]"
        details = {"path": absolute, "mime_type": mime_type}
        if mime_type not in IMAGE_MIME_TYPES:
            # Pi converts other formats to PNG; without an image library Avibe cannot.
            return text_result(
                f"{header}\n[Image omitted: could not be converted to a supported inline image format.]",
                details=details,
            )
        size = os.path.getsize(absolute)
        if size > MAX_INLINE_IMAGE_BYTES:
            # Avibe: no resizing in v1.
            return text_result(
                f"{header}\n[Image omitted: the file is {format_size(size)}, over the "
                f"{format_size(MAX_INLINE_IMAGE_BYTES)} inline image limit. Images are not resized.]",
                details={**details, "bytes": size},
            )
        if self._image_sink is None:
            return text_result(f"{header}\n[Image omitted: image attachments are not available.]", details=details)
        data = await asyncio.to_thread(_read_bytes, absolute)
        name = os.path.basename(absolute)
        token = self._image_sink(data, mime_type, name)
        if inspect.isawaitable(token):
            token = await token
        return ToolResult(
            content=(TextBlock(text=header), ImageBlock(mime_type=mime_type, media_token=token, name=name)),
            details={**details, "bytes": len(data)},
        )


class _Aborted(Exception):
    pass


def _sniff_image(path: str) -> Optional[str]:
    """The image type to attach, or ``None`` for text. A PNG or JPEG Pi cannot send (animated PNG,
    JPEG-LS) keeps its family, so it is omitted rather than decoded as text."""
    with open(path, "rb") as handle:
        head = handle.read(_IMAGE_SNIFF_BYTES)
    supported = detect_supported_image_mime_type(head)
    if supported is not None:
        return supported
    if head.startswith(_PNG_SIGNATURE):
        return "image/apng" if _is_png(head) else None
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jls"
    return None


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _read_text(
    absolute: str, path: str, offset: Optional[int], limit: Optional[int], cancelled: Callable[[], bool]
) -> ToolResult:
    start_line = offset - 1 if offset is not None else 0
    stop_line = start_line + limit if limit is not None else None
    collected, total_lines, first_line_bytes = _scan_lines(absolute, start_line, stop_line, cancelled)
    if start_line >= total_lines:
        raise ToolInputError(f"Offset {offset} is beyond end of file ({total_lines} lines total)")

    truncation = truncate_head("\n".join(collected))
    start_display = start_line + 1
    details: dict[str, Any] = {"path": absolute}
    if truncation.first_line_exceeds_limit:
        text = (
            f"[Line {start_display} is {format_size(first_line_bytes)}, exceeds {format_size(MAX_BYTES)} limit. "
            f"Use bash: sed -n '{start_display}p' {shlex.quote(path)} | head -c {MAX_BYTES}]"
        )
        details["truncation"] = truncation.to_details()
    elif truncation.truncated:
        end_display = start_display + truncation.output_lines - 1
        next_offset = end_display + 1
        if truncation.truncated_by == "lines":
            notice = (
                f"[Showing lines {start_display}-{end_display} of {total_lines}. Use offset={next_offset} to continue.]"
            )
        else:
            notice = (
                f"[Showing lines {start_display}-{end_display} of {total_lines} ({format_size(MAX_BYTES)} limit). "
                f"Use offset={next_offset} to continue.]"
            )
        text = f"{truncation.content}\n\n{notice}"
        details["truncation"] = truncation.to_details()
    elif stop_line is not None and stop_line < total_lines:
        text = f"{truncation.content}\n\n[{total_lines - stop_line} more lines in file. Use offset={stop_line + 1} to continue.]"
    else:
        text = truncation.content
    return text_result(text, details=details)


def _scan_lines(
    path: str, start: int, stop: Optional[int], cancelled: Callable[[], bool] = lambda: False
) -> tuple[list[str], int, int]:
    """Lines ``[start, stop)`` as Pi's ``text.split("\\n")`` gives them, the file's line count, and the
    raw size of line ``start``.

    Collection stops once the kept lines pass either cap, so the kept prefix exceeds a cap exactly when
    the whole selection does: decoding with replacement never makes text shorter than its bytes, and
    ``\\n`` never occurs inside a UTF-8 sequence, so lines decode independently.
    """
    lines: list[bytearray] = []
    kept = 0  # raw bytes of "\\n".join(lines)
    opened = -1  # file line index of lines[-1]
    index = 0  # file line index of the bytes being read
    first_line_bytes = 0
    collecting = stop is None or start < stop

    def collect(segment: bytes) -> None:
        nonlocal kept, opened, collecting
        if opened != index:
            if lines:
                kept += 1
            lines.append(bytearray())
            opened = index
        room = MAX_BYTES + 1 - kept
        if room > 0:
            lines[-1] += segment[:room]
            kept += min(len(segment), room)
        # One line beyond the cap: a final empty piece does not count as a line.
        if kept > MAX_BYTES or len(lines) > MAX_LINES + 1:
            collecting = False

    def count_rest(chunk: bytes, pos: int) -> None:
        nonlocal index, first_line_bytes
        if index == start:
            newline = chunk.find(b"\n", pos)
            first_line_bytes += (len(chunk) if newline == -1 else newline) - pos
        index += chunk.count(b"\n", pos)

    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK_BYTES):
            if cancelled():
                raise _Aborted
            pos = 0
            while collecting:
                newline = chunk.find(b"\n", pos)
                end = len(chunk) if newline == -1 else newline
                if stop is not None and index >= stop:
                    collecting = False
                    continue
                if index == start:
                    first_line_bytes += end - pos
                if index >= start:
                    collect(chunk[pos:end])
                if newline == -1:
                    pos = len(chunk)
                    break
                index += 1
                pos = newline + 1
            else:
                count_rest(chunk, pos)
    if collecting and opened != index and index >= start:
        # An empty file: its one line is empty.
        lines.append(bytearray())
    return [bytes(line).decode("utf-8", "replace") for line in lines], index + 1, first_line_bytes


def detect_supported_image_mime_type(data: bytes) -> Optional[str]:
    """Pi's content sniffing: JPEG, static PNG, GIF, WebP, and BMP."""
    if data.startswith(b"\xff\xd8\xff"):
        return None if len(data) > 3 and data[3] == 0xF7 else "image/jpeg"
    if data.startswith(_PNG_SIGNATURE):
        return "image/png" if _is_png(data) and not _is_animated_png(data) else None
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM") and _is_bmp(data):
        return "image/bmp"
    return None


def _is_png(data: bytes) -> bool:
    return len(data) >= 16 and struct.unpack(">I", data[8:12])[0] == 13 and data[12:16] == b"IHDR"


def _is_animated_png(data: bytes) -> bool:
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        if kind == b"acTL":
            return True
        if kind == b"IDAT":
            return False
        following = offset + 8 + length + 4
        if following <= offset or following > len(data):
            return False
        offset = following
    return False


def _is_bmp(data: bytes) -> bool:
    if len(data) < 26:
        return False
    (declared_size,) = struct.unpack("<I", data[2:6])
    (pixel_offset,) = struct.unpack("<I", data[10:14])
    (dib_size,) = struct.unpack("<I", data[14:18])
    if declared_size != 0 and declared_size < 26:
        return False
    if pixel_offset < 14 + dib_size:
        return False
    if declared_size != 0 and pixel_offset >= declared_size:
        return False
    if dib_size == 12:
        planes, bits = struct.unpack("<HH", data[22:26])
    elif 40 <= dib_size <= 124:
        if len(data) < 30:
            return False
        planes, bits = struct.unpack("<HH", data[26:30])
    else:
        return False
    return planes == 1 and bits in (1, 4, 8, 16, 24, 32)
