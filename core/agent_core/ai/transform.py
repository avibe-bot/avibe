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
    # generated id is also accepted by OpenAI and Gemini tool correlations.
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
    transformed: list[Message] = []

    for message in messages:
        if isinstance(message, UserMessage):
            transformed.append(_transform_user(message, supports_images=supports_images))
            continue
        if isinstance(message, ToolResultMessage):
            transformed.append(
                _transform_tool_result(
                    message,
                    id_map=id_map,
                    supports_images=supports_images,
                    protocol=target_protocol,
                )
            )
            continue
        if not isinstance(message, AssistantMessage):
            transformed.append(message)
            continue

        same_origin = message.origin == target
        content: list[AssistantContent] = []
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
                normalized = normalize_tool_call_id(block.id, target_protocol)
                id_map[block.id] = normalized
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
            continue
        transformed.append(
            AssistantMessage(
                content=tuple(content),
                origin=message.origin,
                stop_reason=message.stop_reason,
                usage=message.usage,
                error_message=message.error_message,
            )
        )

    transformed = _rewrite_result_ids(transformed, id_map)
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


def _transform_tool_result(
    message: ToolResultMessage,
    *,
    id_map: Mapping[str, str],
    supports_images: bool,
    protocol: str,
) -> ToolResultMessage:
    del protocol
    return ToolResultMessage(
        tool_call_id=id_map.get(message.tool_call_id, normalize_tool_call_id(message.tool_call_id, "anthropic"))
        if message.tool_call_id in id_map
        else message.tool_call_id,
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


def _rewrite_result_ids(messages: list[Message], id_map: Mapping[str, str]) -> list[Message]:
    result: list[Message] = []
    for message in messages:
        if isinstance(message, ToolResultMessage):
            result.append(
                ToolResultMessage(
                    tool_call_id=id_map.get(message.tool_call_id, message.tool_call_id),
                    tool_name=message.tool_name,
                    content=message.content,
                    is_error=message.is_error,
                )
            )
        else:
            result.append(message)
    return result


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
