"""The processing tier a response reports having served (stream_wire.ServedTierPaths, docs/plans/fast-mode.md).

A fast request is only confirmed by its response: Anthropic reports ``usage.speed``
and OpenAI ``service_tier``. A relay that drops the request field answers with a
standard tier or with none, and both must read as "not fast".
"""

from __future__ import annotations

import io
import json

import pytest

from core.handlers.model_hub.stream_wire import ProtocolSSEState, observe_buffered_protocol_response


def _frame(document: dict) -> bytes:
    named = f"event: {document['type']}\n".encode() if "type" in document else b""
    return named + b"data: " + json.dumps(document).encode() + b"\n\n"


def _streamed(protocol: str, *documents: dict | bytes) -> ProtocolSSEState:
    state = ProtocolSSEState(protocol)
    for document in documents:
        state.observe(document if isinstance(document, bytes) else _frame(document))
    return state


def _anthropic_stream(speed: str | None) -> tuple[dict, ...]:
    usage = {"output_tokens": 5, **({"speed": speed} if speed is not None else {})}
    return (
        {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage},
        {"type": "message_stop"},
    )


def _responses_stream(tier: str | None, echoed: str = "auto") -> tuple[dict, ...]:
    completed = {"status": "completed", "usage": {"input_tokens": 9, "output_tokens": 5}}
    if tier is not None:
        completed["service_tier"] = tier
    return (
        # The created event echoes what was asked; only the completed one says what ran.
        {"type": "response.created", "response": {"status": "in_progress", "service_tier": echoed}},
        {"type": "response.output_text.delta", "delta": "OK"},
        {"type": "response.completed", "response": completed},
    )


@pytest.mark.parametrize(
    ("speed", "expected"),
    [("fast", "fast"), ("standard", "standard"), (None, None)],
)
def test_anthropic_stream_reports_the_speed_it_served(speed, expected):
    state = _streamed("anthropic", *_anthropic_stream(speed))
    assert state.terminal_outcome == "served"
    assert state.served_tier == expected
    observation = state.terminal_observation()
    assert observation is not None and observation.served_tier == expected


@pytest.mark.parametrize(
    ("tier", "echoed", "expected"),
    [
        ("priority", "priority", "fast"),
        ("default", "priority", "standard"),
        ("flex", "auto", "standard"),
        # Only the completed response says what ran: a relay may echo the request
        # on the created event and then name no tier, which confirms none.
        (None, "priority", None),
    ],
)
def test_responses_stream_reports_the_tier_it_served(tier, echoed, expected):
    state = _streamed("openai_responses", *_responses_stream(tier, echoed))
    assert state.terminal_outcome == "served"
    assert state.served_tier == expected


@pytest.mark.parametrize(("tier", "expected"), [("priority", "fast"), ("default", "standard")])
def test_chat_stream_names_its_tier_on_every_chunk(tier, expected):
    state = _streamed(
        "openai_chat",
        {"choices": [{"delta": {"content": "OK"}}], "service_tier": tier},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "service_tier": tier},
        b"data: [DONE]\n\n",
    )
    assert state.terminal_outcome == "served"
    assert state.served_tier == expected


@pytest.mark.parametrize(
    ("protocol", "body", "expected"),
    [
        ("anthropic", {"type": "message", "content": [], "usage": {"output_tokens": 1, "speed": "fast"}}, "fast"),
        ("anthropic", {"type": "message", "content": [], "usage": {"output_tokens": 1}}, None),
        ("openai_responses", {"object": "response", "status": "completed", "output": [], "service_tier": "priority"},
         "fast"),
        ("openai_responses", {"object": "response", "status": "completed", "output": [], "service_tier": "default"},
         "standard"),
        ("openai_chat", {"object": "chat.completion", "choices": [], "service_tier": "priority"}, "fast"),
        # A relay that answers with another vendor's shape names nothing this reads.
        ("google", {"candidates": [{"finishReason": "STOP"}]}, None),
    ],
)
def test_buffered_response_reports_the_tier_it_served(protocol, body, expected):
    observation = observe_buffered_protocol_response(protocol, io.BytesIO(json.dumps(body).encode()))
    assert observation.outcome == "served"
    assert observation.served_tier == expected
