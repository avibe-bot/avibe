"""Contracts for the draft-only deterministic model fixture, not product smoke.

The credible regression is dispatching a nonexistent Windows tool, dispatching
unrequested text, or executing the same fixture command again after a tool
result. Existing provider-adapter tests cannot cover this HTTP producer.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "signed_cu_model_stub", ROOT / "desktop/scripts/signed-cu-acceptance/model_stub.py"
)
assert SPEC and SPEC.loader
stub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stub)


def request(marker="launch-off", tool="powershell"):
    return {
        "messages": [{"role": "user", "content": f"AVIBE_CU_CASE:{marker}"}],
        "tools": [{"type": "function", "function": {"name": tool}}],
    }


def test_windows_tool_and_real_cli_command_are_bound_to_fixture_marker(tmp_path):
    reply, finish = stub.scripted_reply(request(), python="C:/installed/python.exe", work=tmp_path)
    call = reply["tool_calls"][0]
    assert finish == "tool_calls"
    assert call["function"]["name"] == "powershell"
    args = json.loads(call["function"]["arguments"])
    assert "-I -B -m vibe computer js" in args["command"]
    assert "launch-off.js" in args["command"]
    assert "AVIBE_SESSION_ID" not in args["command"]
    assert args["watch"] is False
    assert args["timeout"] == 90


@pytest.mark.parametrize("payload", [request(tool="bash"), request(marker="delete-everything"), {"messages": [], "tools": []}])
def test_unrequested_or_non_windows_tool_call_is_refused(payload, tmp_path):
    with pytest.raises(ValueError):
        stub.scripted_reply(payload, python="python.exe", work=tmp_path)


def test_actual_tool_result_finishes_without_reexecuting_or_claiming_acceptance(tmp_path):
    payload = request()
    payload["messages"].append({"role": "tool", "tool_call_id": "call_cu_launch_off", "content": "failed"})
    reply, finish = stub.scripted_reply(payload, python="python.exe", work=tmp_path)
    assert finish == "stop"
    assert "tool_calls" not in reply
    assert "external observer" in reply["content"]
    assert "PASS" not in reply["content"]


def test_actual_loopback_wire_has_tool_call_finish_and_done(tmp_path):
    server = stub.ThreadingHTTPServer(("127.0.0.1", 0), stub.handler("python.exe", tmp_path))
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        with urlopen(Request(url, data=json.dumps(request()).encode(), headers={"Content-Type": "application/json"}), timeout=5) as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            frames = response.read().decode().split("\n\n")
        events = [json.loads(frame.removeprefix("data: ")) for frame in frames if frame.startswith("data: {")]
        assert events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "powershell"
        assert events[1]["choices"][0]["finish_reason"] == "tool_calls"
        assert "data: [DONE]" in frames
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
