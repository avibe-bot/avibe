"""C-8 behavioral boundaries absent from the released three-CLI fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from config.v2_compat import to_app_config
from config.v2_settings import RoutingSettings
from core.controller import Controller
from core.services.agent_run_target import resolve_agent_run_target
from core.vibe_agents import VibeAgentStore
from modules.im import MessageContext
from tests.test_api_save_config_merge import _full_config_payload
from vibe import api


RELEASED = json.loads((Path(__file__).parent / "fixtures/agent_registration/released_agents.json").read_text())


@pytest.mark.parametrize("fixture", RELEASED, ids=lambda fixture: fixture["name"])
def test_released_config_shapes_preserve_native_settings_and_keep_avibe_off(tmp_path, fixture):
    """A new optional backend must not change released config values or enable itself."""
    payload = _full_config_payload()
    payload["agents"] = fixture["agents"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    loaded = V2Config.load(path, persist_migrations=False)
    for config in (loaded, V2Config.from_payload(api.config_to_payload(loaded, include_secrets=True))):
        assert config.agents.avibe.enabled is False
        assert config.agents.avibe.__dict__ == {"enabled": False}
        for backend, expected in fixture["preserved"].items():
            actual = getattr(config.agents, backend).__dict__
            assert {field: actual[field] for field in expected} == expected


@pytest.mark.parametrize("invalid", [None, [], "invalid", {"enabled": "false"}, {"enabled": 1}])
def test_invalid_optional_avibe_config_recovers_off_without_disabling_native_backends(tmp_path, invalid):
    """Malformed new optional state must recover locally, not prevent startup."""
    payload = _full_config_payload()
    payload["agents"]["avibe"] = invalid
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    loaded = V2Config.load(path, persist_migrations=False)
    assert loaded.agents.avibe.enabled is False
    assert loaded.agents.codex.enabled is True
    assert loaded.agents.opencode.active_turn_timeout_seconds == 7200
    assert loaded.load_warnings


def test_avibe_agent_can_be_created_listed_selected_and_routed(tmp_path, sqlite_db_factory):
    """The store and shared run-target resolver must agree on the fourth backend."""
    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        blank = store.create(name="unconfigured", backend="avibe")
        assert blank.model is None  # No native model recommendation for an empty Hub catalog.
        created = store.create(name="worker-工作助手", backend="avibe", model="team-model", system_prompt="Follow the plan.")
        store.set_default_agent_name(created.name)
        assert [(agent.name, agent.backend) for agent in store.list_agents() if agent.id == created.id] == [
            ("worker-工作助手", "avibe")
        ]
        config = V2Config.from_payload(_full_config_payload())
        config.agents.avibe.enabled = True
        controller = Controller.__new__(Controller)
        controller.primary_platform = "slack"
        controller.sqlite_engine = store.engine
        controller.config = to_app_config(config, resolve_agent_paths=False)
        controller.agent_service = SimpleNamespace(agents={"avibe": object()})
        controller.vibe_agent_store = store
        controller._get_settings_key = lambda context: context.channel_id
        controller.get_settings_manager_for_context = lambda _context: SimpleNamespace(
            get_channel_routing=lambda _key: RoutingSettings(agent_name=created.name),
        )
        context = MessageContext(user_id="test", channel_id="C-test", platform="slack")
        assert controller.resolve_agent_for_context(context) == "avibe"
        target = resolve_agent_run_target(context, controller=controller, create_session=False)
        assert target.agent_backend == "avibe"
        assert target.agent_name == "worker-工作助手"
        assert target.model == "team-model"
        assert api._enabled_agent_backends_from_config(config)[-1] == "avibe"
    finally:
        store.close()


def test_avibe_native_operations_are_rejected_before_any_native_probe(monkeypatch):
    """Expanding Agent registration must not enable the same id on native APIs."""
    from vibe import cli, global_agents_md

    def forbidden(*_args, **_kwargs):
        pytest.fail("in-process backend reached a native/CLI path")

    monkeypatch.setattr(api, "resolve_cli_path", forbidden)
    monkeypatch.setattr(api, "load_config", forbidden)
    monkeypatch.setattr(api, "_runtime_command_dir", forbidden)
    monkeypatch.setattr(api, "MigrationFileLock", forbidden)
    assert api.start_agent_install_job("avibe")["ok"] is False
    assert api.install_agent("avibe")["ok"] is False
    assert api.get_backend_runtime("avibe")["ok"] is False
    assert api.restart_backend("avibe")["ok"] is False
    with pytest.raises(ValueError):
        global_agents_md.global_instruction_path("avibe")
    parser = cli.build_parser()
    assert parser.parse_args(["agent", "create", "worker", "--backend", "avibe"]).backend == "avibe"
    assert parser.parse_args(["agent", "models", "--backend", "avibe"]).backend == "avibe"
    with pytest.raises(SystemExit):
        parser.parse_args(["agent", "import", "--from", "avibe", "--all"])


def test_native_auth_refresh_preserves_enabled_avibe_builtin(tmp_path, sqlite_db_factory, monkeypatch):
    """Native auth refresh synchronizes all built-ins; CLI-only fixtures miss cross-backend disablement."""
    from core.agent_auth_service import AgentAuthService

    config = V2Config.from_payload(_full_config_payload())
    config.agents.avibe.enabled = True
    monkeypatch.setattr(V2Config, "load", lambda: config)
    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        store.ensure_builtin_default_agents(["avibe"])
        service = AgentAuthService(SimpleNamespace(vibe_agent_store=store))
        service._sync_builtin_default_agents()
        avibe = store.get("avibe")
        assert avibe is not None and avibe.enabled
        assert avibe.backend == "avibe"
    finally:
        store.close()


def test_avibe_model_options_come_from_configured_hub_catalog(monkeypatch):
    """A new backend must not fall through to the OpenCode provider reader."""
    from config.v2_config import ModelHubBackendModelConfig

    config = V2Config.from_payload(_full_config_payload())
    config.model_hub.agents["avibe"].models = [
        ModelHubBackendModelConfig(
            id="team-model", origin="manual", display_name="Team Model",
            reasoning_efforts=["low", "high"], supports_reasoning=True,
        )
    ]
    monkeypatch.setattr(V2Config, "load", lambda: config)
    monkeypatch.setattr(api, "_opencode_model_options", lambda **_: pytest.fail("native model lookup"))
    assert api.agent_model_options("avibe")["models"] == [
        {"value": "team-model", "label": "Team Model", "reasoning_efforts": ["low", "high"]}
    ]
    config.model_hub.agents["avibe"].models = []
    assert api.agent_model_options("avibe")["models"] == []
