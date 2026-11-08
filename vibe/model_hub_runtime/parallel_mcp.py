"""Opt-in, anonymous Parallel search via the supported MCP SDK.

Model Hub owns choices, admission, mapping and retry state. Each search owns
its SDK/HTTP lifetime; no other advertised Parallel tools are exposed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import httpx2

from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT, MCP_DEFAULT_SSE_READ_TIMEOUT
from mcp.types import Implementation

from core.handlers.model_hub.adapter import SEARCH_RESPONSE_MAX_BYTES, SearchResponse
from vibe import __version__

ENDPOINT = "https://mcp.parallel.ai/mcp"


class _SearchResponse(httpx2.Response):
    """Bound decoded JSON and SSE before the SDK accumulates or parses them."""

    async def aiter_bytes(self, chunk_size: int | None = None):
        size = 0
        async for chunk in super().aiter_bytes(chunk_size):
            size += len(chunk)
            if size > SEARCH_RESPONSE_MAX_BYTES:
                raise httpx2.StreamError("Model Hub search response exceeds 8 MiB")
            yield chunk


class _SearchTransport(httpx2.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        upstream = await super().handle_async_request(request)
        return _SearchResponse(
            upstream.status_code, headers=upstream.headers,
            stream=upstream.stream, extensions=upstream.extensions,
        )


async def search_parallel(
    request: Mapping[str, Any], *,
    on_sent: Callable[[], Awaitable[None] | None] | None = None,
) -> SearchResponse:
    sent = False
    primary_request = None
    status = None
    headers = {}
    received_at = None

    async def prepare(outgoing):
        nonlocal primary_request, status, headers, received_at
        if outgoing.method != "POST":
            return
        method = json.loads(outgoing.content).get("method")
        if method not in {"initialize", "tools/list", "tools/call"}:
            return
        # A new RPC can fail before reading any headers. Only its own response
        # can supply the status and retry advice used by the service mapper.
        primary_request = outgoing
        status, headers, received_at = None, {}, None
        if method != "tools/call":
            return

        async def before_headers(event, info):
            nonlocal sent
            wire_request = info.get("request")
            if (event not in {"http11.send_request_headers.started", "http2.send_request_headers.started"}
                    or getattr(wire_request, "method", None) != b"POST" or sent):
                return
            # The SDK's request hook runs before connecting. Recheck admission
            # at the wire boundary instead, after connect/TLS and before bytes.
            if on_sent is not None:
                result = on_sent()
                if result is not None:
                    await result
            sent = True

        outgoing.extensions["trace"] = before_headers

    async def received(response):
        nonlocal status, headers, received_at
        if response.request is primary_request:
            status = response.status_code
            headers = {k: v for k, v in response.headers.items() if k.lower() in {"retry-after", "x-ratelimit-reset"}}
            received_at = datetime.now(timezone.utc)

    body = None
    error = ""
    try:
        # Keep the SDK's timeouts and lifetime, with Model Hub's direct egress.
        async with httpx2.AsyncClient(
            headers={"User-Agent": f"Avibe/{__version__}"}, trust_env=False,
            transport=_SearchTransport(trust_env=False), max_redirects=0,
            timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        ) as http:
            http.event_hooks = {"request": [prepare], "response": [received]}
            async with Client(
                streamable_http_client(ENDPOINT, http_client=http, max_sse_event_size=SEARCH_RESPONSE_MAX_BYTES),
                mode="legacy",
                client_info=Implementation(name="avibe", version=__version__),
            ) as client:
                tools = await client.list_tools()
                tool = next((t for t in tools.tools if str(t.name) == "web_search"), None)
                if tool is None or not {"objective", "search_queries"}.issubset(tool.input_schema.get("properties", {})):
                    return SearchResponse(None, {}, None, "invalid_endpoint")
                query = request["objective"]
                try:
                    result = await client.call_tool("web_search", {"objective": query, "search_queries": [query]})
                except MCPError as exc:
                    # Catch inside the SDK context: task-group cleanup must not
                    # wrap and obscure the provider's primary RPC error.
                    envelope = {"error": exc.error.model_dump(mode="json", exclude_none=True)}
                else:
                    envelope = {"result": result.model_dump(mode="json", by_alias=True, exclude_none=True)}
                body = await asyncio.to_thread(lambda: json.dumps(envelope, separators=(",", ":")).encode())
    except MCPError as exc:
        body = json.dumps({"error": exc.error.model_dump(mode="json", exclude_none=True)}).encode()
    except asyncio.CancelledError:
        error = "cancelled"
    except Exception:
        error = "connection"
    return SearchResponse(status, headers, body, error, on_sent_fired=sent, response_received_at=received_at)
