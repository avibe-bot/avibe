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
    # Explicit pipe capture also works when the installed interpreter has no
    # console. It inherits the REAL tool environment; no caller ID is invented.
    arguments = f'-I -B -m vibe computer js --code "@{script}" --timeout-ms 60000'
    command = "\n".join([
        "$p = New-Object System.Diagnostics.ProcessStartInfo",
        f"$p.FileName = {ps_quote(python)}",
        f"$p.Arguments = {ps_quote(arguments)}",
        "$p.UseShellExecute = $false; $p.CreateNoWindow = $true",
        "$p.RedirectStandardOutput = $true; $p.RedirectStandardError = $true",
        "$child = [System.Diagnostics.Process]::Start($p)",
        "$null = $child.SafeHandle",
        "try {",
        "  $out = $child.StandardOutput.ReadToEndAsync(); $err = $child.StandardError.ReadToEndAsync()",
        "  if (-not $child.WaitForExit(75000)) { throw 'fixture_cli_timeout' }",
        "  $text = $out.GetAwaiter().GetResult(); $errorText = $err.GetAwaiter().GetResult()",
        f"  [IO.File]::WriteAllText({ps_quote(str(output))}, $text, (New-Object Text.UTF8Encoding $false))",
        "  [Console]::Out.Write($text); [Console]::Error.Write($errorText)",
        "  if ($child.ExitCode -ne 0) { throw ('fixture_cli_exit_' + $child.ExitCode) }",
        "} finally { if (-not $child.HasExited) { $child.Kill(); $null = $child.WaitForExit(5000) }; $child.Dispose() }",
    ])
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
                receipt = {"received": True, "finish": finish, "shell_tool_called": bool(delta.get("tool_calls"))}
                tool_messages = [row for row in request.get("messages", []) if row.get("role") == "tool"]
                if tool_messages:
                    raw_content = tool_messages[-1].get("content", "")
                    if isinstance(raw_content, list):
                        raw_content = "\n".join(str(item.get("text", "")) for item in raw_content if isinstance(item, dict))
                    content = str(raw_content).lower()
                    # Preserve the actual shipped CLI result for the external
                    # observer, not a verdict invented by the model fixture.
                    matching_case = next((case for case in CASES if tool_messages[-1].get("tool_call_id") == "call_cu_" + case.replace("-", "_")), None)
                    try:
                        cli_result = json.loads(str(raw_content))
                    except ValueError:
                        cli_result = None
                    if matching_case and isinstance(cli_result, dict) and cli_result.get("kind") == "computer_js":
                        (work / f"{matching_case}.json").write_text(json.dumps(cli_result), encoding="utf-8")
                    receipt["exit_codes"] = re.findall(r"command exited with code (-?\d+)", content)
                    receipt["powershell_error_ids"] = re.findall(r"fullyqualifiederrorid\s*:\s*([a-z0-9_,.]+)", content)
                    receipt["cli_error_codes"] = re.findall(r'"code"\s*:\s*"([a-z_]+)"', content)
                    receipt["tool_result_classes"] = [label for text, label in (
                        ("working directory does not exist", "cwd_missing"),
                        ("working directory is not accessible", "cwd_inaccessible"),
                        ("cannot run in this turn", "turn_disallows_commands"),
                        ("could not start", "command_start_failed"),
                        ("access is denied", "access_denied"),
                        ("permission denied", "permission_denied"),
                        ("not recognized", "command_not_found"),
                        ("active_turn_required", "active_turn_required"),
                        ("missing_caller_session", "missing_caller_session"),
                        ("command exited with code", "command_nonzero"),
                        ("command timed out", "command_timeout"),
                        ("command aborted", "command_aborted"),
                        ("cannot find path", "path_missing"),
                        ("cannot bind", "powershell_binding_failed"),
                        ("no output", "no_output"),
                        ("unknown tool", "unknown_tool"),
                        ("unexpected token", "shell_parse_error"),
                        ("terminator", "shell_quote_error"),
                        ("modulenotfounderror", "python_module_missing"),
                        ("filenotfounderror", "python_file_missing"),
                        ("command cannot be run due to", "native_command_start_failed"),
                        ("erroractionpreference", "powershell_action_preference"),
                    ) if text in content]
                    receipt["tool_result_characters"] = len(content)
                (work / "stub-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
                envelope = {"id": "chatcmpl-cu-fixture", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL}
                frames = []
                for payload, reason in ((delta, None), ({}, finish)):
                    frame = {**envelope, "choices": [{"index": 0, "delta": payload, "finish_reason": reason}]}
                    frames.append("data: " + json.dumps(frame) + "\n\n")
                frames.append("data: [DONE]\n\n")
                self.respond(200, "text/event-stream", "".join(frames).encode())
            except (ValueError, TypeError, KeyError) as error:
                reason = str(error) if str(error) in {"fixture_marker_required", "shipped_windows_powershell_tool_required", "invalid_fixture_request"} else "malformed_fixture_request"
                (work / "stub-receipt.json").write_text(json.dumps({"received": True, "refused": reason}), encoding="utf-8")
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
