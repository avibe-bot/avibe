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

from core.handlers.model_hub.adapter import SearchResponse
from vibe import __version__

ENDPOINT = "https://mcp.parallel.ai/mcp"


async def search_parallel(
    request: Mapping[str, Any], *,
    on_sent: Callable[[], Awaitable[None] | None] | None = None,
) -> SearchResponse:
    sent = False
    status = None
    headers = {}
    received_at = None

    async def prepare(outgoing):
        if outgoing.method != "POST" or json.loads(outgoing.content).get("method") != "tools/call":
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
        # Background GET and DELETE cannot overwrite the primary outcome.
        if response.request.method == "POST":
            status = response.status_code
            headers = {k: v for k, v in response.headers.items() if k.lower() in {"retry-after", "x-ratelimit-reset"}}
            received_at = datetime.now(timezone.utc)

    body = None
    error = ""
    try:
        # Keep the SDK's timeouts and lifetime, with Model Hub's direct egress.
        async with httpx2.AsyncClient(
            headers={"User-Agent": f"Avibe/{__version__}"}, trust_env=False,
            timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        ) as http:
            http.event_hooks = {"request": [prepare], "response": [received]}
            async with Client(
                streamable_http_client(ENDPOINT, http_client=http), mode="legacy",
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
                body = json.dumps(envelope).encode()
                if len(body) > 2 * 1024 * 1024:
                    body, error = None, "body_too_large"
    except MCPError as exc:
        body = json.dumps({"error": exc.error.model_dump(mode="json", exclude_none=True)}).encode()
    except asyncio.CancelledError:
        error = "cancelled"
    except Exception:
        error = "connection"
    return SearchResponse(status, headers, body, error, on_sent_fired=sent, response_received_at=received_at)
