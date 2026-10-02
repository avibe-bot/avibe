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
    auth_headers,
    content_parts,
    drive_sse_stream,
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
        protocol_terminal = False
        part_sequence = 0
        text_key: Any = ("text", 0)
        thinking_key: Any = ("thinking", 0)
        last_part_kind: str | None = None
        try:
            payload = build_google_payload(request, transformed_messages, loaded_images=loaded_images)
            headers = auth_headers(request.endpoint, provider="google", gateway=self._gateway)
            headers.setdefault("content-type", "application/json")
            url = _google_url(request.endpoint.base_url, request.endpoint.model_id)
            async def translate(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
                nonlocal finish_reason, protocol_terminal, part_sequence
                nonlocal text_key, thinking_key, last_part_kind
                async for event in events:
                    if cancel.cancelled:
                        terminal = assembler.terminal(assembler.aborted(cancel.reason))
                        if terminal is not None:
                            yield terminal
                        return
                    if not event.data:
                        continue
                    chunk = json_object(event.data)
                    if chunk is None:
                        terminal = assembler.terminal(
                            assembler.error("Provider returned invalid Gemini JSON", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    top_level_error = chunk.get("error")
                    if top_level_error is not None and not isinstance(top_level_error, Mapping):
                        terminal = assembler.terminal(
                            assembler.error("Gemini error must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if isinstance(top_level_error, Mapping):
                        terminal = assembler.terminal(
                            assembler.error(
                                _string(top_level_error.get("message"))
                                or json.dumps(dict(chunk), ensure_ascii=False),
                                code=_string(top_level_error.get("status"))
                                or _string(top_level_error.get("code"))
                                or None,
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if chunk.get("usageMetadata") is not None and not isinstance(chunk.get("usageMetadata"), Mapping):
                        terminal = assembler.terminal(
                            assembler.error("Gemini usageMetadata must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    assembler.set_usage(_gemini_usage(chunk.get("usageMetadata")))
                    candidates = chunk.get("candidates")
                    if candidates is not None and not isinstance(candidates, list):
                        terminal = assembler.terminal(
                            assembler.error("Gemini candidates must be an array", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if not isinstance(candidates, list) or not candidates:
                        feedback = chunk.get("promptFeedback")
                        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
                            finish_reason = "SAFETY"
                            protocol_terminal = True
                            break
                        continue
                    candidate = candidates[0]
                    if not isinstance(candidate, Mapping):
                        terminal = assembler.terminal(
                            assembler.error("Gemini candidate must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if isinstance(candidate.get("finishReason"), str):
                        finish_reason = candidate["finishReason"]
                    elif candidate.get("finishReason") is not None:
                        terminal = assembler.terminal(
                            assembler.error("Gemini finishReason must be a string", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    candidate_content = candidate.get("content")
                    if candidate_content is not None and not isinstance(candidate_content, Mapping):
                        terminal = assembler.terminal(
                            assembler.error("Gemini candidate content must be an object", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if isinstance(candidate_content, Mapping):
                        parts = candidate_content.get("parts")
                        if parts is not None and not isinstance(parts, list):
                            terminal = assembler.terminal(
                                assembler.error("Gemini content parts must be an array", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if isinstance(parts, list):
                            for part in parts:
                                if not isinstance(part, Mapping):
                                    terminal = assembler.terminal(
                                        assembler.error("Gemini part must be an object", kind="invalid_request")
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                raw_signature = part.get("thoughtSignature")
                                if raw_signature is not None and not isinstance(raw_signature, str):
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "Gemini thoughtSignature must be a string",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                signature = _string(raw_signature)
                                value = _string(part.get("text"))
                                if part.get("text") is not None and not isinstance(part.get("text"), str):
                                    terminal = assembler.terminal(
                                        assembler.error("Gemini text must be a string", kind="invalid_request")
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                if part.get("text") is not None and (value or signature):
                                    if part.get("thought") is True:
                                        if last_part_kind != "thinking":
                                            part_sequence += 1
                                            thinking_key = ("thinking", part_sequence)
                                        last_part_kind = "thinking"
                                        emitted = assembler.thinking_delta(
                                            thinking_key,
                                            value,
                                            signature=signature or None,
                                        )
                                        if emitted is not None:
                                            yield emitted
                                    else:
                                        if last_part_kind != "text":
                                            part_sequence += 1
                                            text_key = ("text", part_sequence)
                                        last_part_kind = "text"
                                        if value:
                                            emitted = assembler.text_delta(text_key, value)
                                            if emitted is not None:
                                                yield emitted
                                    if signature:
                                        if part.get("thought") is True:
                                            assembler.set_thinking_signature(thinking_key, signature)
                                        else:
                                            assembler.set_latest_thinking_signature(
                                                signature,
                                                fallback_key=("gemini-text-signature", text_key),
                                            )
                                function_call = part.get("functionCall")
                                if function_call is not None and not isinstance(function_call, Mapping):
                                    terminal = assembler.terminal(
                                        assembler.error("Gemini functionCall must be an object", kind="invalid_request")
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                if isinstance(function_call, Mapping):
                                    if last_part_kind != "tool":
                                        part_sequence += 1
                                    last_part_kind = "tool"
                                    explicit_id = _string(function_call.get("id"))
                                    name = _string(function_call.get("name"))
                                    if not name:
                                        terminal = assembler.terminal(
                                            assembler.error("functionCall name must not be empty", kind="invalid_request")
                                        )
                                        if terminal is not None:
                                            yield terminal
                                        return
                                    arguments = function_call.get("args")
                                    if arguments is not None and not isinstance(arguments, (Mapping, str)):
                                        terminal = assembler.terminal(
                                            assembler.error(
                                                "functionCall args must be an object or JSON string",
                                                kind="invalid_request",
                                            )
                                        )
                                        if terminal is not None:
                                            yield terminal
                                        return
                                    raw_args = (
                                        json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                                        if isinstance(arguments, Mapping)
                                        else _string(arguments)
                                    )
                                    key = ("google-call", assembler.tool_count())
                                    start = assembler.tool_start(
                                        key,
                                        call_id=None,
                                        native_id=explicit_id or None,
                                        name=name,
                                        signature=signature or None,
                                    )
                                    if start is not None:
                                        yield start
                                    if isinstance(arguments, Mapping):
                                        emitted = assembler.tool_arguments_object(key, arguments)
                                        if emitted is not None:
                                            yield emitted
                                    else:
                                        emitted = assembler.tool_arguments(key, raw_args)
                                        if emitted is not None:
                                            yield emitted
                    if finish_reason:
                        protocol_terminal = True
                if cancel.cancelled:
                    terminal = assembler.terminal(assembler.aborted(cancel.reason))
                    if terminal is not None:
                        yield terminal
                    return
                if not protocol_terminal:
                    terminal = assembler.terminal(assembler.incomplete())
                    if terminal is not None:
                        yield terminal
                    return
                for event in assembler.block_end_events():
                    yield event
                final = assembler.finalize(
                    _normalize_stop(finish_reason, assembler.has_tools())
                )
                terminal = assembler.terminal(final if isinstance(final, ProviderError) else Done(final))
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


def _gemini_usage(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    from core.agent_core.messages import Usage

    cache_read_tokens = _nonnegative(value.get("cachedContentTokenCount"))
    return Usage(
        input_tokens=max(0, _nonnegative(value.get("promptTokenCount")) - cache_read_tokens),
        output_tokens=_nonnegative(value.get("candidatesTokenCount"))
        + _nonnegative(value.get("thoughtsTokenCount")),
        cache_read_tokens=cache_read_tokens,
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


def _google_url(base_url: str, model: str) -> str:
    base = base_url.rstrip("/")
    suffix = f"/models/{model}:streamGenerateContent?alt=sse"
    return base if base.endswith(":streamGenerateContent?alt=sse") else f"{base}{suffix}"


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


GoogleProvider = GoogleAdapter
