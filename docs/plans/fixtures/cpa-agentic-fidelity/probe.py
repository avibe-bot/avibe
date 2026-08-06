#!/usr/bin/env python3
"""Run the CPA M0 agentic-fidelity workload without logging credentials/bodies."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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

SYSTEM_MARKER = "CPA_SYSTEM_MARKER_731"
USER_PROMPT = (
    "Call the requested tools for Shanghai. After tool results are returned, "
    "reply with a concise sentence containing the system marker and each returned output marker."
)
THINKING_BUDGET = 1024
MAX_TOKENS = 1280
TOOL_OUTPUTS = {"lookup_weather": "WEATHER_OK", "lookup_time": "TIME_OK"}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never carry the gateway authorization header across a redirect."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, new_url: str) -> None:
        return None


OPENER = urllib.request.build_opener(_NoRedirectHandler())


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


@dataclass
class TransportResult:
    status: int
    document: dict[str, Any] | None
    events: list[dict[str, Any]]
    done_sentinel: bool
    invalid_event_count: int


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
                "properties": {"city": {"type": "string"}},
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
        f"After tool results, include {SYSTEM_MARKER} and each returned output marker in the final text."
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


def _anthropic_payload(*, model: str, stream: bool, parallel: bool, followup: Turn | None = None) -> dict[str, Any]:
    if followup is None:
        prompt = USER_PROMPT if parallel else "Call lookup_weather for Shanghai. After the result, summarize."
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tool_choice: dict[str, str] = {"type": "auto"}
    else:
        messages = [
            {"role": "user", "content": USER_PROMPT if parallel else "Call lookup_weather for Shanghai. After the result, summarize."},
            {"role": "assistant", "content": copy.deepcopy(followup.continuation)},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.call_id,
                        "content": TOOL_OUTPUTS.get(call.name, "TOOL_OUTPUT_MISSING"),
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
    prompt = USER_PROMPT if parallel else "Call lookup_weather for Shanghai. After the result, summarize."
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
                "output": TOOL_OUTPUTS.get(call.name, "TOOL_OUTPUT_MISSING"),
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
        "parallel_tool_calls": parallel,
        "tool_choice": tool_choice,
        "max_output_tokens": MAX_TOKENS,
        "store": False,
        "stream": stream,
    }


def _chat_payload(*, model: str, stream: bool, parallel: bool, followup: Turn | None = None) -> dict[str, Any]:
    prompt = USER_PROMPT if parallel else "Call lookup_weather for Shanghai. After the result, summarize."
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
                "content": TOOL_OUTPUTS.get(call.name, "TOOL_OUTPUT_MISSING"),
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
        events.append({"kind": "done", "type": event_name})
        return True
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        invalid[0] += 1
        return False
    if isinstance(parsed, dict):
        events.append({"kind": "event", "type": parsed.get("type"), "event": parsed})
    else:
        invalid[0] += 1
    return False


def _request(path: str, payload: dict[str, Any], *, stream: bool) -> TransportResult:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if GATEWAY_TOKEN:
        headers["Authorization"] = f"Bearer {GATEWAY_TOKEN}"
    request = urllib.request.Request(f"{BASE_URL}{path}", body, headers, method="POST")
    try:
        with OPENER.open(request, timeout=60) as response:
            status = response.status
            if not stream:
                try:
                    document = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return TransportResult(status, None, [], False, 1)
                return TransportResult(status, document if isinstance(document, dict) else None, [], False, 0)
            events: list[dict[str, Any]] = []
            data_lines: list[str] = []
            event_name: str | None = None
            done = False
            invalid = [0]
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    done = _flush_sse(data_lines, event_name, events, invalid) or done
                    event_name = None
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            done = _flush_sse(data_lines, event_name, events, invalid) or done
            return TransportResult(status, None, events, done, invalid[0])
    except urllib.error.HTTPError as error:
        return TransportResult(error.code, None, [], False, 0)
    except (OSError, TimeoutError, urllib.error.URLError):
        return TransportResult(0, None, [], False, 0)


def _parse_arguments(value: Any) -> tuple[Any, str | None]:
    if isinstance(value, dict):
        return value, None
    if not isinstance(value, str):
        return value, "arguments_not_object_or_json"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value, "arguments_invalid_json"
    return parsed, None if isinstance(parsed, dict) else "arguments_not_object"


def _parse_anthropic_document(document: dict[str, Any] | None, *, event_count: int = 0, invalid_event_count: int = 0, terminal: bool | None = None) -> Turn:
    errors: list[str] = []
    content = document.get("content") if isinstance(document, dict) else None
    if not isinstance(content, list):
        errors.append("content_missing")
        content = []
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    reasoning = False
    for block in content:
        if not isinstance(block, dict):
            errors.append("content_block_invalid")
            continue
        block_type = block.get("type")
        if block_type == "tool_use":
            arguments, error = _parse_arguments(block.get("input"))
            if error:
                errors.append(error)
            calls.append(ToolCall(str(block.get("id", "")), str(block.get("name", "")), arguments))
        elif block_type == "text":
            text_parts.append(str(block.get("text", "")))
        elif block_type in {"thinking", "redacted_thinking"}:
            reasoning = True
    stop_reason = document.get("stop_reason") if isinstance(document, dict) else None
    return Turn("anthropic", calls, "".join(text_parts), reasoning, stop_reason, content, terminal if terminal is not None else stop_reason is not None, event_count, invalid_event_count, errors)


def _parse_anthropic_stream(result: TransportResult) -> Turn:
    blocks: dict[int, dict[str, Any]] = {}
    stop_reason: str | None = None
    errors: list[str] = []
    for item in result.events:
        event = item.get("event", {})
        event_type = event.get("type")
        if event_type == "content_block_start":
            index = int(event.get("index", len(blocks)))
            block = copy.deepcopy(event.get("content_block", {}))
            block.setdefault("type", "")
            if block.get("type") == "tool_use":
                block["_arguments"] = ""
            blocks[index] = block
        elif event_type == "content_block_delta":
            index = int(event.get("index", 0))
            block = blocks.setdefault(index, {"type": ""})
            delta = event.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "input_json_delta":
                block["_arguments"] = block.get("_arguments", "") + str(delta.get("partial_json", ""))
            elif delta_type == "text_delta":
                block["text"] = block.get("text", "") + str(delta.get("text", ""))
            elif delta_type == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + str(delta.get("thinking", ""))
            elif delta_type == "signature_delta":
                block["signature"] = block.get("signature", "") + str(delta.get("signature", ""))
        elif event_type == "message_delta":
            stop_reason = event.get("delta", {}).get("stop_reason")
    content: list[dict[str, Any]] = []
    for index in sorted(blocks):
        block = blocks[index]
        if "_arguments" in block:
            arguments, error = _parse_arguments(block.pop("_arguments"))
            block["input"] = arguments
            if error:
                errors.append(error)
        content.append(block)
    document = {"content": content, "stop_reason": stop_reason}
    turn = _parse_anthropic_document(
        document,
        event_count=len(result.events),
        invalid_event_count=result.invalid_event_count,
        terminal=result.done_sentinel or any(item.get("type") == "message_stop" for item in result.events),
    )
    turn.parse_errors.extend(errors)
    return turn


def _parse_responses_document(document: dict[str, Any] | None, *, event_count: int = 0, invalid_event_count: int = 0, terminal: bool | None = None) -> Turn:
    errors: list[str] = []
    output = document.get("output") if isinstance(document, dict) else None
    if not isinstance(output, list):
        errors.append("output_missing")
        output = []
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    reasoning = False
    for item in output:
        if not isinstance(item, dict):
            errors.append("output_item_invalid")
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            arguments, error = _parse_arguments(item.get("arguments"))
            if error:
                errors.append(error)
            calls.append(ToolCall(str(item.get("call_id", "")), str(item.get("name", "")), arguments))
        elif item_type == "reasoning":
            reasoning = True
        elif item_type == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text_parts.append(str(part.get("text", "")))
    status = document.get("status") if isinstance(document, dict) else None
    return Turn("responses", calls, "".join(text_parts), reasoning, status, output, terminal if terminal is not None else status in {"completed", "incomplete"}, event_count, invalid_event_count, errors)


def _parse_responses_stream(result: TransportResult) -> Turn:
    for item in reversed(result.events):
        event = item.get("event", {})
        if event.get("type") in {"response.completed", "response.done"} and isinstance(event.get("response"), dict):
            return _parse_responses_document(event["response"], event_count=len(result.events), invalid_event_count=result.invalid_event_count, terminal=True)
    output: dict[str, dict[str, Any]] = {}
    text_parts: list[str] = []
    reasoning = False
    status: str | None = None
    args_by_item: dict[str, str] = {}
    for item in result.events:
        event = item.get("event", {})
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            raw = copy.deepcopy(event.get("item", {}))
            key = str(raw.get("id", event.get("output_index", len(output))))
            output[key] = raw
            if raw.get("type") == "reasoning":
                reasoning = True
        elif event_type == "response.output_item.done":
            raw = event.get("item")
            if isinstance(raw, dict):
                key = str(raw.get("id", event.get("output_index", len(output))))
                output[key] = copy.deepcopy(raw)
                if raw.get("type") == "reasoning":
                    reasoning = True
        elif event_type == "response.function_call_arguments.delta":
            key = str(event.get("item_id", event.get("output_index", "")))
            args_by_item[key] = args_by_item.get(key, "") + str(event.get("delta", ""))
        elif event_type in {"response.output_text.delta", "response.reasoning_summary_text.delta"}:
            if event_type.startswith("response.reasoning"):
                reasoning = True
            else:
                text_parts.append(str(event.get("delta", "")))
        elif event_type == "response.completed":
            status = "completed"
    items: list[dict[str, Any]] = []
    for key, raw in output.items():
        if raw.get("type") == "function_call" and key in args_by_item:
            raw["arguments"] = args_by_item[key]
        items.append(raw)
    if text_parts:
        items.append({"type": "message", "content": [{"type": "output_text", "text": "".join(text_parts)}]})
    return _parse_responses_document({"output": items, "status": status}, event_count=len(result.events), invalid_event_count=result.invalid_event_count, terminal=result.done_sentinel or status == "completed")


def _parse_chat_document(document: dict[str, Any] | None, *, event_count: int = 0, invalid_event_count: int = 0, terminal: bool | None = None) -> Turn:
    errors: list[str] = []
    choice = document.get("choices", [{}])[0] if isinstance(document, dict) and isinstance(document.get("choices"), list) and document.get("choices") else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    calls: list[ToolCall] = []
    for raw in message.get("tool_calls", []) if isinstance(message, dict) else []:
        if not isinstance(raw, dict):
            errors.append("tool_call_invalid")
            continue
        function = raw.get("function", {})
        arguments, error = _parse_arguments(function.get("arguments"))
        if error:
            errors.append(error)
        calls.append(ToolCall(str(raw.get("id", "")), str(function.get("name", "")), arguments))
    content = message.get("content", "") if isinstance(message, dict) else ""
    reasoning_content = message.get("reasoning_content", "") if isinstance(message, dict) else ""
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    continuation = copy.deepcopy(message) if isinstance(message, dict) else {}
    return Turn("chat", calls, str(content or ""), bool(reasoning_content), finish_reason, continuation, terminal if terminal is not None else finish_reason is not None, event_count, invalid_event_count, errors)


def _parse_chat_stream(result: TransportResult) -> Turn:
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    for item in result.events:
        event = item.get("event", {})
        choices = event.get("choices", [])
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta", {})
        if delta.get("content"):
            content.append(str(delta["content"]))
        if delta.get("reasoning_content"):
            reasoning.append(str(delta["reasoning_content"]))
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        for raw in delta.get("tool_calls", []) if isinstance(delta, dict) else []:
            index = int(raw.get("index", 0))
            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if raw.get("id"):
                call["id"] = raw["id"]
            function = raw.get("function", {})
            if function.get("name"):
                call["function"]["name"] += str(function["name"])
            if function.get("arguments"):
                call["function"]["arguments"] += str(function["arguments"])
    message = {"role": "assistant", "content": "".join(content), "tool_calls": [calls[index] for index in sorted(calls)]}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    return _parse_chat_document({"choices": [{"message": message, "finish_reason": finish_reason}]}, event_count=len(result.events), invalid_event_count=result.invalid_event_count, terminal=result.done_sentinel)


def _parse_turn(protocol: str, result: TransportResult, *, stream: bool) -> Turn:
    if protocol == "anthropic":
        return _parse_anthropic_stream(result) if stream else _parse_anthropic_document(result.document, invalid_event_count=result.invalid_event_count)
    if protocol == "responses":
        return _parse_responses_stream(result) if stream else _parse_responses_document(result.document, invalid_event_count=result.invalid_event_count)
    return _parse_chat_stream(result) if stream else _parse_chat_document(result.document, invalid_event_count=result.invalid_event_count)


def _validate_first(turn: Turn, expected_tools: tuple[str, ...], *, stream: bool) -> dict[str, Any]:
    ids = [call.call_id for call in turn.tool_calls]
    names = [call.name for call in turn.tool_calls]
    args_ok = all(call.arguments == {"city": "Shanghai"} for call in turn.tool_calls)
    checks = {
        "parsed": not turn.parse_errors and turn.invalid_event_count == 0,
        "terminal": turn.terminal,
        "expected_tool_count": len(turn.tool_calls) == len(expected_tools),
        "tool_names": sorted(names) == sorted(expected_tools),
        "tool_ids_unique": bool(ids) and len(ids) == len(set(ids)) and all(ids),
        "tool_arguments": args_ok,
        "reasoning_present": turn.reasoning_present,
        "stream_complete": (not stream) or (turn.event_count > 0 and turn.terminal),
    }
    return {"checks": checks, "tool_call_count": len(turn.tool_calls), "tool_names": names, "reasoning_present": turn.reasoning_present, "event_count": turn.event_count}


def _validate_second(turn: Turn, expected_tools: tuple[str, ...], *, stream: bool) -> dict[str, Any]:
    required_markers = [SYSTEM_MARKER, *(TOOL_OUTPUTS[name] for name in expected_tools)]
    checks = {
        "parsed": not turn.parse_errors and turn.invalid_event_count == 0,
        "terminal": turn.terminal,
        "no_followup_tool_calls": not turn.tool_calls,
        "system_marker": SYSTEM_MARKER in turn.text,
        "tool_outputs": all(marker in turn.text for marker in required_markers[1:]),
        "stream_complete": (not stream) or (turn.event_count > 0 and turn.terminal),
    }
    return {"checks": checks, "tool_call_count": len(turn.tool_calls), "text_length": len(turn.text), "reasoning_present": turn.reasoning_present, "event_count": turn.event_count}


def _build_payload(spec: CaseSpec, model: str, *, followup: Turn | None = None) -> dict[str, Any]:
    if spec.client_protocol == "anthropic":
        return _anthropic_payload(model=model, stream=spec.stream, parallel=spec.parallel, followup=followup)
    if spec.client_protocol == "responses":
        return _responses_payload(model=model, stream=spec.stream, parallel=spec.parallel, followup=followup)
    return _chat_payload(model=model, stream=spec.stream, parallel=spec.parallel, followup=followup)


def _run_case(spec: CaseSpec) -> dict[str, Any]:
    model = MODELS[spec.target_protocol]
    first_result = _request(spec.path, _build_payload(spec, model), stream=spec.stream)
    first_turn = _parse_turn(spec.client_protocol, first_result, stream=spec.stream)
    first = _validate_first(first_turn, spec.expected_tools, stream=spec.stream)
    first["checks"]["http_success"] = 200 <= first_result.status < 300
    second: dict[str, Any] = {"skipped": True, "checks": {"not_run": False}}
    if first_turn.tool_calls:
        second_result = _request(spec.path, _build_payload(spec, model, followup=first_turn), stream=spec.stream)
        second_turn = _parse_turn(spec.client_protocol, second_result, stream=spec.stream)
        second = _validate_second(second_turn, spec.expected_tools, stream=spec.stream)
        second["checks"]["http_success"] = 200 <= second_result.status < 300
        second["status"] = second_result.status
    checks = dict(first["checks"])
    checks.update({f"second_{key}": value for key, value in second.get("checks", {}).items() if key != "not_run"})
    return {
        "case": spec.name,
        "status": first_result.status,
        "second_status": second.get("status"),
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
    parsed = urllib.parse.urlsplit(BASE_URL)
    loopback = (
        parsed.scheme == "http"
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
    ok = all(all(result["checks"].values()) for result in results)
    print(json.dumps({"ok": ok, "base_url": BASE_URL, "results": results}, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
