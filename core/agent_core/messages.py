"""C-1 canonical message model (``agent-core-contracts/message.schema.json``).

Vendor-neutral messages exchanged by the provider adapters, the loop, and the
transcript store. ``message_to_dict`` / ``message_from_dict`` produce and read
the persisted form stored in ``messages.content_json.model`` and in tool-result
rows, so the field names here are a shipped surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Union

ProtocolName = Literal["anthropic", "openai_chat", "openai_responses", "google"]
PROTOCOLS: tuple[str, ...] = ("anthropic", "openai_chat", "openai_responses", "google")

StopReason = Literal["stop", "length", "tool_use", "refusal", "safety", "aborted", "error"]
STOP_REASONS: tuple[str, ...] = ("stop", "length", "tool_use", "refusal", "safety", "aborted", "error")

IMAGE_MIME_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/gif", "image/webp")

_LARGE_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class LargeRef:
    """Reserved reference to content stored outside the row; v1 never writes it."""

    ref: str
    bytes: int

    def __post_init__(self) -> None:
        if not _LARGE_REF_RE.match(self.ref) or self.bytes < 0:
            raise ValueError(f"invalid large-content reference: {self.ref!r}")


@dataclass(frozen=True)
class TextBlock:
    text: Optional[str] = None
    ref: Optional[LargeRef] = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.ref is None):
            raise ValueError("a text block holds exactly one of text or ref")


@dataclass(frozen=True)
class ImageBlock:
    mime_type: str
    media_token: str
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.mime_type not in IMAGE_MIME_TYPES:
            raise ValueError(f"unsupported image type: {self.mime_type}")
        if not self.media_token:
            raise ValueError("an image block needs a media token")


@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: Optional[str] = None
    redacted: bool = False


@dataclass(frozen=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    native_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("a tool call needs an id and a name")


UserContent = Union[TextBlock, ImageBlock]
AssistantContent = Union[TextBlock, ThinkingBlock, ToolCallBlock]


@dataclass(frozen=True)
class Origin:
    """Who served an assistant message; equal only when all three fields are equal."""

    provider: str
    api: str
    model: str

    def __post_init__(self) -> None:
        if not self.provider or not self.model:
            raise ValueError("an origin needs a provider and a model")
        if self.api not in PROTOCOLS:
            raise ValueError(f"unknown protocol: {self.api}")


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: Optional[int] = None


@dataclass(frozen=True)
class UserMessage:
    content: tuple[UserContent, ...]

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("a user message needs content")

    @property
    def role(self) -> Literal["user"]:
        return "user"


@dataclass(frozen=True)
class AssistantMessage:
    content: tuple[AssistantContent, ...]
    origin: Origin
    stop_reason: StopReason
    usage: Optional[Usage] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        if self.stop_reason not in STOP_REASONS:
            raise ValueError(f"unknown stop reason: {self.stop_reason}")

    @property
    def role(self) -> Literal["assistant"]:
        return "assistant"

    @property
    def tool_calls(self) -> tuple[ToolCallBlock, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolCallBlock))


@dataclass(frozen=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: tuple[UserContent, ...]
    is_error: bool = False

    @property
    def role(self) -> Literal["tool_result"]:
        return "tool_result"


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


def text(value: str) -> TextBlock:
    return TextBlock(text=value)


# --- persisted form ---------------------------------------------------------


def _strict(data: Any, allowed: set[str], what: str) -> None:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be an object")
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {what} field(s): {', '.join(sorted(unknown))}")


def _typed(value: Any, kind: type, what: str, *, optional: bool = False) -> Any:
    """Return ``value`` if it has exactly the JSON type ``kind``; never coerce."""
    if value is None and optional:
        return None
    # bool is an int subclass in Python; a JSON boolean is never an integer here.
    if isinstance(value, bool) and kind is not bool:
        raise ValueError(f"{what} must be {kind.__name__}")
    if not isinstance(value, kind):
        raise ValueError(f"{what} must be {kind.__name__}")
    return value


def _list(value: Any, what: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be an array")
    return value


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        if block.ref is not None:
            return {"type": "text", "ref": {"ref": block.ref.ref, "bytes": block.ref.bytes}}
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        out: dict[str, Any] = {"type": "image", "mime_type": block.mime_type, "media_token": block.media_token}
        if block.name is not None:
            out["name"] = block.name
        return out
    if isinstance(block, ThinkingBlock):
        out = {"type": "thinking", "text": block.text}
        if block.signature is not None:
            out["signature"] = block.signature
        if block.redacted:
            out["redacted"] = True
        return out
    if isinstance(block, ToolCallBlock):
        out = {"type": "tool_call", "id": block.id, "name": block.name, "arguments": dict(block.arguments)}
        if block.native_id is not None:
            out["native_id"] = block.native_id
        return out
    raise TypeError(f"not a content block: {type(block).__name__}")


def _block_from_dict(data: Any, allowed_types: tuple[str, ...]) -> Any:
    if not isinstance(data, Mapping):
        raise ValueError("a content block must be an object")
    kind = data.get("type")
    if kind not in allowed_types:
        raise ValueError(f"block type {kind!r} is not allowed here")
    if kind == "text":
        _strict(data, {"type", "text", "ref"}, "text block")
        ref = data.get("ref")
        if ref is not None:
            _strict(ref, {"ref", "bytes"}, "large-content reference")
            ref = LargeRef(ref=_typed(ref.get("ref"), str, "ref.ref"), bytes=_typed(ref.get("bytes"), int, "ref.bytes"))
        return TextBlock(text=_typed(data.get("text"), str, "text", optional=True), ref=ref)
    if kind == "image":
        _strict(data, {"type", "mime_type", "media_token", "name"}, "image block")
        return ImageBlock(
            mime_type=_typed(data.get("mime_type"), str, "mime_type"),
            media_token=_typed(data.get("media_token"), str, "media_token"),
            name=_typed(data.get("name"), str, "name", optional=True),
        )
    if kind == "thinking":
        _strict(data, {"type", "text", "signature", "redacted"}, "thinking block")
        return ThinkingBlock(
            text=_typed(data.get("text"), str, "thinking text"),
            signature=_typed(data.get("signature"), str, "signature", optional=True),
            redacted=_typed(data.get("redacted", False), bool, "redacted"),
        )
    _strict(data, {"type", "id", "name", "arguments", "native_id"}, "tool call block")
    return ToolCallBlock(
        id=_typed(data.get("id"), str, "tool call id"),
        name=_typed(data.get("name"), str, "tool name"),
        arguments=dict(_typed(data.get("arguments"), dict, "arguments")),
        native_id=_typed(data.get("native_id"), str, "native_id", optional=True),
    )


def _usage_to_dict(usage: Usage) -> dict[str, Any]:
    out: dict[str, Any] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }
    if usage.reasoning_tokens is not None:
        out["reasoning_tokens"] = usage.reasoning_tokens
    return out


def usage_from_dict(data: Any) -> Usage:
    _strict(
        data,
        {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"},
        "usage",
    )
    return Usage(
        input_tokens=_typed(data.get("input_tokens"), int, "input_tokens"),
        output_tokens=_typed(data.get("output_tokens"), int, "output_tokens"),
        cache_read_tokens=_typed(data.get("cache_read_tokens"), int, "cache_read_tokens"),
        cache_write_tokens=_typed(data.get("cache_write_tokens"), int, "cache_write_tokens"),
        reasoning_tokens=_typed(data.get("reasoning_tokens"), int, "reasoning_tokens", optional=True),
    )


def origin_from_dict(data: Any) -> Origin:
    _strict(data, {"provider", "api", "model"}, "origin")
    return Origin(
        provider=_typed(data.get("provider"), str, "origin.provider"),
        api=_typed(data.get("api"), str, "origin.api"),
        model=_typed(data.get("model"), str, "origin.model"),
    )


def message_to_dict(message: Message) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        return {"role": "user", "content": [_block_to_dict(b) for b in message.content]}
    if isinstance(message, AssistantMessage):
        out: dict[str, Any] = {
            "role": "assistant",
            "content": [_block_to_dict(b) for b in message.content],
            "origin": {"provider": message.origin.provider, "api": message.origin.api, "model": message.origin.model},
            "stop_reason": message.stop_reason,
        }
        if message.usage is not None:
            out["usage"] = _usage_to_dict(message.usage)
        if message.error_message is not None:
            out["error_message"] = message.error_message
        return out
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool_result",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": [_block_to_dict(b) for b in message.content],
            "is_error": message.is_error,
        }
    raise TypeError(f"not a message: {type(message).__name__}")


def message_from_dict(data: Mapping[str, Any]) -> Message:
    if not isinstance(data, Mapping):
        raise ValueError("a message must be an object")
    role = data.get("role")
    if role == "user":
        _strict(data, {"role", "content"}, "user message")
        return UserMessage(
            content=tuple(_block_from_dict(b, ("text", "image")) for b in _list(data.get("content"), "content"))
        )
    if role == "assistant":
        _strict(data, {"role", "content", "origin", "usage", "stop_reason", "error_message"}, "assistant message")
        usage = data.get("usage")
        return AssistantMessage(
            content=tuple(
                _block_from_dict(b, ("text", "thinking", "tool_call")) for b in _list(data.get("content"), "content")
            ),
            origin=origin_from_dict(data.get("origin")),
            stop_reason=_typed(data.get("stop_reason"), str, "stop_reason"),
            usage=usage_from_dict(usage) if usage is not None else None,
            error_message=_typed(data.get("error_message"), str, "error_message", optional=True),
        )
    if role == "tool_result":
        _strict(data, {"role", "tool_call_id", "tool_name", "content", "is_error"}, "tool result message")
        return ToolResultMessage(
            tool_call_id=_typed(data.get("tool_call_id"), str, "tool_call_id"),
            tool_name=_typed(data.get("tool_name"), str, "tool_name"),
            content=tuple(_block_from_dict(b, ("text", "image")) for b in _list(data.get("content"), "content")),
            is_error=_typed(data.get("is_error"), bool, "is_error"),
        )
    raise ValueError(f"unknown message role: {role!r}")
