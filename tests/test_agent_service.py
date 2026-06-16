from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.agents.service import AgentService


class _RuntimeAgent:
    name = "claude"

    def __init__(self, release_first: asyncio.Event | None = None):
        self.started: list[str] = []
        self.release_first = release_first

    def runtime_turn_key(self, request):
        return request.composite_session_id

    async def handle_message(self, request):
        self.started.append(request.message)
        if request.message == "first" and self.release_first is not None:
            await self.release_first.wait()

    async def clear_sessions(self, _session_key):
        return 0

    async def handle_stop(self, _request):
        return False


class _Controller:
    def __init__(self):
        self.session_turns = None


def _request(message: str, runtime_key: str = "session:/repo"):
    return SimpleNamespace(
        context=SimpleNamespace(platform_specific={}),
        message=message,
        composite_session_id=runtime_key,
    )


def test_agent_service_dispatches_runtime_config_refresh() -> None:
    service = AgentService(controller=SimpleNamespace())
    runtime_config = object()
    agent = SimpleNamespace(name="codex", refresh_runtime_config=AsyncMock())
    service.register(agent)

    handled = asyncio.run(service.refresh_runtime_config("codex", runtime_config))

    assert handled is True
    agent.refresh_runtime_config.assert_awaited_once_with(runtime_config)


def test_agent_service_reports_missing_runtime_refresh_contract() -> None:
    service = AgentService(controller=SimpleNamespace())
    service.register(SimpleNamespace(name="codex"))

    assert asyncio.run(service.refresh_runtime_config("codex", object())) is False
    assert asyncio.run(service.refresh_runtime_config("claude", object())) is False


def test_agent_service_serializes_same_runtime_until_terminal_release() -> None:
    async def _run():
        controller = _Controller()
        service = AgentService(controller=controller)
        controller.agent_service = service
        release_first = asyncio.Event()
        agent = _RuntimeAgent(release_first)
        service.register(agent)

        first_request = _request("first")
        first = asyncio.create_task(service.handle_message("claude", first_request))
        await asyncio.sleep(0)
        assert agent.started == ["first"]

        second_request = _request("second")
        second = asyncio.create_task(service.handle_message("claude", second_request))
        await asyncio.sleep(0.05)
        assert agent.started == ["first"]

        service.release_runtime_turn(first_request.context)
        release_first.set()
        await asyncio.wait_for(first, timeout=3)
        await asyncio.wait_for(second, timeout=3)

        assert agent.started == ["first", "second"]

    asyncio.run(_run())


def test_agent_service_allows_distinct_runtime_keys_in_parallel() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _RuntimeAgent()
        service.register(agent)

        await asyncio.gather(
            service.handle_message("claude", _request("one", "one:/repo")),
            service.handle_message("claude", _request("two", "two:/repo")),
        )

        assert sorted(agent.started) == ["one", "two"]

    asyncio.run(_run())


def test_agent_service_runtime_guard_drops_stale_emits_after_next_turn_starts() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _RuntimeAgent()
        service.register(agent)
        first = _request("first")
        second = _request("second")

        await service.handle_message("claude", first)
        assert service.emit_matches_runtime_turn(first.context)
        service.release_runtime_turn(first.context)

        await service.handle_message("claude", second)

        assert service.emit_matches_runtime_turn(second.context)
        assert not service.emit_matches_runtime_turn(first.context)

    asyncio.run(_run())
