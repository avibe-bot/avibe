"""Hermetic KEX-2 coverage: MCP lifecycle and keyless Exa outcomes."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from aiohttp import web
import pytest

from core.handlers.model_hub.adapter import SearchResponse
from core.handlers.model_hub.search_services import ServiceCooldowns, map_response
from config.v2_config import model_hub_keyless_exa_source
from vibe.model_hub_runtime.api_key_vendors import search_service
from vibe.model_hub_runtime.keyless_exa_mcp import KeylessExaMcpClient


NOW = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
SERVICE = search_service("exa-mcp", "search")
assert SERVICE is not None


def _rpc(request_id: int, result: object) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_schema(*, missing: str | None = None) -> dict:
    properties = {
        "query": {"type": "string"},
        "numResults": {"type": "integer"},
        "enableHighlights": {"type": "boolean"},
        "highlightsMaxCharacters": {"type": "integer"},
        "textMaxCharacters": {"type": "integer"},
        "objective": {"type": "string"},
    }
    properties.pop(missing, None)
    return {
        "name": "web_search_advanced_exa",
        "inputSchema": {"type": "object", "properties": properties},
    }


@asynccontextmanager
async def _mcp_stub(*, missing: str | None = None, sse: bool = False, stale_tools_once: bool = False):
    calls: list[dict] = []
    state = {"session": "stub-session-1", "stale_tools": stale_tools_once}

    async def handle(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        calls.append({"headers": dict(request.headers), "payload": payload})
        method = payload["method"]
        request_id = payload.get("id")
        if method == "initialize":
            return web.json_response(
                _rpc(request_id, {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "stub-exa", "version": "1"},
                }),
                headers={"Mcp-Session-Id": state["session"]},
            )
        if method == "notifications/initialized":
            return web.Response(status=202, headers={"Mcp-Session-Id": state["session"]})
        if method == "tools/list":
            if state["stale_tools"]:
                state["stale_tools"] = False
                return web.json_response({"error": {"message": "MCP session not found"}}, status=404)
            return web.json_response(_rpc(request_id, {"tools": [_tool_schema(missing=missing)]}))
        if method == "tools/call":
            result = {
                "content": [{"type": "text", "text": json.dumps({
                    "results": [{
                        "url": "https://example.test/result",
                        "title": "Example result",
                        "highlights": ["A highlight", "Another highlight"],
                        "text": "This must never be surfaced",
                        "publishedDate": "2026-10-23",
                    }],
                })}],
            }
            body = json.dumps(_rpc(request_id, result), separators=(",", ":"))
            if sse:
                return web.Response(
                    body=f"event: message\ndata: {body}\n\n".encode(),
                    content_type="text/event-stream",
                )
            return web.json_response(_rpc(request_id, result))
        raise AssertionError(method)

    app = web.Application()
    app.router.add_post("/mcp", handle)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp", calls
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
async def test_keyless_mcp_negotiates_once_adapts_live_schema_and_maps_highlights(sse: bool) -> None:
    """KEX-2: one session sends only advertised search controls and maps highlights, not text."""
    async with _mcp_stub(sse=sse) as (endpoint, calls):
        client = KeylessExaMcpClient(endpoint=endpoint, client_version="test")
        sent: list[bool] = []
        response = await client.search(
            {
                "type": "auto",
                "query": "avibe",
                "numResults": 3,
                "enableHighlights": True,
                "highlightsMaxCharacters": 1000,
                "textMaxCharacters": 1,
            },
            on_sent=lambda: sent.append(True),
        )

    assert [call["payload"]["method"] for call in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    assert calls[0]["headers"]["User-Agent"] == "Avibe/test"
    assert calls[0]["headers"]["x-exa-source"] == "avibe"
    assert calls[0]["payload"]["params"]["clientInfo"] == {"name": "avibe", "version": "test"}
    assert calls[-1]["payload"]["params"]["arguments"] == {
        "query": "avibe",
        "numResults": 3,
        "enableHighlights": True,
        "highlightsMaxCharacters": 1000,
        "textMaxCharacters": 1,
    }
    assert sent == [True]
    mapped = map_response(SERVICE, response, sent=True, results=3, say=lambda key, **_: key)
    assert mapped.record == {
        "status": "completed",
        "sources": [{
            "url": "https://example.test/result",
            "title": "Example result",
            "snippet": "A highlight\n\nAnother highlight",
            "page_age": "2026-10-23",
        }],
    }
    assert mapped.cost is not None and mapped.cost.usd == 0
    assert not mapped.billable and not mapped.uncertain


@pytest.mark.asyncio
async def test_keyless_mcp_refreshes_tools_schema_for_each_search_on_one_session() -> None:
    """KEX-2: a reused session still refreshes tools/list before every admitted search."""
    async with _mcp_stub() as (endpoint, calls):
        client = KeylessExaMcpClient(endpoint=endpoint, client_version="test")
        arguments = {"query": "avibe", "numResults": 1, "enableHighlights": True, "textMaxCharacters": 1}
        await client.search(arguments)
        await client.search(arguments)
    assert [call["payload"]["method"] for call in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call", "tools/list", "tools/call",
    ]


@pytest.mark.asyncio
async def test_keyless_mcp_replays_one_stale_pre_tool_session_without_double_counting_admission() -> None:
    """KEX-2: session-not-found before tools/call reinitializes and invokes admission once."""
    async with _mcp_stub(stale_tools_once=True) as (endpoint, calls):
        client = KeylessExaMcpClient(endpoint=endpoint, client_version="test")
        admitted: list[bool] = []
        response = await client.search(
            {"query": "avibe", "numResults": 1, "enableHighlights": True, "textMaxCharacters": 1},
            on_sent=lambda: admitted.append(True),
        )
    assert response.http_status == 200
    assert admitted == [True]
    assert [call["payload"]["method"] for call in calls] == [
        "initialize", "notifications/initialized", "tools/list",
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]


@pytest.mark.asyncio
async def test_keyless_mcp_rejects_a_schema_without_text_bound_before_tools_call() -> None:
    """KEX-2: a missing required highlight/text control is unavailable without sending a call."""
    async with _mcp_stub(missing="textMaxCharacters") as (endpoint, calls):
        response = await KeylessExaMcpClient(endpoint=endpoint).search({"query": "avibe", "numResults": 3})
    assert response.error == "invalid_endpoint"
    assert [call["payload"]["method"] for call in calls] == [
        "initialize", "notifications/initialized", "tools/list",
    ]


@pytest.mark.parametrize(
    "status,body,headers,expected",
    [
        (429, {"error": {"message": "slow down"}}, {"Retry-After": "7"}, "service_rate_limited"),
        (200, {"jsonrpc": "2.0", "id": 1, "result": {"_meta": {"ai.exa/rateLimited": True}, "content": []}}, {}, "service_rate_limited"),
        (200, {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "Free MCP rate limit exceeded"}]}}, {}, "service_rate_limited"),
        (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Unauthorized"}}, {}, "service_unavailable"),
        (401, {"error": {"code": -32000, "message": "Unauthorized"}}, {}, "service_unavailable"),
        (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": "PROHIBITED_CONTENT", "message": "blocked"}}, {}, "service_refused"),
    ],
)
def test_keyless_outcome_table_never_reports_a_credential_error(status, body, headers, expected) -> None:
    """KEX-2: only approved rate-limit signals cool the service; ambiguous -32000 is unavailable."""
    response = SearchResponse(status, headers, json.dumps(body).encode(), response_received_at=NOW)
    result = map_response(SERVICE, response, sent=True, results=3, say=lambda key, **_: key)
    assert result.record["error_code"] == expected
    assert result.record["error_code"] != "service_key_invalid"
    assert result.cost is not None and result.cost.usd == 0
    assert not result.billable and not result.uncertain
    assert result.cooling is (expected == "service_rate_limited" or expected == "service_unavailable")


def test_keyless_cooldown_prefers_later_epoch_millisecond_reset() -> None:
    """KEX-2: X-RateLimit-Reset is absolute Unix milliseconds, not a delay."""
    source = model_hub_keyless_exa_source()
    cooldowns = ServiceCooldowns()
    generation = cooldowns.advance(source, "exa/mcp")
    later = NOW + timedelta(seconds=90)
    result = map_response(
        SERVICE,
        SearchResponse(429, {"Retry-After": "5", "X-RateLimit-Reset": str(int(later.timestamp() * 1000))}, b"{}", response_received_at=NOW),
        sent=True,
        results=3,
        say=lambda key, **_: key,
    )
    assert cooldowns.settle(source, "exa/mcp", generation, result, SearchResponse(429, {"Retry-After": "5", "X-RateLimit-Reset": str(int(later.timestamp() * 1000))}, b"{}", response_received_at=NOW), NOW)
    assert cooldowns.until(source, "exa/mcp", NOW) == later
