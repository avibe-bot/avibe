"""Native MCP configs are the Hub's connection store, and Avibe sessions never load them.

Contract (docs/plans/mcp-hub-mvp.md §1, acceptance 6): Hub lists the union of
Claude ``.claude.json``, Codex ``config.toml`` and OpenCode ``opencode.json``
servers, deletes a native entry from its own file only, stores Avibe-added servers under the Hub,
and every spawn path disables the user's native servers.
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


def test_add_writes_avibe_store_without_touching_native(native_files, runtime):
    claude, _codex, _opencode = native_files
    before = claude.read_bytes()
    new = {"name": "fresh", "server": {"command": "/nonexistent/mcp-fresh"}}
    result = _run(runtime, "add", {"servers": [new]})
    assert result["ok"] is False  # discovery failed, but the owned definition remains manageable
    assert repository.read_avibe_definition("fresh") is not None
    assert repository.summarize("fresh")["state"] == "not_generated"
    assert claude.read_bytes() == before
    assert "fresh" in {item["name"] for item in repository.list_packages()}


CODEX_COMMENTED = """# my codex config
model = "gpt-test"  # pinned

[mcp_servers.keep]
command = "keep"   # stays

# the one to delete
[mcp_servers.gone]
command = "gone"

[mcp_servers.gone.env]
TOKEN = "x"

[profiles.fast]
model = "fast"
"""


def test_remove_deletes_codex_table_and_subtables_preserving_text(native_files, runtime):
    _claude, codex, _opencode = native_files
    codex.write_text(CODEX_COMMENTED)
    codex.chmod(0o640)
    repository.package_dir("gone").mkdir(parents=True)

    result = _run(runtime, "remove", {"name": "gone"})

    assert result["ok"] and result["kind"] == "native"
    assert codex.read_text() == CODEX_COMMENTED.replace(
        '[mcp_servers.gone]\ncommand = "gone"\n\n[mcp_servers.gone.env]\nTOKEN = "x"\n\n', ""
    )
    assert codex.stat().st_mode & 0o777 == 0o640
    assert not repository.package_dir("gone").exists()
    assert "gone" not in repository.hidden_names()


def test_remove_deletes_claude_entry_preserving_other_keys_and_layout(native_files, runtime):
    claude, _codex, _opencode = native_files
    claude.write_text(json.dumps(CLAUDE, indent=2) + "\n")

    assert _run(runtime, "remove", {"name": "docs"})["ok"]

    expected = {"numStartups": 7, "mcpServers": {"shared": CLAUDE["mcpServers"]["shared"]}}
    assert claude.read_text() == json.dumps(expected, indent=2) + "\n"


def test_remove_deletes_last_opencode_entry_keeping_comments(native_files, runtime):
    _claude, _codex, opencode = native_files
    text = '{\n  // mine\n  "theme": "x",\n  "mcp": {\n    "keep": {"type": "remote", "url": "https://k.invalid"},\n    "oc": {"type": "local", "command": ["node"]}\n  }\n}\n'
    opencode.write_text(text)

    assert _run(runtime, "remove", {"name": "oc"})["ok"]

    assert opencode.read_text() == text.replace(',\n    "oc": {"type": "local", "command": ["node"]}', "")


def test_remove_of_entry_already_gone_drops_snapshot(native_files, runtime):
    _claude, codex, _opencode = native_files
    repository.package_dir("vanished").mkdir(parents=True)
    before = codex.read_bytes()

    assert _run(runtime, "remove", {"name": "vanished"})["ok"]

    assert codex.read_bytes() == before
    assert not repository.package_dir("vanished").exists()


def test_remove_never_writes_a_file_it_cannot_parse(native_files):
    from core.mcp_hub.errors import McpHubError

    _claude, codex, _opencode = native_files
    codex.write_text("[mcp_servers.local]\ncommand = 'srv'\nbroken")
    with pytest.raises(McpHubError) as caught:
        native.remove_entry(native.CODEX, "local")
    assert caught.value.type == "invalid_native_config"
    assert codex.read_text() == "[mcp_servers.local]\ncommand = 'srv'\nbroken"


def test_concurrent_hide_keeps_every_name(native_files):
    from concurrent.futures import ThreadPoolExecutor

    names = ["shared", "docs", "local"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(repository.hide_name, names))
    assert set(names) <= repository.hidden_names()


def test_unparseable_native_file_is_skipped_and_never_rewritten(native_files):
    _claude, codex, _opencode = native_files
    codex.write_text("[mcp_servers.local\nbroken")
    assert "local" not in native.read_all()
    assert "docs" in native.read_all()
    assert repository.list_packages() == []
    assert native.codex_path().read_text() == "[mcp_servers.local\nbroken"


def test_unreadable_native_bytes_are_skipped_without_aborting_neighbors(native_files):
    _claude, codex, _opencode = native_files
    codex.write_bytes(b"\xff\xfe not utf-8")
    names = native.read_all()
    assert "local" not in names
    assert "docs" in names
    assert repository.list_packages() == []


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
    stored = tomllib.loads(codex.read_text())["mcp_servers"]["node_repl"]
    assert stored["startup_timeout_sec"] == 120
    assert stored["env_vars"] == ["HUB_TEST_PRESENT", "HUB_TEST_MISSING"]


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
    assert only_in_overlay["mcp"]["extra"]["enabled"] is False
    assert only_in_overlay["mcp"]["oc"]["enabled"] is False


def test_remove_last_codex_server_drops_implicit_parent(native_files):
    _claude, codex, _opencode = native_files
    codex.write_text('model = "m"\n\n[mcp_servers.only]\ncommand = "srv"\n')

    assert native.remove_entry(native.CODEX, "only")

    assert codex.read_text() == 'model = "m"\n\n'


def test_refused_native_remove_keeps_oauth_login(native_files, runtime):
    from core.mcp_hub import oauth as mcp_oauth

    _claude, codex, _opencode = native_files
    codex.write_text('[mcp_servers]\ninline = { url = "https://i.invalid/mcp" }\n')
    oauth_file = mcp_oauth.oauth_path("inline")
    oauth_file.parent.mkdir(parents=True)
    oauth_file.write_text("{}")

    result = _run(runtime, "remove", {"name": "inline"})

    assert result["ok"] is False and result["error"]["type"] == "invalid_native_config"
    assert oauth_file.exists()
    assert "inline" in codex.read_text()


@pytest.mark.parametrize("v2", [False, True])
def test_opencode_cli_shapes_and_disabled_entries(native_files, v2):
    """V2 CLI containers must not become a server named 'servers'; V1 keeps that name."""
    from pathlib import Path

    path = native_files[2]
    data = json.loads((Path(__file__).parent / "fixtures/mcp_native/opencode-v2.json").read_text())
    entries = data['mcp']['servers']
    entries['off'] = {'type': 'local', 'command': ['unused'], 'disabled' if v2 else 'enabled': v2}
    if not v2:
        entries['servers'] = {'type': 'remote', 'url': 'https://example.invalid/mcp'}
        data['mcp'] = entries
    path.write_text(json.dumps(data))
    found = native.read_all()
    assert found['same'].definition['transport']['command'] == '/usr/bin/false'
    assert found['same'].definition['transport']['args'] == ['GLOBAL']
    assert 'off' not in found
    assert 'off' in native.disabled_names()
    assert native.disabled_source('off') == 'opencode'
    assert ('servers' in found) is not v2
    if not v2:
        assert found['servers'].definition['transport']['url'] == 'https://example.invalid/mcp'


def test_opencode_v2_delete_preserves_nested_config(native_files):
    path = native_files[2]
    original = '''{ // keep outer
  "mcp": {
    "servers": {
      "drop": {"type": "local", "command": ["false"]},
      "keep": {"type": "remote", "url": "https://example.invalid/mcp"}
    },
    "other": {"keep": true}
  },
  "theme": "system"
}
'''
    path.write_text(original)
    assert native.remove_entry('opencode', 'drop')
    assert path.read_text() == original.replace('      "drop": {"type": "local", "command": ["false"]},\n', '')
    assert native.remove_entry('opencode', 'keep')
    assert json.loads(path.read_text().replace('// keep outer', ''))['mcp'] == {'servers': {}, 'other': {'keep': True}}
