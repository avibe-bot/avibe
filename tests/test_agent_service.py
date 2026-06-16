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


class _RaisingRuntimeAgent(_RuntimeAgent):
    async def handle_message(self, _request):
        raise RuntimeError("backend failed")


class _ClearingRuntimeAgent:
    name = "codex"

    def __init__(self, runtime_keys: set[str], on_clear=None):
        self.runtime_keys = runtime_keys
        self.clear_calls: list[str] = []
        self.on_clear = on_clear

    async def clear_sessions(self, session_key):
        self.clear_calls.append(session_key)
        if self.on_clear is not None:
            await self.on_clear()
        return 1

    def runtime_turn_keys_for_session_key(self, _session_key):
        return self.runtime_keys


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


def test_agent_service_falls_back_to_request_runtime_key_for_legacy_agent_stubs() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        handled = []

        async def _handle_message(request):
            handled.append(request)

        service.register(SimpleNamespace(name="legacy", handle_message=_handle_message))
        request = _request("hello", "legacy:/repo")

        await service.handle_message("legacy", request)

        assert handled == [request]
        assert request.context.platform_specific["agent_runtime_turn_key"] == "legacy:/repo"
        service.release_runtime_turn(request.context)

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


def test_agent_service_clear_sessions_releases_cleared_runtime_gates() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _ClearingRuntimeAgent({"session:/repo", "session:/other"})
        service.register(agent)

        for runtime_key in ("session:/repo", "session:/other", "unrelated:/repo"):
            gate = service._get_turn_gate(runtime_key)
            await gate.lock.acquire()
            gate.token = f"{runtime_key}-token"

        cleared = await service.clear_sessions("scope-1")

        assert cleared == {"codex": 1}
        assert agent.clear_calls == ["scope-1"]
        assert not service._turn_gates["session:/repo"].lock.locked()
        assert not service._turn_gates["session:/other"].lock.locked()
        assert service._turn_gates["unrelated:/repo"].lock.locked()

        service.release_runtime_turn_key("unrelated:/repo")

    asyncio.run(_run())


def test_agent_service_clear_sessions_does_not_release_new_turn_token() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        runtime_key = "session:/repo"
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "old-token"

        async def _on_clear():
            service.release_runtime_turn_key(runtime_key, "old-token")
            await gate.lock.acquire()
            gate.token = "new-token"

        agent = _ClearingRuntimeAgent({runtime_key}, on_clear=_on_clear)
        service.register(agent)

        cleared = await service.clear_sessions("scope-1")

        assert cleared == {"codex": 1}
        assert service._turn_gates[runtime_key].lock.locked()
        assert service._turn_gates[runtime_key].token == "new-token"

        service.release_runtime_turn_key(runtime_key, "new-token")

    asyncio.run(_run())


def test_agent_service_releases_gate_when_exception_terminal_emit_fails() -> None:
    async def _run():
        controller = _Controller()

        async def _emit(*_args, **_kwargs):
            raise RuntimeError("send failed")

        controller.emit_agent_message = _emit
        service = AgentService(controller=controller)
        agent = _RaisingRuntimeAgent()
        service.register(agent)
        request = _request("boom")

        try:
            await service.handle_message("claude", request)
        except RuntimeError as err:
            assert str(err) == "backend failed"
        else:
            raise AssertionError("backend exception should escape")

        assert not service._turn_gates["session:/repo"].lock.locked()

    asyncio.run(_run())
