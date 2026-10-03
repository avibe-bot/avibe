from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from core.agent_core.ai._common import endpoint_origin, sanitize_endpoint_text
from core.agent_core.ai.errors import classify_error
from core.agent_core.ai.provider import ModelEndpoint
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
    source = Origin("openai", "openai_chat", "foreign-test")
    target = Origin("anthropic", "anthropic", "claude-test")
    call = ToolCallBlock("call|foreign", "read", {"path": "x"}, signature="foreign-opaque")
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


def test_cross_provider_transform_drops_results_for_failed_tool_turns() -> None:
    history = (
        UserMessage((TextBlock(text="request"),)),
        AssistantMessage(
            content=(ToolCallBlock("failed-call", "read", {}),),
            origin=Origin("openai", "openai_chat", "model"),
            stop_reason="error",
        ),
        ToolResultMessage(
            tool_call_id="failed-call",
            tool_name="read",
            content=(TextBlock(text="settled"),),
            is_error=True,
        ),
        UserMessage((TextBlock(text="next request"),)),
    )

    transformed = transform_messages(history, Origin("anthropic", "anthropic", "model"))

    assert not any(
        isinstance(message, ToolResultMessage) and message.tool_call_id == "failed-call"
        for message in transformed.messages
    )


def test_transform_keeps_normalized_tool_ids_unique_per_request() -> None:
    unsafe_id = "bad:id"
    colliding_safe_id = "call_" + hashlib.sha256(unsafe_id.encode()).hexdigest()[:24]
    history = (
        AssistantMessage(
            content=(
                ToolCallBlock(unsafe_id, "first", {"n": 1}),
                ToolCallBlock(colliding_safe_id, "second", {"n": 2}),
            ),
            origin=Origin("openai", "openai_responses", "model"),
            stop_reason="tool_use",
        ),
    )

    transformed = transform_messages(history, Origin("anthropic", "anthropic", "model"))
    assistant = transformed.messages[0]
    assert isinstance(assistant, AssistantMessage)
    assert len({call.id for call in assistant.tool_calls}) == 2
    assert assistant.tool_calls[0].id == transformed.tool_call_id_map[unsafe_id]
    assert assistant.tool_calls[1].id == transformed.tool_call_id_map[colliding_safe_id]


def test_transform_correlates_reused_tool_ids_by_turn() -> None:
    origin = Origin("openai", "openai_chat", "model")
    history = (
        AssistantMessage(
            content=(ToolCallBlock("reused", "read", {"turn": 1}),),
            origin=origin,
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            tool_call_id="reused",
            tool_name="read",
            content=(TextBlock(text="first"),),
        ),
        AssistantMessage(
            content=(ToolCallBlock("reused", "read", {"turn": 2}),),
            origin=origin,
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            tool_call_id="reused",
            tool_name="read",
            content=(TextBlock(text="second"),),
        ),
    )

    transformed = transform_messages(history, Origin("anthropic", "anthropic", "model"))
    assistants = [message for message in transformed.messages if isinstance(message, AssistantMessage)]
    results = [message for message in transformed.messages if isinstance(message, ToolResultMessage)]

    assert len(assistants) == 2
    assert len(results) == 2
    assert assistants[0].tool_calls[0].id != assistants[1].tool_calls[0].id
    assert results[0].tool_call_id == assistants[0].tool_calls[0].id
    assert results[1].tool_call_id == assistants[1].tool_calls[0].id


def test_empty_provider_custom_endpoints_have_distinct_origins() -> None:
    left = endpoint_origin(
        ModelEndpoint("openai_chat", "https://one.example/v1", "same-model", "token")
    )
    right = endpoint_origin(
        ModelEndpoint("openai_chat", "https://two.example/v1", "same-model", "token")
    )

    assert left != right
    assert left.provider.startswith("endpoint:")
    assert right.provider.startswith("endpoint:")


def test_unnamed_custom_endpoint_origin_is_credential_free() -> None:
    origin = endpoint_origin(
        ModelEndpoint(
            "openai_chat",
            "https://user:secret@one.example:8443/v1?api_key=query-secret",
            "same-model",
            "token",
        )
    )

    assert "secret" not in origin.provider
    assert "query-secret" not in origin.provider
    assert origin.provider == "endpoint:https://one.example:8443/v1"


def test_endpoint_redaction_handles_punctuation_in_credentials() -> None:
    endpoint = "https://user:p,ss@one.example:8443/v1?token=query-secret"

    message = sanitize_endpoint_text(f"request failed at {endpoint}", endpoint)

    assert message == "request failed at https://one.example:8443/v1"


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


@pytest.mark.parametrize(
    "code",
    [
        "authentication_error",
        "invalid_api_key",
        "permission_error",
        "permission",
        "permission_denied",
        "unauthenticated",
    ],
)
def test_provider_auth_codes_are_not_retryable(code: str) -> None:
    error = classify_error(code=code, body=f'{{"error":{{"code":"{code}"}}}}')

    assert error.kind == "auth"
    assert error.retryable is False


def test_specific_provider_code_precedes_nested_detail_code() -> None:
    error = classify_error(
        status=400,
        body=(
            '{"error":{"code":"context_length_exceeded",'
            '"details":[{"type":"rate_limit"}]}}'
        ),
    )

    assert error.kind == "overflow"
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


def test_error_message_redacts_quoted_json_nested_in_message() -> None:
    error = classify_error(body='{"message":"{\\"token\\":\\"secret-token\\"}"}')

    assert "secret-token" not in error.message
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
