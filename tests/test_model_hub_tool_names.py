from __future__ import annotations

import json

from core.handlers.model_hub.stream_wire import SSEFrameTokenizer, parse_sse_frame
from core.handlers.model_hub.tool_names import (
    StreamingToolNameRewriter,
    rewrite_buffered_tool_names,
    translate_opencode_tool_names,
)


def test_opencode_tool_name_translation_covers_every_request_reference_without_mutation() -> None:
    request = {
        "model": "custom/claude-opus",
        "tools": [
            {
                "type": "function",
                "function": {"name": "todowrite", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "avibe_todo_write", "parameters": {"type": "object"}},
            },
        ],
        "tool_choice": {"type": "function", "function": {"name": "todowrite"}},
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "todowrite", "arguments": "{}"},
                    }
                ],
                "function_call": {"name": "todowrite", "arguments": "{}"},
            },
            {"role": "tool", "name": "todowrite", "tool_call_id": "call-1", "content": "ok"},
        ],
    }

    translation = translate_opencode_tool_names(request)

    translated = translation.request
    assert translated["tools"][0]["function"]["name"] == "avibe_todo_write_2"
    assert translated["tools"][1]["function"]["name"] == "avibe_todo_write"
    assert translated["tool_choice"]["function"]["name"] == "avibe_todo_write_2"
    assert translated["messages"][0]["tool_calls"][0]["function"]["name"] == "avibe_todo_write_2"
    assert translated["messages"][0]["function_call"]["name"] == "avibe_todo_write_2"
    assert translated["messages"][1]["name"] == "avibe_todo_write_2"
    assert translation.response_aliases == {"avibe_todo_write_2": "todowrite"}
    assert request["tools"][0]["function"]["name"] == "todowrite"
    assert request["messages"][0]["tool_calls"][0]["function"]["name"] == "todowrite"


def test_opencode_tool_name_translation_matches_reserved_names_case_insensitively() -> None:
    request = {
        "tools": [
            {"type": "function", "function": {"name": "TodoWrite"}},
            {"type": "function", "function": {"name": "AVIBE_TODO_WRITE"}},
        ],
        "messages": [
            {"role": "system", "name": "TodoWrite", "content": "participant"},
            {"role": "function", "name": "TodoWrite", "content": "result"},
        ],
    }

    translation = translate_opencode_tool_names(request)

    assert translation.request["tools"][0]["function"]["name"] == "avibe_todo_write_2"
    assert translation.request["messages"][0]["name"] == "TodoWrite"
    assert translation.request["messages"][1]["name"] == "avibe_todo_write_2"
    assert translation.response_aliases == {"avibe_todo_write_2": "TodoWrite"}


def test_buffered_chat_response_restores_only_tool_name_fields() -> None:
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "avibe_todo_write remains ordinary content",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "avibe_todo_write", "arguments": "{}"},
                            }
                        ],
                        "function_call": {"name": "avibe_todo_write", "arguments": "{}"},
                    }
                }
            ]
        }
    ).encode()

    rewritten = json.loads(rewrite_buffered_tool_names(payload, {"avibe_todo_write": "todowrite"}))

    message = rewritten["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "todowrite"
    assert message["function_call"]["name"] == "todowrite"
    assert message["content"] == "avibe_todo_write remains ordinary content"


def test_large_buffered_chat_response_restores_tool_name_without_loading_content() -> None:
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "x" * (512 * 1024),
                        "tool_calls": [
                            {"function": {"name": "avibe_todo_write", "arguments": "{}"}}
                        ],
                    }
                }
            ]
        },
        separators=(",", ":"),
    ).encode()

    rewritten = json.loads(
        rewrite_buffered_tool_names(payload, {"avibe_todo_write": "todowrite"})
    )

    message = rewritten["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "todowrite"
    assert len(message["content"]) == 512 * 1024


def test_streaming_chat_response_restores_alias_across_transport_chunks() -> None:
    first = b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"avibe_todo_write"}}]}}]}\r\n\r\n'
    second = b'data: {"choices":[{"delta":{"content":"avibe_todo_write"}}]}\n\n'
    terminal = b"data: [DONE]\n\n"
    wire = first + second + terminal
    rewriter = StreamingToolNameRewriter({"avibe_todo_write": "todowrite"})

    output = b"".join(
        (
            rewriter.feed(wire[:17]),
            rewriter.feed(wire[17:71]),
            rewriter.feed(wire[71:]),
            rewriter.finish(),
        )
    )

    frames = SSEFrameTokenizer().feed(output)
    payloads = [parse_sse_frame(frame)[1] for frame in frames]
    assert len(payloads) == 3
    first_payload = json.loads(payloads[0])
    assert first_payload["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "todowrite"
    assert json.loads(payloads[1])["choices"][0]["delta"]["content"] == "avibe_todo_write"
    assert payloads[2] == b"[DONE]"


def test_streaming_chat_response_reassembles_semantic_tool_name_deltas() -> None:
    first = (
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":'
        b'[{"index":2,"function":{"name":"avibe_todo"}}]}}]}\n\n'
    )
    second = (
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":'
        b'[{"index":2,"function":{"name":"_write","arguments":"{}"}}]}}]}\n\n'
    )
    rewriter = StreamingToolNameRewriter({"avibe_todo_write": "todowrite"})

    assert rewriter.feed(first) == b""
    output = rewriter.feed(second) + rewriter.feed(b"data: [DONE]\n\n")

    frames = SSEFrameTokenizer().feed(output)
    payloads = [parse_sse_frame(frame)[1] for frame in frames]
    first_payload = json.loads(payloads[0])
    second_payload = json.loads(payloads[1])
    first_function = first_payload["choices"][0]["delta"]["tool_calls"][0]["function"]
    second_function = second_payload["choices"][0]["delta"]["tool_calls"][0]["function"]
    assert first_function["name"] == "todowrite"
    assert "name" not in second_function
    assert second_function["arguments"] == "{}"
    assert payloads[2] == b"[DONE]"


def test_streaming_rewriter_drains_unterminated_wire_bytes_without_loss() -> None:
    rewriter = StreamingToolNameRewriter({"avibe_todo_write": "todowrite"})
    partial = b'data: {"choices":[]}'

    assert rewriter.feed(partial) == b""
    assert rewriter.finish() == partial


def test_streaming_rewriter_does_not_reject_large_content_frames() -> None:
    payload = {
        "choices": [
            {
                "index": 0,
                "delta": {"content": "x" * (2 * 1024 * 1024)},
            }
        ]
    }
    frame = b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"
    rewriter = StreamingToolNameRewriter({"avibe_todo_write": "todowrite"})

    assert rewriter.feed(frame) == frame
    assert rewriter.finish() == b""


def test_streaming_rewriter_resumes_after_partial_alias_state_exceeds_its_budget() -> None:
    partial = (
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":'
        b'[{"index":0,"function":{"name":"avibe_todo"}}]}}]}\n\n'
    )
    content = (
        b'data: {"choices":[{"index":1,"delta":{"content":"'
        + b"x" * (512 * 1024)
        + b'"}}]}\n\n'
    )
    aliased = (
        b'data: {"choices":[{"index":2,"delta":{"tool_calls":'
        b'[{"index":0,"function":{"name":"avibe_todo_write"}}]}}]}\n\n'
    )
    terminal = b"data: [DONE]\n\n"
    rewriter = StreamingToolNameRewriter({"avibe_todo_write": "todowrite"})

    output = (
        rewriter.feed(partial)
        + rewriter.feed(content)
        + rewriter.feed(aliased)
        + rewriter.feed(terminal)
    )

    frames = SSEFrameTokenizer().feed(output)
    payloads = [parse_sse_frame(frame)[1] for frame in frames]
    assert frames[0] + b"\n\n" == partial
    assert frames[1] + b"\n\n" == content
    assert json.loads(payloads[2])["choices"][0]["delta"]["tool_calls"][0]["function"][
        "name"
    ] == "todowrite"
    assert payloads[3] == b"[DONE]"
    assert rewriter.finish() == b""


def test_streaming_rewriter_resumes_after_cr_frame_exceeds_budget() -> None:
    partial = b"data: " + b"x" * (256 * 1024) + b"\r"
    aliased = (
        b'data: {"choices":[{"delta":{"function_call":'
        b'{"name":"avibe_todo_write"}}}]}\r\r'
    )
    remainder = b"\r" + aliased + b"data: [DONE]\r\r"
    rewriter = StreamingToolNameRewriter({"avibe_todo_write": "todowrite"})

    output = rewriter.feed(partial) + rewriter.feed(remainder) + rewriter.finish()

    assert output.startswith(partial + b"\r")
    frames = SSEFrameTokenizer().feed(output)
    payloads = [parse_sse_frame(frame)[1] for frame in frames]
    assert json.loads(payloads[1])["choices"][0]["delta"]["function_call"]["name"] == "todowrite"
    assert payloads[2] == b"[DONE]"
