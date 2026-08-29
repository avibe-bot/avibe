"""Bidirectional tool-name compatibility for the OpenCode gateway protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .stream_wire import SSEFrameTokenizer, parse_sse_frame


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

    names = {
        function.get("name")
        for item in tools
        if isinstance(item, Mapping)
        and isinstance((function := item.get("function")), Mapping)
        and isinstance(function.get("name"), str)
    }
    aliases: dict[str, str] = {}
    for original, preferred in _OPENCODE_UPSTREAM_TOOL_ALIASES.items():
        if original not in names:
            continue
        alias = preferred
        suffix = 2
        while alias in names:
            alias = f"{preferred}_{suffix}"
            suffix += 1
        names.add(alias)
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
    """Restore aliases in complete SSE frames while retaining bounded parser state."""

    def __init__(self, aliases: Mapping[str, str]) -> None:
        self._aliases = aliases
        self._tokenizer = SSEFrameTokenizer()

    def feed(self, chunk: bytes) -> bytes:
        if not self._aliases:
            return chunk
        return b"".join(self._rewrite_frame(frame) for frame in self._tokenizer.feed(chunk))

    def finish(self) -> bytes:
        return self._tokenizer.drain_partial_frame()

    def _rewrite_frame(self, frame: bytes) -> bytes:
        _event_name, data = parse_sse_frame(frame)
        if data is None or data == b"[DONE]":
            return frame + b"\n\n"
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            return frame + b"\n\n"
        if not isinstance(decoded, dict) or not _rewrite_chat_response(decoded, self._aliases):
            return frame + b"\n\n"
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


def _rewrite_tool_definition(tool: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    translated = dict(tool)
    function = tool.get("function")
    if isinstance(function, Mapping):
        translated["function"] = _rewrite_name(function, aliases)
    return translated


def _rewrite_message(message: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    translated = dict(message)
    name = message.get("name")
    if isinstance(name, str) and name in aliases:
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
