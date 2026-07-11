from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.backend_restart import BackendRestartCoordinator


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
