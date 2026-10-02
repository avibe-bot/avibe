from __future__ import annotations

import json

import httpx
import pytest

from core.agent_core.ai.errors import classify_error
from core.agent_core.ai.sse import SSEParser
from core.agent_core.ai.transform import SYNTHETIC_TOOL_RESULT, transform_messages
from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

_PROTOCOL_ORIGINS = (
    Origin("anthropic", "anthropic", "model"),
    Origin("openai", "openai_chat", "model"),
    Origin("openai", "openai_responses", "model"),
    Origin("google", "google", "model"),
)


def test_sse_parser_handles_split_crlf_comments_and_multiline_data() -> None:
    parser = SSEParser()
    events = []
    for chunk in (b": keepalive\r", b"\n", b"event: message\r\n", b"data: one\r\n", b"data: two\r", b"\n\r\n"):
        events.extend(parser.feed(chunk))

    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].data == "one\ntwo"


def test_sse_parser_preserves_utf8_codepoints_split_across_byte_chunks() -> None:
    parser = SSEParser()
    events = []
    encoded = "data: 你好\n\n".encode("utf-8")
    for byte in encoded:
        events.extend(parser.feed(bytes([byte])))

    assert events[0].data == "你好"


def test_sse_parser_flushes_a_final_cr_line_ending() -> None:
    assert SSEParser().feed("data: final\r") == []
    parser = SSEParser()
    parser.feed("data: final\r")
    assert parser.finish()[0].data == "final"


def test_sse_parser_strips_one_utf8_bom_at_stream_start() -> None:
    parser = SSEParser()
    events = parser.feed(b"\xef\xbb\xbfdata: first\n\n")
    events.extend(parser.feed("data: second\n\n"))

    assert [event.data for event in events] == ["first", "second"]


def test_cross_provider_transform_drops_opaque_payload_and_answers_orphaned_calls() -> None:
    source = Origin("google", "google", "gemini-test")
    target = Origin("anthropic", "anthropic", "claude-test")
    call = ToolCallBlock("call|foreign", "read", {"path": "x"}, signature="gemini-opaque")
    messages = (
        UserMessage((ImageBlock("image/png", "token", "shot.png"),)),
        AssistantMessage(
            content=(ThinkingBlock("private", "opaque"), call),
            origin=source,
            stop_reason="tool_use",
        ),
    )

    result = transform_messages(messages, target, supports_images=False)

    assistant = result.messages[1]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.content == (
        TextBlock(text="private"),
        ToolCallBlock(
            id=result.tool_call_id_map["call|foreign"],
            name="read",
            arguments={"path": "x"},
        ),
    )
    assert result.messages[0] == UserMessage((TextBlock(text="[image: shot.png]"),))
    synthetic = result.messages[-1]
    assert synthetic.tool_call_id == result.tool_call_id_map["call|foreign"]  # type: ignore[attr-defined]
    assert synthetic.content == (TextBlock(text=SYNTHETIC_TOOL_RESULT),)  # type: ignore[attr-defined]
    assert synthetic.is_error is True  # type: ignore[attr-defined]


def test_error_classification_preserves_retry_after_and_stream_boundary() -> None:
    limited = classify_error(status=429, body='{"error":{"message":"slow down"}}', headers={"Retry-After": "2"})
    assert limited.kind == "rate_limit"
    assert limited.retryable is True
    assert limited.retry_after_s == 2

    overflow = classify_error(status=400, body="context_length_exceeded")
    assert overflow.kind == "overflow"
    assert overflow.retryable is False

    streamed = classify_error(status=503, body="overloaded", streamed=True)
    assert streamed.kind == "overloaded"
    assert streamed.retryable is False


def test_overflow_classifies_cerebras_bodyless_status() -> None:
    assert classify_error(status=400, body="400 status code (no body)").kind == "overflow"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses_are_not_retryable(status: int) -> None:
    error = classify_error(status=status, body="denied")
    assert error.kind == "auth"
    assert error.retryable is False


def test_network_errors_are_retryable_before_any_streamed_output() -> None:
    error = classify_error(exc=httpx.ConnectError("connection failed"))
    assert error.kind == "network"
    assert error.retryable is True


def test_error_json_without_message_redacts_sensitive_values() -> None:
    error = classify_error(body='{"token":"secret-token","api_key":"sk-secret"}')

    assert "secret-token" not in error.message
    assert "sk-secret" not in error.message
    assert "[redacted]" in error.message


@pytest.mark.parametrize("source", _PROTOCOL_ORIGINS)
@pytest.mark.parametrize("target", _PROTOCOL_ORIGINS)
def test_history_from_each_protocol_can_be_replayed_without_foreign_opaque_state(
    source: Origin,
    target: Origin,
) -> None:
    call_id = "native|call"
    history = (
        UserMessage((TextBlock(text="question"),)),
        AssistantMessage(
            content=(
                ThinkingBlock("private", "thinking-signature"),
                ToolCallBlock(call_id, "read", {"path": "x"}, signature="tool-signature"),
            ),
            origin=source,
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            tool_call_id=call_id,
            tool_name="read",
            content=(TextBlock(text="result"),),
        ),
    )

    transformed = transform_messages(history, target, supports_images=True)
    assistant = next(message for message in transformed.messages if isinstance(message, AssistantMessage))
    result = next(message for message in transformed.messages if isinstance(message, ToolResultMessage))
    assert result.tool_call_id == assistant.tool_calls[0].id
    assert any(isinstance(message, ToolResultMessage) for message in transformed.messages)
    if source == target:
        assert assistant.content[0] == ThinkingBlock("private", "thinking-signature")
        assert assistant.tool_calls[0].signature == "tool-signature"
    else:
        assert all(
            not isinstance(block, ThinkingBlock) or block.signature is None
            for block in assistant.content
        )
        assert assistant.tool_calls[0].signature is None
