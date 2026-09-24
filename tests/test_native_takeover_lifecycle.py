"""Hermetic credential ownership, admission and strict retirement contracts."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config.paths import get_state_dir
from core.backend_restart import (
    BackendRestartCoordinator,
    NativeCredentialLease,
    NativeMigrationBlockedError,
    finish_native_operation,
    native_cli_processes,
    pending_native_backends,
)


def controller_fixture(*, backends=("claude", "codex", "opencode"), busy=False):
    admissions = set()
    turns = set()
    service = SimpleNamespace(
        agents={name: SimpleNamespace(retire_for_native_migration=AsyncMock()) for name in backends},
        begin_backend_drain=Mock(side_effect=admissions.add),
        end_backend_drain=Mock(side_effect=admissions.discard),
        prepare_backend_restart=AsyncMock(),
        runtime_turn_tokens_for_backend=Mock(return_value={}),
        backend_runtime_active=Mock(return_value=busy),
        force_cancel_backend_turns=AsyncMock(),
        force_end_backend_activities=Mock(),
    )
    controller = SimpleNamespace(
        agent_service=service,
        session_turns=SimpleNamespace(
            begin_backend_drain=Mock(side_effect=turns.add),
            end_backend_drain=AsyncMock(side_effect=lambda backend, **kw: turns.discard(backend)),
        ),
        model_hub_service=SimpleNamespace(
            migration_blocked_backends=set(), events=SimpleNamespace(path=get_state_dir() / "events.jsonl")
        ),
        config=SimpleNamespace(),
    )
    coordinator = BackendRestartCoordinator(
        controller, AsyncMock(), process_inventory=Mock(return_value=()), drain_timeout=0.01, poll_interval=0.001,
    )
    controller.backend_restart_coordinator = coordinator
    return controller, coordinator, admissions, turns


def test_independent_leases_are_not_reentrant_on_same_thread(tmp_path):
    with NativeCredentialLease(("codex",), state_dir=tmp_path):
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_in_progress"):
            NativeCredentialLease(("codex",), state_dir=tmp_path).acquire()
        with NativeCredentialLease(("claude",), state_dir=tmp_path):
            pass
    with NativeCredentialLease(("codex",), state_dir=tmp_path):
        pass


def test_lease_excludes_an_independent_process_and_releases_on_error(tmp_path):
    program = (
        "from pathlib import Path; import sys; "
        "from core.backend_restart import NativeCredentialLease, NativeMigrationBlockedError\n"
        "try:\n"
        " with NativeCredentialLease(('codex',),state_dir=Path(sys.argv[1])): pass\n"
        "except NativeMigrationBlockedError: sys.exit(17)\n"
    )
    with pytest.raises(RuntimeError):
        with NativeCredentialLease(("codex",), state_dir=tmp_path):
            result = subprocess.run([sys.executable, "-c", program, str(tmp_path)], check=False)
            assert result.returncode == 17
            raise RuntimeError("owned operation failed")
    assert subprocess.run([sys.executable, "-c", program, str(tmp_path)], check=False).returncode == 0


def test_partial_multi_backend_acquire_releases_earlier_locks(tmp_path):
    with NativeCredentialLease(("codex",), state_dir=tmp_path):
        with pytest.raises(NativeMigrationBlockedError):
            NativeCredentialLease(("claude", "codex"), state_dir=tmp_path).acquire()
        with NativeCredentialLease(("claude",), state_dir=tmp_path):
            pass


@pytest.mark.skipif(os.name != "posix", reason="the rejected half needs real POSIX permission bits")
def test_lease_permission_mask_is_posix_only(tmp_path, monkeypatch):
    """The 0o077 mask describes POSIX permissions; elsewhere it describes nothing.

    Windows synthesizes st_mode from file attributes, so a lease file there
    always reports 0o666 and the mask would reject every acquire rather than
    describe one -- the failure that kept the Windows desktop package from
    building (see issue #2132). Both directions are asserted: POSIX still
    refuses a group-accessible lease file, and the identical file acquires once
    os.name is not posix.
    """
    directory = tmp_path / "native-takeover"
    directory.mkdir(mode=0o700)
    (directory / "codex.lock").touch(mode=0o600)
    (directory / "codex.lock").chmod(0o660)

    with pytest.raises(OSError, match="Unsafe native lease file"):
        NativeCredentialLease(("codex",), state_dir=tmp_path).acquire()

    class _NonPosixOs:
        """Scoped to the module under test, so storage.lock keeps using this
        host's real locking primitive instead of importing msvcrt."""

        name = "nt"

        def __getattr__(self, attr):
            return getattr(os, attr)

    monkeypatch.setattr("core.backend_restart.os", _NonPosixOs())
    with NativeCredentialLease(("codex",), state_dir=tmp_path):
        pass


@pytest.mark.parametrize("data", [
    None, [], {}, {"version": 1, "phase": "prepared", "backends": [{}]},
    {"version": 2, "phase": "prepared", "backends": ["codex"]},
    {"version": 1, "phase": [], "backends": ["codex"]},
])
def test_corrupt_journal_blocks_all_native_writers(tmp_path, data):
    directory = tmp_path / "native-takeover"
    directory.mkdir()
    (directory / "current.json").write_text("invalid" if data is None else json.dumps(data))
    assert pending_native_backends(directory) == {"claude", "codex", "opencode"}
    for backend in ("claude", "codex", "opencode"):
        with pytest.raises(NativeMigrationBlockedError, match="migration_recovery_pending"):
            NativeCredentialLease((backend,), state_dir=tmp_path).acquire()
    lease = NativeCredentialLease(("codex",), state_dir=tmp_path).acquire(recovery=True)
    lease.release()


def test_pending_journal_only_blocks_affected_backend(tmp_path):
    directory = tmp_path / "native-takeover"
    directory.mkdir()
    (directory / "current.json").write_text(json.dumps({
        "version": 1, "phase": "exposed", "backends": ["codex"],
    }))
    with pytest.raises(NativeMigrationBlockedError):
        NativeCredentialLease(("codex",), state_dir=tmp_path).acquire()
    with NativeCredentialLease(("claude",), state_dir=tmp_path):
        pass


@pytest.mark.asyncio
async def test_guard_closes_both_admissions_before_retirement_and_retains_only_blocked():
    controller, coordinator, admissions, turns = controller_fixture()

    async def retire():
        assert admissions == turns == {"claude", "codex"}
        with pytest.raises(NativeMigrationBlockedError):
            NativeCredentialLease(("codex",)).acquire()

    controller.agent_service.agents["codex"].retire_for_native_migration.side_effect = retire
    async with coordinator.migration_guard(("codex", "claude")):
        assert admissions == turns == {"claude", "codex"}
        controller.model_hub_service.migration_blocked_backends.add("codex")
        with pytest.raises(NativeMigrationBlockedError):
            await coordinator.request_restart("claude")
    assert admissions == turns == {"codex"}
    controller.agent_service.force_cancel_backend_turns.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_guard_never_cancels_or_yields():
    controller, coordinator, admissions, turns = controller_fixture(busy=True)
    with pytest.raises(NativeMigrationBlockedError, match="native_runtime_busy"):
        async with coordinator.migration_guard(("codex",)):
            pytest.fail("Mutation admitted while busy")
    assert admissions == turns == set()
    controller.agent_service.force_cancel_backend_turns.assert_not_awaited()
    controller.agent_service.agents["codex"].retire_for_native_migration.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_drains_active_work_naturally():
    controller, coordinator, admissions, turns = controller_fixture(busy=True)
    coordinator._drain_timeout = 1
    ready = asyncio.Event()

    async def transaction():
        async with coordinator.migration_guard(("codex",)):
            ready.set()
    task = asyncio.create_task(transaction())
    while not admissions:
        await asyncio.sleep(0)
    assert not ready.is_set()
    controller.agent_service.backend_runtime_active.return_value = False
    await task
    assert ready.is_set()
    assert not admissions and not turns


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_hook", "retire_failure", "external"])
async def test_failed_retirement_or_external_process_never_yields(failure):
    controller, coordinator, admissions, turns = controller_fixture()
    if failure == "missing_hook":
        controller.agent_service.agents["codex"] = object()
    elif failure == "retire_failure":
        controller.agent_service.agents["codex"].retire_for_native_migration.side_effect = RuntimeError("stop failed")
    else:
        coordinator._process_inventory.return_value = (123,)
    with pytest.raises(NativeMigrationBlockedError):
        async with coordinator.migration_guard(("codex",)):
            pytest.fail("unsafe migration admission")
    assert not admissions and not turns
    with NativeCredentialLease(("codex",)):
        pass


@pytest.mark.asyncio
async def test_disabled_backend_still_requires_process_inventory():
    _, coordinator, _, _ = controller_fixture(backends=())
    coordinator._process_inventory.return_value = (999,)
    with pytest.raises(NativeMigrationBlockedError, match="external_native_processes"):
        async with coordinator.migration_guard(("codex",)):
            pytest.fail("unmanaged process bypass")


@pytest.mark.asyncio
async def test_verify_idle_rechecks_external_processes_before_each_cutover():
    _, coordinator, admissions, turns = controller_fixture()
    async with coordinator.migration_guard(("codex",)) as verify_idle:
        await verify_idle()
        coordinator._process_inventory.return_value = (123,)
        with pytest.raises(NativeMigrationBlockedError, match="external_native_processes"):
            await verify_idle()
        assert admissions == turns == {"codex"}
    with pytest.raises(RuntimeError, match="no ownership"):
        await verify_idle()


@pytest.mark.asyncio
async def test_live_auth_lease_refuses_migration_before_admission_closes():
    _, coordinator, admissions, turns = controller_fixture()
    with NativeCredentialLease(("codex",)):
        with pytest.raises(NativeMigrationBlockedError, match="native_auth_in_progress"):
            async with coordinator.migration_guard(("codex",)):
                pytest.fail("overlapping native writers")
    assert not admissions and not turns


@pytest.mark.asyncio
async def test_restart_task_and_migration_have_one_owner():
    controller, coordinator, admissions, turns = controller_fixture(busy=True)
    coordinator._drain_timeout = 1
    assert await coordinator.request_restart("codex") == "draining"
    with pytest.raises(NativeMigrationBlockedError, match="backend_restart_in_progress"):
        async with coordinator.migration_guard(("codex",)):
            pytest.fail("competing restart")
    controller.agent_service.backend_runtime_active.return_value = False
    await coordinator.wait("codex")
    assert not admissions and not turns


@pytest.mark.asyncio
async def test_cancelled_guard_waits_for_strict_retirement_before_reopening():
    controller, coordinator, admissions, turns = controller_fixture()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def retire():
        entered.set()
        await finish.wait()
    controller.agent_service.agents["codex"].retire_for_native_migration.side_effect = retire

    async def transaction():
        async with coordinator.migration_guard(("codex",)):
            pytest.fail("cancelled mutation")
    task = asyncio.create_task(transaction())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert admissions == turns == {"codex"}
    with pytest.raises(NativeMigrationBlockedError):
        NativeCredentialLease(("codex",)).acquire()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not admissions and not turns


@pytest.mark.asyncio
async def test_recovery_error_applies_pending_blocks_before_other_producers():
    from core.controller import Controller

    fake, coordinator, admissions, turns = controller_fixture()
    controller = Controller.__new__(Controller)
    controller.__dict__.update(fake.__dict__)
    coordinator.controller = controller
    directory = get_state_dir() / "native-takeover"
    directory.mkdir(parents=True)
    (directory / "current.json").write_text("corrupt envelope")
    controller.model_hub_service.recover_runtime_intent = AsyncMock(side_effect=RuntimeError("recovery failed"))

    async def producer(**kwargs):
        assert admissions == turns == {"claude", "codex", "opencode"}
        return []
    controller.session_turns.recover_durable_delivery_state = AsyncMock(side_effect=producer)
    controller.session_turns.recover_persisted_agent_run_queue = AsyncMock(side_effect=producer)
    controller.scheduled_task_service = SimpleNamespace(recover_processing_requests=Mock())
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock(side_effect=producer))
    controller._delivery_recovery_complete = asyncio.Event()
    await controller._recover_runtime_owners()
    assert controller._delivery_recovery_complete.is_set()


@pytest.mark.parametrize("command,matched", [
    (["/opt/bin/codex", "login"], True),
    (["codex", "app-server"], True),
    (["claude"], True),
    (["/usr/bin/node", "/npm/@anthropic-ai/claude-code/cli.js", "--resume", "fake"], True),
    (["/usr/bin/node", "/npm/@openai/codex/bin/codex.js"], True),
    (["opencode", "serve", "--port", "1234"], True),
    (["python", "test.py", "claude"], False),
    (["/bin/echo", "codex"], False),
])
def test_process_inventory_matches_executables_not_prompt_arguments(monkeypatch, command, matched):
    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [
        SimpleNamespace(info={
            "pid": 123, "uids": SimpleNamespace(real=os.getuid()), "status": "running",
            "cmdline": command, "name": Path(command[0]).name,
        }),
        SimpleNamespace(info={
            "pid": 456, "uids": SimpleNamespace(real=os.getuid() + 1), "status": "running",
            "cmdline": ["codex"], "name": "codex",
        }),
    ])
    assert native_cli_processes(dict.fromkeys(("claude", "codex", "opencode"), "")) == ((123,) if matched else ())


def test_incomplete_same_user_inventory_refuses(monkeypatch):
    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [SimpleNamespace(info={
        "pid": 123, "uids": SimpleNamespace(real=os.getuid()), "status": "running", "cmdline": None,
    })])
    with pytest.raises(NativeMigrationBlockedError, match="process_inventory_unavailable"):
        native_cli_processes({"codex": "codex"})


@pytest.mark.asyncio
async def test_cancelled_thread_writer_keeps_lease_until_worker_finishes():
    started = threading.Event()
    finish = threading.Event()

    def writer():
        started.set()
        assert finish.wait(5)

    async def operation():
        with NativeCredentialLease(("codex",)):
            await finish_native_operation(asyncio.to_thread(writer))
    task = asyncio.create_task(operation())
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    with pytest.raises(NativeMigrationBlockedError):
        NativeCredentialLease(("codex",)).acquire()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with NativeCredentialLease(("codex",)):
        pass


def auth_service(monkeypatch):
    from core.agent_auth_service import AgentAuthService
    from config.v2_config import V2Config

    config = V2Config.default()
    for supply in config.model_hub.agents.values():
        supply.mode = "direct"
    config.save()
    monkeypatch.setattr(AgentAuthService, "_recover_interrupted_claude_oauth_settings_backup", lambda self: None)
    return AgentAuthService(SimpleNamespace(config=SimpleNamespace()))


@pytest.mark.asyncio
async def test_auth_flow_cross_instance_exclusion_through_terminal_cleanup(monkeypatch):
    from core.agent_auth_service import BackendLoginInProgressError

    first = auth_service(monkeypatch)
    second = auth_service(monkeypatch)
    waiting = asyncio.Event()
    terminal = asyncio.Event()
    cleaning = asyncio.Event()
    clean = asyncio.Event()
    process = SimpleNamespace(returncode=0)
    first._start_codex_process = AsyncMock(return_value=process)
    first._read_codex_output_web = AsyncMock()

    async def waiter(flow):
        waiting.set()
        await terminal.wait()
        flow.state = "success"
    original_cleanup = first._cleanup_native_flow

    async def cleanup(flow):
        cleaning.set()
        await clean.wait()
        await original_cleanup(flow)
    first._wait_for_codex_completion_web = waiter
    first._cleanup_native_flow = cleanup
    second._start_codex_process = AsyncMock()
    flow = await first._start_auth_flow("codex")
    await waiting.wait()
    with pytest.raises(BackendLoginInProgressError):
        await second._start_auth_flow("codex")
    second._start_codex_process.assert_not_awaited()
    terminal.set()
    await cleaning.wait()
    with pytest.raises(NativeMigrationBlockedError):
        NativeCredentialLease(("codex",)).acquire()
    clean.set()
    await flow.waiter_task
    with NativeCredentialLease(("codex",)):
        pass


@pytest.mark.asyncio
async def test_start_failure_releases_native_lease(monkeypatch):
    service = auth_service(monkeypatch)
    service._start_codex_process = AsyncMock(side_effect=RuntimeError("spawn failed"))
    with pytest.raises(RuntimeError, match="spawn failed"):
        await service._start_auth_flow("codex")
    assert not service._flows_by_id
    with NativeCredentialLease(("codex",)):
        pass


@pytest.mark.asyncio
async def test_flow_cancel_releases_after_process_wait(monkeypatch):
    service = auth_service(monkeypatch)
    stopped = asyncio.Event()
    process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock())

    async def wait():
        await stopped.wait()
        process.returncode = -15
    process.wait = wait
    service._start_codex_process = AsyncMock(return_value=process)
    service._read_codex_output_web = AsyncMock()
    running = asyncio.Event()

    async def waiter(flow):
        running.set()
        await asyncio.Event().wait()
    service._wait_for_codex_completion_web = waiter
    flow = await service._start_auth_flow("codex")
    await running.wait()
    cancel = asyncio.create_task(service.cancel_web_flow(flow.flow_id))
    while not process.terminate.called:
        await asyncio.sleep(0)
    with pytest.raises(NativeMigrationBlockedError):
        NativeCredentialLease(("codex",)).acquire()
    stopped.set()
    await cancel
    assert not service._flows_by_id
    with NativeCredentialLease(("codex",)):
        pass


@pytest.mark.asyncio
async def test_flow_cleanup_failure_retains_ownership(monkeypatch):
    service = auth_service(monkeypatch)
    from core.agent_auth_service import WebAuthFlow

    flow = WebAuthFlow("fake", "claude", native_lease=NativeCredentialLease(("claude",)).acquire())
    flow.claude_client = SimpleNamespace(disconnect=AsyncMock(side_effect=RuntimeError("disconnect failed")))
    service._flow_registry.put(flow)
    service._arm_flow_waiter(flow, asyncio.sleep(0))
    with pytest.raises(RuntimeError, match="disconnect failed"):
        await flow.waiter_task
    with pytest.raises(NativeMigrationBlockedError):
        NativeCredentialLease(("claude",)).acquire()
    assert service._flows_by_id["fake"] is flow
    flow.claude_client.disconnect.side_effect = None
    await service._cleanup_native_flow(flow)


@pytest.mark.asyncio
async def test_api_writers_do_not_reach_native_files_under_other_owner(monkeypatch):
    from vibe import api

    monkeypatch.setattr(api, "_config_recovery_message", Mock(side_effect=AssertionError("writer reached")))
    with NativeCredentialLease(("claude", "codex", "opencode")):
        assert api.save_codex_auth({})["error"] == "native_auth_in_progress"
        assert api.save_claude_auth({})["error"] == "native_auth_in_progress"
        assert api.remove_backend_api_key("codex")["error"] == "native_auth_in_progress"
        assert (await api.save_opencode_provider_auth_async("openai", {}))["error"] == "native_auth_in_progress"
        assert (await api.delete_opencode_provider_auth_async("openai"))["error"] == "native_auth_in_progress"
        assert (await api.save_opencode_custom_provider_async({}))["error"] == "native_auth_in_progress"
        assert (await api.delete_opencode_custom_provider_async("fake"))["error"] == "native_auth_in_progress"


def test_unsupported_native_writer_preserves_api_validation():
    from vibe import api

    assert api.remove_backend_api_key("") == {"ok": False, "error": "unsupported_backend"}


@pytest.mark.asyncio
async def test_api_cancel_waits_for_writer_before_releasing():
    from vibe.api import _native_auth_write

    started = asyncio.Event()
    complete = asyncio.Event()

    @_native_auth_write("opencode", authentication=False)
    async def write():
        started.set()
        await complete.wait()
    task = asyncio.create_task(write())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    with pytest.raises(NativeMigrationBlockedError):
        NativeCredentialLease(("opencode",)).acquire()
    complete.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with NativeCredentialLease(("opencode",)):
        pass


@pytest.mark.asyncio
async def test_explicit_nested_claude_cleanup_uses_outer_lease(monkeypatch):
    service = auth_service(monkeypatch)
    service._clear_claude_oauth_credentials_owned = AsyncMock(return_value={"ok": True})
    with NativeCredentialLease(("claude",)) as lease:
        assert await service.clear_claude_oauth_credentials_only(lease=lease) == {"ok": True}
        with pytest.raises(NativeMigrationBlockedError):
            NativeCredentialLease(("claude",)).acquire()
    with pytest.raises(RuntimeError, match="no ownership"):
        await service.clear_claude_oauth_credentials_only(lease=lease)


@pytest.mark.asyncio
async def test_utility_failure_awaits_process_exit_before_return(monkeypatch):
    service = auth_service(monkeypatch)
    process = SimpleNamespace(
        returncode=None, terminate=Mock(), kill=Mock(),
        communicate=AsyncMock(side_effect=asyncio.TimeoutError),
    )
    async def wait():
        process.returncode = -15
    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    ok, _ = await service._run_utility_command("fake-native-cli", "logout")
    assert not ok
    process.terminate.assert_called_once()
    assert process.returncode == -15


@pytest.mark.asyncio
async def test_cancelled_unstarted_waiter_still_releases_lease(monkeypatch):
    from core.agent_auth_service import WebAuthFlow

    service = auth_service(monkeypatch)
    flow = WebAuthFlow("not-started", "codex", native_lease=NativeCredentialLease(("codex",)).acquire())
    flow.waiter_task = asyncio.create_task(asyncio.sleep(100))
    # A cancelled task can skip its coroutine's finally block entirely.
    flow.waiter_task.cancel()
    await service._terminate_web_flow(flow, final_state="cancelled")
    assert flow.native_lease is None
    with NativeCredentialLease(("codex",)):
        pass


@pytest.mark.asyncio
async def test_terminal_ttl_does_not_drop_failed_cleanup_owner(monkeypatch):
    from core.agent_auth_service import WebAuthFlow

    service = auth_service(monkeypatch)
    flow = WebAuthFlow(
        "failed-cleanup", "codex", state="failed", terminal_at=0,
        native_lease=NativeCredentialLease(("codex",)).acquire(),
    )
    service._flow_registry.put(flow)
    service.get_web_flow_status(flow.flow_id)
    assert service._flows_by_id[flow.flow_id] is flow
    await service._cleanup_native_flow(flow)


@pytest.mark.asyncio
async def test_codex_strict_retirement_propagates_stop_failure():
    from modules.agents.codex.agent import CodexAgent

    transport = SimpleNamespace()
    agent = CodexAgent.__new__(CodexAgent)
    agent._transports = {"cwd": transport}
    agent._transport_locks = {}
    agent._stop_and_detach_transport_generation = AsyncMock(side_effect=RuntimeError("stop failed"))
    agent._session_mgr = SimpleNamespace(all_base_sessions=Mock(return_value=["session"]), invalidate_thread=Mock())
    with pytest.raises(RuntimeError, match="stop failed"):
        await agent.retire_for_native_migration()
    assert agent._transports["cwd"] is transport
    agent._session_mgr.invalidate_thread.assert_not_called()


@pytest.mark.asyncio
async def test_codex_strict_retirement_checks_exit_before_detaching():
    from modules.agents.codex.agent import CodexAgent

    process = SimpleNamespace(returncode=None, wait=AsyncMock())
    transport = SimpleNamespace(_process=process, stop=AsyncMock())
    agent = CodexAgent.__new__(CodexAgent)
    agent._reserve_transport_retirement = Mock(return_value="generation")
    agent._finish_transport_retirement = Mock(return_value=True)
    agent._detach_transport_bookkeeping = Mock(return_value=True)
    with pytest.raises(RuntimeError, match="did not exit"):
        await agent._stop_and_detach_transport_generation("cwd", transport, require_process_exit=True)
    agent._finish_transport_retirement.assert_called_once_with("generation", retire=False)
    agent._detach_transport_bookkeeping.assert_not_called()


@pytest.mark.asyncio
async def test_claude_retirement_never_uses_broad_reaper_or_swallows_disconnect():
    from modules.agents.claude_agent import ClaudeAgent

    process = SimpleNamespace(returncode=None, wait=AsyncMock())
    client = SimpleNamespace(_transport=SimpleNamespace(_process=process), disconnect=AsyncMock())
    handler = SimpleNamespace(
        active_sessions=set(), _claude_runtime_generation_lock=lambda key: asyncio.Lock(),
        _retire_claude_runtime_activation=Mock(return_value=True),
        clear_session_tracking=Mock(), _retire_model_hub_process_scope=Mock(),
        cleanup_session=AsyncMock(side_effect=AssertionError("broad cleanup is unsafe")),
    )
    agent = ClaudeAgent.__new__(ClaudeAgent)
    agent.session_handler = handler
    agent.claude_sessions = {"key": client}
    agent.receiver_tasks = {}
    agent._native_session_ids = {}
    agent._last_assistant_text = {}
    agent._pending_assistant_message = {}
    agent._retire_steering_state = Mock()
    agent._stop_receiver_task = AsyncMock()
    with pytest.raises(RuntimeError, match="did not exit"):
        await agent.retire_for_native_migration()
    assert agent.claude_sessions["key"] is client
    handler.clear_session_tracking.assert_not_called()
    process.returncode = 0
    await agent.retire_for_native_migration()
    assert not agent.claude_sessions
    handler.cleanup_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_opencode_unproven_pid_file_is_blocker_not_kill_target():
    from modules.agents.opencode.server import OpenCodeServerManager

    server = OpenCodeServerManager.__new__(OpenCodeServerManager)
    server._get_lock = lambda: asyncio.Lock()
    server._active_requests = 0
    server._has_active_run_sessions = Mock(return_value=False)
    server._runtime_activation_retire = Mock(return_value=True)
    server._process = None
    server.port = 4096
    server._read_pid_file = Mock(return_value={"pid": 123})
    server._pid_exists = Mock(return_value=True)
    server._terminate_pid_tree_sync = Mock(side_effect=AssertionError("must not kill"))
    server._clear_pid_file = Mock()
    with pytest.raises(RuntimeError, match="ownership cannot be proven"):
        await server.retire_for_native_migration()
    server._terminate_pid_tree_sync.assert_not_called()
    server._clear_pid_file.assert_not_called()


@pytest.mark.asyncio
async def test_opencode_strict_stop_failure_retains_tracking():
    from modules.agents.opencode.server import OpenCodeServerManager

    server = OpenCodeServerManager.__new__(OpenCodeServerManager)
    server._get_lock = lambda: asyncio.Lock()
    server._active_requests = 0
    server._has_active_run_sessions = Mock(return_value=False)
    server._runtime_activation_retire = Mock(return_value=True)
    process = SimpleNamespace(pid=123, returncode=None, wait=AsyncMock())
    server._process = process
    server._read_pid_file = Mock(return_value={"pid": 123})
    server._terminate_pid_tree_sync = Mock(return_value=False)
    server._clear_pid_file = Mock()
    with pytest.raises(RuntimeError, match="did not exit"):
        await server.retire_for_native_migration()
    assert server._process is process
    server._clear_pid_file.assert_not_called()
