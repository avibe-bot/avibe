"""Bidirectional tool-name compatibility for the OpenCode gateway protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .stream_wire import SSE_MAX_FRAME_BYTES, SSEFrameTokenizer, parse_sse_frame


_OPENCODE_UPSTREAM_TOOL_ALIASES = {
    # Some Anthropic-compatible relays reserve Claude Code's TodoWrite name
    # case-insensitively and reject OpenCode's otherwise valid definition.
    "todowrite": "avibe_todo_write",
}


@dataclass(frozen=True)
class ToolNameTranslation:
    request: Mapping[str, Any]
    response_aliases: Mapping[str, str]


def translate_opencode_tool_names(request: Mapping[str, Any]) -> ToolNameTranslation:
    """Alias incompatible definitions and every request-side reference to them."""

    tools = request.get("tools")
    if not isinstance(tools, list):
        return ToolNameTranslation(request=request, response_aliases={})

    names = [
        function.get("name")
        for item in tools
        if isinstance(item, Mapping)
        and isinstance((function := item.get("function")), Mapping)
        and isinstance(function.get("name"), str)
    ]
    occupied_names = {name.casefold() for name in names}
    aliases: dict[str, str] = {}
    for original in names:
        preferred = _OPENCODE_UPSTREAM_TOOL_ALIASES.get(original.casefold())
        if preferred is None or original in aliases:
            continue
        alias = preferred
        suffix = 2
        while alias.casefold() in occupied_names:
            alias = f"{preferred}_{suffix}"
            suffix += 1
        occupied_names.add(alias.casefold())
        aliases[original] = alias

    if not aliases:
        return ToolNameTranslation(request=request, response_aliases={})

    translated = dict(request)
    translated["tools"] = [
        _rewrite_tool_definition(item, aliases) if isinstance(item, Mapping) else item for item in tools
    ]
    messages = request.get("messages")
    if isinstance(messages, list):
        translated["messages"] = [
            _rewrite_message(item, aliases) if isinstance(item, Mapping) else item for item in messages
        ]
    tool_choice = request.get("tool_choice")
    if isinstance(tool_choice, Mapping):
        translated["tool_choice"] = _rewrite_named_function(tool_choice, aliases)
    return ToolNameTranslation(
        request=translated,
        response_aliases={alias: original for original, alias in aliases.items()},
    )


def rewrite_buffered_tool_names(payload: bytes, aliases: Mapping[str, str]) -> bytes:
    """Restore aliased tool names in one buffered OpenAI Chat response."""

    if not aliases:
        return payload
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return payload
    if not isinstance(decoded, dict) or not _rewrite_chat_response(decoded, aliases):
        return payload
    return json.dumps(decoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class StreamingToolNameRewriter:
    """Restore aliases across SSE deltas while retaining bounded parser state."""

    def __init__(self, aliases: Mapping[str, str]) -> None:
        self._aliases = aliases
        self._tokenizer = SSEFrameTokenizer()
        self._pending_frames: list[tuple[bytes, dict[str, Any]]] = []
        self._pending_size = 0

    def feed(self, chunk: bytes) -> bytes:
        if not self._aliases:
            return chunk
        output: list[bytes] = []
        for frame in self._tokenizer.feed(chunk):
            output.extend(self._consume_frame(frame))
        return b"".join(output)

    def finish(self) -> bytes:
        if not self._aliases:
            return self._tokenizer.drain_partial_frame()
        return b"".join(
            (*self._flush_pending(rewrite=True), self._tokenizer.drain_partial_frame())
        )

    def _consume_frame(self, frame: bytes) -> list[bytes]:
        _event_name, data = parse_sse_frame(frame)
        if data is None or data == b"[DONE]":
            return [*self._flush_pending(rewrite=True), frame + b"\n\n"]
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            return [*self._flush_pending(rewrite=True), frame + b"\n\n"]
        if not isinstance(decoded, dict):
            return [*self._flush_pending(rewrite=True), frame + b"\n\n"]

        output: list[bytes] = []
        if len(frame) > SSE_MAX_FRAME_BYTES:
            return [*self._flush_pending(rewrite=False), frame + b"\n\n"]
        if self._pending_size + len(frame) > SSE_MAX_FRAME_BYTES:
            output.extend(self._flush_pending(rewrite=False))
        self._pending_frames.append((frame, decoded))
        self._pending_size += len(frame)
        if self._has_partial_alias():
            return output
        output.extend(self._flush_pending(rewrite=True))
        return output

    def _has_partial_alias(self) -> bool:
        for assembled, _locations in self._assembled_stream_names().values():
            if assembled and assembled not in self._aliases and any(
                alias.startswith(assembled) for alias in self._aliases
            ):
                return True
        return False

    def _assembled_stream_names(
        self,
    ) -> dict[tuple[int, str, int], tuple[str, list[tuple[int, dict[str, Any]]]]]:
        assembled: dict[
            tuple[int, str, int],
            tuple[str, list[tuple[int, dict[str, Any]]]],
        ] = {}
        for frame_index, (_frame, payload) in enumerate(self._pending_frames):
            for key, function, fragment in _stream_function_names(payload):
                current, locations = assembled.get(key, ("", []))
                assembled[key] = (
                    current + fragment,
                    [*locations, (frame_index, function)],
                )
        return assembled

    def _flush_pending(self, *, rewrite: bool) -> list[bytes]:
        if not self._pending_frames:
            return []
        changed_frames: set[int] = set()
        if rewrite:
            for assembled, locations in self._assembled_stream_names().values():
                original = self._aliases.get(assembled)
                if original is None or not locations:
                    continue
                first_index, first_function = locations[0]
                first_function["name"] = original
                changed_frames.add(first_index)
                for frame_index, function in locations[1:]:
                    function.pop("name", None)
                    changed_frames.add(frame_index)

        output = [
            _render_sse_frame(frame, payload) if index in changed_frames else frame + b"\n\n"
            for index, (frame, payload) in enumerate(self._pending_frames)
        ]
        self._pending_frames.clear()
        self._pending_size = 0
        return output


def _render_sse_frame(frame: bytes, decoded: Mapping[str, Any]) -> bytes:
    rewritten = json.dumps(decoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    lines: list[bytes] = []
    inserted = False
    for line in frame.split(b"\n"):
        field, separator, _value = line.partition(b":")
        if separator and field == b"data":
            if not inserted:
                lines.append(b"data: " + rewritten)
                inserted = True
            continue
        lines.append(line)
    return b"\n".join(lines) + b"\n\n"


def _stream_function_names(
    payload: Mapping[str, Any],
) -> list[tuple[tuple[int, str, int], dict[str, Any], str]]:
    names: list[tuple[tuple[int, str, int], dict[str, Any], str]] = []
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return names
    for choice_position, choice in enumerate(choices):
        if not isinstance(choice, Mapping):
            continue
        choice_index = choice.get("index")
        if not isinstance(choice_index, int):
            choice_index = choice_position
        for envelope_name in ("delta", "message"):
            envelope = choice.get(envelope_name)
            if not isinstance(envelope, Mapping):
                continue
            function_call = envelope.get("function_call")
            if isinstance(function_call, dict):
                name = function_call.get("name")
                if isinstance(name, str):
                    names.append(((choice_index, "function_call", 0), function_call, name))
            tool_calls = envelope.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call_position, call in enumerate(tool_calls):
                if not isinstance(call, Mapping):
                    continue
                call_index = call.get("index")
                if not isinstance(call_index, int):
                    call_index = call_position
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if isinstance(name, str):
                    names.append(((choice_index, "tool_calls", call_index), function, name))
    return names


def _rewrite_tool_definition(tool: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    translated = dict(tool)
    function = tool.get("function")
    if isinstance(function, Mapping):
        translated["function"] = _rewrite_name(function, aliases)
    return translated


def _rewrite_message(message: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    translated = dict(message)
    name = message.get("name")
    if message.get("role") in {"tool", "function"} and isinstance(name, str) and name in aliases:
        translated["name"] = aliases[name]
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        translated["tool_calls"] = [
            _rewrite_named_function(call, aliases) if isinstance(call, Mapping) else call for call in tool_calls
        ]
    function_call = message.get("function_call")
    if isinstance(function_call, Mapping):
        translated["function_call"] = _rewrite_name(function_call, aliases)
    return translated


def _rewrite_named_function(value: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    translated = dict(value)
    function = value.get("function")
    if isinstance(function, Mapping):
        translated["function"] = _rewrite_name(function, aliases)
    return translated


def _rewrite_name(value: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    translated = dict(value)
    name = value.get("name")
    if isinstance(name, str) and name in aliases:
        translated["name"] = aliases[name]
    return translated


def _rewrite_chat_response(payload: dict[str, Any], aliases: Mapping[str, str]) -> bool:
    changed = False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for envelope_name in ("delta", "message"):
            envelope = choice.get(envelope_name)
            if not isinstance(envelope, dict):
                continue
            function_call = envelope.get("function_call")
            if isinstance(function_call, dict):
                name = function_call.get("name")
                if isinstance(name, str) and name in aliases:
                    function_call["name"] = aliases[name]
                    changed = True
            tool_calls = envelope.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if isinstance(name, str) and name in aliases:
                    function["name"] = aliases[name]
                    changed = True
    return changed
