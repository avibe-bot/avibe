"""Native MCP configs are the Hub's connection store, and Avibe sessions never load them.

Contract (docs/plans/mcp-hub-mvp.md §1, acceptance 6): Hub lists the union of
Claude ``.claude.json``, Codex ``config.toml`` and OpenCode ``opencode.json``
servers, writes add/save/remove back to the canonical file without disturbing
anything else, and every spawn path disables the user's native servers.
Runs under the hermetic homes ``tests/conftest.py`` provides.
"""

import asyncio
import json
import tomllib

import pytest

from core.mcp_hub import native, repository
from core.mcp_hub.runtime import McpRuntime

CLAUDE = {"numStartups": 7, "mcpServers": {
    "docs": {"type": "http", "url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer c"}},
    "shared": {"type": "stdio", "command": "claude-wins", "args": []},
}}
CODEX = """model = "gpt-test"

[mcp_servers.local]
command = "srv"
args = ["--read-only"]
startup_timeout_sec = 30

[mcp_servers.local.env]
MODE = "ro"

[mcp_servers.shared]
command = "codex-loses"
"""
OPENCODE = {"$schema": "https://opencode.ai/config.json", "mcp": {
    "oc": {"type": "local", "command": ["node", "oc.js"], "environment": {"K": "v"}, "enabled": True},
}}


@pytest.fixture
def native_files():
    claude, codex, opencode = native.claude_path(), native.codex_path(), native.opencode_path()
    for path in (claude, codex, opencode):
        path.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(json.dumps(CLAUDE))
    codex.write_text(CODEX)
    opencode.write_text(json.dumps(OPENCODE))
    return claude, codex, opencode


@pytest.fixture
def runtime():
    owner = McpRuntime()
    yield owner
    owner.stop()


def _run(runtime, operation, payload):
    return json.loads(asyncio.run(runtime.submit(operation, payload))[1])


def test_hub_lists_the_union_with_the_canonical_source(native_files):
    summaries = {item["name"]: item for item in repository.list_packages()}
    assert {name: item["source"] for name, item in summaries.items()} == {
        "docs": "claude", "shared": "claude", "local": "codex", "oc": "opencode"}
    assert repository.read_definition("shared")["transport"]["command"] == "claude-wins"
    assert repository.read_definition("local")["transport"] == {
        "type": "stdio", "command": "srv", "args": ["--read-only"], "env": {"MODE": "ro"}}
    assert repository.read_definition("oc")["transport"] == {
        "type": "stdio", "command": "node", "args": ["oc.js"], "env": {"K": "v"}}
    assert all(item["status"] == "draft" for item in summaries.values())
    assert not repository.mcp_root().exists()  # nothing copied into Avibe home


def test_save_and_remove_write_the_native_files_in_place(native_files, runtime):
    claude, codex, opencode = native_files
    new = {"name": "fresh", "server": {"url": "https://fresh.invalid/mcp"}, "description": "Fresh"}
    with_cwd = {"name": "rooted", "server": {"command": "srv", "cwd": "/srv"}}
    assert _run(runtime, "save", {"servers": [new, with_cwd]})["ok"]
    replace = [
        {"name": "local", "server": {"command": "srv2", "env": {"MODE": ""}}},  # blank keeps the secret
        {"name": "oc", "server": {"type": "http", "url": "https://oc.invalid/mcp"}},
    ]
    assert _run(runtime, "save", {"servers": replace, "replace": True, "preserveSecrets": True})["ok"]

    claude_data = json.loads(claude.read_text())
    assert claude_data["numStartups"] == 7
    assert claude_data["mcpServers"]["fresh"] == {"type": "http", "url": "https://fresh.invalid/mcp"}
    codex_data = tomllib.loads(codex.read_text())
    assert codex_data["model"] == "gpt-test"
    assert codex_data["mcp_servers"]["rooted"] == {"command": "srv", "args": [], "cwd": "/srv"}
    assert codex_data["mcp_servers"]["local"] == {
        "startup_timeout_sec": 30, "command": "srv2", "args": [], "env": {"MODE": "ro"}}
    opencode_data = json.loads(opencode.read_text())
    assert opencode_data["mcp"]["oc"] == {"enabled": True, "type": "remote", "url": "https://oc.invalid/mcp"}
    assert repository.read_definition("fresh")["description"] == "Fresh"

    assert _run(runtime, "remove", {"name": "shared"})["ok"]
    assert "shared" not in json.loads(claude.read_text())["mcpServers"]
    assert "shared" not in tomllib.loads(codex.read_text())["mcp_servers"]


def test_unparseable_native_file_is_skipped_and_never_rewritten(native_files, runtime):
    _claude, codex, _opencode = native_files
    codex.write_text("[mcp_servers.local\nbroken")
    assert "local" not in repository.list_names() and "docs" in repository.list_names()
    result = _run(runtime, "save", {"servers": [{"name": "rooted", "server": {"command": "x", "cwd": "/srv"}}]})
    assert result["error"]["type"] == "invalid_native_config"
    assert codex.read_text() == "[mcp_servers.local\nbroken"


def test_underscore_names_and_natively_disabled_relative_cwd(native_files):
    _claude, codex, _opencode = native_files
    codex.write_text(
        CODEX
        + """
[mcp_servers.iphone_use]
command = "python3"
args = ["iphone_use.py"]

[mcp_servers.computer-use]
command = "./Codex Computer Use.app/bin"
args = ["mcp"]
cwd = "."
enabled = false
"""
    )
    summaries = {item["name"]: item for item in repository.list_packages()}
    assert summaries["iphone_use"]["status"] == "draft"
    assert "error" not in summaries["iphone_use"]
    # Native enabled=false is informational; the relative cwd anchors at the file's directory.
    assert summaries["computer-use"]["nativeEnabled"] is False
    assert summaries["computer-use"]["status"] == "draft" and "error" not in summaries["computer-use"]
    assert repository.read_definition("computer-use")["transport"]["cwd"] == str(codex.parent)


def test_natively_disabled_entry_generates_like_any_draft(native_files, runtime):
    import sys
    from pathlib import Path

    _claude, codex, _opencode = native_files
    fixture = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
    codex.write_text(CODEX + f'\n[mcp_servers.off]\ncommand = {json.dumps(sys.executable)}\nargs = [{json.dumps(fixture)}]\nenabled = false\n')
    before = codex.read_bytes()
    draft = {item["name"]: item for item in repository.list_packages()}["off"]
    assert draft["status"] == "draft" and draft["nativeEnabled"] is False and "error" not in draft
    assert _run(runtime, "generate", {"name": "off"})["ok"]
    call = _run(runtime, "call", {"name": "off", "tool": "echo", "arguments": {"text": "hi"}})
    assert call["status"] == "ok", call
    assert codex.read_bytes() == before  # generate and call never rewrite the native file


def test_relative_native_cwd_resolves_against_its_file_and_generates(native_files, runtime):
    import shutil
    import sys
    from pathlib import Path

    claude, codex, opencode = native_files
    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    for path in native_files:
        shutil.copy(fixture, path.parent / "rel_echo.py")  # found only if cwd is the file's dir
    data = json.loads(claude.read_text())
    data["mcpServers"]["rel-claude"] = {"command": sys.executable, "args": ["rel_echo.py"], "cwd": "."}
    claude.write_text(json.dumps(data))
    codex.write_text(CODEX + f'\n[mcp_servers.rel-codex]\ncommand = {json.dumps(sys.executable)}\nargs = ["rel_echo.py"]\ncwd = "."\n')
    data = json.loads(opencode.read_text())
    data["mcp"]["rel-opencode"] = {"type": "local", "command": [sys.executable, "rel_echo.py"], "cwd": "."}
    opencode.write_text(json.dumps(data))

    for name, path in (("rel-claude", claude), ("rel-codex", codex), ("rel-opencode", opencode)):
        assert repository.read_definition(name)["transport"]["cwd"] == str(path.parent)
        result = _run(runtime, "generate", {"name": name})
        assert result["ok"] and result["toolCount"] == 6, (name, result)

    # A replace save keeps each file's own relative spelling (Claude/OpenCode included).
    servers = [{"name": name, "server": {"command": sys.executable, "args": ["rel_echo.py", "-v"], "cwd": str(path.parent)}}
               for name, path in (("rel-claude", claude), ("rel-codex", codex), ("rel-opencode", opencode))]
    assert _run(runtime, "save", {"servers": servers, "replace": True})["ok"]
    assert json.loads(claude.read_text())["mcpServers"]["rel-claude"]["cwd"] == "."
    assert tomllib.loads(codex.read_text())["mcp_servers"]["rel-codex"]["cwd"] == "."
    assert json.loads(opencode.read_text())["mcp"]["rel-opencode"]["cwd"] == "."


def test_generated_package_survives_native_delete(native_files):
    from core.mcp_hub.commands import build_command

    claude, _codex, _opencode = native_files
    definition = {
        "schemaVersion": 1,
        "name": "docs",
        "description": "Search",
        "transport": {"type": "streamable_http", "url": "https://example.invalid/mcp", "headers": {}},
    }
    tool = {"name": "search", "description": "", "inputSchema": {"type": "object", "properties": {}}}
    repository.publish(
        "docs",
        definition=definition,
        manifest={"description": "Search", "generatedAt": "2026-01-01T00:00:00+00:00"},
        tools=[tool],
        commands=[build_command(tool)],
    )
    repository.write_enabled("docs", True)
    repository.remember_source("docs", "claude")
    data = json.loads(claude.read_text())
    del data["mcpServers"]["docs"]
    claude.write_text(json.dumps(data))
    summary = {item["name"]: item for item in repository.list_packages()}["docs"]
    assert summary["nativeDeleted"] is True
    assert summary["source"] == "claude"
    assert summary["status"] == "generated" and summary["enabled"] is True
    assert repository.read_published("docs")["definition"]["name"] == "docs"
    data["mcpServers"]["docs"] = {"type": "http", "url": "https://example.invalid/other"}
    claude.write_text(json.dumps(data))
    restored = {item["name"]: item for item in repository.list_packages()}["docs"]
    assert restored.get("nativeDeleted") is not True
    assert restored["pendingChanges"] is True


def test_native_entries_needing_env_expansion_are_marked_not_dropped(native_files):
    claude, _codex, _opencode = native_files
    data = json.loads(claude.read_text())
    data["mcpServers"]["templated"] = {"command": "srv", "env": {"TOKEN": "${TOKEN}"}}
    claude.write_text(json.dumps(data))
    summary = {item["name"]: item for item in repository.list_packages()}["templated"]
    assert summary["status"] == "error" and summary["error"]["type"] == "invalid_definition"


def test_spawns_disable_every_native_server(native_files):
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from modules.agents.codex.transport import _native_mcp_disabled_args
    from vibe.claude_config import CLAUDE_NATIVE_MCP_DISABLED
    from vibe.opencode_config import managed_opencode_runtime_config_content

    command = SubprocessCLITransport(prompt="x", options=ClaudeAgentOptions(cli_path="/bin/claude", **CLAUDE_NATIVE_MCP_DISABLED))._build_command()
    assert "--strict-mcp-config" in command and "--mcp-config" not in command
    from config.v2_compat import ClaudeCompatConfig
    from modules.claude_client import ClaudeClient

    client = ClaudeClient(ClaudeCompatConfig(enabled=True, permission_mode="default", cwd="/tmp"))
    assert client.options.strict_mcp_config and client.options.mcp_servers == {}

    assert _native_mcp_disabled_args() == [
        "-c", "mcp_servers.local.enabled=false", "-c", "mcp_servers.shared.enabled=false"]

    overlay = json.loads(managed_opencode_runtime_config_content(None, native.opencode_disabled_mcp()))
    assert overlay["mcp"] == {"oc": {**OPENCODE["mcp"]["oc"], "enabled": False}}

    only_in_overlay = json.loads(managed_opencode_runtime_config_content(
        json.dumps({"mcp": {"extra": {"type": "local", "command": ["node"], "enabled": True}}}),
        native.opencode_disabled_mcp(),
    ))
    assert only_in_overlay["mcp"]["extra"]["enabled"] is False
    assert only_in_overlay["mcp"]["oc"]["enabled"] is False
