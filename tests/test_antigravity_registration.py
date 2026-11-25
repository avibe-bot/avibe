"""Focused contract tests for Antigravity backend registration and host foundations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.v2_compat import to_app_config
from config.v2_config import (
    MODEL_HUB_BACKENDS,
    AntigravityConfig,
    ModelHubConfig,
    V2Config,
    hub_supply_block,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.resolver import resolve_model_hub_turn
from core.handlers.model_hub.service import ModelHubError, ModelHubService
from core.managed_skills import resolve_skills
from core.mcp_hub import native as mcp_native
from core.vibe_agents import RECOMMENDED_AGENT_MODELS, iter_global_agent_files
from modules.agents.catalog import (
    AGENT_BACKEND_REGISTRY,
    AGENT_BACKENDS,
    NATIVE_CLI_BACKENDS,
    get_agent_backend_descriptor,
)
from vibe import api
from vibe.backend_model_catalog import backend_model_snapshot
from vibe.cli_health import cli_version
from vibe.cli_repair import identify_repair_source
from vibe.desktop_backends import BACKEND_SPECS, DesktopBackendError, install_desktop_backend


def test_catalog_registers_antigravity_and_never_acp():
    assert "antigravity" in AGENT_BACKENDS and "antigravity" in NATIVE_CLI_BACKENDS
    assert "acp" not in AGENT_BACKEND_REGISTRY
    d = get_agent_backend_descriptor("antigravity")
    assert (d.display_name, d.default_cli, d.default_enabled, d.latest_probe) == (
        "Antigravity", "agy", False, ("github", "google-antigravity/antigravity-cli")
    )


def test_antigravity_config_round_trip_masking_and_upgrade_compat(tmp_path: Path):
    config = V2Config.default()
    assert config.agents.antigravity == AntigravityConfig()
    assert (config.agents.antigravity.enabled, config.agents.antigravity.cli_path) == (False, "agy")

    config.agents.antigravity = AntigravityConfig(
        enabled=True,
        cli_path="/opt/bin/agy",
        auto_update=False,
        idle_timeout_seconds=900,
        auth_mode="gemini-api-key",
        api_key="AIza-test-secret",
        base_url="https://proxy.example.invalid",
        gcp_project="demo-project",
        gcp_location="us-central1",
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)

    loaded = V2Config.load(config_path)
    assert loaded.load_warnings == () and loaded.agents.antigravity == config.agents.antigravity

    compat = to_app_config(loaded, resolve_agent_paths=False)
    assert compat.antigravity is not None
    assert (compat.antigravity.binary, compat.antigravity.auth_mode, compat.antigravity.gcp_project) == (
        "/opt/bin/agy", "gemini-api-key", "demo-project"
    )

    public_payload = api.config_to_payload(loaded, include_secrets=False)
    ag_public = public_payload["agents"]["antigravity"]
    assert (ag_public["api_key"], ag_public["has_api_key"], ag_public["api_key_length"]) == (
        None, True, len("AIza-test-secret")
    )
    stripped = api._strip_agent_auth_fields(public_payload)["agents"]["antigravity"]
    assert not any(f in stripped for f in ("auth_mode", "api_key", "base_url", "gcp_project", "gcp_location"))

    # Upgrade from pre-antigravity config keeps it disabled and excluded from Model Hub.
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert "antigravity" not in raw["model_hub"]["agents"] and "antigravity" in raw["agents"]
    raw["agents"].pop("antigravity")
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    upgraded = V2Config.load(config_path)
    assert upgraded.load_warnings == () and upgraded.agents.antigravity.enabled is False
    assert "antigravity" not in upgraded.model_hub.agents

    # Malformed auto_update recovers that field alone and keeps sibling fields.
    raw["agents"]["antigravity"] = {"enabled": True, "cli_path": "/custom/agy", "auto_update": "not-a-bool"}
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    recovered = V2Config.load(config_path)
    assert recovered.load_warnings and recovered.agents.antigravity.auto_update is False
    assert recovered.agents.antigravity.cli_path == "/custom/agy"


@pytest.mark.asyncio
async def test_model_hub_excludes_antigravity_and_keeps_it_direct(tmp_path: Path):
    assert "antigravity" not in MODEL_HUB_BACKENDS
    hub_config = ModelHubConfig()
    assert "antigravity" not in hub_config.agents
    for cfg in (hub_config, ModelHubConfig(enabled=False), None):
        assert hub_supply_block("antigravity", cfg) is None

    res = resolve_model_hub_turn(hub_config, "antigravity", "gemini-4-neon")
    assert (res.channel, res.source) == ("direct", None)

    class _MemoryStore:
        def __init__(self, cfg: ModelHubConfig) -> None:
            self.cfg = cfg

        def load(self) -> ModelHubConfig:
            return self.cfg

        def save(self, cfg: ModelHubConfig) -> None:
            self.cfg = cfg

        def mutate(self, mutator) -> None:
            mutator(self.cfg)

    service = ModelHubService(store=_MemoryStore(hub_config), adapter=object(), events=BoundedEventLog(tmp_path / "events.json"))  # type: ignore[arg-type]
    assert "antigravity" not in {agent["backend"] for agent in service.list_agents()}
    with pytest.raises(ModelHubError) as exc_info:
        await service.set_agent_mode("antigravity", "hub")
    assert exc_info.value.code == "mode_switch_blocked"


def test_antigravity_bundled_models_cli_health_repair_and_desktop_guard(tmp_path: Path):
    snapshot = backend_model_snapshot("antigravity", schedule_refresh=False)
    assert snapshot["ok"] is True
    assert RECOMMENDED_AGENT_MODELS["antigravity"] in snapshot["models"] and "gemini-4-neon" in snapshot["models"]
    assert [i["value"] for i in snapshot["reasoning_options"]["gemini-4-neon"] if i["value"] != "__default__"] == [
        "low", "medium", "high", "xhigh"
    ]
    options = api.agent_model_options("antigravity")
    assert options["ok"] is True and any(row["value"] == "gemini-4-neon" for row in options["models"])

    assert cli_version("antigravity", "1.5.16\n", "") == "1.5.16"
    assert cli_version("antigravity", "not-a-version", "") is None

    native_bin = tmp_path / ".local" / "bin" / "agy"
    native_bin.parent.mkdir(parents=True)
    native_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    source = identify_repair_source("antigravity", str(native_bin), home=tmp_path)
    assert source is not None and (source.kind, source.destination) == ("native", str(native_bin))

    assert BACKEND_SPECS["antigravity"].install_kind == "release_archive"
    with pytest.raises(DesktopBackendError) as exc_info:
        install_desktop_backend("antigravity", base_env={})
    assert exc_info.value.code == "desktop_toolchain_unavailable"


def test_antigravity_managed_skills_native_agents_and_mcp_sources(tmp_path: Path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "project"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for var, sub in (("CLAUDE_CONFIG_DIR", ".claude-none"), ("CODEX_HOME", ".codex-none"), ("XDG_CONFIG_HOME", ".xdg-none")):
        monkeypatch.setenv(var, str(home / sub))

    for idx, rel in enumerate(
        (".gemini/config/skills/ag-config-skill", ".gemini/skills/ag-gemini-skill", ".gemini/antigravity-cli/skills/ag-cli-skill"),
        start=1,
    ):
        skill_dir = home / rel
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_dir.name}\ndescription: Skill {idx}\n---\nBody {idx}\n", encoding="utf-8")

    discovered = {
        s.name
        for s in resolve_skills(
            workspace,
            home=home,
            avibe_home=tmp_path / ".avibe",
            codex_home=tmp_path / ".codex",
            claude_home=tmp_path / ".claude",
            xdg_config_home=tmp_path / ".xdg",
            builtin_catalog_root=tmp_path / "empty",
        )
    }
    assert {"ag-config-skill", "ag-gemini-skill", "ag-cli-skill"} <= discovered

    first = home / ".gemini" / "config" / "agents" / "planner.md"
    second = home / ".agents" / "agents" / "reviewer.md"
    for p, text in ((first, "# planner\n"), (second, "# reviewer\n")):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    assert iter_global_agent_files("antigravity") == [(first, "antigravity"), (second, "antigravity")]

    global_cfg = home / ".gemini" / "config" / "mcp_config.json"
    global_cfg.parent.mkdir(parents=True, exist_ok=True)
    global_cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ag-http": {"serverUrl": "https://mcp.example.invalid/stream"},
                    "ag-disabled": {"command": "echo", "args": ["off"], "disabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    servers = mcp_native.read_all()
    assert "ag-http" in servers and servers["ag-http"].source == "antigravity"
    assert servers["ag-http"].definition is not None
    assert servers["ag-http"].definition["transport"]["url"] == "https://mcp.example.invalid/stream"
    assert "ag-disabled" not in servers and mcp_native.disabled_source("ag-disabled") == "antigravity"

    proj_cfg = workspace / ".agents" / "mcp_config.json"
    proj_cfg.parent.mkdir(parents=True, exist_ok=True)
    proj_cfg.write_text(json.dumps({"mcpServers": {"ag-proj": {"command": "python3", "args": ["-V"]}}}), encoding="utf-8")
    proj_servers = mcp_native.read_project(workspace)
    assert "ag-proj" in proj_servers and proj_servers["ag-proj"].source == "antigravity" and proj_servers["ag-proj"].definition is not None
