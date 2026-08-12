"""Shared protocol stream framing and terminal-event taxonomy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Final, Literal, Mapping


SSE_LINE_ENDINGS: Final = (b"\r\n", b"\n", b"\r")
# One line may fill the runtime's 64 KiB transport read; one logical event may
# span four such reads. Both retained buffers remain bounded under hostile input.
SSE_MAX_LINE_BYTES: Final = 64 * 1024
SSE_MAX_FRAME_BYTES: Final = 256 * 1024


class SSEFrameLimitError(ValueError):
    """Raised when retained SSE parser state crosses a configured bound."""


@dataclass
class SSEFrameTokenizer:
    """Incrementally split SSE frames across CRLF, LF, and CR line endings."""

    _line: bytearray = field(default_factory=bytearray)
    _frame_lines: list[bytes] = field(default_factory=list)
    _after_cr: bool = False
    _frame_size: int = 0

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
                if len(self._line) > SSE_MAX_LINE_BYTES:
                    raise SSEFrameLimitError("SSE line exceeds the configured limit")
                self._frame_size += 1
                if self._frame_size > SSE_MAX_FRAME_BYTES:
                    raise SSEFrameLimitError("SSE frame exceeds the configured limit")
        return tuple(frames)

    def discard_partial_frame(self) -> bool | None:
        """Discard an unterminated frame and report whether its line is open."""

        if not self._line and not self._frame_lines:
            return None
        line_open = bool(self._line)
        self._line.clear()
        self._frame_lines.clear()
        self._frame_size = 0
        self._after_cr = False
        return line_open

    def _finish_line(self, frames: list[bytes]) -> None:
        line = bytes(self._line)
        self._line.clear()
        if line:
            self._frame_lines.append(line)
            return
        if self._frame_lines:
            frames.append(b"\n".join(self._frame_lines))
            self._frame_lines.clear()
            self._frame_size = 0


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
    success_types: frozenset[str]
    success_literal: bytes | None
    error_types: frozenset[str]
    render_terminal_event: Callable[[str, str, int], dict[str, object]]


PROTOCOL_STREAM_TAXONOMY: Final[Mapping[str, ProtocolStreamTaxonomy]] = {
    # https://platform.claude.com/docs/en/build-with-claude/streaming
    "anthropic": ProtocolStreamTaxonomy(
        success_types=frozenset({"message_stop"}),
        success_literal=None,
        error_types=frozenset({"error"}),
        render_terminal_event=_anthropic_terminal_event,
    ),
    # https://platform.openai.com/docs/api-reference/responses-streaming
    # https://platform.openai.com/docs/api-reference/realtime-server-events/response/done
    "openai_responses": ProtocolStreamTaxonomy(
        success_types=frozenset({"response.completed", "response.done"}),
        success_literal=None,
        error_types=frozenset({"error", "response.failed", "response.incomplete"}),
        render_terminal_event=_responses_terminal_event,
    ),
    # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
    "openai_chat": ProtocolStreamTaxonomy(
        success_types=frozenset(),
        success_literal=b"[DONE]",
        error_types=frozenset({"error"}),
        render_terminal_event=_chat_terminal_event,
    ),
}

StreamTerminalOutcome = Literal["served", "failed_terminal"]


@dataclass
class ProtocolSSEState:
    """Track terminal proof and Responses sequence state from one SSE stream."""

    protocol: str
    tokenizer: SSEFrameTokenizer = field(default_factory=SSEFrameTokenizer)
    terminal_outcome: StreamTerminalOutcome | None = None
    error_payload: bytes | None = None
    last_sequence_number: int = -1
    invalid_after_terminal: bool = False

    def observe(self, chunk: bytes) -> None:
        for frame in self.tokenizer.feed(chunk):
            self._observe_frame(frame)

    def invalidate_partial_frame(self) -> bytes:
        """Make an already-forwarded partial frame non-terminal, then close it."""

        line_open = self.tokenizer.discard_partial_frame()
        if line_open is None:
            return b""
        line_break = b"\n" if line_open else b""
        return line_break + b"data: {}\n\n"

    @property
    def next_sequence_number(self) -> int:
        return self.last_sequence_number + 1

    def _observe_frame(self, frame: bytes) -> None:
        if self.terminal_outcome is not None:
            self.invalid_after_terminal = True
            return
        _event_name, data = parse_sse_frame(frame)
        if data is None:
            return
        taxonomy = PROTOCOL_STREAM_TAXONOMY[self.protocol]
        if taxonomy.success_literal is not None and data == taxonomy.success_literal:
            self.terminal_outcome = "served"
            return
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        event_type = payload["type"] if "type" in payload else None
        if isinstance(event_type, str) and event_type in taxonomy.success_types:
            self.terminal_outcome = "served"
        elif isinstance(event_type, str) and event_type in taxonomy.error_types:
            self.terminal_outcome = "failed_terminal"
            self.error_payload = data
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
