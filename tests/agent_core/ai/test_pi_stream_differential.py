"""Recorded stream projections compared with Pi 7fbbd5f.

The expected Pi projections were produced by driving Pi's native handlers in
an isolated temporary checkout with credential-free stub transports. This
test keeps the recorded wire sequences and the canonical projection in the
Avibe repository so adapter changes cannot silently drift from that audit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.openai_chat import OpenAIChatAdapter
from core.agent_core.ai.openai_responses import OpenAIResponsesAdapter
from core.agent_core.ai.provider import Done, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    Usage,
    UserMessage,
)


def _request(protocol: str) -> ModelRequest:
    provider = {
        "anthropic": "anthropic",
        "openai_chat": "openai",
        "openai_responses": "openai",
    }[protocol]
    return ModelRequest(
        endpoint=ModelEndpoint(
            protocol=protocol,  # type: ignore[arg-type]
            base_url="https://model.test/v1",
            model_id="model-x",
            token="token",
            provider=provider,
        ),
        system="system",
        messages=(UserMessage((TextBlock(text="hello"),)),),
        tools=(),
        max_tokens=128,
        reasoning_effort=None,
    )


def _sse(*events: Mapping[str, Any]) -> str:
    return "".join(f"data: {json.dumps(dict(event), separators=(',', ':'))}\n\n" for event in events)


def _anthropic_sse(*events: Mapping[str, Any]) -> str:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(dict(event), separators=(',', ':'))}\n\n"
        for event in events
    )


def _project_content(message: AssistantMessage) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            projected.append({"type": "text", "text": block.text})
        elif isinstance(block, ThinkingBlock):
            projected.append(
                {
                    "type": "thinking",
                    "text": block.text,
                    "signature": block.signature,
                    "redacted": block.redacted,
                }
            )
        elif isinstance(block, ToolCallBlock):
            projected.append(
                {
                    "type": "tool",
                    "id": block.id,
                    "native_id": block.native_id,
                    "name": block.name,
                    "arguments": block.arguments,
                    "signature": block.signature,
                }
            )
    return projected


def _project_usage(usage: Usage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_tokens,
        "reasoning": usage.reasoning_tokens,
    }


def _project(events: list[Any]) -> dict[str, Any]:
    terminal = events[-1]
    if isinstance(terminal, Done):
        return {
            "outcome": "done",
            "stop_reason": terminal.message.stop_reason,
            "content": _project_content(terminal.message),
            "usage": _project_usage(terminal.message.usage),
        }
    assert isinstance(terminal, ProviderError)
    partial = terminal.partial
    return {
        "outcome": "error",
        "kind": terminal.kind,
        "retryable": terminal.retryable,
        "partial": (
            {
                "stop_reason": partial.stop_reason,
                "content": _project_content(partial),
                "usage": _project_usage(partial.usage),
            }
            if partial is not None
            else None
        ),
    }


_CASES = (
    {
        "name": "responses_output_slots_arrive_in_order",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "reasoning", "id": "rs_1"},
            },
            {"type": "response.reasoning_text.delta", "output_index": 0, "delta": "plan"},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "message", "id": "msg_1"},
            },
            {"type": "response.output_text.delta", "output_index": 1, "delta": "answer"},
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "enc",
                    "summary": [{"type": "summary_text", "text": "plan"}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "rs_1",
                            "encrypted_content": "enc",
                            "summary": [{"type": "summary_text", "text": "plan"}],
                        },
                        {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "answer"}]},
                    ],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 3,
                        "output_tokens_details": {"reasoning_tokens": 1},
                    },
                },
            },
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [
                {
                    "type": "thinking",
                    "text": "plan",
                    "signature": '{"type":"reasoning","id":"rs_1","encrypted_content":"enc","summary":[{"type":"summary_text","text":"plan"}]}',
                    "redacted": False,
                },
                {"type": "text", "text": "answer"},
            ],
            "usage": {"input": 2, "output": 3, "cache_read": 0, "cache_write": 0, "reasoning": 1},
        },
    },
    {
        "name": "responses_unknown_event_is_ignored",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {"type": "response.future_event", "new_field": True},
            {"type": "response.completed", "response": {"status": "completed"}},
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [],
            "usage": None,
        },
    },
    {
        "name": "responses_missing_output_index_shares_pi_slot",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}},
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "read",
                },
            },
            {"type": "response.function_call_arguments.delta", "delta": '{"path":"x"}'},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "read",
                    "arguments": '{"path":"x"}',
                },
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "text": "", "signature": None, "redacted": False},
                {
                    "type": "tool",
                    "id": "call_1|fc_1",
                    "native_id": None,
                    "name": "read",
                    "arguments": {"path": "x"},
                    "signature": None,
                },
            ],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "text": "", "signature": None, "redacted": False},
                {
                    "type": "tool",
                    "id": "call_1",
                    "native_id": "fc_1",
                    "name": "read",
                    "arguments": {"path": "x"},
                    "signature": None,
                },
            ],
            "usage": None,
        },
    },
    {
        "name": "responses_terminal_message_snapshot_is_not_adopted",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": "msg_1"}},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_1",
                            "content": [{"type": "output_text", "text": "terminal"}],
                        }
                    ],
                },
            },
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [{"type": "text", "text": ""}],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [{"type": "text", "text": ""}],
            "usage": None,
        },
    },
    {
        "name": "chat_reasoning_details_merge",
        "adapter": OpenAIChatAdapter,
        "protocol": "openai_chat",
        "body": _sse(
            {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "a"}]}}]},
            {
                "choices": [
                    {
                        "delta": {"reasoning_details": [{"type": "reasoning.text", "text": "b"}]},
                        "finish_reason": "stop",
                    }
                ]
            },
            {"choices": []},
            {"choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}},
        )
        + "data: [DONE]\n\n",
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [
                {
                    "type": "thinking",
                    "text": "",
                    "signature": '[{"type":"reasoning.text","text":"ab"}]',
                    "redacted": False,
                }
            ],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [
                {
                    "type": "thinking",
                    "text": "",
                    "signature": '[{"type":"reasoning.text","text":"ab"}]',
                    "redacted": False,
                }
            ],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
    },
    {
        "name": "chat_error_after_text_keeps_partial",
        "adapter": OpenAIChatAdapter,
        "protocol": "openai_chat",
        "body": _sse(
            {"choices": [{"delta": {"content": "partial"}}]},
            {"error": {"type": "server_error", "message": "failed"}},
        ),
        "pi": {
            "outcome": "error",
            "kind": "server",
            "retryable": False,
            "partial": {
                "stop_reason": "error",
                "content": [{"type": "text", "text": "partial"}],
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
            },
        },
        "avibe": {
            "outcome": "error",
            "kind": "server",
            "retryable": False,
            "partial": {
                "stop_reason": "error",
                "content": [{"type": "text", "text": "partial"}],
                "usage": None,
            },
        },
    },
    {
        "name": "anthropic_message_start_stop_reason_survives_eof",
        "adapter": AnthropicAdapter,
        "protocol": "anthropic",
        "body": _anthropic_sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ),
        "pi": {
            "outcome": "error",
            "kind": "unknown",
            "retryable": False,
            "partial": {
                "stop_reason": "error",
                "content": [],
                "usage": {"input": 7, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
            },
        },
        "avibe": {
            "outcome": "error",
            "kind": "network",
            "retryable": True,
            "partial": {
                "stop_reason": "error",
                "content": [],
                "usage": {"input": 7, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
            },
        },
    },
    {
        "name": "anthropic_unknown_event_is_ignored",
        "adapter": AnthropicAdapter,
        "protocol": "anthropic",
        "body": _anthropic_sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
            {"type": "future_event", "payload": {"new": "field"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [],
            "usage": {"input": 4, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
    },
    {
        "name": "responses_terminal_refusal_is_contract_deviation",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": "msg_1"}},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_1",
                            "content": [{"type": "refusal", "refusal": "cannot help"}],
                        }
                    ],
                },
            },
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [{"type": "text", "text": ""}],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "refusal",
            "content": [{"type": "text", "text": "cannot help"}],
            "usage": None,
        },
    },
    {
        "name": "responses_orphan_refusal_delta_is_ignored",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {"type": "response.refusal.delta", "output_index": 3, "delta": "cannot help"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [],
            "usage": None,
        },
    },
    {
        "name": "chat_late_tool_id_uses_stable_canonical_id",
        "adapter": OpenAIChatAdapter,
        "protocol": "openai_chat",
        "body": _sse(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "read"}}]}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "provider-call", "function": {"arguments": '{"path":"x"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
        + "data: [DONE]\n\n",
        "pi": {
            "outcome": "done",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool",
                    "id": "provider-call",
                    "native_id": None,
                    "name": "read",
                    "arguments": {"path": "x"},
                    "signature": None,
                }
            ],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool",
                    "id": "call_0",
                    "native_id": "provider-call",
                    "name": "read",
                    "arguments": {"path": "x"},
                    "signature": None,
                }
            ],
            "usage": None,
        },
    },
    {
        "name": "responses_consecutive_missing_output_indexes_rebind",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_a", "call_id": "call_a", "name": "read"},
            },
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_b", "call_id": "call_b", "name": "read"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_a",
                "delta": '{"path":"a"}',
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_b",
                "delta": '{"path":"b"}',
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_a",
                    "call_id": "call_a",
                    "name": "read",
                    "arguments": '{"path":"a"}',
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_b",
                    "call_id": "call_b",
                    "name": "read",
                    "arguments": '{"path":"b"}',
                },
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool",
                    "id": "call_a|fc_a",
                    "native_id": None,
                    "name": "read",
                    "arguments": {"path": "a"},
                    "signature": None,
                },
                {
                    "type": "tool",
                    "id": "call_b|fc_b",
                    "native_id": None,
                    "name": "read",
                    "arguments": {"path": "b"},
                    "signature": None,
                },
            ],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool",
                    "id": "call_a",
                    "native_id": "fc_a",
                    "name": "read",
                    "arguments": {"path": "a"},
                    "signature": None,
                },
                {
                    "type": "tool",
                    "id": "call_b",
                    "native_id": "fc_b",
                    "name": "read",
                    "arguments": {"path": "b"},
                    "signature": None,
                },
            ],
            "usage": None,
        },
    },
    {
        "name": "responses_empty_slots_ignore_late_deltas",
        "adapter": OpenAIResponsesAdapter,
        "protocol": "openai_responses",
        "body": _sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "message", "id": "msg_1"},
            },
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "reasoning", "id": "rs_1"},
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {"type": "message", "id": "msg_1", "content": []},
            },
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {"type": "reasoning", "id": "rs_1", "summary": []},
            },
            {"type": "response.output_text.delta", "output_index": 0, "delta": "late text"},
            {"type": "response.reasoning_text.delta", "output_index": 1, "delta": "late thinking"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [
                {"type": "text", "text": ""},
                {"type": "thinking", "text": "", "signature": None, "redacted": False},
            ],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [
                {"type": "text", "text": ""},
                {"type": "thinking", "text": "", "signature": None, "redacted": False},
            ],
            "usage": None,
        },
    },
    {
        "name": "chat_finish_reason_end_maps_to_stop",
        "adapter": OpenAIChatAdapter,
        "protocol": "openai_chat",
        "body": _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "end"}]})
        + "data: [DONE]\n\n",
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [{"type": "text", "text": "ok"}],
            "usage": None,
        },
    },
    {
        "name": "anthropic_null_delta_usage_preserves_message_start",
        "adapter": AnthropicAdapter,
        "protocol": "anthropic",
        "body": _anthropic_sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"input_tokens": None, "output_tokens": None},
            },
            {"type": "message_stop"},
        ),
        "pi": {
            "outcome": "done",
            "stop_reason": "stop",
            "content": [],
            "usage": {"input": 7, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
    },
    {
        "name": "chat_malformed_tool_arguments",
        "adapter": OpenAIChatAdapter,
        "protocol": "openai_chat",
        "body": _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "read", "arguments": "{"}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
        + "data: [DONE]\n\n",
        "pi": {
            "outcome": "done",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool",
                    "id": "call_1",
                    "native_id": None,
                    "name": "read",
                    "arguments": {},
                    "signature": None,
                }
            ],
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
        },
        "avibe": {
            "outcome": "error",
            "kind": "invalid_request",
            "retryable": False,
            "partial": {"stop_reason": "error", "content": [], "usage": None},
        },
    },
    {
        "name": "chat_eof_after_text_is_partial_abort",
        "adapter": OpenAIChatAdapter,
        "protocol": "openai_chat",
        "body": _sse({"choices": [{"delta": {"content": "partial"}}]}),
        "pi": {
            "outcome": "error",
            "kind": "unknown",
            "retryable": False,
            "partial": {
                "stop_reason": "error",
                "content": [{"type": "text", "text": "partial"}],
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
            },
        },
        "avibe": {
            "outcome": "error",
            "kind": "network",
            "retryable": False,
            "partial": {
                "stop_reason": "error",
                "content": [{"type": "text", "text": "partial"}],
                "usage": None,
            },
        },
    },
    {
        "name": "anthropic_empty_block_end_then_error",
        "adapter": AnthropicAdapter,
        "protocol": "anthropic",
        "body": _anthropic_sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
        ),
        "pi": {
            "outcome": "error",
            "kind": "overloaded",
            "retryable": False,
            "partial": {
                "stop_reason": "error",
                "content": [{"type": "text", "text": ""}],
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
            },
        },
        "avibe": {
            "outcome": "error",
            "kind": "overloaded",
            "retryable": True,
            "partial": None,
        },
    },
    {
        "name": "anthropic_malformed_delta",
        "adapter": AnthropicAdapter,
        "protocol": "anthropic",
        "body": _anthropic_sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": None},
        ),
        "pi": {
            "outcome": "error",
            "kind": "unknown",
            "retryable": False,
            "partial": {
                "stop_reason": "error",
                "content": [{"type": "text", "text": ""}],
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": None},
            },
        },
        "avibe": {
            "outcome": "error",
            "kind": "invalid_request",
            "retryable": False,
            "partial": None,
        },
    },
)

_CONTRACT_DEVIATIONS = {
    "responses_unknown_event_is_ignored",
    "responses_missing_output_index_shares_pi_slot",
    "responses_terminal_message_snapshot_is_not_adopted",
    "chat_reasoning_details_merge",
    "chat_error_after_text_keeps_partial",
    "responses_terminal_refusal_is_contract_deviation",
    "responses_orphan_refusal_delta_is_ignored",
    "chat_late_tool_id_uses_stable_canonical_id",
    "responses_consecutive_missing_output_indexes_rebind",
    "responses_empty_slots_ignore_late_deltas",
    "chat_finish_reason_end_maps_to_stop",
    "chat_malformed_tool_arguments",
    "chat_eof_after_text_is_partial_abort",
    "anthropic_message_start_stop_reason_survives_eof",
    "anthropic_empty_block_end_then_error",
    "anthropic_malformed_delta",
}


def _assert_projection_shape(projection: Mapping[str, Any]) -> None:
    assert set(projection) >= {"outcome"}
    if projection["outcome"] == "done":
        assert set(projection) >= {"stop_reason", "content", "usage"}
        usage = projection["usage"]
    else:
        assert set(projection) >= {"kind", "retryable", "partial"}
        partial = projection["partial"]
        usage = partial["usage"] if partial is not None else None
    if usage is not None:
        assert set(usage) == {"input", "output", "cache_read", "cache_write", "reasoning"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
async def test_recorded_pi_stream_projections(case: Mapping[str, Any]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=case["body"],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = case["adapter"](client)
        events = [
            event async for event in adapter.stream(_request(case["protocol"]), CancelToken())
        ]

    _assert_projection_shape(case["pi"])
    if "avibe" in case:
        assert case["name"] in _CONTRACT_DEVIATIONS
        _assert_projection_shape(case["avibe"])
        assert case["avibe"] != case["pi"]
    expected = case.get("avibe", case["pi"])
    assert _project(events) == expected
