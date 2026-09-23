from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.im import MessageContext
from core.message_dispatcher import ConsolidatedMessageDispatcher


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
