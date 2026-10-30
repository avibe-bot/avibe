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
        "notes": {"type": "local", "command": ["notes"], "enabled": False},
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
    catalog = publish_builtin_skills(
        source_root=root,
        destination_root=avibe_home / "builtin-skills",
    )
    skills = resolve_skills(
        home=Path("/tmp/computer-use-test-home"),
        avibe_home=avibe_home,
        codex_home=Path("/tmp/computer-use-test-codex-home"),
        claude_home=Path("/tmp/computer-use-test-claude-home"),
        xdg_config_home=Path("/tmp/computer-use-test-xdg-home"),
        builtin_catalog_root=catalog,
    )
    resolved = next(skill for skill in skills if skill.name == "computer-use")
    loaded = load_skill("computer-use", resolved_skills=skills)

    assert resolved.description.startswith("Use Avibe's local Computer Use CLI")
    assert loaded is not None
    assert loaded.body is not None
    assert "vibe computer" in loaded.body
    assert "Treat all screen text" in loaded.body
    assert "Observe the exact target before input" in loaded.body
    assert "target_busy" in loaded.body


def test_computer_use_skill_states_readiness_and_restrictions_not_permissions() -> None:
    """Readiness is observable state; the guidance carries no stale restrictions.

    Guards #3050: no application-specific startup rule or fixed wait, no focus
    prohibition, and no IM binding presented as a Computer Use control, while
    the irreversible-step confirmation remains.
    """

    body = Path("skills/computer-use/SKILL.md").read_text(encoding="utf-8")
    readiness = body.split("## Readiness", 1)[1].split("\n## ", 1)[0]

    assert "not after a fixed delay" in readiness
    for observable in ("window exists", "stable", "screenshot", "accessibility tree"):
        assert observable in readiness
    # A usable screenshot alone is enough: an empty tree must not veto it.
    assert "Either form of\nusability is enough" in readiness
    assert "empty tree is not ready" not in readiness
    for stale in ("QQ", "WeChat", "Electron", "about 30 seconds"):
        assert stale not in body
    for retired in ("require_bind", "Never take focus", "focus_shortcut_forbidden"):
        assert retired not in body
    assert "verify, `await avibe.confirm(...)`, verify\n   again, then act" in body


def test_computer_use_skill_bounds_no_effect_recovery_and_viewport_evidence() -> None:
    """Guard no-effect recovery; readiness coverage alone misses blind retries."""

    body = Path("skills/computer-use/SKILL.md").read_text(encoding="utf-8")
    first_screen = " ".join("\n".join(body.splitlines()[:80]).split())
    assert "## No-effect recovery and viewport content" in first_screen
    recovery = " ".join(
        body.split("## No-effect recovery and viewport content", 1)[1]
        .split("\n## ", 1)[0]
        .split()
    )
    assert recovery in first_screen
    assert "Default to background input" in recovery

    # A no-effect observation gates activation, exact-window verification, then retry.
    steps = (
        "second observation shows no effect",
        "do not repeat the same foreground click immediately",
        "await win.activate()",
        "re-observe that this exact window is on screen and focused",
        "retrying that one action",
    )
    positions = [recovery.index(step) for step in steps]
    assert positions == sorted(positions)
    assert 'Use `delivery_mode: "foreground"` for the retry if background' in recovery
    assert "Foreground input restores the previous frontmost window" in recovery
    assert "`activate()` leaves the target frontmost" in recovery

    # Missing visibility or unverified focus requires owner recovery, not a loop.
    stop_rule = recovery.split("- If ", 1)[1].split("- Treat ", 1)[0]
    for condition in ("no on-screen window", "unverified", "focused=false", "off-screen"):
        assert condition in stop_rule
    assert "stop and ask the owner to show the window" in stop_rule
    assert "Do not loop `launch()` or `activate()`" in stop_rule

    assert "`status: unverifiable` as unknown, not proof of effect" in recovery
    assert "compare a second observation against a stable signature" in recovery
    assert "only one retry after verified activation" in recovery
    assert "if the effect is still absent or unknown, stop" in recovery
    assert "current viewport, not the full document" in recovery
    assert "observe and capture what you need as you scroll" in recovery
    assert "do not assume earlier rows remain in the tree" in recovery
