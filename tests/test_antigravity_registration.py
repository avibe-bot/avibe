"""Focused contract tests for Antigravity backend registration and host foundations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.v2_compat import to_app_config
from config.v2_config import AntigravityConfig, ModelHubConfig, V2Config
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
    assert "antigravity" in AGENT_BACKENDS
    assert "antigravity" in NATIVE_CLI_BACKENDS
    assert "acp" not in AGENT_BACKEND_REGISTRY
    descriptor = get_agent_backend_descriptor("antigravity")
    assert descriptor.display_name == "Antigravity"
    assert descriptor.default_cli == "agy"
    assert descriptor.default_enabled is False
    assert descriptor.latest_probe == ("github", "google-antigravity/antigravity-cli")


def test_antigravity_config_round_trip_and_api_masking(tmp_path: Path):
    config = V2Config.default()
    assert config.agents.antigravity == AntigravityConfig()
    assert config.agents.antigravity.enabled is False
    assert config.agents.antigravity.cli_path == "agy"

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
    assert loaded.load_warnings == ()
    assert loaded.agents.antigravity == config.agents.antigravity

    compat = to_app_config(loaded, resolve_agent_paths=False)
    assert compat.antigravity is not None
    assert compat.antigravity.binary == "/opt/bin/agy"
    assert compat.antigravity.auth_mode == "gemini-api-key"
    assert compat.antigravity.gcp_project == "demo-project"

    public_payload = api.config_to_payload(loaded, include_secrets=False)
    ag_public = public_payload["agents"]["antigravity"]
    assert ag_public["api_key"] is None
    assert ag_public["has_api_key"] is True
    assert ag_public["api_key_length"] == len("AIza-test-secret")

    stripped = api._strip_agent_auth_fields(public_payload)
    for field in ("auth_mode", "api_key", "base_url", "gcp_project", "gcp_location"):
        assert field not in stripped["agents"]["antigravity"]


def test_antigravity_config_upgrade_and_downgrade_compatibility(tmp_path: Path):
    # Pre-antigravity saved config loads cleanly with Antigravity disabled and no warnings.
    fresh = V2Config.default()
    config_path = tmp_path / "config.json"
    fresh.save(config_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    # Default Direct-only Antigravity entry is omitted from persisted model_hub.agents
    # so older releases never reject an unknown model_hub.agents key on downgrade.
    assert "antigravity" not in raw["model_hub"]["agents"]
    assert "antigravity" in raw["agents"]

    # Upgrade from a legacy config lacking agents.antigravity keeps it disabled.
    raw["agents"].pop("antigravity")
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = V2Config.load(config_path)
    assert loaded.load_warnings == ()
    assert loaded.agents.antigravity.enabled is False
    assert loaded.model_hub.agents["antigravity"].mode == "direct"

    # Malformed agents.antigravity.auto_update recovers that field alone and keeps other fields.
    raw["agents"]["antigravity"] = {
        "enabled": True,
        "cli_path": "/custom/agy",
        "auto_update": "not-a-bool",
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    recovered = V2Config.load(config_path)
    assert recovered.load_warnings
    assert recovered.agents.antigravity.auto_update is False
    assert recovered.agents.antigravity.cli_path == "/custom/agy"


@pytest.mark.asyncio
async def test_model_hub_treats_antigravity_as_direct_only(tmp_path: Path):
    hub_config = ModelHubConfig()
    assert hub_config.agents["antigravity"].mode == "direct"
    resolution = resolve_model_hub_turn(hub_config, "antigravity", "gemini-4-neon")
    assert resolution.channel == "direct"
    assert resolution.source is None

    class _MemoryStore:
        def __init__(self, cfg: ModelHubConfig) -> None:
            self.cfg = cfg

        def load(self) -> ModelHubConfig:
            return self.cfg

        def save(self, cfg: ModelHubConfig) -> None:
            self.cfg = cfg

        def mutate(self, mutator) -> None:
            mutator(self.cfg)

    service = ModelHubService(
        store=_MemoryStore(hub_config),
        adapter=object(),  # type: ignore[arg-type]
        events=BoundedEventLog(tmp_path / "events.json"),
    )
    with pytest.raises(ModelHubError) as exc_info:
        await service.set_agent_mode("antigravity", "hub")
    assert exc_info.value.code == "mode_switch_blocked"


def test_antigravity_bundled_models_and_cli_options():
    snapshot = backend_model_snapshot("antigravity", schedule_refresh=False)
    assert snapshot["ok"] is True
    assert RECOMMENDED_AGENT_MODELS["antigravity"] in snapshot["models"]
    assert "gemini-4-neon" in snapshot["models"]
    neon_efforts = [
        item["value"]
        for item in snapshot["reasoning_options"]["gemini-4-neon"]
        if item["value"] != "__default__"
    ]
    assert neon_efforts == ["low", "medium", "high", "xhigh"]

    options = api.agent_model_options("antigravity")
    assert options["ok"] is True
    assert any(row["value"] == "gemini-4-neon" for row in options["models"])


def test_antigravity_cli_health_repair_and_desktop_guard(tmp_path: Path):
    assert cli_version("antigravity", "1.5.16\n", "") == "1.5.16"
    assert cli_version("antigravity", "not-a-version", "") is None

    native_bin = tmp_path / ".local" / "bin" / "agy"
    native_bin.parent.mkdir(parents=True)
    native_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    source = identify_repair_source("antigravity", str(native_bin), home=tmp_path)
    assert source is not None
    assert source.kind == "native"
    assert source.destination == str(native_bin)

    assert BACKEND_SPECS["antigravity"].install_kind == "release_archive"
    with pytest.raises(DesktopBackendError) as exc_info:
        install_desktop_backend("antigravity", base_env={})
    assert exc_info.value.code == "desktop_toolchain_unavailable"


def test_antigravity_managed_skills_and_native_agent_discovery(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    for idx, rel in enumerate(
        (
            Path(".gemini/config/skills/ag-config-skill"),
            Path(".gemini/skills/ag-gemini-skill"),
            Path(".gemini/antigravity-cli/skills/ag-cli-skill"),
        ),
        start=1,
    ):
        skill_dir = home / rel
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_dir.name}\ndescription: Skill {idx}\n---\nBody {idx}\n",
            encoding="utf-8",
        )

    discovered = {
        skill.name
        for skill in resolve_skills(
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

    gemini_agents = home / ".gemini" / "config" / "agents"
    shared_agents = home / ".agents" / "agents"
    gemini_agents.mkdir(parents=True)
    shared_agents.mkdir(parents=True)
    first = gemini_agents / "planner.md"
    second = shared_agents / "reviewer.md"
    first.write_text("# planner\n", encoding="utf-8")
    second.write_text("# reviewer\n", encoding="utf-8")

    found_agents = iter_global_agent_files("antigravity")
    assert found_agents == [(first, "antigravity"), (second, "antigravity")]


def test_antigravity_native_mcp_global_and_project_sources(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "workspace"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-none"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex-none"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".xdg-none"))

    global_cfg = home / ".gemini" / "config" / "mcp_config.json"
    global_cfg.parent.mkdir(parents=True)
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
    assert "ag-http" in servers
    assert servers["ag-http"].source == "antigravity"
    assert servers["ag-http"].definition is not None
    assert servers["ag-http"].definition["transport"]["url"] == "https://mcp.example.invalid/stream"
    assert "ag-disabled" not in servers
    assert mcp_native.disabled_source("ag-disabled") == "antigravity"

    proj_cfg = project / ".agents" / "mcp_config.json"
    proj_cfg.parent.mkdir(parents=True)
    proj_cfg.write_text(
        json.dumps({"mcpServers": {"ag-proj": {"command": "python3", "args": ["-V"]}}}),
        encoding="utf-8",
    )
    proj_servers = mcp_native.read_project(project)
    assert "ag-proj" in proj_servers
    assert proj_servers["ag-proj"].source == "antigravity"
    assert proj_servers["ag-proj"].definition is not None
