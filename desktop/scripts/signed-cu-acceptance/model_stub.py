"""Loopback deterministic upstream for a REAL shipped Vibey tool Turn, not an LLM.

Only fixed fixture markers dispatch a shell command; tool names come from the
actual provider request. No credentials, request bodies, tool results or model
prompts are logged or retained. The external observer, never this stub, judges
acceptance. Run only inside the disposable standard-user fixture.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "cu-acceptance"
CASES = frozenset({"probe-context", "launch-off", "launch-quit", "launch-crash", "attach-elevated"})


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def scripted_reply(request: dict, *, python: str, work: Path) -> tuple[dict, str]:
    """Select a fixture command from genuine conversation/tool history."""
    messages = request.get("messages", [])
    marker = None
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        found = re.findall(r"AVIBE_CU_CASE:([a-z-]+)", str(content))
        if found:
            marker = found[-1]
            break
    if marker not in CASES:
        raise ValueError("fixture_marker_required")
    call_id = "call_cu_" + marker.replace("-", "_")
    if any(item.get("role") == "tool" and item.get("tool_call_id") == call_id for item in messages):
        return {"content": "Deterministic fixture tool returned. The external observer determines the verdict."}, "stop"
    functions = [item.get("function", {}).get("name") for item in request.get("tools", []) if item.get("type") == "function"]
    if "powershell" not in functions:
        raise ValueError("shipped_windows_powershell_tool_required")
    script = work / f"{marker}.js"
    output = work / f"{marker}.json"
    command = (
        f"& {ps_quote(python)} -I -B -m vibe computer js "
        f"--code {ps_quote('@' + str(script))} --timeout-ms 60000 "
        f"| Set-Content -LiteralPath {ps_quote(str(output))} -Encoding utf8; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
    )
    return {
        "role": "assistant",
        "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
            "name": "powershell", "arguments": json.dumps({"command": command, "timeout": 90, "watch": False}),
        }}],
    }, "tool_calls"


def handler(python: str, work: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status: int, content_type: str, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/v1/models":
                self.respond(404, "application/json", b'{"error":"unknown_fixture_route"}')
                return
            body = {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "acceptance"}]}
            self.respond(200, "application/json", json.dumps(body).encode())

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1/chat/completions" or not 0 < length <= 8 * 1024 * 1024:
                    raise ValueError("invalid_fixture_request")
                request = json.loads(self.rfile.read(length))
                delta, finish = scripted_reply(request, python=python, work=work)
                envelope = {"id": "chatcmpl-cu-fixture", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL}
                frames = []
                for payload, reason in ((delta, None), ({}, finish)):
                    frame = {**envelope, "choices": [{"index": 0, "delta": payload, "finish_reason": reason}]}
                    frames.append("data: " + json.dumps(frame) + "\n\n")
                frames.append("data: [DONE]\n\n")
                self.respond(200, "text/event-stream", "".join(frames).encode())
            except (ValueError, TypeError, KeyError):
                self.respond(400, "application/json", b'{"error":"invalid_fixture_request"}')
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--python", required=True)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(args.python, args.work))
    server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
