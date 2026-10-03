"""OpenAI Chat Completions streaming adapter.

Payload and stream rules are ported from tau's ``tau_ai/openai_compatible.py``
(MIT) and Pi's ``packages/ai/src/api/openai-completions.ts`` (MIT).
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
    endpoint_origin,
    join_endpoint_url,
    json_object,
    prepare_messages,
    StreamAssembler,
    validate_wire_shape,
    WireField,
    usage_counter_error,
)
from core.agent_core.ai.provider import (
    Done,
    MediaLoader,
    ModelRequest,
    ProviderAdapter,
    ProviderError,
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


_CHAT_USAGE_FIELDS = (
    WireField("prompt_tokens", int),
    WireField("completion_tokens", int),
    WireField("prompt_cache_hit_tokens", int),
    WireField("cached_tokens", int),
    WireField(
        "prompt_tokens_details",
        Mapping,
        children=(
            WireField("cached_tokens", int),
            WireField("cache_write_tokens", int),
        ),
    ),
    WireField(
        "completion_tokens_details",
        Mapping,
        children=(WireField("reasoning_tokens", int),),
    ),
)
_CHAT_REASONING_DETAIL_FIELDS = (
    WireField("type", str),
    WireField("text", str),
    WireField("summary", str),
    WireField("id", str),
    WireField("format", str),
    WireField("index", int),
    WireField("signature", str),
    WireField("data", str),
    WireField("encrypted_content", str),
)
_CHAT_TOOL_CALL_FIELDS = (
    WireField("id", str),
    WireField("index", int),
    WireField(
        "function",
        Mapping,
        children=(
            WireField("name", str),
            WireField("arguments", str),
        ),
    ),
)
_CHAT_DELTA_FIELDS = (
    WireField("content", str),
    WireField("reasoning_content", str),
    WireField("reasoning", str),
    WireField("reasoning_text", str),
    WireField("refusal", str),
    WireField(
        "reasoning_details",
        list,
        item_children=_CHAT_REASONING_DETAIL_FIELDS,
    ),
    WireField(
        "tool_calls",
        list,
        item_children=_CHAT_TOOL_CALL_FIELDS,
    ),
    WireField(
        "function_call",
        Mapping,
        children=(
            WireField("name", str),
            WireField("arguments", str),
        ),
    ),
)
_CHAT_WIRE_SHAPES = {
    "chunk": (
        WireField("usage", Mapping, children=_CHAT_USAGE_FIELDS),
        WireField(
            "error",
            Mapping,
            children=(
                WireField("type", str),
                WireField("code", (str, int)),
                WireField("message", str),
            ),
        ),
        WireField(
            "choices",
            list,
            item_children=(
                WireField("finish_reason", str),
                WireField("native_finish_reason", str),
                WireField("usage", Mapping, children=_CHAT_USAGE_FIELDS),
                WireField(
                    "delta",
                    Mapping,
                    children=_CHAT_DELTA_FIELDS,
                ),
            ),
        ),
    ),
}


class OpenAIChatAdapter(ProviderAdapter):
    """Adapter for OpenAI-compatible ``/chat/completions`` endpoints."""

    protocol = "openai_chat"

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
        target = endpoint_origin(request.endpoint)
        assembler = StreamAssembler(
            origin=target,
            protocol=self.protocol,
            verified_origin=not self._gateway,
            endpoint_url=request.endpoint.base_url,
        )
        if cancel.cancelled:
            terminal = assembler.terminal(assembler.aborted(cancel.reason))
            if terminal is not None:
                yield terminal
            return
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
        finish_reason: str | None = None
        native_finish_reason: str | None = None
        refusal_seen = False
        protocol_terminal = False
        try:
            payload = build_chat_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="openai", gateway=self._gateway)
            headers.setdefault("content-type", "application/json")
            url = join_endpoint_url(request.endpoint.base_url, "/chat/completions")
            async def translate(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
                nonlocal finish_reason, native_finish_reason, refusal_seen, protocol_terminal
                async for event in events:
                    if cancel.cancelled:
                        terminal = assembler.terminal(assembler.aborted(cancel.reason))
                        if terminal is not None:
                            yield terminal
                        return
                    if not event.data:
                        continue
                    if event.data == "[DONE]":
                        break
                    chunk = json_object(event.data)
                    if chunk is None:
                        terminal = assembler.terminal(
                            assembler.error("Provider returned invalid OpenAI Chat JSON", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    shape_error = validate_wire_shape(chunk, "chunk", _CHAT_WIRE_SHAPES)
                    if shape_error is not None:
                        terminal = assembler.terminal(
                            assembler.error(shape_error, kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if "usage" in chunk and chunk.get("usage") is not None and not isinstance(chunk.get("usage"), Mapping):
                        terminal = assembler.terminal(
                            assembler.error("OpenAI Chat usage must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    usage_error = usage_counter_error(
                        chunk.get("usage"),
                        label="OpenAI Chat usage",
                        fields=("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "cached_tokens"),
                        nested_fields={
                            "prompt_tokens_details": ("cached_tokens", "cache_write_tokens"),
                            "completion_tokens_details": ("reasoning_tokens",),
                        },
                    )
                    if usage_error is not None:
                        terminal = assembler.terminal(assembler.error(usage_error, kind="invalid_request"))
                        if terminal is not None:
                            yield terminal
                        return
                    if isinstance(chunk.get("usage"), Mapping):
                        assembler.set_usage(_openai_usage(chunk["usage"]))
                    top_level_error = chunk.get("error")
                    if top_level_error is not None and not isinstance(top_level_error, Mapping):
                        terminal = assembler.terminal(
                            assembler.error("OpenAI Chat error must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if isinstance(top_level_error, Mapping):
                        for field in ("type", "code", "message"):
                            value = top_level_error.get(field)
                            if field == "code" and isinstance(value, int) and not isinstance(value, bool):
                                continue
                            if value is not None and not isinstance(value, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        f"OpenAI Chat error {field} must be a string",
                                        kind="invalid_request",
                                    )
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
                    choices = chunk.get("choices")
                    if choices is not None and not isinstance(choices, list):
                        terminal = assembler.terminal(
                            assembler.error("OpenAI Chat choices must be an array", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if choices is None or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, Mapping):
                        terminal = assembler.terminal(
                            assembler.error("OpenAI Chat choice must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    choice_usage = choice.get("usage")
                    if choice_usage is not None and not isinstance(choice_usage, Mapping):
                        terminal = assembler.terminal(
                            assembler.error("OpenAI Chat choice usage must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    usage_error = usage_counter_error(
                        choice_usage,
                        label="OpenAI Chat choice usage",
                        fields=("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "cached_tokens"),
                        nested_fields={
                            "prompt_tokens_details": ("cached_tokens", "cache_write_tokens"),
                            "completion_tokens_details": ("reasoning_tokens",),
                        },
                    )
                    if usage_error is not None:
                        terminal = assembler.terminal(assembler.error(usage_error, kind="invalid_request"))
                        if terminal is not None:
                            yield terminal
                        return
                    if not isinstance(chunk.get("usage"), Mapping) and isinstance(choice_usage, Mapping):
                        assembler.set_usage(_openai_usage(choice_usage))
                    if choice.get("finish_reason") is not None:
                        if not isinstance(choice.get("finish_reason"), str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "OpenAI Chat finish_reason must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                                return
                        finish_reason = _string(choice.get("finish_reason"))
                    raw_native_finish_reason = choice.get("native_finish_reason")
                    if raw_native_finish_reason is not None and not isinstance(
                        raw_native_finish_reason, str
                    ):
                        terminal = assembler.terminal(
                            assembler.error(
                                "OpenAI Chat native_finish_reason must be a string",
                                kind="invalid_request",
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if raw_native_finish_reason is not None:
                        native_finish_reason = _string(raw_native_finish_reason)
                    delta = choice.get("delta")
                    if delta is None or not isinstance(delta, Mapping):
                        if delta is None:
                            continue
                        terminal = assembler.terminal(
                            assembler.error("OpenAI Chat delta must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    raw_content = delta.get("content")
                    if raw_content is not None and not isinstance(raw_content, str):
                        terminal = assembler.terminal(
                            assembler.error(
                                "OpenAI Chat content must be a string",
                                kind="invalid_request",
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    value = _string(raw_content)
                    if value:
                        emitted = assembler.text_delta("chat-text", value)
                        if emitted is not None:
                            yield emitted
                    reasoning_field, reasoning = _first_reasoning_delta(delta)
                    for field in ("reasoning_content", "reasoning", "reasoning_text"):
                        raw_reasoning = delta.get(field)
                        if raw_reasoning is not None and not isinstance(raw_reasoning, str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    f"OpenAI Chat {field} must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                    if reasoning:
                        emitted = assembler.thinking_delta("chat-thinking", reasoning, signature=reasoning_field)
                        if emitted is not None:
                            yield emitted
                    raw_refusal = delta.get("refusal")
                    if raw_refusal is not None and not isinstance(raw_refusal, str):
                        terminal = assembler.terminal(
                            assembler.error(
                                "OpenAI Chat refusal must be a string",
                                kind="invalid_request",
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    refusal = _string(raw_refusal)
                    if refusal:
                        refusal_seen = True
                        emitted = assembler.text_delta("chat-text", refusal)
                        if emitted is not None:
                            yield emitted
                    details = delta.get("reasoning_details")
                    if details is not None and not isinstance(details, list):
                        terminal = assembler.terminal(
                            assembler.error(
                                "OpenAI Chat reasoning_details must be an array",
                                kind="invalid_request",
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if isinstance(details, list):
                        if any(not isinstance(detail, Mapping) for detail in details):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "OpenAI Chat reasoning_details entries must be objects",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        assembler.merge_thinking_details("chat-thinking", details)
                    raw_tool_calls = delta.get("tool_calls")
                    if raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
                        terminal = assembler.terminal(
                            assembler.error("OpenAI Chat tool_calls must be an array", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if isinstance(raw_tool_calls, list):
                        for raw in raw_tool_calls:
                            if not isinstance(raw, Mapping):
                                terminal = assembler.terminal(
                                    assembler.error("tool_calls entry must be an object", kind="invalid_request")
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            raw_id = _string(raw.get("id"))
                            if raw.get("id") is not None and not isinstance(raw.get("id"), str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "OpenAI Chat tool call id must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            index_value = raw.get("index")
                            if index_value is not None and (
                                not isinstance(index_value, int) or isinstance(index_value, bool)
                            ):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "OpenAI Chat tool call index must be an integer",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if isinstance(index_value, int) and not isinstance(index_value, bool):
                                key = ("chat-tool", index_value)
                            else:
                                function = raw.get("function")
                                has_new_identity = bool(raw_id) or (
                                    isinstance(function, Mapping) and function.get("name") is not None
                                )
                                key = assembler.fallback_tool_key(
                                    native_id=raw_id or None,
                                    allocate=has_new_identity,
                                )
                                if key is None:
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "OpenAI Chat tool call could not allocate a slot",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                            function = raw.get("function")
                            if function is not None and not isinstance(function, Mapping):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "OpenAI Chat tool call function must be an object",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if isinstance(function, Mapping):
                                raw_name = function.get("name")
                                if raw_name is not None and not isinstance(raw_name, str):
                                    terminal = assembler.terminal(
                                        assembler.error("tool call name must not be empty", kind="invalid_request")
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                if raw_name == "":
                                    terminal = assembler.terminal(
                                        assembler.error("tool call name must not be empty", kind="invalid_request")
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                name = _string(raw_name)
                                start = assembler.tool_start(key, name=name or None, native_id=raw_id or None)
                                if start is not None:
                                    yield start
                                    pending = assembler.pending_tool_arguments(key)
                                    if pending is not None:
                                        yield pending
                                arguments = _string(function.get("arguments"))
                                raw_arguments = function.get("arguments")
                                if raw_arguments is not None and not isinstance(raw_arguments, str):
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "OpenAI Chat tool arguments must be a string",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                emitted = assembler.tool_arguments(key, arguments)
                                if emitted is not None:
                                    yield emitted
                            elif raw_id:
                                assembler.tool_start(key, native_id=raw_id)
                    legacy_function = delta.get("function_call")
                    if legacy_function is not None:
                        if not isinstance(legacy_function, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("legacy function_call must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        key = ("legacy-tool", 0)
                        raw_name = legacy_function.get("name")
                        if raw_name is not None and not isinstance(raw_name, str):
                            terminal = assembler.terminal(
                                assembler.error("legacy function_call name must not be empty", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if raw_name == "":
                            terminal = assembler.terminal(
                                assembler.error("legacy function_call name must not be empty", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        name = _string(raw_name)
                        start = assembler.tool_start(key, name=name or None, native_id=None)
                        if start is not None:
                            yield start
                            pending = assembler.pending_tool_arguments(key)
                            if pending is not None:
                                yield pending
                        arguments = _string(legacy_function.get("arguments"))
                        raw_arguments = legacy_function.get("arguments")
                        if raw_arguments is not None and not isinstance(raw_arguments, str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "legacy function_call arguments must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        emitted = assembler.tool_arguments(key, arguments)
                        if emitted is not None:
                            yield emitted
                if cancel.cancelled:
                    terminal = assembler.terminal(assembler.aborted(cancel.reason))
                    if terminal is not None:
                        yield terminal
                    return
                if finish_reason is not None:
                    protocol_terminal = True
                if not protocol_terminal:
                    terminal = assembler.terminal(assembler.incomplete())
                    if terminal is not None:
                        yield terminal
                    return
                for event in assembler.block_end_events():
                    yield event
                final = assembler.finalize(
                    "refusal"
                    if refusal_seen
                    else _normalize_stop(
                        finish_reason,
                        assembler.has_tools(),
                        native_finish_reason=native_finish_reason,
                    )
                )
                terminal = assembler.terminal(final if isinstance(final, ProviderError) else Done(final))
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


def build_chat_payload(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    payload_messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
    payload_messages.extend(_message_to_chat(message, request.supports_images, loaded_images) for message in messages)
    payload: dict[str, Any] = {
        "model": request.endpoint.model_id,
        "messages": payload_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if _uses_completion_tokens(request.endpoint.model_id, request.reasoning_effort):
        payload["max_completion_tokens"] = request.max_tokens
    else:
        payload["max_tokens"] = request.max_tokens
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                },
            }
            for tool in request.tools
        ]
    if request.reasoning_effort and request.reasoning_effort.lower() not in {"off", "disabled"}:
        payload["reasoning_effort"] = _openai_effort(request.reasoning_effort)
    return payload


def _message_to_chat(
    message: Any,
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        parts = _chat_content(message.content, supports_images, loaded_images)
        return {"role": "user", "content": parts[0]["text"] if len(parts) == 1 and parts[0]["type"] == "text" else parts}
    if isinstance(message, AssistantMessage):
        output: dict[str, Any] = {"role": "assistant", "content": None}
        text_parts = [block.text for block in message.content if isinstance(block, TextBlock) and block.text]
        if text_parts:
            output["content"] = "".join(text_parts)
        thinking = [block for block in message.content if isinstance(block, ThinkingBlock)]
        for block in thinking:
            if block.signature is None:
                continue
            details = _signature_details(block.signature)
            if details is not None:
                output["reasoning_details"] = details
            elif block.signature in {"reasoning", "reasoning_content", "reasoning_text"}:
                output[block.signature] = block.text
            else:
                output["reasoning_content"] = block.text
        calls = [block for block in message.content if isinstance(block, ToolCallBlock)]
        if calls:
            output["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments), ensure_ascii=False)},
                }
                for call in calls
            ]
        if output["content"] is None and "tool_calls" not in output:
            output["content"] = ""
        return output
    if isinstance(message, ToolResultMessage):
        values = content_parts(message.content, include_images=supports_images, loaded_images=loaded_images)
        text = "\n".join(
            part["text"] if part["type"] == "text" else f"[image: {part['media_token']}]"
            for part in values
        )
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": text or "(no tool output)"}
    raise TypeError(f"unsupported message {type(message).__name__}")


def _chat_content(
    content: tuple[Any, ...],
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in content_parts(content, include_images=supports_images, loaded_images=loaded_images):
        if part["type"] == "text":
            result.append({"type": "text", "text": part["text"]})
        else:
            result.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{part['mime_type']};base64,{part['data']}"},
                }
            )
    return result or [{"type": "text", "text": "(no content)"}]


def _normalize_stop(
    reason: str | None,
    has_tools: bool,
    *,
    native_finish_reason: str | None = None,
) -> str:
    if reason == "stop" and native_finish_reason:
        native = native_finish_reason.lower()
        if native not in {"stop", "end_turn", "tool_calls", "function_call"}:
            reason = native
    normalized = {
        "length": "length",
        "max_tokens": "length",
        "content_filter": "safety",
        "recitation": "safety",
        "refusal": "refusal",
        "safety": "safety",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "stop": "stop",
        "end": "stop",
        None: "stop",
    }.get(reason)
    if normalized is not None:
        return normalized
    return "error"


def _openai_usage(value: Mapping[str, Any]) -> Any:
    from core.agent_core.messages import Usage

    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    cache_read_tokens = (
        _nonnegative(prompt_details.get("cached_tokens"))
        if isinstance(prompt_details, Mapping)
        else _nonnegative(value.get("prompt_cache_hit_tokens") or value.get("cached_tokens"))
    )
    cache_write_tokens = (
        _nonnegative(prompt_details.get("cache_write_tokens"))
        if isinstance(prompt_details, Mapping)
        else 0
    )
    return Usage(
        input_tokens=max(0, _nonnegative(value.get("prompt_tokens")) - cache_read_tokens - cache_write_tokens),
        output_tokens=_nonnegative(value.get("completion_tokens")),
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=(
            _nonnegative(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, Mapping) and "reasoning_tokens" in completion_details
            else None
        ),
    )


def _first_reasoning_delta(delta: Mapping[str, Any]) -> tuple[str | None, str]:
    for key in ("reasoning_content", "reasoning", "reasoning_text"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return key, value
    return None, ""


def _signature_details(signature: str) -> Any:
    try:
        value = json.loads(signature)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, (list, dict)) else None


def _openai_effort(value: str) -> str:
    # Pi preserves extended tiers when the resolved model advertises them;
    # the loop has already filtered ``reasoning_effort`` through
    # ModelCapabilities.reasoning_efforts.
    return value.lower()


def _uses_completion_tokens(model: str, effort: str | None) -> bool:
    normalized = model.lower()
    if any(normalized.startswith(prefix) for prefix in ("o1", "o3", "o4", "gpt-5")):
        return True
    return effort is not None and effort.lower() not in {"none", "off", "disabled"} and "reasoning" in normalized


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


OpenAIChatProvider = OpenAIChatAdapter
