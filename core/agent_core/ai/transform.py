"""Canonical message transformation for provider requests.

The portability rules are from Pi's ``transform-messages`` (MIT, revision
7fbbd5f, ``packages/ai/src/api/transform-messages.ts``), with the canonical
message shapes defined by Avibe's C-1 contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

from core.agent_core.messages import (
    AssistantMessage,
    AssistantContent,
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
) -> TransformResult:
    """Compile canonical history for one target origin.

    The returned map is per request. The transcript is never mutated.
    """

    target_protocol = protocol or target.api
    id_map: dict[str, str] = {}
    used_tool_ids: set[str] = set()
    tool_id_occurrences: dict[str, int] = {}
    pending_tool_ids: dict[str, list[str]] = {}
    suppressed_tool_result_ids: set[str] = set()
    transformed: list[Message] = []

    for message in messages:
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
        same_origin = message.origin == target
        content: list[AssistantContent] = []
        turn_tool_ids: dict[str, list[str]] = {}
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                if block.redacted:
                    if same_origin:
                        content.append(block)
                    continue
                if block.signature is None:
                    if block.text:
                        content.append(TextBlock(text=block.text))
                elif same_origin:
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
        transformed.append(
            AssistantMessage(
                content=tuple(content),
                origin=message.origin,
                stop_reason=message.stop_reason,
                usage=message.usage,
                error_message=message.error_message,
            )
        )

    transformed = _insert_synthetic_results(transformed)
    return TransformResult(messages=tuple(transformed), tool_call_id_map=dict(id_map))


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
