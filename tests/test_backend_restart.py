from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.backend_restart import BackendRestartCoordinator
from core.controller import Controller


class _AgentService:
    def __init__(self) -> None:
        self.active = False
        self.runtime_active = False
        self.draining = False

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

    def force_end_backend_activities(self, backend: str) -> list:
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


def test_restart_drains_active_turn_before_refresh() -> None:
    async def run() -> None:
        service = _AgentService()
        service.active = True
        controller = _controller(service)
        refresh = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, refresh, drain_timeout=1, poll_interval=0.001)

        assert await coordinator.request_restart("opencode") == "draining"
        assert service.draining is True
        refresh.assert_not_awaited()

        service.active = False
        await coordinator.wait("opencode")

        refresh.assert_awaited_once_with("opencode", False)
        controller.session_turns.release_for_backend_refresh.assert_not_awaited()
        controller.session_turns.end_backend_drain.assert_awaited_once_with("opencode", resume_deferred=True)
        assert service.draining is False

    asyncio.run(run())


def test_restart_timeout_forces_cutover_and_releases_workbench_turns() -> None:
    async def run() -> None:
        service = _AgentService()
        service.active = True
        service.runtime_active = True
        controller = _controller(service)

        async def refresh(_backend: str, forced: bool) -> None:
            assert forced is True
            service.runtime_active = False

        coordinator = BackendRestartCoordinator(controller, refresh, drain_timeout=0, poll_interval=0.001)

        await coordinator.request_restart("opencode")
        await coordinator.wait("opencode")

        controller.session_turns.release_for_backend_refresh.assert_awaited_once_with(
            backend="opencode",
            base_session_ids={"ses-1"},
        )
        assert service.draining is False

    asyncio.run(run())


def test_concurrent_restart_requests_coalesce() -> None:
    async def run() -> None:
        service = _AgentService()
        service.active = True
        controller = _controller(service)
        refresh = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, refresh, drain_timeout=1, poll_interval=0.001)

        first, second = await asyncio.gather(
            coordinator.request_restart("opencode"),
            coordinator.request_restart("opencode"),
        )
        assert first == second == "draining"
        controller.session_turns.begin_backend_drain.assert_called_once_with("opencode")

        service.active = False
        await coordinator.wait("opencode")
        refresh.assert_awaited_once()

    asyncio.run(run())


def test_refresh_failure_reopens_barrier() -> None:
    async def run() -> None:
        service = _AgentService()
        service.active = True
        controller = _controller(service)
        refresh = AsyncMock(side_effect=RuntimeError("refresh failed"))
        coordinator = BackendRestartCoordinator(controller, refresh, drain_timeout=1, poll_interval=0.001)

        await coordinator.request_restart("opencode")
        service.active = False
        with pytest.raises(RuntimeError, match="refresh failed"):
            await coordinator.wait("opencode")

        assert service.draining is False
        controller.session_turns.end_backend_drain.assert_awaited_once_with("opencode", resume_deferred=False)

    asyncio.run(run())


def test_idle_refresh_failure_is_propagated_before_ack() -> None:
    async def run() -> None:
        service = _AgentService()
        controller = _controller(service)
        refresh = AsyncMock(side_effect=RuntimeError("invalid config"))
        coordinator = BackendRestartCoordinator(controller, refresh, drain_timeout=1)

        with pytest.raises(RuntimeError, match="invalid config"):
            await coordinator.request_restart("opencode")

        assert service.draining is False
        controller.session_turns.end_backend_drain.assert_awaited_once_with(
            "opencode",
            resume_deferred=False,
        )

    asyncio.run(run())


def test_controller_reconciles_requested_backends_once_in_order() -> None:
    async def run() -> None:
        coordinator = SimpleNamespace(request_restart=AsyncMock(side_effect=["restarted", "draining"]))
        controller = SimpleNamespace(backend_restart_coordinator=coordinator)

        result = await Controller.reconcile_agent_backends(
            controller,
            ["codex", "codex", "opencode"],
        )

        assert result == {
            "ok": True,
            "backends": ["codex", "opencode"],
            "states": {"codex": "restarted", "opencode": "draining"},
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


def test_application_projection_tracks_prepare_drain_failure_retry_and_registration():
    async def run():
        service = _AgentService()
        service.agents = {"opencode": object(), "claude": object()}
        controller = _controller(service)
        refresh = AsyncMock()
        coordinator = BackendRestartCoordinator(controller, refresh, poll_interval=0.001)
        assert coordinator.snapshot("opencode") == {"state": "applied"}
        assert coordinator.snapshot("codex") == {"state": "unavailable"}

        preparing = asyncio.Event()
        release = asyncio.Event()

        async def prepare(_backend):
            preparing.set()
            await release.wait()

        service.prepare_backend_restart = prepare
        request = asyncio.create_task(coordinator.request_restart("opencode"))
        await preparing.wait()
        assert coordinator.snapshot("opencode") == {"state": "draining"}
        assert coordinator.snapshot("claude") == {"state": "applied"}
        service.active = True
        release.set()
        assert await request == "draining"
        assert coordinator.snapshot("opencode") == {"state": "draining"}
        refresh.side_effect = RuntimeError("application failed")
        service.active = False
        with pytest.raises(RuntimeError, match="application failed"):
            await coordinator.wait("opencode")
        assert coordinator.snapshot("opencode") == {"state": "failed", "error": "application failed"}
        assert coordinator.snapshot("claude") == {"state": "applied"}

        refresh.side_effect = None
        assert await coordinator.request_restart("opencode") == "restarted"
        assert coordinator.snapshot("opencode") == {"state": "applied"}
        service.agents.pop("opencode")
        assert coordinator.snapshot("opencode") == {"state": "unavailable"}

    asyncio.run(run())


@pytest.mark.parametrize("failure", [RuntimeError("prepare failed"), asyncio.CancelledError()])
def test_application_projection_retains_prepare_failure_and_cancellation(failure):
    async def run():
        service = _AgentService()
        service.agents = {"opencode": object()}
        service.prepare_backend_restart = AsyncMock(side_effect=failure)
        controller = _controller(service)
        coordinator = BackendRestartCoordinator(controller, AsyncMock())
        with pytest.raises(type(failure)):
            await coordinator.request_restart("opencode")
        assert coordinator.snapshot("opencode")["state"] == "failed"
        assert not service.draining
        controller.session_turns.end_backend_drain.assert_awaited_once_with("opencode", resume_deferred=False)

    asyncio.run(run())


def test_cancelled_application_remains_failed_after_task_is_removed():
    async def run():
        service = _AgentService()
        service.agents = {"opencode": object()}
        service.active = True
        coordinator = BackendRestartCoordinator(_controller(service), AsyncMock())
        await coordinator.request_restart("opencode")
        await asyncio.sleep(0)
        task = coordinator._tasks["opencode"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert coordinator.snapshot("opencode") == {"state": "failed", "error": "cancelled"}
        assert not service.draining

    asyncio.run(run())
