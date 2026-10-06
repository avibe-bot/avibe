"""C-8 behavioral boundaries absent from the released three-CLI fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from config.v2_config import V2Config
from config.v2_compat import to_app_config
from config.v2_settings import RoutingSettings
from core.controller import Controller
from core.services.agent_run_target import resolve_agent_run_target
from core.vibe_agents import BUILTIN_DEFAULT_AGENT_METADATA, VibeAgentStore, is_always_enabled_agent
from modules.agents.catalog import NATIVE_CLI_BACKENDS
from modules.im import MessageContext
from tests.test_api_save_config_merge import _full_config_payload
from vibe import api


RELEASED = json.loads((Path(__file__).parent / "fixtures/agent_registration/released_agents.json").read_text())


@pytest.mark.parametrize("fixture", RELEASED, ids=lambda fixture: fixture["name"])
def test_released_config_shapes_preserve_native_settings(tmp_path, fixture):
    """A new backend must not change released config values."""
    payload = _full_config_payload()
    payload["agents"] = fixture["agents"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    loaded = V2Config.load(path, persist_migrations=False)
    for config in (loaded, V2Config.from_payload(api.config_to_payload(loaded, include_secrets=True))):
        for backend, expected in fixture["preserved"].items():
            actual = getattr(config.agents, backend).__dict__
            assert {field: actual[field] for field in expected} == expected


@pytest.mark.parametrize("persisted", [{"enabled": False}, {"enabled": True}, None, "invalid", {"enabled": "false"}])
def test_a_persisted_avibe_switch_loads_as_on_and_leaves_on_the_next_save(tmp_path, sqlite_db_factory, persisted):
    """Builds with an Avibe Agent switch wrote ``agents.avibe``; the built-in backend has no switch to honor.

    Such a build also left its built-in Agent disabled while the switch was off.
    """
    payload = _full_config_payload()
    payload["agents"]["avibe"] = persisted
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    loaded = V2Config.load(path, persist_migrations=False)
    assert not loaded.load_warnings
    assert loaded.agents.codex.enabled is True
    assert loaded.agents.opencode.active_turn_timeout_seconds == 7200

    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        store.create(
            name="avibe",
            backend="avibe",
            source="builtin",
            metadata={**BUILTIN_DEFAULT_AGENT_METADATA, "backend": "avibe", "backend_enabled": False},
            enabled=False,
        )
        store.ensure_builtin_default_agents(api._enabled_agent_backends_from_config(loaded))
        assert store.get("avibe").enabled
        assert store.get_default_agent_name() == "opencode"
    finally:
        store.close()

    loaded.save(path)
    saved = json.loads(path.read_text())
    assert "avibe" not in saved["agents"]
    assert saved["agents"]["codex"]["enabled"] is True


# Rows an earlier build may have left: (name, backend, source, metadata, enabled).
_BUILT_IN = {**BUILTIN_DEFAULT_AGENT_METADATA, "backend": "avibe", "backend_enabled": True}
_EARLIER_ROWS = {
    "absent": None,
    "disabled": ("avibe", "avibe", "builtin", _BUILT_IN, False),
    # Before the markers were the catalog's, a metadata update could strip them, and the row could then be renamed.
    "markers-stripped": ("avibe", "avibe", "builtin", {}, False),
    "markers-stripped-and-renamed": ("assistant", "avibe", "builtin", {}, False),
    # A user's own Agent already holds the catalog's name for the built-in.
    "user-agent-same-backend": ("vibey", "avibe", "user", {}, False),
    "user-agent-other-backend": ("vibey", "claude", "user", {}, True),
}


@pytest.mark.parametrize("earlier_row", list(_EARLIER_ROWS))
@pytest.mark.parametrize("enabled_backends", [[], ["claude"], list(NATIVE_CLI_BACKENDS)])
def test_the_startup_sync_leaves_the_built_in_avibe_agent_enabled(tmp_path, sqlite_db_factory, earlier_row, enabled_backends):
    """Controller startup runs this sync first; Model Hub then seeds the Avibe supply onto that Agent row.

    Whatever backends the caller read from config, and whatever an earlier build left, exactly one
    built-in Avibe Agent exists and is enabled once the sync returns, and the identity lookup names
    it. A new one is named by the catalog. A row the store created stays the built-in under any name,
    including the ``avibe`` an earlier build gave it; a user's own Agent keeps its name and state.
    """
    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        row = _EARLIER_ROWS[earlier_row]
        earlier = None
        if row is not None:
            name, backend, source, metadata, enabled = row
            earlier = store.create(name=name, backend=backend, source=source, metadata=metadata, enabled=enabled)
        store.ensure_builtin_default_agents(enabled_backends)

        built_in = [agent for agent in store.list_agents() if agent.backend == "avibe" and is_always_enabled_agent(agent)]
        assert len(built_in) == 1 and built_in[0].enabled
        assert store.get_builtin_default_agent_for_backend("avibe") == built_in[0]
        if earlier is None:
            assert built_in[0].name == "vibey"
        if earlier is not None and earlier.source == "builtin":
            assert (built_in[0].id, built_in[0].name) == (earlier.id, earlier.name)
        if earlier is not None and earlier.source == "user":
            kept = store.get_by_id(earlier.id)
            assert (kept.name, kept.backend, kept.enabled, kept.source) == ("vibey", row[1], row[4], "user")
            assert built_in[0].name == "vibey-2"
    finally:
        store.close()


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
    """Native auth refresh synchronizes all built-ins from config, which has no section for the built-in backend."""
    from core.agent_auth_service import AgentAuthService

    config = V2Config.from_payload(_full_config_payload())
    monkeypatch.setattr(V2Config, "load", lambda: config)
    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        store.ensure_builtin_default_agents(["avibe"])
        service = AgentAuthService(SimpleNamespace(vibe_agent_store=store))
        service._sync_builtin_default_agents()
        vibey = store.get("vibey")
        assert vibey is not None and vibey.enabled
        assert vibey.backend == "avibe"
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


def _hub_catalog(*model_ids, origin="provider"):
    from config.v2_config import ModelHubBackendModelConfig, ModelHubConfig

    config = ModelHubConfig()
    config.agents["avibe"].models = [ModelHubBackendModelConfig(id=model_id, origin=origin) for model_id in model_ids]
    return SimpleNamespace(store=SimpleNamespace(load=lambda: config))


@pytest.mark.parametrize("avibe", ["seeded", "user-built list", "chosen", "name taken", "empty list"])
def test_avibe_agent_runs_its_catalogs_first_model_until_it_has_one(tmp_path, sqlite_db_factory, avibe):
    """MH-AVIBE-007: the Agent rows behind the Hub seed, owned by this process.

    The built-in native Agents' models feed the seed in backend-name order. An
    Avibe Agent without a model cannot run a turn, so it takes its catalog's
    first model, seeded or listed by the user, and only while it has none: a
    model the user chose stands. When a user Agent already holds the catalog's
    name for it, the built-in takes the next free name and that Agent is left alone.
    """
    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        if avibe == "name taken":
            store.create(name="vibey", backend="claude", model="claude-haiku-4-5")
        store.ensure_builtin_default_agents(["opencode", "claude", "codex", "avibe"])
        store.create(name="reviewer", backend="claude", model="claude-sonnet-5-5")
        for name, model in (("claude", "claude-opus-5-5"), ("codex", "gpt-5.5"), ("opencode", "openai/gpt-6-sol")):
            store.update(name, model=model)
        if avibe == "chosen":
            store.update("vibey", model="chosen-model")
        controller = Controller.__new__(Controller)
        controller.vibe_agent_store = store
        controller.model_hub_service = {
            "empty list": _hub_catalog(),
            "user-built list": _hub_catalog("my-relay-model", "gpt-5.5", origin="manual"),
        }.get(avibe, _hub_catalog("gpt-5.5", "claude-opus-5-5"))

        assert controller._builtin_agent_models() == [
            ("claude", "claude-opus-5-5"), ("codex", "gpt-5.5"), ("opencode", "openai/gpt-6-sol"),
        ]
        controller._reconcile_avibe_agent_model()
        assert (store.get("vibey").backend, store.get("vibey").model) == {
            "seeded": ("avibe", "gpt-5.5"),
            "user-built list": ("avibe", "my-relay-model"),
            "chosen": ("avibe", "chosen-model"),
            "name taken": ("claude", "claude-haiku-4-5"),
            "empty list": ("avibe", None),
        }[avibe]
    finally:
        store.close()


def test_a_lost_avibe_model_hand_off_heals_on_the_next_start(tmp_path, sqlite_db_factory, monkeypatch):
    """MH-AVIBE-007: a failed Agent write after a committed seed heals at the next start."""
    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        store.ensure_builtin_default_agents(["avibe"])
        controller = Controller.__new__(Controller)
        controller.vibe_agent_store = store
        hub = _hub_catalog("gpt-5.5")
        controller.model_hub_service = SimpleNamespace(store=hub.store, seed_avibe_supply=AsyncMock(return_value=[]))
        real_update = store.update

        def locked_database(*args, **kwargs):
            raise OperationalError("UPDATE agents", {}, Exception("database is locked"))

        # The seed's catalog announcement: its reconciliation fails, the supply stays.
        monkeypatch.setattr(store, "update", locked_database)
        asyncio.run(controller._model_hub_catalog_changed("avibe"))
        assert store.get("vibey").model is None

        monkeypatch.setattr(store, "update", real_update)
        asyncio.run(controller._seed_avibe_model_supply())
        assert store.get("vibey").model == "gpt-5.5"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_first_model_a_user_adds_makes_the_avibe_agent_runnable(tmp_path, sqlite_db_factory):
    """MH-AVIBE-007: any Avibe catalog change reconciles the Agent, not only a seed.

    A seed that found no starting model leaves the catalog empty; the model the
    user then adds by hand reaches the Agent without a restart, through the
    same catalog-change notification every catalog edit sends.
    """
    from tests.test_model_hub_l3 import _canonicalize_fixed_test_routes, _service, _source

    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        store.ensure_builtin_default_agents(["avibe"])
        service = _service(tmp_path, sources=[_source("src_relay0001", "Relay", protocol="openai_chat", model_id="relay-model")])
        _canonicalize_fixed_test_routes(service)
        controller = Controller.__new__(Controller)
        controller.vibe_agent_store = store
        controller.model_hub_service = service
        service.backend_catalog_changed = controller._model_hub_catalog_changed

        assert await service.seed_avibe_supply() == []
        assert store.get("vibey").model is None
        candidate, = service.agent_model_candidates("avibe")["providers"]
        await service.set_agent_models("avibe", [], [{key: value for key, value in candidate.items() if key != "suppliers"}])
        assert store.get("vibey").model == "relay-model"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_starting_model_needs_a_source_that_lists_it(tmp_path, sqlite_db_factory):
    """MH-AVIBE-007: a Source's inventory, not a route, decides a starting model.

    An Anthropic key could pass any model id through, but it lists only Claude
    models: the Codex Agent's ``gpt-6-astra`` is no starting model for Avibe,
    and the Avibe Agent stays without a model rather than routing it there.
    """
    from config.v2_config import ModelHubConfig
    from tests.test_model_hub_l3 import _service, _source

    store = VibeAgentStore(sqlite_db_factory(tmp_path / "agents.sqlite"))
    try:
        store.ensure_builtin_default_agents(["codex", "avibe"])
        store.update("codex", model="gpt-6-astra")
        service = _service(tmp_path, sources=[])
        service.store.config = ModelHubConfig()
        controller = Controller.__new__(Controller)
        controller.vibe_agent_store = store
        controller.model_hub_service = service
        service.builtin_agent_models_override = controller._builtin_agent_models
        service.backend_catalog_changed = controller._model_hub_catalog_changed
        anthropic = _source(
            "src_anthropic01", "Anthropic key", vendor="anthropic", protocol="anthropic", model_id="claude-opus-5-5",
        )

        async with service._mutation_lock:
            await service._commit_new_source_locked(anthropic)

        avibe = service.store.load().agents["avibe"]
        assert (avibe.sources.order, avibe.models) == ([anthropic.id], [])
        assert store.get("vibey").model is None
    finally:
        store.close()
