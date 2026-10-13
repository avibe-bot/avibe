"""Hosted-tool request building (``vibey-hosted-tools.md`` §3.2, §3.5, §4.1), read on the wire.

A native route puts the overlay's declaration into the request verbatim, ahead of the function tools, with its
Responses ``include`` values, and pins the request to the hop's Source. ``build_wire_request`` is the probe's entry:
it must be exactly what the adapter sends. Scenario MH-HOSTED-VIBEY-001.
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

SOURCE = "src_hostedprobe1"
EXECUTOR = HostedExecutor("src_xaiexecutor1", "grok-4.6")
READ = ToolSpec("read", "Read a file.", {"type": "object", "properties": {"path": {"type": "string"}}})
BASH = ToolSpec("bash", "Run a command.", {"type": "object", "properties": {"command": {"type": "string"}}})
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
    ("protocol", "hosted", "declared", "include"),
    [
        ("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, [ANTHROPIC_SEARCH], None),
        (
            "openai_responses",
            {"web_search": _native(OPENAI_SEARCH, (SOURCES_INCLUDE,))},
            [OPENAI_SEARCH],
            ["reasoning.encrypted_content", SOURCES_INCLUDE],
        ),
        # xAI: both search tools native, neither needing an include; a proxied tool adds nothing yet.
        (
            "openai_responses",
            {"web_search": _native(OPENAI_SEARCH), "x_search": _native(X_SEARCH), "image_generation": PROXY},
            [OPENAI_SEARCH, X_SEARCH],
            ["reasoning.encrypted_content"],
        ),
    ],
)
async def test_a_native_route_declares_its_tool_verbatim_first_and_pins_the_request(
    protocol: str, hosted: dict[str, HostedToolRoute], declared: list[dict[str, Any]], include: list[str] | None
) -> None:
    wire = await build_wire_request(_request(protocol, hosted), gateway=True)

    tools = wire.body["tools"]
    hosted_entries, functions = tools[: len(declared)], tools[len(declared) :]
    assert hosted_entries == declared
    assert _function_names(protocol, functions) == ["read", "bash"]
    assert wire.body.get("include") == include
    assert wire.headers[PIN_SOURCE_HEADER] == SOURCE
    if protocol == "anthropic":
        # The tools breakpoint stays on the last function tool; a server tool never carries it.
        assert [index for index, tool in enumerate(tools) if "cache_control" in tool] == [len(tools) - 1]


@pytest.mark.parametrize("protocol", ["anthropic", "openai_responses", "openai_chat"])
@pytest.mark.parametrize("hosted", [{}, {"web_search": PROXY, "x_search": PROXY}], ids=["none", "proxy"])
async def test_without_a_native_route_the_request_is_unchanged_and_unpinned(
    protocol: str, hosted: dict[str, HostedToolRoute]
) -> None:
    wire = await build_wire_request(_request(protocol, hosted), gateway=True)

    assert _function_names(protocol, wire.body["tools"]) == ["read", "bash"]
    assert PIN_SOURCE_HEADER not in wire.headers
    if protocol == "openai_responses":
        assert wire.body["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize(
    ("protocol", "hosted", "tool_choice"),
    [
        ("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, None),
        ("openai_responses", {"web_search": _native(OPENAI_SEARCH, (SOURCES_INCLUDE,))}, None),
        ("openai_chat", {"web_search": PROXY}, "read"),
        # A call probe: the hosted tool alone, forced.
        ("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, "web_search"),
        ("openai_responses", {"x_search": _native(X_SEARCH)}, "x_search"),
    ],
)
async def test_the_probe_entry_builds_exactly_the_request_the_adapter_sends(
    protocol: str, hosted: dict[str, HostedToolRoute], tool_choice: str | None
) -> None:
    request = _request(protocol, hosted, tool_choice=tool_choice, tools=() if tool_choice in hosted else (READ, BASH))
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
        ("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, (), "web_search", {"type": "tool", "name": "web_search"}),
        ("anthropic", {}, (READ,), "read", {"type": "tool", "name": "read"}),
        # OpenAI's hosted choice types have no web_search or x_search: `required` over the tool alone forces it.
        ("openai_responses", {"web_search": _native(OPENAI_SEARCH)}, (), "web_search", "required"),
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
            _request("openai_responses", {"web_search": _native(OPENAI_SEARCH)}, tool_choice="web_search"),
            "only tool",
        ),
        (_request("anthropic", {}, tool_choice="web_search"), "does not declare"),
        # A native declaration is never sent unpinned: unpinned, the gateway could fail it over to an unverified hop.
        (_request("anthropic", {"web_search": _native(ANTHROPIC_SEARCH)}, source_id=None), "pinned"),
    ],
    ids=["forced-hosted-beside-functions", "forced-undeclared", "native-without-source"],
)
async def test_a_request_that_cannot_be_sent_as_specified_is_refused(request_: ModelRequest, refusal: str) -> None:
    with pytest.raises(ValueError, match=refusal):
        await build_wire_request(request_, gateway=True)
