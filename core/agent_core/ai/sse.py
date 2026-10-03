"""Incremental Server-Sent Events parsing.

The parser follows the event-stream grammar from the W3C/WHATWG SSE
specification. It deliberately accepts bytes because an HTTP transport may
split a UTF-8 code point, a CRLF pair, or a ``data:`` field at any boundary.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Iterable


DEFAULT_MAX_PENDING_LINE_SIZE = 8 * 1024 * 1024
DEFAULT_MAX_PENDING_EVENT_SIZE = 32 * 1024 * 1024


class SSEParseError(ValueError):
    """A provider sent an event-stream frame larger than the safety budget."""


@dataclass(frozen=True)
class SSEEvent:
    """One dispatched SSE event."""

    data: str
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None

    @property
    def id(self) -> str | None:
        """Compatibility alias for callers using the SSE field name."""

        return self.event_id


class SSEParser:
    """Parse an SSE byte or text stream incrementally."""

    def __init__(
        self,
        *,
        max_line_size: int | None = None,
        max_event_size: int | None = None,
    ) -> None:
        self.max_line_size = (
            DEFAULT_MAX_PENDING_LINE_SIZE if max_line_size is None else max_line_size
        )
        self.max_event_size = (
            DEFAULT_MAX_PENDING_EVENT_SIZE if max_event_size is None else max_event_size
        )
        if self.max_line_size <= 0 or self.max_event_size <= 0:
            raise ValueError("SSE parser limits must be positive")
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._data: list[str] = []
        self._data_size = 0
        self._event: str | None = None
        self._event_id: str | None = None
        self._retry: int | None = None
        self._at_stream_start = True
        self._scan_position = 0

    def feed(self, chunk: bytes | bytearray | memoryview | str) -> list[SSEEvent]:
        """Consume an arbitrary chunk and return complete events."""

        text = self._decode(chunk)
        if not text:
            return []
        self._buffer += text
        events: list[SSEEvent] = []
        while True:
            line = self._take_line()
            if line is None:
                break
            if line == "":
                event = self._dispatch()
                if event is not None:
                    events.append(event)
                continue
            self._check_line_size(line)
            self._parse_line(line)
        self._check_pending_line_size()
        return events

    def finish(self) -> list[SSEEvent]:
        """Flush a final unterminated line and pending event."""

        events: list[SSEEvent] = []
        decoded_tail = self._decoder.decode(b"", final=True)
        if decoded_tail:
            self._buffer += decoded_tail
        if self._buffer:
            self._check_pending_line_size()
            if self._buffer.endswith("\r"):
                self._parse_line(self._buffer[:-1])
            else:
                self._parse_line(self._buffer)
            self._buffer = ""
            self._scan_position = 0
        event = self._dispatch()
        if event is not None:
            events.append(event)
        return events

    def _decode(self, chunk: bytes | bytearray | memoryview | str) -> str:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            text = self._decoder.decode(bytes(chunk), final=False)
        else:
            pending, _ = self._decoder.getstate()
            text = self._decoder.decode(b"", final=True) + chunk if pending else chunk
        if self._at_stream_start and text:
            self._at_stream_start = False
            if text.startswith("\ufeff"):
                text = text[1:]
        return text

    def _check_line_size(self, line: str) -> None:
        if len(line) > self.max_line_size:
            raise SSEParseError(
                f"SSE pending line exceeds {self.max_line_size} characters"
            )

    def _check_pending_line_size(self) -> None:
        if len(self._buffer) > self.max_line_size:
            raise SSEParseError(
                f"SSE pending line exceeds {self.max_line_size} characters"
            )

    def _take_line(self) -> str | None:
        carriage_return = self._buffer.find("\r", self._scan_position)
        line_feed = self._buffer.find("\n", self._scan_position)
        positions = [position for position in (carriage_return, line_feed) if position >= 0]
        if not positions:
            self._scan_position = len(self._buffer)
            return None
        index = min(positions)
        character = self._buffer[index]
        # A CR may be the first half of a CRLF pair split across HTTP chunks.
        # Wait for the next chunk so that the blank-line dispatch cannot happen
        # one byte too early.
        if character == "\r" and index + 1 == len(self._buffer):
            self._scan_position = index
            return None
        line = self._buffer[:index]
        if character == "\r" and index + 1 < len(self._buffer) and self._buffer[index + 1] == "\n":
            self._buffer = self._buffer[index + 2 :]
        else:
            self._buffer = self._buffer[index + 1 :]
        self._scan_position = 0
        return line

    def _parse_line(self, line: str) -> None:
        if line.startswith(":"):
            return
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "data":
            new_size = self._data_size + len(value) + (1 if self._data else 0)
            if new_size > self.max_event_size:
                raise SSEParseError(
                    f"SSE pending event exceeds {self.max_event_size} characters"
                )
            self._data.append(value)
            self._data_size = new_size
        elif field == "event":
            self._event = value
        elif field == "id" and "\x00" not in value:
            self._event_id = value
        elif field == "retry" and value.isdigit():
            self._retry = int(value)

    def _dispatch(self) -> SSEEvent | None:
        if not self._data:
            self._event = None
            self._retry = None
            return None
        event = SSEEvent(
            data="\n".join(self._data),
            event=self._event,
            event_id=self._event_id,
            retry=self._retry,
        )
        self._data.clear()
        self._data_size = 0
        self._event = None
        self._retry = None
        return event


def parse_sse(chunks: Iterable[bytes | str]) -> list[SSEEvent]:
    """Parse a finite iterable of chunks."""

    parser = SSEParser()
    events: list[SSEEvent] = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    return events


IncrementalSSEParser = SSEParser
SSEDecoder = SSEParser
