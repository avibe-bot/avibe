from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

import core.agent_core.ai._common as common_module
import core.agent_core.ai.sse as sse_module
from core.agent_core.ai.anthropic import AnthropicAdapter, build_messages_payload
from core.agent_core.ai._common import read_response_body
from core.agent_core.ai.openai_chat import OpenAIChatAdapter, build_chat_payload
from core.agent_core.ai.openai_responses import OpenAIResponsesAdapter, build_responses_payload
from core.agent_core.ai.provider import (
    BlockEnd,
    Done,
    MediaLoader,
    ModelEndpoint,
    ModelRequest,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
)
from core.agent_core.ai.registry import ADAPTERS, adapter_class
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
    Usage,
)
from core.agent_core.tools.base import ToolSpec


def _request(
    protocol: str,
    *,
    messages: tuple[Any, ...] = (),
    supports_images: bool = False,
    tools: tuple[ToolSpec, ...] = (),
    model_id: str = "model-x",
    base_url: str = "https://model.test/v1",
    reasoning_effort: str | None = "high",
) -> ModelRequest:
    return ModelRequest(
        endpoint=ModelEndpoint(
            protocol,
            base_url,
            model_id,
            "token",
            provider={"anthropic": "anthropic", "openai_chat": "openai", "openai_responses": "openai"}[protocol],
        ),
        system="system",
        messages=messages or (UserMessage((TextBlock(text="hello"),)),),
        tools=tools,
        max_tokens=128,
        reasoning_effort=reasoning_effort,
        supports_images=supports_images,
    )


def test_registry_exposes_only_v1_protocols() -> None:
    assert set(ADAPTERS) == {"anthropic", "openai_chat", "openai_responses"}
    with pytest.raises(ValueError, match="unsupported provider protocol: google"):
        adapter_class("google")


async def _events(adapter: Any, request: ModelRequest, cancel: CancelToken | None = None) -> list[Any]:
    return [event async for event in adapter.stream(request, cancel or CancelToken())]


@pytest.mark.asyncio
async def test_anthropic_streams_thinking_tool_arguments_and_usage() -> None:
    body = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\r\n\r\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}\r\n\r\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reason"}}\r\n\r\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig"}}\r\n\r\n'
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"read"}}\r\n\r\n'
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"x\\"}"}}\r\n\r\n'
        'data: {"type":"content_block_stop","index":0}\r\n\r\n'
        'data: {"type":"content_block_stop","index":1}\r\n\r\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}\r\n\r\n'
        'data: {"type":"message_stop"}\r\n\r\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "tool_use"
    assert final.message.usage == Usage(input_tokens=5, output_tokens=3)
    assert final.message.content == (
        ThinkingBlock("reason", "sig"),
        ToolCallBlock("toolu_1", "read", {"path": "x"}, native_id="toolu_1"),
    )
    assert [event.index for event in events if isinstance(event, BlockEnd)] == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body", "expected_path"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            'data: {"type":"message_stop"}\n\n',
            "/v1/messages",
        ),
        (
            OpenAIChatAdapter,
            "openai_chat",
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n",
            "/v1/chat/completions",
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
            "/v1/responses",
        ),
    ],
)
async def test_adapters_join_protocol_path_before_existing_query(
    adapter_type: Any,
    protocol: str,
    body: str,
    expected_path: str,
) -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(
            adapter_type(client),
            _request(protocol, base_url="https://model.test/v1?tenant=blue"),
        )

    assert isinstance(events[-1], Done)
    assert seen[0].path == expected_path
    assert seen[0].query == b"tenant=blue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body"),
    [
        (
            OpenAIChatAdapter,
            "openai_chat",
            (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                '"function":{"name":"read","arguments":"{\\"path\\":\\"x"}}]}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        ),
        (
            AnthropicAdapter,
            "anthropic",
            (
                'data: {"type":"content_block_start","index":0,'
                '"content_block":{"type":"tool_use","id":"toolu_1","name":"read"}}\n\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"x"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            (
                'data: {"type":"response.output_item.added","output_index":0,'
                '"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
                'data: {"type":"response.function_call_arguments.delta","output_index":0,'
                '"delta":"{\\"path\\":\\"x"}\n\n'
                'data: {"type":"response.incomplete","response":{"status":"incomplete",'
                '"incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
            ),
        ),
    ],
)
async def test_non_tool_stop_preserves_truncated_tool_calls_for_settlement(
    adapter_type: Any,
    protocol: str,
    body: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    terminal = events[-1]
    assert isinstance(terminal, Done)
    assert terminal.message.stop_reason == "length"
    assert [call.arguments for call in terminal.message.tool_calls] == [{}]


@pytest.mark.asyncio
async def test_anthropic_empty_initial_tool_input_does_not_prefix_streamed_json() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"read","input":{}}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"x\\"}"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_anthropic_nonempty_initial_tool_input_is_preserved_without_deltas() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"read","input":{"path":"x"}}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_anthropic_replays_redacted_thinking_payload_verbatim() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"redacted_thinking","data":"opaque-redacted"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    history = (
        AssistantMessage(
            content=(ThinkingBlock("", "opaque-redacted", redacted=True),),
            origin=Origin("anthropic", "anthropic", "model-x"),
            stop_reason="stop",
        ),
        UserMessage((TextBlock(text="continue"),)),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(
            AnthropicAdapter(client),
            _request("anthropic", messages=history),
        )

    assistant = captured["messages"][0]
    assert assistant["content"] == [{"type": "redacted_thinking", "data": "opaque-redacted"}]
    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.content == (ThinkingBlock("", "opaque-redacted", redacted=True),)


@pytest.mark.asyncio
async def test_anthropic_ignores_unknown_content_block_types_like_pi() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"future_block"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == ()


@pytest.mark.asyncio
async def test_anthropic_mid_output_fallback_is_terminal_like_pi() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"before"}}\n\n'
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"fallback"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "unknown"
    assert events[-1].partial is not None


@pytest.mark.asyncio
async def test_anthropic_ignores_unknown_top_level_events_like_pi() -> None:
    body = (
        'data: {"type":"future_event","payload":{"new":"field"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)


@pytest.mark.asyncio
async def test_anthropic_ignores_unknown_sse_event_before_parsing_like_pi() -> None:
    body = (
        'event: future_event\n'
        'data: not-json\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)


@pytest.mark.asyncio
async def test_anthropic_named_error_frame_uses_nested_error_code() -> None:
    body = (
        'event: error\n'
        'data: {"type":"error","error":{"type":"api_error","message":"busy"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "server"
    assert error.retryable is True


@pytest.mark.asyncio
async def test_anthropic_content_block_start_preserves_initial_content() -> None:
    body = (
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":"initial"}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"thinking","thinking":"plan","signature":"sig"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock("initial"), ThinkingBlock("plan", "sig"))


@pytest.mark.asyncio
async def test_anthropic_malformed_delta_is_terminal_invalid_request() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":null}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block", "delta"),
    [
        ("text", {"type": "text_delta", "text": 7}),
        ("thinking", {"type": "thinking_delta", "thinking": []}),
        ("thinking", {"type": "signature_delta", "signature": {}}),
        ("tool", {"type": "input_json_delta", "partial_json": 7}),
    ],
)
async def test_anthropic_known_delta_fields_reject_wrong_types(
    block: str,
    delta: dict[str, Any],
) -> None:
    content_block = {
        "text": {"type": "text"},
        "thinking": {"type": "thinking"},
        "tool": {"type": "tool_use", "id": "toolu_1", "name": "read"},
    }[block]
    body = (
        f'data: {json.dumps({"type": "content_block_start", "index": 0, "content_block": content_block})}\n\n'
        f'data: {json.dumps({"type": "content_block_delta", "index": 0, "delta": delta})}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"


@pytest.mark.asyncio
async def test_anthropic_pre_output_fallback_is_ignored_like_pi() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"fallback"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == ()


@pytest.mark.asyncio
async def test_anthropic_wrong_kind_delta_is_ignored_like_pi() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"ignored"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock(""),)


@pytest.mark.asyncio
async def test_anthropic_malformed_message_delta_usage_is_terminal_invalid_request() -> None:
    body = (
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":[]}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"


@pytest.mark.asyncio
async def test_anthropic_ignores_unknown_delta_variants_like_pi() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"future_delta","value":"ignored"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock(""),)


@pytest.mark.asyncio
async def test_anthropic_empty_block_end_does_not_count_as_streamed_output() -> None:
    body = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert not any(isinstance(event, BlockEnd) for event in events)
    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "overloaded"
    assert error.retryable is True


@pytest.mark.asyncio
async def test_responses_sets_client_side_state_rules_and_keeps_reasoning_item() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.reasoning_text.delta","output_index":0,"delta":"plan"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_1","encrypted_content":"enc"}}\n\n'
        'data: {"type":"response.output_item.added","output_index":1,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":1,"delta":"done"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"reasoning","id":"rs_1","encrypted_content":"enc"},{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"done"}]}],"usage":{"input_tokens":2,"output_tokens":4,"output_tokens_details":{"reasoning_tokens":1}}}}\n\n'
    )
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert captured["store"] is False
    assert "previous_response_id" not in captured
    assert captured["include"] == ["reasoning.encrypted_content"]
    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.content[0] == ThinkingBlock(
        "plan",
        json.dumps(
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc"},
            separators=(",", ":"),
        ),
    )
    assert final.message.content[1] == TextBlock("done")
    assert final.message.usage == Usage(input_tokens=2, output_tokens=4, reasoning_tokens=1)


@pytest.mark.asyncio
async def test_responses_summary_done_adds_separator() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.reasoning_summary_part.added","output_index":0}\n\n'
        'data: {"type":"response.reasoning_summary_text.delta","output_index":0,"delta":"plan"}\n\n'
        'data: {"type":"response.reasoning_summary_part.done","output_index":0}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (ThinkingBlock("plan\n\n", None),)


@pytest.mark.asyncio
async def test_responses_summary_done_rejects_non_integer_output_index() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.reasoning_summary_part.done","output_index":"0"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"


@pytest.mark.asyncio
async def test_responses_known_lifecycle_events_are_accounted_for() -> None:
    body = (
        'data: {"type":"response.created","response":{"id":"r","status":"in_progress"}}\n\n'
        'data: {"type":"response.in_progress","response":{"status":"in_progress"}}\n\n'
        'data: {"type":"response.queued","response":{"status":"queued"}}\n\n'
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.reasoning_summary_part.added","output_index":0}\n\n'
        'data: {"type":"response.reasoning_summary_text.delta","output_index":0,"delta":"plan"}\n\n'
        'data: {"type":"response.reasoning_summary_part.done","output_index":0}\n\n'
        'data: {"type":"response.content_part.added","output_index":0}\n\n'
        'data: {"type":"response.content_part.done","output_index":0}\n\n'
        'data: {"type":"response.output_text.done","output_index":0}\n\n'
        'data: {"type":"response.reasoning_text.done","output_index":0}\n\n'
        'data: {"type":"response.reasoning_summary_text.done","output_index":0}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (ThinkingBlock("plan\n\n"),)


@pytest.mark.asyncio
async def test_responses_custom_tool_events_are_ignored_without_custom_tool_blocks() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"custom_tool_call","id":"ct_1","call_id":"call_1","name":"lookup"}}\n\n'
        'data: {"type":"response.custom_tool_call_input.delta","output_index":0,"delta":"{\\"query\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.custom_tool_call_input.done","output_index":0,"input":"{\\"query\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"custom_tool_call","id":"ct_1","call_id":"call_1","name":"lookup","input":"{\\"query\\":\\"x\\"}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == ()


@pytest.mark.asyncio
async def test_responses_top_level_error_frame_is_classified() -> None:
    body = 'data: {"type":"error","error":{"code":"server_error","message":"failed"}}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "server"
    assert events[-1].retryable is True


@pytest.mark.asyncio
async def test_chat_accepts_integer_error_code_from_openai_compatible_servers() -> None:
    body = (
        'data: {"error":{"type":"InternalServerError","code":500,'
        '"message":"Internal server error"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "server"
    assert error.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_kind"),
    [
        (
            'data: {"type":"error","error":{"type":"context_length_exceeded"}}\n\n',
            "overflow",
        ),
        (
            'data: {"type":"response.failed","response":{"status":"failed","error":{"type":"context_length_exceeded"}}}\n\n',
            "overflow",
        ),
        (
            'data: {"type":"response.completed","response":{"status":"failed","error":{"type":"context_length_exceeded"}}}\n\n',
            "overflow",
        ),
    ],
)
async def test_responses_error_classification_uses_the_complete_envelope(
    body: str,
    expected_kind: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == expected_kind


@pytest.mark.asyncio
async def test_responses_error_message_excludes_response_request_metadata() -> None:
    body = (
        'data: {"type":"response.failed","response":{"status":"failed",'
        '"error":{"code":"server_error","message":"upstream failed"},'
        '"instructions":"secret system prompt","tools":[{"name":"secret"}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "server"
    assert error.message == "upstream failed"
    assert "secret system prompt" not in error.message


@pytest.mark.asyncio
async def test_responses_failed_without_error_details_has_safe_fixed_message() -> None:
    body = (
        'data: {"type":"response.failed","response":{"status":"failed","error":null,'
        '"instructions":"If you hit a rate limit, wait.","tools":[{"name":"secret"}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "unknown"
    assert error.retryable is False
    assert error.message == "OpenAI Responses response failed"


@pytest.mark.asyncio
async def test_responses_completed_without_status_follows_pi_default() -> None:
    body = 'data: {"type":"response.completed","response":{"output":[]}}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "stop"


@pytest.mark.asyncio
async def test_responses_completed_with_null_output_follows_pi_default() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":0,"delta":"hi"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":null}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "stop"
    assert final.message.content == (TextBlock("hi"),)


@pytest.mark.asyncio
async def test_responses_ignores_unread_fields_on_non_function_output_items() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"tool_search_call","arguments":{}}}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,'
        '"item":{"type":"tool_search_call","arguments":{}}}\n\n'
        'data: {"type":"response.output_item.added","output_index":1,'
        '"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":1,"delta":"hi"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":null}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.content == (TextBlock("hi"),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body", "expected_kind"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","code":529,"message":"Overloaded"}}\n\n',
            "overloaded",
        ),
    ],
)
async def test_provider_error_ignores_unconsumed_code_types(
    adapter_type: Any,
    protocol: str,
    body: str,
    expected_kind: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == expected_kind
    assert error.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    [("content_filter", "safety"), ("unknown_reason", "error")],
)
async def test_responses_incomplete_reasons_have_explicit_outcomes(reason: str, expected: str) -> None:
    body = (
        f'data: {{"type":"response.incomplete","response":{{"status":"incomplete","incomplete_details":{{"reason":"{reason}"}}}}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    terminal = events[-1]
    if expected == "safety":
        assert isinstance(terminal, Done)
        assert terminal.message.stop_reason == "safety"
    else:
        assert isinstance(terminal, ProviderError)
        assert terminal.kind == "unknown"


@pytest.mark.asyncio
async def test_responses_incomplete_without_status_is_not_success() -> None:
    body = (
        'data: {"type":"response.incomplete","response":'
        '{"incomplete_details":{"reason":"unknown_reason"}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    terminal = events[-1]
    assert isinstance(terminal, ProviderError)
    assert terminal.kind == "unknown"
    assert terminal.partial is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            (
                'event: message_start\n'
                'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n'
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":0,'
                '"content_block":{"type":"thinking","thinking":""}}\n\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"thinking_delta","thinking":"ab"}}\n\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"thinking_delta","thinking":"cd"}}\n\n'
            ),
        ),
        (
            OpenAIChatAdapter,
            "openai_chat",
            (
                'data: {"choices":[{"delta":{"content":"ab"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"cd"}}]}\n\n'
            ),
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            (
                'data: {"type":"response.output_item.added","output_index":0,'
                '"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
                'data: {"type":"response.function_call_arguments.delta",'
                '"output_index":0,"delta":"ab"}\n\n'
                'data: {"type":"response.function_call_arguments.delta",'
                '"output_index":0,"delta":"cd"}\n\n'
            ),
        ),
    ],
)
async def test_cumulative_output_budget_covers_text_thinking_and_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: Any,
    protocol: str,
    body: str,
) -> None:
    monkeypatch.setattr(common_module, "MAX_CUMULATIVE_OUTPUT_CHARS", 3)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.retryable is False
    assert "output budget" in error.message.lower()
    assert error.partial is not None


@pytest.mark.asyncio
async def test_many_small_chat_deltas_preserve_the_complete_text() -> None:
    count = 20_000
    body = (
        'data: {"choices":[{"delta":{"content":"x"}}]}\n\n' * count
        + 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        + "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.content == (TextBlock(text="x" * count),)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_responses_failed_or_cancelled_status_is_terminal_provider_error(status: str) -> None:
    body = f'data: {{"type":"response.completed","response":{{"status":"{status}"}}}}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "unknown"


@pytest.mark.asyncio
async def test_responses_non_array_terminal_output_is_malformed() -> None:
    body = (
        'data: {"type":"response.completed","response":{"status":"completed","output":{}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert "output" in error.message


@pytest.mark.asyncio
async def test_responses_terminal_output_item_type_must_be_a_string() -> None:
    body = (
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":7}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert "type" in error.message


def test_responses_replay_preserves_output_item_order() -> None:
    history = (
        AssistantMessage(
            content=(
                ThinkingBlock("plan", json.dumps({"id": "rs_1", "encrypted_content": "enc"}, separators=(",", ":"))),
                TextBlock(text="answer"),
            ),
            origin=Origin("openai", "openai_responses", "model-x"),
            stop_reason="stop",
        ),
    )

    payload = build_responses_payload(
        _request("openai_responses", messages=history),
        history,
    )

    assert payload["input"][0]["type"] == "reasoning"
    assert payload["input"][1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_responses_uses_item_id_for_parallel_argument_deltas_and_incomplete_stop() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_item_a","call_id":"call_a","name":"read"}}\n\n'
        'data: {"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","id":"fc_item_b","call_id":"call_b","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_item_b","delta":"{\\"path\\":\\"b\\"}"}\n\n'
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_item_a","delta":"{\\"path\\":\\"a\\"}"}\n\n'
        'data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "length"
    assert [call.arguments for call in final.message.tool_calls] == [{"path": "a"}, {"path": "b"}]


@pytest.mark.asyncio
async def test_responses_argument_done_emits_only_the_unseen_suffix() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"path\\":"}\n\n'
        'data: {"type":"response.function_call_arguments.done","output_index":0,"arguments":"{\\"path\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{\\"path\\":\\"x\\"}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert [event.arguments_delta for event in events if isinstance(event, ToolCallDelta)] == [
        '{"path":',
        '"x"}',
    ]
    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_responses_empty_output_item_arguments_keep_streamed_arguments() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"path\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":""}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_responses_reasoning_signature_survives_terminal_snapshot_without_encrypted_content() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_1","encrypted_content":"enc1"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"reasoning","id":"rs_1","summary":[]}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert json.loads(final.message.content[0].signature or "")["encrypted_content"] == "enc1"


@pytest.mark.asyncio
async def test_responses_late_tool_argument_delta_after_close_is_ignored() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{\\"path\\":\\"x\\"}"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"LATE"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_responses_rejects_unfinished_tool_call_on_completed_response() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"path\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "invalid_request"
    assert "unfinished tool call" in events[-1].message


@pytest.mark.asyncio
async def test_responses_ignores_final_only_function_call_like_pi() -> None:
    body = (
        'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{}"}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.stop_reason == "stop"
    assert events[-1].message.tool_calls == ()


@pytest.mark.asyncio
async def test_responses_done_alias_follows_terminal_response_state() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.done","response":{"status":"completed","output":[{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"done"}]}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.stop_reason == "stop"


@pytest.mark.asyncio
async def test_responses_does_not_adopt_terminal_message_snapshot() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"hello "},{"type":"output_text","text":"world"}]}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock(""),)


@pytest.mark.asyncio
async def test_responses_refusal_is_not_normalized_to_stop() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.refusal.delta","delta":"cannot help"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "refusal"


@pytest.mark.asyncio
async def test_responses_orphan_refusal_delta_is_ignored_like_pi() -> None:
    body = (
        'data: {"type":"response.refusal.delta","output_index":3,"delta":"cannot help"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "stop"
    assert final.message.content == ()


@pytest.mark.asyncio
async def test_responses_refusal_does_not_skip_message_snapshot_merge() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.refusal.delta","output_index":0,"delta":"cannot"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"cannot help"}]}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert [event.delta for event in events if isinstance(event, TextDelta)] == [
        "cannot",
        " help",
    ]
    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock("cannot help"),)


@pytest.mark.asyncio
async def test_responses_terminal_refusal_output_sets_refusal_stop_reason() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"message","id":"msg_1","content":[{"type":"refusal","refusal":"cannot help"}]}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "refusal"


@pytest.mark.asyncio
async def test_responses_failed_server_code_is_retryable_before_streaming() -> None:
    body = (
        'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"server_error","message":"upstream failed"}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "server"
    assert error.retryable is True


@pytest.mark.asyncio
async def test_responses_failed_tool_start_is_not_replayed_in_partial() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"server_error","message":"upstream failed"}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.retryable is False
    assert error.partial is not None
    assert error.partial.tool_calls == ()


@pytest.mark.asyncio
async def test_responses_orphan_argument_events_are_ignored_like_pi() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","delta":"orphan"}\n\n'
        'data: {"type":"response.function_call_arguments.done","arguments":"{\\"orphan\\":true}"}\n\n'
        'data: {"type":"response.output_item.done","output_index":1,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_responses_output_item_done_can_create_a_function_call_slot() -> None:
    body = (
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{\\"path\\":\\"x\\"}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert any(isinstance(event, ToolCallStart) for event in events)
    assert [event.arguments_delta for event in events if isinstance(event, ToolCallDelta)] == [
        '{"path":"x"}'
    ]
    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_responses_missing_output_index_uses_one_shared_fallback_slot() -> None:
    body = (
        'data: {"type":"response.output_item.added","item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","delta":"{\\"path\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{\\"path\\":\\"x\\"}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert [event.arguments_delta for event in events if isinstance(event, ToolCallDelta)] == [
        '{"path":"x"}'
    ]
    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_responses_consecutive_missing_output_indexes_rebind_tool_slots() -> None:
    body = (
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_a","call_id":"call_a","name":"read"}}\n\n'
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_b","call_id":"call_b","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_a","delta":"{\\"path\\":\\"a\\"}"}\n\n'
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_b","delta":"{\\"path\\":\\"b\\"}"}\n\n'
        'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"fc_a","call_id":"call_a","name":"read","arguments":"{\\"path\\":\\"a\\"}"}}\n\n'
        'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"fc_b","call_id":"call_b","name":"read","arguments":"{\\"path\\":\\"b\\"}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert [call.arguments for call in final.message.tool_calls] == [
        {"path": "a"},
        {"path": "b"},
    ]


@pytest.mark.asyncio
async def test_responses_missing_tool_ids_reuse_the_open_fallback_slot() -> None:
    body = (
        'data: {"type":"response.output_item.added","item":{"type":"function_call","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","delta":"{\\"path\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read","arguments":"{\\"path\\":\\"x\\"}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    final = events[-1]
    assert len(starts) == 1
    assert isinstance(final, Done)
    assert final.message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_responses_non_tool_missing_output_index_keeps_pi_fallback() -> None:
    body = (
        'data: {"type":"response.output_item.added","item":{"type":"function_call","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","delta":"{\\"path\\":\\"x\\"}"}\n\n'
        'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read","arguments":"{\\"path\\":\\"x\\"}"}}\n\n'
        'data: {"type":"response.output_item.added","item":{"type":"message"}}\n\n'
        'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"hello"}]}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.content == (
        ToolCallBlock("call_1", "read", {"path": "x"}),
        TextBlock("hello"),
    )


@pytest.mark.asyncio
async def test_responses_empty_slots_close_before_late_deltas() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_item.added","output_index":1,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","content":[]}}\n\n'
        'data: {"type":"response.output_item.done","output_index":1,"item":{"type":"reasoning","id":"rs_1","summary":[]}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":0,"delta":"late text"}\n\n'
        'data: {"type":"response.reasoning_text.delta","output_index":1,"delta":"late thinking"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert not [event for event in events if isinstance(event, TextDelta)]
    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock(""), ThinkingBlock(""))


@pytest.mark.asyncio
async def test_responses_output_item_done_replaces_a_different_text_snapshot() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":0,"delta":"Hello world"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"Hello, world"}]}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock("Hello, world"),)


@pytest.mark.asyncio
async def test_responses_output_item_done_closes_slot_before_late_delta() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":0,"delta":"a"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"a"}]}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":0,"delta":"LATE"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert [event.delta for event in events if isinstance(event, TextDelta)] == ["a"]
    assert [event.index for event in events if isinstance(event, BlockEnd)] == [0]
    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock("a"),)


@pytest.mark.asyncio
async def test_chat_completions_reassembles_parallel_calls() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","function":{"name":"read","arguments":"{\\"path\\":\\"a\\"}"}}]},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_b","function":{"name":"read","arguments":"{\\"path\\":\\"b\\"}"}}]},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "tool_use"
    assert [call.arguments for call in final.message.tool_calls] == [{"path": "a"}, {"path": "b"}]


@pytest.mark.asyncio
async def test_chat_reuses_indexless_tool_slot_for_argument_only_delta() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_a","function":{"name":"read"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"path\\":\\"x\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.tool_calls[0].id == "call_a"
    assert final.message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            {"index": 1, "id": "call_a", "function": {"name": "read"}},
            {"id": "call_b", "function": {"name": "write", "arguments": '{"q":2}'}},
        ),
        (
            {"id": "call_a", "function": {"name": "read"}},
            {"index": 0, "id": "call_b", "function": {"name": "write", "arguments": '{"q":2}'}},
        ),
        (
            {"id": "call_a", "function": {"name": "read"}},
            {"id": "call_b", "function": {"name": "write", "arguments": '{"q":2}'}},
        ),
    ],
)
async def test_chat_indexed_and_indexless_tool_calls_do_not_collide(
    first: dict[str, Any],
    second: dict[str, Any],
) -> None:
    body = (
        f'data: {json.dumps({"choices": [{"delta": {"tool_calls": [first]}}]})}\n\n'
        f'data: {json.dumps({"choices": [{"delta": {"tool_calls": [second]}}]})}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    final = events[-1]
    assert isinstance(final, Done)
    assert [start.name for start in starts] == ["read", "write"]
    assert [call.name for call in final.message.tool_calls] == ["read", "write"]
    assert [call.arguments for call in final.message.tool_calls] == [{}, {"q": 2}]


@pytest.mark.asyncio
async def test_chat_uses_choice_usage_fallback() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"ok"},"usage":{"prompt_tokens":3,"completion_tokens":2},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.usage == Usage(input_tokens=3, output_tokens=2)


@pytest.mark.asyncio
async def test_chat_reuses_tool_id_received_before_function_name() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"provider-call"}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read","arguments":"{\\"path\\":\\"x\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].id == "provider-call"


@pytest.mark.asyncio
async def test_chat_late_tool_id_matches_tool_call_start_id() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"provider-call","function":{"arguments":"{\\"path\\":\\"x\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    start = next(event for event in events if isinstance(event, ToolCallStart))
    final = events[-1]
    assert isinstance(final, Done)
    assert start.id == final.message.tool_calls[0].id
    assert final.message.tool_calls[0].native_id == "provider-call"


@pytest.mark.asyncio
async def test_chat_malformed_tool_arguments_keep_text_and_usage_in_partial() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"before "}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read","arguments":"not-json"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.partial is not None
    assert error.partial.usage == Usage(input_tokens=3, output_tokens=2)
    assert error.partial.content == (TextBlock("before "),)


@pytest.mark.asyncio
async def test_chat_accumulates_reasoning_details_across_deltas() -> None:
    body = (
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text","text":"a"}]}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text","text":"b"}]},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (
        ThinkingBlock(
            "",
            json.dumps([{"type": "reasoning.text", "text": "ab"}], separators=(",", ":")),
        ),
    )


@pytest.mark.asyncio
async def test_chat_finish_reason_end_maps_to_stop() -> None:
    body = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"end"}]}\n\ndata: [DONE]\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.stop_reason == "stop"


@pytest.mark.asyncio
async def test_anthropic_abort_after_message_start_keeps_usage_in_partial() -> None:
    stream = _StalledStream(
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    cancel = CancelToken()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(_events(AnthropicAdapter(client), _request("anthropic"), cancel))
        await stream.started.wait()
        await asyncio.sleep(0.05)
        cancel.cancel("cancelled after usage")
        events = await asyncio.wait_for(task, timeout=1)

    assert isinstance(events[-1], ProviderError)
    assert events[-1].partial is not None
    assert events[-1].partial.content == ()
    assert events[-1].partial.usage == Usage(input_tokens=7)


@pytest.mark.asyncio
async def test_anthropic_null_delta_usage_does_not_erase_message_start_usage() -> None:
    body = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":null,"output_tokens":null}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.usage == Usage(input_tokens=7)


def test_chat_reasoning_models_use_completion_token_limit() -> None:
    payload = build_chat_payload(_request("openai_chat", model_id="gpt-5-mini"), ())

    assert payload["max_completion_tokens"] == 128
    assert "max_tokens" not in payload


@pytest.mark.parametrize(
    ("builder", "protocol", "field"),
    [
        (build_chat_payload, "openai_chat", "reasoning_effort"),
        (build_responses_payload, "openai_responses", "reasoning"),
    ],
)
def test_openai_payloads_forward_explicit_none_reasoning_effort(
    builder: Any,
    protocol: str,
    field: str,
) -> None:
    payload = builder(_request(protocol, reasoning_effort="none"), ())

    if field == "reasoning_effort":
        assert payload[field] == "none"
    else:
        assert payload[field] == {"effort": "none", "summary": "auto"}
        assert "include" not in payload


def test_anthropic_adaptive_xhigh_maps_to_max_effort() -> None:
    payload = build_messages_payload(
        _request("anthropic", model_id="claude-opus-5", reasoning_effort="xhigh"),
        (),
    )

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "max"}


def test_chat_tool_result_images_become_explicit_placeholders() -> None:
    tool_result = ToolResultMessage(
        tool_call_id="call_1",
        tool_name="screenshot",
        content=(ImageBlock("image/png", "media-1", "shot.png"),),
    )

    payload = build_chat_payload(
        _request("openai_chat", messages=(tool_result,), supports_images=True),
        (tool_result,),
    )

    assert payload["messages"][1]["content"] == "[image: media-1]"


@pytest.mark.asyncio
async def test_chat_emits_block_end_for_each_final_block() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert any(isinstance(event, BlockEnd) and event.index == 0 for event in events)
    assert isinstance(events[-1], Done)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body", "expected"),
    [
        (
            OpenAIChatAdapter,
            "openai_chat",
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"prompt_tokens_details":{"cached_tokens":3,"cache_write_tokens":2}}}\n\n'
            "data: [DONE]\n\n",
            Usage(input_tokens=5, output_tokens=4, cache_read_tokens=3, cache_write_tokens=2),
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":10,"output_tokens":4,"input_tokens_details":{"cached_tokens":3,"cache_write_tokens":2}}}}\n\n',
            Usage(input_tokens=5, output_tokens=4, cache_read_tokens=3, cache_write_tokens=2),
        ),
    ],
)
async def test_provider_usage_excludes_cached_input_tokens(
    adapter_type: Any,
    protocol: str,
    body: str,
    expected: Usage,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.usage == expected


@pytest.mark.asyncio
async def test_anthropic_usage_keeps_total_cache_write_tokens() -> None:
    body = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1,"cache_creation_input_tokens":25,"cache_creation":{"ephemeral_5m_input_tokens":15,"ephemeral_1h_input_tokens":10}}}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.usage == Usage(input_tokens=1, cache_write_tokens=25)


@pytest.mark.asyncio
async def test_anthropic_usage_falls_back_to_nested_cache_write_tokens() -> None:
    body = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1,"cache_creation":{"ephemeral_5m_input_tokens":15,"ephemeral_1h_input_tokens":10}}}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.usage == Usage(input_tokens=1, cache_write_tokens=25)


@pytest.mark.asyncio
async def test_chat_reasoning_field_is_replayed_as_a_marker() -> None:
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"plan"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.content == (ThinkingBlock("plan", "reasoning_content"),)


@pytest.mark.asyncio
async def test_malformed_tool_arguments_are_terminal_invalid_requests() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_bad","function":{"name":"read","arguments":"not-json"}}]},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.retryable is False
    assert "malformed tool arguments" in error.message


@pytest.mark.asyncio
async def test_gateway_without_served_hop_report_removes_all_opaque_payloads() -> None:
    body = (
        'data: {"type":"message_start","message":{"usage":{}}}\n\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","signature":"sig"}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"x"}}\n\n'
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t","name":"read"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(
            AnthropicAdapter(client, gateway=True),
            _request("anthropic"),
        )

    final = events[-1]
    assert isinstance(final, Done)
    assert all(not isinstance(block, ThinkingBlock) or block.signature is None for block in final.message.content)
    assert all(not isinstance(block, ToolCallBlock) or block.signature is None for block in final.message.content)


class _Loader(MediaLoader):
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def load(self, media_token: str) -> tuple[bytes, str]:
        self.tokens.append(media_token)
        return b"snapshot", "image/jpeg"


@pytest.mark.asyncio
async def test_media_loader_bytes_are_resolved_at_request_time() -> None:
    captured: dict[str, Any] = {}
    loader = _Loader()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    message = UserMessage((ImageBlock("image/png", "media-1", "x.png"),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _events(
            OpenAIChatAdapter(client, media_loader=loader),
            _request("openai_chat", messages=(message,), supports_images=True),
        )

    assert loader.tokens == ["media-1"]
    image_part = captured["messages"][1]["content"][0]
    assert image_part["image_url"]["url"] == f"data:image/jpeg;base64,{base64.b64encode(b'snapshot').decode()}"


class _FailingLoader(MediaLoader):
    async def load(self, media_token: str) -> tuple[bytes, str]:
        raise FileNotFoundError(media_token)


@pytest.mark.asyncio
async def test_media_loader_failure_is_a_provider_error() -> None:
    message = UserMessage((ImageBlock("image/png", "missing", "missing.png"),))
    adapter = OpenAIChatAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
        media_loader=_FailingLoader(),
    )

    events = await _events(
        adapter,
        _request("openai_chat", messages=(message,), supports_images=True),
    )

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "unknown"
    assert events[-1].retryable is False


@pytest.mark.asyncio
async def test_pre_cancelled_request_yields_non_retryable_abort() -> None:
    cancel = CancelToken()
    cancel.cancel("user stopped")
    adapter = OpenAIChatAdapter(httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))))
    try:
        events = await _events(adapter, _request("openai_chat"), cancel)
    finally:
        await adapter.aclose()
    assert len(events) == 1
    assert isinstance(events[0], ProviderError)
    assert events[0].kind == "aborted"
    assert events[0].retryable is False


@pytest.mark.asyncio
async def test_stream_error_after_a_delta_is_terminal_and_keeps_partial() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        "data: not-json\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.retryable is False
    assert error.partial is not None
    assert error.partial.content == (TextBlock(text="partial"),)


@pytest.mark.asyncio
async def test_http_413_is_classified_as_overflow() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text='{"error":{"message":"request too large"}}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "overflow"
    assert error.status == 413
    assert error.retryable is False


@pytest.mark.asyncio
async def test_http_redirect_is_terminal_invalid_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307,
            headers={"location": "https://other.test/v1"},
            text='{"error":{"message":"password=super-secret"}}',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.status == 307
    assert error.retryable is False
    assert "super-secret" not in error.message


@pytest.mark.asyncio
async def test_ipv6_endpoint_in_error_text_keeps_one_sanitized_terminal() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            text="upstream http://[::1]:8080/v1 refused",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert len([event for event in events if isinstance(event, ProviderError)]) == 1
    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "server"
    assert error.retryable is True
    assert "http://[::1]:8080/v1" in error.message


@pytest.mark.asyncio
async def test_error_body_reader_applies_a_byte_cap() -> None:
    response = httpx.Response(500, content=b"x" * 70_000)

    body = await read_response_body(response, CancelToken(), max_bytes=64 * 1024)

    assert body is not None
    assert len(body.encode()) == 64 * 1024


class _StalledStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.first_chunk = first_chunk or b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    async def __aiter__(self):
        self.started.set()
        yield self.first_chunk
        await self.release.wait()

    async def aclose(self) -> None:
        self.closed.set()


class _StalledFirstByteStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await self.release.wait()
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed.set()


class _NonCooperativeReadStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.cancel_count = 0
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_count += 1
                self.cancel_seen.set()
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed.set()
        self.release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "first_byte", "idle"])
async def test_shared_driver_bounds_each_network_wait(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    import core.agent_core.ai._common as common

    monkeypatch.setattr(common, "CONNECT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(common, "TIME_TO_FIRST_BYTE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(common, "IDLE_CHUNK_TIMEOUT_S", 0.01)
    opened = asyncio.Event()
    first_byte_stream = _StalledFirstByteStream()
    idle_stream = _StalledStream(
        b'data: {"choices":[{"delta":{},"finish_reason":null}]}\n\n'
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        if phase == "open":
            opened.set()
            await asyncio.Event().wait()
        if phase == "first_byte":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=first_byte_stream,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=idle_stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            _events(OpenAIChatAdapter(client), _request("openai_chat"))
        )
        if phase == "open":
            await opened.wait()
        elif phase == "first_byte":
            await first_byte_stream.started.wait()
        else:
            await idle_stream.started.wait()
        events = await asyncio.wait_for(task, timeout=1)

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "network"
    assert error.retryable is True
    assert error.partial is None
    if phase == "first_byte":
        assert first_byte_stream.closed.is_set()
    elif phase == "idle":
        assert idle_stream.closed.is_set()


@pytest.mark.asyncio
async def test_non_cooperative_stream_cancellation_join_does_not_block_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.agent_core.ai._common as common

    monkeypatch.setattr(common, "TIME_TO_FIRST_BYTE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(common, "CLEANUP_TIMEOUT_S", 0.01)
    stream = _NonCooperativeReadStream()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(_events(OpenAIChatAdapter(client), _request("openai_chat")))
        await stream.started.wait()
        events = await asyncio.wait_for(task, timeout=0.5)

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "network"
    assert error.retryable is True
    assert stream.cancel_seen.is_set()
    assert stream.cancel_count == 1
    assert stream.closed.is_set()


@pytest.mark.asyncio
async def test_cancellation_after_placeholder_slot_has_no_partial_message() -> None:
    stream = _StalledStream(
        b'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    cancel = CancelToken()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(_events(OpenAIResponsesAdapter(client), _request("openai_responses"), cancel))
        await stream.started.wait()
        cancel.cancel("cancelled after placeholder")
        events = await asyncio.wait_for(task, timeout=1)

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "aborted"
    assert error.partial is None


class _StalledErrorBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await self.release.wait()
        yield b'{"error":{"message":"late"}}'

    async def aclose(self) -> None:
        self.closed.set()


class _ErrorBodyReadFailure(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        raise OSError("error body read failed")
        yield b""

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_redirect_error_body_read_failure_is_terminal_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.agent_core.ai._common as common

    monkeypatch.setattr(common, "TIME_TO_FIRST_BYTE_TIMEOUT_S", 0.01)
    stream = _ErrorBodyReadFailure()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(307, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.status == 307
    assert error.retryable is False


class _StalledLoader(MediaLoader):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def load(self, media_token: str) -> tuple[bytes, str]:
        del media_token
        self.started.set()
        await self.release.wait()
        return b"snapshot", "image/png"


class _StalledResolver:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, headers: Mapping[str, str]) -> Origin | None:
        del headers
        self.started.set()
        await self.release.wait()
        return Origin("served", "openai_chat", "served-model")


class _CloseFailStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):
        yield self.body

    async def aclose(self) -> None:
        raise OSError("close failed")


class _TrackCloseStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield self.body

    async def aclose(self) -> None:
        self.closed.set()


class _StalledCloseStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield self.body

    async def aclose(self) -> None:
        self.started.set()
        await self.release.wait()


class _DelayedReadErrorStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes, order: list[str]) -> None:
        self.first_chunk = first_chunk
        self.order = order
        self.read_started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield self.first_chunk
        self.read_started.set()
        await self.release.wait()
        raise OSError("delayed transport read failed")

    async def aclose(self) -> None:
        self.order.append("close")


class _StalledCloseFailStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await self.release.wait()
        yield b""

    async def aclose(self) -> None:
        raise OSError("close failed while cancelled")


class _CancelAfterEofCloseFailStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, cancel: CancelToken) -> None:
        self.body = body
        self.cancel = cancel

    async def __aiter__(self):
        yield self.body
        self.cancel.cancel("cancelled after stream EOF")

    async def aclose(self) -> None:
        raise OSError("close failed after cancellation")


_CANCELLATION_CASES = (
    (AnthropicAdapter, "anthropic", 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}\n\n'),
    (OpenAIChatAdapter, "openai_chat", 'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'),
    (OpenAIResponsesAdapter, "openai_responses", 'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_type", "protocol", "stream_body"), _CANCELLATION_CASES)
@pytest.mark.parametrize("phase", ["open", "stream", "error_body", "media_load"])
async def test_all_adapters_cancel_at_every_http_lifecycle_phase(
    adapter_type: Any,
    protocol: str,
    stream_body: str,
    phase: str,
) -> None:
    started = asyncio.Event()
    stalled_stream = _StalledStream(stream_body.encode())
    stalled_body = _StalledErrorBody()
    stalled_loader = _StalledLoader()

    async def handler(_: httpx.Request) -> httpx.Response:
        if phase == "open":
            started.set()
            await asyncio.Event().wait()
        if phase == "stream":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stalled_stream,
            )
        if phase == "error_body":
            return httpx.Response(500, stream=stalled_body)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=stream_body,
        )

    cancel = CancelToken()
    if phase == "media_load":
        request = _request(
            protocol,
            messages=(UserMessage((ImageBlock("image/png", "media-1", "image.png"),)),),
            supports_images=True,
        )
    else:
        request = _request(protocol)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = adapter_type(
            client,
            media_loader=stalled_loader if phase == "media_load" else None,
        )
        task = asyncio.create_task(_events(adapter, request, cancel))
        if phase == "open":
            await started.wait()
        elif phase == "stream":
            await stalled_stream.started.wait()
        elif phase == "error_body":
            await stalled_body.started.wait()
        else:
            await stalled_loader.started.wait()
        cancel.cancel(f"cancelled during {phase}")
        events = await asyncio.wait_for(task, timeout=1)

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "aborted"
    if phase == "stream":
        assert stalled_stream.closed.is_set()
    elif phase == "error_body":
        assert stalled_body.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_type", "protocol", "stream_body"), _CANCELLATION_CASES)
async def test_all_adapters_cancel_during_served_hop_resolution(
    adapter_type: Any,
    protocol: str,
    stream_body: str,
) -> None:
    resolver = _StalledResolver()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=stream_body,
        )

    cancel = CancelToken()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = adapter_type(client, served_hop_resolver=resolver, gateway=True)
        task = asyncio.create_task(_events(adapter, _request(protocol), cancel))
        await resolver.started.wait()
        cancel.cancel("cancelled during served-hop resolution")
        events = await asyncio.wait_for(task, timeout=1)

    assert len(events) == 1
    assert isinstance(events[0], ProviderError)
    assert events[0].kind == "aborted"


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_type", "protocol", "body"), [
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ),
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        'data: [DONE]\n\n',
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
    ),
])
async def test_close_failure_cannot_emit_a_second_terminal_event(
    adapter_type: Any,
    protocol: str,
    body: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_CloseFailStream(body.encode()),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    terminals = [event for event in events if isinstance(event, (Done, ProviderError))]
    assert len(terminals) == 1


@pytest.mark.asyncio
async def test_consumer_close_suppresses_response_close_failure() -> None:
    body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_CloseFailStream(body),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = OpenAIChatAdapter(client).stream(_request("openai_chat"), CancelToken())
        first = await anext(stream)
        assert isinstance(first, TextDelta)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'data: {"type":"message_stop"}\n\n',
        ),
        (
            OpenAIChatAdapter,
            "openai_chat",
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n',
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
        ),
    ],
)
async def test_stalled_response_close_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: Any,
    protocol: str,
    body: bytes,
) -> None:
    import core.agent_core.ai._common as common

    monkeypatch.setattr(common, "CLEANUP_TIMEOUT_S", 0.01)
    stream = _StalledCloseStream(body)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await asyncio.wait_for(
            _events(adapter_type(client), _request(protocol)),
            timeout=1,
        )

    assert isinstance(events[-1], Done)
    assert stream.started.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'data: {"type":"message_stop"}\n\n',
        ),
        (
            OpenAIChatAdapter,
            "openai_chat",
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n',
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
        ),
    ],
)
async def test_cancellation_during_response_cleanup_is_preserved(
    adapter_type: Any,
    protocol: str,
    body: bytes,
) -> None:
    stream = _StalledCloseStream(body)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(_events(adapter_type(client), _request(protocol)))
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "status", "kind", "retryable"),
    [
        (AnthropicAdapter, "anthropic", 401, "auth", False),
        (OpenAIChatAdapter, "openai_chat", 401, "auth", False),
        (OpenAIResponsesAdapter, "openai_responses", 401, "auth", False),
        (AnthropicAdapter, "anthropic", 429, "rate_limit", True),
        (OpenAIChatAdapter, "openai_chat", 429, "rate_limit", True),
        (OpenAIResponsesAdapter, "openai_responses", 429, "rate_limit", True),
    ],
)
async def test_error_body_read_failure_preserves_http_classification(
    adapter_type: Any,
    protocol: str,
    status: int,
    kind: str,
    retryable: bool,
) -> None:
    stream = _ErrorBodyReadFailure()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"Retry-After": "7"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == kind
    assert error.retryable is retryable
    assert error.status == status
    assert error.retry_after_s == 7
    assert stream.closed.is_set()


@pytest.mark.asyncio
async def test_oversized_sse_pending_line_is_terminal_malformed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sse_module, "DEFAULT_MAX_PENDING_LINE_SIZE", 8)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: too-large-without-a-line-ending",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.retryable is False
    assert "SSE" in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'data: {"type":"message_stop"}\n\n',
        ),
        (
            OpenAIChatAdapter,
            "openai_chat",
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n',
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
        ),
    ],
)
async def test_consumer_close_after_terminal_closes_response(
    adapter_type: Any,
    protocol: str,
    body: bytes,
) -> None:
    stream = _TrackCloseStream(body)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response_stream = adapter_type(client).stream(_request(protocol), CancelToken())
        while not isinstance((event := await anext(response_stream)), (Done, ProviderError)):
            pass
        await response_stream.aclose()

    assert stream.closed.is_set()


@pytest.mark.asyncio
async def test_task_cancellation_is_not_replaced_by_response_close_failure() -> None:
    stream = _StalledCloseFailStream()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(_events(OpenAIChatAdapter(client), _request("openai_chat")))
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_chat_post_loop_cancel_with_close_failure_emits_one_terminal_event() -> None:
    cancel = CancelToken()
    body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_CancelAfterEofCloseFailStream(body, cancel),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"), cancel)

    terminals = [event for event in events if isinstance(event, (Done, ProviderError))]
    assert len(terminals) == 1
    assert isinstance(terminals[0], ProviderError)
    assert terminals[0].kind == "aborted"


_DELAYED_READ_ERROR_CASES = (
    (
        AnthropicAdapter,
        "anthropic",
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}\n\n',
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        b'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        b'data: {"type":"response.output_text.delta","output_index":0,"delta":"partial"}\n\n',
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_type", "protocol", "first_chunk"), _DELAYED_READ_ERROR_CASES)
async def test_delayed_transport_read_emits_terminal_before_close(
    adapter_type: Any,
    protocol: str,
    first_chunk: bytes,
) -> None:
    order: list[str] = []
    stream = _DelayedReadErrorStream(first_chunk, order)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    events: list[Any] = []

    async def collect() -> None:
        async for event in adapter.stream(_request(protocol), CancelToken()):
            events.append(event)
            if isinstance(event, ProviderError):
                order.append("terminal")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = adapter_type(client)
        task = asyncio.create_task(collect())
        await stream.read_started.wait()
        stream.release.set()
        await task
        await adapter.aclose()

    assert order == ["terminal", "close"]
    terminals = [event for event in events if isinstance(event, (Done, ProviderError))]
    assert len(terminals) == 1
    assert isinstance(terminals[0], ProviderError)
    assert terminals[0].kind == "network"
    assert terminals[0].retryable is False


_WIRE_EVENT_CASES = (
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"ping"}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ),
        "done",
        "stop",
        None,
        None,
    ),
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"message_start","message":{"usage":{"input_tokens":4}}}\n\n'
            'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n'
        ),
        "error",
        None,
        "overloaded",
        True,
    ),
    (
        AnthropicAdapter,
        "anthropic",
        'event: error\ndata: {"type":"error","error":{"type":"authentication_error","message":"bad key"}}\n\n',
        "error",
        None,
        "auth",
        False,
    ),
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"message_start","message":{"usage":{"input_tokens":4}}}\n\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}\n\n'
            'data: {"type":"error","error":{"type":"api_error","message":"failed"}}\n\n'
        ),
        "error",
        None,
        "server",
        True,
    ),
    (
        AnthropicAdapter,
        "anthropic",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","name":""}}\n\n',
        "error",
        None,
        "invalid_request",
        False,
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        'data: {"error":{"type":"rate_limit_error","message":"slow down"}}\n\n',
        "error",
        None,
        "rate_limit",
        False,
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        'data: {"error":{"type":"invalid_api_key","message":"bad key"}}\n\n',
        "error",
        None,
        "auth",
        False,
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        (
            'data: {"choices":[{"delta":{"function_call":{"name":"read"}}}]}\n\n'
            'data: {"choices":[{"delta":{"function_call":{"arguments":"{\\"path\\":"}}}]}\n\n'
            'data: {"choices":[{"delta":{"function_call":{"arguments":"\\"x\\"}"}},"finish_reason":"function_call"}]}\n\n'
            "data: [DONE]\n\n"
        ),
        "done",
        "tool_use",
        None,
        None,
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":""}}]}}]}\n\n',
        "error",
        None,
        "invalid_request",
        False,
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        (
            'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            'data: {"error":{"type":"server_error","message":"failed"}}\n\n'
        ),
        "error",
        None,
        "server",
        True,
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        (
            'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
            'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"server_error","message":"busy"},"usage":{"input_tokens":2}}}\n\n'
        ),
        "error",
        None,
        "server",
        True,
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        (
            'data: {"type":"response.incomplete","response":{"status":"incomplete","error":{"code":"context_length_exceeded","message":"too long"},"usage":{"input_tokens":2}}}\n\n'
        ),
        "error",
        None,
        "overflow",
        True,
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","name":""}}\n\n',
        "error",
        None,
        "invalid_request",
        False,
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        'data: {"type":"error","error":{"code":"permission","message":"denied"}}\n\n',
        "error",
        None,
        "auth",
        False,
    ),
)


_WIRE_SHAPE_CASES = (
    (
        AnthropicAdapter,
        "anthropic",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","signature":7}}\n\n',
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":[]}\n\n',
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        'data: {"type":"response.completed","response":{"status":"completed","usage":[]}}\n\n',
    ),
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":7}}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ),
    ),
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text"}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":7}}\n\n'
        ),
    ),
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"message_delta","delta":{"stop_reason":7}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ),
    ),
    (
        AnthropicAdapter,
        "anthropic",
        (
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"tool_use","id":7,"name":"read"}}\n\n'
        ),
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":"read"}]},'
            '"finish_reason":"tool_calls"}]}\n\n'
        ),
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":7,'
            '"function":{"name":"read","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        ),
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        'data: {"choices":[{"delta":{"reasoning_content":7},"finish_reason":"stop"}]}\n\n',
    ),
    (
        OpenAIChatAdapter,
        "openai_chat",
        'data: {"choices":[{"delta":{"reasoning_details":{}},"finish_reason":"stop"}]}\n\n',
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        'data: {"type":"response.completed","response":{"status":5}}\n\n',
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        (
            'data: {"type":"response.completed","response":'
            '{"status":"completed","error":"boom"}}\n\n'
        ),
    ),
    (
        OpenAIResponsesAdapter,
        "openai_responses",
        (
            'data: {"type":"response.incomplete","response":'
            '{"status":"incomplete","incomplete_details":"max_output_tokens"}}\n\n'
        ),
    ),
    *(
        (adapter, protocol, f"data: {json.dumps(frame)}\n\n")
        for adapter, protocol, frame in [
            (AnthropicAdapter, "anthropic", {"type": "error", "error": []}),
            (AnthropicAdapter, "anthropic", {"type": "error", "error": {"message": 7}}),
            (OpenAIChatAdapter, "openai_chat", {"choices": [{"usage": [], "finish_reason": "stop"}]}),
            (OpenAIChatAdapter, "openai_chat", {"choices": [{"delta": {"reasoning": 2}, "finish_reason": "stop"}]}),
            (OpenAIChatAdapter, "openai_chat", {"choices": [{"delta": {"reasoning_text": 2}, "finish_reason": "stop"}]}),
            (OpenAIChatAdapter, "openai_chat", {"choices": [{"delta": {"tool_calls": [{"index": "0", "function": {"name": "read"}}]}, "finish_reason": "tool_calls"}]}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.created", "response": {"status": 2}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "error", "error": {"code": []}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "error", "error": {"type": 7}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.output_item.added", "item": {"type": 2}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.output_item.done", "item": {"type": "message", "content": {}}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.function_call_arguments.delta", "item_id": 2, "delta": "{}"}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.failed", "error": {"message": 7, "code": []}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.failed", "response": {"error": {"message": 7}}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.failed", "response": {"error": [], "usage": {"input_tokens": 4}}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.failed", "response": {"usage": []}}),
            (OpenAIResponsesAdapter, "openai_responses", {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": 2}}}),
        ]
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "named", "data"])
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "error", "kind"),
    [
        (AnthropicAdapter, "anthropic", {"type": "permission_error", "message": "denied"}, "auth"),
        (OpenAIChatAdapter, "openai_chat", {"code": "invalid_api_key", "message": "denied"}, "auth"),
        (OpenAIResponsesAdapter, "openai_responses", {"code": "server_error", "message": "busy"}, "server"),
    ],
)
async def test_provider_error_envelope_is_classified_identically_from_every_source(
    source: str, adapter_type: Any, protocol: str, error: dict[str, Any], kind: str,
) -> None:
    envelope = json.dumps({"type": "error", "error": error})
    body = envelope if source == "http" else (
        ("event: error\n" if source == "named" else "") + f"data: {envelope}\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400 if source == "http" else 200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    assert len(events) == 1
    assert isinstance(events[0], ProviderError)
    assert events[0].kind == kind
    assert events[0].retryable == (kind == "server")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body", "outcome", "stop_reason", "error_kind", "partial"),
    _WIRE_EVENT_CASES,
)
async def test_wire_event_matrix_has_one_explicit_outcome(
    adapter_type: Any,
    protocol: str,
    body: str,
    outcome: str,
    stop_reason: str | None,
    error_kind: str | None,
    partial: bool | None,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    terminals = [event for event in events if isinstance(event, (Done, ProviderError))]
    assert len(terminals) == 1
    terminal = terminals[0]
    if outcome == "done":
        assert isinstance(terminal, Done)
        assert terminal.message.stop_reason == stop_reason
    else:
        assert isinstance(terminal, ProviderError)
        assert terminal.kind == error_kind
        if partial:
            assert terminal.partial is not None
        elif partial is False:
            assert terminal.partial is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_type", "protocol", "body"), _WIRE_SHAPE_CASES)
async def test_known_wire_fields_with_wrong_types_are_terminal_malformed(
    adapter_type: Any,
    protocol: str,
    body: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    terminals = [event for event in events if isinstance(event, (Done, ProviderError))]
    assert len(terminals) == 1
    assert isinstance(terminals[0], ProviderError)
    assert terminals[0].kind == "invalid_request"


@pytest.mark.asyncio
async def test_cancellation_during_a_stalled_read_ends_with_aborted_error() -> None:
    stream = _StalledStream()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    cancel = CancelToken()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(_events(OpenAIChatAdapter(client), _request("openai_chat"), cancel))
        await stream.started.wait()
        cancel.cancel("cancelled by test")
        events = await asyncio.wait_for(task, timeout=1)

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "aborted"
    assert not any(isinstance(event, Done) for event in events)


@pytest.mark.asyncio
async def test_cancellation_during_stream_open_ends_with_aborted_error() -> None:
    started = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    cancel = CancelToken()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(_events(OpenAIChatAdapter(client), _request("openai_chat"), cancel))
        await started.wait()
        cancel.cancel("cancelled while opening")
        events = await asyncio.wait_for(task, timeout=1)

    assert events == [
        ProviderError(
            kind="aborted",
            message="cancelled while opening",
            retryable=False,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}\n\n',
        ),
        (
            OpenAIChatAdapter,
            "openai_chat",
            'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
            'data: {"type":"response.output_text.delta","output_index":0,"delta":"partial"}\n\n',
        ),
    ],
)
async def test_eof_without_protocol_terminal_is_not_success(
    adapter_type: Any,
    protocol: str,
    body: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].retryable is False
    assert events[-1].partial is not None
    assert not any(isinstance(event, Done) for event in events)


@pytest.mark.asyncio
async def test_empty_reasoning_slot_does_not_disable_network_retry() -> None:
    stream = _CloseFailStream(
        b'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "network"
    assert error.retryable is True
    assert error.partial is None


@pytest.mark.asyncio
async def test_adapter_error_redaction_preserves_json_classification() -> None:
    body = '{"details":[{"url":"https://model.test/v1?project=secret"}]}'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text=body)

    request = ModelRequest(
        endpoint=ModelEndpoint(
            protocol="openai_chat",
            base_url="https://user:password@model.test/v1?token=secret",
            model_id="model-x",
            token="token",
            provider="openai",
        ),
        system="system",
        messages=(UserMessage((TextBlock(text="hello"),)),),
        tools=(),
        max_tokens=128,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), request)

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "overflow"
    parsed = json.loads(error.message)
    assert parsed["details"][0]["url"] == "https://model.test/v1"
    assert "password" not in error.message
    assert "project=secret" not in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        '{"error":{"message":"x-goog-api-key: google-secret"}}',
        '{"error":{"x-goog-api-key":"google-secret"}}',
        "provider failed: x-goog-api-key=google-secret",
    ],
)
async def test_shared_error_parser_redacts_google_api_key(body: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert "google-secret" not in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "protocol", "body"),
    [
        (
            AnthropicAdapter,
            "anthropic",
            (
                'data: {"type":"message_start","message":{"usage":{"input_tokens":-1}}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
        ),
        (
            OpenAIChatAdapter,
            "openai_chat",
            (
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":-1}}\n\n'
                "data: [DONE]\n\n"
            ),
        ),
        (
            OpenAIResponsesAdapter,
            "openai_responses",
            (
                'data: {"type":"response.completed","response":{"status":"completed",'
                '"output":[],"usage":{"input_tokens":"2"}}}\n\n'
            ),
        ),
    ],
)
async def test_adapters_reject_malformed_present_usage_counters(
    adapter_type: Any,
    protocol: str,
    body: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(adapter_type(client), _request(protocol))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert "usage" in error.message.lower()


@pytest.mark.asyncio
async def test_chat_requires_finish_reason_before_done_marker() -> None:
    body = "data: [DONE]\n\n"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "network"
    assert events[-1].retryable is True
    assert events[-1].partial is None


@pytest.mark.asyncio
async def test_chat_prompt_feedback_without_candidates_is_incomplete() -> None:
    body = (
        'data: {"promptFeedback":{"blockReason":"SAFETY"}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "network"
    assert error.retryable is True
    assert error.partial is None


@pytest.mark.asyncio
async def test_chat_finish_reason_can_terminate_at_eof_without_done_marker() -> None:
    body = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.stop_reason == "stop"


@pytest.mark.asyncio
async def test_chat_max_tokens_maps_to_length() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"max_tokens"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "length"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_reason", ["safety", "recitation"])
async def test_chat_native_safety_overrides_stop(native_reason: str) -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read","arguments":"{}"}}]},"finish_reason":null}]}\n\n'
        f'data: {json.dumps({"choices": [{"delta": {}, "finish_reason": "stop", "native_finish_reason": native_reason}]})}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "safety"
    assert len(final.message.tool_calls) == 1


@pytest.mark.asyncio
async def test_chat_unknown_finish_reason_is_canonical_error() -> None:
    body = 'data: {"choices":[{"delta":{},"finish_reason":"future_reason"}]}\n\ndata: [DONE]\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.stop_reason == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        [7],
        [{"type": "output_text", "text": 7}],
        [{"type": "refusal", "refusal": 7}],
    ],
)
async def test_responses_rejects_malformed_message_content_parts(content: list[Any]) -> None:
    body = (
        'data: {"type":"response.output_item.done","output_index":0,'
        + json.dumps(
            {
                "item": {
                    "type": "message",
                    "content": content,
                }
            },
            ensure_ascii=False,
        )[1:]
        + "\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.retryable is False


@pytest.mark.asyncio
async def test_responses_rejects_malformed_terminal_message_content() -> None:
    body = (
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"message","content":[{"type":"output_text","text":7}]}]}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"
    assert error.retryable is False


@pytest.mark.asyncio
async def test_anthropic_message_stop_without_reason_is_terminal_error() -> None:
    body = 'data: {"type":"message_stop"}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "invalid_request"


@pytest.mark.asyncio
async def test_anthropic_stop_reason_allows_eof_without_message_stop_like_pi() -> None:
    body = 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "stop"


@pytest.mark.asyncio
async def test_anthropic_message_start_requires_message_stop_even_with_stop_reason() -> None:
    body = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "network"
    assert error.retryable is True
    assert error.partial is not None
    assert error.partial.usage == Usage(input_tokens=7)


@pytest.mark.asyncio
async def test_anthropic_malformed_data_only_frame_is_ignored_like_pi() -> None:
    body = (
        "data: not-json\n\n"
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(AnthropicAdapter(client), _request("anthropic"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.stop_reason == "stop"


@pytest.mark.asyncio
async def test_chat_refusal_stop_is_not_overwritten_by_later_stop_reason() -> None:
    body = (
        'data: {"choices":[{"delta":{"refusal":"cannot help"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.stop_reason == "refusal"


@pytest.mark.asyncio
async def test_responses_message_output_item_done_backfills_text() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"done"}]}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert [event.delta for event in events if isinstance(event, TextDelta)] == ["done"]
    assert events[-1].message.content == (TextBlock("done"),)


@pytest.mark.asyncio
async def test_responses_reasoning_snapshot_emits_unseen_suffix() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.reasoning_text.delta","output_index":0,"delta":"plan"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text","text":"plan"},{"type":"summary_text","text":"final"}]}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert [event.delta for event in events if isinstance(event, ThinkingDelta)] == [
        "plan",
        "\n\nfinal",
    ]
    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (ThinkingBlock("plan\n\nfinal"),)


@pytest.mark.asyncio
async def test_chat_typed_model_hub_error_frame_is_terminal() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        'data: {"object":"chat.completion.chunk","type":"error","error":{"type":"server_error","message":"failed"},"choices":[]}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "server"
    assert events[-1].partial is not None
    assert events[-1].partial.content == (TextBlock("partial"),)


@pytest.mark.asyncio
async def test_responses_snapshot_output_counts_as_streamed_partial() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","content":[{"type":"output_text","text":"snapshot"}]}}\n\n'
        'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"server_error","message":"failed"}}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "server"
    assert error.retryable is False
    assert error.partial is not None
    assert error.partial.content == (TextBlock("snapshot"),)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["content", "refusal"])
async def test_chat_known_text_fields_reject_wrong_types(field: str) -> None:
    delta = {field: 7}
    body = f'data: {json.dumps({"choices": [{"delta": delta, "finish_reason": "stop"}]})}\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request"


@pytest.mark.asyncio
async def test_chat_tool_call_without_integer_index_uses_native_id_alias() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"name":"read"}}]},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"arguments":"{\\"path\\":\\"x\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].id == "call_1"
    assert events[-1].message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_chat_null_optional_fields_are_absent() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"ok","tool_calls":null,"function_call":null},"finish_reason":"stop"}],"usage":null}\n\n'
        'data: {"choices":null,"usage":null}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (TextBlock("ok"),)


@pytest.mark.asyncio
async def test_chat_tool_call_without_name_is_terminal_invalid_request() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"arguments":"{}"}}]},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert isinstance(events[-1], ProviderError)
    assert events[-1].kind == "invalid_request"
    assert "tool call name is missing" in events[-1].message


@pytest.mark.asyncio
async def test_chat_arguments_before_name_are_emitted_after_tool_start() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"arguments":"{\\"path\\":\\"x\\"}"}}]},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read","arguments":null}}]},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIChatAdapter(client), _request("openai_chat"))

    assert [event.arguments_delta for event in events if isinstance(event, ToolCallDelta)] == [
        '{"path":"x"}'
    ]
    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].arguments == {"path": "x"}


@pytest.mark.asyncio
async def test_responses_non_prefix_arguments_done_emits_no_duplicate_delta() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"a\\":1"}\n\n'
        'data: {"type":"response.function_call_arguments.done","output_index":0,"arguments":"{\\"a\\": 1}"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{\\"a\\": 1}"}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert [event.arguments_delta for event in events if isinstance(event, ToolCallDelta)] == ['{"a":1']
    assert isinstance(events[-1], Done)
    assert events[-1].message.tool_calls[0].arguments == {"a": 1}


@pytest.mark.asyncio
async def test_responses_summary_done_replaces_streaming_separator_after_late_delta() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.reasoning_summary_text.delta","output_index":0,"delta":"p1"}\n\n'
        'data: {"type":"response.reasoning_summary_part.done","output_index":0}\n\n'
        'data: {"type":"response.reasoning_summary_text.delta","output_index":0,"delta":"p2"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_1","encrypted_content":"enc","summary":[{"type":"summary_text","text":"p1"},{"type":"summary_text","text":"p2"}]}}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert isinstance(events[-1], Done)
    assert events[-1].message.content == (
        ThinkingBlock(
            "p1\n\np2",
            '{"type":"reasoning","id":"rs_1","encrypted_content":"enc","summary":[{"type":"summary_text","text":"p1"},{"type":"summary_text","text":"p2"}]}',
        ),
    )
