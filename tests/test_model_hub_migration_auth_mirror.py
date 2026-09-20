"""Native takeover's persisted authentication must reach every live consumer.

TestTakeoverMirrorConsumers uses the real migration transaction, not fixture
insertions of the mirror callback. It is intentionally red on the lane's base
until the separately owned migration.py call sites are integrated.
"""

from __future__ import annotations

import asyncio
import copy
import subprocess
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.agent_auth_service import AgentAuthService
from core.backend_restart import NativeMigrationBlockedError
from core.handlers.model_hub.adapter import EngineEnsureResult, EngineHealth, EngineStatus
from core.handlers.model_hub.service import V2ModelHubConfigStore
from modules.agents.claude_agent import ClaudeAgent
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
)
from tests.test_native_takeover_lifecycle import controller_fixture
from vibe import native_oauth_store
from vibe.claude_config import build_claude_subprocess_env


def _forbidden(*args, **kwargs):
    raise AssertionError("native process, HTTP, or broad runtime refresh is forbidden")


@pytest.fixture(autouse=True)
def _fixture_boundaries(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess.Popen, "__init__", _forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbidden)
    monkeypatch.setattr(psutil, "process_iter", _forbidden)
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", native_oauth_store._FixtureKeychainStore())
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")


def _config():
    config = V2Config.default()
    for backend in ("claude", "codex"):
        native = getattr(config.agents, backend)
        native.enabled = True
        native.auth_mode = "api_key"
        native.api_key = f"fixture-{backend}-旧-key"
        native.base_url = "https://fixture-relay.example"
        config.model_hub.agents[backend].mode = "direct"
    config.agents.claude.auth_mode_set = True
    config.agents.codex.oauth_relay_marker = {"base_url": "https://fixture-relay.example"}
    return config


@pytest.fixture
def persisted_config(monkeypatch, tmp_path):
    _isolate_native_home(monkeypatch, tmp_path / "native")
    monkeypatch.setattr(paths, "get_config_path", lambda: tmp_path / "config.json")
    config = _config()
    config.save()
    return config


def _snapshot(config):
    return {
        backend: {name: copy.deepcopy(getattr(getattr(config.agents, backend), name)) for name in names}
        for backend, names in V2ModelHubConfigStore._NATIVE_AUTH_FIELDS.items()
    }


def _cleared(config):
    clean = copy.deepcopy(config)
    for backend in ("claude", "codex"):
        target = getattr(clean.agents, backend)
        target.auth_mode = "oauth"
        target.api_key = target.base_url = None
    clean.agents.claude.auth_mode_set = True
    clean.agents.codex.oauth_relay_marker = None
    return _snapshot(clean)


def _runtime(config, *, raw=False):
    controller, coordinator, admissions, turns = controller_fixture()
    controller.config = copy.deepcopy(config) if raw else to_app_config(config)
    configs = [controller.config]
    for name in ("command_handler", "settings_handler", "message_handler", "session_handler"):
        detached = copy.deepcopy(controller.config)
        setattr(controller, name, SimpleNamespace(config=detached))
        configs.append(detached)
    claude_config = config.agents.claude if raw else controller.config.claude
    controller.claude_client = SimpleNamespace(config=copy.deepcopy(claude_config))
    for backend, agent in controller.agent_service.agents.items():
        agent.config = copy.deepcopy(controller.config)
        configs.append(agent.config)
        agent.refresh_runtime_config = _forbidden
        agent.refresh_auth_state = _forbidden
        agent._get_server = _forbidden
        if backend == "claude":
            # Exercise real strict retirement; the fixture owns no SDK process.
            agent.session_handler = controller.session_handler
            agent.claude_sessions = {}
            agent.claude_client = SimpleNamespace(config=copy.deepcopy(claude_config))
            agent.retire_for_native_migration = lambda agent=agent: ClaudeAgent.retire_for_native_migration(agent)
        if backend == "codex":
            agent.codex_config = copy.deepcopy(config.agents.codex if raw else controller.config.codex)
    controller.agent_service.is_backend_ready = lambda backend: backend not in admissions
    controller.session_turns._draining_backends = turns
    controller.agent_service.refresh_runtime_config = _forbidden
    controller.agent_service.register = _forbidden
    controller.agent_service.release_runtime_turn_tokens = _forbidden
    owner = AgentAuthService(controller)
    owner._apply_backend_runtime_refresh = _forbidden
    owner._refresh_backend_runtime = _forbidden
    owner._refresh_opencode_server = _forbidden
    controller.agent_auth_service = owner
    return SimpleNamespace(
        controller=controller, coordinator=coordinator, owner=owner,
        admissions=admissions, turns=turns, configs=configs,
    )


def _targets(runtime, backend):
    targets = []
    for config in runtime.configs:
        target = getattr(config, backend, None)
        if target is not None:
            targets.append(target)
        target = getattr(getattr(config, "agents", None), backend, None)
        if target is not None:
            targets.append(target)
    agent = runtime.controller.agent_service.agents[backend]
    if backend == "claude":
        targets.extend((runtime.controller.claude_client.config, agent.claude_client.config))
    if backend == "codex":
        targets.append(agent.codex_config)
    return targets


def _assert_mirrored(runtime, snapshot):
    for backend, values in snapshot.items():
        for target in _targets(runtime, backend):
            for name, value in values.items():
                if hasattr(target, name):
                    assert getattr(target, name) == value


@pytest.mark.parametrize("raw", [False, True])
def test_mapper_updates_all_existing_aliases_without_runtime_or_disk_io(monkeypatch, raw):
    config = _config()
    runtime = _runtime(config, raw=raw)
    snapshot = _cleared(config)
    targets = [(backend, target, copy.deepcopy(vars(target))) for backend in snapshot for target in _targets(runtime, backend)]
    aliases = [id(target) for _, target, _ in targets]
    monkeypatch.setattr(V2Config, "load", _forbidden)
    monkeypatch.setattr(V2Config, "save", _forbidden)
    runtime.owner.reconcile_native_auth_snapshot(snapshot)
    _assert_mirrored(runtime, snapshot)
    assert [id(target) for backend in snapshot for target in _targets(runtime, backend)] == aliases
    for backend, target, before in targets:
        after = vars(target)
        assert set(after) == set(before)
        assert {key: value for key, value in after.items() if key not in snapshot[backend]} == {
            key: value for key, value in before.items() if key not in snapshot[backend]
        }
    assert not runtime.admissions and not runtime.turns


def test_mapper_retained_native_values_and_rollback_follow_snapshot_not_hub_mode():
    config = _config()
    for agent in config.model_hub.agents.values():
        agent.mode = "hub"
    config.model_hub.enabled = False
    runtime = _runtime(config, raw=True)
    original = _snapshot(config)
    runtime.owner.reconcile_native_auth_snapshot(_cleared(config))
    runtime.owner.reconcile_native_auth_snapshot(original)
    _assert_mirrored(runtime, original)
    original["codex"]["oauth_relay_marker"]["base_url"] = "https://changed-fixture.example"
    assert runtime.controller.config.agents.codex.oauth_relay_marker["base_url"] == "https://fixture-relay.example"
    assert runtime.controller.config.model_hub.enabled is False
    assert runtime.controller.config.model_hub.agents["claude"].mode == "hub"


@pytest.mark.parametrize("invalid", [
    {"opencode": {"api_key": "fixture-secret"}},
    {"claude": {"enabled": False}},
    {"claude": []},
])
def test_mapper_rejects_non_auth_fields_before_mutating(invalid):
    config = _config()
    runtime = _runtime(config)
    snapshot = _cleared(config)
    snapshot.update(invalid)
    with pytest.raises(ValueError) as error:
        runtime.owner.reconcile_native_auth_snapshot(snapshot)
    assert "fixture-secret" not in str(error.value)
    _assert_mirrored(runtime, _snapshot(config))


@pytest.mark.parametrize("frozen", [False, True])
def test_mapper_preflights_every_target_before_changing_any_alias(frozen):
    config = _config()
    runtime = _runtime(config)
    if frozen:
        @dataclass(frozen=True)
        class Unwritable:
            auth_mode: str = "api_key"
            api_key: str = "fixture-secret"
            base_url: str = "https://fixture-relay.example"
            auth_mode_set: bool = True
        target = Unwritable()
    else:
        class Unwritable:
            def __init__(self):
                self.auth_mode = "api_key"
                self.base_url = "https://fixture-relay.example"
                self.auth_mode_set = True

            @property
            def api_key(self):
                return "fixture-secret"
        target = Unwritable()
    runtime.controller.claude_client.config = target
    with pytest.raises(ValueError) as error:
        runtime.owner.reconcile_native_auth_snapshot(_cleared(config))
    assert "fixture-secret" not in str(error.value)
    assert target.auth_mode == "api_key"
    assert runtime.controller.config.claude.api_key == config.agents.claude.api_key
    assert runtime.controller.agent_service.agents["codex"].codex_config.auth_mode == "api_key"


def test_service_reads_current_store_snapshot_and_allows_standalone_fixture(monkeypatch, tmp_path):
    path = tmp_path / "fixture-config.json"
    monkeypatch.setattr(paths, "get_config_path", lambda: path)
    config = _config()
    config.save(config_path=path)
    service, _, _ = _service(tmp_path)
    service._reconcile_native_auth(("claude",))
    service.store = V2ModelHubConfigStore()
    observed = []
    service.migration_reconcile_auth = observed.append
    service._reconcile_native_auth(("claude",))
    config.agents.claude.api_key = "fixture-current-key"
    config.save(config_path=path)
    service._reconcile_native_auth(("claude",))
    assert observed == [{"claude": _snapshot(_config())["claude"]}, {"claude": _snapshot(config)["claude"]}]
    service.migration_reconcile_auth = Mock(side_effect=RuntimeError("fixture mirror failed"))
    with pytest.raises(RuntimeError, match="fixture mirror failed"):
        service._reconcile_native_auth(("claude",))


@pytest.mark.asyncio
async def test_coordinator_rejects_unowned_wrong_backend_and_cross_task_calls(persisted_config):
    config = persisted_config
    runtime = _runtime(config)
    coordinator = runtime.coordinator
    snapshot = {"claude": _cleared(config)["claude"]}
    for value in (snapshot, {}):
        with pytest.raises(NativeMigrationBlockedError):
            coordinator.reconcile_migration_auth(value)
    async with coordinator.migration_guard(("claude",)):
        with pytest.raises(NativeMigrationBlockedError):
            coordinator.reconcile_migration_auth({"codex": _cleared(config)["codex"]})

        async def unrelated_task():
            with pytest.raises(NativeMigrationBlockedError):
                coordinator.reconcile_migration_auth(snapshot)
        await asyncio.create_task(unrelated_task())
        coordinator.reconcile_migration_auth(snapshot)
        _assert_mirrored(runtime, snapshot)
        assert runtime.admissions == runtime.turns == {"claude"}
    with pytest.raises(NativeMigrationBlockedError):
        coordinator.reconcile_migration_auth(snapshot)
    assert not runtime.admissions and not runtime.turns


@pytest.mark.asyncio
@pytest.mark.parametrize("opened", ["runtime", "turns"])
async def test_coordinator_refuses_a_guard_with_an_opened_admission(opened, persisted_config):
    config = persisted_config
    runtime = _runtime(config)
    async with runtime.coordinator.migration_guard(("claude",)):
        gate = runtime.admissions if opened == "runtime" else runtime.turns
        gate.discard("claude")
        try:
            with pytest.raises(NativeMigrationBlockedError):
                runtime.coordinator.reconcile_migration_auth({"claude": _cleared(config)["claude"]})
            _assert_mirrored(runtime, _snapshot(config))
        finally:
            gate.add("claude")


def test_coordinator_refuses_calls_without_an_event_loop():
    runtime = _runtime(_config())
    with pytest.raises(NativeMigrationBlockedError):
        runtime.coordinator.reconcile_migration_auth({})


@pytest.mark.asyncio
async def test_coordinator_rechecks_the_existing_native_lease(persisted_config):
    config = persisted_config
    runtime = _runtime(config)
    async with runtime.coordinator.migration_guard(("claude",)):
        _, lease = runtime.coordinator._migration_auth_owners["claude"]
        lease.release()
        with pytest.raises(NativeMigrationBlockedError):
            runtime.coordinator.reconcile_migration_auth({"claude": _cleared(config)["claude"]})
        _assert_mirrored(runtime, _snapshot(config))


@pytest.mark.asyncio
async def test_owned_snapshot_seam_updates_v2_consumers_before_queue_admission(monkeypatch, tmp_path):
    """Unit-level seam exercise, not a substitute for the transaction consumer."""
    service, _, runtime, _ = _migration_fixture(monkeypatch, tmp_path)
    config = V2Config.load()
    config.agents.claude.auth_mode = "oauth"
    config.agents.claude.api_key = config.agents.claude.base_url = None
    config.save()
    expected = service.store.native_auth_snapshot(("claude",))
    resumed = []

    async def resume(backend, **kwargs):
        _assert_mirrored(runtime, expected)
        assert backend not in runtime.admissions
        runtime.turns.discard(backend)
        resumed.append(backend)
    runtime.controller.session_turns.end_backend_drain.side_effect = resume
    async with runtime.coordinator.migration_guard(("claude",)):
        service._reconcile_native_auth(("claude",))
        _assert_mirrored(runtime, expected)
        assert runtime.admissions == runtime.turns == {"claude"}
    assert resumed == ["claude"]


@pytest.mark.asyncio
async def test_coordinator_releases_reconciliation_ownership_on_cancellation(persisted_config):
    runtime = _runtime(persisted_config)
    reconcile = runtime.coordinator.reconcile_migration_auth
    entered = asyncio.Event()

    async def work():
        async with runtime.coordinator.migration_guard(("opencode",)):
            reconcile({})
            entered.set()
            await asyncio.Future()
    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(NativeMigrationBlockedError):
        runtime.coordinator.reconcile_migration_auth({})
    assert not runtime.admissions and not runtime.turns


def _migration_fixture(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    path = tmp_path / "fixture-config.json"
    monkeypatch.setattr(paths, "get_config_path", lambda: path)
    config = _config()
    # Use a supported API-key target; pure mapper cases still cover relays.
    config.agents.claude.base_url = "https://api.anthropic.com/fixture-path"
    config.save(config_path=path)
    service, _, adapter = _service(tmp_path, migration_home=home)
    service.store = V2ModelHubConfigStore()
    runtime = _runtime(V2Config.load())
    runtime.controller.model_hub_service = service
    service.migration_guard = runtime.coordinator.migration_guard
    service.migration_reconcile_auth = runtime.coordinator.reconcile_migration_auth
    healthy = EngineStatus(EngineHealth.OK, "fixture", True, "127.0.0.1", 32199, None)

    async def install(**kwargs):
        return EngineEnsureResult(healthy, False)
    adapter.ensure_installed = install
    return service, adapter, runtime, home


class TestTakeoverMirrorConsumers:
    """Durable consuming coverage; requires the main-owned migration call sites."""

    def test_success_then_direct_never_resurrects_legacy_key(self, monkeypatch, tmp_path):
        service, _, runtime, _ = _migration_fixture(monkeypatch, tmp_path)
        save = service.migration_journal.save

        def save_checked(record):
            if record["phase"] == "exposed":
                _assert_mirrored(runtime, record["native_after"])
            save(record)
        monkeypatch.setattr(service.migration_journal, "save", save_checked)
        ids = [item["id"] for item in service.migration_scan()["items"] if item["backend"] == "claude"]
        assert asyncio.run(service.migration_apply(ids))["applied"] == 1
        assert service.migration_journal.load() is None
        assert not runtime.admissions and not runtime.turns
        _assert_mirrored(runtime, service.store.native_auth_snapshot(("claude",)))
        asyncio.run(service.set_agent_mode("claude", "direct"))
        env = build_claude_subprocess_env(runtime.controller.session_handler.config.claude, base_env={})
        assert not env.get("ANTHROPIC_API_KEY") and not env.get("ANTHROPIC_BASE_URL")
        assert V2Config.load().agents.claude.api_key is None
