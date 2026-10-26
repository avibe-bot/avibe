from __future__ import annotations

import asyncio
import inspect
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.inbox_events import RUNS_UPDATED_EVENT, bus
from modules.im import MessageContext
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput, stop_output_for
from modules.agents.service import AgentService
from storage.background import normalize_run_status


def test_close_after_releases_the_owned_runtime_after_terminal_result() -> None:
    controller = SimpleNamespace()
    controller.agent_service = AgentService(controller)
    dispatcher = ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "claude",
            "close_after": True,
            "agent_runtime_turn_key": "runtime-1",
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
            release_runtime_turn=lambda _context: release_order.append("release"),
            reserve_idle_close_after_teardown=AsyncMock(
                return_value=("runtime-1", "close-after:test", None)
            ),
            release_runtime_turn_key=lambda *_args: None,
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
            "agent_runtime_turn_key": "runtime-1",
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
            dispatcher._release_runtime_turn(context, MessageOutput(completes_turn=True))
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
                "_close_after_runtime_pending": True,
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
            dispatcher._release_runtime_turn(context, MessageOutput(completes_turn=True))
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


def test_close_after_waits_for_backend_cleanup_after_terminal_delivery() -> None:
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
                "agent_backend": "opencode",
                "close_after": True,
                "agent_session_target": {
                    "agent_backend": "opencode",
                    "session_anchor": "base-session-1",
                },
                "agent_runtime_turn_key": "runtime-1",
                "agent_runtime_turn_token": "turn-1",
                "_close_after_runtime_pending": True,
            },
        )
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        gate.token = "turn-1"
        gate.backend = "opencode"
        backend_finished = asyncio.Event()
        backend_task = asyncio.create_task(backend_finished.wait())
        gate.task = backend_task
        end_running_agent = AsyncMock(return_value={"ok": True})

        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(context, MessageOutput(completes_turn=True))
            await asyncio.sleep(0.01)
            assert gate.lock.locked()
            end_running_agent.assert_not_awaited()
            backend_finished.set()
            await backend_task
            await asyncio.sleep(0.01)
            end_running_agent.assert_awaited_once()
            assert not gate.lock.locked()

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
                "_close_after_runtime_pending": True,
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
            dispatcher._release_runtime_turn(context, MessageOutput(completes_turn=True))
            await asyncio.wait_for(successor, timeout=1)
            assert dispatcher._close_after_runtime_tasks == set()
            end_running_agent.assert_not_awaited()
            gate.lock.release()

    asyncio.run(exercise())


def test_close_after_does_not_reserve_over_durable_successor() -> None:
    async def exercise() -> None:
        successor_promoted = False

        def on_native_terminal(_context, *, outcome):
            nonlocal successor_promoted
            assert outcome == "terminal"
            successor_promoted = True

        def has_successor(session_id, completed_turn_id):
            assert (session_id, completed_turn_id) == ("session-1", "logical-1")
            return successor_promoted

        controller = SimpleNamespace(
            session_turns=SimpleNamespace(
                on_native_terminal=on_native_terminal,
                has_close_after_successor=has_successor,
            )
        )
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
                "turn_token": "logical-1",
                "agent_runtime_turn_key": "runtime-1",
                "agent_runtime_turn_token": "native-1",
                "agent_session_target": {
                    "agent_backend": "codex",
                    "session_anchor": "base-session-1",
                },
                "_close_after_runtime_pending": True,
            },
        )
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        gate.token = "native-1"
        end_running_agent = AsyncMock(return_value={"ok": True})
        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(
                context, MessageOutput(completes_turn=True)
            )
            assert successor_promoted
            assert not gate.lock.locked()
            await asyncio.sleep(0)
            end_running_agent.assert_not_awaited()
            assert dispatcher._close_after_runtime_tasks == set()

    asyncio.run(exercise())


def test_close_after_preserves_runtime_when_durable_probe_fails() -> None:
    def unreadable_successor(_session_id, _completed_turn_id):
        raise RuntimeError("database unavailable")

    async def exercise() -> None:
        controller = SimpleNamespace(
            session_turns=SimpleNamespace(
                on_native_terminal=lambda *_args, **_kwargs: None,
                has_close_after_successor=unreadable_successor,
            )
        )
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
                "agent_runtime_turn_key": "runtime-1",
                "agent_runtime_turn_token": "native-1",
                "_close_after_runtime_pending": True,
                "agent_session_target": {
                    "agent_backend": "codex",
                    "session_anchor": "base-session-1",
                },
            },
        )
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        gate.token = "native-1"
        end_running_agent = AsyncMock(return_value={"ok": True})
        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(
                context, MessageOutput(completes_turn=True)
            )
            assert not gate.lock.locked()
            await asyncio.sleep(0)
            end_running_agent.assert_not_awaited()

    asyncio.run(exercise())


def test_deferred_close_after_skips_durable_successor() -> None:
    controller = SimpleNamespace(
        session_turns=SimpleNamespace(has_close_after_successor=lambda *_args: True)
    )
    controller.agent_service = AgentService(controller)
    dispatcher = ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "codex",
            "close_after": True,
            "agent_runtime_turn_key": "runtime-1",
            "agent_session_target": {
                "agent_backend": "codex",
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
            dispatcher._schedule_close_after_runtime(context)
            await dispatcher.drain_close_after_runtime()

    asyncio.run(exercise())
    end_running_agent.assert_not_awaited()


def test_detached_close_after_reserves_idle_gate_before_teardown() -> None:
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
                "agent_runtime_turn_token": "old-turn",
            },
        )
        gate = service._get_turn_gate("runtime-1")
        closing = asyncio.Event()
        finish_close = asyncio.Event()

        async def end_running_agent(*_args, **_kwargs):
            assert gate.lock.locked()
            assert gate.token.startswith("close-after:")
            closing.set()
            await finish_close.wait()
            return {"ok": True}

        with (
            patch("core.message_dispatcher.SQLiteBackgroundTaskStore"),
            patch(
                "core.services.running_agents.end_running_agent",
                new=end_running_agent,
            ),
        ):
            dispatcher._terminal_agent_run_ids = lambda *_args: ["run-1"]
            dispatcher._record_agent_run_terminal_for_ids = lambda **_kwargs: None
            await dispatcher._record_agent_run_terminal_result(
                context,
                "done",
                None,
                is_error=False,
                output_semantics=MessageOutput(
                    completes_turn=False,
                    completes_run=True,
                    detached=True,
                ),
            )
            await asyncio.wait_for(closing.wait(), timeout=1)
            successor = asyncio.create_task(gate.lock.acquire())
            await asyncio.sleep(0)
            assert not successor.done()
            finish_close.set()
            await asyncio.wait_for(successor, timeout=1)
            gate.lock.release()

    asyncio.run(exercise())


def test_failed_run_write_does_not_arm_close_after() -> None:
    async def exercise(detached: bool) -> None:
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
        output = MessageOutput(
            completes_turn=not detached,
            completes_run=True,
            detached=detached,
        )
        end_running_agent = AsyncMock(return_value={"ok": True})

        with (
            patch("core.message_dispatcher.SQLiteBackgroundTaskStore"),
            patch("core.services.running_agents.end_running_agent", new=end_running_agent),
        ):
            dispatcher._terminal_agent_run_ids = lambda *_args: ["run-1"]
            dispatcher._record_agent_run_terminal_for_ids = Mock(
                side_effect=RuntimeError("SQLite write failed")
            )
            await dispatcher._record_agent_run_terminal_result(
                context,
                "done",
                None,
                is_error=False,
                output_semantics=output,
            )
            if not detached:
                dispatcher._release_runtime_turn(context, output)
            await asyncio.sleep(0)

        assert not context.platform_specific.get("_close_after_runtime_pending")
        assert dispatcher._close_after_runtime_tasks == set()
        end_running_agent.assert_not_awaited()
        if detached:
            gate.lock.release()

    asyncio.run(exercise(detached=False))
    asyncio.run(exercise(detached=True))


def test_blocking_activity_delays_close_after_until_run_settles() -> None:
    async def exercise() -> None:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        blocked = [True]
        service.activities.has_blocking_run_activity = lambda _run_id: blocked[0]
        run = {"status": "running"}
        controller.scheduled_task_service = SimpleNamespace(
            request_store=SimpleNamespace(get_run=lambda _run_id: run)
        )
        context = MessageContext(
            user_id="user",
            channel_id="session-1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "session-1",
                "agent_backend": "claude",
                "close_after": True,
                "task_execution_id": "run-1",
                "task_trigger_kind": "agent_run",
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
        recorded = []
        store = SimpleNamespace(
            record_turn_run_outputs=lambda _run_ids, **kwargs: recorded.append(kwargs),
            close=lambda: None,
        )
        closed = asyncio.Event()

        async def end_running_agent(*_args, **_kwargs):
            closed.set()
            return {"ok": True}

        output = MessageOutput(completes_turn=True, completes_run=True)
        with (
            patch("core.message_dispatcher.SQLiteBackgroundTaskStore", return_value=store),
            patch("core.services.running_agents.end_running_agent", new=end_running_agent),
        ):
            await dispatcher._record_agent_run_terminal_result(
                context, "done", None, is_error=False, output_semantics=output
            )
            assert recorded[0]["deferred_run_ids"] == ["run-1"]
            dispatcher._release_runtime_turn(context, output)
            await asyncio.sleep(0)
            assert not gate.lock.locked()
            assert not closed.is_set()

            run["status"] = "succeeded"
            bus.publish(RUNS_UPDATED_EVENT, {"run_id": "run-1", "status": "succeeded"})
            await asyncio.sleep(0)
            assert not closed.is_set()

            blocked[0] = False
            bus.publish(RUNS_UPDATED_EVENT, {"run_id": "run-1", "status": "succeeded"})
            await asyncio.wait_for(closed.wait(), timeout=1)

    asyncio.run(exercise())


def test_prewrite_cancel_waits_for_durable_run_settlement() -> None:
    async def exercise() -> None:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        run = {"status": "running"}
        controller.scheduled_task_service = SimpleNamespace(
            request_store=SimpleNamespace(get_run=lambda _run_id: run)
        )
        context = MessageContext(
            user_id="user",
            channel_id="session-1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "session-1",
                "agent_backend": "codex",
                "close_after": True,
                "task_execution_id": "run-1",
                "agent_session_target": {
                    "agent_backend": "codex",
                    "session_anchor": "base-session-1",
                },
                "agent_runtime_turn_key": "runtime-1",
            },
        )
        closed = asyncio.Event()

        async def end_running_agent(*_args, **_kwargs):
            closed.set()
            return {"ok": True}

        with patch(
            "core.services.running_agents.end_running_agent", new=end_running_agent
        ):
            dispatcher.defer_close_after_until_run_terminal(context)
            await asyncio.sleep(0)
            assert not closed.is_set()
            run["status"] = "canceled"
            bus.publish(RUNS_UPDATED_EVENT, {"run_id": "run-1", "status": "canceled"})
            await asyncio.wait_for(closed.wait(), timeout=1)

    asyncio.run(exercise())


def test_close_after_waits_for_backend_terminal_cleanup() -> None:
    async def exercise() -> None:
        controller = SimpleNamespace()
        controller.agent_service = AgentService(controller)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        cleanup = asyncio.Event()
        context = MessageContext(
            user_id="user",
            channel_id="session-1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "session-1",
                "agent_backend": "claude",
                "close_after": True,
                "agent_runtime_turn_key": "runtime-1",
                "agent_session_target": {
                    "agent_backend": "claude",
                    "session_anchor": "base-session-1",
                },
                "_close_after_backend_cleanup": cleanup,
            },
        )
        closed = asyncio.Event()

        async def end_running_agent(*_args, **_kwargs):
            closed.set()
            return {"ok": True}

        with patch(
            "core.services.running_agents.end_running_agent", new=end_running_agent
        ):
            dispatcher._schedule_close_after_runtime(context)
            drain = asyncio.create_task(dispatcher.drain_close_after_runtime())
            await asyncio.sleep(0)
            assert not closed.is_set()
            assert not drain.done()
            cleanup.set()
            await asyncio.wait_for(closed.wait(), timeout=1)
            await asyncio.wait_for(drain, timeout=1)

    asyncio.run(exercise())


def test_deferred_close_after_does_not_stop_successor_after_run_settles() -> None:
    async def exercise() -> None:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        run = {"status": "running"}
        controller.scheduled_task_service = SimpleNamespace(
            request_store=SimpleNamespace(get_run=lambda _run_id: run)
        )
        context = MessageContext(
            user_id="user",
            channel_id="session-1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "session-1",
                "agent_backend": "claude",
                "close_after": True,
                "task_execution_id": "run-1",
                "agent_session_target": {
                    "agent_backend": "claude",
                    "session_anchor": "base-session-1",
                },
                "agent_runtime_turn_key": "runtime-1",
            },
        )
        gate = service._get_turn_gate("runtime-1")
        end_running_agent = AsyncMock(return_value={"ok": True})

        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._schedule_close_after_runtime(
                context, wait_for_run_ids=("run-1",)
            )
            await asyncio.sleep(0)
            await gate.lock.acquire()
            gate.token = "successor-turn"
            run["status"] = "succeeded"
            bus.publish(RUNS_UPDATED_EVENT, {"run_id": "run-1", "status": "succeeded"})
            await asyncio.sleep(0.01)
            end_running_agent.assert_not_awaited()
            assert gate.token == "successor-turn"
            service.release_runtime_turn_key("runtime-1", "successor-turn")
            assert dispatcher._close_after_session_ids == set()

    asyncio.run(exercise())


def test_detached_close_after_does_not_stop_successor_that_won_gate() -> None:
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
            },
        )
        gate = service._get_turn_gate("runtime-1")
        end_running_agent = AsyncMock(return_value={"ok": True})

        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._schedule_close_after_runtime(context)
            await gate.lock.acquire()
            gate.token = "successor-turn"
            await asyncio.sleep(0.01)
            end_running_agent.assert_not_awaited()
            assert gate.token == "successor-turn"
            service.release_runtime_turn_key("runtime-1", "successor-turn")

    asyncio.run(exercise())


def test_idle_close_after_does_not_bypass_queued_successor() -> None:
    async def exercise() -> None:
        service = AgentService(SimpleNamespace())
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        successor = asyncio.create_task(gate.lock.acquire())
        await asyncio.sleep(0)
        gate.lock.release()

        assert await service.reserve_idle_close_after_teardown("runtime-1") is False
        await asyncio.wait_for(successor, timeout=1)
        gate.lock.release()

    asyncio.run(exercise())


def test_detached_close_after_without_runtime_identity_does_not_teardown() -> None:
    async def exercise() -> None:
        controller = SimpleNamespace()
        controller.agent_service = AgentService(controller)
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

        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._schedule_close_after_runtime(context)
            await asyncio.sleep(0.01)

        end_running_agent.assert_not_awaited()
        assert dispatcher._close_after_session_ids == set()

    asyncio.run(exercise())


def test_resultless_close_after_stop_schedules_teardown() -> None:
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(
            release_runtime_turn=lambda _context: None,
            reserve_idle_close_after_teardown=AsyncMock(
                return_value=("runtime-1", "close-after:test", None)
            ),
            release_runtime_turn_key=lambda *_args: None,
        )
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
            "agent_runtime_turn_key": "runtime-1",
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
            dispatcher._release_runtime_turn(context, stop_output_for(None))
            await asyncio.sleep(0.01)

    asyncio.run(exercise())
    end_running_agent.assert_awaited_once()


def test_resultless_close_after_requires_terminal_run_before_teardown() -> None:
    release = Mock()
    reserve = Mock()
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(
            release_runtime_turn=release,
            reserve_close_after_teardown=reserve,
            reserve_idle_close_after_teardown=AsyncMock(),
        )
    )
    dispatcher = ConsolidatedMessageDispatcher(controller)
    wait_for_runs = AsyncMock(return_value=False)
    dispatcher._wait_for_close_after_runs = wait_for_runs
    context = MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "claude",
            "close_after": True,
            "task_trigger_kind": "agent_run",
            "task_execution_id": "run-1",
            "agent_runtime_turn_key": "runtime-1",
            "agent_session_target": {
                "agent_backend": "claude",
                "session_anchor": "base-session-1",
            },
        },
    )
    end_running_agent = AsyncMock(return_value={"ok": True})

    async def exercise() -> None:
        with patch(
            "core.services.running_agents.end_running_agent", new=end_running_agent
        ):
            dispatcher._release_runtime_turn(context, stop_output_for(None))
            await dispatcher.drain_close_after_runtime()

    asyncio.run(exercise())
    release.assert_called_once_with(context)
    reserve.assert_not_called()
    wait_for_runs.assert_awaited_once_with(("run-1",))
    end_running_agent.assert_not_awaited()


def test_shutdown_drain_skips_teardown_for_unsettled_run() -> None:
    controller = SimpleNamespace(
        scheduled_task_service=SimpleNamespace(
            request_store=SimpleNamespace(
                get_run=lambda _run_id: {"status": "running"}
            )
        ),
        agent_service=SimpleNamespace(
            reserve_idle_close_after_teardown=AsyncMock(),
        ),
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
            "agent_runtime_turn_key": "runtime-1",
            "agent_session_target": {
                "agent_backend": "claude",
                "session_anchor": "base-session-1",
            },
        },
    )
    end_running_agent = AsyncMock(return_value={"ok": True})

    async def exercise() -> None:
        with patch(
            "core.services.running_agents.end_running_agent", new=end_running_agent
        ):
            dispatcher._schedule_close_after_runtime(
                context, wait_for_run_ids=("run-1",)
            )
            await asyncio.wait_for(dispatcher.drain_close_after_runtime(), timeout=1)

    asyncio.run(exercise())
    end_running_agent.assert_not_awaited()


def test_turn_only_result_keeps_close_after_runtime_for_activity_retry() -> None:
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
        end_running_agent = AsyncMock(return_value={"ok": True})

        with patch(
            "core.services.running_agents.end_running_agent",
            new=end_running_agent,
        ):
            dispatcher._release_runtime_turn(
                context,
                MessageOutput(completes_turn=True, completes_run=False),
            )
            await asyncio.sleep(0)

        assert not gate.lock.locked()
        assert dispatcher._close_after_runtime_tasks == set()
        end_running_agent.assert_not_awaited()

    asyncio.run(exercise())


def _agent_run_context(run_id: str) -> MessageContext:
    return MessageContext(
        user_id="user",
        channel_id="session-1",
        platform="avibe",
        platform_specific={
            "agent_session_id": "session-1",
            "agent_backend": "claude",
            "close_after": True,
            "task_execution_id": run_id,
            "task_trigger_kind": "agent_run",
            "agent_session_target": {
                "agent_backend": "claude",
                "session_anchor": "base-session-1",
            },
            "agent_runtime_turn_key": "runtime-1",
            "agent_runtime_turn_token": "turn-1",
        },
    )


async def _record_terminal(dispatcher: ConsolidatedMessageDispatcher, context: MessageContext, **kwargs) -> None:
    # The recorder became a coroutine when its write left the loop; called the same way on either side of that.
    result = dispatcher._record_agent_run_terminal_result(context, "done", None, **kwargs)
    if inspect.isawaitable(result):
        await result


def _blocking_store(writing: threading.Event, release: threading.Event, record=None) -> SimpleNamespace:
    def record_turn_run_outputs(run_ids, **kwargs):
        writing.set()
        assert release.wait(5)
        if record is not None:
            return record(run_ids, **kwargs)
        return None

    return SimpleNamespace(record_turn_run_outputs=record_turn_run_outputs, close=lambda: None)


def test_a_runs_terminal_record_never_holds_the_controller_loop() -> None:
    """#2706: a Turn's terminal result is recorded on its Runs in one SQLite write transaction, which waits while
    another process holds SQLite's writer. The emit runs on the controller loop, so the write runs off it: the loop
    keeps turning while the write waits, and the recorder returns once it lands."""

    async def exercise() -> None:
        controller = SimpleNamespace()
        controller.agent_service = AgentService(controller)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        writing, release = threading.Event(), threading.Event()
        threading.Timer(1.0, release.set).start()  # the other writer lets go, eventually
        with patch("core.message_dispatcher.SQLiteBackgroundTaskStore", return_value=_blocking_store(writing, release)):
            record = asyncio.create_task(
                _record_terminal(
                    dispatcher,
                    _agent_run_context("run-1"),
                    is_error=False,
                    output_semantics=MessageOutput(completes_turn=True, completes_run=True),
                )
            )
            started = time.monotonic()
            await asyncio.sleep(0.05)  # the recorder starts here, and must not hold the loop
            loop_lag = time.monotonic() - started - 0.05
            release.set()
            await record
        assert writing.is_set()
        assert loop_lag < 0.1, f"the controller loop was held {loop_lag:.2f}s by the Run terminal write"

    asyncio.run(exercise())


def test_a_cancellation_during_the_runs_terminal_record_is_answered_at_the_next_await() -> None:
    """#2706: the Run terminal record keeps the cancellation point it had on the loop. A cancellation (a Stop, a
    shutdown) that reaches the emitting task while the write runs off the loop waits for it: the Run row lands, the
    close-after tail runs, and the cancellation is delivered at the emitter's next await -- never between the write and
    the bookkeeping that depends on it."""

    async def exercise() -> list[str]:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = _agent_run_context("run-1")
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        gate.token = "turn-1"
        gate.backend = "claude"
        writing, release = threading.Event(), threading.Event()
        recorded: list[dict] = []
        store = _blocking_store(writing, release, record=lambda _run_ids, **kwargs: recorded.append(kwargs))
        outcomes: list[str] = []

        async def emit() -> None:
            await _record_terminal(
                dispatcher,
                context,
                is_error=False,
                output_semantics=MessageOutput(completes_turn=True, completes_run=True),
            )
            outcomes.append("recorded")
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                outcomes.append("cancelled at the next await")
                raise

        with patch("core.message_dispatcher.SQLiteBackgroundTaskStore", return_value=store):
            emitter = asyncio.create_task(emit())
            assert await asyncio.to_thread(writing.wait, 5)
            emitter.cancel()
            await asyncio.sleep(0.05)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await emitter
        assert recorded, "the Run write did not land"
        assert context.platform_specific.get("_close_after_runtime_pending") is True, "close-after was not armed"
        gate.lock.release()
        return outcomes

    assert asyncio.run(exercise()) == ["recorded", "cancelled at the next await"]


def test_an_activity_started_while_the_runs_terminal_record_waits_still_defers_the_run(tmp_path, monkeypatch) -> None:
    """#2706: the Run terminal record runs off the loop, so an owned Activity can start -- durably -- while it waits
    for SQLite's writer, after the loop's in-memory blocking check. The record re-reads the durable Activity rows inside
    its own transaction: the Run's terminal intent is deferred while that Activity is open, and close-after waits for
    the Run, exactly as when the Activity had started before the record."""

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore
    from storage.db import create_sqlite_engine
    from storage.session_activities import SQLiteSessionActivityStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    queued = request_store.enqueue_agent_run(message="run", agent_backend="claude", session_id="session-1")
    assert request_store.claim(queued.id) is not None
    db_path = request_store.sqlite_backend.db_path
    writing, release = threading.Event(), threading.Event()

    class _WaitsForTheWriter(SQLiteBackgroundTaskStore):
        def record_turn_run_outputs(self, *args, **kwargs):
            writing.set()
            assert release.wait(5)
            return super().record_turn_run_outputs(*args, **kwargs)

    async def exercise() -> MessageContext:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = _agent_run_context(queued.id)
        gate = service._get_turn_gate("runtime-1")
        await gate.lock.acquire()
        gate.token = "turn-1"
        gate.backend = "claude"
        with patch("core.message_dispatcher.SQLiteBackgroundTaskStore", new=_WaitsForTheWriter):
            record = asyncio.create_task(
                _record_terminal(
                    dispatcher,
                    context,
                    is_error=False,
                    output_semantics=MessageOutput(completes_turn=True, completes_run=True),
                )
            )
            assert await asyncio.to_thread(writing.wait, 5)
            # The receiver starts an Activity owned by the same Run; it persists before the record commits.
            SQLiteSessionActivityStore(create_sqlite_engine(db_path)).upsert_activity(
                {
                    "id": "activity-1",
                    "backend": "claude",
                    "runtime_key": "runtime-1",
                    "session_id": "session-1",
                    "run_id": queued.id,
                    "detached_from_run": False,
                    "metadata": {},
                },
                phase="active",
            )
            release.set()
            await record
        gate.lock.release()
        return context

    context = asyncio.run(exercise())
    assert normalize_run_status(request_store.get_run(queued.id)["status"]) == "running", (
        "the record settled a Run that an open Activity still owns"
    )
    assert context.platform_specific.get("_close_after_wait_for_run_ids") == (queued.id,), (
        "close-after does not wait for the Run the open Activity holds"
    )


def test_an_activity_that_settles_while_the_runs_terminal_record_waits_stays_first(tmp_path, monkeypatch) -> None:
    """#2706: the other order. An owned Activity that the loop saw open ends -- durably -- and settles the Run while
    the record waits for SQLite's writer. The Activity's settlement committed first, so it stays first: the Run keeps
    the Activity's outcome and the record adds only its output, exactly as when the Activity had ended before the result
    on the loop. Only an Activity the service ended settles a Run whose Turn is still running, so this one is a runtime
    replacement."""

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore
    from storage.db import create_sqlite_engine
    from storage.session_activities import SQLiteSessionActivityStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    queued = request_store.enqueue_agent_run(message="run", agent_backend="claude", session_id="session-1")
    assert request_store.claim(queued.id) is not None
    activity_store = SQLiteSessionActivityStore(create_sqlite_engine(request_store.sqlite_backend.db_path))
    activity_row = {
        "id": "activity-1",
        "backend": "claude",
        "runtime_key": "runtime-1",
        "session_id": "session-1",
        "run_id": queued.id,
        "detached_from_run": False,
        "metadata": {},
    }
    activity_store.upsert_activity(activity_row, phase="active")
    writing, release = threading.Event(), threading.Event()

    class _WaitsForTheWriter(SQLiteBackgroundTaskStore):
        def record_turn_run_outputs(self, *args, **kwargs):
            writing.set()
            assert release.wait(5)
            return super().record_turn_run_outputs(*args, **kwargs)

    async def exercise() -> None:
        controller = SimpleNamespace()
        service = AgentService(controller)
        controller.agent_service = service
        dispatcher = ConsolidatedMessageDispatcher(controller)
        scheduled = ScheduledTaskService(
            controller=controller,
            store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
            request_store=request_store,
        )
        blocked = [True]
        service.activities.has_blocking_run_activity = lambda _run_id: blocked[0]
        with patch("core.message_dispatcher.SQLiteBackgroundTaskStore", new=_WaitsForTheWriter):
            record = asyncio.create_task(
                _record_terminal(
                    dispatcher,
                    _agent_run_context(queued.id),
                    is_error=True,
                    terminal_error="the turn failed",
                    output_semantics=MessageOutput(completes_turn=True, completes_run=True),
                )
            )
            assert await asyncio.to_thread(writing.wait, 5)
            # The Activity ends on the loop and its Run owner settles it, before the record commits.
            blocked[0] = False
            activity_store.upsert_activity(activity_row, phase="awaiting_output")
            activity = SimpleNamespace(
                id="activity-1", status="killed", run_id=queued.id, metadata={"interrupt_reason": "backend_refresh"}
            )
            assert scheduled.settle_activity_runs(activity) == [queued.id]
            release.set()
            await record

    asyncio.run(exercise())
    settled = request_store.get_run(queued.id)
    assert settled["metadata"].get("interrupt_reason") == "backend_refresh", "the later record replaced the Activity's settlement"
    assert settled["error"] != "the turn failed", "the later record replaced the Activity's settlement"


def test_the_runs_terminal_record_reads_only_its_sessions_activities(tmp_path, monkeypatch) -> None:
    """#2706: the record re-reads open Activities under SQLite's writer, so that read must stay bounded by the Turn's
    work: it seeks the Turn's Session through ``ix_runtime_records_scope_anchor`` instead of walking every Activity row,
    and a malformed Activity row is skipped rather than failing every Run's terminal record."""

    import sqlite3

    from sqlalchemy import event

    from core.scheduled_tasks import TaskExecutionStore
    from storage.db import create_sqlite_engine
    from storage.session_activities import SQLiteSessionActivityStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    queued = request_store.enqueue_agent_run(message="run", agent_backend="claude", session_id="session-1")
    assert request_store.claim(queued.id) is not None
    store = request_store.sqlite_backend
    engine = create_sqlite_engine(store.db_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "insert into runtime_records (id, record_type, record_key, scope_id, session_anchor, workdir, payload_json,"
            " expires_at, created_at, updated_at) values ('runtime::session_activity::bad', 'session_activity', 'bad',"
            " null, 'session-1', null, '[]', null, '2026-10-16T00:00:00Z', '2026-10-16T00:00:00Z')"
        )
    SQLiteSessionActivityStore(engine).upsert_activity(
        {
            "id": "activity-elsewhere",
            "backend": "claude",
            "runtime_key": "runtime-2",
            "session_id": "session-2",
            "run_id": queued.id,
            "detached_from_run": False,
            "metadata": {},
        },
        phase="active",
    )
    statements: list[tuple[str, object]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if "runtime_records" in statement:
            statements.append((statement, parameters))

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        deferred = store.record_turn_terminal_outputs(
            [queued.id],
            activity_session_ids=["session-1"],
            output_id="terminal",
            text="done",
            terminal_status="succeeded",
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)

    assert deferred == [], "an Activity in another Session blocked this Turn's Run"
    assert normalize_run_status(request_store.get_run(queued.id)["status"]) == "succeeded"
    (statement, parameters), = statements
    with sqlite3.connect(store.db_path) as raw:
        plan = " ".join(row[-1] for row in raw.execute("EXPLAIN QUERY PLAN " + statement, parameters))
    assert "ix_runtime_records_scope_anchor" in plan and "SCAN runtime_records" not in plan, plan


def test_the_runs_terminal_record_binds_within_an_old_sqlite_variable_limit(tmp_path, monkeypatch) -> None:
    """#2706: a terminal output's Run ids have no size cap, and the record's Activity re-read looks up their Sessions.
    That lookup binds in batches like the rest of the record, so an output with more Run ids than an older SQLite
    build's 999 host parameters still records its Runs instead of failing with ``too many SQL variables``."""

    import sqlite3

    from sqlalchemy import event

    from core.scheduled_tasks import TaskExecutionStore

    if not hasattr(sqlite3.Connection, "setlimit"):
        pytest.skip("sqlite3.Connection.setlimit needs Python 3.11")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    queued = request_store.enqueue_agent_run(message="run", agent_backend="claude", session_id="session-1")
    assert request_store.claim(queued.id) is not None
    store = request_store.sqlite_backend

    def old_sqlite_limit(dbapi_connection, _record):
        dbapi_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

    event.listen(store.engine, "connect", old_sqlite_limit)
    store.engine.dispose()
    try:
        deferred = store.record_turn_terminal_outputs(
            [queued.id, *(f"run-gone-{index}" for index in range(1000))],
            activity_session_ids=["session-1"],
            output_id="terminal",
            text="done",
            terminal_status="succeeded",
        )
    finally:
        event.remove(store.engine, "connect", old_sqlite_limit)
        store.engine.dispose()

    assert deferred == []
    assert normalize_run_status(request_store.get_run(queued.id)["status"]) == "succeeded"
