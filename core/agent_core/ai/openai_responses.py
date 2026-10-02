"""OpenAI Responses API streaming adapter.

The client-side transcript is authoritative: every request sets ``store`` to
false and never sends ``previous_response_id``.

Payload and stream rules are ported from tau's ``tau_ai/openai_compatible.py``
(MIT) and Pi's ``packages/ai/src/api/openai-responses-shared.ts`` (MIT).
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
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


class OpenAIResponsesAdapter(ProviderAdapter):
    """Adapter for the native OpenAI Responses protocol."""

    protocol = "openai_responses"

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
        )
        if isinstance(prepared, ProviderError):
            yield prepared
            return
        transformed_messages, loaded_images = prepared
        if cancel.cancelled:
            yield _aborted(cancel.reason, target)
            return
        content: list[Any] = []
        calls: dict[int, dict[str, Any]] = {}
        call_indexes_by_item: dict[str, int] = {}
        reasoning: dict[int, dict[str, Any]] = {}
        usage = None
        status: str | None = None
        incomplete_reason: str | None = None
        refusal = False
        streamed = False
        origin = target
        verified = not self._gateway
        try:
            payload = build_responses_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="openai", gateway=self._gateway)
            headers.setdefault("content-type", "application/json")
            url = _endpoint_url(request.endpoint.base_url, "/responses")
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
                origin, verified = resolve_served_origin(
                    request.endpoint,
                    response.headers,
                    self._served_hop_resolver,
                    gateway=self._gateway,
                )
                if response.status_code >= 400:
                    body = await read_response_body(response, cancel)
                    if body is None:
                        yield _aborted(cancel.reason, origin, content, usage, verified)
                        return
                    yield classify_error(status=response.status_code, body=body, headers=response.headers)
                    return
                async for event in iter_sse_events(response, cancel):
                    if cancel.cancelled:
                        yield _aborted(cancel.reason, origin, content, usage, verified)
                        return
                    if not event.data or event.data == "[DONE]":
                        continue
                    chunk = json_object(event.data)
                    if chunk is None:
                        yield _error("Provider returned invalid OpenAI Responses JSON", streamed, content, origin, usage, verified)
                        return
                    event_type = _string(chunk.get("type"))
                    if event_type in {"response.created", "response.in_progress"}:
                        response_body = chunk.get("response")
                        if isinstance(response_body, Mapping):
                            status = _string(response_body.get("status")) or status
                    elif event_type == "response.output_text.delta":
                        value = _string(chunk.get("delta"))
                        if value:
                            content = _append_text(content, value)
                            streamed = True
                            yield TextDelta(index=_find(content, TextBlock), delta=value)
                    elif event_type == "response.refusal.delta":
                        value = _string(chunk.get("delta"))
                        if value:
                            content = _append_text(content, value)
                            refusal = True
                            streamed = True
                            yield TextDelta(index=_find(content, TextBlock), delta=value)
                    elif event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                        value = _string(chunk.get("delta"))
                        if value:
                            output_index = _int(chunk.get("output_index"), 0)
                            state = reasoning.setdefault(output_index, {"id": "", "encrypted_content": ""})
                            content = _append_thinking(content, value, output_index, reasoning)
                            state["text"] = f"{state.get('text', '')}{value}"
                            streamed = True
                            yield ThinkingDelta(index=_find(content, ThinkingBlock), delta=value)
                    elif event_type == "response.output_item.added":
                        item = chunk.get("item")
                        output_index = _int(chunk.get("output_index"), len(calls) + len(reasoning))
                        if isinstance(item, Mapping):
                            item_type = _string(item.get("type"))
                            if item_type == "function_call":
                                call_id = _string(item.get("call_id")) or _string(item.get("id")) or f"call_{output_index}"
                                name = _string(item.get("name"))
                                item_id = _string(item.get("id"))
                                state = calls.setdefault(
                                    output_index,
                                    {
                                        "id": call_id,
                                        "name": name,
                                        "arguments": _string(item.get("arguments")),
                                        "item_id": item_id,
                                    },
                                )
                                state["id"] = call_id
                                state["name"] = name
                                state["item_id"] = item_id
                                if "content_index" not in state:
                                    state["content_index"] = len(content)
                                    content.append(
                                        ToolCallBlock(
                                            id=call_id,
                                            native_id=item_id or None,
                                            name=name,
                                            arguments={},
                                        )
                                    )
                                if item_id:
                                    call_indexes_by_item[item_id] = output_index
                                streamed = True
                                yield ToolCallStart(index=state["content_index"], id=call_id, name=name)
                            elif item_type == "reasoning":
                                state = reasoning.setdefault(output_index, {"id": "", "encrypted_content": "", "text": ""})
                                state["id"] = _string(item.get("id"))
                                state["encrypted_content"] = _string(item.get("encrypted_content"))
                                streamed = True
                                if state["encrypted_content"]:
                                    _set_thinking_signature(content, output_index, state)
                    elif event_type == "response.reasoning_summary_text.done":
                        continue
                    elif event_type == "response.function_call_arguments.delta":
                        output_index = _resolve_call_index(chunk, calls, call_indexes_by_item)
                        state = calls.setdefault(output_index, {"id": "", "name": "", "arguments": ""})
                        value = _string(chunk.get("delta"))
                        state["arguments"] += value
                        streamed = streamed or bool(value)
                        yield ToolCallDelta(index=_int(state.get("content_index"), 0), arguments_delta=value)
                    elif event_type == "response.function_call_arguments.done":
                        output_index = _resolve_call_index(chunk, calls, call_indexes_by_item)
                        state = calls.setdefault(output_index, {"id": "", "name": "", "arguments": ""})
                        if "arguments" in chunk:
                            state["arguments"] = _string(chunk.get("arguments"))
                    elif event_type == "response.output_item.done":
                        item = chunk.get("item")
                        if isinstance(item, Mapping):
                            output_index = _resolve_call_index(
                                {**chunk, "item_id": item.get("id")},
                                calls,
                                call_indexes_by_item,
                            )
                            if item.get("type") == "reasoning":
                                state = reasoning.setdefault(output_index, {"id": "", "encrypted_content": "", "text": ""})
                                state["id"] = _string(item.get("id")) or state.get("id", "")
                                state["encrypted_content"] = _string(item.get("encrypted_content")) or state.get("encrypted_content", "")
                                if state["encrypted_content"]:
                                    _set_thinking_signature(content, output_index, state)
                            elif item.get("type") == "function_call":
                                state = calls.setdefault(output_index, {"id": "", "name": "", "arguments": ""})
                                state["id"] = _string(item.get("call_id")) or _string(item.get("id")) or state.get("id", "")
                                state["name"] = _string(item.get("name")) or state.get("name", "")
                                state["item_id"] = _string(item.get("id")) or state.get("item_id", "")
                                if state.get("item_id"):
                                    call_indexes_by_item[state["item_id"]] = output_index
                                state["arguments"] = _string(item.get("arguments")) or state.get("arguments", "")
                    elif event_type in {"response.completed", "response.incomplete"}:
                        response_body = chunk.get("response")
                        if isinstance(response_body, Mapping):
                            status = _string(response_body.get("status")) or status
                            incomplete = response_body.get("incomplete_details")
                            if isinstance(incomplete, Mapping):
                                incomplete_reason = _string(incomplete.get("reason")) or incomplete_reason
                            usage = _responses_usage(response_body.get("usage")) or usage
                            _apply_terminal_output_items(
                                response_body.get("output"),
                                content,
                                calls,
                                reasoning,
                                call_indexes_by_item,
                            )
                        final = _final(content, calls, origin, status, incomplete_reason, usage, verified, refusal=refusal)
                        for index in range(len(content)):
                            yield BlockEnd(index=index)
                        if isinstance(final, ProviderError):
                            yield final
                        else:
                            yield Done(final)
                        return
                    elif event_type == "response.failed":
                        response_body = chunk.get("response")
                        error = response_body.get("error") if isinstance(response_body, Mapping) else chunk.get("error")
                        message = _string(error.get("message")) if isinstance(error, Mapping) else json.dumps(dict(chunk), ensure_ascii=False)
                        code = _string(error.get("code")) if isinstance(error, Mapping) else ""
                        yield _error(message, streamed, content, origin, usage, verified, code=code or None)
                        return
                    elif event_type == "error":
                        yield _error(_string(chunk.get("message")) or json.dumps(dict(chunk), ensure_ascii=False), streamed, content, origin, usage, verified)
                        return
                if cancel.cancelled:
                    yield _aborted(cancel.reason, origin, content, usage, verified)
                    return
                yield incomplete_stream_error(
                    content,
                    origin=origin,
                    usage=usage,
                    streamed=streamed,
                    verified_origin=verified,
                    protocol=self.protocol,
                )
        except httpx.HTTPError as exc:
            yield classify_error(
                exc=exc,
                streamed=streamed,
                partial=(
                    assistant_message(content, origin=origin, stop_reason="error", usage=usage, verified_origin=verified)
                    if streamed
                    else None
                ),
            )


def build_responses_payload(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.endpoint.model_id,
        "instructions": request.system,
        "input": [
            item
            for message in messages
            for item in _message_to_response(message, request.supports_images, loaded_images)
        ],
        "max_output_tokens": request.max_tokens,
        "stream": True,
        "store": False,
    }
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.input_schema),
            }
            for tool in request.tools
        ]
    if request.reasoning_effort and request.reasoning_effort.lower() not in {"none", "off", "disabled"}:
        payload["reasoning"] = {"effort": _openai_effort(request.reasoning_effort), "summary": "auto"}
        payload["include"] = ["reasoning.encrypted_content"]
    return payload


def _message_to_response(
    message: Any,
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if isinstance(message, UserMessage):
        return [{"role": "user", "content": _response_content(message.content, supports_images, input_text=True, loaded_images=loaded_images)}]
    if isinstance(message, AssistantMessage):
        items: list[dict[str, Any]] = []
        text_parts: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text:
                text_parts.append({"type": "output_text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                if block.signature is not None:
                    details = _reasoning_signature(block.signature)
                    if details is not None:
                        items.append({"type": "reasoning", **details})
                elif block.text:
                    text_parts.append({"type": "output_text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                items.append(
                    {
                        "type": "function_call",
                        "id": block.native_id or block.id,
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(dict(block.arguments), ensure_ascii=False),
                    }
                )
        if text_parts:
            items.insert(0, {"role": "assistant", "content": text_parts})
        return items or [{"role": "assistant", "content": ""}]
    if isinstance(message, ToolResultMessage):
        values = content_parts(message.content, include_images=supports_images, loaded_images=loaded_images)
        text = "\n".join(part["text"] for part in values if part["type"] == "text")
        if any(part["type"] == "image" for part in values):
            output: Any = [
                (
                    {"type": "input_text", "text": part["text"]}
                    if part["type"] == "text"
                    else {
                        "type": "input_image",
                        "image_url": f"data:{part['mime_type']};base64,{part['data']}",
                    }
                )
                for part in values
            ]
        else:
            output = text or "(no tool output)"
        return [{"type": "function_call_output", "call_id": message.tool_call_id, "output": output}]
    raise TypeError(f"unsupported message {type(message).__name__}")


def _response_content(
    content: tuple[Any, ...],
    supports_images: bool,
    *,
    input_text: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in content_parts(content, include_images=supports_images, loaded_images=loaded_images):
        if part["type"] == "text":
            result.append({"type": "input_text" if input_text else "output_text", "text": part["text"]})
        else:
            result.append({"type": "input_image", "image_url": f"data:{part['mime_type']};base64,{part['data']}"})
    return result or [{"type": "input_text", "text": "(no content)"}]


def _reasoning_signature(signature: str) -> dict[str, Any] | None:
    try:
        value = json.loads(signature)
    except (TypeError, ValueError):
        return None
    if isinstance(value, Mapping):
        output = dict(value)
        output.pop("type", None)
        if not isinstance(output.get("id"), str) or not isinstance(output.get("encrypted_content"), str):
            return None
        return output if output["encrypted_content"] else None
    return None


def _apply_terminal_output_items(
    items: Any,
    content: list[Any],
    calls: dict[int, dict[str, Any]],
    reasoning: dict[int, dict[str, Any]],
    call_indexes_by_item: dict[str, int],
) -> None:
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "reasoning":
            state = reasoning.setdefault(index, {"id": "", "encrypted_content": "", "text": ""})
            state["id"] = _string(item.get("id")) or state.get("id", "")
            state["encrypted_content"] = _string(item.get("encrypted_content")) or state.get("encrypted_content", "")
            if state["encrypted_content"]:
                _set_thinking_signature(content, index, state)
        elif item.get("type") == "function_call":
            item_id = _string(item.get("id"))
            output_index = call_indexes_by_item.get(item_id, index) if item_id else index
            state = calls.setdefault(output_index, {"id": "", "name": "", "arguments": ""})
            state["id"] = _string(item.get("call_id")) or _string(item.get("id")) or state.get("id", "")
            state["name"] = _string(item.get("name")) or state.get("name", "")
            state["item_id"] = item_id or state.get("item_id", "")
            if item_id:
                call_indexes_by_item[item_id] = output_index
            if "content_index" not in state:
                state["content_index"] = len(content)
                content.append(
                    ToolCallBlock(
                        id=state["id"] or f"call_{output_index}",
                        native_id=item_id or None,
                        name=state["name"],
                        arguments={},
                    )
                )
            state["arguments"] = _string(item.get("arguments")) or state.get("arguments", "")


def _final(
    content: list[Any],
    calls: Mapping[int, Mapping[str, Any]],
    origin: Any,
    status: str | None,
    incomplete: str | None,
    usage: Any,
    verified: bool,
    *,
    refusal: bool = False,
) -> AssistantMessage | ProviderError:
    final: list[Any] = []
    for block in content:
        if isinstance(block, ToolCallBlock):
            state = next((candidate for candidate in calls.values() if candidate.get("id") == block.id), {})
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
    if status in {"failed", "cancelled"}:
        stop = "error"
    elif refusal:
        stop = "refusal"
    elif incomplete in {"content_filter", "safety"}:
        stop = "safety"
    elif incomplete in {"max_output_tokens", "length"}:
        stop = "length"
    else:
        stop = "tool_use" if any(isinstance(block, ToolCallBlock) for block in final) else "stop"
    return assistant_message(final, origin=origin, stop_reason=stop, usage=usage, verified_origin=verified)


def _responses_usage(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    from core.agent_core.messages import Usage

    input_details = value.get("input_tokens_details")
    output_details = value.get("output_tokens_details")
    return Usage(
        input_tokens=_nonnegative(value.get("input_tokens")),
        output_tokens=_nonnegative(value.get("output_tokens")),
        cache_read_tokens=_nonnegative(input_details.get("cached_tokens")) if isinstance(input_details, Mapping) else 0,
        cache_write_tokens=0,
        reasoning_tokens=_nonnegative(output_details.get("reasoning_tokens")) if isinstance(output_details, Mapping) and "reasoning_tokens" in output_details else None,
    )


def _set_thinking_signature(content: list[Any], output_index: int, state: Mapping[str, Any]) -> None:
    signature = json.dumps(
        {"id": state.get("id", ""), "encrypted_content": state.get("encrypted_content", "")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for index, block in enumerate(content):
        if isinstance(block, ThinkingBlock):
            content[index] = ThinkingBlock(text=block.text, signature=signature, redacted=False)
            return
    content.append(ThinkingBlock(text=str(state.get("text", "")), signature=signature))


def _append_text(content: list[Any], value: str) -> list[Any]:
    for index, block in enumerate(content):
        if isinstance(block, TextBlock):
            content[index] = TextBlock(text=(block.text or "") + value)
            return content
    content.append(TextBlock(text=value))
    return content


def _append_thinking(content: list[Any], value: str, output_index: int, reasoning: Mapping[int, Mapping[str, Any]]) -> list[Any]:
    for index, block in enumerate(content):
        if isinstance(block, ThinkingBlock):
            content[index] = ThinkingBlock(text=block.text + value, signature=block.signature)
            return content
    state = reasoning.get(output_index, {})
    signature = None
    if state.get("id") and state.get("encrypted_content"):
        signature = json.dumps(
            {"id": state["id"], "encrypted_content": state["encrypted_content"]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    content.append(ThinkingBlock(text=value, signature=signature))
    return content


def _find(content: list[Any], kind: type[Any]) -> int:
    for index, block in enumerate(content):
        if isinstance(block, kind):
            return index
    return 0


def _resolve_call_index(
    chunk: Mapping[str, Any],
    calls: Mapping[int, Mapping[str, Any]],
    call_indexes_by_item: Mapping[str, int],
) -> int:
    output_index = chunk.get("output_index")
    if isinstance(output_index, int) and not isinstance(output_index, bool):
        return output_index
    item_id = _string(chunk.get("item_id"))
    if item_id and item_id in call_indexes_by_item:
        return call_indexes_by_item[item_id]
    if len(calls) == 1:
        return next(iter(calls))
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


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


OpenAIResponsesProvider = OpenAIResponsesAdapter
