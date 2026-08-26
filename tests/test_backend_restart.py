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


def test_joined_preflight_queues_follow_up_before_reopening_drain() -> None:
    async def run() -> None:
        service = _AgentService()
        controller = _controller(service)
        first_refresh_started = asyncio.Event()
        release_first_refresh = asyncio.Event()
        observed_drains: list[bool] = []
        observed_prepared: list[str] = []

        async def refresh(_backend: str, _forced: bool, prepared: object) -> None:
            observed_drains.append(service.draining)
            observed_prepared.append(str(prepared))
            if len(observed_drains) == 1:
                first_refresh_started.set()
                await release_first_refresh.wait()

        preflight = AsyncMock(side_effect=["generation-a", "generation-b"])
        coordinator = BackendRestartCoordinator(
            controller,
            refresh,
            preflight=preflight,
            drain_timeout=1,
        )

        first_request = asyncio.create_task(coordinator.request_restart("opencode"))
        await first_refresh_started.wait()
        assert await first_request == "draining"

        assert await coordinator.request_restart("opencode") == "draining"
        assert service.draining is True
        release_first_refresh.set()

        await coordinator.wait("opencode")
        assert observed_drains == [True, True]
        assert observed_prepared == ["generation-a", "generation-b"]
        assert preflight.await_count == 2
        controller.session_turns.end_backend_drain.assert_awaited_once_with(
            "opencode",
            resume_deferred=True,
        )
        assert service.draining is False

    asyncio.run(run())


def test_failed_joined_preflight_cannot_change_the_generation_being_refreshed() -> None:
    async def run() -> None:
        service = _AgentService()
        service.active = True
        controller = _controller(service)
        joined_preflight_started = asyncio.Event()
        release_joined_preflight = asyncio.Event()
        first_refresh_applied = asyncio.Event()
        prepared_generations: list[str] = []
        preflight_count = 0

        async def preflight(_backend: str) -> str:
            nonlocal preflight_count
            preflight_count += 1
            if preflight_count == 1:
                return "generation-a"
            joined_preflight_started.set()
            await release_joined_preflight.wait()
            raise RuntimeError("generation-b catalog failed")

        async def refresh(_backend: str, _forced: bool, prepared: object) -> None:
            prepared_generations.append(str(prepared))
            first_refresh_applied.set()

        coordinator = BackendRestartCoordinator(
            controller,
            refresh,
            preflight=preflight,
            drain_timeout=1,
            poll_interval=0.001,
        )

        assert await coordinator.request_restart("opencode") == "draining"
        joined = asyncio.create_task(coordinator.request_restart("opencode"))
        await joined_preflight_started.wait()

        service.active = False
        await first_refresh_applied.wait()
        release_joined_preflight.set()

        with pytest.raises(RuntimeError, match="generation-b catalog failed"):
            await joined
        await coordinator.wait("opencode")
        assert prepared_generations == ["generation-a"]
        assert service.draining is False

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


def test_active_restart_preflight_failure_is_propagated_before_ack() -> None:
    async def run() -> None:
        service = _AgentService()
        service.active = True
        controller = _controller(service)
        refresh = AsyncMock()
        preflight = AsyncMock(side_effect=RuntimeError("catalog export failed"))
        coordinator = BackendRestartCoordinator(
            controller,
            refresh,
            preflight=preflight,
            drain_timeout=1,
        )

        with pytest.raises(RuntimeError, match="catalog export failed"):
            await coordinator.request_restart("opencode")

        preflight.assert_awaited_once_with("opencode")
        refresh.assert_not_awaited()
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
