"""Anthropic Messages streaming adapter.

Payload and stream rules are ported from tau's ``tau_ai/anthropic.py`` (MIT)
and Pi's ``packages/ai/src/api/anthropic-messages.ts`` (MIT). This module uses
httpx directly so Model Hub remains the only gateway boundary.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any

import httpx

from core.agent_core.ai.claude_thinking import is_claude_model, uses_adaptive_thinking
from core.agent_core.ai.hosted import anthropic_hosted_block, citation, reported_counts
from core.agent_core.ai.image_limits import LoadedImage
from core.agent_core.ai._common import (
    ServedHopResolver,
    WireRequest,
    auth_headers,
    content_parts,
    drive_sse_stream,
    dispatch_wire_event,
    endpoint_origin,
    hosted_pin,
    PARTIAL_ERROR_MESSAGE_CHARS,
    PAUSE_TURN,
    PIN_SOURCE_HEADER,
    PinUnavailable,
    REFUSED,
    pin_refusal,
    join_endpoint_url,
    json_object,
    native_declaration,
    native_hosted_tools,
    prepare_messages,
    request_credential_values,
    StreamAssembler,
    WireField,
    WireDispatchError,
    usage_counter_error,
    validate_wire_shape,
)
from core.agent_core.ai.provider import (
    Done,
    ModelEndpoint,
    ModelRequest,
    ProviderAdapter,
    ProviderError,
    MediaLoader,
)
from core.agent_core.ai.search_recovery import search_replay_refusal
from core.agent_core.cancel import CancelToken
from core.agent_core.hosted_continuations import pending_hosted
from core.agent_core.ai.transform import in_flight_position, native_replay
from core.agent_core.messages import (
    MAX_HOSTED_COUNT,
    AssistantMessage,
    HostedToolBlock,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
#: How many times one response may be continued after the provider paused it (``vibey-hosted-tools.md`` §4.3).
MAX_PAUSE_CONTINUATIONS = 5
PAUSE_LIMIT_MESSAGE = f"the provider paused the turn more than {MAX_PAUSE_CONTINUATIONS} times"
#: A paused response continues only on the Source that served it, pinned there (§4.3 as implemented).
PAUSE_UNSERVED_MESSAGE = "the Source that served the paused turn cannot continue it"
#: A continuation goes out only as the first send plus the paused response exactly as served (§4.3).
PAUSE_ALTERED_MESSAGE = "the paused turn cannot be sent back as it was served"
_MAX_CACHE_BREAKPOINTS = 4
# A Claude route caches its static prefix, the tools and then the system prompt, for an hour (``_prefix_ttl``); the
# conversation's breakpoints keep the default five minutes. Anthropic evaluates breakpoints as tools, system, messages
# and requires every longer lifetime to come before every shorter one, which this placement satisfies.
_PREFIX_TTL = "1h"
# Routes whose endpoint rejected the field. They keep the default lifetime for the rest of the process.
_TTL_REJECTED_ROUTES: set[tuple[str, str | None, str, str]] = set()
_TTL_REJECTION = re.compile(r"cache_control|\bttl\b", re.IGNORECASE)
_LEGACY_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 16384,
    "max": 16384,
}
_KNOWN_STREAM_EVENTS = frozenset(
    {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "error",
        "message_stop",
        "ping",
    }
)
_ANTHROPIC_USAGE_FIELDS = (
    WireField("input_tokens", int),
    WireField("output_tokens", int),
    WireField("cache_read_input_tokens", int),
    WireField("cache_creation_input_tokens", int),
    WireField(
        "cache_creation",
        Mapping,
        children=(
            WireField("ephemeral_5m_input_tokens", int),
            WireField("ephemeral_1h_input_tokens", int),
        ),
    ),
    WireField(
        "output_tokens_details",
        Mapping,
        children=(WireField("thinking_tokens", int),),
    ),
)
_ANTHROPIC_WIRE_SHAPES = {
    "message_start": (
        WireField(
            "message",
            Mapping,
            required=True,
            nullable=False,
            children=(
                WireField("usage", Mapping, children=_ANTHROPIC_USAGE_FIELDS),
            ),
        ),
    ),
    "content_block_start": (
        WireField("index", int, required=True, nullable=False),
        WireField(
            "content_block",
            Mapping,
            required=True,
            nullable=False,
            children=(
                WireField("type", str, required=True, nullable=False),
                WireField("id", str),
                WireField("name", str),
                WireField("signature", str),
                WireField("data", str),
                WireField("text", str),
                WireField("thinking", str),
                WireField("input", Mapping),
            ),
        ),
    ),
    "content_block_delta": (
        WireField("index", int, required=True, nullable=False),
        WireField(
            "delta",
            Mapping,
            required=True,
            nullable=False,
            children=(
                WireField("type", str, required=True, nullable=False),
                WireField("text", str),
                WireField("thinking", str),
                WireField("signature", str),
                WireField("partial_json", str),
            ),
        ),
    ),
    "content_block_stop": (
        WireField("index", int, required=True, nullable=False),
    ),
    "message_delta": (
        WireField(
            "delta",
            Mapping,
            children=(WireField("stop_reason", str),),
        ),
        WireField("usage", Mapping, children=_ANTHROPIC_USAGE_FIELDS),
    ),
    "error": (
        WireField(
            "error",
            Mapping,
            children=(
                WireField("type", str),
                WireField("message", str),
            ),
        ),
    ),
    "message_stop": (),
    "ping": (),
}


class AnthropicAdapter(ProviderAdapter):
    """Adapter for the native Anthropic Messages protocol."""

    protocol = "anthropic"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        media_loader: MediaLoader | None = None,
        served_hop_resolver: ServedHopResolver | None = None,
        gateway: bool = False,
    ) -> None:
        # The Model Hub gateway is loopback: an environment or system proxy must never carry it.
        self._client = client or httpx.AsyncClient(timeout=None, trust_env=not gateway)
        self._owns_client = client is None
        self._media_loader = media_loader
        self._served_hop_resolver = served_hop_resolver
        self._gateway = gateway

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[Any]:
        return self._stream(request, cancel)

    async def _stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[Any]:
        origin = endpoint_origin(request.endpoint)

        def new_assembler() -> StreamAssembler:
            return StreamAssembler(
                origin=origin,
                protocol=self.protocol,
                verified_origin=not self._gateway,
                endpoint_url=request.endpoint.base_url,
                sensitive_values=request_credential_values(request.endpoint),
            )

        assembler = new_assembler()
        if cancel.cancelled:
            terminal = assembler.terminal(assembler.aborted(cancel.reason))
            if terminal is not None:
                yield terminal
            return
        target = origin
        prepared = await prepare_messages(
            request.messages,
            target=target,
            supports_images=request.supports_images,
            protocol=self.protocol,
            media_loader=self._media_loader,
            cancel=cancel,
            endpoint=request.endpoint,
        )
        if isinstance(prepared, ProviderError):
            terminal = assembler.terminal(prepared)
            if terminal is not None:
                yield terminal
            return
        transformed_messages, loaded_images = prepared
        if cancel.cancelled:
            terminal = assembler.terminal(assembler.aborted(cancel.reason))
            if terminal is not None:
                yield terminal
            return
        stop_reason: str | None = None
        # Why the provider refused, from ``message_delta.delta.stop_details``: the response's ``error_message``.
        refusal_detail: str | None = None
        message_started = False
        message_stopped = False
        # The hosted tools this request declared natively; ``server_tool_use`` blocks of other tools stay unparsed.
        hosted = frozenset(native_hosted_tools(request))
        # Each hosted call's ``server_tool_use`` by stream index; its input JSON streams into the assembler.
        uses: dict[int, dict[str, Any]] = {}
        # A mixed group crosses a committed response boundary. Carry only calls actually sent natively on this hop;
        # their result-only fragments belong to this response, never a rewrite of the earlier pending row.
        live = in_flight_position(request.messages)
        previous = request.messages[live] if live is not None else None
        deferred = (
            pending_hosted(previous)
            if isinstance(previous, AssistantMessage)
            and native_replay(previous, in_flight=True, endpoint=request.endpoint, target=target, protocol=self.protocol)
            else {}
        )
        # A paused response continues as the same one (§4.3): a continuation's blocks follow the paused ones, its hosted
        # counters add to theirs, and its usage, the final request's, stands for the whole response.
        paused = False
        offset = next_index = 0
        response_usage: Usage | None = None
        # Exact totals, unbounded: a counter past the bound in any response stays past it however later ones add.
        carried_counts: dict[str, int] = {}
        response_counts: dict[str, int] = {}

        def block_index(chunk: Mapping[str, Any]) -> int | None:
            index = _stream_index(chunk)
            return None if index is None else index + offset

        try:
            async def translate(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
                nonlocal message_started, message_stopped, stop_reason, paused, next_index, response_usage
                nonlocal response_counts, refusal_detail
                async for event in events:
                    if cancel.cancelled:
                        terminal = assembler.terminal(assembler.aborted(cancel.reason))
                        if terminal is not None:
                            yield terminal
                        return
                    if event.data == "[DONE]" or not event.data:
                        continue
                    if event.event == "ping":
                        continue
                    if event.event == "error":
                        # Pi 7fbbd5f iterateAnthropicEvents handles named
                        # errors before JSON parsing; these may be plain text.
                        terminal = assembler.terminal(assembler.error(event.data))
                        if terminal is not None:
                            yield terminal
                        return
                    if event.event is not None and event.event not in _KNOWN_STREAM_EVENTS:
                        # Pi filters by the SSE event name before parsing its
                        # JSON payload, so unknown event names are ignored
                        # even when their data is malformed.
                        continue
                    chunk = json_object(event.data)
                    if chunk is None:
                        # Pi ignores data-only frames because Anthropic event
                        # dispatch is keyed by the SSE event name. Avibe
                        # accepts valid data-only JSON for gateway
                        # compatibility, but malformed data-only frames are
                        # still ignored rather than misclassified as provider
                        # payload errors.
                        if event.event is None:
                            continue
                        terminal = assembler.terminal(
                            assembler.error("Provider returned invalid Anthropic JSON", kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    try:
                        event_type = dispatch_wire_event(
                            chunk,
                            known=_KNOWN_STREAM_EVENTS,
                            event_name=event.event,
                        )
                    except WireDispatchError as exc:
                        terminal = assembler.terminal(
                            assembler.error(str(exc), kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if event_type is None:
                        continue
                    shape_error = validate_wire_shape(
                        chunk,
                        event_type,
                        _ANTHROPIC_WIRE_SHAPES,
                    )
                    if shape_error is not None:
                        terminal = assembler.terminal(
                            assembler.error(shape_error, kind="invalid_request")
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    if event_type == "message_start":
                        message_started = True
                        message = chunk.get("message")
                        if not isinstance(message, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("message_start message must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if "usage" in message and not isinstance(message.get("usage"), Mapping):
                            terminal = assembler.terminal(
                                assembler.error("message_start usage must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        usage_error = usage_counter_error(
                            message.get("usage"),
                            label="message_start usage",
                            fields=(
                                "input_tokens",
                                "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                            ),
                            nested_fields={
                                "cache_creation": ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
                                "output_tokens_details": ("thinking_tokens",),
                            },
                        )
                        if usage_error is not None:
                            terminal = assembler.terminal(assembler.error(usage_error, kind="invalid_request"))
                            if terminal is not None:
                                yield terminal
                            return
                        # Each response's own usage: the last request's input already holds every paused part, so
                        # it is the context the merged response stands for (plan §4.3 as implemented).
                        response_usage = _anthropic_usage(message.get("usage"))
                        assembler.set_usage(response_usage)
                        start_usage = message.get("usage")
                        if isinstance(start_usage, Mapping) and "server_tool_use" in start_usage:
                            response_counts = reported_counts(start_usage.get("server_tool_use"), bound=None)
                            assembler.set_hosted_facts(counts=_bounded(_summed(carried_counts, response_counts)))
                    elif event_type == "content_block_start":
                        index = block_index(chunk)
                        if index is None:
                            terminal = assembler.terminal(
                                assembler.error("content_block_start index must be an integer", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        next_index = max(next_index, index + 1)
                        block = chunk.get("content_block")
                        if not isinstance(block, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("content_block must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        kind = block.get("type")
                        if not isinstance(kind, str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "content_block type must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if kind == "fallback":
                            if assembler.has_content():
                                terminal = assembler.terminal(
                                    assembler.exception(
                                        RuntimeError(
                                            "Anthropic performed an unsupported mid-output model fallback"
                                        )
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            continue
                        if kind == "server_tool_use":
                            hosted_call = block.get("name") in hosted and isinstance(block.get("id"), str) and block["id"]
                            if hosted_call and assembler.source_id is not None:
                                # The call keeps its place in the stream; its input streams in and its result follows.
                                assembler.set_block_kind(index, "hosted_use")
                                uses[index] = dict(block)
                                assembler.hosted_block(
                                    ("hosted", block.get("id")),
                                    anthropic_hosted_block(block, None, source_id=assembler.source_id),
                                )
                            continue
                        if kind == "web_search_tool_result":
                            use = next((item for item in uses.values() if item.get("id") == block.get("tool_use_id")), None)
                            prior = deferred.get(block.get("tool_use_id"))
                            if use is None and prior is not None and prior.native.source_id == assembler.source_id:
                                use = prior.native.payload[0]
                                completed = anthropic_hosted_block(use, block, source_id=assembler.source_id)
                                completed = replace(completed, native=replace(completed.native, payload=(dict(block),)))
                            elif use is not None and assembler.source_id is not None:
                                completed = anthropic_hosted_block(use, block, source_id=assembler.source_id)
                            else:
                                continue
                            assembler.set_block_kind(index, "hosted_result")
                            assembler.hosted_block(("hosted", use.get("id")), completed)
                            continue
                        if kind == "text":
                            assembler.set_block_kind(index, "text")
                            assembler.ensure_text_slot(index)
                            for raw in block.get("citations") or ():
                                cited = citation(raw) if isinstance(raw, Mapping) else None
                                if cited is not None:
                                    assembler.add_text_citation(index, cited)
                            initial = block.get("text")
                            if initial is not None and not isinstance(initial, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "text content must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if initial:
                                emitted = assembler.text_delta(index, initial)
                                if emitted is not None:
                                    yield emitted
                        elif kind == "thinking":
                            raw_signature = block.get("signature")
                            if raw_signature is not None and not isinstance(raw_signature, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "thinking signature must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            signature = raw_signature
                            signature = signature if isinstance(signature, str) and signature else None
                            assembler.set_block_kind(index, "thinking")
                            assembler.ensure_thinking_slot(index, signature=signature)
                            initial = block.get("thinking")
                            if initial is not None and not isinstance(initial, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "thinking content must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if initial:
                                emitted = assembler.thinking_delta(index, initial)
                                if emitted is not None:
                                    yield emitted
                        elif kind == "redacted_thinking":
                            signature = block.get("data")
                            if not isinstance(signature, str) or not signature:
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "redacted_thinking data must be a non-empty string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            assembler.redacted_thinking(index, signature)
                            assembler.set_block_kind(index, "redacted")
                        elif kind == "tool_use":
                            raw_id = block.get("id")
                            if raw_id is not None and not isinstance(raw_id, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "tool_use id must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            raw_name = block.get("name")
                            if raw_name is not None and not isinstance(raw_name, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "tool_use name must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            native_id = _string(raw_id) or None
                            name = _string(raw_name)
                            if not name:
                                terminal = assembler.terminal(
                                    assembler.error("tool_use name must not be empty", kind="invalid_request")
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            assembler.set_block_kind(index, "tool")
                            start = assembler.tool_start(index, name=name, native_id=native_id)
                            if start is not None:
                                yield start
                            initial_input = block.get("input")
                            if initial_input is not None and not isinstance(initial_input, Mapping):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "tool_use input must be an object",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            if isinstance(initial_input, Mapping):
                                assembler.set_tool_arguments_object(index, initial_input)
                    elif event_type == "content_block_delta":
                        index = block_index(chunk)
                        if index is None:
                            terminal = assembler.terminal(
                                assembler.error("content_block_delta index must be an integer", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        delta = chunk.get("delta")
                        if not isinstance(delta, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("content_block_delta delta must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        delta_type = delta.get("type")
                        if not isinstance(delta_type, str):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "content_block_delta type must be a string",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        block_kind = assembler.block_kind(index)
                        if block_kind is None:
                            # Pi ignores content deltas for block kinds it
                            # does not know yet; preserve that forward-
                            # compatible policy instead of inventing text.
                            continue
                        if delta_type == "text_delta":
                            if block_kind != "text":
                                continue
                            raw_text = delta.get("text")
                            if "text" in delta and not isinstance(raw_text, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "text_delta text must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_text)
                            if value:
                                emitted = assembler.text_delta(index, value)
                                if emitted is not None:
                                    yield emitted
                        elif delta_type == "thinking_delta":
                            if block_kind != "thinking":
                                continue
                            raw_thinking = delta.get("thinking")
                            if "thinking" in delta and not isinstance(raw_thinking, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "thinking_delta thinking must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_thinking)
                            if value:
                                emitted = assembler.thinking_delta(index, value)
                                if emitted is not None:
                                    yield emitted
                        elif delta_type == "signature_delta":
                            if block_kind != "thinking":
                                continue
                            raw_signature = delta.get("signature")
                            if "signature" in delta and not isinstance(raw_signature, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "signature_delta signature must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_signature)
                            if value:
                                assembler.append_thinking_signature(index, value)
                        elif delta_type == "citations_delta":
                            raw_citation = delta.get("citation")
                            cited = citation(raw_citation) if isinstance(raw_citation, Mapping) else None
                            if block_kind == "text" and cited is not None:
                                assembler.add_text_citation(index, cited)
                        elif delta_type == "input_json_delta" and block_kind == "hosted_use":
                            if isinstance(delta.get("partial_json"), str):
                                assembler.hosted_input_delta(index, delta["partial_json"])
                        elif delta_type == "input_json_delta":
                            if block_kind != "tool":
                                continue
                            raw_partial_json = delta.get("partial_json")
                            if "partial_json" in delta and not isinstance(raw_partial_json, str):
                                terminal = assembler.terminal(
                                    assembler.error(
                                        "input_json_delta partial_json must be a string",
                                        kind="invalid_request",
                                    )
                                )
                                if terminal is not None:
                                    yield terminal
                                return
                            value = _string(raw_partial_json)
                            emitted = assembler.tool_arguments(index, value)
                            if emitted is not None:
                                yield emitted
                        # Pi ignores delta variants it does not know yet.
                        # Known event shape has already been validated above.
                    elif event_type == "content_block_stop":
                        index = block_index(chunk)
                        if index is None:
                            terminal = assembler.terminal(
                                assembler.error("content_block_stop index must be an integer", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        streamed_input = assembler.hosted_input(index) if assembler.block_kind(index) == "hosted_use" else ""
                        if streamed_input:
                            use = uses[index]
                            parsed = json_object(streamed_input)
                            use["input"] = parsed if parsed is not None else use.get("input") or {}
                            current = assembler.hosted_block_at(("hosted", use.get("id")))
                            if current is not None and current.record.status == "pending":
                                assembler.hosted_block(
                                    ("hosted", use.get("id")),
                                    anthropic_hosted_block(use, None, source_id=current.native.source_id),
                                )
                        end = assembler.block_end(index)
                        if end is not None:
                            yield end
                    elif event_type == "message_delta":
                        delta = chunk.get("delta")
                        if "delta" in chunk and not isinstance(delta, Mapping):
                            terminal = assembler.terminal(
                                assembler.error("message_delta delta must be an object", kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        usage = chunk.get("usage")
                        if usage is not None and not isinstance(usage, Mapping):
                            terminal = assembler.terminal(
                                assembler.error(
                                    "message_delta usage must be an object",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        usage_error = usage_counter_error(
                            usage,
                            label="message_delta usage",
                            fields=(
                                "input_tokens",
                                "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                            ),
                            nested_fields={
                                "cache_creation": ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
                                "output_tokens_details": ("thinking_tokens",),
                            },
                        )
                        if usage_error is not None:
                            terminal = assembler.terminal(assembler.error(usage_error, kind="invalid_request"))
                            if terminal is not None:
                                yield terminal
                            return
                        if isinstance(delta, Mapping):
                            if delta.get("stop_reason") is not None:
                                if not isinstance(delta.get("stop_reason"), str):
                                    terminal = assembler.terminal(
                                        assembler.error(
                                            "message_delta stop_reason must be a string",
                                            kind="invalid_request",
                                        )
                                    )
                                    if terminal is not None:
                                        yield terminal
                                    return
                                paused = delta.get("stop_reason") == "pause_turn"
                                stop_reason = _normalize_stop_reason(delta.get("stop_reason"))
                                refusal_detail = (
                                    _refusal_detail(delta.get("stop_details")) if stop_reason == "refusal" else None
                                )
                        response_usage = _anthropic_usage(usage, current=response_usage)
                        assembler.set_usage(response_usage)
                        if isinstance(usage, Mapping) and "server_tool_use" in usage:
                            response_counts = reported_counts(usage.get("server_tool_use"), bound=None)
                            assembler.set_hosted_facts(counts=_bounded(_summed(carried_counts, response_counts)))
                    elif event_type == "error":
                        shape_error = _error_event_shape_error(chunk)
                        if shape_error is not None:
                            terminal = assembler.terminal(
                                assembler.error(shape_error, kind="invalid_request")
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        terminal = assembler.terminal(
                            assembler.error(
                                json.dumps(dict(chunk), ensure_ascii=False),
                            )
                        )
                        if terminal is not None:
                            yield terminal
                        return
                    elif event_type == "message_stop":
                        message_stopped = True
                        if stop_reason is None:
                            terminal = assembler.terminal(
                                assembler.error(
                                    "Anthropic stream ended without a stop reason",
                                    kind="invalid_request",
                                )
                            )
                            if terminal is not None:
                                yield terminal
                            return
                        if paused:
                            yield PAUSE_TURN
                            return
                        final = assembler.finalize(
                            stop_reason or ("tool_use" if assembler.has_tools() else "stop"),
                            error_message=refusal_detail,
                        )
                        terminal = assembler.terminal(final if isinstance(final, ProviderError) else Done(final))
                        if terminal is not None:
                            yield terminal
                        return
                    elif event_type == "ping":
                        continue
                if cancel.cancelled:
                    terminal = assembler.terminal(assembler.aborted(cancel.reason))
                    if terminal is not None:
                        yield terminal
                    return
                if message_started and not message_stopped:
                    terminal = assembler.terminal(assembler.incomplete())
                    if terminal is not None:
                        yield terminal
                    return
                if stop_reason is not None:
                    if paused:
                        yield PAUSE_TURN
                        return
                    final = assembler.finalize(stop_reason, error_message=refusal_detail)
                    terminal = assembler.terminal(
                        final if isinstance(final, ProviderError) else Done(final)
                    )
                    if terminal is not None:
                        yield terminal
                    return
                terminal = assembler.terminal(assembler.incomplete())
                if terminal is not None:
                    yield terminal
            sending = request
            continuations = 0
            # The first send's prepared messages and images: every continuation must send exactly them plus the
            # response so far, as served (§4.3).
            first_sent: tuple[tuple[Any, ...], Mapping[str, LoadedImage]] | None = None
            while True:
                # The lifetime ``wire_request`` gives the prefix: the default once this route has rejected the field.
                prefix_ttl = _prefix_ttl(sending)
                wire = wire_request(sending, transformed_messages, gateway=self._gateway, loaded_images=loaded_images)
                driver = drive_sse_stream(
                    self._client,
                    method="POST",
                    url=wire.url,
                    json_body=wire.body,
                    headers=wire.headers,
                    cancel=cancel,
                    assembler=assembler,
                    endpoint=sending.endpoint,
                    resolver=self._served_hop_resolver,
                    gateway=self._gateway,
                    translate=translate,
                    # The endpoint refused the lifetime before answering: sent again below, with nothing lost.
                    refused=_rejects_ttl if prefix_ttl is not None else None,
                )
                rejected = continued = paused = message_started = message_stopped = False
                unavailable: PinUnavailable | None = None
                stop_reason = refusal_detail = None
                try:
                    async for event in driver:
                        if isinstance(event, PinUnavailable):
                            unavailable = event
                            break
                        if event is PAUSE_TURN:
                            continued = True
                            break
                        if event is REFUSED:
                            rejected = True
                            break
                        if isinstance(event, ProviderError):
                            if event.status == 400:
                                _log_refused_request_shape(sending, wire.body, continuations)
                            repair = search_replay_refusal(event, sending, wire.body)
                            if repair != event.refusal:
                                event = replace(event, refusal=repair)
                            if repair is not None:
                                # The Agent owns bounds and retries of uncommitted output. The adapter only
                                # identifies a changed request, equally for HTTP refusals and streamed errors.
                                event = replace(event, retryable=False, continuable=False, after_thinking=False)
                        yield event
                finally:
                    await driver.aclose()
                if unavailable is not None and continuations == 0:
                    # The pinned Source cannot serve this content; nothing reached a model. The loop renders the
                    # request again for a fresh hop (§3.5).
                    terminal = assembler.terminal(pin_refusal(assembler, request.endpoint, unavailable))
                    if terminal is not None:
                        yield terminal
                    return
                elif unavailable is not None or (
                    continued
                    and self._gateway
                    and (PIN_SOURCE_HEADER not in wire.headers or not assembler.verified_origin)
                ):
                    # A paused response continues only on the Source that served it, pinned, so it stays one origin's
                    # message: a continuation that Source cannot serve, a send no pin held there, or a response whose
                    # Source is unknown (no served-hop report, so its hosted calls could not be kept and sent back) ends
                    # the response with what it has (§4.3 as implemented).
                    terminal = assembler.terminal(assembler.error(PAUSE_UNSERVED_MESSAGE, kind="interrupted"))
                    if terminal is not None:
                        yield terminal
                    return
                elif continued:
                    # The provider paused the turn: send its content back as it is, with no new user message, pinned to
                    # the Source and model that served it, and keep streaming the same response, at most
                    # MAX_PAUSE_CONTINUATIONS times (§4.3).
                    if continuations == MAX_PAUSE_CONTINUATIONS:
                        terminal = assembler.terminal(assembler.error(PAUSE_LIMIT_MESSAGE, kind="interrupted"))
                        if terminal is not None:
                            yield terminal
                        return
                    so_far = assembler.pause()
                    if isinstance(so_far, ProviderError):
                        terminal = assembler.terminal(so_far)
                        if terminal is not None:
                            yield terminal
                        return
                    if first_sent is None:
                        first_sent = (transformed_messages, loaded_images)
                    continuations += 1
                    response_usage = None
                    carried_counts, response_counts = _summed(carried_counts, response_counts), {}
                    offset = next_index
                    sending = replace(sending, messages=(*request.messages, so_far))
                elif rejected:
                    # The endpoint refused the lifetime before answering, so nothing reached the consumer: the same
                    # send once more with the default lifetime, which this route keeps from now on.
                    _remember_ttl_rejection(sending.endpoint)
                    continue
                else:
                    return
                hosted = frozenset(native_hosted_tools(sending))
                prepared = await prepare_messages(
                    sending.messages,
                    target=origin,
                    supports_images=sending.supports_images,
                    protocol=self.protocol,
                    media_loader=self._media_loader,
                    cancel=cancel,
                    endpoint=sending.endpoint,
                )
                if isinstance(prepared, ProviderError):
                    # A paused response keeps what it streamed, as a failed continuation request does.
                    terminal = assembler.terminal(replace(prepared, partial=prepared.partial or assembler.partial()))
                    if terminal is not None:
                        yield terminal
                    return
                transformed_messages, loaded_images = prepared
                if first_sent is not None and _binding_prefix(
                    sending, transformed_messages, loaded_images
                ) != _binding_prefix(request, (*first_sent[0], so_far), first_sent[1]):
                    # The provider continues the paused response from exactly what it sent and served: anything this
                    # render changed (a hosted call or thinking the Session can no longer send back, an earlier
                    # message rendered differently) would make it a different request, or an assistant prefill.
                    # The response ends with what it has instead (§4.3).
                    terminal = assembler.terminal(assembler.error(PAUSE_ALTERED_MESSAGE, kind="interrupted"))
                    if terminal is not None:
                        yield terminal
                    return
        except Exception as exc:
            terminal = assembler.terminal(assembler.exception(exc))
            if terminal is not None:
                yield terminal


def _binding_prefix(
    request: ModelRequest, messages: tuple[Any, ...], loaded_images: Mapping[str, LoadedImage] | None
) -> dict[str, Any]:
    """What a Messages request's replayed content is bound to: its ``system``, ``tools`` and ``messages``, without
    cache markers, which are not part of it. ``messages`` go through the payload builder unchanged, so an untransformed
    response among them renders exactly as served."""

    payload = build_messages_payload(replace(request, cache="none"), messages, loaded_images=loaded_images)
    return {key: payload.get(key) for key in ("system", "tools", "messages")}


def _block_types(content: Any) -> list[str]:
    if not isinstance(content, list):
        return ["text"]
    return [str(block.get("type")) if isinstance(block, Mapping) else "?" for block in content]


def _log_refused_request_shape(request: ModelRequest, body: Mapping[str, Any], continuation: int) -> None:
    """Log the structure of a request the provider refused with HTTP 400, never its content.

    A refusal that depends on how the conversation ends (an assistant prefill, a tool pairing) cannot be diagnosed
    from the error alone. Roles and block types, counts, and the hop name the shape without any text, id, or input.
    """

    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    last = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "assistant"), None)
    after = messages[last + 1 :] if last is not None else []
    tail = " ".join(f"{message.get('role')}[{','.join(_block_types(message.get('content')))}]" for message in messages[-4:])
    thinking = body.get("thinking")
    logger.warning(
        "Anthropic refused a request with HTTP 400: source=%s model=%s continuation=%s messages=%s thinking=%s "
        "tail=%s results_only_after_assistant=%s",
        request.endpoint.source_id,
        request.endpoint.model_id,
        continuation,
        len(messages),
        thinking.get("type") if isinstance(thinking, Mapping) else None,
        tail,
        bool(after) and all(set(_block_types(message.get("content"))) == {"tool_result"} for message in after),
    )


def _continues_unthought_tool_loop(messages: list[dict[str, Any]]) -> bool:
    """Whether a request continues a tool loop (tool results follow its last assistant message) whose assistant message
    does not start with a thinking block, which Anthropic refuses under manual thinking. Anthropic combines consecutive
    user messages into one turn, so a user message after the results (a new prompt, a checkpoint's) still follows the
    tool loop the rule names."""

    last = next((index for index in range(len(messages) - 1, -1, -1) if messages[index]["role"] == "assistant"), None)
    if last is None:
        return False
    continues = any(
        isinstance(block, Mapping) and block.get("type") == "tool_result"
        for message in messages[last + 1 :]
        if isinstance(message["content"], list)
        for block in message["content"]
    )
    content = messages[last]["content"]
    first = content[0] if isinstance(content, list) and content else None
    return continues and not (isinstance(first, Mapping) and first.get("type") in ("thinking", "redacted_thinking"))


def build_messages_payload(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    loaded_images: Mapping[str, LoadedImage] | None = None,
    prefix_ttl: str | None = None,
) -> dict[str, Any]:
    """Build the Anthropic Messages request body.

    ``prefix_ttl`` is the lifetime of the tool and system breakpoints; message breakpoints keep the default.
    """

    payload_messages: list[dict[str, Any]] = []
    for message in messages:
        converted = _message_to_anthropic(message, request.supports_images, loaded_images)
        if converted is None:
            continue
        if converted["role"] == "assistant" and not converted["content"]:
            continue
        if (
            isinstance(message, ToolResultMessage) and payload_messages
            and payload_messages[-1]["role"] == "user"
            and isinstance(payload_messages[-1]["content"], list)
            and all(block.get("type") == "tool_result" for block in payload_messages[-1]["content"])
        ):
            payload_messages[-1]["content"].extend(converted["content"])
        else:
            payload_messages.append(converted)

    system: Any = request.system
    if request.cache == "default" and request.system:
        system = [{"type": "text", "text": request.system, "cache_control": _cache_control(prefix_ttl)}]
    if request.cache == "default":
        _mark_message_breakpoints(payload_messages)
    payload: dict[str, Any] = {
        "model": request.endpoint.model_id,
        "max_tokens": request.max_tokens,
        "stream": True,
        "system": system,
        "messages": payload_messages,
    }
    # A hosted tool this hop runs natively is sent as the provider's declaration in its spec's place (§4.1). The tools
    # breakpoint goes on the last function tool, never on a server tool.
    native = native_hosted_tools(request)
    tools: list[dict[str, Any]] = [
        native_declaration(native[tool.name])
        if tool.name in native
        else {"name": tool.name, "description": tool.description, "input_schema": dict(tool.input_schema)}
        for tool in request.tools
    ]
    functions = [index for index, tool in enumerate(request.tools) if tool.name not in native]
    if request.cache == "default" and functions:
        tools[functions[-1]]["cache_control"] = _cache_control(prefix_ttl)
    if tools:
        payload["tools"] = tools
    effort = (request.reasoning_effort or "").lower()
    adaptive = uses_adaptive_thinking(request.endpoint.model_id)
    if effort in {"none", "off", "disabled"}:
        payload["thinking"] = {"type": "disabled"}
    elif effort and not adaptive and _continues_unthought_tool_loop(payload_messages):
        if is_claude_model(request.endpoint.model_id):
            # Under manual thinking, Anthropic requires the assistant message a tool loop continues to start with its
            # thinking block. One whose thinking cannot go back (another origin's, or a Session whose signatures the
            # provider refused) continues with thinking off, as Anthropic's refusal itself advises; the models that
            # take manual thinking all accept that. A non-Claude Anthropic-compatible model has no verified thinking
            # shape here, so leave the field out rather than guessing.
            payload["thinking"] = {"type": "disabled"}
    elif effort:
        if adaptive:
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": sent_effort(request.endpoint.model_id, effort)}
        else:
            # Pi's Anthropic adapter uses legacy budget thinking for Claude
            # 4.5 and older families. The API requires room for the thinking
            # budget and a response, so expand the request ceiling exactly as
            # Pi's adjustMaxTokensForThinking helper does.
            budget = _LEGACY_THINKING_BUDGETS.get(effort, _LEGACY_THINKING_BUDGETS["high"])
            payload["max_tokens"] = max(payload["max_tokens"], budget + 1024)
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
    return payload


def wire_request(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    gateway: bool,
    loaded_images: Mapping[str, LoadedImage] | None = None,
) -> WireRequest:
    """The Messages request for already prepared ``messages``, as the adapter sends it."""

    payload = build_messages_payload(request, messages, loaded_images=loaded_images, prefix_ttl=_prefix_ttl(request))
    headers = auth_headers(request.endpoint, provider="anthropic", gateway=gateway)
    headers.update({"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})
    headers.update(hosted_pin(request, messages))
    return WireRequest(join_endpoint_url(request.endpoint.base_url, "/messages"), headers, payload)


def _message_to_anthropic(
    message: Any,
    supports_images: bool,
    loaded_images: Mapping[str, LoadedImage] | None = None,
) -> dict[str, Any] | None:
    if isinstance(message, UserMessage):
        content = _anthropic_user_content(message.content, supports_images, loaded_images)
        return {"role": "user", "content": content if len(content) != 1 or content[0]["type"] != "text" else content[0]["text"]}
    if isinstance(message, AssistantMessage):
        content: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text:
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                if block.redacted:
                    if block.signature:
                        content.append({"type": "redacted_thinking", "data": block.signature})
                elif block.signature is not None:
                    content.append({"type": "thinking", "thinking": block.text, "signature": block.signature})
                elif block.text:
                    content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.arguments)})
            elif isinstance(block, HostedToolBlock):
                # Its server_tool_use and web_search_tool_result, verbatim (transform allowed it for this Source).
                content.extend(copy.deepcopy(list(block.native.payload)))
        return {"role": "assistant", "content": content}
    if isinstance(message, ToolResultMessage):
        result = _anthropic_user_content(message.content, supports_images, loaded_images)
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": result,
                    "is_error": message.is_error,
                }
            ],
        }
    return None


def _anthropic_user_content(
    content: tuple[Any, ...],
    supports_images: bool,
    loaded_images: Mapping[str, LoadedImage] | None = None,
) -> list[dict[str, Any]]:
    parts = content_parts(content, include_images=supports_images, loaded_images=loaded_images)
    result: list[dict[str, Any]] = []
    for part in parts:
        if part["type"] == "text":
            result.append({"type": "text", "text": part["text"]})
        else:
            result.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part["mime_type"],
                        "data": part["data"],
                    },
                }
            )
    if not result:
        result.append({"type": "text", "text": "(no content)"})
    return result


def _mark_message_breakpoints(messages: list[dict[str, Any]]) -> None:
    candidates = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and isinstance(message.get("content"), (str, list))
    ]
    for index in candidates[-2:]:
        content = messages[index]["content"]
        if isinstance(content, str):
            messages[index]["content"] = [{"type": "text", "text": content, "cache_control": _cache_control()}]
        elif content:
            last = content[-1]
            if isinstance(last, dict) and last.get("type") in {"text", "image", "tool_result"}:
                last["cache_control"] = _cache_control()


def _cache_control(ttl: str | None = None) -> dict[str, str]:
    """One breakpoint; without ``ttl`` it keeps Anthropic's default five-minute lifetime."""

    return {"type": "ephemeral", "ttl": ttl} if ttl else {"type": "ephemeral"}


def _prefix_ttl(request: ModelRequest) -> str | None:
    """The static prefix's lifetime: an hour where the field is known to work, else the default (``None``).

    Known means a Claude model on the Anthropic vendor, read from the hop: the vendor id exactly, and a model id that
    starts with ``claude-``. Another vendor's Anthropic-compatible endpoint keeps the default even for a Claude id.
    """

    endpoint = request.endpoint
    if (
        request.cache != "default"
        or not (request.tools or request.system)
        or endpoint.provider != "anthropic"
        or not endpoint.model_id.startswith("claude-")
        or _ttl_route(endpoint) in _TTL_REJECTED_ROUTES
    ):
        return None
    return _PREFIX_TTL


def _ttl_route(endpoint: ModelEndpoint) -> tuple[str, str | None, str, str]:
    """The route a TTL rejection is remembered for. Every Vibey hop shares the one gateway URL, so the Source behind
    it is part of the route: one relay's rejection never changes what another Source is sent."""
    return (endpoint.base_url, endpoint.source_id, endpoint.provider, endpoint.model_id)


def _rejects_ttl(event: Any) -> bool:
    """A request the endpoint refused with an error naming the field.

    Only an HTTP error status sets ``status``, so such an error is the response's first and only event.
    """

    return (
        isinstance(event, ProviderError)
        and event.kind == "invalid_request"
        and event.status is not None
        and 400 <= event.status < 500
        and _TTL_REJECTION.search(event.message) is not None
    )


def _remember_ttl_rejection(endpoint: ModelEndpoint) -> None:
    route = _ttl_route(endpoint)
    if route in _TTL_REJECTED_ROUTES:
        return
    _TTL_REJECTED_ROUTES.add(route)
    logger.warning(
        "The endpoint for %s model %s rejected the 1-hour prompt-cache TTL; "
        "this route uses the default 5-minute TTL until the process restarts",
        endpoint.provider,
        endpoint.model_id,
    )


def _error_event_shape_error(chunk: Mapping[str, Any]) -> str | None:
    error = chunk.get("error")
    if error is not None and not isinstance(error, Mapping):
        return "Anthropic error must be an object"
    if isinstance(error, Mapping):
        for field in ("type", "message"):
            value = error.get(field)
            if value is not None and not isinstance(value, str):
                return f"Anthropic error {field} must be a string"
    return None


def _anthropic_usage(value: Any, *, current: Any = None) -> Any:
    """Normalize start/delta with the field policy in conversion-tables.md.

    Tool request counts, tier and geography are not canonical token counters.
    Output already includes thinking; TTLs decompose, never add to, cache writes.
    """

    if not isinstance(value, Mapping):
        return current
    if current is None:
        current = _usage()
    return _usage(
        _pick(value, "input_tokens", current.input_tokens),
        _pick(value, "output_tokens", current.output_tokens),
        _pick(value, "cache_read_input_tokens", current.cache_read_tokens),
        _anthropic_cache_write_tokens(value, current.cache_write_tokens),
        _pick_nested(value, "output_tokens_details", "thinking_tokens", current.reasoning_tokens),
    )


def _anthropic_cache_write_tokens(value: Mapping[str, Any], fallback: int = 0) -> int:
    """Keep Anthropic's aggregate cache count, falling back only when absent."""

    aggregate = value.get("cache_creation_input_tokens")
    if "cache_creation_input_tokens" in value and aggregate is not None:
        return _nonnegative(aggregate)
    breakdown = value.get("cache_creation")
    if isinstance(breakdown, Mapping):
        counters = [
            breakdown[key]
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
            if breakdown.get(key) is not None
        ]
        if counters:
            return sum(_nonnegative(counter) for counter in counters)
    return fallback


def _usage(input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0, reasoning: int | None = None) -> Any:
    from core.agent_core.messages import Usage

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def _summed(counts: Mapping[str, int], more: Mapping[str, int]) -> dict[str, int]:
    summed = dict(counts)
    for name, value in more.items():
        summed[name] = summed.get(name, 0) + value
    return summed


def _bounded(counts: Mapping[str, int]) -> dict[str, int]:
    """The counters a summary keeps of a response's exact totals: one past the bound is no count."""

    return {name: value for name, value in counts.items() if value <= MAX_HOSTED_COUNT}


def _normalize_stop_reason(value: Any) -> str:
    normalized = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        # Generation reached the context window (Claude 4.5 and newer accept input plus max_tokens over the window);
        # Anthropic says to treat the response as truncated, as at max_tokens.
        "model_context_window_exceeded": "length",
        "tool_use": "tool_use",
        "refusal": "refusal",
        "safety": "safety",
    }.get(str(value))
    return normalized or "error"


def _refusal_detail(details: Any) -> str | None:
    """A refusal's ``stop_details`` as the response's ``error_message``: the category of the safeguard that stopped it
    and Anthropic's explanation, each when given as a non-empty string, bounded like a partial's error. A diagnostic
    never fails the response, so any other shape is ignored."""

    if not isinstance(details, Mapping):
        return None
    category, explanation = _string(details.get("category")).strip(), _string(details.get("explanation")).strip()
    fields = (("category", category), ("explanation", explanation))
    detail = "; ".join(f"{name}: {value}" for name, value in fields if value)
    if not detail:
        return None
    return detail if len(detail) <= PARTIAL_ERROR_MESSAGE_CHARS else detail[: PARTIAL_ERROR_MESSAGE_CHARS - 1] + "…"


def sent_effort(model_id: str, effort: str) -> str | None:
    """The token an Anthropic request carries at ``output_config.effort`` for ``effort`` on ``model_id``; None when it
    carries none, for an off effort or a model that thinks on a legacy token budget.

    The effort goes as given: the hop declares only the levels its model takes (Model Hub bounds them by the model's
    catalog row), so ``xhigh`` reaches Opus 4.7 and later natively and never Opus 4.6, which has no such level.
    ``minimal`` has no Anthropic level and goes as ``low``.
    """

    value = effort.lower()
    if not value or value in {"none", "off", "disabled"} or not uses_adaptive_thinking(model_id):
        return None
    return "low" if value == "minimal" else value


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _stream_index(chunk: Mapping[str, Any]) -> int | None:
    value = chunk.get("index")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _pick(value: Mapping[str, Any], key: str, default: int) -> int:
    candidate = value.get(key)
    return default if candidate is None else _nonnegative(candidate)


def _pick_nested(value: Mapping[str, Any], parent: str, key: str, default: int | None) -> int | None:
    nested = value.get(parent)
    if not isinstance(nested, Mapping):
        return default
    candidate = nested.get(key)
    return default if candidate is None else _nonnegative(candidate)


AnthropicProvider = AnthropicAdapter
