"""Shared HTTP, origin, and wire-content helpers for native adapters."""

from __future__ import annotations

import asyncio
import base64
import io
import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Mapping
from dataclasses import replace
from typing import Any, TypeVar
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urlsplit, urlunsplit

import httpx

from core.agent_core.ai.errors import classify_error, parse_retry_after
from core.agent_core.ai.sse import SSEEvent, SSEParseError, SSEParser
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantContent,
    AssistantMessage,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserContent,
)
from core.agent_core.ai.provider import (
    BlockEnd,
    Done,
    MediaLoader,
    ModelEndpoint,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
)

ServedHopResolver = Callable[
    [Mapping[str, str]], Origin | None | Awaitable[Origin | None]
]
_T = TypeVar("_T")
_URL_RE = re.compile(
    r"""https?://(?:\[[^\]\s]+\]|[^\s"'<>()[\],]+)[^\s"'<>)}\],]*""",
    re.IGNORECASE,
)
WireTranslator = Callable[[AsyncIterator[SSEEvent]], AsyncIterator[Any]]

# A provider that never produces headers or pauses forever between chunks must
# not hold an agent turn indefinitely. These bounds are deliberately shared by
# every native adapter; the loop may retry the resulting network error only
# when no model output has been emitted.
CONNECT_TIMEOUT_S = 10.0
TIME_TO_FIRST_BYTE_TIMEOUT_S = 30.0
IDLE_CHUNK_TIMEOUT_S = 30.0
CLEANUP_TIMEOUT_S = 10.0
MAX_CUMULATIVE_OUTPUT_CHARS = 32 * 1024 * 1024


class OutputBudgetExceeded(ValueError):
    """A provider exceeded the shared cumulative assembled-output budget."""


def _string_buffer(value: str = "") -> io.StringIO:
    buffer = io.StringIO()
    buffer.write(value)
    return buffer


def usage_counter_error(
    value: Any,
    *,
    label: str,
    fields: tuple[str, ...],
    nested_fields: Mapping[str, tuple[str, ...]] | None = None,
) -> str | None:
    """Return an error for present usage counters with the wrong JSON shape."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        return f"{label} must be an object"
    for field in fields:
        raw = value.get(field)
        if raw is not None and (not isinstance(raw, int) or isinstance(raw, bool) or raw < 0):
            return f"{label} {field} must be a non-negative integer"
    for parent, child_fields in (nested_fields or {}).items():
        nested = value.get(parent)
        if nested is None:
            continue
        if not isinstance(nested, Mapping):
            return f"{label} {parent} must be an object"
        for field in child_fields:
            raw = nested.get(field)
            if raw is not None and (not isinstance(raw, int) or isinstance(raw, bool) or raw < 0):
                return f"{label} {parent}.{field} must be a non-negative integer"
    return None


class StreamAssembler:
    """Own canonical stream state shared by every native protocol adapter.

    Adapter modules translate wire events into these operations. This class
    owns visible-output accounting, tool-call identity, partial construction,
    and the exactly-one-terminal invariant.
    """

    def __init__(
        self,
        *,
        origin: Origin,
        protocol: str,
        verified_origin: bool = True,
        endpoint_url: str | None = None,
    ) -> None:
        self.origin = origin
        self.protocol = protocol
        self.verified_origin = verified_origin
        self._endpoint_url = endpoint_url
        self.content: list[AssistantContent] = []
        self.usage: Any = None
        self._terminal_seen = False
        self._emitted_output = False
        self._closed_indices: set[int] = set()
        self._slots: dict[tuple[str, Hashable], int] = {}
        self._visible_blocks: set[int] = set()
        self._tools: dict[Hashable, dict[str, Any]] = {}
        self._tool_order: list[Hashable] = []
        self._tool_keys_by_native_id: dict[str, Hashable] = {}
        self._fallback_tool_sequence = 0
        self._latest_fallback_tool_key: Hashable | None = None
        self._driver_mode = False
        self._output_chars = 0
        self._text_buffers: dict[int, io.StringIO] = {}
        self._thinking_buffers: dict[int, io.StringIO] = {}
        self._thinking_signature_buffers: dict[int, io.StringIO] = {}
        self._thinking_details: dict[Hashable, list[dict[str, Any]]] = {}
        self._thinking_detail_buffers: dict[tuple[Hashable, int, str], io.StringIO] = {}
        self._tool_argument_buffers: dict[Hashable, io.StringIO] = {}

    @property
    def streamed(self) -> bool:
        """Whether a non-empty model output was exposed to the loop."""

        return self._emitted_output

    def set_origin(self, origin: Origin, verified_origin: bool) -> None:
        self.origin = origin
        self.verified_origin = verified_origin

    def begin_driver(self) -> None:
        """Defer terminal marking while the shared lifecycle driver runs."""

        self._driver_mode = True

    def end_driver(self) -> None:
        self._driver_mode = False

    def set_usage(self, usage: Any) -> None:
        if usage is not None:
            self.usage = usage

    def _reserve_output(self, chars: int) -> None:
        if chars <= 0:
            return
        if self._output_chars + chars > MAX_CUMULATIVE_OUTPUT_CHARS:
            raise OutputBudgetExceeded(
                "provider cumulative output budget exceeded "
                f"({MAX_CUMULATIVE_OUTPUT_CHARS} characters)"
            )
        self._output_chars += chars

    def _text_value(self, index: int) -> str:
        buffer = self._text_buffers.get(index)
        if buffer is not None:
            return buffer.getvalue()
        block = self.content[index]
        return block.text or "" if isinstance(block, TextBlock) else ""

    def _thinking_value(self, index: int) -> str:
        buffer = self._thinking_buffers.get(index)
        if buffer is not None:
            return buffer.getvalue()
        block = self.content[index]
        return block.text if isinstance(block, ThinkingBlock) else ""

    def _thinking_signature_value(self, index: int) -> str:
        for key, candidate_index in self._slots.items():
            if candidate_index == index and key[0] == "thinking":
                self._materialize_thinking_details(key[1])
                break
        buffer = self._thinking_signature_buffers.get(index)
        if buffer is not None:
            return buffer.getvalue()
        block = self.content[index]
        return block.signature or "" if isinstance(block, ThinkingBlock) else ""

    def _tool_arguments_value(self, key: Hashable) -> str:
        buffer = self._tool_argument_buffers.get(key)
        return buffer.getvalue() if buffer is not None else ""

    def _materialize_thinking_details(self, key: Hashable) -> None:
        details = self._thinking_details.get(key)
        if details is None:
            return
        index = self._slots.get(("thinking", key))
        if index is None:
            return
        values = [dict(item) for item in details]
        for (owner, item_index, field), buffer in self._thinking_detail_buffers.items():
            if owner == key and item_index < len(values):
                values[item_index][field] = buffer.getvalue()
        signature = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        block = self.content[index]
        if isinstance(block, ThinkingBlock):
            self._thinking_signature_buffers[index] = _string_buffer(signature)
            self.content[index] = ThinkingBlock(
                text=self._thinking_value(index),
                signature=signature,
                redacted=block.redacted,
            )
        self._thinking_details.pop(key, None)
        for detail_key in tuple(self._thinking_detail_buffers):
            if detail_key[0] == key:
                del self._thinking_detail_buffers[detail_key]

    def _materialize_content(self) -> None:
        """Publish buffered fragments into canonical blocks at a stream boundary."""

        for key in tuple(self._thinking_details):
            self._materialize_thinking_details(key)
        for index, block in enumerate(self.content):
            if isinstance(block, TextBlock):
                self.content[index] = TextBlock(text=self._text_value(index))
            elif isinstance(block, ThinkingBlock):
                self.content[index] = ThinkingBlock(
                    text=self._thinking_value(index),
                    signature=self._thinking_signature_value(index) or None,
                    redacted=block.redacted,
                )

    def text_delta(self, key: Hashable, value: str) -> TextDelta | None:
        if not value:
            return None
        index = self._ensure_slot("text", key, TextBlock(text=""))
        if index in self._closed_indices:
            return None
        block = self.content[index]
        if not isinstance(block, TextBlock):
            return None
        self._reserve_output(len(value))
        buffer = self._text_buffers.setdefault(index, _string_buffer(block.text or ""))
        buffer.write(value)
        self._visible_blocks.add(index)
        self._emitted_output = True
        return TextDelta(index=index, delta=value)

    def ensure_text_slot(self, key: Hashable) -> int:
        return self._ensure_slot("text", key, TextBlock(text=""))

    def merge_text_snapshot(self, key: Hashable, value: str) -> TextDelta | None:
        if not value:
            return None
        index = self._ensure_slot("text", key, TextBlock(text=""))
        if index in self._closed_indices:
            return None
        block = self.content[index]
        if not isinstance(block, TextBlock):
            return None
        previous = self._text_value(index)
        self._reserve_output(max(0, len(value) - len(previous)))
        self._text_buffers[index] = _string_buffer(value)
        self._visible_blocks.add(index)
        self._emitted_output = True
        suffix = value[len(previous) :] if value.startswith(previous) else ""
        return TextDelta(index=index, delta=suffix) if suffix else None

    def thinking_delta(
        self,
        key: Hashable,
        value: str,
        *,
        signature: str | None = None,
    ) -> ThinkingDelta | None:
        if not value:
            return None
        if ("thinking", key) not in self._slots and signature:
            self._reserve_output(len(signature))
        index = self._ensure_slot(
            "thinking",
            key,
            ThinkingBlock(text="", signature=signature),
        )
        block = self.content[index]
        if index in self._closed_indices:
            return None
        if not isinstance(block, ThinkingBlock):
            return None
        self._reserve_output(len(value))
        buffer = self._thinking_buffers.setdefault(index, _string_buffer(block.text))
        buffer.write(value)
        self._visible_blocks.add(index)
        self._emitted_output = True
        return ThinkingDelta(index=index, delta=value)

    def replace_thinking(self, key: Hashable, value: str) -> ThinkingDelta | None:
        index = self._slots.get(("thinking", key))
        if index is None:
            index = self._ensure_slot("thinking", key, ThinkingBlock(text=""))
        if index in self._closed_indices:
            return None
        block = self.content[index]
        if isinstance(block, ThinkingBlock):
            previous = self._thinking_value(index)
            self._reserve_output(max(0, len(value) - len(previous)))
            self._thinking_buffers[index] = _string_buffer(value)
            if value:
                self._visible_blocks.add(index)
                self._emitted_output = True
            suffix = value[len(previous) :] if value.startswith(previous) else ""
            return ThinkingDelta(index=index, delta=suffix) if suffix else None
        return None

    def set_latest_thinking_signature(
        self,
        signature: str,
        *,
        fallback_key: Hashable | None = None,
    ) -> None:
        for index in range(len(self.content) - 1, -1, -1):
            block = self.content[index]
            if isinstance(block, ThinkingBlock):
                key = next(
                    (
                        slot_key[1]
                        for slot_key, slot_index in self._slots.items()
                        if slot_index == index and slot_key[0] == "thinking"
                    ),
                    None,
                )
                if key is not None:
                    self._materialize_thinking_details(key)
                    block = self.content[index]
                    if not isinstance(block, ThinkingBlock):
                        continue
                if block.signature is not None:
                    return
                self.content[index] = ThinkingBlock(
                    text=self._thinking_value(index),
                    signature=signature,
                    redacted=block.redacted,
                )
                self._thinking_signature_buffers[index] = _string_buffer(signature)
                return
        key = fallback_key if fallback_key is not None else ("thinking-signature", len(self.content))
        self.ensure_thinking_slot(key, signature=signature)

    def ensure_thinking_slot(
        self,
        key: Hashable,
        *,
        signature: str | None = None,
        redacted: bool = False,
    ) -> int:
        if ("thinking", key) not in self._slots and signature:
            self._reserve_output(len(signature))
        return self._ensure_slot(
            "thinking",
            key,
            ThinkingBlock(text="", signature=signature, redacted=redacted),
        )

    def redacted_thinking(self, key: Hashable, signature: str) -> int:
        index = self.ensure_thinking_slot(key, signature=signature, redacted=True)
        block = self.content[index]
        if isinstance(block, ThinkingBlock):
            self._thinking_buffers.pop(index, None)
            self._thinking_signature_buffers[index] = _string_buffer(signature)
            self.content[index] = ThinkingBlock(text="", signature=signature, redacted=True)
        return index

    def set_thinking_signature(self, key: Hashable, signature: str) -> None:
        index = self._slots.get(("thinking", key))
        if index is None:
            index = self._ensure_slot("thinking", key, ThinkingBlock(text=""))
        self._materialize_thinking_details(key)
        block = self.content[index]
        if isinstance(block, ThinkingBlock):
            self._reserve_output(
                max(0, len(signature) - len(self._thinking_signature_value(index)))
            )
            self._thinking_signature_buffers[index] = _string_buffer(signature)
            self.content[index] = ThinkingBlock(
                text=self._thinking_value(index),
                signature=signature,
                redacted=block.redacted,
            )

    def append_thinking_signature(self, key: Hashable, value: str) -> None:
        if not value:
            return
        index = self._slots.get(("thinking", key))
        if index is None:
            index = self._ensure_slot("thinking", key, ThinkingBlock(text=""))
        self._materialize_thinking_details(key)
        block = self.content[index]
        if isinstance(block, ThinkingBlock):
            self._reserve_output(len(value))
            buffer = self._thinking_signature_buffers.setdefault(
                index,
                _string_buffer(self._thinking_signature_value(index)),
            )
            buffer.write(value)
            self.content[index] = ThinkingBlock(
                text=self._thinking_value(index),
                signature=block.signature,
                redacted=block.redacted,
            )

    def merge_thinking_details(self, key: Hashable, details: list[Any]) -> None:
        valid = [detail for detail in details if isinstance(detail, Mapping)]
        if not valid:
            return
        index = self._ensure_slot("thinking", key, ThinkingBlock(text=""))
        accumulated = self._thinking_details.get(key)
        if accumulated is None:
            block = self.content[index]
            accumulated = []
            existing_signature = self._thinking_signature_value(index)
            if isinstance(block, ThinkingBlock) and existing_signature:
                try:
                    previous = json.loads(existing_signature)
                except (TypeError, ValueError):
                    previous = []
                if isinstance(previous, list):
                    accumulated.extend(item for item in previous if isinstance(item, Mapping))
            self._thinking_details[key] = accumulated
        if accumulated is None:
            return
        for detail in valid:
            item = dict(detail)
            previous = accumulated[-1] if accumulated else None
            if (
                isinstance(previous, dict)
                and previous.get("type") == item.get("type")
                and item.get("type") in {"reasoning.text", "reasoning.summary"}
                and isinstance(previous.get("text" if item["type"] == "reasoning.text" else "summary"), str)
                and isinstance(item.get("text" if item["type"] == "reasoning.text" else "summary"), str)
            ):
                field = "text" if item["type"] == "reasoning.text" else "summary"
                detail_index = len(accumulated) - 1
                buffer = self._thinking_detail_buffers.setdefault(
                    (key, detail_index, field),
                    _string_buffer(str(previous[field])),
                )
                self._reserve_output(len(item[field]))
                buffer.write(item[field])
                previous[field] = ""
                for name in ("id", "format", "index", "signature"):
                    if previous.get(name) in (None, "") and name in item:
                        previous[name] = item[name]
                continue
            for field in ("text", "summary"):
                value = item.get(field)
                if isinstance(value, str):
                    self._reserve_output(len(value))
            accumulated.append(item)

    def has_tools(self) -> bool:
        return any(isinstance(block, ToolCallBlock) for block in self.content)

    def has_content(self) -> bool:
        return bool(self.content)

    def has_text_slot(self, key: Hashable) -> bool:
        return ("text", key) in self._slots

    def tool_count(self) -> int:
        return len(self._tool_order)

    def tool_start(
        self,
        key: Hashable,
        *,
        name: str | None = None,
        call_id: str | None = None,
        native_id: str | None = None,
        signature: str | None = None,
    ) -> ToolCallStart | None:
        state = self._tools.setdefault(
            key,
            {
                "id": "",
                "native_id": None,
                "name": "",
                "arguments": "",
                "signature": None,
            },
        )
        if key not in self._tool_order:
            self._tool_order.append(key)
        if call_id:
            state["id"] = call_id
        if native_id:
            state["native_id"] = native_id
            self._tool_keys_by_native_id.setdefault(native_id, key)
        if name and not state["name"]:
            state["name"] = name
            native_id = state["native_id"]
            native_is_unique = native_id and not any(
                other is not state and other.get("id") == native_id
                for other in self._tools.values()
            )
            if "content_index" not in state and not state["id"] and native_is_unique:
                state["id"] = state["native_id"]
        if signature:
            state["signature"] = signature
        if not state["name"]:
            return None
        if "content_index" not in state:
            state["id"] = state["id"] or self._new_tool_id()
            native = state["native_id"]
            state["content_index"] = len(self.content)
            self.content.append(
                ToolCallBlock(
                    id=state["id"],
                    native_id=native,
                    name=state["name"],
                    arguments={},
                    signature=state["signature"],
                )
            )
            self._visible_blocks.add(state["content_index"])
            self._emitted_output = True
            return ToolCallStart(
                index=state["content_index"],
                id=state["id"],
                name=state["name"],
            )
        index = state["content_index"]
        if index in self._closed_indices:
            return None
        block = self.content[index]
        if isinstance(block, ToolCallBlock):
            self.content[index] = ToolCallBlock(
                id=block.id,
                native_id=state["native_id"] or block.native_id,
                name=state["name"] or block.name,
                arguments=dict(block.arguments),
                signature=state["signature"] or block.signature,
            )
        return None

    def tool_key(self, default: Hashable, *, native_id: str | None = None) -> Hashable:
        if native_id:
            return self._tool_keys_by_native_id.get(native_id, default)
        return default

    def bind_tool_alias(self, key: Hashable, native_id: str | None) -> None:
        if native_id:
            self._tool_keys_by_native_id[native_id] = key

    def fallback_tool_key(
        self,
        *,
        native_id: str | None = None,
        allocate: bool,
    ) -> Hashable | None:
        if native_id:
            existing = self._tool_keys_by_native_id.get(native_id)
            if existing is not None:
                self._latest_fallback_tool_key = existing
                return existing
        if not allocate:
            key = self._latest_fallback_tool_key
            if key is None:
                return None
            state = self._tools.get(key)
            if (
                state is not None
                and "content_index" in state
                and state["content_index"] not in self._closed_indices
                and not state.get("finished")
            ):
                return key
            return None
        key = ("tool-fallback", self._fallback_tool_sequence)
        self._fallback_tool_sequence += 1
        self._latest_fallback_tool_key = key
        self.bind_tool_alias(key, native_id)
        return key

    def tool_arguments(self, key: Hashable, value: str, *, replace: bool = False) -> ToolCallDelta | None:
        state = self._tools.get(key)
        if state is not None and "content_index" in state and state["content_index"] in self._closed_indices:
            return None
        if value:
            state = self._tools.setdefault(key, self._new_tool_state())
            previous = self._tool_arguments_value(key)
            if replace:
                self._reserve_output(max(0, len(value) - len(previous)))
                self._tool_argument_buffers[key] = _string_buffer(value)
            else:
                self._reserve_output(len(value))
                buffer = self._tool_argument_buffers.setdefault(
                    key,
                    _string_buffer(previous),
                )
                buffer.write(value)
        state = self._tools.get(key)
        if not value or state is None or "content_index" not in state:
            return None
        self._emitted_output = True
        return ToolCallDelta(index=state["content_index"], arguments_delta=value)

    def tool_arguments_snapshot(self, key: Hashable, value: str) -> ToolCallDelta | None:
        """Replace a provider's final argument snapshot and emit only its suffix."""

        state = self._tools.get(key)
        if state is None or "content_index" not in state:
            return None
        if state["content_index"] in self._closed_indices:
            return None
        previous = self._tool_arguments_value(key)
        if not value:
            return None
        if not value.startswith(previous):
            self._reserve_output(len(value))
            self._tool_argument_buffers[key] = _string_buffer(value)
            return None
        self._reserve_output(len(value) - len(previous))
        self._tool_argument_buffers[key] = _string_buffer(value)
        suffix = value[len(previous) :]
        if not suffix:
            return None
        self._emitted_output = True
        return ToolCallDelta(index=state["content_index"], arguments_delta=suffix)

    def pending_tool_arguments(self, key: Hashable) -> ToolCallDelta | None:
        state = self._tools.get(key)
        if state is None or "content_index" not in state:
            return None
        if state["content_index"] in self._closed_indices:
            return None
        arguments = self._tool_arguments_value(key)
        if not arguments:
            return None
        if state.get("arguments_emitted"):
            return None
        state["arguments_emitted"] = True
        return ToolCallDelta(
            index=state["content_index"],
            arguments_delta=arguments,
        )

    def tool_arguments_object(self, key: Hashable, value: Mapping[str, Any]) -> ToolCallDelta | None:
        state = self.tool_state(key)
        existing = state.get("arguments_obj")
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(value)
        state["arguments_obj"] = merged
        return self.tool_arguments(
            key,
            json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
            replace=True,
        )

    def set_tool_arguments_object(self, key: Hashable, value: Mapping[str, Any]) -> None:
        """Store an initial object without corrupting later JSON deltas."""

        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self._reserve_output(len(encoded))
        self.tool_state(key)["arguments_obj"] = dict(value)

    def tool_state(self, key: Hashable) -> dict[str, Any]:
        return self._tools.setdefault(
            key,
            self._new_tool_state(),
        )

    @staticmethod
    def _new_tool_state() -> dict[str, Any]:
        return {
            "id": "",
            "native_id": None,
            "name": "",
            "arguments": "",
            "signature": None,
        }

    def set_tool_item_id(self, key: Hashable, item_id: str) -> None:
        self.tool_state(key)["item_id"] = item_id

    def tool_item_id(self, key: Hashable) -> str:
        return str(self._tools.get(key, {}).get("item_id", ""))

    def tool_name(self, key: Hashable) -> str:
        return str(self._tools.get(key, {}).get("name", ""))

    def has_tool(self, key: Hashable) -> bool:
        state = self._tools.get(key)
        return state is not None and "content_index" in state

    def finish_tool(self, key: Hashable) -> None:
        state = self._tools.get(key)
        if state is not None and "content_index" in state:
            state["finished"] = True

    def unfinished_tool_calls(self) -> tuple[ToolCallBlock, ...]:
        unfinished: list[ToolCallBlock] = []
        for state in self._tools.values():
            if "content_index" not in state or state.get("finished"):
                continue
            block = self.content[state["content_index"]]
            if isinstance(block, ToolCallBlock):
                unfinished.append(block)
        return tuple(unfinished)

    def block_end_events(self) -> tuple[BlockEnd, ...]:
        events: list[BlockEnd] = []
        for index in sorted(self._visible_blocks):
            if index in self._closed_indices:
                continue
            self._closed_indices.add(index)
            events.append(BlockEnd(index=index))
        return tuple(events)

    def block_end(self, key: Hashable) -> BlockEnd | None:
        for kind in ("text", "thinking"):
            index = self._slots.get((kind, key))
            if index is not None:
                if index in self._closed_indices:
                    return None
                if index not in self._visible_blocks:
                    self._closed_indices.add(index)
                    return None
                self._closed_indices.add(index)
                return BlockEnd(index=index)
        state = self._tools.get(key)
        if state is not None and "content_index" in state:
            index = state["content_index"]
            if index in self._closed_indices:
                return None
            self._closed_indices.add(index)
            return BlockEnd(index=index)
        return None

    def finalize(self, stop_reason: str, *, error_message: str | None = None) -> AssistantMessage | ProviderError:
        self._materialize_content()
        for state in self._tools.values():
            if not state.get("name"):
                return self.error("tool call name is missing", kind="invalid_request")
        if stop_reason == "stop" and self.has_tools():
            stop_reason = "tool_use"
        final: list[AssistantContent] = []
        state_by_index = {
            state.get("content_index"): state
            for state in self._tools.values()
            if "content_index" in state
        }
        key_by_index = {
            state.get("content_index"): key
            for key, state in self._tools.items()
            if "content_index" in state
        }
        for index, block in enumerate(self.content):
            if not isinstance(block, ToolCallBlock):
                final.append(block)
                continue
            state = state_by_index.get(index, {})
            raw_arguments = self._tool_arguments_value(key_by_index[index])
            if not raw_arguments and isinstance(state.get("arguments_obj"), Mapping):
                arguments: dict[str, Any] | ProviderError = dict(state["arguments_obj"])
            else:
                arguments = parsed_arguments(raw_arguments, tool_name=block.name)
            if isinstance(arguments, ProviderError):
                if stop_reason == "tool_use":
                    return self.error(
                        arguments.message,
                        kind="invalid_request",
                    )
                # A length, safety, refusal, or error stop must still carry
                # every model-emitted call so the loop can settle it as an
                # unsuccessful tool result. The arguments are incomplete and
                # must not turn a provider stop into a malformed-request
                # failure.
                arguments = {}
            final.append(
                ToolCallBlock(
                    id=block.id,
                    native_id=state.get("native_id") or block.native_id,
                    name=block.name,
                    arguments=arguments,
                    signature=state.get("signature") or block.signature,
                )
            )
        return assistant_message(
            final,
            origin=self.origin,
            stop_reason=stop_reason,
            usage=self.usage,
            error_message=error_message,
            verified_origin=self.verified_origin,
        )

    def partial(self, *, stop_reason: str = "error") -> AssistantMessage | None:
        if self.usage is None and not self._emitted_output:
            return None
        self._materialize_content()
        content = list(self.content)
        if not self._emitted_output:
            content = []
        return partial_message(
            content,
            origin=self.origin,
            usage=self.usage,
            verified_origin=self.verified_origin,
            stop_reason=stop_reason,
        )

    def error(
        self,
        message: str,
        *,
        kind: str | None = None,
        code: str | None = None,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ProviderError:
        partial = self.partial() if self.streamed or self.usage is not None else None
        if kind == "invalid_request":
            error = ProviderError(
                kind="invalid_request",
                message=message,
                retryable=False,
                status=status,
                partial=partial,
            )
            return replace(error, message=sanitize_endpoint_text(error.message, self._endpoint_url))
        if kind is not None:
            error = ProviderError(
                kind=kind,
                message=message,
                retryable=kind in {"rate_limit", "overloaded", "network", "server"} and not self.streamed,
                retry_after_s=parse_retry_after((headers or {}).get("retry-after")),
                status=status,
                partial=partial,
            )
            return replace(error, message=sanitize_endpoint_text(error.message, self._endpoint_url))
        error = classify_error(
            body=message,
            code=code,
            status=status,
            headers=headers,
            streamed=self.streamed,
            partial=partial,
            protocol=self.protocol,
        )
        return replace(error, message=sanitize_endpoint_text(error.message, self._endpoint_url))

    def exception(
        self,
        exc: BaseException,
        *,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ProviderError:
        error = classify_error(
            exc=exc,
            status=status,
            headers=headers,
            streamed=self.streamed,
            partial=self.partial() if self.streamed or self.usage is not None else None,
        )
        return replace(error, message=sanitize_endpoint_text(error.message, self._endpoint_url))

    def aborted(self, reason: str | None = None) -> ProviderError:
        return ProviderError(
            kind="aborted",
            message=reason or "provider request aborted",
            retryable=False,
            partial=self.partial(stop_reason="aborted"),
        )

    def incomplete(self) -> ProviderError:
        return self.error(
            f"{self.protocol} stream ended before terminal event",
            kind="network",
        )

    def terminal(self, event: Done | ProviderError) -> Done | ProviderError | None:
        if isinstance(event, ProviderError):
            event = replace(
                event,
                message=sanitize_endpoint_text(event.message, self._endpoint_url),
            )
        if self._driver_mode:
            return event
        if self._terminal_seen:
            return None
        self._terminal_seen = True
        return event

    def _ensure_slot(self, kind: str, key: Hashable, block: AssistantContent) -> int:
        slot = (kind, key)
        index = self._slots.get(slot)
        if index is not None:
            return index
        index = len(self.content)
        self._slots[slot] = index
        self.content.append(block)
        return index

    def _new_tool_id(self) -> str:
        used = {state.get("id") for state in self._tools.values()}
        index = max(0, len(self._tool_order) - 1)
        candidate = f"call_{index}"
        while candidate in used:
            index += 1
            candidate = f"call_{index}"
        return candidate


def endpoint_origin(endpoint: ModelEndpoint) -> Origin:
    provider = endpoint.provider or f"endpoint:{credential_free_endpoint_identity(endpoint.base_url)}"
    if provider == "endpoint:":
        provider = f"endpoint:{endpoint.protocol}:{endpoint.model_id}"
    return Origin(
        provider=provider,
        api=endpoint.protocol,
        model=endpoint.model_id,
    )


def credential_free_endpoint_identity(base_url: str) -> str:
    """Return the endpoint location without userinfo, query, or fragment data."""

    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return _fallback_endpoint_identity(base_url)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        path = parsed.path or "/"
        return f"{parsed.scheme}://{host}{path.rstrip('/') or '/'}"
    return base_url.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "unknown"


def join_endpoint_url(base_url: str, suffix: str) -> str:
    """Append a protocol path while preserving the endpoint's query string.

    The endpoint may already be a complete protocol URL (useful for custom
    gateways), and the protocol suffix may add its own query parameters, as
    Some protocols add query parameters to their stream endpoint. URL parsing
    keeps credentials out of this operation's string manipulation and prevents
    a base query from becoming part of the request path.
    """

    base = urlsplit(base_url)
    extra = urlsplit(suffix)
    base_path = base.path.rstrip("/")
    suffix_path = extra.path.strip("/")
    base_leaf = base_path.rsplit("/", 1)[-1]
    suffix_leaf = suffix_path.rsplit("/", 1)[-1]
    same_operation = (
        ":" in base_leaf
        and ":" in suffix_leaf
        and base_leaf.split(":", 1)[1] == suffix_leaf.split(":", 1)[1]
    )
    already_joined = suffix_path and (
        base_path == suffix_path
        or base_path.endswith(f"/{suffix_path}")
        or same_operation
    )
    if suffix_path and not already_joined:
        path = f"{base_path}/{suffix_path}" if base_path else f"/{suffix_path}"
    else:
        path = base_path or "/"
    query = list(parse_qsl(base.query, keep_blank_values=True))
    query_keys = {key for key, _ in query}
    for key, value in parse_qsl(extra.query, keep_blank_values=True):
        if key in query_keys:
            continue
        query.append((key, value))
        query_keys.add(key)
    return urlunsplit((base.scheme, base.netloc, path, urlencode(query), ""))


def _fallback_endpoint_identity(base_url: str) -> str:
    value = base_url.split("?", 1)[0].split("#", 1)[0]
    scheme, separator, rest = value.partition("://")
    if not separator:
        return value.rstrip("/") or "unknown"
    authority, slash, path = rest.partition("/")
    authority = authority.rsplit("@", 1)[-1]
    return f"{scheme}://{authority}{('/' + path) if slash else '/'}"


def sanitize_endpoint_text(text: str, endpoint_url: str | None = None) -> str:
    """Remove endpoint credentials and query strings from diagnostics."""

    def sanitize(match: re.Match[str]) -> str:
        return credential_free_endpoint_identity(match.group(0))

    sanitized = text
    if endpoint_url:
        sanitized = sanitized.replace(endpoint_url, credential_free_endpoint_identity(endpoint_url))
    sanitized = _URL_RE.sub(sanitize, sanitized)
    return sanitized


def dispatch_wire_event(
    event: Any,
    *,
    known: frozenset[str],
    default: str | None = None,
    aliases: Mapping[str, str] | None = None,
) -> str | None:
    """Return a normalized known event type, ignoring unknown wire events like Pi.

    Native adapters use this at the protocol boundary before validating the
    shape of a known event. A missing ``type`` is allowed only for protocols
    whose stream frames are implicitly typed (Chat Completions).
    """

    if not isinstance(event, Mapping):
        return None
    raw_type = event.get("type", default)
    if not isinstance(raw_type, str):
        return None
    normalized = (aliases or {}).get(raw_type, raw_type)
    return normalized if normalized in known else None


async def resolve_served_origin(
    endpoint: ModelEndpoint,
    headers: Mapping[str, str],
    resolver: ServedHopResolver | None,
    *,
    gateway: bool,
    cancel: CancelToken,
) -> tuple[Origin, bool] | None:
    """Return the served origin and whether its signature data is verified."""

    direct = endpoint_origin(endpoint)
    if not gateway:
        return direct, True
    candidate = resolver(headers) if resolver is not None else default_served_hop_resolver(headers)
    if inspect.isawaitable(candidate):
        try:
            served = await _await_injected(candidate, cancel)
        except _CancelledDependency:
            return None
    else:
        served = candidate
    if cancel.cancelled:
        return None
    return served or direct, served is not None


def default_served_hop_resolver(headers: Mapping[str, str]) -> Origin | None:
    """Read the exact C-6 served-hop header."""

    normalized = {key.lower(): value for key, value in headers.items()}
    report = normalized.get("x-avibe-served-hop")
    if not report or not report.isascii():
        return None
    try:
        value = json.loads(report, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, Mapping):
        return None
    if set(value) != {"provider", "api", "model"} or any(not isinstance(value[key], str) for key in value):
        return None
    try:
        return Origin(provider=value["provider"], api=value["api"], model=value["model"])
    except (KeyError, TypeError, ValueError):
        return None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("served-hop report contains duplicate keys")
    return value


def auth_headers(endpoint: ModelEndpoint, *, provider: str, gateway: bool) -> dict[str, str]:
    headers = {str(key): str(value) for key, value in endpoint.request_headers.items()}
    if any(key.lower() in {"authorization", "x-api-key", "x-goog-api-key"} for key in headers):
        return headers
    if gateway or provider in {"openai", "openai_chat", "openai_responses"}:
        headers["Authorization"] = f"Bearer {endpoint.token}"
    elif provider == "anthropic":
        headers["x-api-key"] = endpoint.token
    else:
        headers["Authorization"] = f"Bearer {endpoint.token}"
    return headers


async def iter_sse_events(
    response: httpx.Response,
    cancel: CancelToken,
) -> AsyncIterator[SSEEvent]:
    """Yield SSE events while allowing a CancelToken to interrupt a stalled read."""

    parser = SSEParser()
    iterator = response.aiter_bytes().__aiter__()
    first_chunk = True
    while True:
        if cancel.cancelled:
            return
        try:
            chunk = await _await_network(
                iterator.__anext__(),
                cancel,
                timeout_s=(
                    TIME_TO_FIRST_BYTE_TIMEOUT_S
                    if first_chunk
                    else IDLE_CHUNK_TIMEOUT_S
                ),
            )
        except StopAsyncIteration:
            break
        if chunk is None:
            return
        first_chunk = False
        for event in parser.feed(chunk):
            yield event
    for event in parser.finish():
        yield event


async def drive_sse_stream(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    json_body: Any,
    headers: Mapping[str, str],
    cancel: CancelToken,
    assembler: StreamAssembler,
    endpoint: ModelEndpoint,
    resolver: ServedHopResolver | None,
    gateway: bool,
    translate: WireTranslator,
) -> AsyncIterator[Any]:
    """Own the complete request/stream/terminal/cleanup lifecycle.

    Protocol adapters provide only ``translate``. It receives the shared SSE
    iterator and yields canonical deltas or a candidate ``Done``/
    ``ProviderError``. The driver commits exactly one terminal event before
    closing the response, including when the transport raises while reading.
    """

    response: httpx.Response | None = None
    candidate: Done | ProviderError | None = None
    assembler.begin_driver()
    try:
        try:
            if cancel.cancelled:
                candidate = assembler.aborted(cancel.reason)
            else:
                request = client.build_request(method, url, json=json_body, headers=headers)
                response = await _await_network(
                    client.send(request, stream=True),
                    cancel,
                    timeout_s=CONNECT_TIMEOUT_S,
                )
                if response is None:
                    candidate = assembler.aborted(cancel.reason)
                else:
                    resolved_origin = await resolve_served_origin(
                        endpoint,
                        response.headers,
                        resolver,
                        gateway=gateway,
                        cancel=cancel,
                    )
                    if resolved_origin is None:
                        candidate = assembler.aborted(cancel.reason)
                    else:
                        assembler.set_origin(*resolved_origin)
                        if not 200 <= response.status_code < 300:
                            try:
                                body = await read_response_body(response, cancel)
                            except (GeneratorExit, asyncio.CancelledError):
                                raise
                            except Exception as exc:
                                candidate = assembler.exception(
                                    exc,
                                    status=response.status_code,
                                    headers=response.headers,
                                )
                            else:
                                if body is None:
                                    candidate = assembler.aborted(cancel.reason)
                                elif 300 <= response.status_code < 400:
                                    candidate = assembler.error(
                                        body or f"provider endpoint returned HTTP {response.status_code} redirect",
                                        status=response.status_code,
                                        headers=response.headers,
                                    )
                                else:
                                    candidate = assembler.error(
                                        body,
                                        status=response.status_code,
                                        headers=response.headers,
                                    )
                        else:
                            async for item in translate(iter_sse_events(response, cancel)):
                                if isinstance(item, (Done, ProviderError)):
                                    candidate = item
                                    break
                                yield item
                            if candidate is None:
                                candidate = (
                                    assembler.aborted(cancel.reason)
                                    if cancel.cancelled
                                    else assembler.incomplete()
                                )
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except OutputBudgetExceeded as exc:
            candidate = assembler.error(str(exc), kind="invalid_request")
        except SSEParseError as exc:
            candidate = assembler.error(str(exc), kind="invalid_request")
        except Exception as exc:
            candidate = assembler.exception(exc)

        if candidate is not None:
            terminal = assembler.terminal(candidate)
            if terminal is not None:
                yield terminal
    finally:
        assembler.end_driver()
        if response is not None:
            try:
                await _await_network(
                    response.aclose(),
                    CancelToken(),
                    timeout_s=CLEANUP_TIMEOUT_S,
                )
            except Exception:
                # The terminal event has already been delivered. Cleanup is
                # diagnostic only and must never create a second outcome.
                # asyncio.CancelledError remains a BaseException so consumer
                # and task cancellation continues through this cleanup path.
                pass


async def _await_network(
    awaitable: Awaitable[_T],
    cancel: CancelToken,
    *,
    timeout_s: float | None = None,
) -> _T | None:
    """Own one cancellable network await for opening, reads, and body reads."""

    if cancel.cancelled:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        return None
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {operation, cancellation},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await _cancel_and_join(operation)
            raise TimeoutError(
                f"provider network operation timed out after {timeout_s:.1f}s"
            )
        if cancellation in done and cancel.cancelled:
            await _cancel_and_join(operation)
            return None
        return operation.result()
    except TimeoutError:
        raise
    except BaseException:
        await _cancel_and_join(operation)
        raise
    finally:
        if not cancellation.done():
            cancellation.cancel()
            with _suppress_cancelled():
                await cancellation


async def _cancel_and_join(operation: asyncio.Future[Any]) -> None:
    """Cancel an I/O task without hanging on a non-cooperative awaitable."""

    if operation.done():
        _consume_task_result(operation)
        return
    operation.cancel()
    try:
        done, _ = await asyncio.wait({operation}, timeout=CLEANUP_TIMEOUT_S)
    except asyncio.CancelledError:
        if not operation.done():
            operation.add_done_callback(_consume_task_result)
        raise
    if operation not in done:
        operation.add_done_callback(_consume_task_result)
    else:
        _consume_task_result(operation)


def _consume_task_result(operation: asyncio.Future[Any]) -> None:
    try:
        operation.result()
    except BaseException:
        pass


class _CancelledDependency(Exception):
    """Internal marker for cancellation while loading or resolving injected data."""


async def _await_injected(awaitable: Awaitable[_T], cancel: CancelToken) -> _T:
    """Await a loader or resolver through the shared cancellation owner."""

    result = await _await_network(awaitable, cancel)
    if result is None and cancel.cancelled:
        raise _CancelledDependency
    return result  # type: ignore[return-value]


async def read_response_body(
    response: httpx.Response,
    cancel: CancelToken,
    *,
    max_bytes: int = 64 * 1024,
) -> str | None:
    """Read an error body through the shared cancellation owner with a byte cap."""

    iterator = response.aiter_bytes().__aiter__()
    first_chunk = True
    chunks: list[bytes] = []
    size = 0
    while size < max_bytes:
        try:
            chunk = await _await_network(
                iterator.__anext__(),
                cancel,
                timeout_s=(
                    TIME_TO_FIRST_BYTE_TIMEOUT_S
                    if first_chunk
                    else IDLE_CHUNK_TIMEOUT_S
                ),
            )
        except StopAsyncIteration:
            break
        if chunk is None:
            return None
        first_chunk = False
        if not chunk:
            continue
        remaining = max_bytes - size
        piece = bytes(chunk[:remaining])
        chunks.append(piece)
        size += len(piece)
        if len(piece) < len(chunk):
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


async def prepare_messages(
    messages: tuple[Any, ...],
    *,
    target: Any,
    supports_images: bool,
    protocol: str,
    media_loader: MediaLoader | None,
    cancel: CancelToken,
) -> tuple[tuple[Any, ...], dict[str, tuple[str, str]]] | ProviderError:
    """Transform history and resolve immutable media snapshots as one safe boundary."""

    try:
        from core.agent_core.ai.transform import transform_messages

        transformed = transform_messages(
            messages,
            target=target,
            supports_images=supports_images,
            protocol=protocol,
        )
        loaded_images = await load_images(transformed.messages, media_loader, cancel)
    except _CancelledDependency:
        return ProviderError(
            kind="aborted",
            message=cancel.reason or "provider request aborted",
            retryable=False,
        )
    except Exception as exc:
        return classify_error(body=f"request preparation failed: {type(exc).__name__}: {exc}")
    return transformed.messages, loaded_images


class _suppress_cancelled:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exception_type: type[BaseException] | None, exception: BaseException | None, traceback: Any) -> bool:
        return exception_type is asyncio.CancelledError


def json_object(data: str) -> dict[str, Any] | None:
    try:
        value = json.loads(data)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def image_data(block: ImageBlock) -> str:
    """Resolve the in-memory media token form used by hermetic adapter tests."""

    token = block.media_token
    if token.startswith("data:") and ";base64," in token:
        return token.split(";base64,", 1)[1]
    if token.startswith("data:,"):
        return base64.b64encode(unquote_to_bytes(token[6:])).decode("ascii")
    return token


async def load_image_data(
    block: ImageBlock,
    media_loader: MediaLoader | None,
    cancel: CancelToken | None = None,
) -> tuple[str, str]:
    """Resolve a media token just before the request is sent."""

    if media_loader is not None:
        if cancel is None:
            raw, mime_type = await media_loader.load(block.media_token)
        else:
            raw, mime_type = await _await_injected(media_loader.load(block.media_token), cancel)
        return base64.b64encode(raw).decode("ascii"), mime_type
    return image_data(block), block.mime_type


async def load_images(
    messages: tuple[Any, ...],
    media_loader: MediaLoader | None,
    cancel: CancelToken,
) -> dict[str, tuple[str, str]]:
    """Load every image token once for a request."""

    if media_loader is None:
        return {}
    tokens: list[str] = []
    for message in messages:
        content = getattr(message, "content", ())
        for block in content:
            if isinstance(block, ImageBlock) and block.media_token not in tokens:
                tokens.append(block.media_token)
    loaded: dict[str, tuple[str, str]] = {}
    for token in tokens:
        raw, mime_type = await _await_injected(media_loader.load(token), cancel)
        loaded[token] = (base64.b64encode(raw).decode("ascii"), mime_type)
    return loaded


def text_from_content(content: tuple[UserContent, ...]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text or "")
    return "\n".join(part for part in parts if part)


def content_parts(
    content: tuple[UserContent, ...],
    *,
    include_images: bool = True,
    loaded_images: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            if block.text:
                result.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock) and include_images:
            data, mime_type = (loaded_images or {}).get(block.media_token, (image_data(block), block.mime_type))
            result.append({"type": "image", "mime_type": mime_type, "data": data, "media_token": block.media_token})
    return result


def tool_schema(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }


def parsed_arguments(raw: str, *, tool_name: str = "") -> dict[str, Any] | ProviderError:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return _malformed_arguments(tool_name)
    if not isinstance(value, dict):
        return _malformed_arguments(tool_name)
    return value


def _malformed_arguments(tool_name: str) -> ProviderError:
    suffix = f" for tool {tool_name}" if tool_name else ""
    return ProviderError(
        kind="invalid_request",
        message=f"malformed tool arguments{suffix}: expected a JSON object",
        retryable=False,
    )


def assistant_message(
    content: list[AssistantContent],
    *,
    origin: Origin,
    stop_reason: str,
    usage: Any = None,
    error_message: str | None = None,
    verified_origin: bool = True,
) -> AssistantMessage:
    if not verified_origin:
        sanitized: list[AssistantContent] = []
        for block in content:
            if isinstance(block, ThinkingBlock):
                if block.redacted:
                    continue
                sanitized.append(replace(block, signature=None))
            elif isinstance(block, ToolCallBlock):
                sanitized.append(replace(block, signature=None))
            else:
                sanitized.append(block)
        content = sanitized
    return AssistantMessage(
        content=tuple(content),
        origin=origin,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=usage,
        error_message=error_message,
    )


def partial_message(
    content: list[AssistantContent],
    *,
    origin: Origin,
    usage: Any = None,
    verified_origin: bool = True,
    stop_reason: str = "aborted",
) -> AssistantMessage:
    if stop_reason in {"aborted", "error"}:
        content = [block for block in content if not isinstance(block, ToolCallBlock)]
    return assistant_message(
        content,
        origin=origin,
        stop_reason=stop_reason,
        usage=usage,
        verified_origin=verified_origin,
    )


def _default_provider(protocol: str) -> str:
    return {
        "anthropic": "anthropic",
        "openai_chat": "openai",
        "openai_responses": "openai",
    }.get(protocol, protocol)
