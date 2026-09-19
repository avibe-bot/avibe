"""Readiness observes persisted launch auth and the existing lifecycle owner."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config.v2_config import V2Config
from vibe import api, internal_client, runtime, ui_server


@pytest.fixture
def connection(monkeypatch, tmp_path):
    config = V2Config.default()
    config.agents.claude.enabled = True
    config.agents.codex.enabled = True
    config.agents.opencode.enabled = True
    monkeypatch.setattr(api, "load_config", lambda: config)
    monkeypatch.setattr(api, "resolve_cli_path", lambda _: str(tmp_path / "助手 cli"))
    monkeypatch.setattr(api, "get_claude_auth", lambda: {"ok": True, "active_auth_mode": "api_key"})
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
    getattr(config.agents, backend).cli_path = "/old/missing-cli"
    config.save()
    installed_path = str(tmp_path / "安装 后端/bin" / backend)
    monkeypatch.setattr(api, "resolve_cli_path", lambda _: installed_path)
    monkeypatch.setattr(api.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        communicate=lambda **_: ("fixture installed", ""), returncode=0,
    ))
    monkeypatch.setattr(api, "install_agent", lambda name: api._run_install_command(name, ["fixture-installer"], lambda value: value))
    monkeypatch.setattr(api, "get_claude_auth", lambda: {"ok": True, "active_auth_mode": "api_key"})
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
