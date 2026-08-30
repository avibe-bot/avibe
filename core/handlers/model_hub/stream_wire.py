"""Shared protocol stream framing and terminal-event taxonomy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Final, Literal, Mapping


SSE_LINE_ENDINGS: Final = (b"\r\n", b"\n", b"\r")
# Wire observation is a bounded, fail-closed metadata copy. These are not
# protocol or response limits: the original bytes continue downstream unchanged.
SSE_OBSERVATION_BYTES: Final = 64 * 1024
SSE_OBSERVATION_STRING_BYTES: Final = 16 * 1024
SSE_OBSERVATION_EVENT_BYTES: Final = 256
UTF8_BOM: Final = b"\xef\xbb\xbf"


@dataclass
class SSEFrameTokenizer:
    """Incrementally split exact SSE frames for response mutation."""

    _line: bytearray = field(default_factory=bytearray)
    _frame_lines: list[bytes] = field(default_factory=list)
    _after_cr: bool = False
    _stream_prefix: bytearray = field(default_factory=bytearray)
    _stream_started: bool = False

    @property
    def retained_bytes(self) -> int:
        return len(self._stream_prefix) + len(self._line) + sum(map(len, self._frame_lines))

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
        return tuple(frames)

    def discard_partial_frame(self) -> bool | None:
        """Discard an unterminated frame and report whether its line is open."""

        if not self._line and not self._frame_lines:
            return None
        line_open = bool(self._line)
        self._line.clear()
        self._frame_lines.clear()
        self._after_cr = False
        self._stream_prefix.clear()
        return line_open

    def drain_partial_frame(self) -> bytes:
        """Return and clear any unterminated bytes in normalized SSE form."""

        if not self._stream_started:
            pending = bytes(self._stream_prefix)
        else:
            pending = b"\n".join((*self._frame_lines, bytes(self._line)))
        self._line.clear()
        self._frame_lines.clear()
        self._after_cr = False
        self._stream_prefix.clear()
        return pending

    def _finish_line(self, frames: list[bytes]) -> None:
        line = bytes(self._line)
        self._line.clear()
        if line:
            self._frame_lines.append(line)
            return
        if self._frame_lines:
            frames.append(b"\n".join(self._frame_lines))
            self._frame_lines.clear()


@dataclass(frozen=True)
class SSEObservedFrame:
    """The protocol metadata retained from one SSE frame."""

    event_name: str | None
    data: bytes | None


@dataclass
class _JSONObservationBuffer:
    """Keep JSON structure while eliding large string bodies from observation."""

    _payload: bytearray = field(default_factory=bytearray)
    _in_string: bool = False
    _escaped: bool = False
    _string_start: int = 0
    _string_bytes: int = 0
    _string_elided: bool = False
    _abandoned: bool = False

    @property
    def retained_bytes(self) -> int:
        return len(self._payload)

    def feed_byte(self, byte: int) -> None:
        if self._abandoned:
            return
        if not self._in_string:
            self._payload.append(byte)
            self._enforce_total_budget()
            if byte == 0x22:
                self._in_string = True
                self._escaped = False
                self._string_start = len(self._payload)
                self._string_bytes = 0
                self._string_elided = False
            return

        if self._string_elided:
            if self._escaped:
                self._escaped = False
            elif byte == 0x5C:
                self._escaped = True
            elif byte == 0x22:
                self._payload.append(byte)
                self._in_string = False
            return

        if not self._escaped and byte == 0x22:
            self._payload.append(byte)
            self._in_string = False
            self._enforce_total_budget()
            return

        self._payload.append(byte)
        self._string_bytes += 1
        if self._escaped:
            self._escaped = False
        elif byte == 0x5C:
            self._escaped = True
        if self._string_bytes > SSE_OBSERVATION_STRING_BYTES:
            del self._payload[self._string_start :]
            self._payload.extend(b"__avibe_observation_elided__")
            self._string_elided = True
        self._enforce_total_budget()

    def feed_separator(self) -> None:
        self.feed_byte(0x0A)

    def take(self) -> bytes | None:
        payload = None if self._abandoned else bytes(self._payload)
        self._reset()
        return payload

    def _enforce_total_budget(self) -> None:
        if len(self._payload) <= SSE_OBSERVATION_BYTES:
            return
        self._payload.clear()
        self._abandoned = True

    def _reset(self) -> None:
        self._payload.clear()
        self._in_string = False
        self._escaped = False
        self._string_start = 0
        self._string_bytes = 0
        self._string_elided = False
        self._abandoned = False


@dataclass
class SSEObservationTokenizer:
    """Observe SSE metadata without retaining model-authored string bodies."""

    _field: bytearray = field(default_factory=bytearray)
    _field_too_long: bool = False
    _line_kind: str | None = None
    _value_started: bool = False
    _skip_value_space: bool = False
    _line_started: bool = False
    _frame_started: bool = False
    _event_value: bytearray = field(default_factory=bytearray)
    _event_value_too_long: bool = False
    _current_event_name: str | None = None
    _has_data: bool = False
    _data: _JSONObservationBuffer = field(default_factory=_JSONObservationBuffer)
    _after_cr: bool = False
    _stream_prefix: bytearray = field(default_factory=bytearray)
    _stream_started: bool = False

    @property
    def retained_bytes(self) -> int:
        return len(self._field) + len(self._event_value) + self._data.retained_bytes

    def feed(self, chunk: bytes) -> tuple[SSEObservedFrame, ...]:
        if not self._stream_started:
            self._stream_prefix.extend(chunk)
            prefix = bytes(self._stream_prefix)
            if len(prefix) < len(UTF8_BOM) and UTF8_BOM.startswith(prefix):
                return ()
            self._stream_started = True
            self._stream_prefix.clear()
            chunk = prefix[len(UTF8_BOM) :] if prefix.startswith(UTF8_BOM) else prefix

        frames: list[SSEObservedFrame] = []
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
                self._consume_byte(byte)
        return tuple(frames)

    def discard_partial_frame(self) -> bool | None:
        if not self._frame_started and not self._line_started:
            return None
        line_open = self._line_started
        self._reset_line()
        self._reset_frame()
        self._after_cr = False
        self._stream_prefix.clear()
        return line_open

    def _consume_byte(self, byte: int) -> None:
        self._line_started = True
        self._frame_started = True
        if not self._value_started:
            if byte == 0x3A:
                self._value_started = True
                field_name = bytes(self._field) if not self._field_too_long else b""
                self._line_kind = (
                    "event" if field_name == b"event" else "data" if field_name == b"data" else None
                )
                self._skip_value_space = True
                if self._line_kind == "data":
                    self._start_data_line()
                return
            if len(self._field) < len(b"event"):
                self._field.append(byte)
            else:
                self._field_too_long = True
            return

        if self._skip_value_space:
            self._skip_value_space = False
            if byte == 0x20:
                return
        if self._line_kind == "event":
            if self._event_value_too_long:
                return
            if len(self._event_value) < SSE_OBSERVATION_EVENT_BYTES:
                self._event_value.append(byte)
            else:
                self._event_value.clear()
                self._event_value_too_long = True
        elif self._line_kind == "data":
            self._data.feed_byte(byte)

    def _finish_line(self, frames: list[SSEObservedFrame]) -> None:
        if not self._line_started:
            if self._frame_started:
                frames.append(
                    SSEObservedFrame(
                        event_name=self._current_event_name,
                        data=self._data.take() if self._has_data else None,
                    )
                )
                self._reset_frame()
            return

        if not self._value_started and not self._field_too_long:
            field_name = bytes(self._field)
            if field_name == b"data":
                self._start_data_line()
            elif field_name == b"event":
                self._line_kind = "event"
        if self._line_kind == "event":
            if self._event_value_too_long:
                self._current_event_name = None
            else:
                try:
                    self._current_event_name = self._event_value.decode("utf-8")
                except UnicodeDecodeError:
                    self._current_event_name = None
        self._reset_line()

    def _start_data_line(self) -> None:
        if self._has_data:
            self._data.feed_separator()
        self._has_data = True

    def _reset_line(self) -> None:
        self._field.clear()
        self._field_too_long = False
        self._line_kind = None
        self._value_started = False
        self._skip_value_space = False
        self._line_started = False
        self._event_value.clear()
        self._event_value_too_long = False

    def _reset_frame(self) -> None:
        self._frame_started = False
        self._current_event_name = None
        self._has_data = False
        self._data.take()


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


# Token counts are vendor-reported, never self-measured, so one hostile or buggy
# response must not be able to poison a persisted aggregate. The ceiling is fixed
# in our code; it is never derived from a value the upstream declares.
USAGE_TOKEN_CEILING: Final = 1_000_000_000


@dataclass(frozen=True)
class ProtocolUsageReport:
    """One protocol-normalized token report observed on the wire."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def of(
        cls,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> "ProtocolUsageReport":
        """Build one report with the cached-input subset invariant enforced.

        Cached input is a *part* of the input the turn was billed for, which is
        what the read contract promises and what the settings page will divide
        by. Both numbers come from the same upstream, so the only bound worth
        trusting is the input count this module normalized itself — never a
        total the response declares. Cached input reported without a readable
        input count is a subset of nothing, so it clamps to zero.
        """

        return cls(
            input_tokens=input_tokens,
            cached_input_tokens=min(cached_input_tokens, input_tokens),
            output_tokens=output_tokens,
        )

    def merge(self, other: "ProtocolUsageReport") -> "ProtocolUsageReport":
        """Combine two reports for the same turn by taking the larger of each.

        The per-field max is re-normalized because the winning input and cached
        counts can come from different frames, which is a second way an upstream
        could compose a cached count larger than the input it belongs to.
        """

        return ProtocolUsageReport.of(
            input_tokens=max(self.input_tokens, other.input_tokens),
            cached_input_tokens=max(self.cached_input_tokens, other.cached_input_tokens),
            output_tokens=max(self.output_tokens, other.output_tokens),
        )


@dataclass(frozen=True)
class ProtocolObservation:
    outcome: ProtocolObservationOutcome | None = None
    model_output_started: bool = False
    error_payload: bytes | None = None
    error_envelope_paths: tuple[ErrorEnvelopePath, ...] = ()
    sequence_number: int | None = None
    message: str | None = None
    usage: ProtocolUsageReport | None = None


@dataclass(frozen=True)
class ProtocolTerminalEnvelope:
    event_name: str | None
    selector_path: tuple[str, ...]
    selector_value: str | None
    terminal_outcome: StreamTerminalOutcome
    error_envelope_paths: tuple[ErrorEnvelopePath, ...] = ()
    required_error_path: ErrorEnvelopePath | None = None
    required_error_code_path: ErrorEnvelopePath | None = None


@dataclass(frozen=True)
class ProtocolModelOutputEnvelope:
    event_name: str | None
    selector_path: tuple[str, ...]
    selector_value: str | None
    require_nonempty: bool = False


@dataclass(frozen=True)
class ProtocolUsageTaxonomy:
    """Declare where one protocol reports token usage and how to compose it.

    Leaf paths are relative to a container. Values found under the leaves of one
    logical field are summed within a single container, because a protocol may
    split one quantity across sibling members; candidates from different
    containers are then merged by taking the larger, because protocols repeat a
    cumulative report across frames.
    """

    container_paths: tuple[tuple[str, ...], ...]
    input_paths: tuple[tuple[str, ...], ...]
    cached_input_paths: tuple[tuple[str, ...], ...]
    output_paths: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ProtocolStreamTaxonomy:
    terminal_envelopes: tuple[ProtocolTerminalEnvelope, ...]
    model_output_envelopes: tuple[ProtocolModelOutputEnvelope, ...]
    success_literal: tuple[str | None, bytes] | None
    sequence_number_path: tuple[str, ...] | None
    buffered_error_envelope_paths: tuple[ErrorEnvelopePath, ...]
    terminal_event_name: str | None
    render_terminal_event: Callable[[str, str, int], dict[str, object]]
    usage: ProtocolUsageTaxonomy


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
        usage=ProtocolUsageTaxonomy(
            # https://platform.claude.com/docs/en/build-with-claude/streaming
            # message_start nests the report under `message`; message_delta and
            # buffered responses report it at the top level.
            container_paths=(("message", "usage"), ("usage",)),
            # https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching
            # `input_tokens` excludes both cache members, so the input total is
            # their sum. Only cache reads are input served from cache; cache
            # creation is fresh input that was also written to the cache.
            input_paths=(
                ("input_tokens",),
                ("cache_read_input_tokens",),
                ("cache_creation_input_tokens",),
            ),
            cached_input_paths=(("cache_read_input_tokens",),),
            output_paths=(("output_tokens",),),
        ),
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
            ProtocolModelOutputEnvelope(
                # https://platform.openai.com/docs/api-reference/responses-streaming/response/image-generation-call/partial-image
                "response.image_generation_call.partial_image",
                ("type",),
                "response.image_generation_call.partial_image",
            ),
        ),
        success_literal=None,
        sequence_number_path=("sequence_number",),
        buffered_error_envelope_paths=(("error",),),
        terminal_event_name="error",
        render_terminal_event=_responses_terminal_event,
        usage=ProtocolUsageTaxonomy(
            # https://platform.openai.com/docs/api-reference/responses/object#responses/object-usage
            # Terminal response events nest the completed response; buffered
            # responses are that object.
            container_paths=(("response", "usage"), ("usage",)),
            # `input_tokens` already includes cached input, so the cached count
            # is an informational subset and never an addend.
            input_paths=(("input_tokens",),),
            cached_input_paths=(("input_tokens_details", "cached_tokens"),),
            output_paths=(("output_tokens",),),
        ),
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
        usage=ProtocolUsageTaxonomy(
            # https://platform.openai.com/docs/api-reference/chat/object#chat/object-usage
            # Streaming chat reports usage only when the client asked for it via
            # `stream_options.include_usage`, on a dedicated final chunk.
            container_paths=(("usage",),),
            input_paths=(("prompt_tokens",),),
            cached_input_paths=(("prompt_tokens_details", "cached_tokens"),),
            output_paths=(("completion_tokens",),),
        ),
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


def _usage_sum(container: Mapping[str, object], paths: tuple[tuple[str, ...], ...]) -> int | None:
    """Sum one logical field's leaves, rejecting anything not a bounded count."""

    total: int | None = None
    for path in paths:
        for value in _path_values(container, path):
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            if value < 0 or value > USAGE_TOKEN_CEILING:
                continue
            total = value if total is None else total + value
    if total is None:
        return None
    return min(total, USAGE_TOKEN_CEILING)


def extract_protocol_usage(
    protocol: str,
    payload: Mapping[str, object],
) -> ProtocolUsageReport | None:
    """Read one document's token report, or None when it carries none."""

    taxonomy = PROTOCOL_STREAM_TAXONOMY[protocol].usage
    report: ProtocolUsageReport | None = None
    for container_path in taxonomy.container_paths:
        for container in _path_values(payload, container_path):
            if not isinstance(container, Mapping):
                continue
            input_tokens = _usage_sum(container, taxonomy.input_paths)
            cached_input_tokens = _usage_sum(container, taxonomy.cached_input_paths)
            output_tokens = _usage_sum(container, taxonomy.output_paths)
            if input_tokens is None and cached_input_tokens is None and output_tokens is None:
                continue
            candidate = ProtocolUsageReport.of(
                input_tokens=input_tokens or 0,
                cached_input_tokens=cached_input_tokens or 0,
                output_tokens=output_tokens or 0,
            )
            report = candidate if report is None else report.merge(candidate)
    return report


def _try_parse_payload(data: bytes) -> object | None:
    """Parse untrusted observation data without leaking parser failures."""

    try:
        return json.loads(data)
    except (TypeError, ValueError, RecursionError):
        return None


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
    payload = _try_parse_payload(data)
    if not isinstance(payload, dict):
        if not streamed:
            return ProtocolObservation(outcome="served")
        return ProtocolObservation()

    usage = extract_protocol_usage(protocol, payload)

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
                usage=usage,
            )
        return ProtocolObservation(outcome="served", usage=usage)

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
        return ProtocolObservation(
            outcome=envelope.terminal_outcome,
            model_output_started=model_output_started,
            error_payload=(data if envelope.terminal_outcome == "failed_terminal" else None),
            error_envelope_paths=(
                envelope.error_envelope_paths if envelope.terminal_outcome == "failed_terminal" else ()
            ),
            sequence_number=sequence_number,
            usage=usage,
        )
    return ProtocolObservation(
        model_output_started=model_output_started,
        sequence_number=sequence_number,
        usage=usage,
    )


@dataclass
class ProtocolSSEState:
    """Observe settlement facts without validating the upstream protocol stream."""

    protocol: str
    tokenizer: SSEObservationTokenizer = field(default_factory=SSEObservationTokenizer)
    terminal_outcome: StreamTerminalOutcome | None = None
    error_payload: bytes | None = None
    error_envelope_paths: tuple[ErrorEnvelopePath, ...] = ()
    last_sequence_number: int = -1
    model_output_started: bool = False
    usage: ProtocolUsageReport | None = None

    def observe(self, chunk: bytes) -> None:
        frames = self.tokenizer.feed(chunk)
        for frame in frames:
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

    @property
    def reached_model(self) -> bool:
        """Whether this stream got far enough upstream to have been billed.

        Forwarded model output proves the call reached the model even when the
        stream then ended without a recognized terminal — a connection lost after
        a text delta is a request that happened, and `token_reports` staying at
        zero is exactly how the ledger records that nobody reported its tokens.

        Lives on the tracker rather than beside one of its readers, because both
        sides of the hand-off keep a tracker over the same body and a fact read
        from only one of them is a fact the other half of the turn cannot use.
        """

        return self.terminal_outcome == "served" or self.model_output_started

    def _observe_frame(self, frame: SSEObservedFrame) -> None:
        if self.terminal_outcome is not None:
            return
        observation = observe_protocol_response(
            self.protocol,
            streamed=True,
            event_name=frame.event_name,
            data=frame.data,
            previous_sequence_number=self.last_sequence_number,
        )
        if observation.model_output_started:
            self.model_output_started = True
        if observation.usage is not None:
            self.usage = (
                observation.usage if self.usage is None else self.usage.merge(observation.usage)
            )
        if observation.outcome in {"served", "failed_terminal"}:
            self.terminal_outcome = observation.outcome
        if observation.error_payload is not None:
            self.error_payload = observation.error_payload
            self.error_envelope_paths = observation.error_envelope_paths
        if observation.sequence_number is not None:
            self.last_sequence_number = observation.sequence_number

    def terminal_observation(self) -> ProtocolObservation | None:
        """Report the settled facts, including everything accumulated to get here.

        Usage often arrives before the terminal — Anthropic reports input tokens on
        ``message_start`` — so a terminal that dropped the accumulated report would
        lose tokens the upstream already billed on exactly the streams that end
        badly.
        """

        if self.terminal_outcome is not None:
            return ProtocolObservation(
                outcome=self.terminal_outcome,
                error_payload=self.error_payload,
                error_envelope_paths=self.error_envelope_paths,
                model_output_started=self.model_output_started,
                usage=self.usage,
            )
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
