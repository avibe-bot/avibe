"""Focused contracts for exact native Model Hub opaque replay provenance."""

from __future__ import annotations

import copy
import json

import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.capability_evidence import identity_digest
from core.handlers.model_hub.opaque_provenance import (
    OpaqueProvenanceRegistry,
    filter_opaque_history,
)
from core.handlers.model_hub.provenance import (
    BoundedProvenanceStore,
    HopOrigin,
    PreparedGatewayRoute,
    TurnCorrelationRegistry,
)
from core.handlers.model_hub.request import ModelHubRequest

from tests.test_model_hub_l3 import _outcome, _service, _source


@pytest.fixture
def registry() -> OpaqueProvenanceRegistry:
    return OpaqueProvenanceRegistry(max_entries=8, ttl_seconds=3600)


@pytest.mark.parametrize(
    ("backend", "request_scoped", "turn_id"),
    [("claude", False, "native-turn"), ("codex", True, None), ("opencode", True, None)],
)
def test_native_route_metadata_recovers_identity_without_wire_identity(
    tmp_path, backend, request_scoped, turn_id,
) -> None:
    """MH-OPAQUE-ROUTE-001: native route metadata carries identity in Avibe memory only."""

    correlation = TurnCorrelationRegistry(BoundedProvenanceStore(tmp_path / f"{backend}-turns.json"))
    origin = HopOrigin("openai", "openai_responses", "upstream", "source-a", "sha256:a")
    token = correlation.credentials(
        backend, f"{backend}-process", turn_id, request_scoped=request_scoped,
    )
    route = PreparedGatewayRoute(
        requested_model_id="menu-model",
        resolved_model_id="upstream",
        source_id="source-a",
        gateway_request_model_id="menu-model",
        primary_identity=origin.identity,
    )
    launch_token = correlation.prepare_gateway_turn(
        backend=backend,
        token=token,
        turn_id=turn_id,
        requested_model_id=route.requested_model_id,
        resolved_model_id=route.resolved_model_id,
        source_id=route.source_id,
        gateway_request_model_id=route.gateway_request_model_id,
        via_mapping=False,
        primary_identity=origin.identity,
    )
    gateway_token = token if request_scoped else launch_token
    metadata = correlation.gateway_request_metadata(
        backend=backend, token=gateway_token, turn_id=None, route=route,
    )

    assert correlation.gateway_primary_origin(
        backend=backend, token=gateway_token, request_metadata=metadata,
    ) is None
    if request_scoped:
        assert metadata["avibe_turn_id"] == ""
        explicit = correlation._explicit_route(
            backend=backend, token=gateway_token, request_metadata=metadata,
        )
        assert explicit is not None and explicit[2].primary_identity == origin.identity
    else:
        credential = correlation._credential(backend, gateway_token)
        assert credential is not None and credential.route is not None
        assert credential.route.primary_identity == origin.identity
        assert metadata == {}
    assert origin.identity not in origin.payload()
    assert origin.identity not in json.dumps(metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "protocol", "vendor", "history_key"),
    [
        ("claude", "anthropic", "anthropic", "messages"),
        ("codex", "openai_responses", "openai", "input"),
        ("opencode", "openai_chat", "openai", "messages"),
    ],
)
async def test_native_final_admission_rechecks_source_identity(
    tmp_path, backend, protocol, vendor, history_key,
) -> None:
    """MH-OPAQUE-ADMISSION-002: the final gate filters every native Hub backend."""

    source = _source("source-a", "A", vendor=vendor, protocol=protocol)
    value = f"native-opaque-{backend}"
    if protocol == "anthropic":
        history = [{
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "visible", "signature": value}],
        }]
    elif protocol == "openai_responses":
        history = [{
            "type": "reasoning",
            "id": "rs-1",
            "encrypted_content": value,
            "summary": [{"type": "summary_text", "text": "visible"}],
        }]
    else:
        history = [{
            "role": "assistant",
            "reasoning_details": [{"type": "reasoning", "signature": value}],
        }]
    payload = {history_key: history}

    same_service = _service(
        tmp_path / "same",
        sources=[source],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS, source_id=source.id)],
    )
    same_service.opaque_provenance.observe(
        protocol, value, identity_digest(source),
    )
    same_request = ModelHubRequest(copy.deepcopy(payload), protocol=protocol)
    await same_service.resolve(
        backend=backend, model_id="shared-model", request=same_request, stream=False,
    )
    assert same_service.adapter.requests[-1] == payload
    assert same_request == payload

    unknown_service = _service(
        tmp_path / "unknown",
        sources=[source],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS, source_id=source.id)],
    )
    unknown_request = ModelHubRequest(copy.deepcopy(payload), protocol=protocol)
    await unknown_service.resolve(
        backend=backend, model_id="shared-model", request=unknown_request, stream=False,
    )
    filtered = unknown_service.adapter.requests[-1]
    assert value not in json.dumps(filtered)
    assert unknown_request == payload


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic", "openai_chat"])
def test_same_identity_foreign_unknown_and_ambiguous_values_are_filtered(
    registry: OpaqueProvenanceRegistry, protocol: str,
) -> None:
    """MH-OPAQUE-ADMISSION-001: only one known producing identity may replay opaque data."""

    value = f"opaque-{protocol}"
    registry.observe(protocol, value, "A")
    request = {
        "input" if protocol == "openai_responses" else "messages": (
            [{"type": "reasoning", "encrypted_content": value, "summary": []}]
            if protocol == "openai_responses"
            else ([{"role": "assistant", "content": [{"type": "thinking", "thinking": "visible", "signature": value}]}]
                  if protocol == "anthropic"
                  else [{"role": "assistant", "reasoning_details": [{"type": "reasoning", "signature": value}]}])
        ),
    }
    original = json.loads(json.dumps(request))

    same = filter_opaque_history(request, protocol=protocol, identity="A", registry=registry)
    foreign = filter_opaque_history(request, protocol=protocol, identity="B", registry=registry)
    unknown = filter_opaque_history(request, protocol=protocol, identity="C", registry=OpaqueProvenanceRegistry())

    assert same == request
    assert foreign != request
    assert unknown == filter_opaque_history(request, protocol=protocol, identity="B", registry=registry)
    assert request == original

    registry.observe(protocol, value, "B")
    ambiguous = filter_opaque_history(request, protocol=protocol, identity="A", registry=registry)
    assert ambiguous == unknown


def test_a_to_b_to_a_and_credential_rotation_keep_only_matching_values(
    registry: OpaqueProvenanceRegistry,
) -> None:
    """MH-OPAQUE-ADMISSION-002: nonlinear fallback and rotation remain identity-exact."""

    registry.observe("openai_responses", "a-value", "A")
    registry.observe("openai_responses", "b-value", "B")
    request = {
        "input": [
            {"type": "reasoning", "encrypted_content": "a-value", "summary": []},
            {"type": "reasoning", "encrypted_content": "b-value", "summary": []},
        ]
    }
    assert filter_opaque_history(request, protocol="openai_responses", identity="A", registry=registry)["input"] == [
        request["input"][0]
    ]
    assert filter_opaque_history(request, protocol="openai_responses", identity="B", registry=registry)["input"] == [
        request["input"][1]
    ]
    registry.observe("openai_responses", "a-new-after-rotation", "A2")
    rotated = filter_opaque_history(request, protocol="openai_responses", identity="A2", registry=registry)
    assert rotated["input"] == []


def test_bounded_eviction_and_process_restart_are_unknown(registry: OpaqueProvenanceRegistry) -> None:
    """MH-OPAQUE-REGISTRY-001: restart/eviction loses trust instead of retaining ciphertext."""

    small = OpaqueProvenanceRegistry(max_entries=1, ttl_seconds=3600)
    small.observe("anthropic", "first", "A")
    small.observe("anthropic", "second", "A")
    assert not small.accepts("anthropic", "first", "A")
    assert small.accepts("anthropic", "second", "A")
    small.observe("anthropic", "second", "B")
    small.observe("anthropic", "second", "C")
    assert not small.accepts("anthropic", "second", "B")
    assert not small.accepts("anthropic", "second", "C")
    assert not OpaqueProvenanceRegistry().accepts("anthropic", "second", "A")


def test_unknown_chat_encrypted_detail_is_removed_without_readable_fallback() -> None:
    """Rejected encrypted reasoning shells must not reach the upstream adapter."""

    payload = {
        "messages": [
            {
                "role": "assistant",
                "reasoning_details": [
                    {
                        "type": "reasoning.encrypted",
                        "id": "foreign-detail",
                        "format": "opaque-v1",
                        "data": "foreign-ciphertext",
                        "index": 3,
                    }
                ],
            }
        ]
    }

    assert filter_opaque_history(payload, protocol="openai_chat", identity="B", registry=OpaqueProvenanceRegistry()) == {
        "messages": [{"role": "assistant", "reasoning_details": []}]
    }


def test_rejected_chat_encrypted_detail_keeps_readable_text() -> None:
    payload = {
        "messages": [
            {
                "role": "assistant",
                "reasoning_details": [
                    {
                        "type": "reasoning.encrypted",
                        "id": "foreign-detail",
                        "data": "foreign-ciphertext",
                        "text": "portable reasoning",
                        "summary": "portable summary",
                    }
                ],
            }
        ]
    }

    assert filter_opaque_history(payload, protocol="openai_chat", identity="B", registry=OpaqueProvenanceRegistry()) == {
        "messages": [
            {
                "role": "assistant",
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "portable reasoning"},
                    {"type": "reasoning.summary", "summary": "portable summary"},
                ],
            }
        ]
    }


def test_buffered_and_streaming_observation_commit_only_complete_responses(
    registry: OpaqueProvenanceRegistry,
) -> None:
    """MH-OPAQUE-OBSERVE-001: response observation is complete-only and body-bounded."""

    buffered = registry.begin_response("openai_responses", "A")
    buffered.observe_buffered(
        b'{"output":[{"type":"reasoning","id":"r1","encrypted_content":"buffered"}]}'
    )
    buffered.finish(complete=True)
    assert registry.accepts("openai_responses", "buffered", "A")

    buffered_many = registry.begin_response("openai_responses", "A")
    buffered_many.observe_buffered(
        b'{"output":['
        b'{"type":"reasoning","encrypted_content":"buffered-first"},'
        b'{"type":"reasoning","encrypted_content":"buffered-second"}'
        b']}'
    )
    buffered_many.finish(complete=True)
    assert registry.accepts("openai_responses", "buffered-first", "A")
    assert registry.accepts("openai_responses", "buffered-second", "A")

    incomplete = registry.begin_response("openai_responses", "A")
    incomplete.feed(
        b'event: response.output_item.done\ndata: '
        b'{"type":"response.output_item.done","item":{"type":"reasoning","encrypted_content":"partial"}}\n\n'
    )
    incomplete.finish(complete=False)
    assert not registry.accepts("openai_responses", "partial", "A")

    responses_stream = registry.begin_response("openai_responses", "A")
    responses_stream.feed(
        b'event: response.output_item.done\ndata: '
        b'{"type":"response.output_item.done","item":{"type":"reasoning","encrypted_content":"streamed-responses"}}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
    )
    responses_stream.finish(complete=True)
    assert registry.accepts("openai_responses", "streamed-responses", "A")

    streaming = registry.begin_response("anthropic", "A")
    streaming.feed(
        b'event: content_block_delta\r\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig-"}}\r\n\r\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"stream"}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    streaming.finish(complete=True)
    assert registry.accepts("anthropic", "sig-stream", "A")

    chat_stream = registry.begin_response("openai_chat", "A")
    chat_stream.feed(
        b'data: {"choices":[{"delta":{"reasoning_details":[{"signature":"chat-sig"}]}}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning_details":[{"signature":"chat-sig-2"}]}}]}\n\n'
        b'data: [DONE]\n\n'
    )
    chat_stream.finish(complete=True)
    assert registry.accepts("openai_chat", "chat-sig", "A")
    assert registry.accepts("openai_chat", "chat-sig-2", "A")

    oversized = registry.begin_response("anthropic", "A")
    oversized.feed(
        b'event: content_block_start\ndata: '
        + json.dumps({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "redacted_thinking", "data": "x" * (512 * 1024 + 1)},
        }).encode()
        + b"\n\n"
        + b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
    )
    oversized.finish(complete=True)
    assert not registry.accepts("anthropic", "x" * (512 * 1024 + 1), "A")


def test_filter_preserves_visible_tool_data_schemas_and_user_signature() -> None:
    """MH-OPAQUE-SHAPE-001: filtering never mistakes ordinary user/tool data for provider opacity."""

    registry = OpaqueProvenanceRegistry()
    request = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "visible summary", "signature": "unknown"},
                    {"type": "text", "text": "answer", "signature": "user-data"},
                ],
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "verify", "arguments": '{"signature":"user-data"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ],
        "tools": [{"type": "function", "function": {"name": "verify", "parameters": {"signature": {"type": "string"}}}}],
    }
    filtered = filter_opaque_history(
        request, protocol="anthropic", identity="B", registry=registry,
    )
    assert filtered["messages"][0]["content"] == [
        {"type": "text", "text": "visible summary"},
        {"type": "text", "text": "answer", "signature": "user-data"},
    ]
    assert filtered["messages"][0]["tool_calls"][0]["function"]["arguments"] == '{"signature":"user-data"}'
    assert filtered["messages"][1] == request["messages"][1]
    assert filtered["tools"] == request["tools"]


def test_tool_call_opaque_metadata_is_exact_and_independent_of_arguments() -> None:
    """MH-OPAQUE-SHAPE-002: tool arguments survive while known extension signatures follow provenance."""

    registry = OpaqueProvenanceRegistry()
    registry.observe("openai_chat", "chat-known", "A")
    request = {
        "messages": [{
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-known",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"signature":"user-data"}'},
                    "extra_content": {"google": {"thought_signature": "chat-known"}},
                },
                {
                    "id": "call-foreign",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"signature":"foreign-argument"}'},
                    "extra_content": {"google": {"thought_signature": "foreign-signature"}},
                },
            ],
        }],
    }
    filtered = filter_opaque_history(
        request, protocol="openai_chat", identity="A", registry=registry,
    )
    assert filtered == {
        "messages": [{
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-known",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"signature":"user-data"}'},
                    "extra_content": {"google": {"thought_signature": "chat-known"}},
                },
                {
                    "id": "call-foreign",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"signature":"foreign-argument"}'},
                    "extra_content": {"google": {}},
                },
            ],
        }],
    }
    assert request["messages"][0]["tool_calls"][1]["extra_content"]["google"]["thought_signature"] == "foreign-signature"


def test_google_model_part_signature_is_filtered_without_touching_nested_user_data() -> None:
    """MH-OPAQUE-SHAPE-003: every model Part owns its direct signature field."""

    registry = OpaqueProvenanceRegistry()
    registry.observe("google", "google-known", "A")
    registry.observe("google", "google-foreign", "B")
    registry.observe("google", "google-ambiguous", "A")
    registry.observe("google", "google-ambiguous", "B")
    request = {
        "contents": [
            {
                "role": "model",
                "parts": [
                    {"text": "visible", "thoughtSignature": "google-known"},
                    {"text": "", "thoughtSignature": "google-foreign"},
                    {"inlineData": {"mimeType": "image/png", "data": "image-data"}, "thoughtSignature": "google-ambiguous"},
                    {"functionCall": {"name": "echo", "args": {"signature": "user-data"}}, "thoughtSignature": "unknown-signature"},
                ],
            },
            {
                "role": "user",
                "parts": [{
                    "functionResponse": {"name": "echo", "response": {"signature": "user-data"}},
                    "thoughtSignature": "user-data",
                }],
            },
        ],
    }
    filtered = filter_opaque_history(
        request, protocol="google", identity="A", registry=registry,
    )
    assert filtered == {
        "contents": [
            {
                "role": "model",
                "parts": [
                    {"text": "visible", "thoughtSignature": "google-known"},
                    {"text": ""},
                    {"inlineData": {"mimeType": "image/png", "data": "image-data"}},
                    {"functionCall": {"name": "echo", "args": {"signature": "user-data"}}},
                ],
            },
            {
                "role": "user",
                "parts": [{
                    "functionResponse": {"name": "echo", "response": {"signature": "user-data"}},
                    "thoughtSignature": "user-data",
                }],
            },
        ],
    }


def test_buffered_observation_uses_exact_protocol_paths_and_block_types() -> None:
    """MH-OPAQUE-OBSERVE-002: response fields certify only their owning protocol blocks."""

    registry = OpaqueProvenanceRegistry()

    anthropic = registry.begin_response("anthropic", "A")
    anthropic.observe_buffered(json.dumps({
        "content": [
            {"type": "tool_use", "input": {"content": [{"data": "tool-argument"}]}},
            {"type": "redacted_thinking", "data": "anthropic-opaque"},
        ],
    }).encode())
    anthropic.finish(complete=True)

    responses = registry.begin_response("openai_responses", "A")
    responses.observe_buffered(json.dumps({
        "output": [
            {"type": "function_call", "encrypted_content": "function-field"},
            {"type": "reasoning", "encrypted_content": "responses-opaque"},
        ],
    }).encode())
    responses.finish(complete=True)

    chat = registry.begin_response("openai_chat", "A")
    chat.observe_buffered(json.dumps({
        "choices": [{"message": {"tool_calls": [{
            "extra_content": {"google": {"thought_signature": "chat-tool-opaque"}},
        }]}}],
    }).encode())
    chat.finish(complete=True)

    google = registry.begin_response("google", "A")
    google.observe_buffered(json.dumps({
        "candidates": [
            {"content": {"role": "model", "parts": [
                {"text": "Visible answer", "thoughtSignature": "google-text-opaque"},
                {"text": "", "thoughtSignature": "google-empty-opaque"},
                {"inlineData": {"mimeType": "image/png", "data": "synthetic-image-data"}, "thoughtSignature": "google-image-opaque"},
                {"functionCall": {"name": "echo", "args": {}}, "thoughtSignature": "google-call-opaque"},
            ]}},
            {"content": {"role": "user", "parts": [
                {"functionResponse": {"name": "echo", "response": {}}, "thoughtSignature": "google-user-field"},
            ]}},
        ],
    }).encode())
    google.finish(complete=True)

    chat_stream = registry.begin_response("openai_chat", "A")
    chat_stream.feed(b'data: {"choices":[{"delta":{"tool_calls":[{"extra_content":{"google":{"thought_signature":"chat-stream-opaque"}}}]}}]}\n\n')
    chat_stream.feed(b'data: [DONE]\n\n')
    chat_stream.finish(complete=True)

    google_stream = registry.begin_response("google", "A")
    google_stream.feed(('data: ' + json.dumps({
        "candidates": [{"content": {"role": "model", "parts": [
            {"text": "Visible answer", "thoughtSignature": "google-stream-text"},
            {"text": "", "thoughtSignature": "google-stream-empty"},
            {"inlineData": {"mimeType": "image/png", "data": "stream-image-data"}, "thoughtSignature": "google-stream-image"},
            {"functionCall": {"name": "echo", "args": {}}, "thoughtSignature": "google-stream-call"},
        ]}}],
    }) + '\n\n').encode())
    google_stream.finish(complete=True)

    assert registry.accepts("anthropic", "anthropic-opaque", "A")
    assert not registry.accepts("anthropic", "tool-argument", "A")
    assert registry.accepts("openai_responses", "responses-opaque", "A")
    assert not registry.accepts("openai_responses", "function-field", "A")
    assert registry.accepts("openai_chat", "chat-tool-opaque", "A")
    assert registry.accepts("openai_chat", "chat-stream-opaque", "A")
    assert registry.accepts("google", "google-text-opaque", "A")
    assert registry.accepts("google", "google-empty-opaque", "A")
    assert registry.accepts("google", "google-image-opaque", "A")
    assert registry.accepts("google", "google-call-opaque", "A")
    assert registry.accepts("google", "google-stream-text", "A")
    assert registry.accepts("google", "google-stream-empty", "A")
    assert registry.accepts("google", "google-stream-image", "A")
    assert registry.accepts("google", "google-stream-call", "A")
    assert not registry.accepts("google", "google-user-field", "A")


def test_buffered_anthropic_observation_commits_only_the_item_owned_field() -> None:
    registry = OpaqueProvenanceRegistry()
    observer = registry.begin_response("anthropic", "A")
    observer.observe_buffered(json.dumps({
        "content": [
            {"type": "thinking", "signature": "thinking-signature", "data": "thinking-data"},
            {"type": "redacted_thinking", "signature": "redacted-signature", "data": "redacted-data"},
        ],
    }).encode())
    observer.finish(complete=True)

    assert registry.accepts("anthropic", "thinking-signature", "A")
    assert not registry.accepts("anthropic", "thinking-data", "A")
    assert not registry.accepts("anthropic", "redacted-signature", "A")
    assert registry.accepts("anthropic", "redacted-data", "A")


def test_responses_orphan_reasoning_is_rerendered_as_visible_summary() -> None:
    """MH-OPAQUE-CONTINUATION-001: unsafe reasoning ids are removed, not left malformed."""

    request = {
        "input": [
            {
                "type": "reasoning",
                "id": "rs-foreign",
                "encrypted_content": "foreign",
                "summary": [{"type": "summary_text", "text": "Visible summary"}],
            },
            {"type": "function_call", "call_id": "call-1", "name": "verify", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
        ]
    }
    filtered = filter_opaque_history(
        request,
        protocol="openai_responses",
        identity="B",
        registry=OpaqueProvenanceRegistry(),
    )
    assert filtered["input"][0] == {
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Visible summary"}],
    }
    assert filtered["input"][1:] == request["input"][1:]
