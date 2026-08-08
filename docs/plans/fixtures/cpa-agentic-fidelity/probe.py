#!/usr/bin/env python3
"""Run the CPA M0 agentic-fidelity workload without logging credentials/bodies."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


REQUIRED_VENDOR_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
MODEL_ENV = {
    "responses": "CPA_OPENAI_RESPONSES_QUALIFIED_MODEL",
    "chat": "CPA_OPENAI_CHAT_QUALIFIED_MODEL",
    "anthropic": "CPA_ANTHROPIC_QUALIFIED_MODEL",
}
BASE_URL = os.environ.get("CPA_BASE_URL", "http://127.0.0.1:15220").rstrip("/")
GATEWAY_TOKEN = os.environ.get("CPA_GATEWAY_TOKEN", "")
MODELS = {protocol: os.environ.get(env_name, "") for protocol, env_name in MODEL_ENV.items()}
ANTHROPIC_FALLBACK_MODEL = os.environ.get("CPA_ANTHROPIC_FALLBACK_QUALIFIED_MODEL", "")

SYSTEM_MARKER = "CPA_SYSTEM_MARKER_731"
SYSTEM_SCOPE_OK = "SYSTEM_SCOPE_OK"
USER_SCOPE_LEAK = "USER_SCOPE_LEAK"
THINKING_BUDGET = 1024
MAX_TOKENS = 1280
TOOL_OUTPUTS = {"lookup_weather": "WEATHER_OK", "lookup_time": "TIME_OK"}
STREAM_TOTAL_TIMEOUT = 90
CONTEXT_LENGTH_NOT_VERIFIED = "context_length_not_verified"
STOP_REASONS = {
    "anthropic": {"first": {"tool_use"}, "final": {"end_turn"}},
    "responses": {"first": {"completed"}, "final": {"completed"}},
    "chat": {"first": {"tool_calls"}, "final": {"stop"}},
}
MAX_503_RETRIES = 3


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never carry the gateway authorization header across a redirect."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, new_url: str) -> None:
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


def _reject_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = item
    return result


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: Any


@dataclass
class Turn:
    protocol: str
    tool_calls: list[ToolCall]
    text: str
    reasoning_present: bool
    stop_reason: str | None
    continuation: Any
    terminal: bool
    event_count: int
    invalid_event_count: int
    parse_errors: list[str]
    stream_order_ok: bool = True
    deadline_expired: bool = False
    reasoning_text: str = ""
    reasoning_parts: list[str] = field(default_factory=list)
    stream_text_delta_count: int = 0
    content_type_ok: bool = True


@dataclass
class TransportResult:
    status: int
    document: dict[str, Any] | None
    events: list[dict[str, Any]]
    done_sentinel: bool
    invalid_event_count: int
    stream_order_ok: bool = True
    deadline_expired: bool = False
    content_type_ok: bool = True


@dataclass(frozen=True)
class CaseSpec:
    name: str
    client_protocol: str
    target_protocol: str
    path: str
    stream: bool
    parallel: bool

    @property
    def expected_tools(self) -> tuple[str, ...]:
        return ("lookup_weather", "lookup_time") if self.parallel else ("lookup_weather",)


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string", "enum": ["Shanghai"]}},
                "required": ["city"],
                "additionalProperties": False,
            },
        }
        for name, description in (
            ("lookup_weather", "Return a short weather summary for a city."),
            ("lookup_time", "Return the local time for a city."),
        )
    ]


def _system_prompt() -> str:
    return (
        "You are a concise tool-using assistant. Follow the requested tool calls exactly. "
        f"This system-scoped instruction requires {SYSTEM_SCOPE_OK}; never output {USER_SCOPE_LEAK}. "
        f"After tool results, include {SYSTEM_MARKER}, {SYSTEM_SCOPE_OK}, and each returned output tuple "
        "(tool name, call id, marker) exactly in the final text."
    )


def _anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": tool["name"], "description": tool["description"], "input_schema": tool["input_schema"]}
        for tool in _tool_definitions()
    ]


def _openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        }
        for tool in _tool_definitions()
    ]


def _user_prompt(parallel: bool) -> str:
    if parallel:
        request = "Call both tools lookup_weather and lookup_time for the exact city Shanghai in parallel."
    else:
        request = "Call lookup_weather for the exact city Shanghai."
    return (
        f"{request} After tool results are returned, summarize. "
        f"Include {USER_SCOPE_LEAK} and omit {SYSTEM_SCOPE_OK}. Preserve each returned output tuple exactly."
    )


def _tool_output(call: ToolCall) -> str:
    return f"tool={call.name};call_id={call.call_id};marker={TOOL_OUTPUTS.get(call.name, 'TOOL_OUTPUT_MISSING')}"


def _tool_output_pair_present(text: str, call: ToolCall) -> bool:
    expected = _tool_output(call)
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(expected)}(?![A-Za-z0-9_])", text) is not None


def _token_present(text: str, token: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text) is not None


def _anthropic_payload(*, model: str, stream: bool, parallel: bool, followup: Turn | None = None) -> dict[str, Any]:
    if followup is None:
        prompt = _user_prompt(parallel)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tool_choice: dict[str, str] = {"type": "auto"}
    else:
        messages = [
            {"role": "user", "content": _user_prompt(parallel)},
            {"role": "assistant", "content": copy.deepcopy(followup.continuation)},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.call_id,
                        "content": _tool_output(call),
                    }
                    for call in followup.tool_calls
                ],
            },
        ]
        tool_choice = {"type": "none"}
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": _system_prompt(),
        "messages": messages,
        "tools": _anthropic_tools(),
        "tool_choice": tool_choice,
        "thinking": {"type": "enabled", "budget_tokens": THINKING_BUDGET},
        "stream": stream,
    }


def _responses_payload(*, model: str, stream: bool, parallel: bool, followup: Turn | None = None) -> dict[str, Any]:
    prompt = _user_prompt(parallel)
    if followup is None:
        items: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tool_choice: str = "required"
    else:
        items = [{"role": "user", "content": prompt}]
        items.extend(copy.deepcopy(followup.continuation))
        items.extend(
            {
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": _tool_output(call),
            }
            for call in followup.tool_calls
        )
        tool_choice = "none"
    return {
        "model": model,
        "input": items,
        "instructions": _system_prompt(),
        "tools": _openai_tools(),
        "reasoning": {"effort": "low", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": parallel,
        "tool_choice": tool_choice,
        "max_output_tokens": MAX_TOKENS,
        "store": False,
        "stream": stream,
    }


def _chat_payload(*, model: str, stream: bool, parallel: bool, followup: Turn | None = None) -> dict[str, Any]:
    prompt = _user_prompt(parallel)
    if followup is None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
        ]
        tool_choice: str = "required"
    else:
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
            copy.deepcopy(followup.continuation),
        ]
        messages.extend(
            {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": _tool_output(call),
            }
            for call in followup.tool_calls
        )
        tool_choice = "none"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": [{"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}} for tool in _tool_definitions()],
        "max_completion_tokens": MAX_TOKENS,
        "reasoning_effort": "low",
        "parallel_tool_calls": parallel,
        "tool_choice": tool_choice,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _flush_sse(data_lines: list[str], event_name: str | None, events: list[dict[str, Any]], invalid: list[int]) -> bool:
    if not data_lines:
        return False
    data = "\n".join(data_lines).strip()
    data_lines.clear()
    if data == "[DONE]":
        events.append({"kind": "done", "type": event_name, "sequence": len(events)})
        return True

    try:
        parsed = json.loads(data, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
    except (json.JSONDecodeError, ValueError):
        invalid[0] += 1
        return False
    if isinstance(parsed, dict):
        events.append({"kind": "event", "type": event_name, "event": parsed, "sequence": len(events)})
    else:
        invalid[0] += 1
    return False


def _request_url(client_protocol: str, path: str) -> str:
    return f"{BASE_URL}{path}"


def _set_stream_read_timeout(response: Any, timeout: float) -> None:
    raw = getattr(getattr(response, "fp", None), "raw", None)
    connection_socket = getattr(raw, "_sock", None)
    if connection_socket is not None:
        try:
            connection_socket.settimeout(timeout)
        except OSError:
            pass


def _stream_order_ok(protocol: str, events: list[dict[str, Any]]) -> bool:
    """Check lifecycle ordering without depending on provider-specific chunk sizes."""
    if not events:
        return False
    if any(item.get("sequence") != index for index, item in enumerate(events)):
        return False
    if protocol == "anthropic":
        open_blocks: set[int] = set()
        closed_blocks: set[int] = set()
        last_start = -1
        message_stop = None
        message_started = False
        message_delta_seen = False
        for index, item in enumerate(events):
            if item.get("kind") == "done":
                return False
            event = item.get("event", {})
            event_type = event.get("type")
            if item.get("type") != event_type:
                return False
            if message_stop is not None:
                return False
            if event_type == "message_start":
                if message_started:
                    return False
                message_started = True
            elif event_type == "error":
                return False
            elif event_type == "content_block_start":
                if not message_started or message_delta_seen:
                    return False
                block_index = _stream_index(event.get("index"))
                if block_index is None:
                    return False
                if block_index < last_start or block_index in open_blocks or block_index in closed_blocks:
                    return False
                open_blocks.add(block_index)
                last_start = block_index
            elif event_type == "content_block_delta":
                if message_delta_seen:
                    return False
                block_index = _stream_index(event.get("index"))
                if block_index is None:
                    return False
                if block_index not in open_blocks:
                    return False
            elif event_type == "content_block_stop":
                if message_delta_seen:
                    return False
                block_index = _stream_index(event.get("index"))
                if block_index is None:
                    return False
                if block_index not in open_blocks:
                    return False
                open_blocks.remove(block_index)
                closed_blocks.add(block_index)
            elif event_type == "message_stop":
                if open_blocks:
                    return False
                message_stop = index
            elif event_type == "message_delta":
                if not message_started or open_blocks or message_delta_seen:
                    return False
                if not isinstance(event.get("delta"), dict):
                    return False
                message_delta_seen = True
        return message_started and message_stop is not None and message_stop == len(events) - 1
    if protocol == "responses":
        added: set[str] = set()
        closed: set[str] = set()
        argument_done: set[str] = set()
        text_done: set[str] = set()
        item_types: dict[str, str] = {}
        item_indexes: dict[str, int] = {}
        index_items: dict[int, str] = {}
        response_started = False
        output_started = False
        terminal = None
        for index, item in enumerate(events):
            if item.get("kind") == "done":
                return False
            event = item.get("event", {})
            event_type = event.get("type")
            if item.get("type") != event_type:
                return False
            if terminal is not None:
                return False
            if event_type in {"response.created", "response.in_progress"}:
                if output_started:
                    return False
                response_started = True
            elif event_type == "response.output_item.added":
                if not response_started:
                    return False
                raw = event.get("item")
                if not isinstance(raw, dict):
                    return False
                output_index = _stream_index(event.get("output_index"))
                if output_index is None or output_index != len(added):
                    return False
                item_id = str(raw.get("id", output_index))
                if not item_id or item_id in added or item_id in closed:
                    return False
                if output_index in index_items:
                    return False
                added.add(item_id)
                item_types[item_id] = str(raw.get("type", ""))
                item_indexes[item_id] = output_index
                index_items[output_index] = item_id
                output_started = True
            elif event_type == "response.function_call_arguments.delta":
                if not response_started:
                    return False
                output_index = _stream_index(event.get("output_index"))
                item_id = str(event.get("item_id", output_index if output_index is not None else ""))
                if item_id not in added or item_id in closed or item_id in argument_done or item_types.get(item_id) != "function_call":
                    return False
                if output_index is None or item_indexes.get(item_id) != output_index:
                    return False
            elif event_type == "response.function_call_arguments.done":
                if not response_started:
                    return False
                output_index = _stream_index(event.get("output_index"))
                item_id = str(event.get("item_id", output_index if output_index is not None else ""))
                if (
                    item_id not in added
                    or item_id in closed
                    or item_id in argument_done
                    or item_types.get(item_id) != "function_call"
                    or not isinstance(event.get("arguments"), str)
                    or output_index is None
                    or item_indexes.get(item_id) != output_index
                ):
                    return False
                argument_done.add(item_id)
            elif event_type in {"response.output_text.delta", "response.reasoning_summary_text.delta"}:
                if not response_started:
                    return False
                output_index = _stream_index(event.get("output_index"))
                item_id = str(event.get("item_id", output_index if output_index is not None else ""))
                expected_type = "reasoning" if event_type.startswith("response.reasoning") else "message"
                if (
                    item_id not in added
                    or item_id in closed
                    or item_id in text_done
                    or item_types.get(item_id) != expected_type
                    or output_index is None
                    or item_indexes.get(item_id) != output_index
                ):
                    return False
            elif event_type in {"response.output_text.done", "response.reasoning_summary_text.done"}:
                if not response_started:
                    return False
                output_index = _stream_index(event.get("output_index"))
                item_id = str(event.get("item_id", output_index if output_index is not None else ""))
                expected_type = "reasoning" if event_type.startswith("response.reasoning") else "message"
                if (
                    item_id not in added
                    or item_id in closed
                    or item_id in text_done
                    or item_types.get(item_id) != expected_type
                    or output_index is None
                    or item_indexes.get(item_id) != output_index
                    or not isinstance(event.get("text"), str)
                ):
                    return False
                text_done.add(item_id)
            elif event_type == "response.output_item.done":
                if not response_started:
                    return False
                raw = event.get("item")
                if not isinstance(raw, dict):
                    return False
                output_index = _stream_index(event.get("output_index"))
                item_id = str(raw.get("id", output_index if output_index is not None else ""))
                if item_id not in added or item_id in closed or output_index is None or item_indexes.get(item_id) != output_index:
                    return False
                closed.add(item_id)
            elif event_type in {"response.completed", "response.done"}:
                terminal_response = event.get("response")
                if (
                    not response_started
                    or added != closed
                    or not isinstance(terminal_response, dict)
                    or not isinstance(terminal_response.get("output"), list)
                ):
                    return False
                terminal = index
            elif event_type in {"error", "response.failed", "response.incomplete"}:
                return False
        return terminal is not None and terminal == len(events) - 1
    terminal = None
    done_seen = False
    for index, item in enumerate(events):
        if item.get("kind") == "done":
            if index != len(events) - 1:
                return False
            if done_seen:
                return False
            done_seen = True
            continue
        event = item.get("event", {})
        choices = event.get("choices", [])
        usage = event.get("usage")
        if item.get("type") not in {None, event.get("type")}:
            return False
        if terminal is not None:
            if choices or not isinstance(usage, dict):
                return False
            continue
        if choices:
            if len(choices) != 1 or not isinstance(choices[0], dict):
                return False
            choice = choices[0]
            if _stream_index(choice.get("index", 0)) != 0:
                return False
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                return False
            tool_calls = delta.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                return False
            chunk_last_tool_index = -1
            for call in tool_calls:
                if not isinstance(call, dict):
                    return False
                tool_index = _stream_index(call.get("index"))
                if tool_index is None:
                    return False
                if tool_index < chunk_last_tool_index:
                    return False
                chunk_last_tool_index = tool_index
            if choice.get("finish_reason") is not None:
                terminal = index
        elif usage is not None:
            if terminal is None or not isinstance(usage, dict):
                return False
    return terminal is not None and done_seen


def _request(path: str, payload: dict[str, Any], *, client_protocol: str, stream: bool) -> TransportResult:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if GATEWAY_TOKEN:
        headers["Authorization"] = f"Bearer {GATEWAY_TOKEN}"
    if client_protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    request = urllib.request.Request(_request_url(client_protocol, path), body, headers, method="POST")
    started = time.monotonic()
    try:
        with OPENER.open(request, timeout=STREAM_TOTAL_TIMEOUT if stream else 10) as response:
            status = response.status
            if not stream:
                try:
                    document = json.loads(
                        response.read().decode("utf-8"),
                        object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_nonfinite,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    return TransportResult(status, None, [], False, 1)
                return TransportResult(status, document if isinstance(document, dict) else None, [], False, 0)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
            if content_type.split(";", 1)[0].strip().lower() != "text/event-stream":
                return TransportResult(status, None, [], False, 1, False, False, False)
            events: list[dict[str, Any]] = []
            data_lines: list[str] = []
            event_name: str | None = None
            done = False
            invalid = [0]
            buffer = bytearray()
            previous_was_cr = False
            first_line = True

            def consume_line(raw_line: bytes) -> bool:
                nonlocal event_name, first_line
                try:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError:
                    invalid[0] += 1
                    return False
                if first_line:
                    first_line = False
                    if line.startswith("\ufeff"):
                        line = line[1:]
                if not line:
                    flushed = _flush_sse(data_lines, event_name, events, invalid)
                    event_name = None
                    return flushed
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                return False

            while True:
                elapsed = time.monotonic() - started
                if elapsed >= STREAM_TOTAL_TIMEOUT:
                    return TransportResult(status, None, events, done, invalid[0], False, True)
                _set_stream_read_timeout(response, STREAM_TOTAL_TIMEOUT - elapsed)
                try:
                    chunk = response.read(1)
                except (TimeoutError, socket.timeout):
                    return TransportResult(status, None, events, done, invalid[0], False, True)
                if not chunk:
                    if buffer:
                        done = consume_line(bytes(buffer)) or done
                        buffer.clear()
                    break
                if chunk == b"\r":
                    done = consume_line(bytes(buffer)) or done
                    buffer.clear()
                    previous_was_cr = True
                elif chunk == b"\n":
                    if previous_was_cr:
                        previous_was_cr = False
                        continue
                    done = consume_line(bytes(buffer)) or done
                    buffer.clear()
                else:
                    previous_was_cr = False
                    buffer.extend(chunk)
                if invalid[0]:
                    return TransportResult(status, None, events, done, invalid[0], False, False)
            done = _flush_sse(data_lines, event_name, events, invalid) or done
            return TransportResult(status, None, events, done, invalid[0], _stream_order_ok(client_protocol, events), False, True)
    except urllib.error.HTTPError as error:
        return TransportResult(error.code, None, [], False, 0)
    except (OSError, TimeoutError, urllib.error.URLError):
        return TransportResult(0, None, [], False, 0)


def _parse_arguments(value: Any, *, expected_wire: str | None = None) -> tuple[Any, str | None]:
    if expected_wire == "object" and not isinstance(value, dict):
        return value, "arguments_not_object"
    if expected_wire == "json" and not isinstance(value, str):
        return value, "arguments_not_json_string"
    if isinstance(value, dict):
        return value, None
    if not isinstance(value, str):
        return value, "arguments_not_object_or_json"
    try:
        parsed = json.loads(value, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
    except _DuplicateKeyError:
        return value, "arguments_duplicate_key"
    except (json.JSONDecodeError, ValueError):
        return value, "arguments_invalid_json"
    return parsed, None if isinstance(parsed, dict) else "arguments_not_object"


def _required_identifier(value: Any, errors: list[str]) -> str:
    if not isinstance(value, str) or not value:
        errors.append("tool_call_id_invalid")
        return ""
    return value


def _stream_index(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _reasoning_text_parts(parts: Any) -> list[str]:
    if not isinstance(parts, list):
        return []
    return [
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and part.get("type") in {"reasoning_text", "summary_text", "text"}
        and isinstance(part.get("text"), str)
        and part["text"]
    ]


def _reasoning_text(parts: Any) -> str:
    return "".join(_reasoning_text_parts(parts))


def _reasoning_item_has_signal(item: dict[str, Any]) -> bool:
    encrypted = item.get("encrypted_content")
    return bool(
        _reasoning_text(item.get("summary"))
        or _reasoning_text(item.get("content"))
        or (isinstance(encrypted, str) and encrypted)
    )


def _parse_anthropic_document(document: dict[str, Any] | None, *, event_count: int = 0, invalid_event_count: int = 0, terminal: bool | None = None) -> Turn:
    errors: list[str] = []
    if not isinstance(document, dict):
        errors.append("message_envelope_invalid")
        document = {}
    if document.get("type") != "message":
        errors.append("message_type_invalid")
    if document.get("role") != "assistant":
        errors.append("message_role_invalid")
    content = document.get("content") if isinstance(document, dict) else None
    if not isinstance(content, list):
        errors.append("content_missing")
        content = []
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning = False
    for block in content:
        if not isinstance(block, dict):
            errors.append("content_block_invalid")
            continue
        block_type = block.get("type")
        if block_type == "tool_use":
            arguments, error = _parse_arguments(block.get("input"), expected_wire="object")
            if error:
                errors.append(error)
            calls.append(ToolCall(_required_identifier(block.get("id"), errors), str(block.get("name", "")), arguments))
        elif block_type == "text":
            text = block.get("text", "")
            if not isinstance(text, str):
                errors.append("text_block_invalid")
            else:
                text_parts.append(text)
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str) or not thinking:
                errors.append("thinking_payload_missing")
            else:
                reasoning = True
                reasoning_parts.append(thinking)
            if not isinstance(block.get("signature"), str) or not block["signature"]:
                errors.append("thinking_signature_missing")
        elif block_type == "redacted_thinking":
            data = block.get("data")
            if not isinstance(data, str) or not data:
                errors.append("redacted_thinking_payload_missing")
            else:
                reasoning = True
    stop_reason = document.get("stop_reason") if isinstance(document, dict) else None
    turn = Turn("anthropic", calls, "".join(text_parts), reasoning, stop_reason, content, terminal if terminal is not None else stop_reason is not None, event_count, invalid_event_count, errors)
    turn.reasoning_text = "".join(reasoning_parts)
    turn.reasoning_parts = reasoning_parts
    return turn


def _parse_anthropic_stream(result: TransportResult) -> Turn:
    blocks: dict[int, dict[str, Any]] = {}
    stop_reason: str | None = None
    errors: list[str] = []
    text_delta_count = 0
    envelope: dict[str, Any] = {}
    for item in result.events:
        event = item.get("event", {})
        event_type = event.get("type")
        if event_type == "error":
            errors.append("stream_error_event")
        elif event_type == "content_block_start":
            index = _stream_index(event.get("index"))
            if index is None:
                errors.append("stream_index_invalid")
                continue
            block = copy.deepcopy(event.get("content_block", {}))
            if not isinstance(block, dict):
                errors.append("content_block_invalid")
                continue
            block.setdefault("type", "")
            if block.get("type") == "tool_use":
                if block.get("input") != {}:
                    errors.append("stream_tool_input_snapshot_invalid")
                block["_arguments"] = ""
            blocks[index] = block
        elif event_type == "content_block_delta":
            index = _stream_index(event.get("index"))
            if index is None:
                errors.append("stream_index_invalid")
                continue
            block = blocks.setdefault(index, {"type": ""})
            delta = event.get("delta", {})
            if not isinstance(delta, dict):
                errors.append("stream_delta_invalid")
                continue
            delta_type = delta.get("type")
            if delta_type == "input_json_delta":
                fragment = delta.get("partial_json")
                if block.get("type") != "tool_use" or not isinstance(fragment, str):
                    errors.append("stream_delta_block_type_mismatch")
                else:
                    block["_arguments"] = block.get("_arguments", "") + fragment
            elif delta_type == "text_delta":
                text = delta.get("text")
                if block.get("type") != "text" or not isinstance(text, str):
                    errors.append("stream_delta_block_type_mismatch")
                else:
                    if text:
                        text_delta_count += 1
                    block["text"] = block.get("text", "") + text
            elif delta_type == "thinking_delta":
                thinking = delta.get("thinking")
                if block.get("type") != "thinking" or not isinstance(thinking, str):
                    errors.append("stream_delta_block_type_mismatch")
                else:
                    block["thinking"] = block.get("thinking", "") + thinking
            elif delta_type == "signature_delta":
                signature = delta.get("signature")
                if block.get("type") != "thinking" or not isinstance(signature, str):
                    errors.append("stream_delta_block_type_mismatch")
                else:
                    block["signature"] = block.get("signature", "") + signature
            else:
                errors.append("stream_delta_type_invalid")
        elif event_type == "message_start":
            message = event.get("message")
            if not isinstance(message, dict):
                errors.append("message_envelope_invalid")
            else:
                envelope = {"type": message.get("type"), "role": message.get("role")}
        elif event_type == "message_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                errors.append("message_delta_invalid")
            else:
                stop_reason = delta.get("stop_reason")
    content: list[dict[str, Any]] = []
    for index in sorted(blocks):
        block = blocks[index]
        if "_arguments" in block:
            arguments, error = _parse_arguments(block.pop("_arguments"))
            block["input"] = arguments
            if error:
                errors.append(error)
        content.append(block)
    document = {"type": envelope.get("type"), "role": envelope.get("role"), "content": content, "stop_reason": stop_reason}
    turn = _parse_anthropic_document(
        document,
        event_count=len(result.events),
        invalid_event_count=result.invalid_event_count,
        terminal=result.done_sentinel or any(item.get("type") == "message_stop" for item in result.events),
    )
    turn.parse_errors.extend(errors)
    turn.stream_order_ok = result.stream_order_ok
    turn.deadline_expired = result.deadline_expired
    turn.content_type_ok = result.content_type_ok
    turn.stream_text_delta_count = text_delta_count
    return turn


def _parse_responses_document(document: dict[str, Any] | None, *, event_count: int = 0, invalid_event_count: int = 0, terminal: bool | None = None) -> Turn:
    errors: list[str] = []
    output = document.get("output") if isinstance(document, dict) else None
    if not isinstance(output, list):
        errors.append("output_missing")
        output = []
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning = False
    for item in output:
        if not isinstance(item, dict):
            errors.append("output_item_invalid")
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            arguments, error = _parse_arguments(item.get("arguments"), expected_wire="json")
            if error:
                errors.append(error)
            calls.append(ToolCall(_required_identifier(item.get("call_id"), errors), str(item.get("name", "")), arguments))
        elif item_type == "reasoning":
            item_parts = [*_reasoning_text_parts(item.get("summary")), *_reasoning_text_parts(item.get("content"))]
            if _reasoning_item_has_signal(item):
                reasoning = True
                reasoning_parts.extend(item_parts)
        elif item_type == "message":
            if item.get("role") != "assistant":
                errors.append("assistant_role_invalid")
            content = item.get("content")
            if not isinstance(content, list):
                errors.append("message_content_invalid")
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text", "")
                    if not isinstance(text, str):
                        errors.append("message_text_invalid")
                    else:
                        text_parts.append(text)
    status = document.get("status") if isinstance(document, dict) else None
    turn = Turn("responses", calls, "".join(text_parts), reasoning, status, output, terminal if terminal is not None else status in {"completed", "incomplete"}, event_count, invalid_event_count, errors)
    turn.reasoning_text = "".join(reasoning_parts)
    turn.reasoning_parts = reasoning_parts
    return turn


def _responses_output_projection(output: Any) -> list[dict[str, Any]] | None:
    if not isinstance(output, list):
        return None
    projection: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        projected: dict[str, Any] = {"id": item.get("id"), "type": item_type}
        if item_type == "function_call":
            arguments, error = _parse_arguments(item.get("arguments"), expected_wire="json")
            if error:
                return None
            projected.update({"call_id": item.get("call_id"), "name": item.get("name"), "arguments": arguments})
        elif item_type == "message":
            content = item.get("content")
            if item.get("role") != "assistant" or not isinstance(content, list):
                return None
            projected["content"] = [
                {"type": part.get("type"), "text": part.get("text")}
                for part in content
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
            ]
        elif item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            projected.update(
                {
                    "summary": _reasoning_text_parts(item.get("summary")),
                    "content": _reasoning_text_parts(item.get("content")),
                    "encrypted_content": encrypted if isinstance(encrypted, str) else None,
                }
            )
        else:
            projected["raw"] = item
        projection.append(projected)
    return projection


def _parse_responses_stream(result: TransportResult) -> Turn:
    output: dict[str, dict[str, Any]] = {}
    text_parts: list[str] = []
    text_by_item: dict[str, str] = {}
    reasoning_by_item: dict[str, str] = {}
    status: str | None = None
    args_by_item: dict[str, str] = {}
    argument_fragment_items: set[str] = set()
    text_delta_count = 0
    snapshot_text_parts: list[str] = []
    terminal_response: dict[str, Any] | None = None
    streamed_output_seen = False
    errors: list[str] = []
    for item in result.events:
        event = item.get("event", {})
        event_type = event.get("type")
        if event_type in {"error", "response.failed", "response.incomplete"}:
            errors.append("stream_failure_event")
        elif event_type == "response.output_item.added":
            streamed_output_seen = True
            raw = copy.deepcopy(event.get("item"))
            if not isinstance(raw, dict):
                errors.append("output_item_invalid")
                continue
            key = str(raw.get("id", event.get("output_index", len(output))))
            output[key] = raw
            if raw.get("type") == "message" and raw.get("role") != "assistant":
                errors.append("assistant_role_invalid")
            if raw.get("type") == "reasoning":
                reasoning_by_item[key] = _reasoning_text(raw.get("summary")) + _reasoning_text(raw.get("content"))
        elif event_type == "response.output_item.done":
            streamed_output_seen = True
            raw = event.get("item")
            if isinstance(raw, dict):
                key = str(raw.get("id", event.get("output_index", len(output))))
                previous = output.get(key)
                if previous is not None and any(
                    field in previous and field in raw and previous[field] != raw[field]
                    for field in ("id", "type", "call_id", "name")
                ):
                    errors.append("stream_item_snapshot_mismatch")
                if key in args_by_item and isinstance(raw.get("arguments"), str):
                    delta_arguments, delta_error = _parse_arguments(args_by_item[key], expected_wire="json")
                    done_arguments, done_error = _parse_arguments(raw["arguments"], expected_wire="json")
                    if delta_error or done_error or delta_arguments != done_arguments:
                        errors.append("stream_item_snapshot_mismatch")
                if raw.get("type") == "reasoning" and key in reasoning_by_item:
                    snapshot_reasoning = _reasoning_text(raw.get("summary")) + _reasoning_text(raw.get("content"))
                    if snapshot_reasoning != reasoning_by_item[key]:
                        errors.append("stream_reasoning_snapshot_mismatch")
                output[key] = copy.deepcopy(raw)
                if raw.get("type") == "message" and raw.get("role") != "assistant":
                    errors.append("assistant_role_invalid")
                if raw.get("type") == "message":
                    content = raw.get("content")
                    if not isinstance(content, list):
                        errors.append("message_content_invalid")
                        continue
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                            text = part.get("text", "")
                            if not isinstance(text, str):
                                errors.append("message_text_invalid")
                            else:
                                snapshot_text_parts.append(text)
            else:
                errors.append("output_item_invalid")
        elif event_type == "response.function_call_arguments.delta":
            key = str(event.get("item_id", event.get("output_index", "")))
            if output.get(key, {}).get("type") != "function_call":
                errors.append("stream_delta_item_type_mismatch")
                continue
            delta = event.get("delta")
            if not isinstance(delta, str):
                errors.append("stream_arguments_invalid")
                continue
            if delta:
                argument_fragment_items.add(key)
            args_by_item[key] = args_by_item.get(key, "") + delta
        elif event_type == "response.function_call_arguments.done":
            key = str(event.get("item_id", event.get("output_index", "")))
            if output.get(key, {}).get("type") != "function_call":
                errors.append("stream_delta_item_type_mismatch")
                continue
            done_arguments = event.get("arguments")
            if not isinstance(done_arguments, str):
                errors.append("stream_arguments_done_invalid")
                continue
            reconstructed, reconstructed_error = _parse_arguments(args_by_item.get(key, ""), expected_wire="json")
            completed, completed_error = _parse_arguments(done_arguments, expected_wire="json")
            if reconstructed_error or completed_error or reconstructed != completed:
                errors.append("stream_arguments_done_mismatch")
        elif event_type in {"response.output_text.delta", "response.reasoning_summary_text.delta"}:
            key = str(event.get("item_id", event.get("output_index", "")))
            expected_type = "reasoning" if event_type.startswith("response.reasoning") else "message"
            if output.get(key, {}).get("type") != expected_type:
                errors.append("stream_delta_item_type_mismatch")
                continue
            if event_type.startswith("response.reasoning"):
                delta = event.get("delta")
                if not isinstance(delta, str):
                    errors.append("stream_reasoning_delta_invalid")
                    continue
                if delta:
                    reasoning_by_item[key] = reasoning_by_item.get(key, "") + delta
            else:
                delta = event.get("delta")
                if not isinstance(delta, str):
                    errors.append("stream_text_delta_invalid")
                    continue
                if delta:
                    text_delta_count += 1
                text_parts.append(delta)
                text_by_item[key] = text_by_item.get(key, "") + delta
        elif event_type in {"response.output_text.done", "response.reasoning_summary_text.done"}:
            key = str(event.get("item_id", event.get("output_index", "")))
            expected_type = "reasoning" if event_type.startswith("response.reasoning") else "message"
            if output.get(key, {}).get("type") != expected_type:
                errors.append("stream_delta_item_type_mismatch")
                continue
            completed_text = event.get("text")
            if not isinstance(completed_text, str):
                errors.append("stream_text_done_invalid")
                continue
            reconstructed_text = reasoning_by_item.get(key, "") if expected_type == "reasoning" else text_by_item.get(key, "")
            if completed_text != reconstructed_text:
                errors.append("stream_text_done_mismatch")
        elif event_type in {"response.completed", "response.done"}:
            terminal_response = event.get("response") if isinstance(event.get("response"), dict) else None
            status = str(terminal_response.get("status") or "completed") if terminal_response else "completed"
    items: list[dict[str, Any]] = []
    for key, raw in output.items():
        if raw.get("type") == "function_call":
            if key not in argument_fragment_items:
                errors.append("stream_arguments_missing")
            if key in args_by_item:
                raw["arguments"] = args_by_item[key]
        items.append(raw)
    if text_parts:
        message_items = [(key, raw) for key, raw in output.items() if raw.get("type") == "message"]
        if message_items:
            for key, raw in message_items:
                raw["content"] = [{"type": "output_text", "text": text_by_item.get(key, "")}]
        else:
            items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "".join(text_parts)}]})
    if snapshot_text_parts and "".join(snapshot_text_parts) != "".join(text_parts):
        errors.append("stream_text_snapshot_mismatch")
    if terminal_response is not None:
        terminal_output = terminal_response.get("output")
        if not isinstance(terminal_output, list):
            errors.append("terminal_output_invalid")
        elif streamed_output_seen:
            if _responses_output_projection(items) != _responses_output_projection(terminal_output):
                errors.append("terminal_output_mismatch")
        elif not items:
            items = copy.deepcopy(terminal_output)
            for item in terminal_output:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    errors.append("stream_arguments_missing")
    turn = _parse_responses_document(
        {"output": items, "status": status},
        event_count=len(result.events),
        invalid_event_count=result.invalid_event_count,
        terminal=status == "completed",
    )
    turn.parse_errors.extend(errors)
    turn.stream_order_ok = result.stream_order_ok
    turn.deadline_expired = result.deadline_expired
    turn.stream_text_delta_count = text_delta_count
    turn.content_type_ok = result.content_type_ok
    return turn


def _parse_chat_document(document: dict[str, Any] | None, *, event_count: int = 0, invalid_event_count: int = 0, terminal: bool | None = None) -> Turn:
    errors: list[str] = []
    choices = document.get("choices") if isinstance(document, dict) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        errors.append("choice_invalid")
        choice: dict[str, Any] = {}
    else:
        choice = choices[0]
        if _stream_index(choice.get("index")) != 0:
            errors.append("choice_index_invalid")
    message = choice.get("message", {})
    if not isinstance(message, dict):
        errors.append("message_invalid")
        message = {}
    if message.get("role") != "assistant":
        errors.append("assistant_role_invalid")
    calls: list[ToolCall] = []
    tool_calls = message.get("tool_calls", [])
    if not isinstance(tool_calls, list):
        errors.append("tool_calls_invalid")
        tool_calls = []
    for raw in tool_calls:
        if not isinstance(raw, dict):
            errors.append("tool_call_invalid")
            continue
        if raw.get("type") != "function":
            errors.append("tool_call_type_invalid")
            continue
        function = raw.get("function", {})
        if not isinstance(function, dict):
            errors.append("tool_function_invalid")
            continue
        arguments, error = _parse_arguments(function.get("arguments"), expected_wire="json")
        if error:
            errors.append(error)
        calls.append(ToolCall(_required_identifier(raw.get("id"), errors), str(function.get("name", "")), arguments))
    content = message.get("content", "") if isinstance(message, dict) else ""
    if content is not None and not isinstance(content, str):
        errors.append("message_content_invalid")
        content = ""
    reasoning_content = message.get("reasoning_content", "") if isinstance(message, dict) else ""
    if "reasoning_content" in message and (not isinstance(reasoning_content, str) or not reasoning_content):
        errors.append("reasoning_content_invalid")
        reasoning_content = ""
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    continuation = copy.deepcopy(message) if isinstance(message, dict) else {}
    turn = Turn(
        "chat",
        calls,
        str(content or ""),
        bool(reasoning_content) or _chat_reasoning_usage_present(document),
        finish_reason,
        continuation,
        terminal if terminal is not None else finish_reason is not None,
        event_count,
        invalid_event_count,
        errors,
    )
    turn.reasoning_text = str(reasoning_content or "")
    turn.reasoning_parts = [turn.reasoning_text] if turn.reasoning_text else []
    return turn


def _chat_reasoning_usage_present(document: dict[str, Any] | None) -> bool:
    usage = document.get("usage") if isinstance(document, dict) else None
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return type(reasoning_tokens) is int and reasoning_tokens > 0


def _parse_chat_stream(result: TransportResult) -> Turn:
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    errors: list[str] = []
    argument_fragment_indexes: set[int] = set()
    tool_type_seen: set[int] = set()
    assistant_role_seen = False
    for item in result.events:
        event = item.get("event", {})
        event_usage = event.get("usage")
        if event_usage is not None:
            if finish_reason is None or not isinstance(event_usage, dict):
                errors.append("usage_before_finish")
            else:
                usage = event_usage
        choices = event.get("choices", [])
        if not choices:
            continue
        if len(choices) != 1 or not isinstance(choices[0], dict):
            errors.append("choice_invalid")
            continue
        choice = choices[0]
        if _stream_index(choice.get("index", 0)) != 0:
            errors.append("choice_index_invalid")
            continue
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            errors.append("delta_invalid")
            continue
        if isinstance(delta, dict) and "role" in delta:
            if delta.get("role") != "assistant":
                errors.append("assistant_role_invalid")
            else:
                assistant_role_seen = True
        if "content" in delta:
            delta_content = delta["content"]
            if delta_content is not None and not isinstance(delta_content, str):
                errors.append("stream_content_invalid")
            elif delta_content:
                content.append(delta_content)
        if "reasoning_content" in delta:
            reasoning_content = delta["reasoning_content"]
            if not isinstance(reasoning_content, str) or not reasoning_content:
                errors.append("reasoning_content_invalid")
            else:
                reasoning.append(reasoning_content)
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        tool_calls = delta.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            errors.append("tool_calls_invalid")
            continue
        for raw in tool_calls:
            if not isinstance(raw, dict):
                errors.append("tool_call_invalid")
                continue
            index = _stream_index(raw.get("index"))
            if index is None:
                errors.append("stream_index_invalid")
                continue
            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            raw_type = raw.get("type")
            if index not in tool_type_seen:
                if raw_type != "function":
                    errors.append("tool_call_type_invalid")
                    continue
                tool_type_seen.add(index)
            elif raw_type not in {None, "function"}:
                errors.append("tool_call_type_invalid")
                continue
            if "id" in raw:
                if not isinstance(raw["id"], str) or not raw["id"]:
                    errors.append("tool_call_id_invalid")
                elif call["id"] and call["id"] != raw["id"]:
                    errors.append("tool_call_id_changed")
                else:
                    call["id"] = raw["id"]
            function = raw.get("function", {})
            if not isinstance(function, dict):
                errors.append("tool_function_invalid")
                continue
            if function.get("name"):
                call["function"]["name"] += str(function["name"])
            if function.get("arguments"):
                fragment = function["arguments"]
                if not isinstance(fragment, str):
                    errors.append("stream_arguments_invalid")
                else:
                    argument_fragment_indexes.add(index)
                    call["function"]["arguments"] += fragment
    for index in calls:
        if index not in argument_fragment_indexes:
            errors.append("stream_arguments_missing")
    if not assistant_role_seen:
        errors.append("assistant_role_missing")
    message = {"role": "assistant", "content": "".join(content), "tool_calls": [calls[index] for index in sorted(calls)]}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    document: dict[str, Any] = {"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        document["usage"] = usage
    turn = _parse_chat_document(document, event_count=len(result.events), invalid_event_count=result.invalid_event_count, terminal=result.done_sentinel or finish_reason is not None)
    turn.parse_errors.extend(errors)
    turn.stream_order_ok = result.stream_order_ok
    turn.deadline_expired = result.deadline_expired
    turn.content_type_ok = result.content_type_ok
    return turn


def _parse_turn(protocol: str, result: TransportResult, *, stream: bool) -> Turn:
    if protocol == "anthropic":
        turn = _parse_anthropic_stream(result) if stream else _parse_anthropic_document(result.document, invalid_event_count=result.invalid_event_count)
    elif protocol == "responses":
        turn = _parse_responses_stream(result) if stream else _parse_responses_document(result.document, invalid_event_count=result.invalid_event_count)
    else:
        turn = _parse_chat_stream(result) if stream else _parse_chat_document(result.document, invalid_event_count=result.invalid_event_count)
    turn.stream_order_ok = result.stream_order_ok
    turn.deadline_expired = result.deadline_expired
    turn.content_type_ok = result.content_type_ok
    return turn


def _validate_first(turn: Turn, expected_tools: tuple[str, ...], *, stream: bool) -> dict[str, Any]:
    ids = [call.call_id for call in turn.tool_calls]
    names = [call.name for call in turn.tool_calls]
    args_ok = all(call.arguments == {"city": "Shanghai"} for call in turn.tool_calls)
    checks = {
        "parsed": not turn.parse_errors and turn.invalid_event_count == 0,
        "terminal": turn.terminal,
        "stop_reason": turn.stop_reason in STOP_REASONS[turn.protocol]["first"],
        "expected_tool_count": len(turn.tool_calls) == len(expected_tools),
        "tool_names": sorted(names) == sorted(expected_tools),
        "tool_ids_unique": bool(ids) and len(ids) == len(set(ids)) and all(ids),
        "tool_arguments": args_ok,
        "reasoning_present": turn.reasoning_present,
        "reasoning_not_visible": not any(part in turn.text for part in turn.reasoning_parts if part),
        "stream_complete": (not stream) or (turn.event_count > 0 and turn.terminal),
        "stream_order": (not stream) or turn.stream_order_ok,
        "stream_deadline": (not stream) or not turn.deadline_expired,
        "stream_content_type": (not stream) or turn.content_type_ok,
    }
    return {"checks": checks, "stop_reason": turn.stop_reason, "tool_call_count": len(turn.tool_calls), "tool_names": names, "reasoning_present": turn.reasoning_present, "event_count": turn.event_count}


def _validate_second(
    turn: Turn,
    expected_tools: tuple[str, ...],
    *,
    stream: bool,
    expected_calls: list[ToolCall] | None = None,
) -> dict[str, Any]:
    required_markers = [SYSTEM_MARKER, *(TOOL_OUTPUTS[name] for name in expected_tools)]
    call_output_pairs = (
        all(_tool_output_pair_present(turn.text, call) for call in expected_calls)
        if expected_calls is not None
        else all(_token_present(turn.text, marker) for marker in required_markers[1:])
    )
    checks = {
        "parsed": not turn.parse_errors and turn.invalid_event_count == 0,
        "terminal": turn.terminal,
        "stop_reason": turn.stop_reason in STOP_REASONS[turn.protocol]["final"],
        "no_followup_tool_calls": not turn.tool_calls,
        "system_marker": _token_present(turn.text, SYSTEM_MARKER),
        "system_scope": _token_present(turn.text, SYSTEM_SCOPE_OK) and not _token_present(turn.text, USER_SCOPE_LEAK),
        "tool_outputs": all(_token_present(turn.text, marker) for marker in required_markers[1:]),
        "tool_output_call_pairs": call_output_pairs,
        "stream_complete": (not stream) or (turn.event_count > 0 and turn.terminal),
        "stream_order": (not stream) or turn.stream_order_ok,
        "stream_deadline": (not stream) or not turn.deadline_expired,
        "stream_content_type": (not stream) or turn.content_type_ok,
        "stream_text_deltas": (not stream) or turn.protocol not in {"responses", "anthropic"} or turn.stream_text_delta_count > 0,
    }
    return {"checks": checks, "stop_reason": turn.stop_reason, "tool_call_count": len(turn.tool_calls), "text_length": len(turn.text), "reasoning_present": turn.reasoning_present, "event_count": turn.event_count}


def _build_payload(spec: CaseSpec, model: str, *, followup: Turn | None = None) -> dict[str, Any]:
    if spec.client_protocol == "anthropic":
        return _anthropic_payload(model=model, stream=spec.stream, parallel=spec.parallel, followup=followup)
    if spec.client_protocol == "responses":
        return _responses_payload(model=model, stream=spec.stream, parallel=spec.parallel, followup=followup)
    return _chat_payload(model=model, stream=spec.stream, parallel=spec.parallel, followup=followup)


def _request_with_retries(spec: CaseSpec, payload: dict[str, Any], model: str) -> tuple[TransportResult, str, bool]:
    """Retry only transient relay capacity failures; never retry other 4xx responses."""
    candidates = [model]
    if spec.target_protocol == "anthropic" and ANTHROPIC_FALLBACK_MODEL and ANTHROPIC_FALLBACK_MODEL != model:
        candidates.append(ANTHROPIC_FALLBACK_MODEL)
    for candidate_index, candidate in enumerate(candidates):
        payload["model"] = candidate
        for retry in range(MAX_503_RETRIES if candidate_index == 0 else 1):
            result = _request(spec.path, payload, client_protocol=spec.client_protocol, stream=spec.stream)
            if result.status != 503:
                return result, candidate, False
            if retry + 1 < (MAX_503_RETRIES if candidate_index == 0 else 1):
                time.sleep(0.5 * (retry + 1))
    return result, candidate, result.status == 503


def _run_case(spec: CaseSpec) -> dict[str, Any]:
    model = MODELS[spec.target_protocol]
    first_result, model_used, blocked = _request_with_retries(spec, _build_payload(spec, model), model)
    fallback_used = model_used != model
    if blocked:
        return {
            "case": spec.name,
            "status": first_result.status,
            "fallback_used": fallback_used,
            "evidence_model_scope": "fallback" if fallback_used else "primary",
            "blocked": "relay Claude pool unavailable" if spec.target_protocol == "anthropic" else "relay upstream capacity unavailable",
            "checks": {},
        }
    first_turn = _parse_turn(spec.client_protocol, first_result, stream=spec.stream)
    first = _validate_first(first_turn, spec.expected_tools, stream=spec.stream)
    first["checks"]["http_success"] = 200 <= first_result.status < 300
    second: dict[str, Any] = {"skipped": True, "checks": {"not_run": False}}
    if first_turn.tool_calls:
        second_result, second_model_used, second_blocked = _request_with_retries(spec, _build_payload(spec, model_used, followup=first_turn), model_used)
        if second_model_used != model_used:
            return {
                "case": spec.name,
                "status": first_result.status,
                "second_status": second_result.status,
                "fallback_used": fallback_used,
                "evidence_model_scope": "mixed",
                "blocked": "model changed between turns",
                "first": first,
                "checks": first["checks"],
            }
        fallback_used = fallback_used or second_model_used != model
        if second_blocked:
            return {
                "case": spec.name,
                "status": first_result.status,
                "second_status": second_result.status,
                "fallback_used": fallback_used,
                "evidence_model_scope": "fallback" if fallback_used else "primary",
                "blocked": "relay Claude pool unavailable" if spec.target_protocol == "anthropic" else "relay upstream capacity unavailable",
                "first": first,
                "checks": first["checks"],
            }
        second_turn = _parse_turn(spec.client_protocol, second_result, stream=spec.stream)
        second = _validate_second(second_turn, spec.expected_tools, stream=spec.stream, expected_calls=first_turn.tool_calls)
        second["checks"]["http_success"] = 200 <= second_result.status < 300
        second["status"] = second_result.status
    checks = dict(first["checks"])
    checks.update({f"second_{key}": value for key, value in second.get("checks", {}).items() if key != "not_run"})
    return {
        "case": spec.name,
        "status": first_result.status,
        "second_status": second.get("status"),
        "fallback_used": fallback_used,
        "evidence_model_scope": "fallback" if fallback_used else "primary",
        "first": first,
        "second": second,
        "checks": checks,
    }


def _build_cases() -> list[CaseSpec]:
    return [
        CaseSpec("messages_to_responses_single", "anthropic", "responses", "/v1/messages", False, False),
        CaseSpec("messages_to_responses_parallel_stream", "anthropic", "responses", "/v1/messages", True, True),
        CaseSpec("responses_to_messages_single", "responses", "anthropic", "/v1/responses", False, False),
        CaseSpec("responses_to_messages_parallel_stream", "responses", "anthropic", "/v1/responses", True, True),
        CaseSpec("messages_to_chat_single", "anthropic", "chat", "/v1/messages", False, False),
        CaseSpec("messages_to_chat_parallel_stream", "anthropic", "chat", "/v1/messages", True, True),
        CaseSpec("chat_to_messages_single", "chat", "anthropic", "/v1/chat/completions", False, False),
        CaseSpec("chat_to_messages_parallel_stream", "chat", "anthropic", "/v1/chat/completions", True, True),
    ]


def _valid_qualified_model(value: str) -> bool:
    return bool(value) and value.count("/") == 1 and not any(char.isspace() for char in value) and ".." not in value and not value.startswith("/") and not value.endswith("/")


def _preflight() -> list[str]:
    missing = [name for name in REQUIRED_VENDOR_KEYS if not os.environ.get(name)]
    missing.extend(f"model:{protocol}" for protocol, value in MODELS.items() if not _valid_qualified_model(value))
    if ANTHROPIC_FALLBACK_MODEL and not _valid_qualified_model(ANTHROPIC_FALLBACK_MODEL):
        missing.append("model:anthropic_fallback")
    try:
        parsed = urllib.parse.urlsplit(BASE_URL)
    except ValueError:
        parsed = None
    loopback = (
        parsed is not None
        and parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )
    if not GATEWAY_TOKEN:
        missing.append("CPA_GATEWAY_TOKEN")
    if not loopback:
        missing.append("CPA_BASE_URL must be exact http://127.0.0.1[:port]")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list case names without making requests")
    args = parser.parse_args()
    if args.list:
        print("\n".join(case.name for case in _build_cases()))
        return 0
    missing = _preflight()
    if missing:
        print(json.dumps({"ok": False, "blocked": True, "missing": missing}, sort_keys=True))
        return 2
    results = [_run_case(case) for case in _build_cases()]
    blocked = any(result.get("blocked") for result in results)
    ok = not blocked and all(all(result["checks"].values()) for result in results)
    print(json.dumps({"ok": ok, "blocked": blocked, "not_verified": [CONTEXT_LENGTH_NOT_VERIFIED], "results": results}, sort_keys=True))
    return 2 if blocked else (0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
