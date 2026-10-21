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

    assert _native_mcp_disabled_args() == [
        "-c", "mcp_servers.local.enabled=false", "-c", "mcp_servers.shared.enabled=false"]

    overlay = json.loads(managed_opencode_runtime_config_content(None, native.opencode_disabled_mcp()))
    assert overlay["mcp"] == {"oc": {**OPENCODE["mcp"]["oc"], "enabled": False}}
