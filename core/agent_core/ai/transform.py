"""Canonical message transformation for provider requests.

The portability rules are from Pi's ``transform-messages`` (MIT, revision
7fbbd5f, ``packages/ai/src/api/transform-messages.ts``), with the canonical
message shapes defined by Avibe's C-1 contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

from core.agent_core.hosted_continuations import effective_deferred_history, hosted_completions, result_fragment
from core.agent_core.ai.hosted import portable_hosted
from core.agent_core.ai.provider import ModelEndpoint, OpaquePayload
from core.agent_core.messages import (
    AssistantMessage,
    AssistantContent,
    HostedToolBlock,
    ImageBlock,
    Message,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

SYNTHETIC_TOOL_RESULT = "[tool call interrupted; no result recorded]"
_SAFE_TOOL_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class TransformResult:
    """Transformed messages and the canonical-to-wire tool id map."""

    messages: tuple[Message, ...]
    tool_call_id_map: Mapping[str, str]

    @property
    def id_map(self) -> Mapping[str, str]:
        """Short alias used by adapter code."""

        return self.tool_call_id_map

    def __iter__(self):
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index: int) -> Message:
        return self.messages[index]


def normalize_tool_call_id(tool_call_id: str, protocol: str = "anthropic") -> str:
    """Return a deterministic id accepted by all supported native protocols."""

    # Anthropic's documented alphabet is the strictest common subset. The
    # The generated id is also accepted by OpenAI tool correlations.
    del protocol
    if _SAFE_TOOL_ID.fullmatch(tool_call_id) and len(tool_call_id) <= 64:
        return tool_call_id
    digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:24]
    return f"call_{digest}"


def transform_messages(
    messages: list[Message] | tuple[Message, ...],
    target: Origin,
    supports_images: bool = True,
    protocol: str | None = None,
    endpoint: ModelEndpoint | None = None,
) -> TransformResult:
    """Compile canonical history for one target origin.

    The returned map is per request. The transcript is never mutated. ``endpoint`` is the hop the request goes to: a
    response's opaque payloads (signatures, redacted thinking) go back only to a hop of the Source identity that served
    it, and its hosted calls verbatim only to the Source that served them (``native_replay``). Without an
    ``endpoint``, the origin alone decides.
    """

    target_protocol = protocol or target.api
    # Failed Anthropic attempts and their owned client results are audit facts, not request history. All three
    # continuation decisions below use this same effective view; raw positions are retained by the helper for callers
    # such as the provider adapter that must address the original request tuple.
    effective = effective_deferred_history(messages)
    raw_live = in_flight_position(messages)
    live = next((index for index, (raw, _) in enumerate(effective) if raw == raw_live), None)
    messages = tuple(message for _, message in effective)
    # The response the request continues within its turn keeps its hosted calls in place, verbatim or as text (§4.4);
    # every earlier one goes back verbatim with replay evidence or as a synthetic call and result (§5).
    messages, portable_pairs, parallel_groups, continuing_pairs = _complete_hosted_history(
        messages, live=live, endpoint=endpoint, target=target, protocol=target_protocol
    )
    id_map: dict[str, str] = {}
    used_tool_ids: set[str] = set()
    tool_id_occurrences: dict[str, int] = {}
    pending_tool_ids: dict[str, list[str]] = {}
    suppressed_tool_result_ids: set[str] = set()
    transformed: list[Message] = []

    for position, message in enumerate(messages):
        if isinstance(message, UserMessage):
            pending_tool_ids.clear()
            suppressed_tool_result_ids.clear()
            transformed.append(_transform_user(message, supports_images=supports_images))
            continue
        if isinstance(message, ToolResultMessage):
            if message.tool_call_id in suppressed_tool_result_ids:
                continue
            pending = pending_tool_ids.get(message.tool_call_id)
            if pending:
                wire_id = pending.pop(0)
                if not pending:
                    pending_tool_ids.pop(message.tool_call_id, None)
            else:
                wire_id = id_map.get(
                    message.tool_call_id,
                    normalize_tool_call_id(message.tool_call_id, target_protocol),
                )
            transformed.append(
                _transform_tool_result(
                    message,
                    tool_call_id=wire_id,
                    supports_images=supports_images,
                )
            )
            continue
        if not isinstance(message, AssistantMessage):
            transformed.append(message)
            continue

        pending_tool_ids.clear()
        # Opaque payloads are bound to the account that issued them, the Source identity: another account's ciphertext
        # is refused (xAI, OpenAI). A message another identity served, one no known identity did, or one whose
        # identity's provider refused an opaque payload this turn is sent as a different origin's.
        same_origin = message.origin == target and _opaque_admitted(message, endpoint, "signature")
        content: list[AssistantContent] = []
        turn_tool_ids: dict[str, list[str]] = {}
        in_flight = position == live
        verbatim = position not in portable_pairs and native_replay(
            message, in_flight=in_flight or position in continuing_pairs,
            endpoint=endpoint, target=target, protocol=target_protocol
        )
        hosted = {} if verbatim else portable_hosted(
            message, in_flight=in_flight, interrupted=SYNTHETIC_TOOL_RESULT, parallel_group=position in parallel_groups
        )
        # The synthetic results of the current run of hosted calls, and the assistant parts they split off.
        results: list[ToolResultMessage] = []
        parts: list[Message] = []
        foreign_thinking = False

        def split() -> None:
            nonlocal content, results
            if results and position not in parallel_groups:
                parts.append(_assistant_part(message, content, stop_reason="tool_use"))
                parts.extend(results)
                content, results = [], []

        for block in message.content:
            if isinstance(block, HostedToolBlock) and verbatim:
                content.append(block)  # its native payload, rendered in place by the protocol's builder
                continue
            if isinstance(block, HostedToolBlock):
                portable = hosted[id(block)]
                if portable.kind == "in_place":
                    split()
                    content.append(TextBlock(text=portable.text))
                    continue
                name, arguments, text, is_error = portable.name, dict(portable.arguments), portable.text, portable.is_error
                call_id = _claim_tool_call_id(
                    block.id,
                    normalize_tool_call_id(block.id, target_protocol),
                    used_tool_ids=used_tool_ids,
                    occurrences=tool_id_occurrences,
                )
                content.append(ToolCallBlock(id=call_id, name=name, arguments=arguments))
                results.append(
                    ToolResultMessage(
                        tool_call_id=call_id, tool_name=name, content=(TextBlock(text=text),), is_error=is_error
                    )
                )
                continue
            split()
            if isinstance(block, ThinkingBlock):
                # Thinking is a model's private reasoning, not something the assistant said, so it never goes to a
                # different origin in any form: as text it would be attributed to the target model, and providers
                # refuse requests that carry another model's reasoning as text (cross-provider.md).
                if not same_origin:
                    foreign_thinking = True
                    continue
                if block.redacted or block.signature is not None:
                    content.append(block)
                elif block.text:
                    content.append(TextBlock(text=block.text))
                continue
            if isinstance(block, TextBlock):
                content.append(block)
                continue
            if isinstance(block, ToolCallBlock):
                normalized = _claim_tool_call_id(
                    block.id,
                    normalize_tool_call_id(block.id, target_protocol),
                    used_tool_ids=used_tool_ids,
                    occurrences=tool_id_occurrences,
                )
                # Results are correlated to the pending calls in this turn.
                # Keep the last occurrence in the public map for compatibility
                # with callers that only have a stored id.
                id_map[block.id] = normalized
                turn_tool_ids.setdefault(block.id, []).append(normalized)
                content.append(
                    ToolCallBlock(
                        id=normalized,
                        name=block.name,
                        arguments=dict(block.arguments),
                        native_id=block.native_id if same_origin else None,
                        signature=block.signature if same_origin else None,
                    )
                )
                continue
            content.append(block)

        # An aborted/error response is not a valid provider turn to replay.
        if message.stop_reason in {"aborted", "error"}:
            suppressed_tool_result_ids.update(turn_tool_ids)
            continue
        for source_id, wire_ids in turn_tool_ids.items():
            pending_tool_ids.setdefault(source_id, []).extend(wire_ids)
        split()
        transformed.extend(parts)
        # A response whose only content was another origin's thinking is skipped, as an aborted one is, rather than
        # sent as an empty assistant message; one that was empty as served (a refusal) still goes, as before.
        if content or not (parts or foreign_thinking):
            transformed.append(_assistant_part(message, content, stop_reason=message.stop_reason))
        if position in parallel_groups:
            transformed.extend(results)

    transformed = _insert_synthetic_results(transformed)
    return TransformResult(messages=tuple(transformed), tool_call_id_map=dict(id_map))


def _complete_hosted_history(
    messages: list[Message] | tuple[Message, ...], *, live: int | None, endpoint: ModelEndpoint | None,
    target: Origin, protocol: str,
) -> tuple[tuple[Message, ...], set[int], set[int], set[int]]:
    """Join deferred records for eligibility/portable replay, not for native wire ordering (C-1).

    Both halves must be native or neither may be: a result-only native fragment cannot follow a synthetic call.
    Portable calls remain in the original parallel group and are settled alongside its client results.
    """
    pairs = hosted_completions(messages)
    paired = {(pair.result_position, id(pair.result)) for pair in pairs}
    orphaned = {
        position for position, message in enumerate(messages)
        if isinstance(message, AssistantMessage) and message.origin.api == "anthropic"
        for block in message.content if isinstance(block, HostedToolBlock) and result_fragment(block)
        and (position, id(block)) not in paired
    }
    if not pairs:
        return tuple(messages), orphaned, set(), set()
    continuing = {live} if live is not None else set()
    for pair in reversed(pairs):
        if pair.result_position in continuing:
            continuing.add(pair.use_position)
    resolved = list(messages)
    for pair in pairs:
        message = resolved[pair.use_position]
        resolved[pair.use_position] = replace(message, content=tuple(
            replace(block, record=pair.result.record) if block is pair.call else block for block in message.content
        ))
    blocked = orphaned | {
        position for pair in pairs for position in (pair.use_position, pair.result_position)
        if resolved[position].stop_reason in {"aborted", "error"}
        or not native_replay(resolved[position], in_flight=position in continuing, endpoint=endpoint,
                             target=target, protocol=protocol)
    }
    # A response can finish a previous group and open another one. Admission is message-wide, so refusal propagates
    # through that connected exchange rather than leaving an orphan native fragment at either end.
    while True:
        expanded = blocked | {
            position for pair in pairs if pair.use_position in blocked or pair.result_position in blocked
            for position in (pair.use_position, pair.result_position)
        }
        if expanded == blocked:
            break
        blocked = expanded
    parallel = set()
    for pair in pairs:
        if pair.use_position not in blocked:
            continue
        parallel.add(pair.use_position)
        message = resolved[pair.result_position]
        content = tuple(block for block in message.content if block is not pair.result)
        resolved[pair.result_position] = replace(
            message, content=content,
            hosted_summary=message.hosted_summary if any(isinstance(b, HostedToolBlock) for b in content) else None,
        )
    return tuple(resolved), blocked, parallel, continuing


def _assistant_part(message: AssistantMessage, content: list[AssistantContent], *, stop_reason: str) -> AssistantMessage:
    hosted = any(isinstance(block, HostedToolBlock) for block in content)
    return AssistantMessage(
        content=tuple(content),
        origin=message.origin,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=message.usage,
        error_message=message.error_message,
        hosted_summary=message.hosted_summary if hosted else None,
    )


def in_flight_position(messages: list[Message] | tuple[Message, ...]) -> int | None:
    """The raw position of the response a request continues within its turn (§4.4), or None.

    The effective deferred-exchange history is shared with pairing and wire compilation: failed Anthropic attempts and
    their owned client results disappear before this tail check, while the returned position remains an index into the
    caller's original message sequence.
    """
    history = effective_deferred_history(messages)
    for raw_position, message in reversed(history):
        if isinstance(message, ToolResultMessage):
            continue
        if not isinstance(message, AssistantMessage) or message.stop_reason != "tool_use":
            return None
        return raw_position
    return None


def _opaque_admitted(message: AssistantMessage, endpoint: ModelEndpoint | None, payload: OpaquePayload) -> bool:
    """Whether ``message``'s opaque payloads of kind ``payload`` may go to ``endpoint``: a hop of the Source identity
    that served it, whose provider has not refused that kind in this Session (§3.5)."""

    return endpoint is None or (
        message.source_identity == endpoint.source_identity
        and (message.source_identity, payload) not in endpoint.opaque_refused
    )


def native_replay(
    message: AssistantMessage, *, in_flight: bool, endpoint: ModelEndpoint | None, target: Origin, protocol: str
) -> bool:
    """Whether a response's hosted calls go back verbatim, their native payloads in place (§4.4, §5).

    Only to the hop the request goes to, when that hop's Source served them on the same origin: one hop then builds
    the request and serves it (§3.5; a moved hop gets the in-place form). The hop must still declare each tool they
    called natively, since a provider refuses its server items without their declaration, over a protocol with native
    hosted content, and the request must not be portable.
    * The response its turn continues (``in_flight``) needs no ``replay``: that is the provider's own continuation
      contract.
    * Any earlier response needs ``replay`` assumed on the hop for every tool it called, and every one of its calls
      finished.
    """

    blocks = [block for block in message.content if isinstance(block, HostedToolBlock)]
    if not blocks or endpoint is None or not endpoint.source_id or endpoint.portable or message.origin != target:
        return False
    # A native payload is opaque provider content too: it goes back only to the account that issued it.
    if not _opaque_admitted(message, endpoint, "hosted"):
        return False
    if protocol not in ("anthropic", "openai_responses", "google"):
        return False
    if {block.native.source_id for block in blocks} != {endpoint.source_id}:
        return False
    for block in blocks:
        route = endpoint.hosted_tools.get(block.tool)
        if route is None or route.mode != "native":
            return False
        if not in_flight and (not route.replay or block.record.status == "pending"):
            return False
    return True


def transform_messages_for_target(
    messages: list[Message] | tuple[Message, ...],
    target: Origin,
    *,
    supports_images: bool = True,
) -> TransformResult:
    """Positional-argument convenience wrapper."""

    return transform_messages(messages, target=target, supports_images=supports_images)


portable_tool_call_id = normalize_tool_call_id


def _transform_user(message: UserMessage, *, supports_images: bool) -> UserMessage:
    if supports_images:
        return message
    return UserMessage(content=_replace_images(message.content, tool=False))


def _claim_tool_call_id(
    source_id: str,
    base_id: str,
    *,
    used_tool_ids: set[str],
    occurrences: dict[str, int],
) -> str:
    occurrence = occurrences.get(source_id, 0)
    occurrences[source_id] = occurrence + 1
    candidate = base_id
    if candidate in used_tool_ids:
        salt = occurrence
        while candidate in used_tool_ids:
            digest = hashlib.sha256(f"{source_id}\x00{salt}".encode("utf-8")).hexdigest()[:16]
            prefix = base_id[: max(1, 64 - len(digest) - 1)]
            candidate = f"{prefix}_{digest}"
            salt += 1
    used_tool_ids.add(candidate)
    return candidate


def _transform_tool_result(
    message: ToolResultMessage,
    *,
    tool_call_id: str,
    supports_images: bool,
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=tool_call_id,
        tool_name=message.tool_name,
        content=message.content if supports_images else _replace_images(message.content, tool=True),
        is_error=message.is_error,
    )


def _replace_images(content: tuple[Any, ...], *, tool: bool) -> tuple[Any, ...]:
    placeholder_template = "[image: {}]"
    result: list[Any] = []
    for block in content:
        if isinstance(block, ImageBlock):
            result.append(TextBlock(text=placeholder_template.format(block.name or block.mime_type)))
        else:
            result.append(block)
    return tuple(result)


def _insert_synthetic_results(messages: list[Message]) -> list[Message]:
    result: list[Message] = []
    pending: list[ToolCallBlock] = []
    answered: set[str] = set()

    def close_pending() -> None:
        nonlocal pending, answered
        for call in pending:
            if call.id not in answered:
                result.append(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=(TextBlock(text=SYNTHETIC_TOOL_RESULT),),
                        is_error=True,
                    )
                )
        pending = []
        answered = set()

    for message in messages:
        if isinstance(message, AssistantMessage):
            close_pending()
            calls = list(message.tool_calls)
            if calls:
                pending = calls
            result.append(message)
        elif isinstance(message, ToolResultMessage):
            if message.tool_call_id in answered:
                continue
            answered.add(message.tool_call_id)
            result.append(message)
        elif isinstance(message, UserMessage):
            close_pending()
            result.append(message)
        else:
            result.append(message)
    close_pending()
    return result
