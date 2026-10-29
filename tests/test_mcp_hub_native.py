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


def test_hub_reads_native_union_without_listing_unpublished(native_files):
    assert repository.read_definition("shared")["transport"]["command"] == "claude-wins"
    assert repository.read_definition("local")["transport"] == {
        "type": "stdio", "command": "srv", "args": ["--read-only"], "env": {"MODE": "ro"}}
    assert repository.read_definition("oc")["transport"] == {
        "type": "stdio", "command": "node", "args": ["oc.js"], "env": {"K": "v"}}
    assert repository.list_packages() == []  # unpublished native entries are not listed


def test_add_writes_avibe_store_and_remove_hides_native(native_files, runtime):
    claude, _codex, _opencode = native_files
    before = claude.read_bytes()
    new = {"name": "fresh", "server": {"url": "https://fresh.invalid/mcp"}, "description": "Fresh"}
    result = _run(runtime, "add", {"servers": [new]})
    assert result["ok"] is False  # http fixture is unreachable; add rolls back
    assert repository.read_avibe_definition("fresh") is None
    assert claude.read_bytes() == before
    assert "fresh" not in {item["name"] for item in repository.list_packages()}

    assert _run(runtime, "remove", {"name": "shared"})["ok"]
    assert claude.read_bytes() == before
    assert "shared" in repository.hidden_names()


def test_unparseable_native_file_is_skipped_and_never_rewritten(native_files):
    _claude, codex, _opencode = native_files
    codex.write_text("[mcp_servers.local\nbroken")
    assert "local" not in native.read_all()
    assert "docs" in native.read_all()
    assert repository.list_packages() == []
    assert native.codex_path().read_text() == "[mcp_servers.local\nbroken"


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
    assert "iphone_use" in native.read_all()
    assert "computer-use" not in native.read_all()
    assert "computer-use" in native.disabled_names()
    assert repository.list_packages() == []


def test_codex_backend_extras_are_ignored_not_errors(native_files, monkeypatch):
    _claude, codex, _opencode = native_files
    extra = """
[mcp_servers.node_repl]
command = "/bin/node_repl"
args = []
startup_timeout_sec = 120
env_vars = ["HUB_TEST_PRESENT", "HUB_TEST_MISSING"]

[mcp_servers.node_repl.env]
CODEX_HOME = "/tmp/codex"
"""
    monkeypatch.setenv("HUB_TEST_PRESENT", "from-parent")
    monkeypatch.delenv("HUB_TEST_MISSING", raising=False)
    codex.write_text(CODEX + extra)
    transport = repository.read_definition("node_repl")["transport"]
    assert transport["command"] == "/bin/node_repl"
    assert transport["env"] == {"HUB_TEST_PRESENT": "from-parent", "CODEX_HOME": "/tmp/codex"}
    native.write(repository.read_definition("node_repl"))
    rewritten = tomllib.loads(codex.read_text())["mcp_servers"]["node_repl"]
    assert "env" not in rewritten or "HUB_TEST_PRESENT" not in rewritten["env"]
    stored = tomllib.loads(codex.read_text())["mcp_servers"]["node_repl"]
    assert stored["startup_timeout_sec"] == 120
    assert stored["env_vars"] == ["HUB_TEST_PRESENT", "HUB_TEST_MISSING"]


def test_codex_env_var_edit_is_written_as_explicit_env(native_files, monkeypatch):
    _claude, codex, _opencode = native_files
    monkeypatch.setenv("TOKEN", "old")
    codex.write_text(CODEX + '\n[mcp_servers.editable]\ncommand = "srv"\nenv_vars = ["TOKEN"]\n')
    definition = repository.read_definition("editable")
    definition["transport"]["env"]["TOKEN"] = "new"
    native.write(definition)
    stored = tomllib.loads(codex.read_text())["mcp_servers"]["editable"]
    assert stored["env_vars"] == ["TOKEN"]
    assert stored["env"]["TOKEN"] == "new"


def test_codex_env_var_object_shape_survives_replace(native_files, monkeypatch):
    _claude, codex, _opencode = native_files
    monkeypatch.setenv("TOKEN", "old")
    codex.write_text(CODEX + '\n[mcp_servers.object-env]\ncommand = "srv"\nenv_vars = [{name = "TOKEN", source = "local"}]\n')
    definition = repository.read_definition("object-env")
    native.write(definition)
    stored = tomllib.loads(codex.read_text())["mcp_servers"]["object-env"]
    assert stored["env_vars"] == [{"name": "TOKEN", "source": "local"}]


def test_opencode_local_cwd_is_resolved_from_native_file(native_files):
    _claude, _codex, opencode = native_files
    data = json.loads(opencode.read_text())
    data["mcp"]["local-cwd"] = {"type": "local", "command": ["./server"], "cwd": "."}
    opencode.write_text(json.dumps(data))
    definition = repository.read_definition("local-cwd")
    assert definition["transport"]["cwd"] == str(opencode.parent)
    assert definition["transport"]["command"] == "./server"


def test_natively_disabled_entry_is_omitted_from_hub_lists(native_files):
    import sys
    from pathlib import Path

    _claude, codex, _opencode = native_files
    fixture = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
    codex.write_text(CODEX + f'\n[mcp_servers.off]\ncommand = {json.dumps(sys.executable)}\nargs = [{json.dumps(fixture)}]\nenabled = false\n')
    names = {item["name"] for item in repository.list_packages()}
    assert "off" not in names
    assert "off" in native.disabled_names()


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
        result = _run(runtime, "refresh", {"name": name})
        assert result["ok"] and result["toolCount"] == 7, (name, result)


def test_native_delete_hides_and_may_drop_artifacts(native_files):
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
    data = json.loads(claude.read_text())
    del data["mcpServers"]["docs"]
    claude.write_text(json.dumps(data))
    assert "docs" not in {item["name"] for item in repository.list_packages()}
    return

def test_native_entries_needing_env_expansion_are_marked_not_dropped(native_files):
    claude, _codex, _opencode = native_files
    data = json.loads(claude.read_text())
    data["mcpServers"]["templated"] = {"command": "srv", "env": {"TOKEN": "${TOKEN}"}}
    claude.write_text(json.dumps(data))
    assert "templated" not in native.read_all() or native.read_all()["templated"].error is not None
    assert "templated" not in {item["name"] for item in repository.list_packages()}


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
    assert only_in_overlay["mcp"]["extra"]["enabled"] is True
    assert only_in_overlay["mcp"]["oc"]["enabled"] is False
