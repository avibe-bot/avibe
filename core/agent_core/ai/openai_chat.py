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
    read_response_body,
    resolve_served_origin,
    terminal_event,
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
        if cancel.cancelled:
            yield _aborted(cancel.reason, target)
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
            yield prepared
            return
        transformed_messages, loaded_images = prepared
        if cancel.cancelled:
            yield _aborted(cancel.reason, target)
            return
        content: list[Any] = []
        tools: dict[int, dict[str, Any]] = {}
        usage = None
        finish_reason: str | None = None
        streamed = False
        terminal_seen = False
        verified = not self._gateway
        origin = target
        try:
            payload = build_chat_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="openai", gateway=self._gateway)
            headers.setdefault("content-type", "application/json")
            url = _endpoint_url(request.endpoint.base_url, "/chat/completions")
            async with open_stream(
                self._client,
                method="POST",
                url=url,
                json_body=payload,
                headers=headers,
                cancel=cancel,
            ) as response:
                if response is None:
                    yield _aborted(cancel.reason, origin, content, usage, verified)
                    return
                resolved_origin = await resolve_served_origin(
                    request.endpoint,
                    response.headers,
                    self._served_hop_resolver,
                    gateway=self._gateway,
                    cancel=cancel,
                )
                if resolved_origin is None:
                    yield terminal_event(response, _aborted(cancel.reason, origin, content, usage, verified))
                    return
                origin, verified = resolved_origin
                if response.status_code >= 400:
                    body = await read_response_body(response, cancel)
                    if body is None:
                        yield terminal_event(response, _aborted(cancel.reason, origin, content, usage, verified))
                        return
                    yield terminal_event(
                        response,
                        classify_error(status=response.status_code, body=body, headers=response.headers),
                    )
                    return
                async for event in iter_sse_events(response, cancel):
                    if cancel.cancelled:
                        yield terminal_event(response, _aborted(cancel.reason, origin, content, usage, verified))
                        return
                    if not event.data:
                        continue
                    if event.data == "[DONE]":
                        terminal_seen = True
                        break
                    chunk = json_object(event.data)
                    if chunk is None:
                        yield terminal_event(
                            response,
                            _error("Provider returned invalid OpenAI Chat JSON", streamed, content, origin, usage, verified),
                        )
                        return
                    if isinstance(chunk.get("usage"), Mapping):
                        usage = _openai_usage(chunk["usage"])
                    top_level_error = chunk.get("error")
                    if isinstance(top_level_error, Mapping):
                        yield terminal_event(
                            response,
                            _error(
                                _error_text(top_level_error, chunk),
                                streamed,
                                content,
                                origin,
                                usage,
                                verified,
                                code=_string(top_level_error.get("code"))
                                or _string(top_level_error.get("type"))
                                or None,
                            ),
                        )
                        return
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, Mapping):
                        continue
                    if choice.get("finish_reason") is not None:
                        finish_reason = _string(choice.get("finish_reason"))
                    delta = choice.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    value = _string(delta.get("content"))
                    if value:
                        content = _append_text(content, value)
                        streamed = True
                        yield TextDelta(index=_find_block_index(content, TextBlock), delta=value)
                    reasoning_field, reasoning = _first_reasoning_delta(delta)
                    if reasoning:
                        content = _append_thinking(content, reasoning, reasoning_field)
                        streamed = True
                        yield ThinkingDelta(index=_find_block_index(content, ThinkingBlock), delta=reasoning)
                    refusal = _string(delta.get("refusal"))
                    if refusal:
                        content = _append_text(content, refusal)
                        finish_reason = "refusal"
                        streamed = True
                        yield TextDelta(index=_find_block_index(content, TextBlock), delta=refusal)
                    details = delta.get("reasoning_details")
                    if isinstance(details, list):
                        content = _apply_reasoning_details(content, details)
                    raw_tool_calls = delta.get("tool_calls")
                    if isinstance(raw_tool_calls, list):
                        for raw in raw_tool_calls:
                            if not isinstance(raw, Mapping):
                                yield terminal_event(
                                    response,
                                    _invalid_tool_metadata("tool_calls entry must be an object"),
                                )
                                return
                            index = _int(raw.get("index"), len(tools))
                            state = tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            function = raw.get("function")
                            if isinstance(function, Mapping):
                                name = _string(function.get("name"))
                                if "name" in function and not name:
                                    yield terminal_event(
                                        response,
                                        _invalid_tool_metadata("tool call name must not be empty"),
                                    )
                                    return
                                if name and not state["name"]:
                                    state["name"] = name
                                    content.append(
                                        ToolCallBlock(
                                            id=_string(raw.get("id")) or f"call_{index}",
                                            native_id=_string(raw.get("id")) or None,
                                            name=name,
                                            arguments={},
                                        )
                                    )
                                    state["content_index"] = len(content) - 1
                                    state["id"] = _string(raw.get("id")) or f"call_{index}"
                                    streamed = True
                                    yield ToolCallStart(index=state["content_index"], id=state["id"], name=name)
                                arguments = _string(function.get("arguments"))
                                if arguments:
                                    state["arguments"] += arguments
                                    streamed = True
                                    yield ToolCallDelta(
                                        index=_int(state.get("content_index"), 0),
                                        arguments_delta=arguments,
                                    )
                            raw_id = _string(raw.get("id"))
                            if raw_id and not state["id"]:
                                state["id"] = raw_id
                    legacy_function = delta.get("function_call")
                    if legacy_function is not None:
                        if not isinstance(legacy_function, Mapping):
                            yield terminal_event(
                                response,
                                _invalid_tool_metadata("legacy function_call must be an object"),
                            )
                            return
                        state = tools.setdefault(0, {"id": "call_0", "name": "", "arguments": ""})
                        name = _string(legacy_function.get("name"))
                        if "name" in legacy_function and not name:
                            yield terminal_event(
                                response,
                                _invalid_tool_metadata("legacy function_call name must not be empty"),
                            )
                            return
                        if name and not state["name"]:
                            state["name"] = name
                            content.append(
                                ToolCallBlock(
                                    id=state["id"],
                                    native_id=state["id"],
                                    name=name,
                                    arguments={},
                                )
                            )
                            state["content_index"] = len(content) - 1
                            streamed = True
                            yield ToolCallStart(index=state["content_index"], id=state["id"], name=name)
                        arguments = _string(legacy_function.get("arguments"))
                        if arguments:
                            state["arguments"] += arguments
                            streamed = True
                            yield ToolCallDelta(
                                index=_int(state.get("content_index"), 0),
                                arguments_delta=arguments,
                            )
                if cancel.cancelled:
                    yield _aborted(cancel.reason, origin, content, usage, verified)
                    return
                if not terminal_seen:
                    yield terminal_event(
                        response,
                        incomplete_stream_error(
                            content,
                            origin=origin,
                            usage=usage,
                            streamed=streamed,
                            verified_origin=verified,
                            protocol=self.protocol,
                        ),
                    )
                    return
                for index in range(len(content)):
                    yield BlockEnd(index=index)
                final = _final_message(content, tools, origin, finish_reason, usage, verified)
                if isinstance(final, ProviderError):
                    yield terminal_event(response, final)
                else:
                    yield terminal_event(response, Done(final))
        except Exception as exc:
            yield classify_error(
                exc=exc,
                streamed=streamed,
                partial=(
                    assistant_message(content, origin=origin, stop_reason="error", usage=usage, verified_origin=verified)
                    if streamed
                    else None
                ),
            )


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
    if request.reasoning_effort and request.reasoning_effort.lower() not in {"none", "off", "disabled"}:
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


def _final_message(
    content: list[Any],
    tools: Mapping[int, Mapping[str, Any]],
    origin: Any,
    finish_reason: str | None,
    usage: Any,
    verified: bool,
) -> AssistantMessage | ProviderError:
    for state in tools.values():
        if state.get("id") and not state.get("name"):
            return _invalid_tool_metadata("tool call name is missing")
    final: list[Any] = []
    for block in content:
        if isinstance(block, ToolCallBlock):
            state = next(
                (
                    candidate
                    for candidate in tools.values()
                    if candidate.get("id") == block.id
                ),
                {},
            )
            arguments = parsed_arguments(str(state.get("arguments", "")), tool_name=block.name)
            if isinstance(arguments, ProviderError):
                return arguments
            final.append(
                ToolCallBlock(
                    id=block.id,
                    native_id=block.native_id,
                    name=block.name,
                    arguments=arguments,
                )
            )
        else:
            final.append(block)
    return assistant_message(
        final,
        origin=origin,
        stop_reason=_normalize_stop(finish_reason, bool(tools)),
        usage=usage,
        verified_origin=verified,
    )


def _normalize_stop(reason: str | None, has_tools: bool) -> str:
    normalized = {
        "length": "length",
        "content_filter": "safety",
        "refusal": "refusal",
        "safety": "safety",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "stop": "stop",
        None: "stop",
    }.get(reason)
    if normalized is not None:
        return normalized
    return "error"


def _openai_usage(value: Mapping[str, Any]) -> Any:
    from core.agent_core.messages import Usage

    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    return Usage(
        input_tokens=_nonnegative(value.get("prompt_tokens")),
        output_tokens=_nonnegative(value.get("completion_tokens")),
        cache_read_tokens=_nonnegative(prompt_details.get("cached_tokens")) if isinstance(prompt_details, Mapping) else 0,
        cache_write_tokens=0,
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


def _apply_reasoning_details(content: list[Any], details: list[Any]) -> list[Any]:
    valid = [detail for detail in details if isinstance(detail, Mapping)]
    if not valid:
        return content
    signature = json.dumps(valid, ensure_ascii=False, separators=(",", ":"))
    for index, block in enumerate(content):
        if isinstance(block, ThinkingBlock):
            content[index] = ThinkingBlock(text=block.text, signature=signature, redacted=block.redacted)
            return content
    content.append(ThinkingBlock(text="", signature=signature))
    return content


def _signature_details(signature: str) -> Any:
    try:
        value = json.loads(signature)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, (list, dict)) else None


def _append_text(content: list[Any], value: str) -> list[Any]:
    for index, block in enumerate(content):
        if isinstance(block, TextBlock):
            content[index] = TextBlock(text=(block.text or "") + value)
            return content
    content.append(TextBlock(text=value))
    return content


def _append_thinking(content: list[Any], value: str, marker: str | None = None) -> list[Any]:
    for index, block in enumerate(content):
        if isinstance(block, ThinkingBlock):
            content[index] = ThinkingBlock(
                text=block.text + value,
                signature=block.signature or marker,
                redacted=block.redacted,
            )
            return content
    content.append(ThinkingBlock(text=value, signature=marker))
    return content


def _find_block_index(content: list[Any], kind: type[Any]) -> int:
    for index, block in enumerate(content):
        if isinstance(block, kind):
            return index
    return 0


def _error(
    message: str,
    streamed: bool,
    content: list[Any],
    origin: Any,
    usage: Any,
    verified: bool,
    *,
    code: str | None = None,
) -> ProviderError:
    return classify_error(
        body=message,
        code=code,
        streamed=streamed,
        partial=(
            assistant_message(content, origin=origin, stop_reason="error", usage=usage, verified_origin=verified)
            if streamed
            else None
        ),
    )


def _error_text(error: Mapping[str, Any], chunk: Mapping[str, Any]) -> str:
    message = _string(error.get("message"))
    return message or json.dumps(dict(chunk), ensure_ascii=False)


def _invalid_tool_metadata(message: str) -> ProviderError:
    return ProviderError(kind="invalid_request", message=message, retryable=False)


def _aborted(reason: str | None, origin: Any, content: list[Any] | None = None, usage: Any = None, verified: bool = True) -> ProviderError:
    return ProviderError(
        kind="aborted",
        message=reason or "provider request aborted",
        retryable=False,
        partial=(
            assistant_message(content, origin=origin, stop_reason="aborted", usage=usage, verified_origin=verified)
            if content
            else None
        ),
    )


def _endpoint_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _openai_effort(value: str) -> str:
    return {"xhigh": "high", "max": "high"}.get(value.lower(), value.lower())


def _uses_completion_tokens(model: str, effort: str | None) -> bool:
    normalized = model.lower()
    if any(normalized.startswith(prefix) for prefix in ("o1", "o3", "o4", "gpt-5")):
        return True
    return effort is not None and effort.lower() not in {"none", "off", "disabled"} and "reasoning" in normalized


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


OpenAIChatProvider = OpenAIChatAdapter
