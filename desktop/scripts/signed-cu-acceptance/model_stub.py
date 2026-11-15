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
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "cu-acceptance"
CASES = frozenset({"probe-context", "launch-off", "launch-quit", "launch-crash", "attach-elevated"})


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def fixture_case(request: dict) -> str:
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
    return marker


def scripted_reply(request: dict, *, python: str, work: Path) -> tuple[dict, str]:
    """Select a fixture command from genuine conversation/tool history."""
    messages = request.get("messages", [])
    marker = fixture_case(request)
    call_id = "call_cu_" + marker.replace("-", "_")
    if any(item.get("role") == "tool" and item.get("tool_call_id") == call_id for item in messages):
        return {"content": "Deterministic fixture tool returned. The external observer determines the verdict."}, "stop"
    functions = [item.get("function", {}).get("name") for item in request.get("tools", []) if item.get("type") == "function"]
    if "powershell" not in functions:
        raise ValueError("shipped_windows_powershell_tool_required")
    launch_case = marker.startswith("launch-")
    operation_id = None
    if marker != "probe-context":
        context = json.loads((work / f"{marker}-context.json").read_text(encoding="utf-8-sig"))
        operation_id = context["operation_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("invalid_operation_id")
    script = work / f"{marker}.js"
    output = work / f"{marker}.json"
    # Explicit pipe capture also works when the installed interpreter has no
    # console. It inherits the REAL tool environment; no caller ID is invented.
    if launch_case:
        wrapper = work / f"{marker}.ps1"
        type_script = work / f"{marker}-type.js"
        budget_ms = context.get("operation_budget_ms", 110000)
        if not isinstance(budget_ms, int) or not 1 <= budget_ms <= 110000:
            raise ValueError("invalid_operation_budget")
        arguments = subprocess.list2cmdline([
            "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            str(wrapper), "-Python", python, "-LaunchScript", str(script), "-TypeScript", str(type_script),
            "-OperationId", operation_id, "-ReceiptPath", str(work / f"input-{operation_id}.json"),
            "-BudgetMs", str(budget_ms),
        ])
        file_line = "$p.FileName = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'"
    else:
        arguments = f'-I -B -m vibe computer js --code "@{script}" --timeout-ms 60000'
        file_line = f"$p.FileName = {ps_quote(python)}"
    command = "\n".join([
        "$p = New-Object System.Diagnostics.ProcessStartInfo",
        file_line,
        f"$p.Arguments = {ps_quote(arguments)}",
        "$p.UseShellExecute = $false; $p.CreateNoWindow = $true",
        "$p.RedirectStandardOutput = $true; $p.RedirectStandardError = $true",
        "$child = [System.Diagnostics.Process]::Start($p)",
        "$null = $child.SafeHandle",
        "try {",
        "  $out = $child.StandardOutput.ReadToEndAsync(); $err = $child.StandardError.ReadToEndAsync()",
        "  if (-not $child.WaitForExit(75000)) { throw 'fixture_cli_timeout' }",
        "  $text = $out.GetAwaiter().GetResult(); $errorText = $err.GetAwaiter().GetResult()",
        "  $resultText = if ($text.Length) { $text } else { $errorText }",
        f"  [IO.File]::WriteAllText({ps_quote(str(output))}, $resultText, (New-Object Text.UTF8Encoding $false))",
        "  [Console]::Out.Write($text); [Console]::Error.Write($errorText)",
        "  if ($child.ExitCode -ne 0) { throw ('fixture_cli_exit_' + $child.ExitCode) }",
        "} finally { if (-not $child.HasExited) { $child.Kill(); $null = $child.WaitForExit(5000) }; $child.Dispose() }",
    ])
    if operation_id:
        # One writer for the operation result. The dispatch receipt is distinct
        # from the wrapper receipt, so even startup/parse failures are retained.
        command = "\n".join([
            "$ErrorActionPreference = 'Stop'", "Set-StrictMode -Version Latest",
            f"$operationId = {ps_quote(operation_id)}",
            f"$resultPath = {ps_quote(str(output))}",
            f"$receiptPath = {ps_quote(str(work / f'dispatch-{operation_id}.json'))}",
            "$receipt = [ordered]@{operation_id=$operationId; phase='dispatch_start'; exit_code=$null; first_error=$null; cleanup_errors=@()}",
            "$child=$null; $envelope=$null",
            "function Save-Dispatch { $receipt | ConvertTo-Json -Depth 10 -Compress | Set-Content -LiteralPath ($receiptPath + '.tmp') -Encoding utf8; Move-Item -LiteralPath ($receiptPath + '.tmp') -Destination $receiptPath -Force }",
            "try {",
            "  Save-Dispatch",
            "  $p = New-Object System.Diagnostics.ProcessStartInfo", file_line,
            f"  $p.Arguments = {ps_quote(arguments)}",
            "  $p.UseShellExecute=$false; $p.CreateNoWindow=$true; $p.RedirectStandardOutput=$true; $p.RedirectStandardError=$true",
            "  $child=[Diagnostics.Process]::Start($p); $null=$child.SafeHandle",
            "  $out=$child.StandardOutput.ReadToEndAsync(); $err=$child.StandardError.ReadToEndAsync()",
            "  $receipt.phase='dispatch_wait'; Save-Dispatch",
            "  if (-not $child.WaitForExit(135000)) { throw 'input_dispatch_timeout' }",
            "  $receipt.exit_code=$child.ExitCode; $receipt.phase='dispatch_drain'; Save-Dispatch",
            "  if (-not $out.Wait(5000) -or -not $err.Wait(5000)) { throw 'input_dispatch_drain_timeout' }",
            "  if ($child.ExitCode -ne 0) { throw 'input_dispatch_nonzero' }",
            "  $receipt.phase='dispatch_parse'; Save-Dispatch",
            "  $envelope=$out.GetAwaiter().GetResult() | ConvertFrom-Json",
            "  if ($envelope.kind -ne 'computer_js') { throw 'input_dispatch_envelope_invalid' }",
            ("  if ($envelope.operation_id -ne $operationId) { throw 'input_dispatch_identity_mismatch' }" if launch_case else "  $envelope | Add-Member -NotePropertyName operation_id -NotePropertyValue $operationId"),
            "  $receipt.phase='dispatch_complete'",
            "} catch {",
            "  $receipt.first_error=@{call=$receipt.phase; exit_code=$receipt.exit_code; error_class=$_.Exception.GetType().FullName; code=if ($_.Exception.Message -match '^input_[a-z_]+$') {$_.Exception.Message} else {'input_dispatch_exception'}}",
            "  $envelope=[ordered]@{ok=$false; kind='computer_js'; operation_id=$operationId; result=$null}",
            "} finally {",
            "  if ($child) { try { if (-not $child.HasExited) {$child.Kill(); $null=$child.WaitForExit(5000)}; $child.Dispose() } catch {$receipt.cleanup_errors+=@{call='dispatch_cleanup'; error_class=$_.Exception.GetType().FullName; exit_code=$null}} }",
            "  if ($receipt.cleanup_errors.Count -gt 0) { $envelope.ok=$false }",
            "  Save-Dispatch",
            "  $encoded=$envelope | ConvertTo-Json -Depth 20 -Compress",
            "  [IO.File]::WriteAllText(($resultPath + '.tmp'), $encoded, (New-Object Text.UTF8Encoding $false))",
            "  Move-Item -LiteralPath ($resultPath + '.tmp') -Destination $resultPath -Force",
            "  [Console]::Out.Write($encoded)",
            "}",
        ])
    return {
        "role": "assistant",
        "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
            "name": "powershell", "arguments": json.dumps({"command": command, "timeout": 150 if operation_id else 90, "watch": False}),
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
                marker = fixture_case(request)
                context_path = work / f"{marker}-context.json"
                operation_id = None
                if context_path.is_file():
                    operation_id = json.loads(context_path.read_text(encoding="utf-8-sig")).get("operation_id")
                    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                        raise ValueError("invalid_operation_id")
                receipt = {"case": marker, "operation_id": operation_id, "received": True, "finish": finish, "shell_tool_called": bool(delta.get("tool_calls"))}
                tool_messages = [row for row in request.get("messages", []) if row.get("role") == "tool"]
                if tool_messages:
                    raw_content = tool_messages[-1].get("content", "")
                    if isinstance(raw_content, list):
                        raw_content = "\n".join(str(item.get("text", "")) for item in raw_content if isinstance(item, dict))
                    content = str(raw_content).lower()
                    # Case results have one writer: the actual generated tool
                    # command. Never rewrite them from provider/tool history.
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
                receipt_path = work / (f"stub-{operation_id}.json" if operation_id else "stub-receipt.json")
                temporary = receipt_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(receipt), encoding="utf-8")
                temporary.replace(receipt_path)
                envelope = {"id": "chatcmpl-cu-fixture", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL}
                frames = []
                for payload, reason in ((delta, None), ({}, finish)):
                    frame = {**envelope, "choices": [{"index": 0, "delta": payload, "finish_reason": reason}]}
                    frames.append("data: " + json.dumps(frame) + "\n\n")
                frames.append("data: [DONE]\n\n")
                self.respond(200, "text/event-stream", "".join(frames).encode())
            except (ValueError, TypeError, KeyError) as error:
                reason = str(error) if str(error) in {"fixture_marker_required", "shipped_windows_powershell_tool_required", "invalid_fixture_request"} else "malformed_fixture_request"
                (work / "stub-refused.json").write_text(json.dumps({"received": True, "refused": reason}), encoding="utf-8")
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
