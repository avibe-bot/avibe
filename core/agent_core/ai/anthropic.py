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
    assistant_message,
    auth_headers,
    content_parts,
    endpoint_origin,
    incomplete_stream_error,
    iter_sse_events,
    json_object,
    open_stream,
    parsed_arguments,
    prepare_messages,
    resolve_served_origin,
    text_from_content,
)
from core.agent_core.ai.errors import classify_error
from core.agent_core.ai.provider import (
    BlockEnd,
    Done,
    ModelRequest,
    ProviderAdapter,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
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
_SYNTHETIC_EVENT_TYPES = {"message_stop", "content_block_stop"}


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
        if cancel.cancelled:
            yield _aborted_error(cancel.reason, origin)
            return
        target = origin
        prepared = await prepare_messages(
            request.messages,
            target=target,
            supports_images=request.supports_images,
            protocol=self.protocol,
            media_loader=self._media_loader,
        )
        if isinstance(prepared, ProviderError):
            yield prepared
            return
        transformed_messages, loaded_images = prepared
        if cancel.cancelled:
            yield _aborted_error(cancel.reason, origin)
            return
        content: list[Any] = []
        block_state: dict[int, dict[str, Any]] = {}
        content_indices: dict[int, int] = {}
        usage = None
        stop_reason: str | None = None
        streamed = False
        terminal_seen = False
        verified_origin = not self._gateway
        served_origin = origin
        try:
            payload = build_messages_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="anthropic", gateway=self._gateway)
            headers.update({"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})
            url = _endpoint_url(request.endpoint.base_url, "/messages")
            async with open_stream(
                self._client,
                method="POST",
                url=url,
                json_body=payload,
                headers=headers,
                cancel=cancel,
            ) as response:
                if response is None:
                    yield _aborted_error(cancel.reason, served_origin, content, usage, verified_origin)
                    return
                served_origin, verified_origin = resolve_served_origin(
                    request.endpoint,
                    response.headers,
                    self._served_hop_resolver,
                    gateway=self._gateway,
                )
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    yield classify_error(
                        status=response.status_code,
                        body=body,
                        headers=response.headers,
                        streamed=False,
                    )
                    return
                async for event in iter_sse_events(response, cancel):
                    if cancel.cancelled:
                        yield _aborted_error(cancel.reason, served_origin, content, usage, verified_origin)
                        return
                    if event.data == "[DONE]" or not event.data:
                        continue
                    chunk = json_object(event.data)
                    if chunk is None:
                        yield _stream_error(
                            "Provider returned invalid Anthropic JSON",
                            streamed=streamed,
                            content=content,
                            origin=served_origin,
                            usage=usage,
                            verified_origin=verified_origin,
                        )
                        return
                    event_type = chunk.get("type")
                    if event_type == "message_start":
                        usage = _anthropic_usage(chunk.get("message", {}).get("usage"))
                    elif event_type == "content_block_start":
                        index = _int(chunk.get("index"), 0)
                        block = chunk.get("content_block")
                        if not isinstance(block, Mapping):
                            continue
                        kind = block.get("type")
                        if kind == "text":
                            block_state[index] = {"kind": "text", "text": ""}
                            content_indices[index] = len(content)
                            content.append(TextBlock(text=""))
                        elif kind == "thinking":
                            signature = block.get("signature")
                            block_state[index] = {
                                "kind": "thinking",
                                "text": "",
                                "signature": signature if isinstance(signature, str) and signature else None,
                            }
                            content_indices[index] = len(content)
                            content.append(ThinkingBlock(text="", signature=block_state[index]["signature"]))
                        elif kind == "redacted_thinking":
                            signature = block.get("data")
                            if not isinstance(signature, str) or not signature:
                                continue
                            content_indices[index] = len(content)
                            content.append(
                                ThinkingBlock(
                                    text="",
                                    signature=signature,
                                    redacted=True,
                                )
                            )
                            block_state[index] = {"kind": "redacted", "text": ""}
                        elif kind == "tool_use":
                            native_id = _string(block.get("id")) or f"tool_{index}"
                            name = _string(block.get("name"))
                            block_state[index] = {
                                "kind": "tool",
                                "id": native_id,
                                "name": name,
                                "arguments": "",
                            }
                            content_indices[index] = len(content)
                            content.append(
                                ToolCallBlock(
                                    id=native_id,
                                    native_id=native_id,
                                    name=name,
                                    arguments={},
                                )
                            )
                            streamed = True
                            yield ToolCallStart(index=content_indices[index], id=native_id, name=name)
                    elif event_type == "content_block_delta":
                        index = _int(chunk.get("index"), 0)
                        delta = chunk.get("delta")
                        if not isinstance(delta, Mapping):
                            continue
                        delta_type = delta.get("type")
                        state = block_state.setdefault(index, {"kind": "text", "text": ""})
                        content_index = content_indices.get(index, index)
                        if delta_type == "text_delta":
                            value = _string(delta.get("text"))
                            if value:
                                state["text"] += value
                                _set_block_text(content, content_index, value, append=True)
                                streamed = True
                                yield TextDelta(index=content_index, delta=value)
                        elif delta_type == "thinking_delta":
                            value = _string(delta.get("thinking"))
                            if value:
                                state["text"] += value
                                _set_thinking_text(content, content_index, value)
                                streamed = True
                                yield ThinkingDelta(index=content_index, delta=value)
                        elif delta_type == "signature_delta":
                            value = _string(delta.get("signature"))
                            if value:
                                state["signature"] = f"{state.get('signature') or ''}{value}"
                                _set_thinking_signature(content, content_index, state["signature"])
                        elif delta_type == "input_json_delta":
                            value = _string(delta.get("partial_json"))
                            state["arguments"] = f"{state.get('arguments', '')}{value}"
                            streamed = True
                            yield ToolCallDelta(index=content_index, arguments_delta=value)
                    elif event_type == "content_block_stop":
                        index = _int(chunk.get("index"), 0)
                        yield BlockEnd(index=content_indices.get(index, index))
                    elif event_type == "message_delta":
                        delta = chunk.get("delta")
                        if isinstance(delta, Mapping):
                            if delta.get("stop_reason") is not None:
                                stop_reason = _normalize_stop_reason(delta.get("stop_reason"))
                        usage = _merge_anthropic_usage(usage, chunk.get("usage"))
                    elif event_type == "error":
                        message = _error_event_message(chunk)
                        yield _stream_error(
                            message,
                            streamed=streamed,
                            content=content,
                            origin=served_origin,
                            usage=usage,
                            verified_origin=verified_origin,
                        )
                        return
                    elif event_type == "message_stop":
                        terminal_seen = True
                        final = _final_message(
                            content,
                            block_state,
                            origin=served_origin,
                            stop_reason=stop_reason or ("tool_use" if _has_tools(content) else "stop"),
                            usage=usage,
                            verified_origin=verified_origin,
                        )
                        yield Done(final)
                        return
                if cancel.cancelled:
                    yield _aborted_error(cancel.reason, served_origin, content, usage, verified_origin)
                    return
                if not terminal_seen:
                    yield incomplete_stream_error(
                        content,
                        origin=served_origin,
                        usage=usage,
                        streamed=streamed,
                        verified_origin=verified_origin,
                        protocol=self.protocol,
                    )
                    return
                final = _final_message(
                    content,
                    block_state,
                    origin=served_origin,
                    stop_reason=stop_reason or ("tool_use" if _has_tools(content) else "stop"),
                    usage=usage,
                    verified_origin=verified_origin,
                )
                yield Done(final)
        except httpx.HTTPError as exc:
            yield classify_error(
                exc=exc,
                streamed=streamed,
                partial=_partial(content, served_origin, usage, verified_origin) if streamed else None,
            )


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
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": _anthropic_effort(effort)}
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


def _final_message(
    content: list[Any],
    states: Mapping[int, Mapping[str, Any]],
    *,
    origin: Any,
    stop_reason: str,
    usage: Any,
    verified_origin: bool,
) -> AssistantMessage:
    final: list[Any] = []
    for block in content:
        if isinstance(block, TextBlock):
            final.append(block)
        elif isinstance(block, ThinkingBlock):
            final.append(block)
        elif isinstance(block, ToolCallBlock):
            state = next(
                (
                    candidate
                    for candidate in states.values()
                    if candidate.get("kind") == "tool" and candidate.get("id") == block.id
                ),
                {},
            )
            args = parsed_arguments(str(state.get("arguments", "")))
            final.append(
                ToolCallBlock(
                    id=block.id,
                    native_id=block.native_id,
                    name=block.name,
                    arguments=args,
                )
            )
    return assistant_message(
        final,
        origin=origin,
        stop_reason=stop_reason,
        usage=usage,
        verified_origin=verified_origin,
    )


def _partial(content: list[Any], origin: Any, usage: Any, verified: bool) -> AssistantMessage:
    return assistant_message(content, origin=origin, stop_reason="error", usage=usage, verified_origin=verified)


def _stream_error(message: str, *, streamed: bool, content: list[Any], origin: Any, usage: Any, verified_origin: bool) -> ProviderError:
    return classify_error(
        body=message,
        streamed=streamed,
        partial=_partial(content, origin, usage, verified_origin) if streamed else None,
    )


def _aborted_error(
    reason: str | None,
    origin: Any,
    content: list[Any] | None = None,
    usage: Any = None,
    verified: bool = True,
) -> ProviderError:
    return ProviderError(
        kind="aborted",
        message=reason or "provider request aborted",
        retryable=False,
        partial=_partial(content, origin, usage, verified) if content else None,
    )


def _anthropic_usage(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    cache_creation = value.get("cache_creation")
    cache_write = value.get("cache_creation_input_tokens", 0)
    if not isinstance(cache_write, int) or isinstance(cache_write, bool) or cache_write < 0:
        cache_write = 0
        if isinstance(cache_creation, Mapping):
            cache_write = sum(
                _nonnegative(cache_creation.get(key))
                for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
            )
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
        _pick(value, "cache_creation_input_tokens", current.cache_write_tokens),
        _pick_nested(value, "output_tokens_details", "thinking_tokens", current.reasoning_tokens),
    )


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
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "pause_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_use",
        "refusal": "refusal",
        "safety": "safety",
    }.get(str(value), "stop")


def _anthropic_effort(effort: str) -> str:
    return {"minimal": "low", "xhigh": "max"}.get(effort, effort)


def _endpoint_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _error_event_message(chunk: Mapping[str, Any]) -> str:
    error = chunk.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        return error["message"]
    return json.dumps(dict(chunk), ensure_ascii=False)


def _set_block_text(content: list[Any], index: int, value: str, *, append: bool) -> None:
    if index >= len(content):
        return
    block = content[index]
    if isinstance(block, TextBlock):
        content[index] = TextBlock(text=(block.text or "") + value if append else value)


def _set_thinking_text(content: list[Any], index: int, value: str) -> None:
    if index >= len(content):
        return
    block = content[index]
    if isinstance(block, ThinkingBlock):
        content[index] = ThinkingBlock(text=block.text + value, signature=block.signature, redacted=block.redacted)


def _set_thinking_signature(content: list[Any], index: int, signature: str) -> None:
    if index >= len(content):
        return
    block = content[index]
    if isinstance(block, ThinkingBlock):
        content[index] = ThinkingBlock(text=block.text, signature=signature, redacted=block.redacted)


def _has_tools(content: list[Any]) -> bool:
    return any(isinstance(block, ToolCallBlock) for block in content)


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _pick(value: Mapping[str, Any], key: str, default: int) -> int:
    return _nonnegative(value[key]) if key in value else default


def _pick_nested(value: Mapping[str, Any], parent: str, key: str, default: int | None) -> int | None:
    nested = value.get(parent)
    return _nonnegative(nested[key]) if isinstance(nested, Mapping) and key in nested else default


AnthropicProvider = AnthropicAdapter
