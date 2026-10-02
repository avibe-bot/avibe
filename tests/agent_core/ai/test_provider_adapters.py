from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.google import GoogleAdapter
from core.agent_core.ai.openai_chat import OpenAIChatAdapter
from core.agent_core.ai.openai_responses import OpenAIResponsesAdapter
from core.agent_core.ai.provider import BlockEnd, Done, MediaLoader, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    UserMessage,
    Usage,
)
from core.agent_core.tools.base import ToolSpec


def _request(protocol: str, *, messages: tuple[Any, ...] = (), supports_images: bool = False, tools: tuple[ToolSpec, ...] = ()) -> ModelRequest:
    return ModelRequest(
        endpoint=ModelEndpoint(protocol, "https://model.test/v1", "model-x", "token"),
        system="system",
        messages=messages or (UserMessage((TextBlock(text="hello"),)),),
        tools=tools,
        max_tokens=128,
        reasoning_effort="high",
        supports_images=supports_images,
    )


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
async def test_responses_sets_client_side_state_rules_and_keeps_reasoning_item() -> None:
    body = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_1"}}\n\n'
        'data: {"type":"response.reasoning_text.delta","output_index":0,"delta":"plan"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_1","encrypted_content":"enc"}}\n\n'
        'data: {"type":"response.output_text.delta","delta":"done"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":2,"output_tokens":4,"output_tokens_details":{"reasoning_tokens":1}}}}\n\n'
    )
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(OpenAIResponsesAdapter(client), _request("openai_responses"))

    assert captured["store"] is False
    assert "previous_response_id" not in captured
    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.content[0] == ThinkingBlock("plan", json.dumps({"id": "rs_1", "encrypted_content": "enc"}, separators=(",", ":")))
    assert final.message.usage == Usage(input_tokens=2, output_tokens=4, reasoning_tokens=1)


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
async def test_responses_refusal_is_not_normalized_to_stop() -> None:
    body = (
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
async def test_gemini_carries_tool_thought_signature_and_usage() -> None:
    body = (
        'data: {"candidates":[{"content":{"parts":[{"functionCall":{"id":"call_1","name":"read","args":{"path":"x"}},"thoughtSignature":"opaque"}]}}],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":4,"thoughtsTokenCount":2}}\n\n'
        'data: {"candidates":[{"finishReason":"STOP"}]}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(GoogleAdapter(client), _request("google"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.tool_calls[0].signature == "opaque"
    assert final.message.usage == Usage(input_tokens=7, output_tokens=4, reasoning_tokens=2)


@pytest.mark.asyncio
async def test_gemini_malformed_function_call_is_error_even_with_partial_call() -> None:
    body = (
        'data: {"candidates":[{"content":{"parts":[{"functionCall":{"id":"call_1","name":"read","args":{"path":"x"}}}]},"finishReason":"MALFORMED_FUNCTION_CALL"}]}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _events(GoogleAdapter(client), _request("google"))

    final = events[-1]
    assert isinstance(final, Done)
    assert final.message.stop_reason == "error"


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


class _StalledStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        await self.release.wait()


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
