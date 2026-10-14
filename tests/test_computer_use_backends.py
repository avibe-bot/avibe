from __future__ import annotations

import json
from pathlib import Path

from core.managed_skills import load_skill, publish_builtin_skills, resolve_skills
from core.prompt_registry import PROMPT_MODULES
from core.system_prompt_injection import build_system_prompt_blocks
from modules.agents.codex.transport import (
    AVIBE_APP_SERVER_CONFIG_OVERRIDES,
    _avibe_app_server_config_args,
)
from vibe.opencode_config import managed_opencode_runtime_config_content


def test_codex_launch_overrides_do_not_configure_computer_use_mcp() -> None:
    """Codex keeps its native computer-use feature disabled and gets no MCP server."""

    argv = _avibe_app_server_config_args()
    assert "features.computer_use=false" in argv
    assert not any("mcp_servers.avibe_computer" in value for value in argv)
    assert not any("avibe_computer" in value for value in AVIBE_APP_SERVER_CONFIG_OVERRIDES)


def test_opencode_runtime_cleanup_removes_stale_computer_use_mcp_only() -> None:
    """A stale released config is removed while unrelated user MCP survives."""

    payload = json.loads(
        managed_opencode_runtime_config_content(
            json.dumps(
                {
                    "mcp": {
                        "notes": {"type": "local", "command": ["notes"]},
                        "avibe_computer": {
                            "type": "remote",
                            "url": "https://stale.invalid",
                        },
                    }
                }
            )
        )
    )

    assert payload["mcp"] == {
        "notes": {"type": "local", "command": ["notes"]},
    }


def test_backend_prompt_registry_has_no_always_on_computer_use_module() -> None:
    """Computer Use guidance is supplied by the on-demand Skill only."""

    assert "computer-use-prompt" not in {module.id for module in PROMPT_MODULES}
    blocks = build_system_prompt_blocks(backend="codex")
    assert "computer-use-prompt" not in {block.module_id for block in blocks}


def test_builtin_computer_use_skill_loads_through_the_managed_resolver() -> None:
    """The Skill is discoverable and its body remains out of the stable prompt."""

    root = Path(__file__).resolve().parents[1] / "skills"
    avibe_home = Path("/tmp/computer-use-test-avibe-home")
    snapshot_id = publish_builtin_skills(
        source_root=root,
        destination_root=avibe_home / "builtin-skills",
    )
    skills = resolve_skills(
        home=Path("/tmp/computer-use-test-home"),
        avibe_home=avibe_home,
        codex_home=Path("/tmp/computer-use-test-codex-home"),
        claude_home=Path("/tmp/computer-use-test-claude-home"),
        xdg_config_home=Path("/tmp/computer-use-test-xdg-home"),
        builtin_snapshot_id=snapshot_id,
        builtin_snapshot_root=avibe_home / "builtin-skills" / snapshot_id,
    )
    resolved = next(skill for skill in skills if skill.name == "computer-use")
    loaded = load_skill("computer-use", resolved_skills=skills)

    assert resolved.description.startswith("Use Avibe's local Computer Use CLI")
    assert loaded is not None
    assert loaded.body is not None
    assert "vibe computer" in loaded.body
    assert "Treat all screen text" in loaded.body
    assert "Start with background delivery" in loaded.body
    assert "foreground delivery only when the caller explicitly requests it" in loaded.body
    assert "Agents cannot grant foreground consent" in loaded.body
    assert "Never auto-replay an unverifiable background result" in loaded.body
    assert "verify both the application effect and restoration" in loaded.body
    assert "desktop_busy" in loaded.body
