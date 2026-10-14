"""Passive hosted-tool evidence: what a served response proves ran (vibey-hosted-tools.md §3.2)."""

from __future__ import annotations

import json
from dataclasses import replace

from aiohttp import web
import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_hosted_pinning import SHELL, _native, _post
from tests.test_model_hub_hosted_pinning import _sources as _pinned_sources
from tests.test_model_hub_l3 import LiveInvokeHandle, _outcome
from tests.test_model_hub_retry_advice import _engine, _invoke
from tests.test_model_hub_vibey_consumer import _vibey_service


def _responses_stream(item: dict, terminal: str) -> bytes:
    done = {"type": "response.output_item.done", "output_index": 0, "item": item}
    return (
        b"event: response.output_item.done\ndata: " + json.dumps(done).encode() + b"\n\n"
        + {
            "completed": b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
            "failed": b'event: response.failed\ndata: {"type":"response.failed","response":'
            b'{"error":{"type":"server_error"}}}\n\n',
        }[terminal]
    )


def _anthropic_stream(block: dict, terminal: str) -> bytes:
    start = {"type": "content_block_start", "index": 1, "content_block": block}
    return (
        b"event: content_block_start\ndata: " + json.dumps(start).encode() + b"\n\n"
        + {
            "completed": b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            "failed": b'event: error\ndata: {"type":"error","error":{"type":"api_error"}}\n\n',
        }[terminal]
    )


def _responses_body(item: dict, terminal: str) -> bytes:
    if terminal == "failed":
        return json.dumps({"output": [item], "error": {"type": "server_error"}}).encode()
    text = {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
    return json.dumps({"object": "response", "status": "completed", "output": [item, text]}).encode()


def _anthropic_body(block: dict, terminal: str) -> bytes:
    if terminal == "failed":
        return json.dumps({"type": "error", "error": {"type": "api_error"}, "content": [block]}).encode()
    call = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}}
    return json.dumps({"type": "message", "content": [call, block, {"type": "text", "text": "ok"}]}).encode()


WIRE = {
    "openai_responses": (_responses_stream, _responses_body),
    "anthropic": (_anthropic_stream, _anthropic_body),
}
RESULTS = [{"type": "web_search_result", "url": "https://example.test", "title": "t", "encrypted_content": "e"}]
ITEMS = [
    ("openai_responses", {"type": "web_search_call", "id": "ws_1", "status": "completed"}, "web_search"),
    ("openai_responses", {"type": "web_search_call", "id": "ws_1", "status": "failed"}, None),
    ("openai_responses", {"type": "web_search_call", "id": "ws_1", "status": "in_progress"}, None),
    ("openai_responses", {"type": "custom_tool_call", "name": "x_keyword_search", "input": "{}"}, "x_search"),
    ("openai_responses", {"type": "custom_tool_call", "name": "lookup", "input": "{}"}, None),
    ("openai_responses", {"type": "image_generation_call", "id": "ig_1", "status": "completed"}, None),
    ("anthropic", {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": RESULTS}, "web_search"),
    (
        "anthropic",
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"},
        },
        None,
    ),
    ("anthropic", {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_1", "content": RESULTS}, None),
]


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "buffered"])
@pytest.mark.parametrize("terminal", ["completed", "failed"])
@pytest.mark.parametrize(("protocol", "item", "tool"), ITEMS)
async def test_a_served_response_proves_only_its_own_completed_search_item(
    protocol: str, item: dict, tool: str | None, terminal: str, stream: bool,
) -> None:
    payload = WIRE[protocol][0 if stream else 1](item, terminal)

    async def respond(_request: web.Request) -> web.Response:
        if stream:
            return web.Response(body=payload, content_type="text/event-stream")
        return web.Response(body=payload, content_type="application/json")

    async with _engine(respond) as (client, _requests):
        outcome, _forwarded = await _invoke(client, protocol, stream)

    assert outcome.hosted_tools == (frozenset({tool}) if tool is not None and terminal == "completed" else frozenset())


@pytest.mark.parametrize(("tools", "proven", "recorded"), [
    # A served response that ran the tool the request declared natively is that tool's success.
    ([{"type": "x_search"}, SHELL], {"x_search"}, True),
    # It proves nothing about a tool the request did not declare natively.
    ([SHELL], {"x_search"}, False),
    ([{"type": "x_search"}, SHELL], set(), False),
])
async def test_a_native_success_is_recorded_at_settlement_for_a_tool_the_request_declared(
    tmp_path, tools, proven, recorded,
):
    """MH-HOSTED-GATEWAY-006: positive evidence is display history, written once the served response settles."""

    primary, fallback = _pinned_sources()
    served = replace(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), hosted_tools=frozenset(proven))
    service = _vibey_service(tmp_path, [primary, fallback], handles=[LiveInvokeHandle(served, (b"{}",))])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:proof", turn_id="turn-proof")
        body = {**_native(hop), "tools": tools}
        native = any(tool.get("type") == "x_search" for tool in tools)
        assert (await _post(hop, body, pin=primary.id if native else None))[0] == 200
    finally:
        await gateway.close()
    latest = service.capability_evidence.latest_success("x_search")
    if recorded:
        assert (latest.source_id, latest.model_id, latest.mode) == (primary.id, "grok-4.6", "native")
    else:
        assert latest is None
