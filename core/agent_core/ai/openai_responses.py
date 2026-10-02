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
    auth_headers,
    content_parts,
    drive_sse_stream,
    dispatch_wire_event,
    endpoint_origin,
    json_object,
    prepare_messages,
    StreamAssembler,
)
from core.agent_core.ai.provider import (
    Done,
    MediaLoader,
    ModelRequest,
    ProviderAdapter,
    ProviderError,
    TextDelta,
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

_KNOWN_RESPONSE_EVENTS = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.queued",
        "response.output_text.delta",
        "response.output_text.done",
        "response.refusal.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.output_item.added",
        "response.output_item.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.completed",
        "response.incomplete",
        "response.failed",
        "error",
    }
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
        reasoning: dict[int, dict[str, Any]] = {}
        status: str | None = None
        incomplete_reason: str | None = None
        refusal = False

        try:
            payload = build_responses_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="openai", gateway=self._gateway)
            headers.setdefault("content-type", "application/json")
            url = _endpoint_url(request.endpoint.base_url, "/responses")
            async def translate(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
                nonlocal status, incomplete_reason, refusal
                async for event in events:
                    if cancel.cancelled:
                        terminal = assembler.terminal(assembler.aborted(cancel.reason))
                        if terminal is not None:
                            yield terminal
                        return
                    if not event.data or event.data == "[DONE]":
                        continue
                    chunk = json_object(event.data)
                    if chunk is None:
                        terminal = assembler.terminal(
                            assembler.error(
                                "Provider returned invalid OpenAI Responses JSON",
                                kind="invalid_request",
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    event_type = dispatch_wire_event(
                        chunk,
                        known=_KNOWN_RESPONSE_EVENTS,
                        aliases={"response.done": "response.completed"},
                    )
                    if event_type is None:
                        continue
                    if event_type in {"response.created", "response.in_progress"}:
                        response_body = chunk.get("response")
                        if "response" in chunk and not isinstance(response_body, Mapping):
                            terminal = assembler.terminal(
                                assembler.error(
                                    f"{event_type} response must be an object",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if isinstance(response_body, Mapping):
                            status = _string(response_body.get("status")) or status
                    elif event_type == "response.output_text.delta":
                        if "delta" in chunk and not isinstance(chunk.get("delta"), str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "response.output_text.delta delta must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        value = _string(chunk.get("delta"))
                        if value:
                            output_index = _output_index(chunk)
                            if output_index is None:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "response.output_text.delta output_index must be an integer",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if not assembler.has_text_slot(output_index):
                                # Pi creates output slots only from
                                # response.output_item.added. Deltas for an
                                # unknown slot are ignored, preserving
                                # arrival order and avoiding invented blocks.
                                continue
                            emitted = assembler.text_delta(output_index, value)
                            if emitted is not None:
                                yield emitted
                    elif event_type == "response.refusal.delta":
                        if "delta" in chunk and not isinstance(chunk.get("delta"), str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "response.refusal.delta delta must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        value = _string(chunk.get("delta"))
                        if value:
                            output_index = _output_index(chunk)
                            if output_index is None:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "response.refusal.delta output_index must be an integer",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if not assembler.has_text_slot(output_index):
                                continue
                            refusal = True
                            emitted = assembler.text_delta(output_index, value)
                            if emitted is not None:
                                yield emitted
                    elif event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                        if "delta" in chunk and not isinstance(chunk.get("delta"), str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    f"{event_type} delta must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        value = _string(chunk.get("delta"))
                        if value:
                            output_index = _output_index(chunk)
                            if output_index is None:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        f"{event_type} output_index must be an integer",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if output_index not in reasoning:
                                continue
                            emitted = assembler.thinking_delta(output_index, value)
                            if emitted is not None:
                                yield emitted
                    elif event_type == "response.output_item.added":
                        item = chunk.get("item")
                        output_index = _output_index(chunk)
                        if output_index is None:
                            terminal = assembler.terminal(
                                assembler.error(
                                    "response.output_item.added output_index must be an integer",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if not isinstance(item, Mapping):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "response.output_item.added item must be an object",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        item_type = _string(item.get("type"))
                        if item_type == "function_call":
                            raw_arguments = item.get("arguments")
                            if raw_arguments is not None and not isinstance(raw_arguments, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "function_call arguments must be a JSON string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            call_id = _string(item.get("call_id")) or None
                            name = _string(item.get("name"))
                            if not name:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "function_call name must not be empty",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            item_id = _string(item.get("id"))
                            if "output_index" in chunk:
                                key_index = _output_index(chunk)
                                if key_index is None:
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "response.output_item.added output_index must be an integer",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                key = ("responses-tool", key_index)
                            else:
                                key = assembler.fallback_tool_key(
                                    native_id=item_id or call_id,
                                    allocate=True,
                                )
                                if key is None:
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "response.output_item.added could not allocate a tool slot",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                            assembler.bind_tool_alias(key, item_id)
                            assembler.bind_tool_alias(key, call_id)
                            assembler.set_tool_item_id(key, item_id)
                            start = assembler.tool_start(
                                key,
                                call_id=call_id,
                                native_id=item_id or None,
                                name=name,
                            )
                            if start is not None:
                                yield start
                            emitted = assembler.tool_arguments_snapshot(key, _string(item.get("arguments")))
                            if emitted is not None:
                                yield emitted
                        elif item_type == "reasoning":
                            state = reasoning.setdefault(output_index, {"id": "", "encrypted_content": ""})
                            _merge_reasoning_item(state, item)
                            assembler.ensure_thinking_slot(output_index)
                            if state["encrypted_content"]:
                                assembler.set_thinking_signature(
                                    output_index,
                                    _reasoning_signature_json(state),
                                )
                        elif item_type == "message":
                            assembler.ensure_text_slot(output_index)
                    elif event_type == "response.reasoning_summary_text.done":
                        continue
                    elif event_type == "response.reasoning_summary_part.done":
                        if "output_index" in chunk:
                            output_index = _output_index(chunk)
                            if output_index is None:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "response.reasoning_summary_part.done output_index must be an integer",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                        else:
                            output_index = 0
                        if output_index in reasoning:
                            emitted = assembler.thinking_delta(output_index, "\n\n")
                            if emitted is not None:
                                yield emitted
                    elif event_type == "response.function_call_arguments.delta":
                        if "delta" in chunk and not isinstance(chunk.get("delta"), str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "response.function_call_arguments.delta delta must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        item_id = _string(chunk.get("item_id")) or None
                        if "output_index" in chunk:
                            output_index = _output_index(chunk)
                            if output_index is None:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "response.function_call_arguments.delta output_index must be an integer",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            key = assembler.tool_key(
                                ("responses-tool", output_index),
                                native_id=item_id,
                            )
                        else:
                            key = assembler.fallback_tool_key(native_id=item_id, allocate=False)
                            if key is None:
                                continue
                        if not assembler.has_tool(key):
                            continue
                        value = _string(chunk.get("delta"))
                        emitted = assembler.tool_arguments(key, value)
                        if emitted is not None:
                            yield emitted
                    elif event_type == "response.function_call_arguments.done":
                        item_id = _string(chunk.get("item_id")) or None
                        if "output_index" in chunk:
                            output_index = _output_index(chunk)
                            if output_index is None:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "response.function_call_arguments.done output_index must be an integer",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            key = assembler.tool_key(
                                ("responses-tool", output_index),
                                native_id=item_id,
                            )
                        else:
                            key = assembler.fallback_tool_key(native_id=item_id, allocate=False)
                            if key is None:
                                continue
                        if not assembler.has_tool(key):
                            continue
                        if "arguments" in chunk:
                            complete_arguments = chunk.get("arguments")
                            if not isinstance(complete_arguments, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "function-call arguments must be a JSON string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            emitted = assembler.tool_arguments_snapshot(key, complete_arguments)
                            if emitted is not None:
                                yield emitted
                    elif event_type in {
                        "response.custom_tool_call_input.delta",
                        "response.custom_tool_call_input.done",
                    }:
                        # Avibe's canonical tool model has no custom-tool
                        # input slot. Pi supports these events conditionally;
                        # ignoring them preserves the canonical history shape.
                        continue
                    elif event_type == "response.output_item.done":
                        item = chunk.get("item")
                        if "item" in chunk and not isinstance(item, Mapping):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "response.output_item.done item must be an object",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if isinstance(item, Mapping):
                            item_id = _string(item.get("id"))
                            if item.get("type") == "function_call" and "output_index" not in chunk:
                                call_id = _string(item.get("call_id")) or None
                                key = assembler.fallback_tool_key(
                                    native_id=item_id or call_id,
                                    allocate=False,
                                )
                                if key is None:
                                    key = assembler.fallback_tool_key(
                                        native_id=item_id or call_id,
                                        allocate=True,
                                    )
                                if key is None:
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "response.output_item.done could not allocate a tool slot",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                            else:
                                output_index = _output_index(chunk)
                                if output_index is None:
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "response.output_item.done output_index must be an integer",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                key = (
                                    ("responses-tool", output_index)
                                    if item.get("type") == "function_call"
                                    else output_index
                                )
                            assembler.bind_tool_alias(key, item_id)
                            assembler.bind_tool_alias(key, _string(item.get("call_id")) or None)
                            if item.get("type") == "reasoning":
                                state = reasoning.setdefault(output_index, {"id": "", "encrypted_content": ""})
                                _merge_reasoning_item(state, item)
                                assembler.ensure_thinking_slot(output_index)
                                if state["encrypted_content"]:
                                    assembler.set_thinking_signature(
                                        output_index,
                                        _reasoning_signature_json(state),
                                    )
                                summary_text = _reasoning_summary_text(item)
                                if summary_text is not None:
                                    emitted = assembler.replace_thinking(output_index, summary_text)
                                    if emitted is not None:
                                        yield emitted
                                end = assembler.block_end(output_index)
                                if end is not None:
                                    yield end
                            elif item.get("type") == "function_call":
                                key = assembler.tool_key(
                                    key,
                                    native_id=item_id or _string(item.get("call_id")) or None,
                                )
                                name = _string(item.get("name")) or assembler.tool_name(key)
                                if not name:
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "function_call name must not be empty",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                item_id = _string(item.get("id")) or assembler.tool_item_id(key)
                                call_id = _string(item.get("call_id")) or None
                                raw_arguments = item.get("arguments")
                                if raw_arguments is not None and not isinstance(raw_arguments, str):
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "function_call arguments must be a JSON string",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                start = assembler.tool_start(
                                    key,
                                    call_id=call_id or None,
                                    native_id=item_id or None,
                                    name=name,
                                )
                                if start is not None:
                                    yield start
                                if raw_arguments is not None:
                                    emitted = assembler.tool_arguments_snapshot(key, raw_arguments)
                                    if emitted is not None:
                                        yield emitted
                                assembler.finish_tool(key)
                                end = assembler.block_end(key)
                                if end is not None:
                                    yield end
                            elif item.get("type") == "message":
                                assembler.ensure_text_slot(output_index)
                                message_refusal, emitted = _append_message_text(
                                    assembler,
                                    output_index,
                                    item,
                                )
                                if emitted is not None:
                                    yield emitted
                                refusal = refusal or message_refusal
                                end = assembler.block_end(output_index)
                                if end is not None:
                                    yield end
                    elif event_type in {"response.completed", "response.incomplete"}:
                        response_body = chunk.get("response")
                        if not isinstance(response_body, Mapping):
                            terminal = assembler.terminal(
                                assembler.error(f"{event_type} response must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        response_error: Mapping[str, Any] | None = None
                        status = _string(response_body.get("status")) or status
                        incomplete = response_body.get("incomplete_details")
                        if isinstance(incomplete, Mapping):
                            incomplete_reason = _string(incomplete.get("reason")) or incomplete_reason
                        usage = _responses_usage(response_body.get("usage"))
                        assembler.set_usage(usage)
                        if isinstance(response_body.get("error"), Mapping):
                            response_error = response_body["error"]
                        terminal_output_error, terminal_refusal = _apply_terminal_output_items(
                            response_body.get("output"),
                            assembler,
                            reasoning,
                        )
                        refusal = refusal or terminal_refusal
                        if terminal_output_error is not None:
                            terminal = assembler.terminal(terminal_output_error)
                            if terminal is not None:
                                yield terminal
                            return
                        if response_error is not None:
                            terminal = assembler.terminal(
                                assembler.error(
                                    _string(response_error.get("message"))
                                    or "OpenAI Responses response failed",
                                    code=_string(response_error.get("code")) or None,
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if status in {"failed", "cancelled"}:
                            terminal = assembler.terminal(
                                assembler.error(
                                    f"OpenAI Responses response {status}",
                                    kind="unknown",
                                )
                            )
                        elif status == "incomplete" and incomplete_reason not in {
                            "max_output_tokens",
                            "length",
                            "content_filter",
                            "safety",
                        }:
                            terminal = assembler.terminal(
                                assembler.error(
                                    f"OpenAI Responses response incomplete: {incomplete_reason or 'unknown'}",
                                )
                            )
                        else:
                            stop = (
                                "refusal"
                                if refusal
                                else "safety"
                                if incomplete_reason in {"content_filter", "safety"}
                                else "length"
                                if incomplete_reason in {"max_output_tokens", "length"}
                                else "tool_use"
                                if assembler.has_tools()
                                else "stop"
                            )
                            if stop == "tool_use":
                                unfinished = assembler.unfinished_tool_calls()
                                if unfinished:
                                    names = ", ".join(block.name for block in unfinished)
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "OpenAI Responses stream completed with unfinished tool call"
                                            + (f": {names}" if names else ""),
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                            final = assembler.finalize(stop)
                            terminal = assembler.terminal(
                                final if isinstance(final, ProviderError) else Done(final)
                            )
                        if terminal is not None:
                            yield terminal
                        return
                    elif event_type == "response.failed":
                        response_body = chunk.get("response")
                        if "response" in chunk and not isinstance(response_body, Mapping):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "response.failed response must be an object",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        error = response_body.get("error") if isinstance(response_body, Mapping) else chunk.get("error")
                        if isinstance(response_body, Mapping):
                            assembler.set_usage(_responses_usage(response_body.get("usage")))
                        message = _string(error.get("message")) if isinstance(error, Mapping) else json.dumps(dict(chunk), ensure_ascii=False)
                        code = _string(error.get("code")) if isinstance(error, Mapping) else ""
                        terminal = assembler.terminal(
                            assembler.error(
                                message,
                                code=code or None,
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    elif event_type == "error":
                        nested_error = chunk.get("error")
                        error_message = (
                            _string(nested_error.get("message"))
                            if isinstance(nested_error, Mapping)
                            else _string(chunk.get("message"))
                        )
                        error_code = (
                            _string(nested_error.get("code"))
                            if isinstance(nested_error, Mapping)
                            else _string(chunk.get("code"))
                        )
                        terminal = assembler.terminal(
                            assembler.error(
                                error_message or json.dumps(dict(chunk), ensure_ascii=False),
                                code=error_code or None,
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    elif event_type in {
                        "response.queued",
                        "response.content_part.added",
                        "response.content_part.done",
                        "response.output_text.done",
                        "response.reasoning_text.done",
                    }:
                        continue
                    else:
                        # Unknown frames are filtered by dispatch_wire_event
                        # above, but keep this fallthrough for future aliases.
                        continue
                if cancel.cancelled:
                    terminal = assembler.terminal(assembler.aborted(cancel.reason))
                    if terminal is not None:
                        yield terminal
                    return
                terminal = assembler.terminal(assembler.incomplete())
                if terminal is not None:
                    yield terminal
            async for event in drive_sse_stream(
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
            ):
                yield event
        except Exception as exc:
            terminal = assembler.terminal(assembler.exception(exc))
            if terminal is not None:
                yield terminal


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
        for block in message.content:
            if isinstance(block, TextBlock) and block.text:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": block.text}],
                    }
                )
            elif isinstance(block, ThinkingBlock):
                if block.signature is not None:
                    details = _reasoning_signature(block.signature)
                    if details is not None:
                        items.append({"type": "reasoning", **details})
                elif block.text:
                    items.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": block.text}],
                        }
                    )
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
    assembler: StreamAssembler,
    reasoning: dict[int, dict[str, Any]],
) -> tuple[ProviderError | None, bool]:
    if not isinstance(items, list):
        return None, False
    refusal = False
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            return assembler.error("response output item must be an object", kind="invalid_request"), refusal
        if item.get("type") == "reasoning":
            item_id = _string(item.get("id"))
            state_index, state = next(
                (
                    (candidate_index, candidate)
                    for candidate_index, candidate in reasoning.items()
                    if item_id and candidate.get("id") == item_id
                ),
                (index, reasoning.get(index)),
            )
            if state is None:
                continue
            _merge_reasoning_item(state, item)
            if state["encrypted_content"]:
                assembler.set_thinking_signature(state_index, _reasoning_signature_json(state))
        elif item.get("type") == "message":
            message_refusal = _message_has_refusal(item)
            refusal = refusal or message_refusal
            if message_refusal and assembler.has_text_slot(index):
                _append_message_refusal(assembler, index, item)
    return None, refusal


def _merge_reasoning_item(state: dict[str, Any], item: Mapping[str, Any]) -> None:
    previous = state.get("item")
    merged = dict(previous) if isinstance(previous, Mapping) else {}
    for key, value in item.items():
        if key == "encrypted_content" and not value:
            continue
        merged[key] = value
    merged["type"] = "reasoning"
    state["item"] = merged
    state["id"] = _string(item.get("id")) or state.get("id", "")
    encrypted_content = _string(item.get("encrypted_content"))
    state["encrypted_content"] = encrypted_content or state.get("encrypted_content", "")
    if state["id"]:
        merged["id"] = state["id"]
    if state["encrypted_content"]:
        merged["encrypted_content"] = state["encrypted_content"]


def _message_has_refusal(item: Mapping[str, Any]) -> bool:
    value = item.get("content")
    if not isinstance(value, list):
        return False
    return any(
        isinstance(part, Mapping) and bool(_string(part.get("refusal")))
        for part in value
    )


def _append_message_refusal(assembler: StreamAssembler, index: int, item: Mapping[str, Any]) -> None:
    value = item.get("content")
    if not isinstance(value, list):
        return
    refusal_parts = [
        _string(part.get("refusal"))
        for part in value
        if isinstance(part, Mapping) and _string(part.get("refusal"))
    ]
    if refusal_parts:
        assembler.merge_text_snapshot(index, "".join(refusal_parts))


def _append_message_text(
    assembler: StreamAssembler,
    index: int,
    item: Mapping[str, Any],
) -> tuple[bool, TextDelta | None]:
    value = item.get("content")
    if not isinstance(value, list):
        return False, None
    snapshot_parts: list[str] = []
    refusal = False
    for part in value:
        if not isinstance(part, Mapping):
            continue
        if _string(part.get("refusal")):
            refusal = True
        text = _string(part.get("text")) or _string(part.get("refusal"))
        if text:
            snapshot_parts.append(text)
    emitted = None
    if snapshot_parts:
        emitted = assembler.merge_text_snapshot(index, "".join(snapshot_parts))
    return refusal, emitted


def _reasoning_signature_json(state: Mapping[str, Any]) -> str:
    item = state.get("item")
    if isinstance(item, Mapping):
        return json.dumps(dict(item), ensure_ascii=False, separators=(",", ":"))
    return json.dumps(
        {
            "type": "reasoning",
            "id": state.get("id", ""),
            "encrypted_content": state.get("encrypted_content", ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _responses_usage(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    from core.agent_core.messages import Usage

    input_details = value.get("input_tokens_details")
    output_details = value.get("output_tokens_details")
    cache_read_tokens = (
        _nonnegative(input_details.get("cached_tokens"))
        if isinstance(input_details, Mapping)
        else 0
    )
    cache_write_tokens = (
        _nonnegative(input_details.get("cache_write_tokens"))
        if isinstance(input_details, Mapping)
        else 0
    )
    return Usage(
        input_tokens=max(0, _nonnegative(value.get("input_tokens")) - cache_read_tokens - cache_write_tokens),
        output_tokens=_nonnegative(value.get("output_tokens")),
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=_nonnegative(output_details.get("reasoning_tokens")) if isinstance(output_details, Mapping) and "reasoning_tokens" in output_details else None,
    )


def _reasoning_summary_text(item: Mapping[str, Any]) -> str | None:
    summary = item.get("summary")
    if summary is None:
        summary = item.get("content")
    if not isinstance(summary, list):
        return None
    parts: list[str] = []
    for part in summary:
        if isinstance(part, Mapping):
            text = _string(part.get("text"))
            if text:
                parts.append(text)
    return "\n\n".join(parts) if parts else None


def _endpoint_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _openai_effort(value: str) -> str:
    return {"xhigh": "high", "max": "high"}.get(value.lower(), value.lower())


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _output_index(chunk: Mapping[str, Any]) -> int | None:
    if "output_index" not in chunk:
        return 0
    value = chunk.get("output_index")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


OpenAIResponsesProvider = OpenAIResponsesAdapter
