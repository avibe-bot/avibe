"""Shared protocol stream framing and terminal-event taxonomy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Final, Mapping


SSE_LINE_ENDINGS: Final = (b"\r\n", b"\n", b"\r")


@dataclass
class SSEFrameTokenizer:
    """Incrementally split SSE frames across CRLF, LF, and CR line endings."""

    _line: bytearray = field(default_factory=bytearray)
    _frame_lines: list[bytes] = field(default_factory=list)
    _after_cr: bool = False

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        frames: list[bytes] = []
        for byte in chunk:
            if self._after_cr:
                self._after_cr = False
                if byte == 0x0A:
                    continue
            if byte == 0x0D:
                self._finish_line(frames)
                self._after_cr = True
            elif byte == 0x0A:
                self._finish_line(frames)
            else:
                self._line.append(byte)
        return tuple(frames)

    def take_partial_frame(self) -> bytes | None:
        if not self._line and not self._frame_lines:
            return None
        lines = [*self._frame_lines]
        if self._line:
            lines.append(bytes(self._line))
        self._line.clear()
        self._frame_lines.clear()
        self._after_cr = False
        return b"\n".join(lines)

    def _finish_line(self, frames: list[bytes]) -> None:
        line = bytes(self._line)
        self._line.clear()
        if line:
            self._frame_lines.append(line)
            return
        if self._frame_lines:
            frames.append(b"\n".join(self._frame_lines))
            self._frame_lines.clear()


def parse_sse_frame(frame: bytes) -> tuple[str | None, bytes | None]:
    """Return the event name and joined data field from one normalized frame."""

    event_name: str | None = None
    data_lines: list[bytes] = []
    for line in frame.split(b"\n"):
        field, separator, value = line.partition(b":")
        if not separator:
            continue
        if value.startswith(b" "):
            value = value[1:]
        if field == b"event":
            try:
                event_name = value.decode("utf-8")
            except UnicodeDecodeError:
                event_name = None
        elif field == b"data":
            data_lines.append(value)
    return event_name, b"\n".join(data_lines) if data_lines else None


def _anthropic_terminal_event(
    _key: str,
    message: str,
    _next_sequence_number: int,
) -> dict[str, object]:
    return {"type": "error", "error": {"type": "api_error", "message": message}}


def _responses_terminal_event(
    key: str,
    message: str,
    next_sequence_number: int,
) -> dict[str, object]:
    return {
        "type": "error",
        "code": key,
        "message": message,
        "param": None,
        "sequence_number": next_sequence_number,
    }


def _chat_terminal_event(
    key: str,
    message: str,
    _next_sequence_number: int,
) -> dict[str, object]:
    return {
        "object": "chat.completion.chunk",
        "type": "error",
        "error": {"type": "server_error", "code": key, "message": message},
        "choices": [],
    }


@dataclass(frozen=True)
class ProtocolStreamTaxonomy:
    terminal_types: frozenset[str]
    terminal_literal: bytes | None
    render_terminal_event: Callable[[str, str, int], dict[str, object]]

    def terminal_fixture(self) -> bytes:
        if self.terminal_literal is not None:
            return self.terminal_literal
        return json.dumps(
            {"type": next(iter(self.terminal_types))},
            separators=(",", ":"),
        ).encode("utf-8")


PROTOCOL_STREAM_TAXONOMY: Final[Mapping[str, ProtocolStreamTaxonomy]] = {
    "anthropic": ProtocolStreamTaxonomy(
        terminal_types=frozenset({"message_stop"}),
        terminal_literal=None,
        render_terminal_event=_anthropic_terminal_event,
    ),
    "openai_responses": ProtocolStreamTaxonomy(
        terminal_types=frozenset({"response.completed"}),
        terminal_literal=None,
        render_terminal_event=_responses_terminal_event,
    ),
    "openai_chat": ProtocolStreamTaxonomy(
        terminal_types=frozenset(),
        terminal_literal=b"[DONE]",
        render_terminal_event=_chat_terminal_event,
    ),
}


@dataclass
class ProtocolSSEState:
    """Track terminal proof and Responses sequence state from one SSE stream."""

    protocol: str
    tokenizer: SSEFrameTokenizer = field(default_factory=SSEFrameTokenizer)
    terminal_seen: bool = False
    last_sequence_number: int = -1

    def observe(self, chunk: bytes) -> None:
        for frame in self.tokenizer.feed(chunk):
            self._observe_frame(frame)

    def close_partial_frame(self) -> bytes:
        frame = self.tokenizer.take_partial_frame()
        if frame is None:
            return b""
        self._observe_frame(frame)
        return b"\n\n"

    @property
    def next_sequence_number(self) -> int:
        return self.last_sequence_number + 1

    def _observe_frame(self, frame: bytes) -> None:
        _event_name, data = parse_sse_frame(frame)
        if data is None:
            return
        taxonomy = PROTOCOL_STREAM_TAXONOMY[self.protocol]
        if taxonomy.terminal_literal is not None and data == taxonomy.terminal_literal:
            self.terminal_seen = True
            return
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        event_type = payload["type"] if "type" in payload else None
        if isinstance(event_type, str) and event_type in taxonomy.terminal_types:
            self.terminal_seen = True
        sequence_number = payload.get("sequence_number")
        if isinstance(sequence_number, int) and not isinstance(sequence_number, bool):
            self.last_sequence_number = max(self.last_sequence_number, sequence_number)


def render_protocol_terminal_event(
    protocol: str,
    key: str,
    message: str,
    next_sequence_number: int,
) -> dict[str, object]:
    """Render a terminal outcome in the requested protocol's wire shape."""

    try:
        taxonomy = PROTOCOL_STREAM_TAXONOMY[protocol]
    except KeyError as exc:
        raise ValueError(f"unsupported terminal event protocol: {protocol}") from exc
    return taxonomy.render_terminal_event(key, message, next_sequence_number)
