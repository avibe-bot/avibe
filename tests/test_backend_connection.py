"""Readiness observes persisted launch auth and the existing lifecycle owner."""
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config.v2_config import (
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    V2Config,
)
from vibe import api, internal_client, runtime, ui_server

_REAL_GET_CLAUDE_AUTH = api.get_claude_auth
_REAL_CLAUDE_STATUS_PROBE = api._read_claude_cli_oauth_signed_in


@pytest.fixture(autouse=True)
def native_safety(monkeypatch, _isolate_vibe_remote_home):
    import subprocess

    import psutil

    from vibe import native_oauth_store

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Connection tests must not inspect or launch real native processes")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess.Popen, "__init__", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(psutil, "process_iter", forbidden)
    # The connection projection deliberately observes inherited launch
    # credentials. Keep the fixture independent from the coding agent's own
    # process environment so direct native-store cases exercise their stated
    # source and do not accidentally select an ambient API key.
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", native_oauth_store._FixtureKeychainStore())


@pytest.fixture
def connection(monkeypatch, tmp_path):
    config = V2Config.default()
    config.agents.claude.enabled = True
    config.agents.codex.enabled = True
    config.agents.opencode.enabled = True
    for supply in config.model_hub.agents.values():
        supply.mode = "direct"
    config.save()
    monkeypatch.setattr(api, "load_config", lambda: config)
    monkeypatch.setattr(api, "resolve_cli_path", lambda _: str(tmp_path / "助手 cli"))
    monkeypatch.setattr(api, "get_claude_auth", lambda **_: {"ok": True, "active_auth_mode": "api_key"})
    monkeypatch.setattr(api, "get_codex_auth", lambda: {"ok": True, "active_auth_mode": "oauth"})
    monkeypatch.setattr(api, "_backend_apply_receipts", {})
    probe = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "state": "applied"}})
    monkeypatch.setattr(internal_client, "backend_application", probe)
    monkeypatch.setattr(runtime, "service_process_running", lambda: True)
    return SimpleNamespace(config=config, probe=probe)


@pytest.mark.parametrize("state,ready", [("applied", True), ("draining", False), ("failed", False), ("unavailable", False)])
def test_application_is_consumed_instead_of_acceptance(connection, state, ready):
    connection.probe.return_value["body"]["state"] = state
    observed = asyncio.run(api.get_backend_connection("claude"))
    assert observed["ready"] is ready
    assert observed["entry_eligible"] is ready
    assert observed["auth"] == "api_key"
    assert observed["supply_mode"] == "direct"


def test_stopped_entry_is_distinct_from_running_broken_ipc(connection, monkeypatch):
    connection.probe.side_effect = internal_client.InternalServerUnavailable("missing IPC")
    unknown = asyncio.run(api.get_backend_connection("claude"))
    assert unknown["application"] == "unknown"
    assert not unknown["ready"] and not unknown["entry_eligible"]
    monkeypatch.setattr(runtime, "service_process_running", lambda: False)
    stopped = asyncio.run(api.get_backend_connection("claude"))
    assert stopped["application"] == "stopped"
    assert stopped["entry_eligible"] and not stopped["ready"]


def test_marker_failure_survives_new_reads_and_only_explicit_apply_retry_clears(connection, monkeypatch):
    monkeypatch.setattr(api, "_request_controller_restart", lambda *args, **kwargs: (False, None))
    assert not api.restart_backend("claude")["ok"]
    for _ in range(3):
        failed = asyncio.run(api.get_backend_connection("claude"))
        assert failed["application"] == "failed" and not failed["ready"]
    assert asyncio.run(api.get_backend_connection("codex"))["ready"]
    monkeypatch.setattr(api, "_request_controller_restart", lambda *args, **kwargs: (True, None))
    assert api.restart_backend("claude")["ok"]
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]


def test_keychain_uncertainty_and_disabled_backend_are_not_ready(connection, monkeypatch):
    monkeypatch.setattr(api, "get_codex_auth", lambda: {"ok": True, "active_auth_mode": "oauth", "auth_mode_uncertain": True})
    assert asyncio.run(api.get_backend_connection("codex"))["auth"] == "unknown"
    connection.config.agents.claude.enabled = False
    assert not asyncio.run(api.get_backend_connection("claude"))["ready"]


def test_opencode_permission_does_not_gate_another_backend_or_launch_daemon(connection, monkeypatch):
    from vibe import opencode_config
    monkeypatch.setattr(api, "_read_opencode_config_api_key_provider_ids", AsyncMock(return_value={"provider"}))
    monkeypatch.setattr(opencode_config, "read_opencode_provider_auth_entries", lambda **_: {})
    monkeypatch.setattr(api, "opencode_permission_status", lambda: {"ok": True, "permission_allowed": False})
    daemon = AsyncMock(side_effect=AssertionError("readiness must not launch a daemon"))
    monkeypatch.setattr(api, "_opencode_get_server", daemon)
    state = asyncio.run(api.get_backend_connection("opencode"))
    assert state["auth"] == "api_key" and state["permission_required"] and not state["entry_eligible"]
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]
    daemon.assert_not_called()


def test_native_asgi_connection_route_auth_allowlist_and_body(connection):
    client = ui_server.app.test_client()
    response = client.get("/api/backend/claude/connection")
    assert response.status_code == 200
    assert response.get_json()["ready"] is True
    assert response.headers["Cache-Control"] == "private, no-store"
    assert client.get("/api/backend/other/connection").status_code == 400
    remote = client.get("/api/backend/claude/connection", environ_base={"REMOTE_ADDR": "203.0.113.44"})
    assert remote.status_code in {401, 403, 503}
    assert not remote.get_json().get("ready")


def test_only_confirmed_new_controller_clears_old_failed_delivery(connection, monkeypatch):
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda **_: 100)
    api.record_backend_apply_receipt("claude", {"ok": False, "message": "marker not delivered"})
    connection.probe.return_value["body"]["controller_pid"] = 100
    assert not asyncio.run(api.get_backend_connection("claude"))["ready"]
    connection.probe.return_value["body"]["controller_pid"] = 101
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("application", ["applied", "draining", "failed", "stopped"])
def test_install_job_applies_persisted_path_before_admitting_connection(monkeypatch, tmp_path, backend, application):
    """The real install-job result and readiness consume the same rolling apply."""
    from core.backend_restart import BackendRestartCoordinator
    from vibe import opencode_config

    config = V2Config.default()
    for name in ("claude", "codex", "opencode"):
        getattr(config.agents, name).enabled = True
        config.model_hub.agents[name].mode = "direct"
    getattr(config.agents, backend).cli_path = "/old/missing-cli"
    config.save()
    installed_path = str(tmp_path / "安装 后端/bin" / backend)
    monkeypatch.setattr(api, "resolve_cli_path", lambda _: installed_path)
    monkeypatch.setattr(api.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        communicate=lambda **_: ("fixture installed", ""), returncode=0,
    ))
    monkeypatch.setattr(api, "install_agent", lambda name: api._run_install_command(name, ["fixture-installer"], lambda value: value))
    monkeypatch.setattr(api, "get_claude_auth", lambda **_: {"ok": True, "active_auth_mode": "api_key"})
    monkeypatch.setattr(api, "get_codex_auth", lambda: {"ok": True, "active_auth_mode": "api_key"})
    monkeypatch.setattr(api, "_read_opencode_config_api_key_provider_ids", AsyncMock(return_value={"fixture"}))
    monkeypatch.setattr(opencode_config, "read_opencode_provider_auth_entries", lambda **_: {})
    monkeypatch.setattr(api, "opencode_permission_status", lambda: {"ok": True, "permission_allowed": True})
    monkeypatch.setattr(runtime, "service_process_running", lambda: application != "stopped")
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda **_: 123 if application != "stopped" else None)
    monkeypatch.setattr(api, "_backend_apply_receipts", {})
    monkeypatch.setattr(api, "_AGENT_INSTALL_JOBS", {})
    monkeypatch.setattr(api, "_AGENT_INSTALL_LATEST_BY_BACKEND", {})

    async def run():
        service = SimpleNamespace(
            agents={name: object() for name in ("claude", "codex", "opencode")},
            active=application == "draining",
            begin_backend_drain=Mock(), end_backend_drain=Mock(),
            prepare_backend_restart=AsyncMock(),
        )
        service.runtime_turn_tokens_for_backend = lambda name: {"fixture": "turn"} if service.active and name == backend else {}
        controller = SimpleNamespace(config=config, agent_service=service, session_turns=SimpleNamespace(
            begin_backend_drain=Mock(), end_backend_drain=AsyncMock(),
        ))
        applied = []

        async def refresh(name, _forced):
            applied.append(name)
            if application == "failed":
                raise RuntimeError("fixture apply failed")
            controller.config = V2Config.load()

        coordinator = BackendRestartCoordinator(controller, refresh, poll_interval=0.001)
        loop = asyncio.get_running_loop()

        def marker(name, **_kwargs):
            assert getattr(V2Config.load().agents, name).cli_path == installed_path
            try:
                asyncio.run_coroutine_threadsafe(coordinator.request_restart(name), loop).result(2)
                return True, None
            except Exception as exc:
                return True, str(exc)

        async def projection(name):
            if application == "stopped":
                raise internal_client.InternalServerUnavailable("fixture stopped")
            return {"status_code": 200, "body": {"ok": True, "controller_pid": 123, **coordinator.snapshot(name)}}

        monkeypatch.setattr(api, "_request_controller_restart", marker)
        monkeypatch.setattr(internal_client, "backend_application", projection)
        started = api.start_agent_install_job(backend)
        async def finished_job():
            while (job := api.get_agent_install_job(started["job_id"]))["status"] == "running":
                await asyncio.sleep(0.001)
            return job
        job = await asyncio.wait_for(finished_job(), 3)
        assert job["path"] == installed_path
        assert getattr(V2Config.load().agents, backend).cli_path == installed_path
        state = await api.get_backend_connection(backend)
        assert state["application"] == application, (job, state)
        assert state["ready"] is (application == "applied")
        assert state["entry_eligible"] is (application in {"applied", "stopped"})
        assert job["ok"] is (application != "failed")
        if application == "stopped":
            assert job["restart"]["apply_on_next_start"] and not applied
        elif application == "draining":
            assert getattr(controller.config.agents, backend).cli_path == "/old/missing-cli"
            service.active = False
            await coordinator.wait(backend)
            assert (await api.get_backend_connection(backend))["ready"]
            assert getattr(controller.config.agents, backend).cli_path == installed_path
            assert applied == [backend]
        elif application == "applied":
            assert getattr(controller.config.agents, backend).cli_path == installed_path
            assert applied == [backend]
        else:
            assert getattr(controller.config.agents, backend).cli_path == "/old/missing-cli"
            other = "codex" if backend != "codex" else "claude"
            assert (await api.get_backend_connection(other))["ready"]

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_disabled_applied_configuration_stays_saved_without_readiness(monkeypatch, backend):
    from config.v2_compat import to_app_config
    from core.agent_auth_service import AgentAuthService
    from core.backend_restart import BackendRestartCoordinator
    from modules.agents.claude_agent import ClaudeAgent

    config = V2Config.default()
    for name in ("claude", "codex", "opencode"):
        getattr(config.agents, name).enabled = name == backend
        config.model_hub.agents[name].mode = "direct"
    config.save()
    monkeypatch.setattr(api, "resolve_cli_path", lambda _: "/fixture/bin/assistant")
    monkeypatch.setattr(api, "_read_claude_cli_oauth_signed_in", lambda *a, **kw: False)
    monkeypatch.setattr(api, "_get_oauth_service", lambda: SimpleNamespace(
        _recover_interrupted_claude_oauth_settings_backup=Mock(),
    ))
    monkeypatch.setattr(api, "_clear_claude_oauth_credentials_after_api_key_save", lambda *_, **kw: {"ok": True})
    monkeypatch.setattr(api, "_refresh_opencode_provider_catalog_async", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(api, "_backend_apply_receipts", {})
    monkeypatch.setattr(runtime, "service_process_running", lambda: True)
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda **_: 123)

    async def run():
        service = SimpleNamespace(
            agents={}, active=False, begin_backend_drain=Mock(), end_backend_drain=Mock(),
            prepare_backend_restart=AsyncMock(), runtime_turn_tokens_for_backend=lambda _: {},
        )
        controller = SimpleNamespace(config=to_app_config(config), agent_service=service,
            session_turns=SimpleNamespace(begin_backend_drain=Mock(), end_backend_drain=AsyncMock()))
        # Claude's loaded compat object remains registered even when disabled.
        claude = SimpleNamespace(config=controller.config, controller=controller, refresh_auth_state=AsyncMock())
        claude.refresh_runtime_config = lambda value: ClaudeAgent.refresh_runtime_config(claude, value)
        service.agents["claude"] = claude
        if backend != "claude":
            service.agents[backend] = SimpleNamespace(shutdown_runtime=AsyncMock())
        async def refresh(name, value):
            if name == "claude":
                await claude.refresh_runtime_config(value)
                return True
            return False
        service.refresh_runtime_config = refresh
        owner = AgentAuthService(controller)
        coordinator = BackendRestartCoordinator(controller, owner._apply_backend_runtime_refresh)
        async def projection(name):
            return {"status_code": 200, "body": {"ok": True, **coordinator.snapshot(name)}}
        monkeypatch.setattr(internal_client, "backend_application", projection)
        # Persisted disable alone must not reinterpret an unexpectedly missing
        # enabled registration as applied (a stale successful outcome cannot help).
        if backend != "claude":
            agent = service.agents.pop(backend)
            assert coordinator.snapshot(backend)["state"] == "unavailable"
            service.agents[backend] = agent
        getattr(config.agents, backend).enabled = False
        config.save()
        await coordinator.request_restart(backend)
        loop = asyncio.get_running_loop()
        marker_calls = []
        def marker(name, **_kwargs):
            marker_calls.append(name)
            asyncio.run_coroutine_threadsafe(coordinator.request_restart(name), loop).result(2)
            return True, None
        monkeypatch.setattr(api, "_request_controller_restart", marker)
        payload = {"auth_mode": "api_key", "api_key": "fixture-saved-key"}
        if backend == "opencode":
            result = await asyncio.to_thread(lambda: asyncio.run(api.save_opencode_provider_auth_async("fixture", payload)))
            assert await api._read_opencode_config_api_key("fixture") == "fixture-saved-key"
        else:
            save = api.save_claude_auth if backend == "claude" else api.save_codex_auth
            result = await asyncio.to_thread(save, payload)
            assert result["active_auth_mode"] == "api_key"
        assert result["ok"] and result["restart"]["ok"]
        assert marker_calls == [backend]
        assert getattr(V2Config.load().agents, backend).enabled is False
        for _ in range(2):
            state = await api.get_backend_connection(backend)
            assert state["application"] == "applied"
            assert not state["enabled"] and not state["ready"] and not state["entry_eligible"]
        # Startup from the same loaded disabled shape is also authoritative.
        startup = BackendRestartCoordinator(controller, owner._apply_backend_runtime_refresh)
        assert startup.snapshot(backend) == {"state": "applied", "disabled": True}
        # A concurrent disk enable cannot borrow the disabled controller's
        # applied state before that enablement is reconciled.
        getattr(config.agents, backend).enabled = True
        config.save()
        stale = await api.get_backend_connection(backend)
        assert stale["application"] == "unknown" and not stale["ready"] and not stale["entry_eligible"]
        getattr(config.agents, backend).enabled = False
        config.save()
        # A failed subsequent prepare retains the failure even when disabled.
        service.prepare_backend_restart.side_effect = RuntimeError("fixture failure")
        with pytest.raises(RuntimeError, match="fixture failure"):
            await coordinator.request_restart(backend)
        assert (await api.get_backend_connection(backend))["application"] == "failed"
        monkeypatch.setattr(internal_client, "backend_application", AsyncMock(side_effect=internal_client.InternalServerUnavailable()))
        unknown = await api.get_backend_connection(backend)
        assert unknown["application"] == "unknown" and not unknown["ready"]

    asyncio.run(run())


@pytest.mark.parametrize("contents,required", [
    ('{ malformed', False), ('{}', True), ('{"permission":"ask"}', True),
    ('{"permission":"deny"}', True), ('{"permission":"allow"}', False),
    ('{"permission":{"*":"allow"}}', False),
])
def test_permission_read_policy_consumes_real_file_without_overwriting(connection, monkeypatch, contents, required):
    from vibe.opencode_config import get_opencode_config_paths, get_opencode_auth_path

    config_path = get_opencode_config_paths(api.Path.home())[0]
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(contents)
    auth_path = get_opencode_auth_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text('{"fixture":{"type":"api","key":"fixture-only-key"}}')
    monkeypatch.setattr(api, "_read_opencode_config_api_key_provider_ids", AsyncMock(return_value=set()))
    result = asyncio.run(api.get_backend_connection("opencode"))
    assert result["permission_required"] is required
    assert result["ready"] is (not required)
    assert result["auth"] == "api_key"
    if contents == '{ malformed':
        assert api.setup_opencode_permission()["ok"] is False
    assert config_path.read_text() == contents
    connection.probe.return_value["body"]["state"] = "failed"
    assert not asyncio.run(api.get_backend_connection("opencode"))["ready"]
    connection.probe.return_value["body"]["state"] = "applied"
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]


@pytest.fixture
def hub_connection(connection, monkeypatch):
    """Persist Hub custody; any unconfigured native credential read is a bug."""
    for supply in connection.config.model_hub.agents.values():
        supply.mode = "hub"
    connection.config.save()
    monkeypatch.setattr(api, "load_config", V2Config.load)

    def native_read(*_args, **_kwargs):
        raise AssertionError("Hub-owned supply must not read dormant native credentials")

    connection.native_read = Mock(side_effect=native_read)
    monkeypatch.setattr(api, "get_claude_auth", connection.native_read)
    monkeypatch.setattr(api, "get_codex_auth", connection.native_read)
    monkeypatch.setattr(api, "_read_opencode_config_api_key_provider_ids", AsyncMock(side_effect=native_read))
    monkeypatch.setattr(api, "opencode_permission_status", lambda: {"ok": True, "permission_allowed": True})
    return connection


def _hub_source(*, kind="api_key", source_id="src_takeover01", channel="hub", vendor="openai"):
    return ModelHubSourceConfig(
        id=source_id, kind=kind, vendor=vendor,
        display_name="Migrated account", protocol="openai_responses" if vendor == "openai" else "anthropic",
        supply_channel=channel, billing="metered" if kind == "api_key" else "monthly",
        state=ModelHubSourceStateConfig(status="standby"), models=[],
        credential_ref="cred_current01" if channel == "hub" else None,
        account_label="private-account@example.test",
    )


def _place_hub_source(connection, backend, source):
    if source.supply_channel == "hub":
        _store_hub_credential(source)
    connection.config.model_hub.sources.append(source)
    connection.config.model_hub.agents[backend].sources.order.append(source.id)
    connection.config.save()


def _store_hub_credential(source):
    from config import paths
    from vibe.model_hub_runtime.state import EngineStateStore

    store = EngineStateStore(paths.get_runtime_dir() / "model-hub" / "state")
    if source.kind == "api_key":
        source.credential_ref = store.store_api_key(
            "fixture-secret-not-for-response", vendor=source.vendor, protocol=source.protocol, base_url=source.base_url,
        )
    else:
        source.credential_ref = store.bind_oauth_credential(source.id, source.vendor, f"{source.id}.json")
    return store


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("kind", ["api_key", "subscription"])
def test_hub_takeover_connection_uses_current_source_not_cleared_native_store(hub_connection, backend, kind):
    source = _hub_source(kind=kind)
    source.verification_pending = "vp_" + "a" * 32
    _place_hub_source(hub_connection, backend, source)
    assert source.models == []  # Connection observation is not model entitlement.

    state = asyncio.run(api.get_backend_connection(backend))
    assert state["supply_mode"] == "hub"
    assert state["auth"] == ("api_key" if kind == "api_key" else "subscription")
    assert state["ready"] and state["entry_eligible"]
    assert state["application"] == "applied"
    assert source.credential_ref not in json.dumps(state)
    assert source.account_label not in json.dumps(state)
    hub_connection.native_read.assert_not_called()

    # Retained source identity survives a credential rotation; readers must not
    # cache the prior ref or attempt to recover it from native residue.
    old_ref = source.credential_ref
    source.credential_ref = "cred_missing02"
    hub_connection.config.save()
    missing = asyncio.run(api.get_backend_connection(backend))
    assert missing["auth"] == "none" and not missing["entry_eligible"]
    store = _store_hub_credential(source)
    if kind == "subscription":
        # bind_oauth_credential deliberately reuses one auth-name identity.
        source.credential_ref = store.bind_oauth_credential(source.id, source.vendor, "rotated.json")
    assert source.credential_ref != old_ref
    hub_connection.config.save()
    assert asyncio.run(api.get_backend_connection(backend))["ready"]


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("condition", ["missing", "missing_credential", "unselected", "runtime_disabled", "needs_action", "error"])
def test_unusable_hub_supply_never_borrows_unrelated_native_auth(hub_connection, backend, condition):
    source = _hub_source()
    if condition != "missing":
        _place_hub_source(hub_connection, backend, source)
    if condition == "unselected":
        hub_connection.config.model_hub.agents[backend].sources.order = []
    elif condition == "runtime_disabled":
        hub_connection.config.model_hub.enabled = False
    elif condition == "missing_credential":
        source.credential_ref = "cred_missing02"
    elif condition == "needs_action":
        source.state = ModelHubSourceStateConfig(
            status="needs_action", detail_key="models.source.needs_action.credential_revoked",
        )
    elif condition == "error":
        source.state = ModelHubSourceStateConfig(status="error", detail_key="models.source.error.unclassified")
    hub_connection.config.save()
    state = asyncio.run(api.get_backend_connection(backend))
    assert state["supply_mode"] == "hub"
    assert state["auth"] == "none"
    assert not state["ready"] and not state["entry_eligible"]
    hub_connection.native_read.assert_not_called()


def test_hub_explicit_route_reference_is_configured_supply_without_model_resolution(hub_connection):
    source = _hub_source()
    _store_hub_credential(source)
    hub_connection.config.model_hub.sources = [source]
    backend_model = hub_connection.config.model_hub.agents["claude"].models[0].id
    hub_connection.config.model_hub.agents["claude"].routes[backend_model] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "unobserved-model"),),
    )
    hub_connection.config.save()
    assert hub_connection.config.model_hub.effective_source_order("claude") == []
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]
    assert not asyncio.run(api.get_backend_connection("codex"))["entry_eligible"]


def test_hub_connection_does_not_create_missing_state_or_accept_staged_grant(hub_connection):
    from config import paths

    source = _hub_source(kind="subscription")
    hub_connection.config.model_hub.sources = [source]
    hub_connection.config.model_hub.agents["claude"].sources.order = [source.id]
    hub_connection.config.save()
    state_root = paths.get_runtime_dir() / "model-hub" / "state"
    assert not state_root.exists()
    assert not asyncio.run(api.get_backend_connection("claude"))["entry_eligible"]
    assert not state_root.exists()

    store = _store_hub_credential(source)
    record = store.root / "credentials" / f"{source.credential_ref}.json"
    payload = json.loads(record.read_text())
    payload["activation_state"] = "staged"
    record.write_text(json.dumps(payload))
    hub_connection.config.save()
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["auth"] == "none" and not state["ready"]
    assert not store.auth_dir.exists()


@pytest.mark.parametrize("expired", [False, True])
def test_hub_cooldown_uses_existing_retry_time_not_model_entitlement(hub_connection, expired):
    source = _hub_source()
    source.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at=(datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)).isoformat(),
        detail_key="models.source.cooldown.rate_limited",
    )
    _place_hub_source(hub_connection, "claude", source)
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["ready"] is expired and state["entry_eligible"] is expired


@pytest.mark.parametrize("backend,vendor", [("claude", "anthropic"), ("codex", "openai")])
@pytest.mark.parametrize("recovered_cooldown", [False, True])
def test_retained_native_subscription_uses_only_its_explicit_owner(
    hub_connection, monkeypatch, backend, vendor, recovered_cooldown,
):
    from modules.agents.model_hub import ModelHubRuntimeRouter

    source = _hub_source(kind="subscription", channel="native_cli", vendor=vendor)
    source.state.status = "active"
    if recovered_cooldown:
        source.state = ModelHubSourceStateConfig(
            status="cooldown", retry_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            detail_key="models.source.cooldown.rate_limited",
        )
    _place_hub_source(hub_connection, backend, source)
    native_ready = Mock(return_value=True)
    monkeypatch.setattr(ModelHubRuntimeRouter, "_default_native_cli_ready", native_ready)
    state = asyncio.run(api.get_backend_connection(backend))
    assert state["supply_mode"] == "hub" and state["auth"] == "subscription"
    assert state["ready"]
    native_ready.assert_called_once_with(backend, verified_oauth=backend == "codex")
    hub_connection.native_read.assert_not_called()

    native_ready.reset_mock()
    other = "codex" if backend == "claude" else "claude"
    assert not asyncio.run(api.get_backend_connection(other))["entry_eligible"]
    native_ready.assert_not_called()
    native_ready.return_value = False  # Existing readiness rejects a conflicting key/endpoint.
    assert not asyncio.run(api.get_backend_connection(backend))["entry_eligible"]
    hub_connection.config.model_hub.agents[backend].sources.order = []
    hub_connection.config.save()
    native_ready.reset_mock()
    assert not asyncio.run(api.get_backend_connection(backend))["entry_eligible"]
    native_ready.assert_not_called()


def test_api_key_connection_does_not_need_claude_status_probe(connection, monkeypatch):
    from vibe.claude_config import read_claude_settings_env

    # Exercise the real getter, not the fixture's public-auth stub.
    monkeypatch.setattr(api, "get_claude_auth", _REAL_GET_CLAUDE_AUTH)
    probe = Mock(side_effect=AssertionError("read-only connection launched Claude"))
    monkeypatch.setattr(api, "_read_claude_cli_oauth_signed_in", probe)
    connection.config.agents.claude.auth_mode = "api_key"
    connection.config.agents.claude.api_key = "fixture-legacy-key"
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["auth"] == "api_key" and state["ready"]
    assert "fixture-legacy-key" not in json.dumps(state)
    assert not read_claude_settings_env()
    probe.assert_not_called()


@pytest.fixture
def claude_native_observation(connection, monkeypatch):
    """Exercise the native resolver with an opaque, non-interactive fake store."""
    from vibe import native_oauth_store

    forbidden = Mock(side_effect=AssertionError("readiness must not read or mutate Keychain values"))
    keychain = SimpleNamespace(
        metadata=Mock(return_value=native_oauth_store._KeychainMetadata("not_found", "fixture-absent")),
        read=forbidden, write=forbidden, delete=forbidden,
    )
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    monkeypatch.setattr(native_oauth_store, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(native_oauth_store.getpass, "getuser", lambda: "fixture-user")
    monkeypatch.setattr(api, "get_claude_auth", _REAL_GET_CLAUDE_AUTH)
    probe = Mock(return_value=None)
    keychain.status_probe = probe
    monkeypatch.setattr(api, "_read_claude_cli_oauth_signed_in", probe)
    yield keychain
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"loggedIn": True, "authMethod": "claude.ai"}, "subscription"),
        ({"loggedIn": True, "authMethod": "setup-token"}, "subscription"),
        ({"loggedIn": True, "authMethod": "api-key"}, "none"),
        ({"loggedIn": False}, "none"),
        ({"loggedIn": True}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_direct_claude_keychain_status_restores_readiness_without_secret_read(
    connection, claude_native_observation, monkeypatch, status, expected,
):
    from core.backend_restart import NativeCredentialLease, NativeMigrationBlockedError
    from vibe import native_oauth_store
    from vibe.claude_config import get_claude_credentials_paths

    claude_native_observation.metadata.return_value = native_oauth_store._KeychainMetadata(
        "found", "fixture-revision",
    )
    assert not any(path.exists() for path in get_claude_credentials_paths())

    def run(command, **kwargs):
        assert command[1:] == ["auth", "status", "--json"]
        assert kwargs["timeout"] == 3
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert not kwargs["check"]
        # An independent migration owner cannot enter while status is reading.
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_in_progress"):
            NativeCredentialLease(("claude",)).acquire(recovery=True)
        return subprocess.CompletedProcess(command, 0, json.dumps(status), "")

    process = Mock(side_effect=run)
    monkeypatch.setattr(api, "_read_claude_cli_oauth_signed_in", _REAL_CLAUDE_STATUS_PROBE)
    monkeypatch.setattr(subprocess, "run", process)
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["auth"] == expected
    assert state["ready"] is (expected == "subscription")
    assert state["entry_eligible"] is state["ready"]
    process.assert_called_once()
    if expected != "unknown":
        claude_native_observation.metadata.assert_not_called()
    with NativeCredentialLease(("claude",)):
        pass


def test_direct_claude_status_cancellation_joins_worker_before_releasing_lease(
    connection, claude_native_observation, monkeypatch,
):
    from core.backend_restart import NativeCredentialLease, NativeMigrationBlockedError

    async def scenario():
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def status(*_args, **_kwargs):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3), "fixture did not release the status reader"
            return True

        monkeypatch.setattr(api, "_read_claude_cli_oauth_signed_in", status)
        task = asyncio.create_task(api.get_backend_connection("claude"))
        try:
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                with pytest.raises(NativeMigrationBlockedError, match="native_auth_in_progress"):
                    NativeCredentialLease(("claude",)).acquire(recovery=True)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        with NativeCredentialLease(("claude",)):
            pass

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["hub", "hub_disabled", "pending", "busy", "corrupt_config"])
def test_direct_claude_rechecks_custody_after_async_application_wait(
    connection, claude_native_observation, monkeypatch, change,
):
    from config import paths
    from core.backend_restart import NativeCredentialLease

    lease = None

    async def application(_backend):
        nonlocal lease
        if change in {"hub", "hub_disabled"}:
            persisted = V2Config.load()
            persisted.model_hub.agents["claude"].mode = "hub"
            persisted.model_hub.enabled = change != "hub_disabled"
            persisted.save()
        elif change == "pending":
            journal = paths.get_state_dir() / "native-takeover" / "current.json"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({"version": 1, "phase": "prepared", "backends": ["claude"]}))
        elif change == "corrupt_config":
            paths.get_config_path().write_text("{invalid")
        else:
            lease = NativeCredentialLease(("claude",)).acquire()
        return {"status_code": 200, "body": {"ok": True, "state": "applied"}}

    monkeypatch.setattr(internal_client, "backend_application", application)
    try:
        state = asyncio.run(api.get_backend_connection("claude"))
        assert state["auth"] == "unknown"
        assert not state["ready"] and not state["entry_eligible"]
        claude_native_observation.status_probe.assert_not_called()
        claude_native_observation.metadata.assert_not_called()
    finally:
        if lease is not None:
            lease.release()


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("fixture-cli", 3), OSError("fixture-native-detail")])
def test_direct_claude_status_failure_is_uncertain_and_releases_lease(
    connection, claude_native_observation, monkeypatch, error,
):
    from core.backend_restart import NativeCredentialLease
    from vibe import native_oauth_store

    claude_native_observation.metadata.return_value = native_oauth_store._KeychainMetadata(
        "found", "fixture-revision",
    )
    monkeypatch.setattr(api, "_read_claude_cli_oauth_signed_in", _REAL_CLAUDE_STATUS_PROBE)
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["auth"] == "unknown" and not state["entry_eligible"]
    assert "fixture-" not in json.dumps(state)
    with NativeCredentialLease(("claude",)):
        pass


@pytest.mark.parametrize("payload_kind", ["claude_oauth", "mcp_only"])
def test_claude_keychain_container_is_unknown_not_logged_out_or_proven_oauth(
    connection, claude_native_observation, payload_kind,
):
    from vibe import native_oauth_store

    # Both secret contents have exactly the same safe Keychain metadata. The
    # observer must not inspect either payload or infer Claude OAuth from it.
    claude_native_observation.stored_payload = {
        "claude_oauth": {"claudeAiOauth": {"accessToken": "fixture-oauth-secret"}},
        "mcp_only": {"mcpOAuth": {"provider": {"accessToken": "fixture-mcp-secret"}}},
    }[payload_kind]
    claude_native_observation.metadata.return_value = native_oauth_store._KeychainMetadata(
        "found", "fixture-revision", {"svce": "Claude Code-credentials", "acct": "fixture-user"},
    )
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["auth"] == "unknown"
    assert not state["ready"] and not state["entry_eligible"]
    assert state["application"] == "applied"
    assert state["supply_mode"] == "direct"
    assert "fixture-" not in json.dumps(state)
    claude_native_observation.metadata.assert_called_once()


@pytest.mark.parametrize("metadata_state", ["not_found", "permission_needed", "found"])
@pytest.mark.parametrize("file_oauth", [False, True])
def test_claude_native_observation_respects_selected_store(
    connection, claude_native_observation, metadata_state, file_oauth,
):
    from vibe import native_oauth_store
    from vibe.claude_config import get_claude_credentials_path

    claude_native_observation.metadata.return_value = native_oauth_store._KeychainMetadata(
        metadata_state, "fixture-revision",
    )
    if file_oauth:
        path = get_claude_credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fixture-file-secret"}}))
    state = asyncio.run(api.get_backend_connection("claude"))
    expected_auth = (
        "unknown" if metadata_state != "not_found" else "subscription" if file_oauth else "none"
    )
    assert state["auth"] == expected_auth
    assert state["ready"] is (expected_auth == "subscription")
    assert state["entry_eligible"] is state["ready"]
    assert "fixture-file-secret" not in json.dumps(state)


@pytest.mark.parametrize("key_var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
@pytest.mark.parametrize("source", ["settings", "legacy_env", "explicit_oauth_env"])
def test_claude_connection_observes_launch_key_precedence(
    connection, claude_native_observation, monkeypatch, key_var, source,
):
    from vibe import native_oauth_store
    from vibe.claude_config import get_claude_settings_path

    claude_native_observation.metadata.return_value = native_oauth_store._KeychainMetadata(
        "found", "fixture-revision",
    )
    connection.config.agents.claude.auth_mode = "oauth"
    connection.config.agents.claude.auth_mode_set = source != "legacy_env"
    if source == "settings":
        path = get_claude_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"env": {key_var: "fixture-settings-secret"}}))
    else:
        monkeypatch.setenv(key_var, "fixture-inherited-secret")
    state = asyncio.run(api.get_backend_connection("claude"))
    key_selected = source != "explicit_oauth_env"
    assert state["auth"] == ("api_key" if key_selected else "unknown")
    assert state["ready"] is key_selected
    assert "fixture-" not in json.dumps(state)
    if key_selected:
        claude_native_observation.metadata.assert_not_called()


@pytest.mark.parametrize("gate", ["backend_disabled", "not_installed", "draining", "permission"])
def test_hub_supply_keeps_existing_application_install_and_permission_gates(hub_connection, monkeypatch, gate):
    _place_hub_source(hub_connection, "opencode", _hub_source())
    if gate == "backend_disabled":
        hub_connection.config.agents.opencode.enabled = False
        hub_connection.config.save()
    elif gate == "not_installed":
        monkeypatch.setattr(api, "resolve_cli_path", lambda _: None)
    elif gate == "draining":
        hub_connection.probe.return_value["body"]["state"] = "draining"
    else:
        monkeypatch.setattr(api, "opencode_permission_status", lambda: {"ok": True, "permission_allowed": False})
    state = asyncio.run(api.get_backend_connection("opencode"))
    assert state["supply_mode"] == "hub"
    assert not state["ready"] and not state["entry_eligible"]
    if gate == "permission":
        assert state["permission_required"]


@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize("phase", ["prepared", "withdrawn", "exposed", "reverting", "corrupt"])
def test_pending_takeover_blocks_connection_even_without_controller(hub_connection, monkeypatch, phase, running):
    from config import paths

    for backend in ("claude", "codex"):
        _place_hub_source(hub_connection, backend, _hub_source(source_id=f"src_{backend}0001"))
    if not running:
        hub_connection.probe.side_effect = internal_client.InternalServerUnavailable()
        monkeypatch.setattr(runtime, "service_process_running", lambda: False)
    journal = paths.get_state_dir() / "native-takeover" / "current.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "{broken" if phase == "corrupt"
        else json.dumps({"version": 1, "phase": phase, "backends": ["claude"]}),
    )
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["supply_mode"] == "hub"
    assert not state["ready"] and not state["entry_eligible"]
    sibling = asyncio.run(api.get_backend_connection("codex"))
    assert sibling["entry_eligible"] is (phase != "corrupt")


def test_hub_connection_mode_survives_stopped_runtime_and_missing_native_auth(hub_connection, monkeypatch):
    _place_hub_source(hub_connection, "claude", _hub_source())
    hub_connection.probe.side_effect = internal_client.InternalServerUnavailable()
    monkeypatch.setattr(runtime, "service_process_running", lambda: False)
    state = asyncio.run(api.get_backend_connection("claude"))
    assert state["supply_mode"] == "hub" and state["application"] == "stopped"
    assert state["entry_eligible"] and not state["ready"]
    hub_connection.config.model_hub.enabled = False
    hub_connection.config.save()
    disabled = asyncio.run(api.get_backend_connection("claude"))
    assert disabled["supply_mode"] == "hub"
    assert not disabled["entry_eligible"] and not disabled["ready"]


def test_hub_connection_route_preserves_auth_boundary_and_redacts_owner_data(hub_connection, monkeypatch):
    source = _hub_source(kind="subscription")
    _place_hub_source(hub_connection, "claude", source)
    observe = AsyncMock(wraps=api.get_backend_connection)
    monkeypatch.setattr(api, "get_backend_connection", observe)
    client = ui_server.app.test_client()
    response = client.get("/api/backend/claude/connection")
    assert response.status_code == 200
    assert response.get_json()["supply_mode"] == "hub"
    assert response.get_json()["entry_eligible"]
    assert response.headers["Cache-Control"] == "private, no-store"
    assert source.credential_ref not in response.text and source.account_label not in response.text
    observe.reset_mock()
    denied = client.get("/api/backend/claude/connection", environ_base={"REMOTE_ADDR": "203.0.113.44"})
    assert denied.status_code in {401, 403, 503}
    observe.assert_not_called()
    assert "auth" not in denied.get_json() and "supply_mode" not in denied.get_json()
