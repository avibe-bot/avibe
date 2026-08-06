#!/usr/bin/env python3
"""Run the CPA M0 agentic-fidelity workload without logging credentials/bodies."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any


REQUIRED_VENDOR_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
BASE_URL = os.environ.get("CPA_BASE_URL", "http://127.0.0.1:15220").rstrip("/")
GATEWAY_TOKEN = os.environ.get("CPA_GATEWAY_TOKEN", "")
MODELS = {
    "responses": os.environ.get("CPA_OPENAI_RESPONSES_MODEL", ""),
    "chat": os.environ.get("CPA_OPENAI_CHAT_MODEL", ""),
    "anthropic": os.environ.get("CPA_ANTHROPIC_MODEL", ""),
}


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "lookup_weather",
            "description": "Return a short weather summary for a city.",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        {
            "name": "lookup_time",
            "description": "Return the local time for a city.",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    ]


def _anthropic_payload(
    *, model: str, stream: bool, round_trip: bool = False, parallel: bool = True
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Call both tools for Shanghai, then summarize."
                if parallel
                else "Call lookup_weather for Shanghai, then summarize."
            ),
        }
    ]
    if round_trip:
        messages = [
            {"role": "user", "content": "Call lookup_weather for Shanghai."},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_fixture_weather",
                        "name": "lookup_weather",
                        "input": {"city": "Shanghai"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_fixture_weather",
                        "content": "18 C and clear",
                    }
                ],
            },
        ]
    return {
        "model": model,
        "max_tokens": 96,
        "system": "You are a concise tool-using assistant. Keep the final answer under 20 words.",
        "messages": messages,
        "tools": _tool_definitions(),
        "thinking": {"type": "enabled", "budget_tokens": 64},
        "stream": stream,
    }


def _responses_payload(*, model: str, stream: bool, round_trip: bool = False) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
        {"role": "user", "content": "Call both tools for Shanghai, then summarize."}
    ]
    if round_trip:
        items = [
            {"role": "user", "content": "Call lookup_weather for Shanghai."},
            {
                "type": "function_call",
                "call_id": "call_fixture_weather",
                "name": "lookup_weather",
                "arguments": '{"city":"Shanghai"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_fixture_weather",
                "output": "18 C and clear",
            },
        ]
    return {
        "model": model,
        "input": items,
        "instructions": "You are a concise tool-using assistant. Keep the final answer under 20 words.",
        "tools": [
            {"type": "function", "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}
            for tool in _tool_definitions()
        ],
        "reasoning": {"effort": "low"},
        "stream": stream,
    }


def _chat_payload(*, model: str, stream: bool, round_trip: bool = False) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a concise tool-using assistant."},
        {"role": "user", "content": "Call both tools for Shanghai, then summarize."},
    ]
    if round_trip:
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_fixture_weather",
                            "type": "function",
                            "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_fixture_weather", "content": "18 C and clear"},
            ]
        )
    return {
        "model": model,
        "messages": messages,
        "tools": [
            {"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}
            for tool in _tool_definitions()
        ],
        "max_tokens": 96,
        "reasoning_effort": "low",
        "stream": stream,
    }


def _request(path: str, payload: dict[str, Any], *, stream: bool) -> tuple[int, list[dict[str, Any]]]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if GATEWAY_TOKEN:
        headers["Authorization"] = f"Bearer {GATEWAY_TOKEN}"
    request = urllib.request.Request(f"{BASE_URL}{path}", body, headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            if not stream:
                json.loads(response.read())
                return status, [{"kind": "json"}]
            events = []
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    events.append({"kind": "done"})
                    continue
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    events.append({"kind": "invalid_json"})
                else:
                    events.append({"kind": "event", "type": parsed.get("type")})
            return status, events
    except urllib.error.HTTPError as error:
        return error.code, []
    except (OSError, TimeoutError):
        return 0, []


def _case(name: str, path: str, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    status, events = _request(path, payload, stream=stream)
    event_types = [item.get("type") for item in events if item.get("kind") == "event"]
    terminal_types = {"message_stop", "response.completed", "response.done", "done"}
    checks = {
        "http_success": 200 <= status < 300,
        "stream_has_data": (not stream) or bool(event_types),
        "stream_has_done": (not stream)
        or any(item.get("kind") == "done" or item.get("type") in terminal_types for item in events),
        "stream_json_only": all(item.get("kind") != "invalid_json" for item in events),
    }
    return {"case": name, "status": status, "event_type_count": len(event_types), "checks": checks}


def _build_cases() -> Iterable[dict[str, Any]]:
    yield {"name": "messages_to_responses_single_tool", "path": "/v1/messages", "payload": _anthropic_payload(model=MODELS["responses"], stream=False, parallel=False), "stream": False}
    yield {"name": "responses_to_messages_tool_round_trip", "path": "/v1/responses", "payload": _responses_payload(model=MODELS["anthropic"], stream=False, round_trip=True), "stream": False}
    yield {"name": "messages_to_responses_parallel_tools_stream", "path": "/v1/messages", "payload": _anthropic_payload(model=MODELS["responses"], stream=True), "stream": True}
    yield {"name": "responses_to_messages_tools_stream", "path": "/v1/responses", "payload": _responses_payload(model=MODELS["anthropic"], stream=True), "stream": True}
    yield {"name": "messages_to_chat_parallel_tools_stream", "path": "/v1/messages", "payload": _anthropic_payload(model=MODELS["chat"], stream=True), "stream": True}
    yield {"name": "chat_to_messages_tool_round_trip", "path": "/v1/chat/completions", "payload": _chat_payload(model=MODELS["anthropic"], stream=False, round_trip=True), "stream": False}
    yield {"name": "chat_to_messages_tools_stream", "path": "/v1/chat/completions", "payload": _chat_payload(model=MODELS["anthropic"], stream=True), "stream": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list case names without making requests")
    args = parser.parse_args()
    if args.list:
        print("\n".join(case["name"] for case in _build_cases()))
        return 0
    missing = [name for name in REQUIRED_VENDOR_KEYS if not os.environ.get(name)]
    missing_models = [name for name, value in MODELS.items() if not value]
    parsed_base = urllib.parse.urlparse(BASE_URL)
    loopback = parsed_base.scheme == "http" and parsed_base.hostname in {"127.0.0.1", "localhost", "::1"}
    if missing or missing_models or not loopback:
        labels = [*missing, *[f"model:{name}" for name in missing_models]]
        if not loopback:
            labels.append("CPA_BASE_URL must be an http loopback URL")
        print(json.dumps({"ok": False, "blocked": True, "missing": labels}, sort_keys=True))
        return 2
    results = []
    for case in _build_cases():
        result = _case(case["name"], case["path"], case["payload"], stream=case["stream"])
        results.append(result)
    ok = all(all(result["checks"].values()) for result in results)
    print(json.dumps({"ok": ok, "base_url": BASE_URL, "results": results}, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
