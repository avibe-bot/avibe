from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib

from core.computer_use import (
    COMPUTER_USE_TOOL_SNAPSHOT_SHA256,
    ManagedMcpServerSpec,
    managed_mcp_server_spec,
)
from core.handlers.session_handler import (
    apply_managed_computer_use_to_claude_options,
)
from core.system_prompt_injection import build_system_prompt_blocks
from modules.agents.codex.transport import _managed_mcp_config_args
from vibe.opencode_config import managed_opencode_runtime_config_content


def _spec() -> ManagedMcpServerSpec:
    return ManagedMcpServerSpec(
        name="avibe_computer",
        command="/Applications/Avibe.app/Contents/Resources/python",
        args=("-I", "/Applications/Avibe.app/Contents/Resources/core/computer_server.py"),
        env={"AVIBE_COMPUTER_USE_STATE_DIR": "/tmp/desktop state"},
        fingerprint="managed-v1",
    )


def test_claude_translation_preserves_native_config_and_agent_tool_allowlist() -> None:
    """Managed MCP is additive and strict_mcp_config remains SDK-default false."""

    options: dict = {}
    tools = apply_managed_computer_use_to_claude_options(
        options,
        ["Read", "Bash"],
        _spec(),
    )
    assert options["mcp_servers"] == {
        "avibe_computer": {
            "type": "stdio",
            "command": "/Applications/Avibe.app/Contents/Resources/python",
            "args": ["-I", "/Applications/Avibe.app/Contents/Resources/core/computer_server.py"],
            "env": {"AVIBE_COMPUTER_USE_STATE_DIR": "/tmp/desktop state"},
        }
    }
    assert "strict_mcp_config" not in options
    assert tools == ["Read", "Bash", "mcp__avibe_computer__*"]

    no_agent_allowlist: dict = {}
    assert (
        apply_managed_computer_use_to_claude_options(
            no_agent_allowlist,
            None,
            _spec(),
        )
        is None
    )
    assert "allowed_tools" not in no_agent_allowlist


def test_codex_translation_is_fixed_last_and_approves_managed_tools() -> None:
    """Codex receives one parseable managed server with server-level approval."""

    argv = _managed_mcp_config_args(_spec())
    overrides = [argv[index + 1] for index in range(0, len(argv), 2)]
    parsed = tomllib.loads("\n".join(overrides))
    server = parsed["mcp_servers"]["avibe_computer"]
    assert server == {
        "command": "/Applications/Avibe.app/Contents/Resources/python",
        "args": ["-I", "/Applications/Avibe.app/Contents/Resources/core/computer_server.py"],
        "default_tools_approval_mode": "approve",
        "env": {"AVIBE_COMPUTER_USE_STATE_DIR": "/tmp/desktop state"},
    }


def test_opencode_translation_preserves_user_mcp_and_owns_only_reserved_name() -> None:
    """The runtime overlay must not erase a user's other native MCP servers."""

    raw = """
    {
      "mcp": {
        "notes": {"type": "local", "command": ["notes"]},
        "avibe_computer": {"type": "remote", "url": "https://stale.invalid"}
      }
    }
    """
    import json

    payload = json.loads(
        managed_opencode_runtime_config_content(
            raw,
            computer_use_spec=_spec(),
        )
    )
    assert payload["mcp"]["notes"] == {
        "type": "local",
        "command": ["notes"],
    }
    assert payload["mcp"]["avibe_computer"] == {
        "type": "local",
        "command": [
            "/Applications/Avibe.app/Contents/Resources/python",
            "-I",
            "/Applications/Avibe.app/Contents/Resources/core/computer_server.py",
        ],
        "environment": {
            "AVIBE_COMPUTER_USE_STATE_DIR": "/tmp/desktop state",
        },
        "enabled": True,
    }


def test_computer_prompt_exists_exactly_when_managed_server_is_configured() -> None:
    """Agents pay the guidance cost only when the 28-tool server is injected."""

    disabled = build_system_prompt_blocks(include_computer_use=False)
    enabled = build_system_prompt_blocks(include_computer_use=True)
    assert "computer-use-prompt" not in {block.module_id for block in disabled}
    assert [block.module_id for block in enabled].count("computer-use-prompt") == 1


def test_managed_server_absolute_entrypoint_survives_shadowing_cwd(tmp_path: Path) -> None:
    """A workspace ``core`` package cannot replace Avibe's managed server."""

    shadow = tmp_path / "shadow"
    (shadow / "core").mkdir(parents=True)
    (shadow / "core" / "__init__.py").write_text(
        "raise RuntimeError('shadow package executed')\n",
        encoding="utf-8",
    )
    (shadow / "core" / "computer_server.py").write_text(
        "raise RuntimeError('shadow module executed')\n",
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    snapshot = Path("desktop/cua-driver/tools-v0.31.0.json").resolve()
    state = {
        "schema_version": 1,
        "enabled": True,
        "state": "off",
        "reason": None,
        "shell_pid": 1,
        "instance_id": "shadow-test",
        "generation": 1,
        "driver_version": "0.31.0",
        "tool_snapshot": {
            "path": str(snapshot),
            "sha256": COMPUTER_USE_TOOL_SNAPSHOT_SHA256,
        },
    }
    state_path = state_dir / "computer-use.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    spec = managed_mcp_server_spec(
        state_path=state_path,
        python_executable=sys.executable,
    )
    assert spec is not None
    result = subprocess.run(
        [spec.command, *spec.args],
        cwd=shadow,
        env={**spec.env, "PATH": str(Path(sys.executable).parent)},
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert len(payload["result"]["tools"]) == 28
