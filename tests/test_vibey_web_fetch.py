"""WF-2V owning boundary: frozen declarations, gateway result + portable replay.

Existing WF-1 fixtures only read stored records; they cannot detect a runtime
that omits the function, loses its gateway answer, or writes empty ordinary text.
All network is a stub. Replay is compiled by real adapters, not the stub.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from config import v2_config
from core.agent_core.ai.provider import ModelRequest
from core.agent_core.messages import (
    AssistantMessage, Origin, TextBlock, ToolCallBlock, ToolResultMessage, UserMessage,
    WebFetchRecord, message_from_dict, message_to_dict,
)
from modules.agents.vibey.models import HubModelRouter, selection_from_hop
from modules.agents.vibey.tools import hosted_tools
from tests.agent_core.ai._wire import build_wire_request

FIXTURE = Path(__file__).parent / "agent_core/fixtures/web_fetch_v21.json"
SCHEMA = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}


def selection(protocol="anthropic", *, offered=True, token="turn-token"):
    return selection_from_hop({
        "backend": "vibey", "protocol": protocol, "base_url": "http://hub.invalid/vibey/v1",
        "token": token, "runtime_model": "model", "provider": "anthropic" if protocol == "anthropic" else "openai",
        "source_id": "src_modeltest", "source_identity": "test-identity",
        "request_headers": {"x-avibe-turn-id": "turn-fetch"}, "capabilities": {},
        "hosted_tools": {"web_fetch": {"native": "unsupported", "replay": "unsupported"}} if offered else {},
    }, gateway_base_url="http://hub.invalid/vibey")


def router(first):
    async def resolve():
        return first
    return HubModelRouter(resolve, lambda _: None, first=first)


@pytest.mark.parametrize("protocol", ["anthropic", "openai_responses", "openai_chat", "google"])
@pytest.mark.parametrize("active,offered", [(False, True), (True, False), (True, True)])
async def test_fetch_declaration_is_a_frozen_url_only_function(monkeypatch, protocol, active, offered):
    monkeypatch.setattr(v2_config, "WEB_FETCH_ACTIVE", active)
    turn = router(selection(protocol, offered=offered))
    tools = hosted_tools(turn)
    assert [tool.spec.name for tool in tools] == (["web_fetch"] if active and offered else [])
    # A later resolution cannot add/remove a tool, even on Claude. New credentials
    # belong to execution, not the turn's already-frozen tool list.
    turn.current = selection(protocol, offered=not offered, token="next-hop-token")
    request = ModelRequest(endpoint=turn.current.endpoint, system="System", messages=(UserMessage((TextBlock("read"),)),),
                           tools=tuple(tool.spec for tool in tools), max_tokens=128)
    wire = await build_wire_request(request, gateway=True)
    declared = wire.body.get("tools", [])
    if not active or not offered:
        assert declared == []
    else:
        assert len(declared) == 1
        entry = declared[0]
        if request.endpoint.protocol == "openai_chat":
            entry = entry["function"]
        assert entry["name"] == "web_fetch"
        assert entry.get("input_schema", entry.get("parameters")) == SCHEMA
        assert entry.get("type") != "web_fetch_20260318"
        assert "untrusted quoted source" in entry["description"]
        assert "x-avibe-pin-source" not in wire.headers


@pytest.mark.parametrize("outcome", ["completed", "policy", "closed", "off", "malformed", "unreachable"])
async def test_gateway_result_preserves_one_frame_and_live_turn(monkeypatch, outcome):
    monkeypatch.setattr(v2_config, "WEB_FETCH_ACTIVE", True)
    stored = json.loads(FIXTURE.read_text())
    answer = copy.deepcopy(stored["hosted"])
    if outcome == "policy":
        answer.pop("execution")
        answer["record"].update(status="failed", error="blocked", error_code="fetch_url_blocked", content="", size_bytes=0, attempts=[])
    if outcome == "malformed":
        answer["record"]["size_bytes"] += 1
    sent = []

    def gateway(request):
        sent.append(request)
        if outcome == "unreachable":
            raise httpx.ConnectError("offline", request=request)
        if outcome in {"closed", "off"}:
            return httpx.Response(409, json={"error": {"code": "turn_not_live" if outcome == "closed" else "tool_not_enabled"}})
        return httpx.Response(200, json=answer)

    turn = router(selection())
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        (tool,) = hosted_tools(turn, client)
        turn.current = selection(token="next-hop-token")
        result = await tool.execute({"url": "https://example.test/page"}, None)
    assert len(sent) == 1  # No network retry, including malformed/billable answers.
    assert sent[0].url.path == "/vibey/v1/hosted-tools/web_fetch"
    assert json.loads(sent[0].content) == {"arguments": {"url": "https://example.test/page"}}
    assert sent[0].headers["x-avibe-turn-id"] == "turn-fetch"
    assert sent[0].headers["authorization"] == "Bearer next-hop-token"
    assert isinstance(result.hosted.record, WebFetchRecord)
    assert result.hosted.record.requested_url == "https://example.test/page"
    assert result.is_error == (outcome != "completed")
    message = ToolResultMessage("fetch1", "web_fetch", result.content, is_error=result.is_error, hosted=result.hosted)
    saved = message_to_dict(message)
    assert message_from_dict(saved) == message
    assert len(saved["content"][0]["text"].encode()) <= 48000
    if outcome == "completed":
        assert saved["content"] == stored["content"]
        assert saved["hosted"] == stored["hosted"]
    else:
        assert result.hosted.record.error_code == {
            "policy": "fetch_url_blocked", "closed": "gateway_refused", "off": "gateway_refused",
            "malformed": "gateway_no_result", "unreachable": "gateway_unreachable",
        }[outcome]


@pytest.mark.parametrize("protocol", ["anthropic", "openai_responses", "openai_chat", "google"])
async def test_newly_produced_result_replays_without_fetch_or_rebill(monkeypatch, protocol):
    monkeypatch.setattr(v2_config, "WEB_FETCH_ACTIVE", True)
    fixture = json.loads(FIXTURE.read_text())
    calls = []
    def gateway(request):
        calls.append(request)
        return httpx.Response(200, json=fixture["hosted"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        (tool,) = hosted_tools(router(selection()), client)
        result = await tool.execute({"url": "https://example.test/page"}, None)
    # Persist/reload, then switch provider and disable the tool for the next turn.
    first = AssistantMessage(origin=Origin("anthropic", "anthropic", "model"),
                             content=(ToolCallBlock("fetch1", "web_fetch", {"url": "https://example.test/page"}),),
                             stop_reason="tool_use")
    saved = message_to_dict(ToolResultMessage("fetch1", "web_fetch", result.content, is_error=result.is_error, hosted=result.hosted))
    assert saved["content"] == fixture["content"]
    replay = message_from_dict(saved)
    request = ModelRequest(endpoint=selection(protocol, offered=False).endpoint, system="System",
                           messages=(UserMessage((TextBlock("read"),)), first, replay, UserMessage((TextBlock("summarize earlier read"),))),
                           tools=(), max_tokens=128)
    wire = await build_wire_request(request, gateway=True)
    body = json.dumps(wire.body, ensure_ascii=False)
    assert "Hello 世界" in body and "untrusted external quotation" in body
    assert "web_fetch" in body and "https://example.test/page" in body
    assert len(calls) == 1
    assert message_to_dict(replay) == saved  # Costs/attempts remain captured facts.
    # The #2920 downgrade keeps ordinary content even after hosted is discarded.
    portable = message_from_dict({key: value for key, value in saved.items() if key != "hosted"})
    assert portable.content == replay.content
