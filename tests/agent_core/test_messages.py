"""C-1 persisted message shape.

Rows written by one release are read by the next, so the dict form must round
trip exactly and must refuse fields it does not know rather than drop them.
"""

from __future__ import annotations

import pytest

from core.agent_core.messages import (
    AssistantMessage,
    TextBlock,
    message_from_dict,
    message_to_dict,
)

ASSISTANT = {
    "role": "assistant",
    "content": [
        {"type": "thinking", "text": "The failing test imports a renamed helper.", "signature": "EqQBCkYIBxgCKkD"},
        {"type": "text", "text": "运行测试看看。"},
        {"type": "tool_call", "id": "toolu_01AbC", "name": "bash", "arguments": {"command": "pytest -q"}},
    ],
    "origin": {"provider": "anthropic", "api": "anthropic", "model": "claude-opus-5-5"},
    "usage": {"input_tokens": 1834, "output_tokens": 212, "cache_read_tokens": 40210, "cache_write_tokens": 0},
    "stop_reason": "tool_use",
}
TOOL_RESULT = {
    "role": "tool_result",
    "tool_call_id": "toolu_01AbC",
    "tool_name": "read",
    "content": [
        {"type": "text", "text": "Read image file [image/png]"},
        {"type": "image", "mime_type": "image/png", "media_token": "W8YdX9o4", "name": "shot.png"},
    ],
    "is_error": False,
}
USER_WITH_REF = {
    "role": "user",
    "content": [{"type": "text", "ref": {"ref": "sha256:" + "a" * 64, "bytes": 81920}}],
}
REDACTED = {
    "role": "assistant",
    "content": [{"type": "thinking", "text": "", "signature": "opaque", "redacted": True}],
    "origin": {"provider": "openai", "api": "openai_responses", "model": "gpt-5.6"},
    "stop_reason": "error",
    "error_message": "stream reset",
}


UNSIGNED = {
    "role": "assistant",
    "content": [{"type": "thinking", "text": "Summary only."}, {"type": "text", "text": "done"}],
    "origin": {"provider": "google", "api": "google", "model": "gemini-3-pro"},
    "stop_reason": "stop",
}


@pytest.mark.parametrize("payload", [ASSISTANT, TOOL_RESULT, USER_WITH_REF, REDACTED, UNSIGNED])
def test_persisted_shape_round_trips_exactly(payload):
    assert message_to_dict(message_from_dict(payload)) == payload


def test_assistant_message_exposes_tool_calls_in_order():
    message = message_from_dict(ASSISTANT)
    assert isinstance(message, AssistantMessage)
    assert [call.id for call in message.tool_calls] == ["toolu_01AbC"]


@pytest.mark.parametrize(
    "payload",
    [
        {**ASSISTANT, "cost": 1},
        {**TOOL_RESULT, "content": [{"type": "text", "text": "x", "lang": "en"}]},
        {**TOOL_RESULT, "content": [{"type": "thinking", "text": "x"}]},
        {**ASSISTANT, "stop_reason": "end_turn"},
        {**ASSISTANT, "origin": {"provider": "anthropic", "api": "anthropic-messages", "model": "m"}},
        {"role": "system", "content": []},
        {"role": "user", "content": [{"type": "text", "ref": {"ref": "sha256:" + "a" * 64, "bytes": 1, "enc": "gz"}}]},
        {**TOOL_RESULT, "is_error": "false"},
        {**TOOL_RESULT, "is_error": 0},
        {**REDACTED, "content": [{"type": "thinking", "text": "", "redacted": "yes"}]},
        {**ASSISTANT, "usage": {**ASSISTANT["usage"], "input_tokens": "1834"}},
        {**ASSISTANT, "usage": {**ASSISTANT["usage"], "output_tokens": True}},
        {**ASSISTANT, "content": [{"type": "tool_call", "id": "t", "name": "bash", "arguments": "[]"}]},
        {**ASSISTANT, "content": [{"type": "text", "text": 5}]},
        {**ASSISTANT, "content": "text"},
    ],
    ids=[
        "unknown-field", "unknown-block-field", "block-not-allowed", "stop-reason", "protocol", "role",
        "unknown-ref-field", "string-flag", "integer-flag", "string-redacted", "string-count", "boolean-count",
        "string-arguments", "number-text", "content-not-list",
    ],
)
def test_reader_refuses_shapes_it_does_not_know(payload):
    with pytest.raises(ValueError):
        message_from_dict(payload)


def test_text_block_holds_exactly_one_form():
    with pytest.raises(ValueError):
        TextBlock()
    with pytest.raises(ValueError):
        message_from_dict({"role": "user", "content": [{"type": "text", "text": "a", "ref": USER_WITH_REF["content"][0]["ref"]}]})
