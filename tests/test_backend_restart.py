from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.backend_restart import BackendRestartCoordinator, NativeCredentialLease, NativeMigrationBlockedError
from core.controller import Controller
from core.run_settlement import SETTLED_BY_BACKEND_DISABLED


class _AgentService:
    def __init__(self) -> None:
        self.active = False
        self.runtime_active = False
        self.draining = False
        self.agents: dict = {}

    def begin_backend_drain(self, backend: str) -> None:
        assert backend == "opencode"
        self.draining = True

    def end_backend_drain(self, backend: str) -> None:
        assert backend == "opencode"
        self.draining = False

    async def prepare_backend_restart(self, backend: str) -> None:
        assert backend == "opencode"

    def runtime_turn_tokens_for_backend(self, backend: str) -> dict[str, str]:
        return {"session:key": "token"} if self.active else {}

    def backend_runtime_active(self, backend: str) -> bool:
        return self.runtime_active

    def force_end_backend_activities(self, backend: str, *, reason: str = "backend_refresh") -> list:
        assert backend == "opencode"
        return []

    async def force_cancel_backend_turns(self, backend: str) -> None:
        assert backend == "opencode"


def _controller(service: _AgentService):
    session_turns = SimpleNamespace(
        begin_backend_drain=Mock(),
        end_backend_drain=AsyncMock(),
        active_session_ids_for_backend=Mock(
            side_effect=lambda _backend: {"ses-1"} if service.active else set()
        ),
        active_runtime_session_ids_for_backend=Mock(
            side_effect=lambda _backend: {"ses-1"} if service.active else set()
        ),
        release_for_backend_refresh=AsyncMock(),
    )
    return SimpleNamespace(agent_service=service, session_turns=session_turns)


def test_runtime_gen_004_a_restart_request_never_drains_holds_or_interrupts() -> None:
    """RUNTIME-GEN-004: a routine restart request interrupts nothing and holds no turn.

    Work is running, yet the request renews at once: admission never closes,
    no turn is interrupted, and nothing is torn down. A renewal that fails is
    reported, never shown as applied.
    """

    async def run() -> None:
        service = _AgentService()
        service.agents = {"opencode": object()}
        service.active = True
        service.runtime_active = True
        controller = _controller(service)
        refresh = AsyncMock()
        renew = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, refresh, renew=renew, poll_interval=0.001)

        assert await coordinator.request_restart("opencode", config_save=True) == "restarted"
        assert await coordinator.request_restart("opencode") == "restarted"

        assert [awaited.args for awaited in renew.await_args_list] == [("opencode", True), ("opencode", False)]
        assert service.draining is False
        refresh.assert_not_awaited()
        controller.session_turns.begin_backend_drain.assert_not_called()
        controller.session_turns.release_for_backend_refresh.assert_not_awaited()
        assert coordinator.snapshot("opencode")["state"] == "applied"

        renew.side_effect = RuntimeError("config rejected")
        with pytest.raises(RuntimeError, match="config rejected"):
            await coordinator.request_restart("opencode")
        assert coordinator.snapshot("opencode") == {"state": "failed", "error": "config rejected"}

        renew.side_effect = None
        await coordinator.request_restart("opencode")
        assert coordinator.snapshot("opencode") == {"state": "applied"}

    asyncio.run(run())


def test_controller_reconciles_requested_backends_once_in_order() -> None:
    async def run() -> None:
        coordinator = SimpleNamespace(request_restart=AsyncMock(side_effect=["restarted", "restarted"]))
        controller = SimpleNamespace(backend_restart_coordinator=coordinator)

        result = await Controller.reconcile_agent_backends(
            controller,
            ["codex", "codex", "opencode"],
        )

        assert result == {
            "ok": True,
            "backends": ["codex", "opencode"],
            "states": {"codex": "restarted", "opencode": "restarted"},
        }
        assert [awaited.args for awaited in coordinator.request_restart.await_args_list] == [
            ("codex",),
            ("opencode",),
        ]

    asyncio.run(run())


def test_controller_rejects_unknown_backend_reconcile() -> None:
    controller = SimpleNamespace(backend_restart_coordinator=SimpleNamespace(request_restart=AsyncMock()))

    with pytest.raises(ValueError, match="Unsupported agent backend: unknown"):
        asyncio.run(Controller.reconcile_agent_backends(controller, ["unknown"]))

    controller.backend_restart_coordinator.request_restart.assert_not_awaited()


def test_runtime_gen_008_idle_maintenance_runs_behind_closed_admission_then_renews():
    """RUNTIME-GEN-008: an unattended CLI update renews after its install.

    Admission is closed only while the CLI is replaced, so no turn launches a
    half-installed binary. The runtime then renews in place rather than being
    torn down, and messages held meanwhile resume on it.
    """

    async def run():
        service = _AgentService()
        controller = _controller(service)
        refresh = AsyncMock()
        renew = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, refresh, renew=renew, poll_interval=0.001)

        async def install():
            # Nothing may start a turn on the CLI while it is being replaced.
            assert service.draining is True
            controller.session_turns.begin_backend_drain.assert_called_once_with("opencode")
            renew.assert_not_awaited()
            return {"ok": True}

        assert await coordinator.run_when_idle("opencode", install) == {"ok": True}

        renew.assert_awaited_once_with("opencode", False)
        refresh.assert_not_awaited()
        controller.session_turns.end_backend_drain.assert_awaited_once_with("opencode", resume_deferred=True)
        assert service.draining is False

    asyncio.run(run())


def test_maintenance_yields_to_a_busy_backend_without_touching_its_work():
    async def run():
        service = _AgentService()
        service.active = True
        controller = _controller(service)
        refresh = AsyncMock()
        renew = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, refresh, renew=renew, poll_interval=0.001)
        operation = AsyncMock()

        assert await coordinator.run_when_idle("opencode", operation) is None

        operation.assert_not_awaited()
        refresh.assert_not_awaited()
        renew.assert_not_awaited()
        controller.session_turns.release_for_backend_refresh.assert_not_awaited()
        # The turn keeps running; messages held meanwhile resume unchanged.
        controller.session_turns.end_backend_drain.assert_awaited_once_with("opencode")
        assert service.draining is False

    asyncio.run(run())


def test_maintenance_and_a_native_login_exclude_each_other():
    async def run():
        service = _AgentService()
        controller = _controller(service)
        refresh = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, refresh, renew=AsyncMock(), poll_interval=0.001)
        operation = AsyncMock()

        # A login in the Web process holds the backend's lease for its whole flow.
        login = NativeCredentialLease(("opencode",)).acquire()
        try:
            assert await coordinator.run_when_idle("opencode", operation) is None
        finally:
            login.release()
        operation.assert_not_awaited()
        controller.session_turns.begin_backend_drain.assert_not_called()

        async def install():
            with pytest.raises(NativeMigrationBlockedError) as blocked:
                NativeCredentialLease(("opencode",)).acquire()
            assert blocked.value.reason == "native_auth_in_progress"
            return {"ok": True}

        assert await coordinator.run_when_idle("opencode", install) == {"ok": True}
        NativeCredentialLease(("opencode",)).acquire().release()

    asyncio.run(run())


def test_cancelled_requester_waits_for_maintenance_and_its_renewal():
    async def run():
        service = _AgentService()
        controller = _controller(service)
        renew = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, AsyncMock(), renew=renew, poll_interval=0.001)
        installing = asyncio.Event()
        finish = asyncio.Event()

        async def install():
            installing.set()
            await finish.wait()
            return {"ok": True}

        requester = asyncio.create_task(coordinator.run_when_idle("opencode", install))
        await installing.wait()
        requester.cancel()
        await asyncio.sleep(0.01)
        # Shutdown cancels the update checker; the CLI swap must not be abandoned.
        assert not requester.done()

        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await requester
        renew.assert_awaited_once_with("opencode", False)
        assert service.draining is False

    asyncio.run(run())


def test_a_restart_requested_during_maintenance_renews_without_waiting_for_it():
    async def run():
        service = _AgentService()
        service.agents = {"opencode": object()}
        controller = _controller(service)
        renew = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, AsyncMock(), renew=renew, poll_interval=0.001)
        installing = asyncio.Event()
        finish = asyncio.Event()

        async def install():
            installing.set()
            await finish.wait()
            return {"ok": True}

        maintenance = asyncio.create_task(coordinator.run_when_idle("opencode", install))
        await installing.wait()
        assert coordinator.snapshot("opencode") == {"state": "draining"}
        assert await asyncio.wait_for(coordinator.request_restart("opencode"), timeout=1) == "restarted"
        renew.assert_awaited_once_with("opencode", False)

        finish.set()
        assert await maintenance == {"ok": True}
        # The install renews again, so the replaced CLI is what new turns use.
        assert renew.await_count == 2
        assert coordinator.snapshot("opencode") == {"state": "applied"}

    asyncio.run(run())


class _DisabledAgent:
    """An agent whose turn is still running when its backend is disabled."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.shutdown_runtime = AsyncMock()


def _disabling_controller(agent: _DisabledAgent):
    from core.agent_auth_service import AgentAuthService
    from modules.agents.service import AgentService

    service = _AgentService()
    controller = _controller(service)
    controller.config = SimpleNamespace(codex=object())
    controller.agent_service = AgentService(controller)
    controller.agent_service.register(agent)
    auth = AgentAuthService(controller)
    auth._load_backend_runtime_config = Mock(return_value=None)
    auth._sync_builtin_default_agents = Mock()
    coordinator = BackendRestartCoordinator(controller, AsyncMock(), renew=auth.renew_backend_runtime)
    controller.backend_restart_coordinator = coordinator
    return controller, auth, coordinator


def test_runtime_gen_006_disabling_a_backend_stops_its_work_at_once():
    """RUNTIME-GEN-006: disabling a backend is the user's own interruption.

    The agent leaves the registry first, so a new message sees the backend
    disabled immediately and nothing is held. Its running turns and Activities
    settle with the runtime-interruption notice, and every process of the
    backend stops. Nothing waits for the work to finish.
    """

    async def run() -> None:
        agent = _DisabledAgent("codex")
        controller, _auth, coordinator = _disabling_controller(agent)
        controller.agent_service.force_cancel_backend_turns = AsyncMock(
            side_effect=lambda backend: order.append(("cancel", backend))
        )
        order: list = []
        agent.shutdown_runtime.side_effect = lambda **_kwargs: order.append(("stop", "codex"))
        controller.session_turns.active_runtime_session_ids_for_backend = Mock(return_value={"ses-1"})

        assert await asyncio.wait_for(coordinator.request_restart("codex", config_save=True), timeout=1) == "restarted"

        assert "codex" not in controller.agent_service.agents
        assert controller.config.codex is None
        # The user's own interruption is reported as such, not as a runtime refresh.
        controller.session_turns.release_for_backend_refresh.assert_awaited_once_with(
            backend="codex", base_session_ids={"ses-1"}, settled_by=SETTLED_BY_BACKEND_DISABLED
        )
        assert order == [("cancel", "codex"), ("stop", "codex")]
        controller.session_turns.begin_backend_drain.assert_not_called()

    asyncio.run(run())


def test_a_disable_whose_interruption_fails_still_stops_and_retries_the_processes():
    """Disabling never leaves a backend's processes unowned.

    An interruption failure does not skip the stop, and a stop that fails is
    retried by the idle sweep, because the agent is already out of routing.
    """

    async def run() -> None:
        agent = _DisabledAgent("codex")
        controller, _auth, coordinator = _disabling_controller(agent)
        controller.session_turns.release_for_backend_refresh = AsyncMock(side_effect=[RuntimeError("db busy"), 0])
        agent.shutdown_runtime.side_effect = [RuntimeError("process survived"), None]

        assert await coordinator.request_restart("codex", config_save=True) == "restarted"

        assert "codex" not in controller.agent_service.agents
        assert agent.shutdown_runtime.await_count == 1
        # The retry settles the work that failed to settle and stops the process.
        await controller.agent_service.retry_pending()
        assert controller.session_turns.release_for_backend_refresh.await_count == 2
        assert agent.shutdown_runtime.await_count == 2
        await controller.agent_service.retry_pending()
        assert agent.shutdown_runtime.await_count == 2

    asyncio.run(run())


def test_a_cancelled_disable_still_hands_the_process_stop_to_the_teardown_owner():
    """A requester cancelled while the disable interrupts work never leaves the
    disabled backend's processes without an owner."""

    async def run() -> None:
        agent = _DisabledAgent("codex")
        controller, _auth, coordinator = _disabling_controller(agent)
        interrupting = asyncio.Event()
        finish = asyncio.Event()

        async def slow_release(**_kwargs):
            interrupting.set()
            await finish.wait()
            return 0

        controller.session_turns.release_for_backend_refresh = slow_release
        disabling = asyncio.create_task(coordinator.request_restart("codex", config_save=True))
        await interrupting.wait()
        disabling.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await disabling

        agent.shutdown_runtime.assert_awaited_once_with(settle_reason="backend_disabled")
        assert controller.config.codex is None

    asyncio.run(run())


def test_runtime_gen_007_re_enabling_starts_a_fresh_agent_at_once(monkeypatch):
    """RUNTIME-GEN-007: re-enabling registers a new agent without waiting.

    Disabling already stopped the old agent's work, so nothing old remains and
    the new agent is the backend's only one.
    """
    import modules.agents.codex as codex_module

    async def run() -> None:
        old = _DisabledAgent("codex")
        controller, auth, coordinator = _disabling_controller(old)
        await coordinator.request_restart("codex", config_save=True)
        old.shutdown_runtime.assert_awaited_once_with(settle_reason="backend_disabled")

        runtime_config = SimpleNamespace(enabled=True)
        auth._load_backend_runtime_config = Mock(return_value=runtime_config)
        monkeypatch.setattr(
            codex_module, "CodexAgent", lambda _controller, config: SimpleNamespace(name="codex", config=config)
        )

        assert await asyncio.wait_for(coordinator.request_restart("codex", config_save=True), timeout=1) == "restarted"

        fresh = controller.agent_service.agents["codex"]
        assert fresh is not old and fresh.config is runtime_config
        controller.session_turns.begin_backend_drain.assert_not_called()

    asyncio.run(run())
