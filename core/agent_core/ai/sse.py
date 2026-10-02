"""Incremental Server-Sent Events parsing.

The parser follows the event-stream grammar from the W3C/WHATWG SSE
specification. It deliberately accepts bytes because an HTTP transport may
split a UTF-8 code point, a CRLF pair, or a ``data:`` field at any boundary.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Iterable


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

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._data: list[str] = []
        self._event: str | None = None
        self._event_id: str | None = None
        self._retry: int | None = None

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
            self._parse_line(line)
        return events

    def finish(self) -> list[SSEEvent]:
        """Flush a final unterminated line and pending event."""

        events: list[SSEEvent] = []
        decoded_tail = self._decoder.decode(b"", final=True)
        if decoded_tail:
            self._buffer += decoded_tail
        if self._buffer:
            if self._buffer.endswith("\r"):
                self._parse_line(self._buffer[:-1])
            else:
                self._parse_line(self._buffer)
            self._buffer = ""
        event = self._dispatch()
        if event is not None:
            events.append(event)
        return events

    def _decode(self, chunk: bytes | bytearray | memoryview | str) -> str:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            return self._decoder.decode(bytes(chunk), final=False)
        pending, _ = self._decoder.getstate()
        if pending:
            return self._decoder.decode(b"", final=True) + chunk
        return chunk

    def _take_line(self) -> str | None:
        for index, character in enumerate(self._buffer):
            if character not in "\r\n":
                continue
            # A CR may be the first half of a CRLF pair split across HTTP
            # chunks. Wait for the next chunk so that the blank-line dispatch
            # cannot happen one byte too early.
            if character == "\r" and index + 1 == len(self._buffer):
                return None
            line = self._buffer[:index]
            if character == "\r" and index + 1 < len(self._buffer) and self._buffer[index + 1] == "\n":
                self._buffer = self._buffer[index + 2 :]
            else:
                self._buffer = self._buffer[index + 1 :]
            return line
        return None

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
            self._data.append(value)
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
