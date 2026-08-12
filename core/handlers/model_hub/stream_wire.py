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
UTF8_BOM: Final = b"\xef\xbb\xbf"


class SSEFrameLimitError(ValueError):
    """Raised when retained SSE parser state crosses a configured bound."""


@dataclass
class SSEFrameTokenizer:
    """Incrementally split SSE frames across CRLF, LF, and CR line endings."""

    _line: bytearray = field(default_factory=bytearray)
    _frame_lines: list[bytes] = field(default_factory=list)
    _after_cr: bool = False
    _frame_size: int = 0
    _stream_prefix: bytearray = field(default_factory=bytearray)
    _stream_started: bool = False

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        if not self._stream_started:
            self._stream_prefix.extend(chunk)
            prefix = bytes(self._stream_prefix)
            if len(prefix) < len(UTF8_BOM) and UTF8_BOM.startswith(prefix):
                return ()
            self._stream_started = True
            self._stream_prefix.clear()
            chunk = prefix[len(UTF8_BOM) :] if prefix.startswith(UTF8_BOM) else prefix
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
        self._stream_prefix.clear()
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


StreamTerminalOutcome = Literal["served", "failed_terminal"]
ProtocolObservationOutcome = Literal["served", "failed_terminal", "protocol_error"]
ErrorEnvelopePath = tuple[str, ...]


@dataclass(frozen=True)
class ProtocolObservation:
    outcome: ProtocolObservationOutcome | None = None
    model_output_started: bool = False
    completion_observed: bool = False
    error_payload: bytes | None = None
    error_envelope_paths: tuple[ErrorEnvelopePath, ...] = ()
    sequence_number: int | None = None
    message: str | None = None


@dataclass(frozen=True)
class ProtocolTerminalEnvelope:
    event_name: str | None
    selector_path: tuple[str, ...]
    selector_value: str | None
    terminal_outcome: StreamTerminalOutcome
    error_envelope_paths: tuple[ErrorEnvelopePath, ...] = ()
    required_error_path: ErrorEnvelopePath | None = None
    required_error_code_path: ErrorEnvelopePath | None = None
    wire_terminal: bool = True


@dataclass(frozen=True)
class ProtocolModelOutputEnvelope:
    event_name: str | None
    selector_path: tuple[str, ...]
    selector_value: str | None
    require_nonempty: bool = False


@dataclass(frozen=True)
class ProtocolStreamTaxonomy:
    terminal_envelopes: tuple[ProtocolTerminalEnvelope, ...]
    model_output_envelopes: tuple[ProtocolModelOutputEnvelope, ...]
    success_literal: tuple[str | None, bytes] | None
    sequence_number_path: tuple[str, ...] | None
    buffered_error_envelope_paths: tuple[ErrorEnvelopePath, ...]
    terminal_event_name: str | None
    render_terminal_event: Callable[[str, str, int], dict[str, object]]


PROTOCOL_STREAM_TAXONOMY: Final[Mapping[str, ProtocolStreamTaxonomy]] = {
    "anthropic": ProtocolStreamTaxonomy(
        terminal_envelopes=(
            ProtocolTerminalEnvelope(
                # https://platform.claude.com/docs/en/build-with-claude/streaming
                "message_stop",
                ("type",),
                "message_stop",
                "served",
            ),
            ProtocolTerminalEnvelope(
                # https://platform.claude.com/docs/en/build-with-claude/streaming#error-events
                "error",
                ("type",),
                "error",
                "failed_terminal",
                (("error",),),
            ),
        ),
        model_output_envelopes=(
            ProtocolModelOutputEnvelope(
                # https://platform.claude.com/docs/en/build-with-claude/streaming#content-block-start-event
                "content_block_start",
                ("type",),
                "content_block_start",
            ),
            ProtocolModelOutputEnvelope(
                # https://platform.claude.com/docs/en/build-with-claude/streaming#content-block-delta-event
                "content_block_delta",
                ("type",),
                "content_block_delta",
            ),
        ),
        success_literal=None,
        sequence_number_path=None,
        buffered_error_envelope_paths=(("error",),),
        terminal_event_name="error",
        render_terminal_event=_anthropic_terminal_event,
    ),
    "openai_responses": ProtocolStreamTaxonomy(
        terminal_envelopes=(
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/completed
                "response.completed",
                ("type",),
                "response.completed",
                "served",
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/error
                "error",
                ("type",),
                "error",
                "failed_terminal",
                ((),),
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/failed
                "response.failed",
                ("type",),
                "response.failed",
                "failed_terminal",
                (("response", "error"),),
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/incomplete
                "response.incomplete",
                ("type",),
                "response.incomplete",
                "failed_terminal",
                (("response", "error"),),
                ("response", "error"),
                ("response", "error", "code"),
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/incomplete
                # Incomplete output without an error envelope is completed
                # output and does not change Source health.
                "response.incomplete",
                ("type",),
                "response.incomplete",
                "served",
            ),
        ),
        model_output_envelopes=(
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/output-text/delta
                "response.output_text.delta",
                ("type",),
                "response.output_text.delta",
            ),
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal/delta
                "response.refusal.delta",
                ("type",),
                "response.refusal.delta",
            ),
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/reasoning-summary-text/delta
                "response.reasoning_summary_text.delta",
                ("type",),
                "response.reasoning_summary_text.delta",
            ),
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/function-call-arguments/delta
                "response.function_call_arguments.delta",
                ("type",),
                "response.function_call_arguments.delta",
            ),
        ),
        success_literal=None,
        sequence_number_path=("sequence_number",),
        buffered_error_envelope_paths=(("error",),),
        terminal_event_name="error",
        render_terminal_event=_responses_terminal_event,
    ),
    "openai_chat": ProtocolStreamTaxonomy(
        terminal_envelopes=(
            ProtocolTerminalEnvelope(
                # https://developers.openai.com/api/reference/resources/chat
                # Chat streaming errors use the top-level error member; they
                # do not require a second, top-level type discriminator.
                None,
                ("error",),
                None,
                "failed_terminal",
                (("error",),),
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "finish_reason"),
                "stop",
                "served",
                wire_terminal=False,
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "finish_reason"),
                "length",
                "served",
                wire_terminal=False,
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "finish_reason"),
                "content_filter",
                "served",
                wire_terminal=False,
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "finish_reason"),
                "tool_calls",
                "served",
                wire_terminal=False,
            ),
            ProtocolTerminalEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "finish_reason"),
                "function_call",
                "served",
                wire_terminal=False,
            ),
        ),
        model_output_envelopes=(
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "delta", "content"),
                None,
                require_nonempty=True,
            ),
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "delta", "refusal"),
                None,
                require_nonempty=True,
            ),
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "delta", "tool_calls"),
                None,
                require_nonempty=True,
            ),
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
                None,
                ("choices", "*", "delta", "function_call"),
                None,
                require_nonempty=True,
            ),
        ),
        # https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream
        success_literal=(None, b"[DONE]"),
        sequence_number_path=None,
        buffered_error_envelope_paths=(("error",),),
        terminal_event_name=None,
        render_terminal_event=_chat_terminal_event,
    ),
}


def _path_values(document: Mapping[str, object], path: tuple[str, ...]) -> tuple[object, ...]:
    values: tuple[object, ...] = (document,)
    for component in path:
        next_values: list[object] = []
        for value in values:
            if component == "*" and isinstance(value, list):
                next_values.extend(value)
            elif isinstance(value, Mapping) and component in value:
                next_values.append(value[component])
        values = tuple(next_values)
        if not values:
            break
    return values


def _selector_matches(
    payload: Mapping[str, object],
    *,
    selector_path: tuple[str, ...],
    selector_value: str | None,
    require_nonempty: bool = False,
) -> bool:
    values = _path_values(payload, selector_path)
    if selector_value is not None:
        return selector_value in values
    if require_nonempty:
        return any(bool(value) for value in values)
    return any(isinstance(value, Mapping) for value in values)


def is_protocol_model_output(
    protocol: str,
    event_name: str | None,
    payload: Mapping[str, object],
) -> bool:
    """Return the sole table-backed first-model-output boundary fact."""

    return any(
        envelope.event_name == event_name
        and _selector_matches(
            payload,
            selector_path=envelope.selector_path,
            selector_value=envelope.selector_value,
            require_nonempty=envelope.require_nonempty,
        )
        for envelope in PROTOCOL_STREAM_TAXONOMY[protocol].model_output_envelopes
    )


def observe_protocol_response(
    protocol: str,
    *,
    streamed: bool,
    data: bytes | None,
    event_name: str | None = None,
    previous_sequence_number: int = -1,
) -> ProtocolObservation:
    """Observe one response fact before any caller can construct success."""

    taxonomy = PROTOCOL_STREAM_TAXONOMY[protocol]
    if data is None:
        return ProtocolObservation()
    if streamed and taxonomy.success_literal == (event_name, data):
        return ProtocolObservation(outcome="served")
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return ProtocolObservation(outcome="served" if not streamed else None)
    if not isinstance(payload, dict):
        return ProtocolObservation(outcome="served" if not streamed else None)

    if not streamed:
        if any(
            isinstance(value, Mapping)
            for path in taxonomy.buffered_error_envelope_paths
            for value in _path_values(payload, path)
        ):
            return ProtocolObservation(
                outcome="failed_terminal",
                error_payload=data,
                error_envelope_paths=taxonomy.buffered_error_envelope_paths,
            )
        return ProtocolObservation(outcome="served")

    sequence_number: int | None = None
    if taxonomy.sequence_number_path is not None:
        sequence_values = _path_values(payload, taxonomy.sequence_number_path)
        candidate = sequence_values[0] if len(sequence_values) == 1 else None
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            sequence_number = max(previous_sequence_number, candidate)

    model_output_started = is_protocol_model_output(protocol, event_name, payload)
    for envelope in taxonomy.terminal_envelopes:
        if envelope.event_name != event_name:
            continue
        if not _selector_matches(
            payload,
            selector_path=envelope.selector_path,
            selector_value=envelope.selector_value,
        ):
            continue
        if envelope.required_error_path is not None and not any(
            isinstance(value, Mapping) for value in _path_values(payload, envelope.required_error_path)
        ):
            continue
        if envelope.required_error_code_path is not None and not any(
            isinstance(value, str) and value
            for value in _path_values(payload, envelope.required_error_code_path)
        ):
            continue
        if envelope.terminal_outcome == "served" and not envelope.wire_terminal:
            return ProtocolObservation(
                model_output_started=model_output_started,
                completion_observed=True,
                sequence_number=sequence_number,
            )
        return ProtocolObservation(
            outcome=envelope.terminal_outcome,
            model_output_started=model_output_started,
            error_payload=(data if envelope.terminal_outcome == "failed_terminal" else None),
            error_envelope_paths=(
                envelope.error_envelope_paths if envelope.terminal_outcome == "failed_terminal" else ()
            ),
            sequence_number=sequence_number,
        )
    return ProtocolObservation(
        model_output_started=model_output_started,
        sequence_number=sequence_number,
    )


@dataclass
class ProtocolSSEState:
    """Observe settlement facts without validating the upstream protocol stream."""

    protocol: str
    tokenizer: SSEFrameTokenizer = field(default_factory=SSEFrameTokenizer)
    terminal_outcome: StreamTerminalOutcome | None = None
    error_payload: bytes | None = None
    error_envelope_paths: tuple[ErrorEnvelopePath, ...] = ()
    last_sequence_number: int = -1
    model_output_started: bool = False
    completion_observed: bool = False

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
            return
        event_name, data = parse_sse_frame(frame)
        observation = observe_protocol_response(
            self.protocol,
            streamed=True,
            event_name=event_name,
            data=data,
            previous_sequence_number=self.last_sequence_number,
        )
        if observation.model_output_started:
            self.model_output_started = True
        if observation.completion_observed:
            self.completion_observed = True
        if observation.outcome in {"served", "failed_terminal"}:
            self.terminal_outcome = observation.outcome
        if observation.error_payload is not None:
            self.error_payload = observation.error_payload
            self.error_envelope_paths = observation.error_envelope_paths
        if observation.sequence_number is not None:
            self.last_sequence_number = observation.sequence_number

    def terminal_observation(self, *, allow_completion: bool = False) -> ProtocolObservation | None:
        if self.terminal_outcome is not None:
            return ProtocolObservation(
                outcome=self.terminal_outcome,
                error_payload=self.error_payload,
                error_envelope_paths=self.error_envelope_paths,
            )
        if allow_completion and self.completion_observed:
            return ProtocolObservation(outcome="served")
        return None


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


def render_protocol_terminal_frame(protocol: str, payload: bytes) -> bytes:
    """Frame one locally projected terminal in the protocol's native SSE form."""

    try:
        event_name = PROTOCOL_STREAM_TAXONOMY[protocol].terminal_event_name
    except KeyError as exc:
        raise ValueError(f"unsupported terminal event protocol: {protocol}") from exc
    event_line = b"" if event_name is None else b"event: " + event_name.encode() + b"\n"
    return event_line + b"data: " + payload + b"\n\n"
