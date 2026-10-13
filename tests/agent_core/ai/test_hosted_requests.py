"""Hosted-tool request building (``vibey-hosted-tools.md`` §3.2, §3.5, §4.1), read on the wire.

``ModelRequest.tools`` is the one owner of what a request declares. A hosted tool's canonical spec is sent as the
overlay's declaration, verbatim and in its place, on a hop whose route for it is native, with the route's Responses
``include`` values, and that request is pinned to the hop's Source; anywhere else the spec is an ordinary function.
``build_wire_request`` is the probe's entry: it must be exactly what the adapter sends. Scenario MH-HOSTED-VIBEY-001.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.agent_core.ai._common import PIN_SOURCE_HEADER
from core.agent_core.ai.provider import (
    Done,
    HostedExecutor,
    HostedToolRoute,
    ModelEndpoint,
    ModelRequest,
)
from core.agent_core.ai.registry import build_wire_request, create_adapter
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import TextBlock, UserMessage
from core.agent_core.tools.base import ToolSpec
from core.agent_core.tools.hosted import hosted_tool_spec

SOURCE = "src_hostedprobe1"
EXECUTOR = HostedExecutor("src_xaiexecutor1", "grok-4.6")
READ = ToolSpec("read", "Read a file.", {"type": "object", "properties": {"path": {"type": "string"}}})
BASH = ToolSpec("bash", "Run a command.", {"type": "object", "properties": {"command": {"type": "string"}}})
WEB = hosted_tool_spec("web_search")
X = hosted_tool_spec("x_search")
# The §4.1 declarations, as HopResolution carries them from the overlay.
ANTHROPIC_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
OPENAI_SEARCH = {"type": "web_search"}
X_SEARCH = {"type": "x_search"}
SOURCES_INCLUDE = "web_search_call.action.sources"
PROVIDER = {"anthropic": "anthropic", "openai_responses": "openai", "openai_chat": "openai"}
STREAMS = {
    "anthropic": (
        'data: {"type":"message_start","message":{"usage":{}}}\n\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    ),
    "openai_responses": (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":0,"delta":"ok"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":null}}\n\n'
    ),
    "openai_chat": (
        'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    ),
}


def _native(declaration: dict[str, Any], include: tuple[str, ...] = ()) -> HostedToolRoute:
    return HostedToolRoute("native", EXECUTOR, False, declaration=declaration, include=include)


PROXY = HostedToolRoute("proxy", EXECUTOR, False)


def _request(
    protocol: str,
    hosted: dict[str, HostedToolRoute],
    *,
    tools: tuple[ToolSpec, ...] = (READ, BASH),
    effort: str | None = "high",
    tool_choice: str | None = None,
    source_id: str | None = SOURCE,
) -> ModelRequest:
    return ModelRequest(
        endpoint=ModelEndpoint(
            protocol,  # type: ignore[arg-type]
            "http://hub.invalid/vibey/v1",
            "claude-opus-5-5" if protocol == "anthropic" else "grok-4.6",
            "hub-token",
            request_headers={"x-avibe-turn": "turn-1"},
            provider=PROVIDER[protocol],
            source_id=source_id,
            hosted_tools=hosted,
        ),
        system="You are a coding agent.",
        messages=(UserMessage((TextBlock("search for 'Avibe local-first agent OS'"),)),),
        tools=tools,
        max_tokens=256,
        reasoning_effort=effort,
        tool_choice=tool_choice,
    )


def _function_names(protocol: str, entries: list[dict[str, Any]]) -> list[str]:
    if protocol == "openai_chat":
        return [entry["function"]["name"] for entry in entries]
    return [entry["name"] for entry in entries]


@pytest.mark.parametrize(
    ("protocol", "hosted", "tools", "declared", "include"),
    [
        ("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, (WEB, READ, BASH), [ANTHROPIC_SEARCH], None),
        (
            "openai_responses",
            {"web_search": _native(OPENAI_SEARCH, (SOURCES_INCLUDE,))},
            (WEB, READ, BASH),
            [OPENAI_SEARCH],
            ["reasoning.encrypted_content", SOURCES_INCLUDE],
        ),
        # xAI: both search tools native, neither needing an include.
        (
            "openai_responses",
            {"web_search": _native(OPENAI_SEARCH), "x_search": _native(X_SEARCH)},
            (WEB, X, READ, BASH),
            [OPENAI_SEARCH, X_SEARCH],
            ["reasoning.encrypted_content"],
        ),
    ],
)
async def test_a_hosted_spec_on_a_native_route_is_sent_as_the_declaration_in_its_place_and_pinned(
    protocol: str,
    hosted: dict[str, HostedToolRoute],
    tools: tuple[ToolSpec, ...],
    declared: list[dict[str, Any]],
    include: list[str] | None,
) -> None:
    wire = await build_wire_request(_request(protocol, hosted, tools=tools), gateway=True)

    sent = wire.body["tools"]
    assert sent[: len(declared)] == declared
    assert _function_names(protocol, sent[len(declared) :]) == ["read", "bash"]
    assert wire.body.get("include") == include
    assert wire.headers[PIN_SOURCE_HEADER] == SOURCE
    if protocol == "anthropic":
        # The tools breakpoint stays on the last function tool; a server tool never carries it.
        assert [index for index, tool in enumerate(sent) if "cache_control" in tool] == [len(sent) - 1]


@pytest.mark.parametrize("protocol", ["anthropic", "openai_responses", "openai_chat"])
@pytest.mark.parametrize(
    ("hosted", "tools", "functions"),
    [
        ({}, (READ, BASH), ["read", "bash"]),
        # A proxied hosted tool is its canonical function.
        ({"web_search": PROXY}, (WEB, READ, BASH), ["web_search", "read", "bash"]),
        # The endpoint alone declares nothing: a native route for a tool the request does not carry sends nothing.
        ({"web_search": _native(OPENAI_SEARCH, (SOURCES_INCLUDE,))}, (READ, BASH), ["read", "bash"]),
    ],
    ids=["no-route", "proxy-route", "native-route-undeclared"],
)
async def test_without_a_declared_native_tool_every_tool_is_a_function_and_the_request_unpinned(
    protocol: str, hosted: dict[str, HostedToolRoute], tools: tuple[ToolSpec, ...], functions: list[str]
) -> None:
    wire = await build_wire_request(_request(protocol, hosted, tools=tools), gateway=True)

    assert _function_names(protocol, wire.body["tools"]) == functions
    assert PIN_SOURCE_HEADER not in wire.headers
    if protocol == "openai_responses":
        assert wire.body["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize(
    ("protocol", "hosted", "tools", "tool_choice"),
    [
        ("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, (WEB, READ, BASH), None),
        ("openai_responses", {"web_search": _native(OPENAI_SEARCH, (SOURCES_INCLUDE,))}, (WEB, READ, BASH), None),
        ("openai_chat", {"web_search": PROXY}, (WEB, READ, BASH), "read"),
        # Call probes: the hosted tool alone, forced on Responses and asked for in the message on Anthropic.
        ("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, (WEB,), None),
        ("openai_responses", {"x_search": _native(X_SEARCH)}, (X,), "x_search"),
    ],
)
async def test_the_probe_entry_builds_exactly_the_request_the_adapter_sends(
    protocol: str, hosted: dict[str, HostedToolRoute], tools: tuple[ToolSpec, ...], tool_choice: str | None
) -> None:
    request = _request(protocol, hosted, tools=tools, tool_choice=tool_choice)
    sent: list[httpx.Request] = []

    def gateway(wire: httpx.Request) -> httpx.Response:
        sent.append(wire)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=STREAMS[protocol])

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        adapter = create_adapter(protocol, client=client, gateway=True)  # type: ignore[arg-type]
        events = [event async for event in adapter.stream(request, CancelToken())]
    built = await build_wire_request(request, gateway=True)

    assert isinstance(events[-1], Done)
    (wire,) = sent
    assert str(wire.url) == built.url
    assert {key: wire.headers[key] for key in built.headers} == built.headers
    assert json.loads(wire.content) == built.body


@pytest.mark.parametrize(
    ("protocol", "hosted", "tools", "tool_choice", "expected"),
    [
        # OpenAI's hosted choice types have no web_search or x_search: `required` over the tool alone forces it.
        ("openai_responses", {"web_search": _native(OPENAI_SEARCH)}, (WEB,), "web_search", "required"),
        ("openai_responses", {}, (READ, BASH), "read", {"type": "function", "name": "read"}),
        ("openai_chat", {}, (READ, BASH), "bash", {"type": "function", "function": {"name": "bash"}}),
    ],
)
async def test_tool_choice_forces_the_named_tool_in_each_protocols_form(
    protocol: str, hosted: dict[str, HostedToolRoute], tools: tuple[ToolSpec, ...], tool_choice: str, expected: Any
) -> None:
    wire = await build_wire_request(_request(protocol, hosted, tools=tools, tool_choice=tool_choice), gateway=True)

    assert wire.body["tool_choice"] == expected


@pytest.mark.parametrize(
    ("request_", "refusal"),
    [
        # `required` would let the model pick a function instead of the hosted tool.
        (
            _request(
                "openai_responses", {"web_search": _native(OPENAI_SEARCH)}, tools=(WEB, READ), tool_choice="web_search"
            ),
            "only tool",
        ),
        (_request("openai_chat", {}, tool_choice="web_search"), "does not declare"),
        # Current Claude models and extended thinking refuse a forced choice: a probe would test that refusal.
        (
            _request("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, tools=(WEB,), tool_choice="web_search"),
            "never",
        ),
        # A native declaration is never sent unpinned: unpinned, the gateway could fail it over to an unverified hop.
        (
            _request("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, tools=(WEB, READ), source_id=None),
            "pinned",
        ),
    ],
    ids=["forced-hosted-beside-functions", "forced-undeclared", "anthropic-forced", "native-without-source"],
)
async def test_a_request_that_cannot_be_sent_as_specified_is_refused(request_: ModelRequest, refusal: str) -> None:
    with pytest.raises(ValueError, match=refusal):
        await build_wire_request(request_, gateway=True)
