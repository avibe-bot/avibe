"""Persisted native custody and whole-document writer exclusion, fixture only."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from config import paths
from config.v2_config import ModelHubSourceConfig, ModelHubSourceStateConfig, V2Config
from core.agent_auth_service import AgentAuthService, BackendLoginInProgressError
from core.backend_restart import NativeCredentialLease, NativeMigrationBlockedError
from modules.im import MessageContext
from tests.test_native_takeover_lifecycle import controller_fixture
from vibe import api, claude_config, opencode_config


@pytest.fixture(autouse=True)
def no_live_operations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This custody test must not reach a live operation")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(psutil, "process_iter", forbidden)
    monkeypatch.setattr(api, "restart_backend", Mock(return_value={"ok": True}))
    monkeypatch.setattr(api, "_opencode_get_server", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_config_recovery_message", lambda: None)


def saved_config(*, mode="hub", enabled=True, source=None):
    config = V2Config.default()
    config.model_hub.enabled = enabled
    for supply in config.model_hub.agents.values():
        supply.mode = mode
    if source is not None:
        config.model_hub.sources = [source]
    config.save()
    return config


def native_source(*, vendor="openai", channel="native_cli"):
    return ModelHubSourceConfig(
        id="src_retained1",
        kind="subscription",
        vendor=vendor,
        display_name="Fixture subscription",
        protocol="anthropic" if vendor == "anthropic" else "openai_responses",
        supply_channel=channel,
        billing="monthly",
        state=ModelHubSourceStateConfig(),
        models=[],
        credential_ref="fixture-ref" if channel == "hub" else None,
    )


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("enabled", [True, False])
def test_custody_follows_persisted_mode_not_runtime_switch(backend, enabled):
    saved_config(mode="direct", enabled=enabled)
    with NativeCredentialLease((backend,)) as lease:
        lease.assert_auth_custody(backend)
    saved_config(enabled=enabled)
    with NativeCredentialLease((backend,)) as lease:
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
            lease.assert_auth_custody(backend)


def test_missing_config_is_new_hub_custody_and_malformed_is_fail_closed():
    with NativeCredentialLease(("codex",)) as lease:
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
            lease.assert_auth_custody("codex")
        config_path = paths.get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{bad-json")
        with pytest.raises(NativeMigrationBlockedError, match="config_recovery"):
            lease.assert_auth_custody("codex")


def test_legacy_missing_backend_config_keeps_direct_custody():
    saved_config()
    payload = json.loads(paths.get_config_path().read_text())
    payload.pop("model_hub")
    paths.get_config_path().write_text(json.dumps(payload))
    with NativeCredentialLease(("codex",)) as lease:
        lease.assert_auth_custody("codex")


@pytest.mark.parametrize("backend,vendor", [("codex", "openai"), ("claude", "anthropic")])
def test_retained_subscription_requires_matching_source_binding(backend, vendor):
    source = native_source(vendor=vendor)
    saved_config(source=source, enabled=False)
    with NativeCredentialLease((backend,)) as lease:
        lease.assert_auth_custody(backend, source_id=source.id)
        for source_id in (None, "src_different"):
            with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
                lease.assert_auth_custody(backend, source_id=source_id)
    source.supply_channel = "hub"
    source.credential_ref = "fixture-ref"
    saved_config(source=source)
    with NativeCredentialLease((backend,)) as lease:
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
            lease.assert_auth_custody(backend, source_id=source.id)


@pytest.mark.parametrize("backend,vendor", [("codex", "openai"), ("claude", "anthropic")])
def test_hub_backend_admits_creating_the_empty_native_subscription_slot(backend, vendor):
    saved_config(enabled=False)
    with NativeCredentialLease((backend,)) as lease:
        lease.assert_auth_custody(backend, source_id="src_new000001", new_source=True)
        # Generic Settings/IM login carries no Source to create.
        for source_id, new_source in ((None, True), ("src_new000001", False)):
            with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
                lease.assert_auth_custody(backend, source_id=source_id, new_source=new_source)
    source = native_source(vendor=vendor)
    saved_config(source=source)
    with NativeCredentialLease((backend,)) as lease:
        # The vendor's single native credential already belongs to a Source.
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
            lease.assert_auth_custody(backend, source_id="src_new000001", new_source=True)
    source.supply_channel = "hub"
    source.credential_ref = "fixture-ref"
    saved_config(source=source)
    with NativeCredentialLease((backend,)) as lease:
        # A Hub Source id cannot be reused as a new native Source.
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
            lease.assert_auth_custody(backend, source_id=source.id, new_source=True)
        lease.assert_auth_custody(backend, source_id="src_new000001", new_source=True)


def test_other_vendor_source_cannot_authorize_backend():
    saved_config(source=native_source(vendor="anthropic"))
    with NativeCredentialLease(("codex",)) as lease:
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
            lease.assert_auth_custody("codex", source_id="src_retained1")


@pytest.mark.asyncio
async def test_stale_controller_cannot_spawn_native_login_after_takeover():
    stale = saved_config(mode="direct")
    service = AgentAuthService(SimpleNamespace(config=stale))
    saved_config(enabled=False)
    service._start_codex_process = AsyncMock()
    with pytest.raises(NativeMigrationBlockedError, match="native_auth_hub_owned"):
        await service.start_web_setup("codex")
    service._start_codex_process.assert_not_awaited()
    with NativeCredentialLease(("codex",)):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,expected",
    [("en", "Manage Codex authentication in Model Hub."),
     ("zh", "请在模型网关中管理 Codex 的认证。")],
)
async def test_im_native_setup_after_takeover_directs_to_hub_without_reset(language, expected):
    config = saved_config()
    config.language = language
    service = AgentAuthService(SimpleNamespace(config=config, _get_settings_key=lambda _: "fixture"))
    service._send_message = AsyncMock()
    service._send_setup_start_failure = AsyncMock()
    service._start_codex_process = AsyncMock()
    context = SimpleNamespace(channel_id="fixture", user_id="fixture", platform="slack")

    await service.start_setup(context, backend="codex")

    assert service._send_message.await_args.args[1].casefold() == expected.casefold()
    service._send_setup_start_failure.assert_not_awaited()
    service._start_codex_process.assert_not_awaited()
    assert service._flows == {}


@pytest.mark.asyncio
async def test_bound_native_source_keeps_lease_until_terminal_cleanup():
    source = native_source()
    config = saved_config(source=source)
    first = AgentAuthService(SimpleNamespace(config=config))
    second = AgentAuthService(SimpleNamespace(config=config))
    first._start_codex_process = AsyncMock(return_value=SimpleNamespace(returncode=0))
    first._read_codex_output_web = AsyncMock()
    waiting = asyncio.Event()

    async def wait(flow):
        waiting.set()
        await asyncio.Event().wait()

    first._wait_for_codex_completion_web = wait
    flow = await first.start_web_setup("codex", owner_ref=source.id)
    await asyncio.wait_for(waiting.wait(), 2)
    try:
        with pytest.raises(BackendLoginInProgressError):
            await second.start_web_setup("codex", owner_ref=source.id)
        _, coordinator, admissions, _ = controller_fixture()
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_in_progress"):
            async with coordinator.migration_guard(("codex",)):
                pytest.fail("native flow overlapped takeover")
        assert not admissions
    finally:
        await first.cancel_web_flow(flow.flow_id)
    with NativeCredentialLease(("codex",)):
        pass


@pytest.mark.parametrize("mode,retained", [("direct", False), ("hub", False), ("hub", True)])
def test_constructor_only_restores_backup_under_direct_custody(mode, retained):
    config = saved_config(
        mode=mode, source=native_source(vendor="anthropic") if retained else None,
    )
    claude_config.write_claude_oauth_settings_backup({"ANTHROPIC_API_KEY": "fixture-old-key"})
    backup = claude_config.get_claude_oauth_settings_backup_path()
    before = backup.read_bytes()
    AgentAuthService(SimpleNamespace(config=config))
    if mode == "direct":
        assert not backup.exists()
        assert claude_config.read_claude_settings_env()["ANTHROPIC_API_KEY"] == "fixture-old-key"
    else:
        assert backup.read_bytes() == before
        assert not claude_config.read_claude_settings_env()


@pytest.mark.asyncio
async def test_all_generic_auth_api_writers_refuse_hub_and_point_to_hub(monkeypatch):
    saved_config(enabled=False)
    writer = Mock(side_effect=AssertionError("Native writer reached"))
    monkeypatch.setattr(claude_config, "apply_claude_auth", writer)
    from vibe import codex_config
    monkeypatch.setattr(codex_config, "apply_codex_auth", writer)
    monkeypatch.setattr(opencode_config, "upsert_opencode_custom_provider", writer)
    monkeypatch.setattr(opencode_config, "upsert_opencode_provider_api_key", writer)
    service = AgentAuthService(SimpleNamespace(config=V2Config.default()))
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service)
    results = [
        api.save_codex_auth({"auth_mode": "api_key", "api_key": "fixture"}),
        api.save_claude_auth({"auth_mode": "api_key", "api_key": "fixture"}),
        api.remove_backend_api_key("codex"),
        api.remove_backend_api_key("claude"),
        await api.save_opencode_provider_auth_async("openai", {"api_key": "fixture"}),
        await api.delete_opencode_provider_auth_async("openai"),
        await api.save_opencode_custom_provider_async({}),
        await api.delete_opencode_custom_provider_async("fixture"),
        await api.start_oauth_web_async("codex"),
        await api.start_oauth_web_async("claude"),
        await api.start_oauth_web_async("opencode", provider_id="openai"),
        await api.remove_backend_auth_async("codex"),
        await api.remove_claude_oauth_credentials_async(),
        await api.test_backend_auth_async("claude"),
        await api.test_opencode_provider_async("openai"),
    ]
    for result in results:
        assert result["ok"] is False
        assert result["error"] == "native_auth_hub_owned"
        assert result["reauth_channel"] == "hub"
    writer.assert_not_called()
    with NativeCredentialLease(("claude", "codex", "opencode")):
        pass


async def non_auth_write(kind):
    if kind == "permission":
        return await asyncio.to_thread(api.setup_opencode_permission)
    if kind == "add":
        return await api.save_opencode_provider_model_async(
            "openai", {"model_id": "fixture-model", "api_key": "must-not-be-used"},
        )
    return await api.delete_opencode_provider_model_async("openai", "fixture-model")


def opencode_fixture():
    path = opencode_config.get_opencode_config_paths(Path.home())[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "provider": {"openai": {"options": {"apiKey": "fixture-existing"}}},
    }))
    opencode_config.upsert_opencode_provider_model("openai", "fixture-model")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission", "add", "delete"])
@pytest.mark.parametrize("blocker", ["lease", "journal"])
async def test_non_auth_rmw_is_excluded_before_first_read(monkeypatch, kind, blocker):
    saved_config()
    path = opencode_fixture()
    before = path.read_bytes()
    lease = NativeCredentialLease(("opencode",))
    if blocker == "lease":
        lease.acquire()
    else:
        lease.directory.mkdir(parents=True, exist_ok=True)
        (lease.directory / "current.json").write_text(json.dumps({
            "version": 1, "phase": "exposed", "backends": ["opencode"],
        }))
    try:
        result = await non_auth_write(kind)
        assert result["error"] == (
            "native_auth_in_progress" if blocker == "lease" else "migration_recovery_pending"
        )
        assert path.read_bytes() == before
        api._opencode_get_server.assert_not_awaited()
    finally:
        lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission", "add", "delete"])
async def test_non_auth_rmw_remains_allowed_in_hub_without_introducing_key(kind):
    saved_config(enabled=False)
    path = opencode_fixture()
    opencode_config.remove_opencode_provider_api_key("openai")
    assert (await non_auth_write(kind))["ok"]
    assert "apiKey" not in path.read_text()
    assert "must-not-be-used" not in path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission", "add", "delete"])
async def test_rmw_cancellation_tail_cannot_overlap_takeover(monkeypatch, kind):
    saved_config()
    path = opencode_fixture()
    started, finish, settled = threading.Event(), threading.Event(), threading.Event()
    original = Path.write_text if kind == "permission" else opencode_config._write_opencode_config

    def slow_write(*args, **kwargs):
        started.set()
        try:
            assert finish.wait(5)
            return original(*args, **kwargs)
        finally:
            settled.set()

    if kind == "permission":
        monkeypatch.setattr(Path, "write_text", slow_write)
    else:
        monkeypatch.setattr(opencode_config, "_write_opencode_config", slow_write)
    task = asyncio.create_task(non_auth_write(kind))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        _, coordinator, admissions, _ = controller_fixture()
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_in_progress"):
            async with coordinator.migration_guard(("opencode",)):
                pytest.fail("A stale credential writer crossed native withdrawal")
        assert not admissions
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(settled.wait, 2)
    # The synchronous request thread can still be returning after write_text.
    for _ in range(200):
        try:
            lease = NativeCredentialLease(("opencode",)).acquire()
            lease.release()
            break
        except NativeMigrationBlockedError:
            await asyncio.sleep(0.005)
    else:
        pytest.fail("Completed writer did not release its lease")
    async with coordinator.migration_guard(("opencode",)) as verify_idle:
        await verify_idle()
        opencode_config.remove_opencode_provider_api_key("openai")
    assert "apiKey" not in path.read_text()


@pytest.mark.asyncio
async def test_recovery_pending_propagates_without_becoming_login_busy():
    config = saved_config(mode="direct")
    service = AgentAuthService(SimpleNamespace(config=config))
    directory = paths.get_state_dir() / "native-takeover"
    (directory / "current.json").write_text(json.dumps({
        "version": 1, "phase": "reverting", "backends": ["codex"],
    }))
    with pytest.raises(NativeMigrationBlockedError, match="migration_recovery_pending"):
        await service.start_web_setup("codex")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["im_api_key", "web_redirect"])
async def test_concurrent_cancel_joins_request_owned_writer_before_releasing(monkeypatch, surface):
    from core.agent_auth_service import WebAuthFlow

    config = saved_config(mode="direct")
    service = AgentAuthService(SimpleNamespace(config=config))
    started, finish = asyncio.Event(), asyncio.Event()
    written = []

    async def write(*args):
        started.set()
        await finish.wait()
        opencode_config.upsert_opencode_provider_api_key("openai", "fixture-new")
        written.append(True)
        return {"ok": True}

    if surface == "im_api_key":
        context = MessageContext(user_id="fixture", channel_id="fixture")
        service._get_settings_key = lambda _: "fixture"
        service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        service._install_opencode_api_key = write
        service._refresh_backend_runtime = AsyncMock()
        service._clear_backend_sessions_for_context = AsyncMock()
        service._send_message = AsyncMock()
        flow = await service._start_auth_flow("opencode", context=context)
        assert not flow.native_cli
        submit = asyncio.create_task(service.submit_code(context, "fixture-new", "opencode"))
        terminate = lambda: service._terminate_flow(flow)
    else:
        flow = WebAuthFlow(
            "redirect", "opencode", state="awaiting_code", provider="openai", callback_kind="redirect",
            native_lease=service._acquire_native_lease("opencode"),
        )
        service._flow_registry.put(flow)
        service._submit_opencode_callback_url = write
        submit = asyncio.create_task(service.submit_web_code(flow.flow_id, "fixture-callback"))
        terminate = lambda: service._terminate_web_flow(flow, final_state="cancelled")
    await asyncio.wait_for(started.wait(), 2)
    submit.cancel()
    cleanup = asyncio.create_task(terminate())
    try:
        await asyncio.sleep(0)
        assert not cleanup.done()
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_in_progress"):
            NativeCredentialLease(("opencode",)).acquire()
        assert not written
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(submit, 2)
        await asyncio.wait_for(cleanup, 2)
    assert written
    with NativeCredentialLease(("opencode",)):
        pass
