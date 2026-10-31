"""Anthropic prompt-cache breakpoints, read on the wire at the adapter's HTTP boundary.

A Claude route caches its static prefix (tools, then system) for an hour and the conversation for the default five
minutes; every other route keeps the default everywhere. An endpoint that rejects the field never fails a turn
(C-2 ``conversion-tables.md``, Anthropic "Cache" row). Usage parsing of the TTL breakdown is pinned by
``test_provider_adapters.py`` (``test_anthropic_usage_*``).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
import pytest

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.provider import Done, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import AssistantMessage, Origin, TextBlock, ToolCallBlock, ToolResultMessage, UserMessage
from core.agent_core.tools.base import ToolSpec
from core.handlers.model_hub.provenance import produce_turn_outcome, render_turn_outcome_copy

HOUR = {"type": "ephemeral", "ttl": "1h"}
DEFAULT = {"type": "ephemeral"}
LIFETIME_S = {"1h": 3600, None: 300}
SYSTEM = "You are a coding agent."
CLAUDE = "claude-opus-5-5"
TOOLS = (
    ToolSpec("read", "Read a file.", {"type": "object"}),
    ToolSpec("bash", "Run a command.", {"type": "object"}),
)
CONVERSATION = (
    UserMessage((TextBlock("list the files"),)),
    AssistantMessage(
        (ToolCallBlock("toolu_1", "bash", {"command": "ls"}),),
        origin=Origin("anthropic", "anthropic", CLAUDE),
        stop_reason="tool_use",
    ),
    ToolResultMessage("toolu_1", "bash", (TextBlock("a.txt"),)),
)
SUCCESS = (
    'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
    'data: {"type":"message_stop"}\n\n'
)
TTL_REJECTED = "system.0.cache_control.ttl: Extra inputs are not permitted"
# The two shapes the rejection reaches the adapter in: the endpoint's own Anthropic envelope, and Model Hub's gateway
# (Vibey's only route), which surfaces the upstream message inside its own copy.
ANTHROPIC_REJECTION = {"type": "error", "error": {"type": "invalid_request_error", "message": TTL_REJECTED}}
GATEWAY_REJECTION = {
    "error": {
        "type": "upstream_request_invalid",
        "code": "upstream_request_invalid",
        "message": render_turn_outcome_copy(
            produce_turn_outcome("turn.request_nonfallback", upstream_detail=TTL_REJECTED), "en"
        ),
    }
}


def _request(
    provider: str = "anthropic",
    model_id: str = CLAUDE,
    *,
    cache: str = "default",
    system: str = SYSTEM,
    tools: tuple[ToolSpec, ...] = TOOLS,
    messages: tuple[Any, ...] = CONVERSATION,
    base_url: str | None = None,
    source_id: str | None = None,
) -> ModelRequest:
    # A fresh endpoint per request unless one is given: a route's TTL memory lasts the process.
    return ModelRequest(
        endpoint=ModelEndpoint(
            "anthropic",
            base_url or f"https://{uuid.uuid4().hex}.test/v1",
            model_id,
            "token",
            provider=provider,
            source_id=source_id,
        ),
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=128,
        cache=cache,  # type: ignore[arg-type]
    )


def _breakpoints(body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every breakpoint in Anthropic's evaluation order: tools, then system, then messages."""

    blocks = [(f"tools.{i}", tool) for i, tool in enumerate(body.get("tools", ()))]
    if isinstance(body["system"], list):
        blocks += [(f"system.{i}", block) for i, block in enumerate(body["system"])]
    for m, message in enumerate(body["messages"]):
        if isinstance(message["content"], list):
            blocks += [(f"messages.{m}.content.{c}", block) for c, block in enumerate(message["content"])]
    return [(where, block["cache_control"]) for where, block in blocks if "cache_control" in block]


def _carries_ttl(body: dict[str, Any]) -> bool:
    return any("ttl" in cache_control for _, cache_control in _breakpoints(body))


def _without_ttl(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_ttl(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: {k: v for k, v in item.items() if k != "ttl"} if key == "cache_control" else _without_ttl(item)
        for key, item in value.items()
    }


async def _run(client: httpx.AsyncClient, request: ModelRequest) -> list[Any]:
    return [event async for event in AnthropicAdapter(client).stream(request, CancelToken())]


async def _wire(request: ModelRequest) -> tuple[bytes, dict[str, Any]]:
    sent: list[bytes] = []

    def handler(wire: httpx.Request) -> httpx.Response:
        sent.append(wire.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _run(client, request)
    assert isinstance(events[-1], Done)
    assert len(sent) == 1
    return sent[0], json.loads(sent[0])


@pytest.mark.parametrize(
    ("provider", "model_id", "cache", "prefix"),
    [
        ("anthropic", CLAUDE, "default", HOUR),
        ("anthropic", "claude-haiku-4-5-20251001", "default", HOUR),
        # Another vendor's Anthropic-compatible endpoint, even when it names a Claude model: support is unknown.
        ("custom", CLAUDE, "default", DEFAULT),
        ("custom", "kimi-k2-turbo-preview", "default", DEFAULT),
        # A non-Claude model behind the Anthropic vendor, and an id that only contains the family name.
        ("anthropic", "gpt-5", "default", DEFAULT),
        ("anthropic", "relay-claude-opus-5-5", "default", DEFAULT),
        ("anthropic", CLAUDE, "none", None),
    ],
)
async def test_only_a_claude_route_caches_its_static_prefix_for_an_hour(
    provider: str, model_id: str, cache: str, prefix: dict[str, str] | None,
) -> None:
    raw, body = await _wire(_request(provider, model_id, cache=cache))

    if prefix is None:
        assert body["system"] == SYSTEM
        assert _breakpoints(body) == []
    else:
        assert _breakpoints(body) == [
            ("tools.1", prefix),
            ("system.0", prefix),
            ("messages.0.content.0", DEFAULT),
            ("messages.2.content.0", DEFAULT),
        ]
    if prefix is not HOUR:
        # Today's request, unchanged: no route but a Claude one ever sees the field.
        assert b'"ttl"' not in raw


@pytest.mark.parametrize(
    ("system", "tools", "messages"),
    [
        (SYSTEM, TOOLS, CONVERSATION),
        (SYSTEM, (), CONVERSATION),
        ("", TOOLS, CONVERSATION),
        (SYSTEM, TOOLS, (UserMessage((TextBlock("hello"),)),)),
        ("", (), CONVERSATION),
    ],
    ids=["tools-system-messages", "no-tools", "no-system", "one-message", "messages-only"],
)
async def test_a_longer_cache_lifetime_never_follows_a_shorter_one(
    system: str, tools: tuple[ToolSpec, ...], messages: tuple[Any, ...],
) -> None:
    # Anthropic rejects a 1h breakpoint evaluated after a 5m one; tools, system, messages is its evaluation order.
    _raw, body = await _wire(_request(system=system, tools=tools, messages=messages))

    lifetimes = [LIFETIME_S[cache_control.get("ttl")] for _, cache_control in _breakpoints(body)]
    assert lifetimes == sorted(lifetimes, reverse=True)
    assert len(lifetimes) <= 4
    assert all(
        where.startswith(("tools.", "system.")) == (cache_control == HOUR)
        for where, cache_control in _breakpoints(body)
    )


@pytest.mark.parametrize("rejection", [ANTHROPIC_REJECTION, GATEWAY_REJECTION], ids=["endpoint", "model-hub"])
@pytest.mark.parametrize("retry_accepted", [True, False], ids=["ttl-rejected", "cache-control-rejected"])
async def test_a_rejected_ttl_is_retried_once_without_it_and_remembered_for_the_route(
    rejection: dict[str, Any], retry_accepted: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(wire: httpx.Request) -> httpx.Response:
        body = json.loads(wire.content)
        sent.append(body)
        if _carries_ttl(body) or not retry_accepted:
            return httpx.Response(400, json=rejection)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SUCCESS)

    caplog.set_level(logging.WARNING, logger="core.agent_core.ai.anthropic")
    # Two Sources behind one gateway URL, as every Vibey hop is.
    gateway = f"https://{uuid.uuid4().hex}.test/vibey/v1"
    request = _request(base_url=gateway, source_id="src_relayaaaa01")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runs = [
            await _run(client, request),
            # A later run builds its own adapter; the route keeps the default for the process.
            await _run(client, request),
            # The memory is the route's: another model on the same endpoint still asks for the hour,
            await _run(client, _request(model_id="claude-sonnet-5", base_url=gateway, source_id="src_relayaaaa01")),
            # and so does another Source of the same model behind the same gateway URL.
            await _run(client, _request(base_url=gateway, source_id="src_directbbb02")),
        ]

    rejected, retried, remembered, other_rejected, other_retried, source_rejected, source_retried = sent
    assert _breakpoints(rejected)[:2] == [("tools.1", HOUR), ("system.0", HOUR)]
    # Exactly one retry: the same request with the default lifetime.
    assert retried == _without_ttl(rejected)
    assert not _carries_ttl(retried)
    assert not _carries_ttl(remembered)
    assert _carries_ttl(other_rejected)
    assert other_retried == _without_ttl(other_rejected)
    assert _carries_ttl(source_rejected)
    assert source_retried == _without_ttl(source_rejected)
    for events in runs:
        # One terminal per run: a rejected attempt never reaches the consumer.
        terminals = [event for event in events if isinstance(event, (Done, ProviderError))]
        assert [type(event) for event in terminals] == [Done if retry_accepted else ProviderError]
        assert retry_accepted or terminals[0].status == 400
    # Logged once per route, not once per request.
    assert len([record for record in caplog.records if "prompt-cache TTL" in record.getMessage()]) == 3


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        # A Claude route's request rejected for something else.
        ("anthropic", "messages.2: tool_result block(s) provided when previous message has no tool_use"),
        # A rejection naming the field on a route that never sent it.
        ("custom", TTL_REJECTED),
    ],
    ids=["unrelated-rejection", "no-ttl-sent"],
)
async def test_a_rejection_the_ttl_did_not_cause_is_surfaced_without_a_retry(provider: str, message: str) -> None:
    sent: list[dict[str, Any]] = []

    def handler(wire: httpx.Request) -> httpx.Response:
        sent.append(json.loads(wire.content))
        if len(sent) == 1:
            error = {"type": "invalid_request_error", "message": message}
            return httpx.Response(400, json={"type": "error", "error": error})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SUCCESS)

    request = _request(provider)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _run(client, request)
        assert len(sent) == 1
        await _run(client, request)

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "invalid_request"
    # The route was not downgraded: its next request asks for the hour again.
    assert _carries_ttl(sent[1]) == (provider == "anthropic")
