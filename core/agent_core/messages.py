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


def _require(value: Any, kind: type, what: str, *, optional: bool = False) -> None:
    """Exact-type check shared by construction and reading: what one accepts, the other accepts."""
    if value is None and optional:
        return
    # bool is an int subclass in Python; a boolean is never accepted as a number.
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise ValueError(f"{what} must be {kind.__name__}")


@dataclass(frozen=True)
class LargeRef:
    """Reserved reference to content stored outside the row; v1 never writes it."""

    ref: str
    bytes: int

    def __post_init__(self) -> None:
        _require(self.ref, str, "ref")
        _require(self.bytes, int, "bytes")
        if not _LARGE_REF_RE.fullmatch(self.ref) or self.bytes < 0:
            raise ValueError(f"invalid large-content reference: {self.ref!r}")


@dataclass(frozen=True)
class TextBlock:
    text: Optional[str] = None
    ref: Optional[LargeRef] = None

    def __post_init__(self) -> None:
        _require(self.text, str, "text", optional=True)
        _require(self.ref, LargeRef, "ref", optional=True)
        if (self.text is None) == (self.ref is None):
            raise ValueError("a text block holds exactly one of text or ref")


@dataclass(frozen=True)
class ImageBlock:
    mime_type: str
    media_token: str
    name: Optional[str] = None

    def __post_init__(self) -> None:
        _require(self.mime_type, str, "mime_type")
        _require(self.media_token, str, "media_token")
        _require(self.name, str, "name", optional=True)
        if self.mime_type not in IMAGE_MIME_TYPES:
            raise ValueError(f"unsupported image type: {self.mime_type}")
        if not self.media_token:
            raise ValueError("an image block needs a media token")
        if self.name is not None and not self.name:
            raise ValueError("an image name, when present, must not be empty")


@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: Optional[str] = None
    redacted: bool = False
    """True for reasoning the provider returned only in opaque form: ``signature`` holds that payload, ``text`` is empty."""

    def __post_init__(self) -> None:
        _require(self.text, str, "thinking text")
        _require(self.signature, str, "signature", optional=True)
        _require(self.redacted, bool, "redacted")
        if self.signature is not None and not self.signature:
            raise ValueError("a signature, when present, must not be empty")
        if self.redacted and (self.signature is None or self.text):
            raise ValueError("redacted thinking keeps its payload in signature and has empty text")


@dataclass(frozen=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    native_id: Optional[str] = None
    signature: Optional[str] = None
    """Opaque provider payload on the call itself (Gemini ``thoughtSignature``); same rules as a thinking signature."""

    def __post_init__(self) -> None:
        _require(self.id, str, "tool call id")
        _require(self.name, str, "tool name")
        _require(self.arguments, dict, "arguments")
        require_json_value(self.arguments, "arguments")
        _require(self.native_id, str, "native_id", optional=True)
        _require(self.signature, str, "signature", optional=True)
        if not self.id or not self.name:
            raise ValueError("a tool call needs an id and a name")
        if self.native_id is not None and not self.native_id:
            raise ValueError("a native id, when present, must not be empty")
        if self.signature is not None and not self.signature:
            raise ValueError("a signature, when present, must not be empty")


def require_json_value(value: Any, what: str) -> None:
    """Refuse anything that would not survive a JSON round trip unchanged.

    Allowed: dict with str keys, list, str, int, finite float, bool, None. A tuple,
    bytes, a non-str key, or NaN would be coerced or rejected on persistence.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{what} must be a finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            require_json_value(item, f"{what}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{what} keys must be strings")
            require_json_value(item, f"{what}.{key}")
        return
    raise ValueError(f"{what} must be JSON (got {type(value).__name__})")


def _require_blocks(content: Any, allowed: tuple[type, ...], what: str) -> None:
    if not isinstance(content, tuple) or not all(isinstance(block, allowed) for block in content):
        raise ValueError(f"{what} content must be a tuple of {', '.join(k.__name__ for k in allowed)}")


UserContent = Union[TextBlock, ImageBlock]
AssistantContent = Union[TextBlock, ThinkingBlock, ToolCallBlock]


@dataclass(frozen=True)
class Origin:
    """Who served an assistant message; equal only when all three fields are equal."""

    provider: str
    api: str
    model: str

    def __post_init__(self) -> None:
        for name in ("provider", "api", "model"):
            _require(getattr(self, name), str, f"origin.{name}")
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

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            _require(getattr(self, name), int, name)
        _require(self.reasoning_tokens, int, "reasoning_tokens", optional=True)
        counts = (self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens)
        if any(c < 0 for c in counts) or (self.reasoning_tokens is not None and self.reasoning_tokens < 0):
            raise ValueError("token counts must not be negative")


@dataclass(frozen=True)
class UserMessage:
    content: tuple[UserContent, ...]

    def __post_init__(self) -> None:
        _require_blocks(self.content, (TextBlock, ImageBlock), "user message")
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
        _require_blocks(self.content, (TextBlock, ThinkingBlock, ToolCallBlock), "assistant message")
        _require(self.origin, Origin, "origin")
        _require(self.stop_reason, str, "stop_reason")
        _require(self.usage, Usage, "usage", optional=True)
        _require(self.error_message, str, "error_message", optional=True)
        if self.error_message is not None and not self.error_message:
            raise ValueError("an error message, when present, must not be empty")
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

    def __post_init__(self) -> None:
        _require(self.tool_call_id, str, "tool_call_id")
        _require(self.tool_name, str, "tool_name")
        _require_blocks(self.content, (TextBlock, ImageBlock), "tool result")
        _require(self.is_error, bool, "is_error")
        if not self.tool_call_id or not self.tool_name:
            raise ValueError("a tool result needs its call id and tool name")

    @property
    def role(self) -> Literal["tool_result"]:
        return "tool_result"


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


def text(value: str) -> TextBlock:
    return TextBlock(text=value)


# --- persisted form ---------------------------------------------------------
#
# Two owners, so no field can be checked differently from another:
# * the dataclasses above own value invariants (non-empty ids, non-negative
#   counts, enumerations, the large-reference pattern);
# * ``_read`` owns the wire shape of every object: it must be a JSON object,
#   unknown keys are refused, required keys must be present, an optional key is
#   either absent or holds a value (an explicit null is refused, because the
#   writer never produces one), and every value has exactly its JSON type.


def _read(data: Any, what: str, required: Mapping[str, type], optional: Mapping[str, type] = {}) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be an object")
    unknown = set(data) - set(required) - set(optional)
    if unknown:
        raise ValueError(f"unknown {what} field(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for name, kind in {**required, **optional}.items():
        if name not in data:
            if name in required:
                raise ValueError(f"{what} needs {name}")
            continue
        value = data[name]
        # bool is an int subclass in Python; a JSON boolean is never accepted as a number.
        if value is None or not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
            raise ValueError(f"{what}.{name} must be {kind.__name__}")
        out[name] = value
    return out


def _put(out: dict[str, Any], name: str, value: Any) -> None:
    if value is not None:
        out[name] = value


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        if block.ref is not None:
            return {"type": "text", "ref": {"ref": block.ref.ref, "bytes": block.ref.bytes}}
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        out: dict[str, Any] = {"type": "image", "mime_type": block.mime_type, "media_token": block.media_token}
        _put(out, "name", block.name)
        return out
    if isinstance(block, ThinkingBlock):
        out = {"type": "thinking", "text": block.text}
        _put(out, "signature", block.signature)
        if block.redacted:
            out["redacted"] = True
        return out
    if isinstance(block, ToolCallBlock):
        out = {"type": "tool_call", "id": block.id, "name": block.name, "arguments": dict(block.arguments)}
        _put(out, "native_id", block.native_id)
        _put(out, "signature", block.signature)
        return out
    raise TypeError(f"not a content block: {type(block).__name__}")


def _block_from_dict(data: Any, allowed_types: tuple[str, ...]) -> Any:
    kind = data.get("type") if isinstance(data, Mapping) else None
    if kind not in allowed_types:
        raise ValueError(f"block type {kind!r} is not allowed here")
    if kind == "text":
        f = _read(data, "text block", {"type": str}, {"text": str, "ref": dict})
        ref = None
        if "ref" in f:
            r = _read(f["ref"], "large-content reference", {"ref": str, "bytes": int})
            ref = LargeRef(ref=r["ref"], bytes=r["bytes"])
        return TextBlock(text=f.get("text"), ref=ref)
    if kind == "image":
        f = _read(data, "image block", {"type": str, "mime_type": str, "media_token": str}, {"name": str})
        return ImageBlock(mime_type=f["mime_type"], media_token=f["media_token"], name=f.get("name"))
    if kind == "thinking":
        f = _read(data, "thinking block", {"type": str, "text": str}, {"signature": str, "redacted": bool})
        if f.get("redacted") is False:
            raise ValueError("thinking block.redacted is written only when true")
        return ThinkingBlock(text=f["text"], signature=f.get("signature"), redacted=bool(f.get("redacted")))
    f = _read(
        data,
        "tool call block",
        {"type": str, "id": str, "name": str, "arguments": dict},
        {"native_id": str, "signature": str},
    )
    return ToolCallBlock(
        id=f["id"], name=f["name"], arguments=dict(f["arguments"]), native_id=f.get("native_id"), signature=f.get("signature")
    )


_USAGE_REQUIRED = {"input_tokens": int, "output_tokens": int, "cache_read_tokens": int, "cache_write_tokens": int}


def _usage_to_dict(usage: Usage) -> dict[str, Any]:
    out: dict[str, Any] = {name: getattr(usage, name) for name in _USAGE_REQUIRED}
    _put(out, "reasoning_tokens", usage.reasoning_tokens)
    return out


def usage_from_dict(data: Any) -> Usage:
    return Usage(**_read(data, "usage", _USAGE_REQUIRED, {"reasoning_tokens": int}))


def origin_from_dict(data: Any) -> Origin:
    return Origin(**_read(data, "origin", {"provider": str, "api": str, "model": str}))


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
        _put(out, "error_message", message.error_message)
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
    role = data.get("role") if isinstance(data, Mapping) else None
    if role == "user":
        f = _read(data, "user message", {"role": str, "content": list})
        return UserMessage(content=tuple(_block_from_dict(b, ("text", "image")) for b in f["content"]))
    if role == "assistant":
        f = _read(
            data,
            "assistant message",
            {"role": str, "content": list, "origin": dict, "stop_reason": str},
            {"usage": dict, "error_message": str},
        )
        return AssistantMessage(
            content=tuple(_block_from_dict(b, ("text", "thinking", "tool_call")) for b in f["content"]),
            origin=origin_from_dict(f["origin"]),
            stop_reason=f["stop_reason"],
            usage=usage_from_dict(f["usage"]) if "usage" in f else None,
            error_message=f.get("error_message"),
        )
    if role == "tool_result":
        f = _read(
            data,
            "tool result message",
            {"role": str, "tool_call_id": str, "tool_name": str, "content": list, "is_error": bool},
        )
        return ToolResultMessage(
            tool_call_id=f["tool_call_id"],
            tool_name=f["tool_name"],
            content=tuple(_block_from_dict(b, ("text", "image")) for b in f["content"]),
            is_error=f["is_error"],
        )
    raise ValueError(f"unknown message role: {role!r}")
