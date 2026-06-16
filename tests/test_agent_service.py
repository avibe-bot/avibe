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

    def runtime_turn_keys(self):
        return self.runtime_keys


class _StopRuntimeAgent(_RuntimeAgent):
    def __init__(self, reason: str):
        super().__init__()
        self.reason = reason

    async def handle_stop(self, request):
        request.stop_failure_reason = self.reason
        return False


class _RefreshingRuntimeAgent(_ClearingRuntimeAgent):
    def __init__(self, runtime_keys: set[str]):
        super().__init__(runtime_keys)
        self.refresh_calls: list[object] = []

    async def refresh_runtime_config(self, runtime_config):
        self.refresh_calls.append(runtime_config)


class _Controller:
    def __init__(self):
        self.session_turns = None


class _FailingTurnManager:
    def on_running(self, _context):
        raise RuntimeError("status failed")


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
            gate.backend = "codex"

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
        gate.backend = "codex"

        async def _on_clear():
            service.release_runtime_turn_key(runtime_key, "old-token")
            await gate.lock.acquire()
            gate.token = "new-token"
            gate.backend = "codex"

        agent = _ClearingRuntimeAgent({runtime_key}, on_clear=_on_clear)
        service.register(agent)

        cleared = await service.clear_sessions("scope-1")

        assert cleared == {"codex": 1}
        assert service._turn_gates[runtime_key].lock.locked()
        assert service._turn_gates[runtime_key].token == "new-token"

        service.release_runtime_turn_key(runtime_key, "new-token")

    asyncio.run(_run())


def test_agent_service_marks_runtime_started_from_matching_context_only() -> None:
    service = AgentService(controller=_Controller())
    runtime_key = "session:/repo"
    gate = service._get_turn_gate(runtime_key)
    gate.token = "runtime-token"
    gate.backend = "claude"
    context = SimpleNamespace(
        platform_specific={
            "agent_runtime_turn_key": runtime_key,
            "agent_runtime_turn_token": "runtime-token",
        }
    )

    service.mark_runtime_turn_started(context)

    assert gate.runtime_started is True

    stale_context = SimpleNamespace(
        platform_specific={
            "agent_runtime_turn_key": runtime_key,
            "agent_runtime_turn_token": "old-token",
        }
    )
    gate.runtime_started = False

    service.mark_runtime_turn_started(stale_context)

    assert gate.runtime_started is False


def test_agent_service_clear_backend_sessions_does_not_release_other_backend_gate() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        runtime_key = "session:/repo"
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "claude-token"
        gate.backend = "claude"
        agent = _ClearingRuntimeAgent({runtime_key})
        service.register(agent)

        count = await service.clear_backend_sessions("codex", "scope-1")

        assert count == 1
        assert gate.lock.locked()
        assert gate.token == "claude-token"
        service.release_runtime_turn_key(runtime_key, "claude-token")

    asyncio.run(_run())


def test_agent_service_refresh_runtime_config_releases_backend_gates() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        runtime_key = "session:/repo"
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "refresh-token"
        gate.backend = "codex"
        agent = _RefreshingRuntimeAgent({runtime_key})
        service.register(agent)
        runtime_config = object()

        handled = await service.refresh_runtime_config("codex", runtime_config)

        assert handled is True
        assert agent.refresh_calls == [runtime_config]
        assert not gate.lock.locked()
        assert gate.token == ""
        assert gate.backend == ""

    asyncio.run(_run())


def test_agent_service_releases_runtime_gate_for_stale_stop() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _StopRuntimeAgent("not_active")
        service.register(agent)
        request = _request("stop")
        gate = service._get_turn_gate("session:/repo")
        await gate.lock.acquire()
        gate.token = "stop-token"
        gate.backend = "claude"
        gate.runtime_started = True

        handled = await service.handle_stop("claude", request)

        assert handled is False
        assert not gate.lock.locked()
        assert request.context.platform_specific["agent_runtime_turn_token"] == "stop-token"

    asyncio.run(_run())


def test_agent_service_keeps_runtime_gate_for_startup_window_stop() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _StopRuntimeAgent("not_active")
        service.register(agent)
        request = _request("stop")
        gate = service._get_turn_gate("session:/repo")
        await gate.lock.acquire()
        gate.token = "stop-token"
        gate.backend = "codex"
        gate.runtime_started = False

        handled = await service.handle_stop("claude", request)

        assert handled is False
        assert gate.lock.locked()
        assert gate.token == "stop-token"
        service.release_runtime_turn_key("session:/repo", "stop-token")

    asyncio.run(_run())


def test_agent_service_keeps_runtime_gate_for_interrupt_failure_stop() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _StopRuntimeAgent("interrupt_failed")
        service.register(agent)
        request = _request("stop")
        gate = service._get_turn_gate("session:/repo")
        await gate.lock.acquire()
        gate.token = "stop-token"
        gate.backend = "claude"
        gate.runtime_started = True

        handled = await service.handle_stop("claude", request)

        assert handled is False
        assert gate.lock.locked()
        service.release_runtime_turn_key("session:/repo", "stop-token")

    asyncio.run(_run())


def test_agent_service_releases_gate_when_on_running_fails() -> None:
    async def _run():
        controller = _Controller()
        controller.session_turns = _FailingTurnManager()
        service = AgentService(controller=controller)
        agent = _RuntimeAgent()
        service.register(agent)
        request = _request("hello")

        try:
            await service.handle_message("claude", request)
        except RuntimeError as err:
            assert str(err) == "status failed"
        else:
            raise AssertionError("on_running failure should escape")

        gate = service._turn_gates["session:/repo"]
        assert not gate.lock.locked()
        assert gate.token == ""
        assert gate.runtime_started is False
        assert agent.started == []

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
