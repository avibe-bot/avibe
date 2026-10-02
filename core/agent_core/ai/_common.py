"""Shared HTTP, origin, and wire-content helpers for native adapters."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, TypeVar
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
from core.agent_core.ai.provider import MediaLoader, ModelEndpoint, ProviderError

ServedHopResolver = Callable[
    [Mapping[str, str]], Origin | None | Awaitable[Origin | None]
]
_T = TypeVar("_T")


def endpoint_origin(endpoint: ModelEndpoint) -> Origin:
    provider = endpoint.provider or f"endpoint:{endpoint.base_url.rstrip('/')}"
    if provider == "endpoint:":
        provider = f"endpoint:{endpoint.protocol}:{endpoint.model_id}"
    return Origin(
        provider=provider,
        api=endpoint.protocol,
        model=endpoint.model_id,
    )


async def resolve_served_origin(
    endpoint: ModelEndpoint,
    headers: Mapping[str, str],
    resolver: ServedHopResolver | None,
    *,
    gateway: bool,
    cancel: CancelToken,
) -> tuple[Origin, bool] | None:
    """Return the served origin and whether its signature data is verified."""

    direct = endpoint_origin(endpoint)
    if not gateway:
        return direct, True
    candidate = resolver(headers) if resolver is not None else default_served_hop_resolver(headers)
    if inspect.isawaitable(candidate):
        try:
            served = await _await_injected(candidate, cancel)
        except _CancelledDependency:
            return None
    else:
        served = candidate
    if cancel.cancelled:
        return None
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
        try:
            chunk = await _await_network(iterator.__anext__(), cancel)
        except StopAsyncIteration:
            break
        if chunk is None:
            return
        for event in parser.feed(chunk):
            yield event
    for event in parser.finish():
        yield event


@asynccontextmanager
async def open_stream(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    json_body: Any,
    headers: Mapping[str, str],
    cancel: CancelToken,
) -> AsyncIterator[httpx.Response | None]:
    """Open and close a streaming response while racing the request against cancellation."""

    if cancel.cancelled:
        yield None
        return
    request = client.build_request(method, url, json=json_body, headers=headers)
    response: httpx.Response | None = None
    try:
        response = await _await_network(client.send(request, stream=True), cancel)
        if response is None:
            yield None
            return
        response.extensions["avibe_terminal_event"] = False
        yield response
    finally:
        if response is not None:
            try:
                # Cleanup must still run after the caller has cancelled. Use the
                # same await owner with a fresh token so cancellation interrupts
                # opening/reads but never leaks an already-open response.
                await _await_network(response.aclose(), CancelToken())
            except Exception as exc:
                if response.extensions.get("avibe_terminal_event"):
                    return
                raise ProviderStreamCloseError(str(exc)) from exc


async def _await_network(awaitable: Awaitable[_T], cancel: CancelToken) -> _T | None:
    """Own one cancellable network await for opening, reads, and body reads."""

    if cancel.cancelled:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        return None
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {operation, cancellation},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation in done and cancel.cancelled:
            operation.cancel()
            with _suppress_base_exceptions():
                await operation
            return None
        return operation.result()
    finally:
        if not cancellation.done():
            cancellation.cancel()
            with _suppress_cancelled():
                await cancellation


class ProviderStreamCloseError(httpx.NetworkError):
    """A response-close failure before a terminal provider event."""


class _CancelledDependency(Exception):
    """Internal marker for cancellation while loading or resolving injected data."""


async def _await_injected(awaitable: Awaitable[_T], cancel: CancelToken) -> _T:
    """Await a loader or resolver through the shared cancellation owner."""

    result = await _await_network(awaitable, cancel)
    if result is None and cancel.cancelled:
        raise _CancelledDependency
    return result  # type: ignore[return-value]


def mark_stream_terminal(response: httpx.Response) -> None:
    """Record that the adapter has emitted its one terminal event."""

    response.extensions["avibe_terminal_event"] = True


def terminal_event(response: httpx.Response, event: _T) -> _T:
    """Mark and return a terminal ProviderEvent in one expression."""

    mark_stream_terminal(response)
    return event


async def read_response_body(
    response: httpx.Response,
    cancel: CancelToken,
    *,
    max_bytes: int = 64 * 1024,
) -> str | None:
    """Read an error body through the shared cancellation owner with a byte cap."""

    iterator = response.aiter_bytes().__aiter__()
    chunks: list[bytes] = []
    size = 0
    while size < max_bytes:
        try:
            chunk = await _await_network(iterator.__anext__(), cancel)
        except StopAsyncIteration:
            break
        if chunk is None:
            return None
        if not chunk:
            continue
        remaining = max_bytes - size
        piece = bytes(chunk[:remaining])
        chunks.append(piece)
        size += len(piece)
        if len(piece) < len(chunk):
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


async def prepare_messages(
    messages: tuple[Any, ...],
    *,
    target: Any,
    supports_images: bool,
    protocol: str,
    media_loader: MediaLoader | None,
    cancel: CancelToken,
) -> tuple[tuple[Any, ...], dict[str, tuple[str, str]]] | ProviderError:
    """Transform history and resolve immutable media snapshots as one safe boundary."""

    try:
        from core.agent_core.ai.transform import transform_messages

        transformed = transform_messages(
            messages,
            target=target,
            supports_images=supports_images,
            protocol=protocol,
        )
        loaded_images = await load_images(transformed.messages, media_loader, cancel)
    except _CancelledDependency:
        return ProviderError(
            kind="aborted",
            message=cancel.reason or "provider request aborted",
            retryable=False,
        )
    except Exception as exc:
        return classify_error(body=f"request preparation failed: {type(exc).__name__}: {exc}")
    return transformed.messages, loaded_images


def incomplete_stream_error(
    content: list[AssistantContent],
    *,
    origin: Origin,
    usage: Any,
    streamed: bool,
    verified_origin: bool,
    protocol: str,
) -> ProviderError:
    """Classify a successful HTTP stream that ended without its protocol terminator."""

    return classify_error(
        body=f"{protocol} stream ended before terminal event",
        streamed=streamed,
        partial=(
                partial_message(
                    content,
                    origin=origin,
                    usage=usage,
                    verified_origin=verified_origin,
                    stop_reason="error",
                )
            if streamed or usage is not None
            else None
        ),
    )


class _suppress_cancelled:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exception_type: type[BaseException] | None, exception: BaseException | None, traceback: Any) -> bool:
        return exception_type is asyncio.CancelledError


class _suppress_base_exceptions:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exception_type: type[BaseException] | None, exception: BaseException | None, traceback: Any) -> bool:
        return exception_type is not None


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


async def load_image_data(
    block: ImageBlock,
    media_loader: MediaLoader | None,
    cancel: CancelToken | None = None,
) -> tuple[str, str]:
    """Resolve a media token just before the request is sent."""

    if media_loader is not None:
        if cancel is None:
            raw, mime_type = await media_loader.load(block.media_token)
        else:
            raw, mime_type = await _await_injected(media_loader.load(block.media_token), cancel)
        return base64.b64encode(raw).decode("ascii"), mime_type
    return image_data(block), block.mime_type


async def load_images(
    messages: tuple[Any, ...],
    media_loader: MediaLoader | None,
    cancel: CancelToken,
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
        raw, mime_type = await _await_injected(media_loader.load(token), cancel)
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


def parsed_arguments(raw: str, *, tool_name: str = "") -> dict[str, Any] | ProviderError:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return _malformed_arguments(tool_name)
    if not isinstance(value, dict):
        return _malformed_arguments(tool_name)
    return value


def _malformed_arguments(tool_name: str) -> ProviderError:
    suffix = f" for tool {tool_name}" if tool_name else ""
    return ProviderError(
        kind="invalid_request",
        message=f"malformed tool arguments{suffix}: expected a JSON object",
        retryable=False,
    )


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
    stop_reason: str = "aborted",
) -> AssistantMessage:
    return assistant_message(
        content,
        origin=origin,
        stop_reason=stop_reason,
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
