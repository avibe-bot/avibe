from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.inbox_events import RUNS_UPDATED_EVENT, bus
from modules.im import MessageContext
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput, stop_output_for
from modules.agents.service import AgentService


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
            dispatcher._record_agent_run_terminal_result(
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
            dispatcher._record_agent_run_terminal_result(
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
            dispatcher._record_agent_run_terminal_result(
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
