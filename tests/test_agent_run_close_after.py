from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.im import MessageContext
from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.agents.service import AgentService


def test_close_after_releases_the_owned_runtime_after_terminal_result() -> None:
    controller = SimpleNamespace()
    dispatcher = ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "claude",
            "close_after": True,
            "agent_session_target": {
                "agent_backend": "claude",
                "session_anchor": "base-session-1",
            },
        },
    )
    end_running_agent = AsyncMock(return_value={"ok": True, "action": "ended"})

    async def exercise() -> None:
        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._schedule_close_after_runtime(context)
            await asyncio.sleep(0.01)

    asyncio.run(exercise())

    end_running_agent.assert_awaited_once_with(
        controller,
        backend="claude",
        session_id="session-1",
        base_session_id="base-session-1",
    )
    assert dispatcher._close_after_session_ids == set()


def test_close_after_waits_until_runtime_turn_release() -> None:
    release_order: list[str] = []
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(
            release_runtime_turn=lambda _context: release_order.append("release")
        )
    )
    dispatcher = ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "codex",
            "close_after": True,
            "agent_session_target": {
                "agent_backend": "codex",
                "session_anchor": "base-session-1",
            },
            "_close_after_runtime_pending": True,
        },
    )
    end_running_agent = AsyncMock(return_value={"ok": True, "action": "ended"})

    async def exercise() -> None:
        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(context)
            assert release_order == ["release"]
            await asyncio.sleep(0.01)

    asyncio.run(exercise())

    end_running_agent.assert_awaited_once_with(
        controller,
        backend="codex",
        session_id="session-1",
        base_session_id="base-session-1",
    )


def test_close_after_does_not_schedule_for_ordinary_runs() -> None:
    controller = SimpleNamespace()
    dispatcher = ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "claude",
        },
    )

    dispatcher._schedule_close_after_runtime(context)

    assert dispatcher._close_after_runtime_tasks == set()


def test_close_after_holds_runtime_gate_until_teardown_finishes() -> None:
    async def exercise() -> None:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="user",
            channel_id="session-1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "session-1",
                "agent_backend": "codex",
                "close_after": True,
                "agent_session_target": {
                    "agent_backend": "codex",
                    "session_anchor": "base-session-1",
                },
                "agent_runtime_turn_key": "runtime-1",
                "agent_runtime_turn_token": "turn-1",
            },
        )
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        gate.token = "turn-1"
        gate.backend = "codex"
        closing = asyncio.Event()
        finish_close = asyncio.Event()

        async def end_running_agent(*_args, **_kwargs):
            closing.set()
            await finish_close.wait()
            return {"ok": True}

        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(context)
            assert gate.lock.locked()
            assert gate.token.startswith("close-after:")
            await asyncio.wait_for(closing.wait(), timeout=1)
            successor = asyncio.create_task(gate.lock.acquire())
            await asyncio.sleep(0)
            assert not successor.done()
            finish_close.set()
            await asyncio.wait_for(successor, timeout=1)
            gate.lock.release()

    asyncio.run(exercise())


def test_close_after_does_not_close_when_successor_is_already_queued() -> None:
    async def exercise() -> None:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="user",
            channel_id="session-1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "session-1",
                "agent_backend": "claude",
                "close_after": True,
                "agent_session_target": {
                    "agent_backend": "claude",
                    "session_anchor": "base-session-1",
                },
                "agent_runtime_turn_key": "runtime-1",
                "agent_runtime_turn_token": "turn-1",
            },
        )
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        gate.token = "turn-1"
        gate.backend = "claude"
        successor = asyncio.create_task(gate.lock.acquire())
        await asyncio.sleep(0)
        end_running_agent = AsyncMock(return_value={"ok": True})
        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(context)
            await asyncio.wait_for(successor, timeout=1)
            assert dispatcher._close_after_runtime_tasks == set()
            end_running_agent.assert_not_awaited()
            gate.lock.release()

    asyncio.run(exercise())


def test_resultless_close_after_stop_schedules_teardown() -> None:
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(release_runtime_turn=lambda _context: None)
    )
    dispatcher = ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "claude",
            "close_after": True,
            "agent_session_target": {
                "agent_backend": "claude",
                "session_anchor": "base-session-1",
            },
        },
    )
    end_running_agent = AsyncMock(return_value={"ok": True})

    async def exercise() -> None:
        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(context)
            await asyncio.sleep(0.01)

    asyncio.run(exercise())
    end_running_agent.assert_awaited_once()
