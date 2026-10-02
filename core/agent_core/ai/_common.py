"""Shared HTTP, origin, and wire-content helpers for native adapters."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import unquote_to_bytes

import httpx

from core.agent_core.ai.errors import classify_error
from core.agent_core.ai.sse import SSEEvent, SSEParser
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantContent,
    AssistantMessage,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserContent,
)
from core.agent_core.ai.provider import ModelEndpoint
from core.agent_core.ai.provider import MediaLoader

ServedHopResolver = Callable[[Mapping[str, str]], Origin | None]


def endpoint_origin(endpoint: ModelEndpoint) -> Origin:
    return Origin(
        provider=endpoint.provider or _default_provider(endpoint.protocol),
        api=endpoint.protocol,
        model=endpoint.model_id,
    )


def resolve_served_origin(
    endpoint: ModelEndpoint,
    headers: Mapping[str, str],
    resolver: ServedHopResolver | None,
    *,
    gateway: bool,
) -> tuple[Origin, bool]:
    """Return the served origin and whether its signature data is verified."""

    direct = endpoint_origin(endpoint)
    if not gateway:
        return direct, True
    served = resolver(headers) if resolver is not None else default_served_hop_resolver(headers)
    return served or direct, served is not None


def default_served_hop_resolver(headers: Mapping[str, str]) -> Origin | None:
    """Read the exact C-6 served-hop header."""

    normalized = {key.lower(): value for key, value in headers.items()}
    report = normalized.get("x-avibe-served-hop")
    if not report or not report.isascii():
        return None
    try:
        value = json.loads(report, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, Mapping):
        return None
    if set(value) != {"provider", "api", "model"} or any(not isinstance(value[key], str) for key in value):
        return None
    try:
        return Origin(provider=value["provider"], api=value["api"], model=value["model"])
    except (KeyError, TypeError, ValueError):
        return None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("served-hop report contains duplicate keys")
    return value


def auth_headers(endpoint: ModelEndpoint, *, provider: str, gateway: bool) -> dict[str, str]:
    headers = {str(key): str(value) for key, value in endpoint.request_headers.items()}
    if any(key.lower() in {"authorization", "x-api-key", "x-goog-api-key"} for key in headers):
        return headers
    if gateway or provider in {"openai", "openai_chat", "openai_responses"}:
        headers["Authorization"] = f"Bearer {endpoint.token}"
    elif provider == "anthropic":
        headers["x-api-key"] = endpoint.token
    elif provider == "google":
        headers["x-goog-api-key"] = endpoint.token
    else:
        headers["Authorization"] = f"Bearer {endpoint.token}"
    return headers


async def iter_sse_events(
    response: httpx.Response,
    cancel: CancelToken,
) -> AsyncIterator[SSEEvent]:
    """Yield SSE events while allowing a CancelToken to interrupt a stalled read."""

    parser = SSEParser()
    iterator = response.aiter_bytes().__aiter__()
    while True:
        if cancel.cancelled:
            return
        read_task = asyncio.create_task(iterator.__anext__())
        cancel_task = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait({read_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done and cancel.cancelled:
                read_task.cancel()
                with _suppress_cancelled():
                    await read_task
                return
            cancel_task.cancel()
            with _suppress_cancelled():
                await cancel_task
            try:
                chunk = read_task.result()
            except StopAsyncIteration:
                break
            for event in parser.feed(chunk):
                yield event
        finally:
            if not read_task.done():
                read_task.cancel()
            if not cancel_task.done():
                cancel_task.cancel()
    for event in parser.finish():
        yield event


class _suppress_cancelled:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exception_type: type[BaseException] | None, exception: BaseException | None, traceback: Any) -> bool:
        return exception_type is asyncio.CancelledError


def json_object(data: str) -> dict[str, Any] | None:
    try:
        value = json.loads(data)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def image_data(block: ImageBlock) -> str:
    """Resolve the in-memory media token form used by hermetic adapter tests."""

    token = block.media_token
    if token.startswith("data:") and ";base64," in token:
        return token.split(";base64,", 1)[1]
    if token.startswith("data:,"):
        return base64.b64encode(unquote_to_bytes(token[6:])).decode("ascii")
    return token


async def load_image_data(block: ImageBlock, media_loader: MediaLoader | None) -> tuple[str, str]:
    """Resolve a media token just before the request is sent."""

    if media_loader is not None:
        raw, mime_type = await media_loader.load(block.media_token)
        return base64.b64encode(raw).decode("ascii"), mime_type
    return image_data(block), block.mime_type


async def load_images(
    messages: tuple[Any, ...],
    media_loader: MediaLoader | None,
) -> dict[str, tuple[str, str]]:
    """Load every image token once for a request."""

    if media_loader is None:
        return {}
    tokens: list[str] = []
    for message in messages:
        content = getattr(message, "content", ())
        for block in content:
            if isinstance(block, ImageBlock) and block.media_token not in tokens:
                tokens.append(block.media_token)
    loaded: dict[str, tuple[str, str]] = {}
    for token in tokens:
        raw, mime_type = await media_loader.load(token)
        loaded[token] = (base64.b64encode(raw).decode("ascii"), mime_type)
    return loaded


def text_from_content(content: tuple[UserContent, ...]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text or "")
    return "\n".join(part for part in parts if part)


def content_parts(
    content: tuple[UserContent, ...],
    *,
    include_images: bool = True,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            if block.text:
                result.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock) and include_images:
            data, mime_type = (loaded_images or {}).get(block.media_token, (image_data(block), block.mime_type))
            result.append({"type": "image", "mime_type": mime_type, "data": data, "media_token": block.media_token})
    return result


def tool_schema(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }


def parsed_arguments(raw: str) -> dict[str, Any]:
    value = json_object(raw)
    return value if value is not None else {}


def assistant_message(
    content: list[AssistantContent],
    *,
    origin: Origin,
    stop_reason: str,
    usage: Any = None,
    error_message: str | None = None,
    verified_origin: bool = True,
) -> AssistantMessage:
    if not verified_origin:
        sanitized: list[AssistantContent] = []
        for block in content:
            if isinstance(block, ThinkingBlock):
                if block.redacted:
                    continue
                sanitized.append(replace(block, signature=None))
            elif isinstance(block, ToolCallBlock):
                sanitized.append(replace(block, signature=None))
            else:
                sanitized.append(block)
        content = sanitized
    return AssistantMessage(
        content=tuple(content),
        origin=origin,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=usage,
        error_message=error_message,
    )


def partial_message(
    content: list[AssistantContent],
    *,
    origin: Origin,
    usage: Any = None,
    verified_origin: bool = True,
) -> AssistantMessage:
    return assistant_message(
        content,
        origin=origin,
        stop_reason="aborted",
        usage=usage,
        verified_origin=verified_origin,
    )


def _default_provider(protocol: str) -> str:
    return {
        "anthropic": "anthropic",
        "openai_chat": "openai",
        "openai_responses": "openai",
        "google": "google",
    }.get(protocol, protocol)
