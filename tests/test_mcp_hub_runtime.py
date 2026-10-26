"""MCP Hub owner path against a real loopback stdio server (plan §6, §7, acceptance 2-5).

Runs the real SDK, runtime thread, repository and CLI under the per-test hermetic
``AVIBE_HOME`` that ``tests/conftest.py`` provides.
"""

import asyncio
import json
import re
import shlex
import sys
from pathlib import Path

import pytest

from core.mcp_hub import repository
from core.mcp_hub import runtime as mcp_runtime
from core.mcp_hub.prompt import build_mcp_prompt_block
from core.mcp_hub.runtime import McpRuntime

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")


def _run(runtime: McpRuntime, operation: str, payload: dict) -> dict:
    status, body = asyncio.run(runtime.submit(operation, payload))
    assert status == 200
    return json.loads(body)


def _server(command: str = sys.executable) -> dict:
    return {"name": "echo", "server": {"command": command, "args": [FIXTURE]}, "description": "Echo tools"}


@pytest.fixture
def runtime():
    owner = McpRuntime()
    yield owner
    owner.stop()


@pytest.fixture
def generated(runtime):
    assert _run(runtime, "save", {"servers": [_server()]})["ok"]
    assert _run(runtime, "generate", {"name": "echo"})["ok"]
    return runtime


def test_save_only_saves_and_initial_generate_publishes_then_enables(runtime):
    _run(runtime, "save", {"servers": [_server()]})
    assert repository.summarize("echo")["status"] == "draft"
    assert repository.read_enabled("echo") is False

    first = _run(runtime, "generate", {"name": "echo"})
    published = repository.read_published("echo")
    assert published["revision"] == first["revision"]
    assert [tool["name"] for tool in published["tools"]] == ["add", "crash", "echo", "fail", "nap", "reject"]
    assert published["definition"]["transport"]["cwd"]  # frozen at generation
    assert published["manifest"]["serverInfo"] == {"version": "0.1.0"}
    assert repository.read_enabled("echo") is True


def test_regenerate_preserves_state_and_keeps_current_plus_previous(generated):
    _run(generated, "disable", {"name": "echo"})
    revisions = [_run(generated, "generate", {"name": "echo"})["revision"] for _ in range(3)]
    kept = sorted(path.name for path in (repository.package_dir("echo") / "revisions").iterdir())
    assert kept == sorted(revisions[-2:])
    assert repository.read_enabled("echo") is False


def test_failed_generation_leaves_previous_revision_active(generated):
    before = repository.read_published("echo")["revision"]
    _run(generated, "save", {"servers": [_server("/nonexistent/mcp-server")], "replace": True})
    result = _run(generated, "generate", {"name": "echo"})
    assert result["ok"] is False and result["error"]["type"] == "connection"
    assert repository.read_published("echo")["revision"] == before
    assert repository.summarize("echo")["pendingChanges"] is True


def test_calls_return_envelopes_and_respect_enabled_state(generated):
    ok = _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "hi", "repeat": 2},
                                  "sessionId": "ses-1"})
    assert ok["status"] == "ok" and ok["revision"] == repository.read_published("echo")["revision"]
    assert ok["result"]["content"] == [{"type": "text", "text": "hihi"}]
    assert ok["result"]["structuredContent"] == {"result": "hihi"} and ok["result"]["isError"] is False

    failed = _run(generated, "call", {"name": "echo", "tool": "fail", "arguments": {"reason": "x"}})
    assert failed["status"] == "error" and failed["error"]["type"] == "tool_error"
    assert failed["result"]["isError"] is True

    invalid = _run(generated, "call", {"name": "echo", "tool": "add", "arguments": {"a": "x", "b": 1}})
    assert invalid["error"]["type"] == "invalid_arguments"

    _run(generated, "disable", {"name": "echo"})
    assert _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x"}})["error"]["type"] == "disabled"
    _run(generated, "enable", {"name": "echo"})
    assert _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x"}})["status"] == "ok"


def test_concurrent_first_calls_in_one_session_share_the_open(generated):
    async def both():
        first = generated.submit("call", {"name": "echo", "tool": "echo", "arguments": {"text": "a"}, "sessionId": "ses-race"})
        second = generated.submit("call", {"name": "echo", "tool": "echo", "arguments": {"text": "b"}, "sessionId": "ses-race"})
        return await asyncio.gather(first, second)

    bodies = [json.loads(body) for _status, body in asyncio.run(both())]
    assert all(item["status"] == "ok" for item in bodies), bodies


def test_stdio_process_cap_reports_capacity(generated, monkeypatch):
    monkeypatch.setattr(mcp_runtime, "STDIO_PROCESS_CAP", 0)
    result = _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x"}})
    assert result["error"]["type"] == "capacity"


def test_oversized_result_is_omitted_but_reported_complete(generated, monkeypatch):
    monkeypatch.setattr(mcp_runtime, "RESULT_BYTE_CAP", 64)
    result = _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x" * 100}})
    assert result["status"] == "ok" and result["resultOmitted"] == "too_large"
    assert result["result"] == {"isError": False}


def test_reader_rereads_current_when_its_revision_was_pruned(generated, monkeypatch):
    live = repository.read_published("echo")["revision"]
    reads = iter(["gone-revision", live])
    monkeypatch.setattr(repository, "_current_revision", lambda name: next(reads))
    assert repository.read_published("echo")["revision"] == live


def test_bad_package_file_disables_only_that_package(generated):
    _run(generated, "save", {"servers": [{**_server(), "name": "other"}]})
    state = repository.package_dir("echo") / "state.json"
    state.write_text("{not json")
    summaries = {item["name"]: item for item in repository.list_packages()}
    assert summaries["echo"]["status"] == "error" and summaries["echo"]["enabled"] is False
    assert summaries["other"]["status"] == "draft"
    assert state.read_text() == "{not json"


def test_future_format_revision_needs_regeneration(generated):
    published = repository.read_published("echo")
    manifest = repository.package_dir("echo") / "revisions" / published["revision"] / "manifest.json"
    manifest.write_text(json.dumps({"formatVersion": 99}))
    assert repository.summarize("echo")["status"] == "needs_regeneration"
    assert build_mcp_prompt_block() is None


def test_cli_list_and_help_work_with_the_owner_down(generated, capsys):
    from vibe import mcp_cli

    generated.stop()
    assert mcp_cli.main(["--list"]) == 0
    assert "echo  Echo tools" in capsys.readouterr().out
    assert mcp_cli.main(["echo", "--list"]) == 0
    assert "echo  Echo text back" in capsys.readouterr().out
    assert mcp_cli.main(["echo", "echo", "--help"]) == 0
    assert "--text <string> (required)" in capsys.readouterr().out
    assert mcp_cli.main(["echo", "echo", "--text", "x"]) == 1
    assert "not running" in capsys.readouterr().err
    assert mcp_cli.main(["echo", "echo", "--bogus", "x"]) == 2


def test_prompt_index_lists_enabled_servers_and_its_examples_parse(generated, capsys, monkeypatch):
    from vibe import mcp_cli

    text = build_mcp_prompt_block().text
    assert "- echo: Echo tools" in text
    examples = re.findall(r"`(vibe mcp[^`]*)`", text)
    assert examples
    sent = []
    monkeypatch.setattr(mcp_cli, "_call", lambda name, tool, arguments: sent.append((name, tool)) or 0)
    for example in examples:
        argv = shlex.split(example.replace("<server>", "echo").replace("<tool>", "echo")
                           .replace("[arguments]", "--text x"))[2:]
        assert mcp_cli.main(argv) == 0, example
    assert sent == [("echo", "echo")]

    _run(generated, "disable", {"name": "echo"})
    assert build_mcp_prompt_block() is None


@pytest.mark.parametrize(("argv", "allowed"), [(["--list"], True), (["add", "x", "--", "srv"], False)])
def test_editor_discovers_but_cannot_manage(generated, argv, allowed, capsys):
    from vibe import mcp_cli
    from vibe.authorization import AuthorizationContext, invocation_authority

    editor = AuthorizationContext(instance_role="editor", is_remote=True, instance_kind="organization")
    with invocation_authority(editor):
        code = mcp_cli.main(argv)
    assert (code == 0) is allowed
    if not allowed:
        assert "owner" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("tool", "arguments", "error_type"),
    [
        ("reject", {}, "tool_error"),  # a JSON-RPC error the server returned
        ("crash", {}, "unknown_outcome"),  # transport died after tools/call was sent
        ("nap", {"seconds": 5}, "unknown_outcome"),  # call timeout
    ],
)
def test_call_failure_classification_and_reconnect(generated, monkeypatch, tool, arguments, error_type):
    monkeypatch.setattr(mcp_runtime, "CALL_TIMEOUT_SECONDS", 1)
    payload = {"name": "echo", "tool": tool, "arguments": arguments, "sessionId": "ses-1"}
    assert _run(generated, "call", payload)["error"]["type"] == error_type
    # An unknown-outcome connection is closed, so the session's next call reconnects.
    again = _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x"}, "sessionId": "ses-1"})
    assert again["status"] == "ok"


def test_streamable_http_generate_and_call(runtime):
    import socket
    import subprocess
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen([sys.executable, FIXTURE, "http", str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as attempt:
                if attempt.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        _run(runtime, "save", {"servers": [{"name": "web", "server": {
            "type": "http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {"X-Test": "v"}}}]})
        assert _run(runtime, "generate", {"name": "web"})["toolCount"] == 6
        for text in ("one", "two"):
            result = _run(runtime, "call", {"name": "web", "tool": "echo", "arguments": {"text": text},
                                            "sessionId": "ses-http"})
            assert result["result"]["content"] == [{"type": "text", "text": text}]
        assert _run(runtime, "call", {"name": "web", "tool": "reject", "arguments": {}})["error"]["type"] == "tool_error"
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("tools.json", {"not": "a list"}),
        ("commands.json", [{"name": "add"}]),
        *[("commands.json", field) for field in ("description", "jsonOnlyKeys", "requiredJsonOnly")],
        ("definition.json", {"transport": {"type": "stdio"}}),
    ],
)
def test_malformed_revision_file_disables_only_that_package(generated, capsys, filename, content):
    from vibe import mcp_cli

    _run(generated, "save", {"servers": [{**_server(), "name": "other"}]})
    revision = repository.read_published("echo")["revision"]
    path = repository.package_dir("echo") / "revisions" / revision / filename
    if isinstance(content, str):  # drop one field from every published command
        commands = json.loads(path.read_text())
        for command in commands:
            del command[content]
        content = commands
    path.write_text(json.dumps(content))
    summaries = {item["name"]: item for item in repository.list_packages()}
    assert summaries["echo"]["status"] == "error" and summaries["echo"]["error"]["type"] == "invalid_package"
    assert summaries["other"]["status"] == "draft"
    assert mcp_cli.main(["--list", "--all"]) == 0
    assert mcp_cli.main(["echo", "--list"]) == 1
    call = _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x"}})
    assert call["error"]["type"] == "invalid_package"


def test_strict_legacy_server_that_exits_on_discover_still_generates(runtime):
    strict = str(Path(__file__).parent / "fixtures" / "mcp_strict_legacy_server.py")
    _run(runtime, "save", {"servers": [{"name": "strict", "server": {"command": sys.executable, "args": [strict]}}]})
    result = _run(runtime, "generate", {"name": "strict"})
    assert result["ok"], result
    assert result["toolCount"] == 6
    call = _run(runtime, "call", {"name": "strict", "tool": "echo", "arguments": {"text": "hi"}})
    assert call["status"] == "ok", call
