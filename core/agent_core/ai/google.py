"""Google Gemini ``streamGenerateContent`` adapter.

Payload and stream rules are ported from tau's ``tau_ai/google.py`` (MIT) and
Pi's provider conversion rules (MIT, revision 7fbbd5f).
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
    partial_message,
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
    MediaLoader,
    ModelRequest,
    ProviderAdapter,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
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


class GoogleAdapter(ProviderAdapter):
    """Adapter for Gemini's native streaming generate-content protocol."""

    protocol = "google"

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
        calls: list[dict[str, Any]] = []
        usage = None
        finish_reason: str | None = None
        streamed = False
        terminal_seen = False
        origin = target
        verified = not self._gateway
        try:
            payload = build_google_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="google", gateway=self._gateway)
            headers.setdefault("content-type", "application/json")
            url = _google_url(request.endpoint.base_url, request.endpoint.model_id)
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
                    chunk = json_object(event.data)
                    if chunk is None:
                        yield terminal_event(
                            response,
                            _error(
                                "Provider returned invalid Gemini JSON",
                                streamed,
                                content,
                                origin,
                                usage,
                                verified,
                                include_partial=usage is not None,
                            ),
                        )
                        return
                    top_level_error = chunk.get("error")
                    if isinstance(top_level_error, Mapping):
                        yield terminal_event(
                            response,
                            _error(
                                _string(top_level_error.get("message"))
                                or json.dumps(dict(chunk), ensure_ascii=False),
                                streamed,
                                content,
                                origin,
                                usage,
                                verified,
                                code=_string(top_level_error.get("status"))
                                or _string(top_level_error.get("code"))
                                or None,
                                include_partial=usage is not None,
                            ),
                        )
                        return
                    usage = _gemini_usage(chunk.get("usageMetadata")) or usage
                    candidates = chunk.get("candidates")
                    if not isinstance(candidates, list) or not candidates:
                        feedback = chunk.get("promptFeedback")
                        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
                            finish_reason = "SAFETY"
                            terminal_seen = True
                            break
                        continue
                    candidate = candidates[0]
                    if not isinstance(candidate, Mapping):
                        continue
                    if isinstance(candidate.get("finishReason"), str):
                        finish_reason = candidate["finishReason"]
                    candidate_content = candidate.get("content")
                    if isinstance(candidate_content, Mapping):
                        parts = candidate_content.get("parts")
                        if isinstance(parts, list):
                            for part in parts:
                                if not isinstance(part, Mapping):
                                    continue
                                signature = _string(part.get("thoughtSignature"))
                                value = _string(part.get("text"))
                                if value:
                                    if part.get("thought") is True:
                                        content = _append_thinking(content, value, signature or None)
                                        streamed = True
                                        yield ThinkingDelta(index=_find(content, ThinkingBlock), delta=value)
                                    else:
                                        content = _append_text(content, value)
                                        streamed = True
                                        yield TextDelta(index=_find(content, TextBlock), delta=value)
                                    if signature:
                                        _set_thinking_signature(content, signature)
                                function_call = part.get("functionCall")
                                if isinstance(function_call, Mapping):
                                    explicit_id = _string(function_call.get("id"))
                                    name = _string(function_call.get("name"))
                                    if not name:
                                        yield terminal_event(
                                            response,
                                            _invalid_tool_metadata("functionCall name must not be empty"),
                                        )
                                        return
                                    arguments = function_call.get("args")
                                    if arguments is not None and not isinstance(arguments, (Mapping, str)):
                                        yield terminal_event(
                                            response,
                                            _invalid_tool_metadata("functionCall args must be an object or JSON string"),
                                        )
                                        return
                                    raw_args = (
                                        json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                                        if isinstance(arguments, Mapping)
                                        else _string(arguments)
                                    )
                                    state = (
                                        next((item for item in calls if item["id"] == explicit_id), None)
                                        if explicit_id
                                        else None
                                    )
                                    if state is None:
                                        call_id = explicit_id or f"call_{len(calls)}"
                                        state = {
                                            "id": call_id,
                                            "name": name,
                                            "arguments": raw_args,
                                            "arguments_obj": dict(arguments) if isinstance(arguments, Mapping) else None,
                                            "signature": signature or None,
                                        }
                                        calls.append(state)
                                        content.append(
                                            ToolCallBlock(
                                                id=call_id,
                                                native_id=call_id,
                                                name=name,
                                                arguments={},
                                                signature=state["signature"],
                                            )
                                        )
                                        state["content_index"] = len(content) - 1
                                        streamed = True
                                        yield ToolCallStart(index=state["content_index"], id=call_id, name=name)
                                    else:
                                        if isinstance(arguments, Mapping):
                                            existing = state.get("arguments_obj")
                                            if isinstance(existing, dict):
                                                existing.update(arguments)
                                                state["arguments"] = json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
                                            else:
                                                state["arguments"] += raw_args
                                        else:
                                            state["arguments"] += raw_args
                                        if signature:
                                            state["signature"] = signature
                                    if raw_args:
                                        streamed = True
                                        yield ToolCallDelta(
                                            index=_nonnegative(state.get("content_index")),
                                            arguments_delta=raw_args,
                                        )
                    if finish_reason:
                        terminal_seen = True
                        break
                if cancel.cancelled:
                    yield terminal_event(response, _aborted(cancel.reason, origin, content, usage, verified))
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
                final = _final(content, calls, origin, finish_reason or ("tool_use" if calls else "stop"), usage, verified)
                if isinstance(final, ProviderError):
                    yield terminal_event(response, final)
                else:
                    yield terminal_event(response, Done(final))
        except Exception as exc:
            yield classify_error(
                exc=exc,
                streamed=streamed,
                partial=(
                    partial_message(
                        content,
                        origin=origin,
                        stop_reason="error",
                        usage=usage,
                        verified_origin=verified,
                    )
                    if streamed or usage is not None
                    else None
                ),
            )


def build_google_payload(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
    thinking = _thinking_config(request.endpoint.model_id, request.reasoning_effort)
    if thinking is not None:
        config["thinkingConfig"] = thinking
    payload: dict[str, Any] = {
        "contents": _google_contents(
            messages,
            model=request.endpoint.model_id,
            supports_images=request.supports_images,
            loaded_images=loaded_images,
        ),
    }
    if request.system:
        payload["systemInstruction"] = {"parts": [{"text": request.system}]}
    if config:
        payload["generationConfig"] = config
    if request.tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": _sanitize_schema(dict(tool.input_schema)),
                    }
                    for tool in request.tools
                ]
            }
        ]
    return payload


def _message_to_google(
    message: Any,
    *,
    model: str,
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if isinstance(message, UserMessage):
        return [{"role": "user", "parts": _google_parts(message.content, supports_images, loaded_images)}]
    if isinstance(message, AssistantMessage):
        parts: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text:
                parts.append({"text": block.text})
            elif isinstance(block, ThinkingBlock):
                if block.redacted:
                    continue
                if block.signature is not None:
                    parts.append({"text": block.text, "thought": True, "thoughtSignature": block.signature})
                elif block.text:
                    parts.append({"text": block.text})
            elif isinstance(block, ToolCallBlock):
                function_call: dict[str, Any] = {
                    "id": block.id,
                    "name": block.name,
                    "args": dict(block.arguments),
                }
                part: dict[str, Any] = {"functionCall": function_call}
                if block.signature is not None:
                    part["thoughtSignature"] = block.signature
                parts.append(part)
        return [{"role": "model", "parts": parts or [{"text": ""}]}]
    if isinstance(message, ToolResultMessage):
        values = content_parts(message.content, include_images=supports_images, loaded_images=loaded_images)
        text = "\n".join(part["text"] for part in values if part["type"] == "text")
        response: dict[str, Any] = {
            "name": message.tool_name,
            "response": {"error" if message.is_error else "output": text or "(no tool output)"},
            "id": message.tool_call_id,
        }
        image_parts = [
            {"inlineData": {"mimeType": part["mime_type"], "data": part["data"]}}
            for part in values
            if part["type"] == "image"
        ]
        if image_parts:
            response["parts"] = image_parts
        return [{"role": "user", "parts": [{"functionResponse": response}]}]
    raise TypeError(f"unsupported message {type(message).__name__}")


def _google_contents(
    messages: tuple[Any, ...],
    *,
    model: str,
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None,
) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    last_was_tool_result = False
    for message in messages:
        converted = _message_to_google(
            message,
            model=model,
            supports_images=supports_images,
            loaded_images=loaded_images,
        )
        if isinstance(message, ToolResultMessage) and last_was_tool_result and contents:
            contents[-1]["parts"].extend(converted[0]["parts"])
        else:
            contents.extend(converted)
        last_was_tool_result = isinstance(message, ToolResultMessage)
    return contents


def _google_parts(
    content: tuple[Any, ...],
    supports_images: bool,
    loaded_images: Mapping[str, tuple[str, str]] | None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in content_parts(content, include_images=supports_images, loaded_images=loaded_images):
        if part["type"] == "text":
            result.append({"text": part["text"]})
        else:
            result.append({"inlineData": {"mimeType": part["mime_type"], "data": part["data"]}})
    return result or [{"text": "(no content)"}]


def _final(
    content: list[Any],
    calls: list[Mapping[str, Any]],
    origin: Any,
    reason: str,
    usage: Any,
    verified: bool,
) -> AssistantMessage | ProviderError:
    final: list[Any] = []
    call_index = 0
    for block in content:
        if isinstance(block, ToolCallBlock):
            state = calls[call_index] if call_index < len(calls) else {}
            arguments = parsed_arguments(str(state.get("arguments", "")), tool_name=block.name)
            if isinstance(arguments, ProviderError):
                return arguments
            final.append(
                ToolCallBlock(
                    id=block.id,
                    native_id=block.native_id,
                    name=block.name,
                    arguments=arguments,
                    signature=state.get("signature") or block.signature,
                )
            )
            call_index += 1
        else:
            final.append(block)
    stop = _normalize_stop(reason, bool(calls))
    return assistant_message(final, origin=origin, stop_reason=stop, usage=usage, verified_origin=verified)


def _gemini_usage(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    from core.agent_core.messages import Usage

    return Usage(
        input_tokens=_nonnegative(value.get("promptTokenCount")),
        output_tokens=_nonnegative(value.get("candidatesTokenCount")),
        cache_read_tokens=_nonnegative(value.get("cachedContentTokenCount")),
        cache_write_tokens=0,
        reasoning_tokens=_nonnegative(value.get("thoughtsTokenCount")) if "thoughtsTokenCount" in value else None,
    )


def _thinking_config(model: str, effort: str | None) -> dict[str, Any] | None:
    if effort is None:
        return None
    normalized = effort.lower()
    normalized_model = model.lower()
    if "gemini-3" in normalized_model or "gemma-4" in normalized_model:
        if normalized in {"none", "off", "disabled"}:
            return {"includeThoughts": True, "thinkingLevel": "MINIMAL"}
        return {
            "includeThoughts": True,
            "thinkingLevel": {
                "minimal": "MINIMAL",
                "low": "LOW",
                "medium": "MEDIUM",
                "high": "HIGH",
                "xhigh": "HIGH",
            }.get(normalized, "HIGH"),
        }
    budgets = {
        "gemini-2.5-pro": {"minimal": 128, "low": 2048, "medium": 8192, "high": 32768, "xhigh": 32768},
        "gemini-2.5-flash": {"minimal": 128, "low": 2048, "medium": 8192, "high": 24576, "xhigh": 24576},
    }
    for prefix, values in budgets.items():
        if prefix in normalized_model:
            if normalized in {"none", "off", "disabled"}:
                return {"includeThoughts": True, "thinkingBudget": values["minimal"]}
            return {"includeThoughts": True, "thinkingBudget": values.get(normalized, values["medium"])}
    return None


def _normalize_stop(reason: str | None, has_tools: bool) -> str:
    if reason in {"STOP", None}:
        return "tool_use" if has_tools else "stop"
    if reason in {"MAX_TOKENS", "LENGTH"}:
        return "length"
    if reason in {
        "SAFETY",
        "IMAGE_SAFETY",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "IMAGE_PROHIBITED_CONTENT",
        "RECITATION",
        "SPII",
    }:
        return "safety"
    if reason in {
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
        "OTHER_ERROR",
        "FINISH_REASON_UNSPECIFIED",
        "IMAGE_RECITATION",
        "IMAGE_OTHER",
        "LANGUAGE",
        "NO_IMAGE",
        "OTHER",
    }:
        return "error"
    return "error"


def _sanitize_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_schema(item) for key, item in value.items() if key not in {"additionalProperties", "$schema"}}
    if isinstance(value, list):
        return [_sanitize_schema(item) for item in value]
    return value


def _append_text(content: list[Any], value: str) -> list[Any]:
    for index, block in enumerate(content):
        if isinstance(block, TextBlock):
            content[index] = TextBlock(text=(block.text or "") + value)
            return content
    content.append(TextBlock(text=value))
    return content


def _append_thinking(content: list[Any], value: str, signature: str | None) -> list[Any]:
    for index, block in enumerate(content):
        if isinstance(block, ThinkingBlock):
            content[index] = ThinkingBlock(text=block.text + value, signature=signature or block.signature)
            return content
    content.append(ThinkingBlock(text=value, signature=signature))
    return content


def _set_thinking_signature(content: list[Any], signature: str) -> None:
    for index, block in enumerate(content):
        if isinstance(block, ThinkingBlock):
            content[index] = ThinkingBlock(text=block.text, signature=signature)
            return


def _find(content: list[Any], kind: type[Any]) -> int:
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
    include_partial: bool = False,
) -> ProviderError:
    return classify_error(
        body=message,
        code=code,
        streamed=streamed,
            partial=(
                partial_message(
                    content,
                    origin=origin,
                    stop_reason="error",
                    usage=usage,
                    verified_origin=verified,
                )
                if streamed or include_partial or usage is not None
                else None
            ),
    )


def _invalid_tool_metadata(message: str) -> ProviderError:
    return ProviderError(kind="invalid_request", message=message, retryable=False)


def _aborted(reason: str | None, origin: Any, content: list[Any] | None = None, usage: Any = None, verified: bool = True) -> ProviderError:
    return ProviderError(
        kind="aborted",
        message=reason or "provider request aborted",
        retryable=False,
        partial=(
            partial_message(
                content,
                origin=origin,
                usage=usage,
                verified_origin=verified,
            )
            if content or usage is not None
            else None
        ),
    )


def _google_url(base_url: str, model: str) -> str:
    base = base_url.rstrip("/")
    suffix = f"/models/{model}:streamGenerateContent?alt=sse"
    return base if base.endswith(":streamGenerateContent?alt=sse") else f"{base}{suffix}"


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


GoogleProvider = GoogleAdapter
