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
from vibe.backend_model_catalog import backend_model_snapshot, load_bundled_catalog
from vibe.cli_health import cli_version
from vibe.cli_repair import identify_repair_source
from vibe.desktop_backends import BACKEND_SPECS, DesktopBackendError, install_desktop_backend


AGY_MODEL_IDS = ['gemini-4-neon', 'gemini-4-carbon', 'gemini-4-argon', 'gemini-4-hydrogen', 'gemini-pro-orchestrator-neon', 'gemini-pro-orchestrator', 'gemini-pro-orchestrator-argon', 'gemini-3.8-flash', 'claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-oss-120b-medium']


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


@pytest.mark.asyncio
async def test_antigravity_live_model_catalog_cache_is_account_scoped_and_enriches_efforts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modules.agents import acp
    from modules.agents.antigravity.auth import run_antigravity_connection_probe
    from vibe.backend_model_catalog import backend_builtin_snapshot

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    cfg = V2Config.default()
    cfg.agents.antigravity.enabled = True
    cfg.agents.antigravity.auth_mode = "gemini-api-key"
    cfg.agents.antigravity.api_key = "fixture-key"
    cfg.save()

    from tests.helpers.acp_probe_stub import make_probe_client

    Client, observed = make_probe_client(
        options=[{'id': 'model', 'category': 'model', 'currentValue': 'live-only-model', 'options': [{'value': 'live-only-model', 'name': 'Live Only Model'}, {'value': 'gemini-2.5-pro', 'name': 'Gemini 2.5 Pro'}]}, {'id': 'thought_level', 'category': 'thought_level', 'currentValue': 'low', 'options': [{'value': 'low'}, {'value': 'xhigh'}]}],
        selection={'configOptions': [{'id': 'model', 'category': 'model', 'currentValue': 'gemini-2.5-pro', 'options': [{'value': 'live-only-model', 'name': 'Live Only Model'}, {'value': 'gemini-2.5-pro', 'name': 'Gemini 2.5 Pro'}]}, {'id': 'thought_level', 'category': 'thought_level', 'currentValue': 'medium', 'options': [{'value': 'low'}, {'value': 'medium'}]}]},
        chunks=[("sess-probe-1", "ok")],
    )
    monkeypatch.setattr(acp, "AcpClient", Client)
    probe = await run_antigravity_connection_probe(
        binary="/fake/agy",
        cwd=str(tmp_path),
        model="gemini-2.5-pro",
        config=cfg.agents.antigravity,
    )
    assert probe["ok"] is True and observed["spawns"] == 1

    # (a) Matching fingerprint uses authoritative live models and preserves per-model efforts.
    snapshot = backend_model_snapshot("antigravity", schedule_refresh=False)
    assert snapshot["models"] == ["live-only-model", "gemini-2.5-pro"]
    assert "gemini-4-neon" not in snapshot["models"]
    assert snapshot["model_labels"] == {
        "live-only-model": "Live Only Model",
        "gemini-2.5-pro": "Gemini 2.5 Pro",
    }
    assert [i["value"] for i in snapshot["reasoning_options"]["live-only-model"] if i["value"] != "__default__"] == [
        "low",
        "xhigh",
    ]
    assert [i["value"] for i in snapshot["reasoning_options"]["gemini-2.5-pro"] if i["value"] != "__default__"] == [
        "low",
        "medium",
    ]
    builtin = backend_builtin_snapshot("antigravity", schedule_refresh=False)
    assert [m["id"] for m in builtin["models"]] == ["live-only-model", "gemini-2.5-pro"]
    assert observed["spawns"] == 1

    # (b) Changing auth_mode/api_key invalidates the fingerprint and falls back to static catalog without spawning.
    cfg.agents.antigravity.auth_mode = "gemini-api-key"
    cfg.agents.antigravity.api_key = "AIza-different-account-key"
    cfg.save()

    fallback = backend_model_snapshot("antigravity", schedule_refresh=False)
    assert "gemini-4-neon" in fallback["models"]
    assert "live-only-model" not in fallback["models"]
    assert observed["spawns"] == 1


@pytest.fixture
def agy_session_options() -> dict:
    """Trimmed agy 1.5.17 capture: legacy model IDs differ from selectable values."""
    return {
        "sessionId": "captured-session",
        "models": {"availableModels": [{"modelId": value} for value in [
            "gemini-4-neon-high", "gemini-4-neon-xhigh", "gemini-4-neon-medium", "gemini-4-neon-low",
            "gemini-4-carbon-medium", "gemini-4-carbon-high", "gemini-4-carbon-low",
            "gemini-3.8-flash-high", "claude-opus-5-5-low", "claude-sonnet-5-5-medium", "gpt-oss-120b-medium",
        ]]},
        "configOptions": [
            {"id": "model", "category": "model", "type": "select", "currentValue": "gemini-4-neon",
             "options": [{"value": value} for value in AGY_MODEL_IDS]},
            {"id": "thought_level", "category": "thought_level", "type": "select", "currentValue": "high",
             "options": [{"value": value} for value in ["low", "medium", "high", "xhigh"]]},
            {"id": "mode", "category": "mode", "type": "select", "currentValue": "default",
             "options": [{"value": value} for value in ["default", "accept-edits", "auto", "plan"]]},
        ],
    }


@pytest.mark.parametrize("extractor", ["catalog", "auth"])
def test_live_catalog_uses_only_config_options(agy_session_options, extractor):
    from modules.agents.antigravity.auth import extract_acp_live_models
    from modules.agents.antigravity.models import extract_live_model_catalog_entries

    expected = AGY_MODEL_IDS
    if extractor == "auth":
        assert extract_acp_live_models(agy_session_options) == expected
        assert extract_acp_live_models({"models": agy_session_options["models"]}) == []
    else:
        entries = extract_live_model_catalog_entries(agy_session_options)
        assert [entry["id"] for entry in entries] == expected
        assert entries[0]["reasoning_efforts"] == ["low", "medium", "high", "xhigh"]
        assert all("reasoning_efforts" not in entry for entry in entries[1:])
        assert extract_live_model_catalog_entries({"models": agy_session_options["models"]}) == []


@pytest.mark.parametrize("refresh", [False, True])
def test_legacy_model_cache_is_ignored_and_replaced(tmp_path, monkeypatch, agy_session_options, refresh):
    from config import paths
    from modules.agents.antigravity.models import (
        auth_config_fingerprint,
        read_antigravity_live_models,
        record_antigravity_live_models,
    )

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    cfg = V2Config.default()
    cfg.agents.antigravity.auth_mode = "gemini-api-key"
    cfg.agents.antigravity.api_key = "fixture-key"
    cache_path = paths.get_state_dir() / "antigravity_models_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "auth_fingerprint": auth_config_fingerprint(cfg),
        "models": [{"id": "gemini-4-carbon-medium"}],
    }))
    if not refresh:
        assert read_antigravity_live_models(cfg) == []
    else:
        # Config-option updates must discard legacy entries too, even without a full session payload.
        record_antigravity_live_models(cfg, agy_session_options["configOptions"])
        entries = read_antigravity_live_models(cfg)
        assert [entry["id"] for entry in entries] == AGY_MODEL_IDS


def test_live_effort_observation_preserves_empty_and_never_guesses(tmp_path, monkeypatch):
    from modules.agents.antigravity.models import record_antigravity_live_models, read_antigravity_live_models
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    cfg = V2Config.default()
    cfg.agents.antigravity.auth_mode = "gemini-api-key"
    cfg.agents.antigravity.api_key = "fixture-key"
    cfg.save()
    model_option = {"id": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}, {"value": "live-only"}]}
    record_antigravity_live_models(cfg, {"configOptions": [model_option]})
    entries = read_antigravity_live_models(cfg)
    assert entries[0].get("reasoning_efforts") == []
    snapshot = backend_model_snapshot("antigravity", schedule_refresh=False)
    assert all(row["value"] == "__default__" for row in snapshot["reasoning_options"]["gemini-4-neon"])
    assert all(row["value"] == "__default__" for row in snapshot["reasoning_options"]["live-only"])


def test_bundled_antigravity_menu_matches_agy_1_5_17():
    bundle = load_bundled_catalog()["backends"]["antigravity"]["models"]
    expected = {
        "gemini-4-neon": ["low", "medium", "high", "xhigh"],
        "gemini-4-carbon": ["low", "medium", "high"],
        "gemini-4-argon": ["low", "medium", "high"],
        "gemini-4-hydrogen": ["low", "medium", "high", "xhigh"],
        "gemini-pro-orchestrator-neon": [], "gemini-pro-orchestrator": [], "gemini-pro-orchestrator-argon": [],
        "gemini-3.8-flash": ["low", "medium", "high"], "claude-opus-5-5": ["low", "medium", "high"],
        "claude-sonnet-5-5": ["low", "medium", "high"], "gpt-oss-120b-medium": [],
    }
    assert {entry["id"]: entry.get("reasoning_efforts") for entry in bundle} == expected
