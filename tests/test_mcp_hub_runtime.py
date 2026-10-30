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
from tests.ui_server_test_helpers import save_config


FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")


def _run(runtime: McpRuntime, operation: str, payload: dict) -> dict:
    status, body = asyncio.run(runtime.submit(operation, payload))
    assert status == 200
    return json.loads(body)


def _server(command: str = sys.executable) -> dict:
    return {"name": "echo", "server": {"command": command, "args": [FIXTURE]}, "description": "Echo tools"}


@pytest.fixture
def runtime(tmp_path):
    config = save_config(tmp_path)
    config.runtime.default_cwd = str(tmp_path)
    config.save()
    owner = McpRuntime()
    yield owner
    owner.stop()


@pytest.fixture
def generated(runtime):
    assert _run(runtime, "add", {"servers": [_server()]})["ok"]
    return runtime


def test_describe_walks_cause_chain_for_http_status():
    class Response:
        status_code = 400
        reason = "Bad Request"
        text = '{"message":"Bad credentials"}'

    class HttpError(Exception):
        def __init__(self):
            super().__init__("boom")
            self.response = Response()

    outer = Exception("Server returned an error response")
    outer.__cause__ = HttpError()
    assert mcp_runtime._describe(outer).startswith("HTTP 400:")
    assert "Bad Request" in mcp_runtime._describe(outer)


def test_add_writes_avibe_store_and_registers(runtime):
    first = _run(runtime, "add", {"servers": [_server()]})
    assert first["ok"] and first["toolCount"] == 7
    assert first["added"][0]["name"] == "echo"
    assert repository.read_avibe_definition("echo") is not None
    assert first.get("failed") == []


def test_batch_add_registers_neighbors_and_rolls_back_the_failure(runtime):
    """One failing server in the middle must not block or leave an invisible definition."""

    good = _server()
    bad = {"name": "github", "server": {"command": "/nonexistent/mcp-github"}}
    other = {"name": "zeta", "server": {"command": sys.executable, "args": [FIXTURE]}, "description": "Z"}
    result = _run(runtime, "add", {"servers": [good, bad, other]})
    names = {item["name"] for item in result["added"]}
    failed = {item["name"]: item["error"] for item in result["failed"]}
    assert result["ok"] is True
    assert names == {"echo", "zeta"}
    assert "github" in failed and failed["github"]
    assert repository.read_avibe_definition("github") is None
    assert "github" not in {item["name"] for item in repository.list_packages()}
    assert repository.read_published("echo") is not None
    assert repository.read_published("zeta") is not None
    published = repository.read_published("echo")
    assert [tool["name"] for tool in published["tools"]] == ["add", "crash", "echo", "fail", "nap", "reject", "session_text"]
    assert repository.summarize("echo")["source"] == "avibe"


def test_refresh_keeps_one_published_snapshot(generated):
    revisions = [_run(generated, "refresh", {"name": "echo"})["revision"] for _ in range(3)]
    kept = sorted(path.name for path in (repository.package_dir("echo") / "revisions").iterdir())
    assert kept == [revisions[-1]]


def test_failed_replace_add_restores_previous_avibe_definition(generated):
    before = repository.read_published("echo")["revision"]
    previous = repository.read_avibe_definition("echo")
    result = _run(generated, "add", {"servers": [_server("/nonexistent/mcp-server")], "replace": True})
    assert result["ok"] is False
    assert result["failed"][0]["name"] == "echo"
    assert repository.read_published("echo")["revision"] == before
    assert repository.read_avibe_definition("echo") == previous


def test_malformed_existing_definition_does_not_abort_batch_add(runtime):
    path = repository.package_dir("broken") / "definition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    result = _run(runtime, "add", {"servers": [{"name": "broken", "server": {"command": "/nonexistent/x"}}, _server()]})
    assert result["ok"] is True
    assert {item["name"] for item in result["added"]} == {"echo"}
    assert any(item["name"] == "broken" for item in result["failed"])


def test_failed_readd_of_hidden_native_stays_hidden(runtime):
    from core.mcp_hub import native

    claude = native.claude_path()
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(json.dumps({"mcpServers": {
        "echo": {"type": "stdio", "command": sys.executable, "args": [FIXTURE]},
    }}))
    assert _run(runtime, "discover", {})["ok"]
    assert _run(runtime, "remove", {"name": "echo"})["ok"]
    assert "echo" in repository.hidden_names()
    result = _run(runtime, "add", {"servers": [{"name": "echo", "server": {"command": "/nonexistent/mcp-echo"}}]})
    assert result["ok"] is False
    assert "echo" in repository.hidden_names()
    assert repository.read_published("echo") is None


def test_failed_explicit_refresh_unlists_the_server(generated):
    repository.write_avibe_definition({
        "name": "echo",
        "description": "Echo tools",
        "transport": {"type": "stdio", "command": "/nonexistent/mcp-echo", "args": [FIXTURE]},
    })
    result = _run(generated, "refresh", {"name": "echo", "waitStdio": True})
    assert result["ok"] is False
    assert "echo" not in {item["name"] for item in repository.list_packages()}


def test_failed_discover_unlists_previous_revision(generated):
    repository.write_avibe_definition({
        "name": "echo",
        "description": "Echo tools",
        "transport": {"type": "stdio", "command": "/nonexistent/mcp-echo", "args": [FIXTURE]},
    })
    result = _run(generated, "discover", {})
    by_name = {item["name"]: item for item in result["results"]}
    assert by_name["echo"]["status"] == "failed"
    assert "echo" not in {item["name"] for item in repository.list_packages()}
    assert (repository.package_dir("echo") / "revisions").exists()


def test_readding_hidden_native_identical_definition_registers_again(runtime):
    from core.mcp_hub import native

    claude = native.claude_path()
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(json.dumps({"mcpServers": {
        "echo": {"type": "stdio", "command": sys.executable, "args": [FIXTURE]},
    }}))
    assert _run(runtime, "discover", {})["ok"]
    assert _run(runtime, "remove", {"name": "echo"})["ok"]
    assert "echo" in repository.hidden_names()
    assert repository.read_published("echo") is None
    result = _run(runtime, "add", {"servers": [_server()]})
    assert result["ok"] is True
    assert result["added"][0]["name"] == "echo"
    assert "echo" not in repository.hidden_names()
    assert repository.read_published("echo") is not None


def test_native_definition_change_is_not_unchanged_until_published(runtime):
    from core.mcp_hub import native

    claude = native.claude_path()
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(json.dumps({"mcpServers": {
        "echo": {"type": "stdio", "command": sys.executable, "args": [FIXTURE]},
    }}))
    assert _run(runtime, "discover", {})["ok"]
    claude.write_text(json.dumps({"mcpServers": {
        "echo": {"type": "stdio", "command": sys.executable, "args": [FIXTURE, "--other"]},
    }}))
    pasted = {
        "name": "echo",
        "server": {"command": sys.executable, "args": [FIXTURE, "--other"]},
        "description": "Echo tools",
    }
    blocked = _run(runtime, "add", {"servers": [pasted]})
    assert blocked["ok"] is False
    assert blocked["failed"][0]["type"] == "exists"
    replaced = _run(runtime, "add", {"servers": [pasted], "replace": True})
    assert replaced["ok"] is True
    assert replaced["added"][0]["name"] == "echo"
    published = repository.read_published("echo")["definition"]
    assert published["transport"]["args"] == [FIXTURE, "--other"]


def test_identical_native_readd_without_cwd_is_unchanged(runtime):
    from core.mcp_hub import native

    claude = native.claude_path()
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(json.dumps({"mcpServers": {
        "echo": {"type": "stdio", "command": sys.executable, "args": [FIXTURE]},
    }}))
    assert _run(runtime, "discover", {})["ok"]
    result = _run(runtime, "add", {"servers": [{
        "name": "echo",
        "server": {"command": sys.executable, "args": [FIXTURE]},
    }]})
    assert result["ok"] is True
    assert result["unchanged"][0]["name"] == "echo"


def test_explicit_cwd_difference_is_exists_not_unchanged(generated):
    same_command = {
        "name": "echo",
        "server": {"command": sys.executable, "args": [FIXTURE], "cwd": "/tmp/mcp-other-cwd"},
        "description": "Echo tools",
    }
    result = _run(generated, "add", {"servers": [same_command]})
    assert result["ok"] is False
    assert result["failed"][0]["type"] == "exists"
    assert repository.read_avibe_definition("echo")["transport"].get("cwd") != "/tmp/mcp-other-cwd"


def test_discover_keeps_neighbors_when_one_native_refresh_fails(runtime):
    from core.mcp_hub import native

    claude = native.claude_path()
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(json.dumps({"mcpServers": {
        "echo": {"type": "stdio", "command": sys.executable, "args": [FIXTURE]},
        "dead": {"type": "stdio", "command": "/nonexistent/mcp-dead"},
    }}))
    result = _run(runtime, "discover", {})
    by_name = {item["name"]: item for item in result["results"]}
    assert result["ok"] is True
    assert by_name["echo"]["status"] == "registered"
    assert by_name["dead"]["status"] == "failed"


def test_avibe_owned_server_is_listed_even_if_codex_disables_the_same_name(generated):
    from core.mcp_hub import native

    path = native.codex_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[mcp_servers.echo]\ncommand = "codex-echo"\nenabled = false\n')
    names = {item["name"] for item in repository.list_packages()}
    assert "echo" in names
    assert repository.summarize("echo")["source"] == "avibe"


def test_calls_return_envelopes(generated):
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


def test_malformed_avibe_definition_is_omitted_from_the_list(generated):
    path = repository.package_dir("broken") / "definition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    names = {item["name"] for item in repository.list_packages()}
    assert "echo" in names
    assert "broken" not in names


def test_future_format_revision_is_not_listed(generated):
    published = repository.read_published("echo")
    manifest = repository.package_dir("echo") / "revisions" / published["revision"] / "manifest.json"
    manifest.write_text(json.dumps({"formatVersion": 99}))
    assert "echo" not in {item["name"] for item in repository.list_packages()}
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


def test_cli_treats_help_as_a_server_name(generated, capsys):
    from vibe import mcp_cli

    added = _run(generated, "add", {"servers": [{**_server(), "name": "help"}]})
    assert added["ok"] is True
    generated.stop()
    assert mcp_cli.main(["help", "--list"]) == 0
    assert "echo  Echo text back" in capsys.readouterr().out
    assert mcp_cli.main(["-h"]) == 0
    assert "usage:" in capsys.readouterr().out


def test_mcp_add_and_discover_are_listed_and_print_usage(capsys):
    from vibe import cli, mcp_cli

    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "mcp-add" in help_text and "mcp-discover" in help_text
    assert mcp_cli.add_main(["--help"]) == 0
    assert "vibe mcp-add" in capsys.readouterr().out
    assert mcp_cli.discover_main(["--help"]) == 0
    assert "vibe mcp-discover" in capsys.readouterr().out


def test_mcp_add_does_not_treat_json_stdin_after_separator_as_avibe_option(monkeypatch):
    from vibe import mcp_cli

    consumed = []

    class FakeStdin:
        def read(self):
            consumed.append(True)
            return "{}"

    captured = []

    def fake_owner(operation, payload):
        captured.append((operation, payload))
        return 0

    monkeypatch.setattr(sys, "stdin", FakeStdin())
    monkeypatch.setattr(mcp_cli, "_owner", fake_owner)
    monkeypatch.setattr(mcp_cli, "_admit", lambda _floor: None)
    assert mcp_cli.add_main(["local", "--", "server", "--json-stdin"]) == 0
    assert consumed == []
    servers = captured[0][1]["servers"]
    assert servers[0]["server"]["command"] == "server"
    assert servers[0]["server"]["args"] == ["--json-stdin"]


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



@pytest.mark.parametrize(
    ("server_info", "line"),
    [
        ({"description": "Search\n  the\tdocs", "title": "Docs"}, "- docs: Search the docs"),
        ({"title": "Docs " + "x" * 200}, "- docs: Docs " + "x" * 155),
        ({}, "- docs: Tools from an MCP server."),
    ],
)
def test_prompt_line_uses_server_description_then_title_then_fallback(runtime, server_info, line):
    from types import SimpleNamespace

    definition = {"name": "docs", "description": "", "transport": {"type": "streamable_http", "url": "https://example.invalid/mcp"}}
    repository.write_avibe_definition(definition)
    McpRuntime._publish("docs", definition, definition, [], SimpleNamespace(version=None, **server_info))
    assert line in build_mcp_prompt_block().text.splitlines()


@pytest.mark.parametrize(("argv", "allowed"), [(["--list"], True), (["echo", "--refresh"], False)])
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
        _run(runtime, "add", {"servers": [{"name": "web", "server": {
            "type": "http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {"X-Test": "v"}}}]})
        assert _run(runtime, "refresh", {"name": "web"})["toolCount"] == 7
        for text in ("one", "two"):
            result = _run(runtime, "call", {"name": "web", "tool": "echo", "arguments": {"text": text},
                                            "sessionId": "ses-http"})
            assert result["result"]["content"] == [{"type": "text", "text": text}]
        assert _run(runtime, "call", {"name": "web", "tool": "reject", "arguments": {}})["error"]["type"] == "tool_error"
        session_text = _run(runtime, "call", {"name": "web", "tool": "session_text", "arguments": {}})
        assert session_text["error"]["type"] == "tool_error"
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_malformed_revision_file_disables_only_that_package(generated, capsys):
    from vibe import mcp_cli

    _run(generated, "add", {"servers": [{**_server(), "name": "other"}]})
    revision = repository.read_published("echo")["revision"]
    path = repository.package_dir("echo") / "revisions" / revision / "tools.json"
    path.write_text("{not json")
    names = {item["name"] for item in repository.list_packages()}
    assert "echo" not in names
    assert "other" in names
    assert mcp_cli.main(["--list"]) == 0
    assert mcp_cli.main(["echo", "--list"]) == 1
    call = _run(generated, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x"}})
    assert call["error"]["type"] == "invalid_package"


def test_strict_legacy_server_that_exits_on_discover_still_generates(runtime):
    strict = str(Path(__file__).parent / "fixtures" / "mcp_strict_legacy_server.py")
    result = _run(runtime, "add", {"servers": [{"name": "strict", "server": {"command": sys.executable, "args": [strict]}}]})
    assert result["ok"], result
    assert result["toolCount"] == 7
    call = _run(runtime, "call", {"name": "strict", "tool": "echo", "arguments": {"text": "hi"}})
    assert call["status"] == "ok", call
