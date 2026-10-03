"""Anthropic Messages streaming adapter.

Payload and stream rules are ported from tau's ``tau_ai/anthropic.py`` (MIT)
and Pi's ``packages/ai/src/api/anthropic-messages.ts`` (MIT). This module uses
httpx directly so Model Hub remains the only gateway boundary.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from core.agent_core.ai._common import (
    ServedHopResolver,
    auth_headers,
    content_parts,
    drive_sse_stream,
    dispatch_wire_event,
    endpoint_origin,
    join_endpoint_url,
    json_object,
    prepare_messages,
    StreamAssembler,
    WireField,
    usage_counter_error,
    validate_wire_shape,
)
from core.agent_core.ai.provider import (
    Done,
    ModelRequest,
    ProviderAdapter,
    ProviderError,
    MediaLoader,
)
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

ANTHROPIC_VERSION = "2023-06-01"
_MAX_CACHE_BREAKPOINTS = 4
_LEGACY_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 16384,
    "max": 16384,
}
_KNOWN_STREAM_EVENTS = frozenset(
    {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "error",
        "message_stop",
        "ping",
    }
)
_ANTHROPIC_USAGE_FIELDS = (
    WireField("input_tokens", int),
    WireField("output_tokens", int),
    WireField("cache_read_input_tokens", int),
    WireField("cache_creation_input_tokens", int),
    WireField(
        "cache_creation",
        Mapping,
        children=(
            WireField("ephemeral_5m_input_tokens", int),
            WireField("ephemeral_1h_input_tokens", int),
        ),
    ),
    WireField(
        "output_tokens_details",
        Mapping,
        children=(WireField("thinking_tokens", int),),
    ),
)
_ANTHROPIC_WIRE_SHAPES = {
    "message_start": (
        WireField(
            "message",
            Mapping,
            required=True,
            nullable=False,
            children=(
                WireField("usage", Mapping, children=_ANTHROPIC_USAGE_FIELDS),
            ),
        ),
    ),
    "content_block_start": (
        WireField("index", int, required=True, nullable=False),
        WireField(
            "content_block",
            Mapping,
            required=True,
            nullable=False,
            children=(
                WireField("type", str, required=True, nullable=False),
                WireField("id", str),
                WireField("name", str),
                WireField("signature", str),
                WireField("data", str),
                WireField("text", str),
                WireField("thinking", str),
                WireField("input", Mapping),
            ),
        ),
    ),
    "content_block_delta": (
        WireField("index", int, required=True, nullable=False),
        WireField(
            "delta",
            Mapping,
            required=True,
            nullable=False,
            children=(
                WireField("type", str, required=True, nullable=False),
                WireField("text", str),
                WireField("thinking", str),
                WireField("signature", str),
                WireField("partial_json", str),
            ),
        ),
    ),
    "content_block_stop": (
        WireField("index", int, required=True, nullable=False),
    ),
    "message_delta": (
        WireField(
            "delta",
            Mapping,
            children=(WireField("stop_reason", str),),
        ),
        WireField("usage", Mapping, children=_ANTHROPIC_USAGE_FIELDS),
    ),
    "error": (
        WireField(
            "error",
            Mapping,
            children=(
                WireField("type", str),
                WireField("message", str),
            ),
        ),
    ),
    "message_stop": (),
    "ping": (),
}


class AnthropicAdapter(ProviderAdapter):
    """Adapter for the native Anthropic Messages protocol."""

    protocol = "anthropic"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        media_loader: MediaLoader | None = None,
        served_hop_resolver: ServedHopResolver | None = None,
        gateway: bool = False,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=None)
        self._owns_client = client is None
        self._media_loader = media_loader
        self._served_hop_resolver = served_hop_resolver
        self._gateway = gateway

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[Any]:
        return self._stream(request, cancel)

    async def _stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[Any]:
        origin = endpoint_origin(request.endpoint)
        assembler = StreamAssembler(
            origin=origin,
            protocol=self.protocol,
            verified_origin=not self._gateway,
            endpoint_url=request.endpoint.base_url,
        )
        if cancel.cancelled:
            terminal = assembler.terminal(assembler.aborted(cancel.reason))
            if terminal is not None:
                yield terminal
            return
        target = origin
        prepared = await prepare_messages(
            request.messages,
            target=target,
            supports_images=request.supports_images,
            protocol=self.protocol,
            media_loader=self._media_loader,
            cancel=cancel,
        )
        if isinstance(prepared, ProviderError):
            terminal = assembler.terminal(prepared)
            if terminal is not None:
                yield terminal
            return
        transformed_messages, loaded_images = prepared
        if cancel.cancelled:
            terminal = assembler.terminal(assembler.aborted(cancel.reason))
            if terminal is not None:
                yield terminal
            return
        block_state: dict[int, dict[str, Any]] = {}
        stop_reason: str | None = None
        message_started = False
        message_stopped = False
        try:
            payload = build_messages_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="anthropic", gateway=self._gateway)
            headers.update({"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})
            url = join_endpoint_url(request.endpoint.base_url, "/messages")
            async def translate(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
                nonlocal message_started, message_stopped, stop_reason
                async for event in events:
                    if cancel.cancelled:
                        terminal = assembler.terminal(assembler.aborted(cancel.reason))
                        if terminal is not None:
                            yield terminal
                        return
                    if event.data == "[DONE]" or not event.data:
                        continue
                    if event.event == "error":
                        error_chunk = json_object(event.data)
                        if isinstance(error_chunk, Mapping):
                            shape_error = validate_wire_shape(
                                error_chunk,
                                "error",
                                _ANTHROPIC_WIRE_SHAPES,
                            )
                            shape_error = shape_error or _error_event_shape_error(error_chunk)
                            if shape_error is not None:
                                terminal = assembler.terminal(
                                    assembler.error(shape_error, kind="invalid_request")
                                )
                            else:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        json.dumps(dict(error_chunk), ensure_ascii=False),
                                    )
                                )
                        else:
                            terminal = assembler.terminal(assembler.error(event.data))
                        if terminal is not None:
                            yield terminal
                        return
                    if event.event is not None and event.event not in _KNOWN_STREAM_EVENTS:
                        # Pi filters by the SSE event name before parsing its
                        # JSON payload, so unknown event names are ignored
                        # even when their data is malformed.
                        continue
                    chunk = json_object(event.data)
                    if chunk is None:
                        # Pi ignores data-only frames because Anthropic event
                        # dispatch is keyed by the SSE event name. Avibe
                        # accepts valid data-only JSON for gateway
                        # compatibility, but malformed data-only frames are
                        # still ignored rather than misclassified as provider
                        # payload errors.
                        if event.event is None:
                            continue
                        terminal = assembler.terminal(
                            assembler.error("Provider returned invalid Anthropic JSON", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    event_type = dispatch_wire_event(chunk, known=_KNOWN_STREAM_EVENTS)
                    if event_type is None:
                        continue
                    shape_error = validate_wire_shape(
                        chunk,
                        event_type,
                        _ANTHROPIC_WIRE_SHAPES,
                    )
                    if shape_error is not None:
                        terminal = assembler.terminal(
                            assembler.error(shape_error, kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if event_type == "message_start":
                        message_started = True
                        message = chunk.get("message")
                        if not isinstance(message, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("message_start message must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if "usage" in message and not isinstance(message.get("usage"), Mapping):
                            terminal = assembler.terminal(
                                assembler.error("message_start usage must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        usage_error = usage_counter_error(
                            message.get("usage"),
                            label="message_start usage",
                            fields=(
                                "input_tokens",
                                "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                            ),
                            nested_fields={
                                "cache_creation": ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
                                "output_tokens_details": ("thinking_tokens",),
                            },
                        )
                        if usage_error is not None:
                            terminal = assembler.terminal(assembler.error(usage_error, kind="invalid_request"))
                            if terminal is not None:
                                yield terminal
                            return
                        assembler.set_usage(
                            _anthropic_usage(message.get("usage"))
                        )
                    elif event_type == "content_block_start":
                        index = _stream_index(chunk)
                        if index is None:
                            terminal = assembler.terminal(
                                assembler.error("content_block_start index must be an integer", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        block = chunk.get("content_block")
                        if not isinstance(block, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("content_block must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        kind = block.get("type")
                        if not isinstance(kind, str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "content_block type must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if kind == "fallback":
                            if assembler.has_content():
                                terminal = assembler.terminal(
                                    assembler.exception(
                                        RuntimeError(
                                            "Anthropic performed an unsupported mid-output model fallback"
                                        )
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            continue
                        if kind == "text":
                            block_state[index] = {"kind": "text"}
                            assembler.ensure_text_slot(index)
                            initial = block.get("text")
                            if initial is not None and not isinstance(initial, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "text content must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if initial:
                                emitted = assembler.text_delta(index, initial)
                                if emitted is not None:
                                    yield emitted
                        elif kind == "thinking":
                            raw_signature = block.get("signature")
                            if raw_signature is not None and not isinstance(raw_signature, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "thinking signature must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            signature = raw_signature
                            signature = signature if isinstance(signature, str) and signature else None
                            block_state[index] = {"kind": "thinking"}
                            assembler.ensure_thinking_slot(index, signature=signature)
                            initial = block.get("thinking")
                            if initial is not None and not isinstance(initial, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "thinking content must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if initial:
                                emitted = assembler.thinking_delta(index, initial)
                                if emitted is not None:
                                    yield emitted
                        elif kind == "redacted_thinking":
                            signature = block.get("data")
                            if not isinstance(signature, str) or not signature:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "redacted_thinking data must be a non-empty string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            assembler.redacted_thinking(index, signature)
                            block_state[index] = {"kind": "redacted"}
                        elif kind == "tool_use":
                            raw_id = block.get("id")
                            if raw_id is not None and not isinstance(raw_id, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "tool_use id must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            raw_name = block.get("name")
                            if raw_name is not None and not isinstance(raw_name, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "tool_use name must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            native_id = _string(raw_id) or None
                            name = _string(raw_name)
                            if not name:
                                terminal = assembler.terminal(
                                    assembler.error("tool_use name must not be empty", kind="invalid_request")
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            block_state[index] = {"kind": "tool"}
                            start = assembler.tool_start(index, name=name, native_id=native_id)
                            if start is not None:
                                yield start
                            initial_input = block.get("input")
                            if initial_input is not None and not isinstance(initial_input, Mapping):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "tool_use input must be an object",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if isinstance(initial_input, Mapping):
                                assembler.set_tool_arguments_object(index, initial_input)
                    elif event_type == "content_block_delta":
                        index = _stream_index(chunk)
                        if index is None:
                            terminal = assembler.terminal(
                                assembler.error("content_block_delta index must be an integer", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        delta = chunk.get("delta")
                        if not isinstance(delta, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("content_block_delta delta must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        delta_type = delta.get("type")
                        if not isinstance(delta_type, str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "content_block_delta type must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        state = block_state.get(index)
                        if state is None:
                            # Pi ignores content deltas for block kinds it
                            # does not know yet; preserve that forward-
                            # compatible policy instead of inventing text.
                            continue
                        if delta_type == "text_delta":
                            if state.get("kind") != "text":
                                continue
                            raw_text = delta.get("text")
                            if "text" in delta and not isinstance(raw_text, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "text_delta text must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_text)
                            if value:
                                emitted = assembler.text_delta(index, value)
                                if emitted is not None:
                                    yield emitted
                        elif delta_type == "thinking_delta":
                            if state.get("kind") != "thinking":
                                continue
                            raw_thinking = delta.get("thinking")
                            if "thinking" in delta and not isinstance(raw_thinking, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "thinking_delta thinking must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_thinking)
                            if value:
                                emitted = assembler.thinking_delta(index, value)
                                if emitted is not None:
                                    yield emitted
                        elif delta_type == "signature_delta":
                            if state.get("kind") != "thinking":
                                continue
                            raw_signature = delta.get("signature")
                            if "signature" in delta and not isinstance(raw_signature, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "signature_delta signature must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_signature)
                            if value:
                                assembler.append_thinking_signature(index, value)
                        elif delta_type == "input_json_delta":
                            if state.get("kind") != "tool":
                                continue
                            raw_partial_json = delta.get("partial_json")
                            if "partial_json" in delta and not isinstance(raw_partial_json, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "input_json_delta partial_json must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_partial_json)
                            emitted = assembler.tool_arguments(index, value)
                            if emitted is not None:
                                yield emitted
                        # Pi ignores delta variants it does not know yet.
                        # Known event shape has already been validated above.
                    elif event_type == "content_block_stop":
                        index = _stream_index(chunk)
                        if index is None:
                            terminal = assembler.terminal(
                                assembler.error("content_block_stop index must be an integer", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        end = assembler.block_end(index)
                        if end is not None:
                            yield end
                    elif event_type == "message_delta":
                        delta = chunk.get("delta")
                        if "delta" in chunk and not isinstance(delta, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("message_delta delta must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        usage = chunk.get("usage")
                        if usage is not None and not isinstance(usage, Mapping):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "message_delta usage must be an object",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        usage_error = usage_counter_error(
                            usage,
                            label="message_delta usage",
                            fields=(
                                "input_tokens",
                                "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                            ),
                            nested_fields={
                                "cache_creation": ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
                                "output_tokens_details": ("thinking_tokens",),
                            },
                        )
                        if usage_error is not None:
                            terminal = assembler.terminal(assembler.error(usage_error, kind="invalid_request"))
                            if terminal is not None:
                                yield terminal
                            return
                        if isinstance(delta, Mapping):
                            if delta.get("stop_reason") is not None:
                                if not isinstance(delta.get("stop_reason"), str):
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "message_delta stop_reason must be a string",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                stop_reason = _normalize_stop_reason(delta.get("stop_reason"))
                        assembler.set_usage(_merge_anthropic_usage(assembler.usage, usage))
                    elif event_type == "error":
                        shape_error = _error_event_shape_error(chunk)
                        if shape_error is not None:
                            terminal = assembler.terminal(
                                assembler.error(shape_error, kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        terminal = assembler.terminal(
                            assembler.error(
                                json.dumps(dict(chunk), ensure_ascii=False),
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    elif event_type == "message_stop":
                        message_stopped = True
                        if stop_reason is None:
                            terminal = assembler.terminal(
                                assembler.error(
                                    "Anthropic stream ended without a stop reason",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        final = assembler.finalize(
                            stop_reason or ("tool_use" if assembler.has_tools() else "stop")
                        )
                        terminal = assembler.terminal(final if isinstance(final, ProviderError) else Done(final))
                        if terminal is not None:
                            yield terminal
                        return
                    elif event_type == "ping":
                        continue
                if cancel.cancelled:
                    terminal = assembler.terminal(assembler.aborted(cancel.reason))
                    if terminal is not None:
                        yield terminal
                    return
                if message_started and not message_stopped:
                    terminal = assembler.terminal(assembler.incomplete())
                    if terminal is not None:
                        yield terminal
                    return
                if stop_reason is not None:
                    final = assembler.finalize(stop_reason)
                    terminal = assembler.terminal(
                        final if isinstance(final, ProviderError) else Done(final)
                    )
                    if terminal is not None:
                        yield terminal
                    return
                terminal = assembler.terminal(assembler.incomplete())
                if terminal is not None:
                    yield terminal
            driver = drive_sse_stream(
                self._client,
                method="POST",
                url=url,
                json_body=payload,
                headers=headers,
                cancel=cancel,
                assembler=assembler,
                endpoint=request.endpoint,
                resolver=self._served_hop_resolver,
                gateway=self._gateway,
                translate=translate,
            )
            try:
                async for event in driver:
                    yield event
            finally:
                await driver.aclose()
        except Exception as exc:
            terminal = assembler.terminal(assembler.exception(exc))
            if terminal is not None:
                yield terminal


def build_messages_payload(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the Anthropic Messages request body."""

    payload_messages: list[dict[str, Any]] = []
    for message in messages:
        converted = _message_to_anthropic(message, request.supports_images, loaded_images)
        if converted is None:
            continue
        if converted["role"] == "assistant" and not converted["content"]:
            continue
        payload_messages.append(converted)

    system: Any = request.system
    if request.cache == "default" and request.system:
        system = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
    if request.cache == "default":
        _mark_message_breakpoints(payload_messages)
    payload: dict[str, Any] = {
        "model": request.endpoint.model_id,
        "max_tokens": request.max_tokens,
        "stream": True,
        "system": system,
        "messages": payload_messages,
    }
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.input_schema),
                **(
                    {"cache_control": {"type": "ephemeral"}}
                    if request.cache == "default" and index == len(request.tools) - 1
                    else {}
                ),
            }
            for index, tool in enumerate(request.tools)
        ]
    effort = (request.reasoning_effort or "").lower()
    if effort in {"none", "off", "disabled"}:
        payload["thinking"] = {"type": "disabled"}
    elif effort:
        if _uses_adaptive_thinking(request.endpoint.model_id):
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": _anthropic_effort(effort)}
        else:
            # Pi's Anthropic adapter uses legacy budget thinking for Claude
            # 4.5 and older families. The API requires room for the thinking
            # budget and a response, so expand the request ceiling exactly as
            # Pi's adjustMaxTokensForThinking helper does.
            budget = _LEGACY_THINKING_BUDGETS.get(effort, _LEGACY_THINKING_BUDGETS["high"])
            payload["max_tokens"] = max(payload["max_tokens"], budget + 1024)
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
    return payload


def _message_to_anthropic(
    message: Any,
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    if isinstance(message, UserMessage):
        content = _anthropic_user_content(message.content, supports_images, loaded_images)
        return {"role": "user", "content": content if len(content) != 1 or content[0]["type"] != "text" else content[0]["text"]}
    if isinstance(message, AssistantMessage):
        content: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text:
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                if block.redacted:
                    if block.signature:
                        content.append({"type": "redacted_thinking", "data": block.signature})
                elif block.signature is not None:
                    content.append({"type": "thinking", "thinking": block.text, "signature": block.signature})
                elif block.text:
                    content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.arguments)})
        return {"role": "assistant", "content": content}
    if isinstance(message, ToolResultMessage):
        result = _anthropic_user_content(message.content, supports_images, loaded_images)
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": result,
                    "is_error": message.is_error,
                }
            ],
        }
    return None


def _anthropic_user_content(
    content: tuple[Any, ...],
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    parts = content_parts(content, include_images=supports_images, loaded_images=loaded_images)
    result: list[dict[str, Any]] = []
    for part in parts:
        if part["type"] == "text":
            result.append({"type": "text", "text": part["text"]})
        else:
            result.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part["mime_type"],
                        "data": part["data"],
                    },
                }
            )
    if not result:
        result.append({"type": "text", "text": "(no content)"})
    return result


def _mark_message_breakpoints(messages: list[dict[str, Any]]) -> None:
    candidates = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and isinstance(message.get("content"), (str, list))
    ]
    for index in candidates[-2:]:
        content = messages[index]["content"]
        if isinstance(content, str):
            messages[index]["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif content:
            last = content[-1]
            if isinstance(last, dict) and last.get("type") in {"text", "image", "tool_result"}:
                last["cache_control"] = {"type": "ephemeral"}


def _error_event_shape_error(chunk: Mapping[str, Any]) -> str | None:
    error = chunk.get("error")
    if error is not None and not isinstance(error, Mapping):
        return "Anthropic error must be an object"
    if isinstance(error, Mapping):
        for field in ("type", "message"):
            value = error.get(field)
            if value is not None and not isinstance(value, str):
                return f"Anthropic error {field} must be a string"
    return None


def _anthropic_usage(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    cache_write = _anthropic_cache_write_tokens(value)
    return _usage(
        _nonnegative(value.get("input_tokens")),
        _nonnegative(value.get("output_tokens")),
        _nonnegative(value.get("cache_read_input_tokens")),
        _nonnegative(cache_write),
    )


def _merge_anthropic_usage(current: Any, value: Any) -> Any:
    if not isinstance(value, Mapping):
        return current
    if current is None:
        current = _usage()
    return _usage(
        _pick(value, "input_tokens", current.input_tokens),
        _pick(value, "output_tokens", current.output_tokens),
        _pick(value, "cache_read_input_tokens", current.cache_read_tokens),
        _anthropic_cache_write_tokens(value, current.cache_write_tokens),
        _pick_nested(value, "output_tokens_details", "thinking_tokens", current.reasoning_tokens),
    )


def _anthropic_cache_write_tokens(value: Mapping[str, Any], fallback: int = 0) -> int:
    """Keep Anthropic's aggregate cache count, falling back only when absent."""

    aggregate = value.get("cache_creation_input_tokens")
    if "cache_creation_input_tokens" in value and aggregate is not None:
        return _nonnegative(aggregate)
    breakdown = value.get("cache_creation")
    if isinstance(breakdown, Mapping):
        return sum(
            _nonnegative(breakdown.get(key))
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
    return fallback


def _usage(input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0, reasoning: int | None = None) -> Any:
    from core.agent_core.messages import Usage

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def _normalize_stop_reason(value: Any) -> str:
    normalized = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "pause_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_use",
        "refusal": "refusal",
        "safety": "safety",
    }.get(str(value))
    return normalized or "error"


def _anthropic_effort(effort: str) -> str:
    return {"minimal": "low", "xhigh": "max"}.get(effort, effort)


def _uses_adaptive_thinking(model_id: str) -> bool:
    normalized = model_id.lower().replace(".", "-")
    return any(
        family in normalized
        for family in (
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-fable-5",
            "claude-mythos-5",
        )
    )


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _stream_index(chunk: Mapping[str, Any]) -> int | None:
    value = chunk.get("index")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _pick(value: Mapping[str, Any], key: str, default: int) -> int:
    candidate = value.get(key)
    return default if candidate is None else _nonnegative(candidate)


def _pick_nested(value: Mapping[str, Any], parent: str, key: str, default: int | None) -> int | None:
    nested = value.get(parent)
    if not isinstance(nested, Mapping):
        return default
    candidate = nested.get(key)
    return default if candidate is None else _nonnegative(candidate)


AnthropicProvider = AnthropicAdapter
