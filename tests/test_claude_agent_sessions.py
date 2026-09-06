import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.base import BaseAgent
from core.agent_input import AgentInputMetadata
from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    backend_dispatch_attempted,
    set_dispatch_phase,
)
from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.run_settlement import SETTLED_BY_BACKEND_REFRESH
from core.session_activities import SessionActivity, activity_completion_output
from core.services.agent_steering import ActiveSteerTarget, SteerOutcome, SteerRequest
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.service import AgentService
from modules.claude_sdk_compat import TextBlock, UserMessage


class _StubSessions:
    @staticmethod
    def list_agent_sessions(settings_key, agent_name):
        assert settings_key == "wechat-user"
        assert agent_name == "claude"
        return {"wechat_o9": "session-id"}

    @staticmethod
    def clear_agent_sessions(settings_key, agent_name):
        return None


class _StubSessionManager:
    def __init__(self):
        self.cleared = []
        self.session = SimpleNamespace(session_active={})

    async def clear_session(self, settings_key):
        self.cleared.append(settings_key)

    async def get_or_create_session(self, user_id, channel_id):
        return self.session


class _StubClient:
    def __init__(self):
        self.closed = False
        self.disconnected = False

    async def close(self):
        self.closed = True

    async def disconnect(self):
        self.disconnected = True


class _StubSettingsManager:
    sessions = _StubSessions()


class _StubController:
    def __init__(self):
        self.config = type("Config", (), {})()
        self.im_client = SimpleNamespace(formatter=SimpleNamespace())
        self.settings_manager = _StubSettingsManager()
        self.session_manager = _StubSessionManager()
        self.receiver_tasks = {}
        self.claude_sessions = {}
        self.claude_client = SimpleNamespace(_is_skip_message=lambda message: False)
        self.agent_auth_service = SimpleNamespace(maybe_emit_auth_recovery_message=AsyncMock(return_value=False))

        async def _cleanup_session(
            composite_key,
            *,
            current_receiver_task=None,
            activation_retired=False,
            reason="unspecified",
        ):
            del activation_retired, reason
            receiver_task = self.receiver_tasks.pop(composite_key, None)
            client = self.claude_sessions.pop(composite_key, None)
            cleanup_from_receiver = receiver_task is not None and receiver_task is current_receiver_task
            clear_tracking = getattr(self.session_handler, "clear_session_tracking", None)
            if callable(clear_tracking):
                clear_tracking(composite_key)
            try:
                if client is not None:
                    if cleanup_from_receiver:
                        async def _deferred_disconnect():
                            await client.disconnect()

                        asyncio.create_task(_deferred_disconnect())
                        return
                    await client.disconnect()
            finally:
                if receiver_task is not None and not cleanup_from_receiver:
                    if receiver_task.done():
                        try:
                            receiver_task.exception()
                        except asyncio.CancelledError:
                            pass
                    else:
                        receiver_task.cancel()
                        try:
                            await receiver_task
                        except asyncio.CancelledError:
                            pass

        self.session_handler = SimpleNamespace(
            cleanup_session=AsyncMock(side_effect=_cleanup_session),
            capture_session_id=lambda *args, **kwargs: None,
            touch_session_activity=lambda _key: None,
        )


class ClaudeAgentSessionTests(unittest.IsolatedAsyncioTestCase):
    def assert_backend_failure_emitted(self, emitter, context, display_text, diagnostic):
        self.assertEqual(emitter.await_count, 2)
        notify_call, terminal_call = emitter.await_args_list
        self.assertEqual(notify_call.args[:3], (context, "notify", display_text))
        self.assertFalse(notify_call.kwargs["output"].settles_run)
        self.assertEqual(terminal_call.args[:3], (context, "result", ""))
        self.assertTrue(terminal_call.kwargs["is_error"])
        self.assertEqual(terminal_call.kwargs["level"], "silent")
        self.assertEqual(terminal_call.kwargs["terminal_error"], diagnostic)

    def test_process_termination_notify_is_localized(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        controller.claude_sessions[runtime_key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-6)),
        )
        controller.session_handler = SimpleNamespace(
            _t=lambda key, **kwargs: (
                f"process terminated: {kwargs['reason']}"
                if key == "error.claudeProcessTerminated"
                else f"{kwargs['signal']} (signal {kwargs['number']})"
                if key == "error.claudeProcessSignal"
                else key
            ),
        )
        agent = ClaudeAgent(controller)

        assert agent._format_error_notify(
            RuntimeError("Cannot write to terminated process (exit code: -6)"),
            composite_key=runtime_key,
        ) == "❌ process terminated: SIGABRT (signal 6)"

    def test_process_termination_notify_uses_shared_i18n_fallback(self):
        controller = _StubController()
        controller.config.language = "zh"
        runtime_key = "wechat_o9:/tmp/work"
        controller.claude_sessions[runtime_key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-6)),
        )
        controller.session_handler = SimpleNamespace()
        agent = ClaudeAgent(controller)

        assert agent._format_error_notify(
            RuntimeError("Cannot write to terminated process (exit code: -6)"),
            composite_key=runtime_key,
        ) == "❌ Claude Code 进程已终止（SIGABRT（信号 6））；会话已重置，请重试。"

    async def test_handle_message_serializes_queries_for_same_runtime_session(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        first_query_started = asyncio.Event()
        release_result = asyncio.Event()
        queries: list[tuple[str, str]] = []

        class ResultMessage:
            subtype = "success"
            result = "done"
            duration_ms = 1

        class _Client:
            _vibe_runtime_base_session_id = "wechat_o9"
            _vibe_runtime_session_key = runtime_key

            async def query(self, message, *, session_id):
                queries.append((message, session_id))
                if len(queries) == 1:
                    first_query_started.set()

            def receive_messages(self):
                async def _iterate():
                    await release_result.wait()
                    yield ResultMessage()

                return _iterate()

        client = _Client()
        controller._get_session_key = lambda _context: "wechat-user"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=lambda _key: None,
            mark_session_idle=lambda _key: None,
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_args, **_kwargs: None,
            touch_session_activity=lambda _key: None,
        )

        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        async def _emit_result(context, *_args, **_kwargs):
            service.release_runtime_turn(context)

        agent.emit_result_message = AsyncMock(side_effect=_emit_result)

        def _request(message: str):
            return SimpleNamespace(
                context=SimpleNamespace(
                    user_id="U1",
                    channel_id="C1",
                    platform_specific={"turn_token": message},
                ),
                message=message,
                working_path="/tmp/work",
                base_session_id="wechat_o9",
                composite_session_id=runtime_key,
                session_key="wechat-user",
                subagent_name=None,
                subagent_model=None,
                subagent_reasoning_effort=None,
                ack_message_id=None,
                ack_reaction_message_id=None,
                ack_reaction_emoji=None,
                files=None,
            )

        first = asyncio.create_task(service.handle_message("claude", _request("first")))
        await asyncio.wait_for(first_query_started.wait(), timeout=3)
        await asyncio.sleep(0)
        second = asyncio.create_task(service.handle_message("claude", _request("second")))
        await asyncio.sleep(0.05)

        self.assertEqual(
            queries,
            [("first", runtime_key)],
            "the second prompt must not be written into the same Claude runtime before the first result",
        )

        release_result.set()
        await asyncio.wait_for(first, timeout=3)
        await asyncio.wait_for(second, timeout=3)

        self.assertEqual(queries, [("first", runtime_key), ("second", runtime_key)])

    async def test_cancelled_waiter_does_not_leak_runtime_session_lock(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        first_query_started = asyncio.Event()
        release_result = asyncio.Event()
        queries: list[tuple[str, str]] = []

        class ResultMessage:
            subtype = "success"
            result = "done"
            duration_ms = 1

        class _Client:
            _vibe_runtime_base_session_id = "wechat_o9"
            _vibe_runtime_session_key = runtime_key

            async def query(self, message, *, session_id):
                queries.append((message, session_id))
                if len(queries) == 1:
                    first_query_started.set()

            def receive_messages(self):
                async def _iterate():
                    await release_result.wait()
                    yield ResultMessage()

                return _iterate()

        client = _Client()
        controller._get_session_key = lambda _context: "wechat-user"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=lambda _key: None,
            mark_session_idle=lambda _key: None,
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_args, **_kwargs: None,
            touch_session_activity=lambda _key: None,
        )

        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        async def _emit_result(context, *_args, **_kwargs):
            service.release_runtime_turn(context)

        agent.emit_result_message = AsyncMock(side_effect=_emit_result)

        def _request(message: str):
            return SimpleNamespace(
                context=SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={}),
                message=message,
                working_path="/tmp/work",
                base_session_id="wechat_o9",
                composite_session_id=runtime_key,
                session_key="wechat-user",
                subagent_name=None,
                subagent_model=None,
                subagent_reasoning_effort=None,
                ack_message_id=None,
                ack_reaction_message_id=None,
                ack_reaction_emoji=None,
                files=None,
            )

        first = asyncio.create_task(service.handle_message("claude", _request("first")))
        await asyncio.wait_for(first_query_started.wait(), timeout=3)
        blocked = asyncio.create_task(service.handle_message("claude", _request("cancelled")))
        await asyncio.sleep(0.05)
        blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked

        release_result.set()
        await asyncio.wait_for(first, timeout=3)

        third = asyncio.create_task(service.handle_message("claude", _request("third")))
        await asyncio.wait_for(third, timeout=3)

        self.assertEqual(queries, [("first", runtime_key), ("third", runtime_key)])
        self.assertNotIn(runtime_key, agent._pending_requests)

    async def test_handle_message_receiver_eof_without_result_settles_current_turn(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        mark_active_calls = []
        mark_idle_calls = []
        queries = []

        class _Client:
            _vibe_runtime_base_session_id = "wechat_o9"
            _vibe_runtime_session_key = runtime_key

            async def query(self, message, *, session_id):
                queries.append((message, session_id))

            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    if False:
                        yield None

                return _iterate()

        client = _Client()
        controller._get_session_key = lambda _context: "wechat-user"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=lambda key: mark_active_calls.append(key),
            mark_session_idle=lambda key: mark_idle_calls.append(key),
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_args, **_kwargs: None,
            clear_session_tracking=lambda key: mark_idle_calls.append(f"clear:{key}"),
            touch_session_activity=lambda _key: None,
        )

        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()

        async def _emit(context, message_type, text, **_kwargs):
            if message_type == "result":
                service.release_runtime_turn(context)

        controller.emit_agent_message = AsyncMock(side_effect=_emit)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        request = SimpleNamespace(
            context=context,
            message="first",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id=runtime_key,
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            files=None,
        )

        await service.handle_message("claude", request)
        receiver_task = controller.receiver_tasks.get(runtime_key)
        if receiver_task is not None:
            await asyncio.wait_for(receiver_task, timeout=1)

        self.assertEqual(queries, [("first", runtime_key)])
        self.assertEqual(mark_active_calls, [runtime_key])
        self.assertIn(runtime_key, mark_idle_calls)
        controller.emit_agent_message.assert_awaited_once_with(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=ANY,
            terminal_error="Claude receiver ended without a terminal result",
        )
        agent._remove_ack_reaction.assert_awaited_once_with(request)
        self.assertNotIn(runtime_key, agent._pending_requests)
        self.assertNotIn(runtime_key, controller.claude_sessions)
        self.assertFalse(service._turn_gates[runtime_key].lock.locked())

    async def test_handle_stop_releases_runtime_turn_gate(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        interrupted = False

        class _Client:
            async def interrupt(self):
                nonlocal interrupted
                interrupted = True

            async def disconnect(self):
                return None

        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        controller.processing_indicator = SimpleNamespace(finish=AsyncMock())

        async def _emit(context, message_type, text, **_kwargs):
            if message_type == "result":
                service.release_runtime_turn(context)

        controller.emit_agent_message = AsyncMock(side_effect=_emit)
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "T1",
                    "agent_runtime_turn_key": runtime_key,
                    "agent_runtime_turn_token": "R1",
                }
            ),
            ack_reaction_message_id="m1",
            ack_reaction_emoji=":eyes:",
        )
        agent._pending_requests[runtime_key] = [pending_request]
        agent._pending_reactions[runtime_key] = [("m1", ":eyes:")]
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "R1"
        controller.claude_sessions[runtime_key] = _Client()

        request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            message="stop",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id=runtime_key,
            session_key="wechat-user",
            stop_failure_reason=None,
        )

        handled = await service.handle_stop("claude", request)

        self.assertTrue(handled)
        self.assertTrue(interrupted)
        self.assertNotIn(runtime_key, agent._pending_requests)
        self.assertNotIn(runtime_key, controller.claude_sessions)
        controller.session_handler.cleanup_session.assert_awaited_once_with(
            runtime_key,
            current_receiver_task=None,
            activation_retired=False,
            reason="user_stop",
        )
        self.assertFalse(service._turn_gates[runtime_key].lock.locked())
        self.assertEqual(request.context.platform_specific["turn_token"], "T1")
        self.assertEqual(request.context.platform_specific["agent_runtime_turn_token"], "R1")
        controller.processing_indicator.finish.assert_awaited_once_with(
            pending_request, terminal_emoji=STOPPED_REACTION_EMOJI
        )

    async def test_handle_stop_waits_for_in_flight_steering_write(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        interrupt_called = asyncio.Event()

        class _Client:

            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()

            async def end_input(self):
                return None

            async def interrupt(self):
                interrupt_called.set()

            async def disconnect(self):
                return None

        async def _receiver():
            await asyncio.Future()

        agent = ClaudeAgent(controller)
        controller.processing_indicator = SimpleNamespace(finish=AsyncMock())
        controller.emit_agent_message = AsyncMock()
        controller.session_handler.active_sessions = {runtime_key}
        pending_request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[runtime_key] = [pending_request]
        client = _Client()
        receiver_task = asyncio.create_task(_receiver())
        controller.claude_sessions[runtime_key] = client
        controller.receiver_tasks[runtime_key] = receiver_task
        target = ActiveSteerTarget(
            runtime_key=runtime_key,
            logical_turn_id="logical-turn",
            context=pending_request.context,
            agent_request=pending_request,
            agent=agent,
        )
        steer_request = SteerRequest(
            target_session_id="wechat_o9",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{runtime_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="steer while active",
        )
        stop_request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            composite_session_id=runtime_key,
            stop_failure_reason=None,
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(steer_request, target))
        await query_started.wait()
        stop_task = asyncio.create_task(agent.handle_stop(stop_request))
        await asyncio.sleep(0)
        self.assertFalse(interrupt_called.is_set())

        release_query.set()
        receipt = await steer_task
        stopped = await stop_task

        self.assertIs(receipt.outcome, SteerOutcome.ACCEPTED)
        self.assertTrue(stopped)
        self.assertTrue(interrupt_called.is_set())
        self.assertNotIn(runtime_key, agent._steering_locks)
        self.assertNotIn(runtime_key, agent._steering_generations)
        self.assertNotIn(runtime_key, agent._steering_closing)

    async def test_handle_stop_failure_keeps_runtime_nonsteerable(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"

        class _Client:
            async def interrupt(self):
                raise TimeoutError("interrupt acknowledgement timed out")

        async def _receiver():
            await asyncio.Future()

        agent = ClaudeAgent(controller)
        controller.emit_agent_message = AsyncMock()
        client = _Client()
        receiver_task = asyncio.create_task(_receiver())
        controller.claude_sessions[runtime_key] = client
        controller.receiver_tasks[runtime_key] = receiver_task
        controller.session_handler.active_sessions = {runtime_key}
        pending_request = SimpleNamespace(context=SimpleNamespace(platform_specific={}))
        agent._pending_requests[runtime_key] = [pending_request]
        target = ActiveSteerTarget(
            runtime_key=runtime_key,
            logical_turn_id="logical-turn",
            context=pending_request.context,
            agent_request=pending_request,
            agent=agent,
        )
        stop_request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            composite_session_id=runtime_key,
            stop_failure_reason=None,
        )

        try:
            self.assertIsNotNone(agent.steering_native_turn_id(target))
            self.assertFalse(await agent.handle_stop(stop_request))

            self.assertEqual(stop_request.stop_failure_reason, "interrupt_failed")
            self.assertNotIn(runtime_key, agent._steering_closing_keys())
            self.assertIn(runtime_key, agent._ambiguous_interrupt_keys())
            self.assertIsNone(agent.steering_native_turn_id(target))
            self.assertEqual(agent._pending_requests[runtime_key], [pending_request])
            self.assertFalse(receiver_task.done())
        finally:
            receiver_task.cancel()
            await asyncio.gather(receiver_task, return_exceptions=True)

    async def test_handle_stop_cleans_up_when_silent_result_emit_fails(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"

        class _Client:
            async def interrupt(self):
                return None

            async def disconnect(self):
                return None

        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        controller.processing_indicator = SimpleNamespace(finish=AsyncMock())
        controller.emit_agent_message = AsyncMock(side_effect=RuntimeError("send failed"))
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "T1",
                    "agent_runtime_turn_key": runtime_key,
                    "agent_runtime_turn_token": "R1",
                }
            ),
            ack_reaction_message_id="m1",
            ack_reaction_emoji=":eyes:",
        )
        agent._pending_requests[runtime_key] = [pending_request]
        agent._pending_reactions[runtime_key] = [("m1", ":eyes:")]
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "R1"
        controller.claude_sessions[runtime_key] = _Client()

        request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            message="stop",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id=runtime_key,
            session_key="wechat-user",
            stop_failure_reason=None,
        )

        handled = await service.handle_stop("claude", request)

        self.assertTrue(handled)
        self.assertIsNone(request.stop_failure_reason)
        self.assertNotIn(runtime_key, controller.claude_sessions)
        self.assertFalse(service._turn_gates[runtime_key].lock.locked())
        controller.session_handler.cleanup_session.assert_awaited_once_with(
            runtime_key,
            current_receiver_task=None,
            activation_retired=False,
            reason="user_stop",
        )
        controller.processing_indicator.finish.assert_awaited_once_with(
            pending_request, terminal_emoji=STOPPED_REACTION_EMOJI
        )

    async def test_handle_stop_keeps_runtime_gate_until_cleanup_finishes(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        disconnect_started = asyncio.Event()
        allow_disconnect = asyncio.Event()
        emit_called = asyncio.Event()

        class _Client:
            async def interrupt(self):
                return None

            async def disconnect(self):
                disconnect_started.set()
                await allow_disconnect.wait()

        async def _receiver():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        controller.processing_indicator = SimpleNamespace(finish=AsyncMock())

        async def _emit(context, message_type, text, **_kwargs):
            emit_called.set()
            if message_type == "result":
                service.release_runtime_turn(context)

        controller.emit_agent_message = AsyncMock(side_effect=_emit)
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "T1",
                    "agent_runtime_turn_key": runtime_key,
                    "agent_runtime_turn_token": "R1",
                }
            ),
            ack_reaction_message_id="m1",
            ack_reaction_emoji=":eyes:",
        )
        agent._pending_requests[runtime_key] = [pending_request]
        agent._pending_reactions[runtime_key] = [("m1", ":eyes:")]
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "R1"
        controller.claude_sessions[runtime_key] = _Client()
        controller.receiver_tasks[runtime_key] = asyncio.create_task(_receiver())

        request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            message="stop",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id=runtime_key,
            session_key="wechat-user",
            stop_failure_reason=None,
        )

        stop_task = asyncio.create_task(service.handle_stop("claude", request))
        await asyncio.wait_for(disconnect_started.wait(), timeout=3)
        await asyncio.sleep(0)

        self.assertTrue(service._turn_gates[runtime_key].lock.locked())
        self.assertFalse(emit_called.is_set())

        allow_disconnect.set()
        handled = await asyncio.wait_for(stop_task, timeout=3)

        self.assertTrue(handled)
        self.assertTrue(emit_called.is_set())
        self.assertFalse(service._turn_gates[runtime_key].lock.locked())

    async def test_setup_failure_emits_terminal_result_before_runtime_gate_release(self):
        controller = _StubController()
        runtime_key = "wechat_o9:/tmp/work"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(side_effect=RuntimeError("setup failed")),
            mark_session_active=lambda _key: None,
            mark_session_idle=lambda _key: None,
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_args, **_kwargs: None,
        )
        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        agent._delete_ack = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()

        observed_gate_current = []

        async def _emit(context, message_type, text, **_kwargs):
            if message_type == "result":
                observed_gate_current.append(service.emit_matches_runtime_turn(context))
                service.release_runtime_turn(context)

        controller.emit_agent_message.side_effect = _emit
        request = SimpleNamespace(
            context=SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={}),
            message="hello",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id=runtime_key,
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            files=None,
        )

        await service.handle_message("claude", request)

        self.assertEqual(observed_gate_current, [True])
        self.assertFalse(service._turn_gates[runtime_key].lock.locked())

    async def test_setup_auth_failure_without_client_does_not_raise_unbound_client(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(
                side_effect=RuntimeError("OAuth login required"),
            ),
            mark_session_idle=lambda _key: None,
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_args, **_kwargs: None,
        )
        agent = ClaudeAgent(controller)
        agent._delete_ack = AsyncMock()

        request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            message="hello",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id="wechat_o9:/tmp/work",
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            files=None,
        )

        await agent.handle_message(request)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once()
        controller.session_handler.handle_session_error.assert_not_awaited()

    async def test_result_keeps_claude_session_active_when_requests_are_queued(self):
        controller = _StubController()
        mark_idle_calls = []
        controller.session_handler = SimpleNamespace(
            mark_session_idle=lambda composite_key: mark_idle_calls.append(composite_key),
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_: None,
        )
        controller._get_session_key = lambda context: "wechat-user"
        controller.emit_agent_message = AsyncMock()

        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        context = SimpleNamespace(user_id="U1", channel_id="C1")
        composite_key = "session-1:/tmp/work"
        queued_request = SimpleNamespace(started_at=None)
        next_request = SimpleNamespace(started_at=None)
        agent._pending_requests[composite_key] = [queued_request, next_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:"), ("m2", ":eyes:")]
        agent._last_assistant_text[composite_key] = "final assistant text"
        controller.session_manager.session.session_active[composite_key] = True

        result_message = type(
            "ResultMessage",
            (),
            {"subtype": "success", "result": "done", "duration_ms": 1},
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield result_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        self.assertEqual(mark_idle_calls, [])
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "final assistant text",
            subtype="success",
            duration_ms=1,
            parse_mode="markdown",
            request=queued_request,
        )
        self.assertEqual(agent._pending_requests[composite_key], [next_request])
        self.assertEqual(agent._pending_reactions[composite_key], [("m2", ":eyes:")])
        self.assertTrue(controller.session_manager.session.session_active[composite_key])

    def test_buffered_result_without_steered_assistant_is_suppressed(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        composite_key = "session-1:/tmp/work"
        agent._steering_generations[composite_key] = 1
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "unknown"

        self.assertTrue(agent._terminal_claim_superseded(composite_key, 1))
        self.assertEqual(agent._pending_steering_input_state(composite_key), "unknown")

    async def test_steered_assistant_and_result_settle_the_pending_turn(self):
        """A Result before the native input echo remains nonterminal."""
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        agent._get_formatter = lambda _context: SimpleNamespace(
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        composite_key = "session-1:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "accepted"
        agent._advance_steering_generation(composite_key)
        primary_result = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "result": "primary final answer",
                "duration_ms": 1,
            },
        )()
        text_block = TextBlock("steered final answer")
        assistant_message = type(
            "AssistantMessage",
            (),
            {"content": [text_block]},
        )()
        result_message = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "result": "steered final answer",
                "duration_ms": 2,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield primary_result
                    yield UserMessage("steered prompt")
                    yield assistant_message
                    yield result_message

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-1",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once_with(
            context,
            "steered final answer",
            subtype="success",
            duration_ms=2,
            parse_mode="markdown",
            request=pending_request,
        )
        controller.emit_agent_message.assert_any_await(
            context,
            "output",
            "primary final answer",
            parse_mode="markdown",
            output=ANY,
        )
        self.assertNotIn(composite_key, agent._pending_requests)

    async def test_presteer_result_stays_visible_without_agent_initiated_turn(self):
        """HFR-460: queued native steering keeps its user-owned Turn."""

        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        output_tokens = []
        output_semantics = []

        async def _emit_output(emit_context, message_type, *_args, **kwargs):
            if message_type == "output":
                output_tokens.append(
                    emit_context.platform_specific.get("agent_runtime_turn_token")
                )
                output_semantics.append(kwargs.get("output"))

        controller.emit_agent_message = AsyncMock(side_effect=_emit_output)
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        begin_agent_initiated_turn = AsyncMock()
        controller.agent_service = SimpleNamespace(
            activity_registry=None,
            begin_agent_initiated_turn=begin_agent_initiated_turn,
        )
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        agent._get_formatter = lambda _context: SimpleNamespace(
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        composite_key = "session-delayed-steer:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={
                "turn_token": "T1",
                "agent_runtime_turn_token": "R1",
            },
        )
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "T2",
                    "agent_runtime_turn_token": "R2",
                }
            ),
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "accepted"
        agent._advance_steering_generation(composite_key)
        primary_assistant = type(
            "AssistantMessage",
            (),
            {"content": [TextBlock("primary work completed")]},
        )()
        primary_result = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "result": "primary work completed",
                "duration_ms": 1,
            },
        )()
        steered_assistant = type(
            "AssistantMessage",
            (),
            {"content": [TextBlock("screenshots ready")]},
        )()
        steered_result = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "result": "screenshots ready",
                "duration_ms": 2,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield primary_assistant
                    yield primary_result
                    yield UserMessage("steered prompt")
                    yield steered_assistant
                    yield steered_result

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-delayed-steer",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        controller.emit_agent_message.assert_any_await(
            context,
            "output",
            "primary work completed",
            parse_mode="markdown",
            output=ANY,
        )
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "screenshots ready",
            subtype="success",
            duration_ms=2,
            parse_mode="markdown",
            request=pending_request,
        )
        self.assertNotIn(composite_key, agent._pending_requests)
        self.assertEqual(output_tokens, ["R2"])
        self.assertEqual(len(output_semantics), 1)
        self.assertFalse(output_semantics[0].records_run_output)
        begin_agent_initiated_turn.assert_not_awaited()

    async def test_presteer_result_settles_its_claimed_activity_without_closing_turn(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock(return_value="primary-output")
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        composite_key = "session-presteer-activity:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        activity = SessionActivity(
            id="activity-primary",
            backend="claude",
            runtime_key=composite_key,
            session_id="session-presteer-activity",
            kind="task",
            status="completed",
            turn_id="T1",
            run_id="run-primary",
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            output_activities=[activity],
            output=activity_completion_output(
                activity,
                detached=False,
                completes_turn=True,
            ),
        )
        agent._pending_requests[composite_key] = [pending_request]
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "accepted"
        agent._advance_steering_generation(composite_key)

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield type(
                        "ResultMessage",
                        (),
                        {
                            "subtype": "success",
                            "result": "primary result",
                            "duration_ms": 1,
                        },
                    )()
                    yield UserMessage("steered prompt")
                    yield type(
                        "ResultMessage",
                        (),
                        {
                            "subtype": "success",
                            "result": "steered result",
                            "duration_ms": 2,
                        },
                    )()

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-presteer-activity",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        primary_call = controller.emit_agent_message.await_args_list[0]
        self.assertEqual(primary_call.args[1:3], ("output", "primary result"))
        primary_output = primary_call.kwargs["output"]
        self.assertFalse(primary_output.completes_turn)
        self.assertTrue(primary_output.settles_run)
        self.assertEqual(primary_output.activity_id, "activity-primary")
        self.assertEqual(primary_output.run_ids, ("run-primary",))
        self.assertEqual(pending_request.output_activities, [])
        self.assertTrue(pending_request.output.completes_turn)
        self.assertIsNone(pending_request.output.activity_id)
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "steered result",
            subtype="success",
            duration_ms=2,
            parse_mode="markdown",
            request=pending_request,
        )

    async def test_silent_presteer_result_settles_activity_without_a_message_id(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock(return_value=None)
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        composite_key = "session-silent-presteer-activity:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        activity = SessionActivity(
            id="activity-primary",
            backend="claude",
            runtime_key=composite_key,
            session_id="session-silent-presteer-activity",
            kind="task",
            status="completed",
            turn_id="T1",
            run_id="run-primary",
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            output_activities=[activity],
            output=activity_completion_output(
                activity,
                detached=False,
                completes_turn=True,
            ),
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._turns_with_foreground_tools.add(composite_key)
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "accepted"
        agent._advance_steering_generation(composite_key)

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield type(
                        "ResultMessage",
                        (),
                        {
                            "subtype": "success",
                            "result": "tool-only primary",
                            "duration_ms": 1,
                        },
                    )()
                    yield UserMessage("steered prompt")
                    yield type(
                        "ResultMessage",
                        (),
                        {
                            "subtype": "success",
                            "result": "steered result",
                            "duration_ms": 2,
                        },
                    )()

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-silent-presteer-activity",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        primary_call = controller.emit_agent_message.await_args_list[0]
        self.assertEqual(primary_call.args[1], "output")
        self.assertEqual(
            primary_call.args[2],
            "<silent>Claude turn completed without assistant text.</silent>",
        )
        self.assertEqual(pending_request.output_activities, [])
        self.assertNotIn(composite_key, agent._turns_with_foreground_tools)
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "steered result",
            subtype="success",
            duration_ms=2,
            parse_mode="markdown",
            request=pending_request,
        )

    async def test_user_echo_allows_one_result_to_own_primary_and_steer(self):
        """HFR-435: Claude may merge a steer into the primary Result."""

        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        agent._get_formatter = lambda _context: SimpleNamespace(
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        composite_key = "session-merged-steer:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "accepted"
        agent._advance_steering_generation(composite_key)

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield UserMessage("steered prompt")
                    yield type(
                        "AssistantMessage",
                        (),
                        {"content": [TextBlock("primary and steer answered")]},
                    )()
                    yield type(
                        "ResultMessage",
                        (),
                        {
                            "subtype": "success",
                            "result": "primary and steer answered",
                            "duration_ms": 2,
                        },
                    )()

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-merged-steer",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once_with(
            context,
            "primary and steer answered",
            subtype="success",
            duration_ms=2,
            parse_mode="markdown",
            request=pending_request,
        )
        self.assertNotIn(composite_key, agent._pending_requests)
        self.assertNotIn(composite_key, agent._native_input_receipts)

    async def test_user_echo_retires_primary_assistant_before_result_only_steer(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock(return_value="primary-output")
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        agent._get_formatter = lambda _context: SimpleNamespace(
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        composite_key = "session-assistant-receipt:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "accepted"
        agent._advance_steering_generation(composite_key)

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield type(
                        "AssistantMessage",
                        (),
                        {"content": [TextBlock("primary answer")]},
                    )()
                    yield UserMessage("steered prompt")
                    yield type(
                        "ResultMessage",
                        (),
                        {
                            "subtype": "success",
                            "result": "steered result",
                            "duration_ms": 2,
                        },
                    )()

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-assistant-receipt",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        controller.emit_agent_message.assert_awaited_once_with(
            context,
            "output",
            "primary answer",
            parse_mode="markdown",
            output=ANY,
        )
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "steered result",
            subtype="success",
            duration_ms=2,
            parse_mode="markdown",
            request=pending_request,
        )
        self.assertNotIn(composite_key, agent._last_assistant_text)

    async def test_repeated_user_echoes_allow_one_merged_result_to_settle(self):
        """HFR-460: repeated steering does not require one Result per input."""

        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        agent._get_formatter = lambda _context: SimpleNamespace(
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        composite_key = "session-repeated-steer:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        for text in ("first steer", "second steer"):
            receipt = agent._register_native_input(
                composite_key,
                text,
                kind="steer",
            )
            receipt.state = "accepted"
            agent._advance_steering_generation(composite_key)

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield UserMessage("first steer")
                    yield UserMessage("second steer")
                    yield type(
                        "ResultMessage",
                        (),
                        {
                            "subtype": "success",
                            "result": "both steers answered",
                            "duration_ms": 3,
                        },
                    )()

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-repeated-steer",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once_with(
            context,
            "both steers answered",
            subtype="success",
            duration_ms=3,
            parse_mode="markdown",
            request=pending_request,
        )
        self.assertNotIn(composite_key, agent._pending_requests)
        self.assertNotIn(composite_key, agent._native_input_receipts)

    async def test_ambiguous_results_emit_each_answer_in_order(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        emissions = []

        async def _emit_output(_context, message_type, text, **_kwargs):
            emissions.append((message_type, text))

        controller.emit_agent_message = AsyncMock(side_effect=_emit_output)
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        async def _emit_result(_context, text, **_kwargs):
            emissions.append(("result", text))

        agent.emit_result_message = AsyncMock(side_effect=_emit_result)
        agent._get_formatter = lambda _context: SimpleNamespace(
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        composite_key = "session-ambiguous:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._turns_with_foreground_tools.add(composite_key)
        agent._foreground_tool_use_ids[composite_key] = {"primary-tool"}
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "unknown"
        agent._advance_steering_generation(composite_key)

        primary_assistant = type(
            "AssistantMessage",
            (),
            {"content": [TextBlock("primary answer")]},
        )()
        primary_result = type(
            "ResultMessage",
            (),
            {"subtype": "success", "result": "primary answer", "duration_ms": 1},
        )()
        steered_result = type(
            "ResultMessage",
            (),
            {"subtype": "success", "result": "steered answer", "duration_ms": 2},
        )()
        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield primary_assistant
                    yield primary_result
                    yield UserMessage("steered prompt")
                    yield steered_result

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-ambiguous",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        controller.emit_agent_message.assert_any_await(
            context,
            "output",
            "primary answer",
            parse_mode="markdown",
            output=ANY,
        )
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "steered answer",
            subtype="success",
            duration_ms=2,
            parse_mode="markdown",
            request=pending_request,
        )
        self.assertNotIn(composite_key, agent._ambiguous_primary_results)
        self.assertNotIn(composite_key, agent._pending_requests)
        self.assertEqual(
            emissions,
            [("output", "primary answer"), ("result", "steered answer")],
        )

    async def test_buffered_pre_steer_assistant_keeps_primary_result_ownership(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        agent._get_formatter = lambda _context: SimpleNamespace(
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        composite_key = "session-buffered:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        assistant_received = asyncio.Event()
        release_assistant = asyncio.Event()
        result_receive_waiting = asyncio.Event()
        release_result = asyncio.Event()
        end_stream = asyncio.Event()

        original_begin = agent._maybe_begin_agent_initiated_turn

        async def _hold_assistant(*args, **kwargs):
            assistant_received.set()
            await release_assistant.wait()
            return await original_begin(*args, **kwargs)

        agent._maybe_begin_agent_initiated_turn = _hold_assistant
        assistant_message = type(
            "AssistantMessage",
            (),
            {"content": [TextBlock("buffered primary answer")]},
        )()
        result_message = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "result": "buffered primary answer",
                "duration_ms": 1,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message
                    result_receive_waiting.set()
                    await release_result.wait()
                    yield result_message
                    await end_stream.wait()

                return _iterate()

        receiver_task = asyncio.create_task(
            agent._receive_messages(
                _Client(),
                "session-buffered",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        await assistant_received.wait()
        release_assistant.set()
        await result_receive_waiting.wait()
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "unknown"
        agent._advance_steering_generation(composite_key)
        release_result.set()
        for _ in range(100):
            if composite_key in agent._ambiguous_primary_results:
                break
            await asyncio.sleep(0)
        else:
            self.fail("primary Result was not buffered behind the steering EOF boundary")

        self.assertEqual(agent._pending_requests[composite_key], [pending_request])
        agent.emit_result_message.assert_not_awaited()

        receiver_task.cancel()
        await asyncio.gather(receiver_task, return_exceptions=True)

    async def test_buffered_tool_only_result_keeps_foreground_state_through_eof(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler.mark_session_idle = lambda _key: None
        controller.session_handler.handle_session_error = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        composite_key = "session-buffered-tool:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={"turn_token": "T1"},
        )
        pending_request = SimpleNamespace(
            context=context,
            started_at=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._turns_with_foreground_tools.add(composite_key)
        receipt = agent._register_native_input(
            composite_key,
            "steered prompt",
            kind="steer",
        )
        receipt.state = "unknown"
        agent._advance_steering_generation(composite_key)

        result_message = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "result": "Bash completed",
                "duration_ms": 1,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield result_message

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-buffered-tool",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once()
        selected = agent.emit_result_message.await_args.args[1]
        self.assertIn("<silent>", selected)
        self.assertNotIn("Bash completed", selected)
        self.assertNotIn(composite_key, agent._pending_requests)

    async def test_toolcall_emit_adopts_current_pending_turn_token(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "session-1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        composite_key = "session-1:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={
                "turn_token": "T1",
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "R1",
            },
        )
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "T2",
                    "agent_runtime_turn_key": composite_key,
                    "agent_runtime_turn_token": "R2",
                }
            )
        )
        agent._pending_requests[composite_key] = [pending_request]

        class FakeToolUseBlock:
            name = "Bash"
            input = {"command": "pwd"}

        class AssistantMessage:
            content = [FakeToolUseBlock()]

        class _Formatter:
            @staticmethod
            def format_toolcall(*_args, **_kwargs):
                return "Bash(pwd)"

            @staticmethod
            def format_toolcall_label(*_args, **_kwargs):
                return "🔧 Bash: pwd"

            @staticmethod
            def format_assistant_message(parts):
                return "\n\n".join(parts)

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield AssistantMessage()

                return _iterate()

        with patch("modules.agents.claude_agent.ToolUseBlock", FakeToolUseBlock):
            agent._get_formatter = lambda _context: _Formatter()
            await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        first_emit = controller.emit_agent_message.await_args_list[0]
        self.assertEqual(first_emit.args, (context, "toolcall", "Bash(pwd)"))
        self.assertEqual(
            first_emit.kwargs,
            {"parse_mode": "markdown", "status_label": "🔧 Bash: pwd"},
        )
        self.assertEqual(context.platform_specific["turn_token"], "T2")
        self.assertEqual(context.platform_specific["agent_runtime_turn_key"], composite_key)
        self.assertEqual(context.platform_specific["agent_runtime_turn_token"], "R2")

    async def test_handle_message_uses_runtime_session_key_for_claude_tracking(self):
        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        runtime_key = "wechat_o9:reviewer:/tmp/work"
        request_context = SimpleNamespace(platform_specific={})
        set_dispatch_phase(request_context, DISPATCH_PHASE_PREWRITE)

        async def _query_at_native_boundary(*_args, **_kwargs):
            self.assertIs(backend_dispatch_attempted(request_context), True)

        client = SimpleNamespace(
            query=AsyncMock(side_effect=_query_at_native_boundary),
            _vibe_runtime_base_session_id="wechat_o9:reviewer",
            _vibe_runtime_session_key=runtime_key,
        )
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=SimpleNamespace(),
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_: None,
        )
        mark_active_calls = []
        controller.session_handler.mark_session_active = lambda composite_key: mark_active_calls.append(composite_key)

        agent = ClaudeAgent(controller)
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        agent._receive_messages = AsyncMock()

        request = SimpleNamespace(
            context=request_context,
            message="hello",
            input_metadata=AgentInputMetadata(user_id="U1", user_name="Sender"),
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id="wechat_o9:/tmp/work",
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id="m1",
            ack_reaction_emoji=":eyes:",
            files=None,
        )

        with patch("core.agent_input.datetime") as clock:
            clock.now.return_value.astimezone.return_value = datetime(2026, 9, 6, 11, tzinfo=timezone.utc)

            async def prepare_session(*_args, **_kwargs):
                clock.now.return_value.astimezone.return_value = datetime(2026, 9, 6, 11, 10, tzinfo=timezone.utc)
                return client

            controller.session_handler.get_or_create_claude_session.side_effect = prepare_session
            await agent.handle_message(request)
        await asyncio.sleep(0)

        controller.session_handler.get_or_create_claude_session.assert_awaited_once()
        self.assertEqual(mark_active_calls, [runtime_key])
        client.query.assert_awaited_once_with(
            "[Now: 2026-09-06 11:10:00 UTC+00:00]\n[Sender<U1>]\nhello", session_id=runtime_key,
        )
        self.assertEqual(request.message, "hello")
        self.assertIn(runtime_key, agent._pending_requests)
        self.assertIn(runtime_key, agent._pending_reactions)
        self.assertNotIn(request.composite_session_id, agent._pending_requests)
        self.assertNotIn(request.composite_session_id, agent._pending_reactions)
        self.assertIn(runtime_key, controller.receiver_tasks)
        agent._receive_messages.assert_awaited_once_with(
            client,
            "wechat_o9:reviewer",
            "/tmp/work",
            request.context,
            composite_key=runtime_key,
        )

    async def test_handle_message_error_keeps_session_active_when_requests_remain_queued(self):
        controller = _StubController()
        mark_idle_calls = []
        controller.emit_agent_message = AsyncMock()
        runtime_key = "wechat_o9:reviewer:/tmp/work"
        queued_request = SimpleNamespace()
        client = SimpleNamespace(
            query=AsyncMock(side_effect=RuntimeError("boom")),
            _vibe_runtime_base_session_id="wechat_o9:reviewer",
            _vibe_runtime_session_key=runtime_key,
        )
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=lambda composite_key: None,
            mark_session_idle=lambda composite_key: mark_idle_calls.append(composite_key),
            handle_session_error=AsyncMock(),
            capture_session_id=lambda *_: None,
        )

        agent = ClaudeAgent(controller)
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()

        request = SimpleNamespace(
            context=SimpleNamespace(),
            message="hello",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id="wechat_o9:/tmp/work",
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id="m1",
            ack_reaction_emoji=":eyes:",
            files=None,
        )
        agent._pending_requests[runtime_key] = [queued_request]
        agent._pending_reactions[runtime_key] = [("m2", ":eyes:")]

        with patch("core.message_mirror.persist_agent_message") as persist:
            await agent.handle_message(request)

        self.assertEqual(mark_idle_calls, [])
        self.assertEqual(agent._pending_requests[runtime_key], [queued_request])
        self.assertEqual(agent._pending_reactions[runtime_key], [("m2", ":eyes:")])
        agent._remove_ack_reaction.assert_awaited_once_with(request)
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[1:], ("notify", "❌ Claude error: boom"))
        self.assertEqual(persist.call_args.kwargs["metadata"]["backend"], "claude")
        self.assertEqual(persist.call_args.kwargs["metadata"]["event"], "backend_failure")
        self.assertTrue(persist.call_args.kwargs["metadata"]["failure_id"])
        self.assertTrue(
            persist.call_args.kwargs["native_message_id"].startswith("backend-failure:")
        )

    async def test_handle_message_terminated_auth_failure_cleans_cached_runtime(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        runtime_key = "wechat_o9:reviewer:/tmp/work"
        queued_request = SimpleNamespace()
        client = SimpleNamespace(
            query=AsyncMock(
                side_effect=RuntimeError(
                    "Failed to authenticate: OAuth login required",
                )
            ),
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-6)),
            _vibe_runtime_base_session_id="wechat_o9:reviewer",
            _vibe_runtime_session_key=runtime_key,
        )
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=lambda _composite_key: None,
            mark_session_idle=lambda _composite_key: None,
            handle_session_error=AsyncMock(),
            cleanup_session=AsyncMock(),
            capture_session_id=lambda *_: None,
        )
        agent = ClaudeAgent(controller)
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        agent._pending_requests[runtime_key] = [queued_request]

        request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            message="hello",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id="wechat_o9:/tmp/work",
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id="m1",
            ack_reaction_emoji="eyes",
            files=None,
        )

        await agent.handle_message(request)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once()
        controller.session_handler.cleanup_session.assert_awaited_once_with(
            runtime_key,
            current_receiver_task=None,
            activation_retired=False,
            reason="query_auth_failure",
        )
        self.assertEqual(agent._pending_requests[runtime_key], [queued_request])
        controller.session_handler.handle_session_error.assert_not_awaited()

    async def test_clear_sessions_cancels_receiver_tasks_for_cleared_session(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        client = _StubClient()
        controller.claude_sessions[session_key] = client

        task_cancelled = asyncio.Event()

        async def _receiver():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        controller.receiver_tasks[session_key] = asyncio.create_task(_receiver())
        agent._steering_lock(session_key)
        agent._steering_generations[session_key] = 1
        await asyncio.sleep(0)

        cleared = await agent.clear_sessions("wechat-user")

        self.assertEqual(cleared, 1)
        self.assertTrue(client.disconnected)
        self.assertTrue(task_cancelled.is_set())
        self.assertNotIn(session_key, controller.receiver_tasks)
        self.assertNotIn(session_key, controller.claude_sessions)
        self.assertNotIn(session_key, agent._steering_locks)
        self.assertNotIn(session_key, agent._steering_generations)
        self.assertEqual(controller.session_manager.cleared, ["wechat-user"])

    async def test_clear_sessions_cancels_subagent_runtime_keys_for_cleared_session(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:reviewer:/tmp/work"
        client = _StubClient()
        controller.claude_sessions[session_key] = client

        cleared = await agent.clear_sessions("wechat-user")

        self.assertEqual(cleared, 1)
        self.assertTrue(client.disconnected)
        self.assertNotIn(session_key, controller.claude_sessions)

    async def test_runtime_turn_keys_for_session_key_matches_subagent_runtime_keys(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        controller.claude_sessions["wechat_o9:reviewer:/tmp/work"] = _StubClient()
        controller.claude_sessions["other:/tmp/work"] = _StubClient()

        keys = agent.runtime_turn_keys_for_session_key("wechat-user")

        self.assertEqual(keys, {"wechat_o9:reviewer:/tmp/work"})

    async def test_clear_sessions_swallows_receiver_task_failure(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        client = _StubClient()
        controller.claude_sessions[session_key] = client
        disconnected = asyncio.Event()

        async def _disconnect():
            client.disconnected = True
            disconnected.set()

        client.disconnect = _disconnect

        async def _receiver():
            await disconnected.wait()
            raise RuntimeError("receiver failed")

        controller.receiver_tasks[session_key] = asyncio.create_task(_receiver())
        await asyncio.sleep(0)

        cleared = await agent.clear_sessions("wechat-user")

        self.assertEqual(cleared, 1)
        self.assertTrue(client.disconnected)
        self.assertNotIn(session_key, controller.receiver_tasks)
        self.assertNotIn(session_key, controller.claude_sessions)
        self.assertEqual(controller.session_manager.cleared, ["wechat-user"])

    async def test_clear_sessions_drains_finished_receiver_task_failure(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        client = _StubClient()
        controller.claude_sessions[session_key] = client

        class _DoneReceiverTask:
            drained = False

            @staticmethod
            def done():
                return True

            def exception(self):
                self.drained = True
                return RuntimeError("receiver already failed")

        receiver_task = _DoneReceiverTask()
        controller.receiver_tasks[session_key] = receiver_task

        cleared = await agent.clear_sessions("wechat-user")

        self.assertEqual(cleared, 1)
        self.assertTrue(client.disconnected)
        self.assertTrue(receiver_task.drained)
        self.assertNotIn(session_key, controller.receiver_tasks)
        self.assertNotIn(session_key, controller.claude_sessions)
        self.assertEqual(controller.session_manager.cleared, ["wechat-user"])

    async def test_cleanup_runtime_session_cancels_receiver_when_disconnect_is_cancelled(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        disconnect_started = asyncio.Event()
        receiver_cancelled = asyncio.Event()

        class _SlowDisconnectClient(_StubClient):
            async def disconnect(self):
                self.disconnected = True
                disconnect_started.set()
                await asyncio.Future()

        client = _SlowDisconnectClient()
        controller.claude_sessions[session_key] = client

        async def _receiver():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                receiver_cancelled.set()
                raise

        receiver_task = asyncio.create_task(_receiver())
        controller.receiver_tasks[session_key] = receiver_task
        cleanup_task = asyncio.create_task(agent._cleanup_runtime_session(session_key))

        await disconnect_started.wait()
        self.assertNotIn(session_key, controller.receiver_tasks)

        cleanup_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cleanup_task

        self.assertTrue(client.disconnected)
        self.assertTrue(receiver_cancelled.is_set())
        self.assertNotIn(session_key, controller.receiver_tasks)
        self.assertNotIn(session_key, controller.claude_sessions)

    async def test_cleanup_runtime_session_preserves_new_receiver_during_disconnect(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        disconnect_started = asyncio.Event()
        old_receiver_cancelled = asyncio.Event()
        clear_tracking_calls = []
        controller.session_handler.clear_session_tracking = lambda key: clear_tracking_calls.append(key)

        class _SlowDisconnectClient(_StubClient):
            async def disconnect(self):
                self.disconnected = True
                disconnect_started.set()
                await asyncio.Future()

        client = _SlowDisconnectClient()
        controller.claude_sessions[session_key] = client

        async def _old_receiver():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                old_receiver_cancelled.set()
                raise

        old_receiver = asyncio.create_task(_old_receiver())
        new_receiver = asyncio.create_task(asyncio.sleep(3600))
        old_request = SimpleNamespace(name="old")
        agent._pending_requests[session_key] = [old_request]
        agent._pending_reactions[session_key] = [("old", ":eyes:")]
        agent._last_assistant_text[session_key] = "old text"
        agent._pending_assistant_message[session_key] = "old assistant"
        controller.receiver_tasks[session_key] = old_receiver
        cleanup_task = asyncio.create_task(agent._cleanup_runtime_session(session_key))

        await disconnect_started.wait()
        self.assertNotIn(session_key, controller.receiver_tasks)
        self.assertNotIn(session_key, agent._pending_requests)
        self.assertNotIn(session_key, agent._pending_reactions)
        self.assertNotIn(session_key, agent._last_assistant_text)
        self.assertNotIn(session_key, agent._pending_assistant_message)
        self.assertEqual(clear_tracking_calls, [session_key])

        new_request = SimpleNamespace(name="new")
        controller.receiver_tasks[session_key] = new_receiver
        agent._pending_requests[session_key] = [new_request]
        agent._pending_reactions[session_key] = [("new", ":eyes:")]
        agent._last_assistant_text[session_key] = "new text"
        agent._pending_assistant_message[session_key] = "new assistant"

        cleanup_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cleanup_task

        self.assertTrue(old_receiver_cancelled.is_set())
        self.assertIs(controller.receiver_tasks[session_key], new_receiver)
        self.assertEqual(agent._pending_requests[session_key], [new_request])
        self.assertEqual(agent._pending_reactions[session_key], [("new", ":eyes:")])
        self.assertEqual(agent._last_assistant_text[session_key], "new text")
        self.assertEqual(agent._pending_assistant_message[session_key], "new assistant")
        new_receiver.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await new_receiver

    async def test_cleanup_runtime_session_defers_disconnect_for_current_receiver(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        cleanup_returned = asyncio.Event()
        disconnect_started = asyncio.Event()
        release_disconnect = asyncio.Event()

        class _SlowDisconnectClient(_StubClient):
            async def disconnect(self):
                self.disconnected = True
                disconnect_started.set()
                await release_disconnect.wait()

        client = _SlowDisconnectClient()
        controller.claude_sessions[session_key] = client

        async def _receiver():
            await agent._cleanup_runtime_session(
                session_key,
                current_receiver_task=asyncio.current_task(),
            )
            cleanup_returned.set()

        receiver_task = asyncio.create_task(_receiver())
        controller.receiver_tasks[session_key] = receiver_task

        await cleanup_returned.wait()
        self.assertNotIn(session_key, controller.receiver_tasks)
        self.assertNotIn(session_key, controller.claude_sessions)

        await disconnect_started.wait()
        self.assertTrue(client.disconnected)
        release_disconnect.set()
        await asyncio.sleep(0)

    async def test_refresh_auth_state_disconnects_runtime_sessions(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        client = _StubClient()
        controller.claude_sessions[session_key] = client
        agent._last_assistant_text[session_key] = "hello"
        agent._pending_assistant_message[session_key] = "pending"
        agent._pending_reactions[session_key] = [("m1", "⏳")]
        agent._pending_requests[session_key] = ["request"]

        task_cancelled = asyncio.Event()

        async def _receiver():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        controller.receiver_tasks[session_key] = asyncio.create_task(_receiver())
        await asyncio.sleep(0)

        await agent.refresh_auth_state()

        self.assertTrue(client.disconnected)
        self.assertTrue(task_cancelled.is_set())
        self.assertNotIn(session_key, controller.receiver_tasks)
        self.assertNotIn(session_key, controller.claude_sessions)
        self.assertNotIn(session_key, agent._last_assistant_text)
        self.assertNotIn(session_key, agent._pending_assistant_message)
        self.assertNotIn(session_key, agent._pending_reactions)
        self.assertNotIn(session_key, agent._pending_requests)

    async def test_cleanup_runtime_session_delegates_runtime_cleanup_to_session_handler(self):
        controller = _StubController()
        cleanup_calls = []
        controller.session_handler = SimpleNamespace(
            cleanup_session=AsyncMock(side_effect=lambda key, **kwargs: cleanup_calls.append((key, kwargs))),
        )
        agent = ClaudeAgent(controller)
        session_key = "wechat_o9:/tmp/work"
        receiver_task = asyncio.create_task(asyncio.sleep(3600))
        agent._last_assistant_text[session_key] = "hello"
        agent._pending_assistant_message[session_key] = "pending"
        agent._pending_reactions[session_key] = [("m1", "⏳")]
        agent._pending_requests[session_key] = ["request"]
        agent._native_session_ids[session_key] = "native-session-1"

        await agent._cleanup_runtime_session(session_key, current_receiver_task=receiver_task)

        controller.session_handler.cleanup_session.assert_awaited_once_with(
            session_key,
            current_receiver_task=receiver_task,
            activation_retired=False,
            reason="claude_agent_cleanup",
        )
        self.assertNotIn(session_key, agent._last_assistant_text)
        self.assertNotIn(session_key, agent._pending_assistant_message)
        self.assertNotIn(session_key, agent._pending_reactions)
        self.assertNotIn(session_key, agent._pending_requests)
        self.assertNotIn(session_key, agent._native_session_ids)
        receiver_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await receiver_task

    async def test_prepare_resume_binding_cleans_only_target_runtime_session(self):
        controller = _StubController()
        agent = ClaudeAgent(controller)
        target_key = "wechat_o9:/tmp/work"
        other_key = "wechat_o10:/tmp/work"
        target_client = _StubClient()
        other_client = _StubClient()
        controller.claude_sessions[target_key] = target_client
        controller.claude_sessions[other_key] = other_client
        controller.receiver_tasks[target_key] = asyncio.create_task(asyncio.sleep(3600))
        controller.receiver_tasks[other_key] = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)

        await agent.prepare_resume_binding(
            base_session_id="wechat_o9",
            session_key="wechat-user",
            working_path="/tmp/work",
        )

        self.assertTrue(target_client.disconnected)
        self.assertFalse(other_client.disconnected)
        self.assertNotIn(target_key, controller.claude_sessions)
        self.assertIn(other_key, controller.claude_sessions)
        self.assertNotIn(target_key, controller.receiver_tasks)
        self.assertIn(other_key, controller.receiver_tasks)

        controller.receiver_tasks[other_key].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await controller.receiver_tasks[other_key]

    async def test_receiver_auth_error_prefers_oauth_recovery_message(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        composite_key = "session-1:/tmp/work"
        failed_request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={"turn_token": "auth-turn"}),
        )
        agent._pending_requests[composite_key] = [failed_request]
        controller.receiver_tasks[composite_key] = asyncio.current_task()
        controller.claude_sessions[composite_key] = _StubClient()
        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(),
            cleanup_session=AsyncMock(),
            claude_error_diagnostic=lambda _key, _error: "Claude process terminated: SIGABRT",
        )
        agent._clear_pending_reactions = AsyncMock()
        context = SimpleNamespace(platform_specific={})

        class _FailingClient:
            def receive_messages(self):
                async def _iterate():
                    raise RuntimeError(
                        'Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid bearer token"}}'
                    )
                    yield  # pragma: no cover

                return _iterate()

        await agent._receive_messages(_FailingClient(), "session-1", "/tmp/work", context)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once()
        agent.session_handler.handle_session_error.assert_not_awaited()
        agent.session_handler.cleanup_session.assert_awaited_once_with(
            composite_key,
            current_receiver_task=asyncio.current_task(),
            activation_retired=False,
            reason="receiver_error_auth_failure",
        )
        self.assertFalse(agent._pending_requests.get(composite_key))

    async def test_receiver_eof_with_terminated_process_reports_localized_failure(self):
        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        settlement_order = []
        agent.record_model_hub_native_failure = AsyncMock(
            side_effect=lambda *_args: settlement_order.append("record")
        )
        composite_key = "session-eof:/tmp/work"
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "eof-turn",
                    "agent_runtime_turn_token": "eof-runtime",
                }
            ),
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._remove_specific_pending_reaction = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(
                side_effect=lambda *_args, **_kwargs: settlement_order.append("handle")
            ),
            cleanup_session=AsyncMock(),
            claude_error_diagnostic=lambda _key, _error: "Claude process terminated: SIGABRT",
        )
        controller.claude_sessions[composite_key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-6)),
        )
        controller.receiver_tasks[composite_key] = asyncio.current_task()

        with patch("core.message_mirror.persist_agent_message") as persist:
            await agent._handle_receiver_eof(composite_key, pending_request.context)

        agent.session_handler.handle_session_error.assert_awaited_once()
        agent.record_model_hub_native_failure.assert_awaited_once_with(
            pending_request.context,
            "Claude process terminated: SIGABRT",
        )
        self.assertEqual(settlement_order, ["record", "handle"])
        agent.session_handler.cleanup_session.assert_not_awaited()
        persist.assert_called_once()
        self.assertIn("Claude Code process terminated", persist.call_args.args[2])
        terminal_call = controller.emit_agent_message.await_args
        self.assertIn("SIGABRT", terminal_call.kwargs["terminal_error"])
        self.assertFalse(agent._pending_requests.get(composite_key))

    async def test_receiver_eof_contained_teardown_skips_durable_failure_row(self):
        """A contained teardown must stay silent on every surface.

        ``handle_session_error`` only suppresses the IM send; the durable
        ``notify`` row is what the web Chat renders, so a contained failure has
        to skip that too or the containment is surface-specific.
        """
        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        agent.record_model_hub_native_failure = AsyncMock()
        composite_key = "session-eof-contained:/tmp/work"
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "eof-turn",
                    "agent_runtime_turn_token": "eof-runtime",
                }
            ),
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._remove_specific_pending_reaction = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(return_value=True),
            cleanup_session=AsyncMock(),
            claude_error_diagnostic=lambda _key, _error: "Command failed with exit code -9",
        )
        controller.claude_sessions[composite_key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-9)),
        )
        controller.receiver_tasks[composite_key] = asyncio.current_task()

        with patch("core.message_mirror.persist_agent_message") as persist:
            await agent._handle_receiver_eof(composite_key, pending_request.context)

        agent.session_handler.handle_session_error.assert_awaited_once()
        persist.assert_not_called()

    async def test_contained_teardown_records_no_model_hub_failure(self):
        """Operational cleanup is not evidence about a Model Hub source.

        ``record_model_hub_native_failure`` converts the pending attempt into a
        failed one — ``unclassified_error`` for ``-9`` — and the silent result
        can then settle that provenance as exhausted. Classifying the teardown
        only after the recording has already run corrupts source health for a
        process the service killed itself, so the probe has to come first.
        """
        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        agent.record_model_hub_native_failure = AsyncMock()
        composite_key = "session-eof-teardown:/tmp/work"
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "eof-turn",
                    "agent_runtime_turn_token": "eof-runtime",
                }
            ),
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._remove_specific_pending_reaction = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        probed: list[object] = []

        def _is_intentional(_key, _error, *, client=None):
            probed.append(client)
            return True

        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(return_value=True),
            claude_teardown_is_intentional=_is_intentional,
            cleanup_session=AsyncMock(),
            claude_error_diagnostic=lambda _key, _error: "Command failed with exit code -9",
        )
        client = SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(returncode=-9)))
        controller.claude_sessions[composite_key] = client
        controller.receiver_tasks[composite_key] = asyncio.current_task()

        await agent._handle_receiver_eof(composite_key, pending_request.context)

        agent.record_model_hub_native_failure.assert_not_awaited()
        # The exact client whose returncode was read, not a fresh lookup.
        self.assertEqual(probed, [client])

    # --- contained teardown settlement (#1204) -------------------------------
    #
    # #1202 stopped a service-initiated teardown from being REPORTED as a backend
    # failure. The emit that follows still described it as one: an ordinary silent
    # ``result`` carrying only the terminal-turn default, so an ``agent_runs`` row
    # was terminalized from an empty body and the durable Turn read ``failed``.
    # These assert the settlement now matches the classification on all three paths
    # that can reach it, and — the point of the negative controls — that an ordinary
    # backend failure is untouched.

    def assert_contained_settlement(self, emitter):
        """The terminal emit routed a contained teardown through the settlement lane."""

        call = emitter.await_args
        self.assertEqual(call.args[1:3], ("result", ""))
        self.assertEqual(call.kwargs["level"], "silent")
        output = call.kwargs["output"]
        self.assertEqual(output.settled_by, SETTLED_BY_BACKEND_REFRESH)
        # ``settles_run`` False keeps the empty body out of
        # ``_record_agent_run_terminal_result``; the settlement lane owns the row.
        self.assertFalse(output.settles_run)
        self.assertTrue(output.completes_turn)
        # No terminal result was produced, and that stays true whoever killed the
        # process: clearing the flag would collapse the status bubble to a green
        # ``done`` for a turn that answered nothing.
        self.assertTrue(call.kwargs["is_error"])
        # Suppressing the DIAGNOSTIC is what separates this from a real failure —
        # the dispatcher reads the pair as an intentional silent end (``stopped``)
        # rather than a fault, and the settlement carries the machine-readable cause.
        self.assertIsNone(call.kwargs["terminal_error"])

    def assert_uncontained_settlement(self, emitter, diagnostic_fragment):
        """An ordinary backend failure still settles as one."""

        call = emitter.await_args
        self.assertEqual(call.args[1:3], ("result", ""))
        self.assertTrue(call.kwargs["is_error"])
        self.assertIn(diagnostic_fragment, call.kwargs["terminal_error"])
        self.assertIsNone(call.kwargs["output"].settled_by)
        self.assertTrue(call.kwargs["output"].settles_run)

    def _eof_agent(self, composite_key, *, contained, returncode=-9):
        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        agent.record_model_hub_native_failure = AsyncMock()
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "eof-turn",
                    "agent_runtime_turn_token": "eof-runtime",
                }
            ),
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._remove_specific_pending_reaction = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(return_value=contained),
            claude_teardown_is_intentional=lambda *_a, **_k: contained,
            cleanup_session=AsyncMock(),
            claude_error_diagnostic=lambda _key, _error: f"Claude process terminated: {returncode}",
        )
        controller.claude_sessions[composite_key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=returncode)),
        )
        controller.receiver_tasks[composite_key] = asyncio.current_task()
        return controller, agent, pending_request

    async def test_receiver_eof_contained_teardown_settles_as_backend_refresh(self):
        key = "session-eof-settle:/tmp/work"
        controller, agent, request = self._eof_agent(key, contained=True)

        await agent._handle_receiver_eof(key, request.context)

        self.assert_contained_settlement(controller.emit_agent_message)

    async def test_receiver_eof_real_failure_still_settles_as_a_backend_failure(self):
        """The negative control: only a CLASSIFIED teardown gets the new semantics."""

        key = "session-eof-real:/tmp/work"
        controller, agent, request = self._eof_agent(key, contained=False, returncode=-6)

        with patch("core.message_mirror.persist_agent_message"):
            await agent._handle_receiver_eof(key, request.context)

        self.assert_uncontained_settlement(controller.emit_agent_message, "-6")

    async def test_receiver_eof_without_a_returncode_settles_as_a_backend_failure(self):
        """A live process that merely stopped talking was never classified at all.

        This is the branch that skips ``handle_session_error`` entirely, so
        ``contained`` is only ever its initial value. If that default were True the
        most common EOF — no returncode, no explanation — would silently become an
        infrastructure interruption.
        """

        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        agent.record_model_hub_native_failure = AsyncMock()
        key = "session-eof-alive:/tmp/work"
        request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "eof-turn",
                    "agent_runtime_turn_token": "eof-runtime",
                }
            ),
        )
        agent._pending_requests[key] = [request]
        agent._remove_specific_pending_reaction = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        agent.session_handler = SimpleNamespace(cleanup_session=AsyncMock())
        controller.claude_sessions[key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=None)),
        )
        controller.receiver_tasks[key] = asyncio.current_task()

        await agent._handle_receiver_eof(key, request.context)

        self.assert_uncontained_settlement(
            controller.emit_agent_message,
            "Claude receiver ended without a terminal result",
        )

    def _receiver_exception_agent(self, composite_key, *, contained):
        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        agent.record_model_hub_native_failure = AsyncMock()
        request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "exc-turn",
                    "agent_runtime_turn_token": "exc-runtime",
                }
            ),
        )
        agent._pending_requests[composite_key] = [request]
        agent._clear_pending_reactions = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(return_value=contained),
            claude_teardown_is_intentional=lambda *_a, **_k: contained,
            mark_session_idle=lambda _key: None,
            cleanup_session=AsyncMock(),
            claude_error_diagnostic=lambda _key, _error: "Claude process terminated: SIGTERM",
        )
        controller.claude_sessions[composite_key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-15)),
        )
        return controller, agent, request

    async def test_receiver_exception_contained_teardown_settles_as_backend_refresh(self):
        key = "session-exc-settle:/tmp/work"
        controller, agent, request = self._receiver_exception_agent(key, contained=True)

        await agent._handle_receiver_exception(
            key, request.context, RuntimeError("Cannot write to terminated process (exit code: -15)")
        )

        self.assert_contained_settlement(controller.emit_agent_message)

    async def test_receiver_exception_real_failure_still_settles_as_a_backend_failure(self):
        key = "session-exc-real:/tmp/work"
        controller, agent, request = self._receiver_exception_agent(key, contained=False)

        with patch("core.message_mirror.persist_agent_message"):
            await agent._handle_receiver_exception(
                key, request.context, RuntimeError("boom")
            )

        self.assert_uncontained_settlement(controller.emit_agent_message, "SIGTERM")

    async def test_receiver_exception_requeues_a_claimed_activity_batch(self):
        """A dying receiver must not take an Activity claim down with it.

        The EOF path already requeues before its terminal emit; this one only
        peeked at the FIFO head, so the request still owned the batch and the
        ``run_ids`` on its ``activity_completion_output``. With the contained
        settlement declining to complete the Run, emitting on that output would
        skip those Runs AND drop the claim -- the batch would be neither
        delivered nor redeliverable. Requeueing restores it and strips the
        provenance the release can no longer honour.
        """

        key = "session-exc-activity:/tmp/work"
        controller, agent, request = self._receiver_exception_agent(key, contained=True)
        activity = SessionActivity(
            id="activity-1",
            backend="claude",
            runtime_key=key,
            session_id="ses-1",
            kind="background_task",
            status="completed",
            run_id="run-1",
        )
        request.output_activities = [activity]
        request.output = activity_completion_output(
            activity,
            activities=[activity],
            detached=False,
            completes_turn=True,
        )
        registry = SimpleNamespace(requeue_completed_outputs=Mock())
        agent._activity_registry = lambda: registry

        await agent._handle_receiver_exception(
            key,
            request.context,
            RuntimeError("Cannot write to terminated process (exit code: -15)"),
        )

        registry.requeue_completed_outputs.assert_called_once_with([activity])
        self.assertEqual(request.output_activities, [])
        self.assert_contained_settlement(controller.emit_agent_message)
        released = controller.emit_agent_message.await_args.kwargs["output"]
        self.assertEqual(released.activity_ids, ())
        self.assertIsNone(released.activity_batch_id)
        self.assertEqual(released.run_ids, ())

    async def test_direct_query_contained_teardown_settles_as_backend_refresh(self):
        """The third path: the query itself fails, no receiver ever runs."""

        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        runtime_key = "wechat_o9:reviewer:/tmp/work"
        client = SimpleNamespace(
            query=AsyncMock(
                side_effect=RuntimeError("Cannot write to terminated process (exit code: -9)")
            ),
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-9)),
            _vibe_runtime_base_session_id="wechat_o9:reviewer",
            _vibe_runtime_session_key=runtime_key,
        )
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=lambda _key: None,
            mark_session_idle=lambda _key: None,
            handle_session_error=AsyncMock(return_value=True),
            claude_teardown_is_intentional=lambda *_a, **_k: True,
            cleanup_session=AsyncMock(),
            capture_session_id=lambda *_: None,
        )
        agent = ClaudeAgent(controller)
        agent.record_model_hub_native_failure = AsyncMock()
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            message="hello",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id="wechat_o9:/tmp/work",
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            files=None,
        )

        with patch("core.message_mirror.persist_agent_message") as persist:
            await agent.handle_message(request)

        agent.record_model_hub_native_failure.assert_not_awaited()
        persist.assert_not_called()
        self.assert_contained_settlement(controller.emit_agent_message)

    async def test_direct_query_requeues_a_claimed_activity_batch(self):
        """The same claim leak on the path where no receiver ever runs.

        The request joins the FIFO before ``client.query`` is awaited, so a
        background Activity that finishes while the query is in flight attaches
        its batch to exactly this request -- the race the auth branch above
        already requeues for. If the query then fails into a contained teardown,
        the release declines to complete the Run and drops the claim with it.
        Requeue like both receiver paths do.
        """

        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        runtime_key = "wechat_o9:activity:/tmp/work"
        activity = SessionActivity(
            id="activity-2",
            backend="claude",
            runtime_key=runtime_key,
            session_id="ses-2",
            kind="background_task",
            status="completed",
            run_id="run-2",
        )
        request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            message="hello",
            working_path="/tmp/work",
            base_session_id="wechat_o9",
            composite_session_id="wechat_o9:/tmp/work",
            session_key="wechat-user",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            ack_message_id=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            files=None,
        )

        async def _query_after_activity_lands(*_args, **_kwargs):
            # The batch attaches to the queued request mid-query, then the
            # transport turns out to be gone.
            request.output_activities = [activity]
            request.output = activity_completion_output(
                activity,
                activities=[activity],
                detached=False,
                completes_turn=True,
            )
            raise RuntimeError("Cannot write to terminated process (exit code: -9)")

        client = SimpleNamespace(
            query=AsyncMock(side_effect=_query_after_activity_lands),
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-9)),
            _vibe_runtime_base_session_id="wechat_o9:activity",
            _vibe_runtime_session_key=runtime_key,
        )
        controller.session_handler = SimpleNamespace(
            get_or_create_claude_session=AsyncMock(return_value=client),
            mark_session_active=lambda _key: None,
            mark_session_idle=lambda _key: None,
            handle_session_error=AsyncMock(return_value=True),
            claude_teardown_is_intentional=lambda *_a, **_k: True,
            cleanup_session=AsyncMock(),
            capture_session_id=lambda *_: None,
        )
        agent = ClaudeAgent(controller)
        agent.record_model_hub_native_failure = AsyncMock()
        agent._prepare_message_with_files = lambda req: req.message
        agent._delete_ack = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        registry = SimpleNamespace(
            requeue_completed_outputs=Mock(),
            has_active=lambda *_a, **_k: False,
            has_completed_output=lambda *_a, **_k: False,
        )
        agent._activity_registry = lambda: registry

        with patch("core.message_mirror.persist_agent_message"):
            await agent.handle_message(request)

        registry.requeue_completed_outputs.assert_called_once_with([activity])
        self.assertEqual(request.output_activities, [])
        self.assert_contained_settlement(controller.emit_agent_message)
        released = controller.emit_agent_message.await_args.kwargs["output"]
        self.assertEqual(released.activity_ids, ())
        self.assertIsNone(released.activity_batch_id)
        self.assertEqual(released.run_ids, ())

    async def test_receiver_eof_terminated_auth_adopts_turn_before_recovery(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "telegram::user::U1"
        recovery_contexts = []

        async def _recover(context, *_args, **_kwargs):
            recovery_contexts.append(context)
            self.assertEqual(context.platform_specific["turn_token"], "eof-auth-turn")
            self.assertEqual(
                context.platform_specific["agent_runtime_turn_token"],
                "eof-auth-runtime",
            )
            return True

        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(
            side_effect=_recover,
        )
        agent = ClaudeAgent(controller)
        composite_key = "session-eof-auth:/tmp/work"
        stale_context = SimpleNamespace(
            platform_specific={
                "turn_token": "old-turn",
                "agent_runtime_turn_token": "old-runtime",
            }
        )
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "eof-auth-turn",
                    "agent_runtime_turn_token": "eof-auth-runtime",
                }
            ),
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent._pending_reactions[composite_key] = [("m1", "eyes")]
        agent._remove_ack_reaction = AsyncMock()
        agent._clear_pending_reactions = AsyncMock()
        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(),
            cleanup_session=AsyncMock(),
            claude_error_diagnostic=lambda _key, _error: (
                "OAuth login failed; Claude process terminated: SIGABRT"
            ),
        )
        controller.claude_sessions[composite_key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-6)),
        )
        controller.receiver_tasks[composite_key] = asyncio.current_task()

        await agent._handle_receiver_eof(composite_key, stale_context)

        self.assertEqual(recovery_contexts, [stale_context])
        agent.session_handler.handle_session_error.assert_not_awaited()
        agent.session_handler.cleanup_session.assert_awaited_once_with(
            composite_key,
            current_receiver_task=asyncio.current_task(),
            activation_retired=False,
            reason="receiver_auth_failure",
        )
        self.assertFalse(agent._pending_requests.get(composite_key))

    async def test_receiver_non_auth_error_settles_dot_and_persists(self):
        # A non-auth receiver error (connection loss, concurrent read, …) is NOT
        # handled by the OAuth-recovery path, so it must still settle the terminal
        # turn through the OUTBOUND chokepoint (empty error result → dot red + SSE
        # release) AND persist a durable error notify for the web Chat — instead of
        # hanging running to the 600s stream timeout and then settling idle (Codex P2).
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=False)
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        agent.session_handler = SimpleNamespace(handle_session_error=AsyncMock())
        agent._clear_pending_reactions = AsyncMock()
        context = SimpleNamespace()

        class _FailingClient:
            def receive_messages(self):
                async def _iterate():
                    raise RuntimeError("Connection lost")
                    yield  # pragma: no cover

                return _iterate()

        with patch("core.message_mirror.persist_agent_message") as persist:
            await agent._receive_messages(_FailingClient(), "session-1", "/tmp/work", context)

        agent.session_handler.handle_session_error.assert_awaited_once()
        controller.emit_agent_message.assert_awaited_once_with(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=ANY,
            terminal_error="Connection lost",
        )
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[1], "notify")
        self.assertEqual(persist.call_args.args[2], "❌ Claude error: Connection lost")
        self.assertEqual(persist.call_args.kwargs["metadata"]["backend"], "claude")
        self.assertEqual(persist.call_args.kwargs["metadata"]["event"], "backend_failure")
        self.assertTrue(persist.call_args.kwargs["metadata"]["failure_id"])
        self.assertTrue(
            persist.call_args.kwargs["native_message_id"].startswith("backend-failure:")
        )

    async def test_receiver_buffer_error_persists_connection_lost_notify(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=False)
        controller.emit_agent_message = AsyncMock()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        agent.session_handler = SimpleNamespace(
            handle_session_error=AsyncMock(),
            _t=lambda key: "Connection to Claude was lost. Please try your message again."
            if key == "error.sessionConnectionLost"
            else key,
        )
        agent._clear_pending_reactions = AsyncMock()
        context = SimpleNamespace()

        class _FailingClient:
            def receive_messages(self):
                async def _iterate():
                    raise RuntimeError(
                        "Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"
                    )
                    yield  # pragma: no cover

                return _iterate()

        with patch("core.message_mirror.persist_agent_message") as persist:
            await agent._receive_messages(_FailingClient(), "session-1", "/tmp/work", context)

        agent.session_handler.handle_session_error.assert_awaited_once()
        terminal_error = "Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"
        controller.emit_agent_message.assert_awaited_once_with(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=ANY,
            terminal_error=terminal_error,
        )
        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once_with(
            context,
            "claude",
            "❌ Connection to Claude was lost. Please try your message again.",
            output=ANY,
            terminal_error=terminal_error,
        )
        persist.assert_called_once()
        self.assertEqual(
            persist.call_args.args,
            (
                context,
                "notify",
                "❌ Connection to Claude was lost. Please try your message again.",
            ),
        )
        self.assertEqual(persist.call_args.kwargs["metadata"]["backend"], "claude")
        self.assertEqual(persist.call_args.kwargs["metadata"]["event"], "backend_failure")
        self.assertTrue(persist.call_args.kwargs["metadata"]["failure_id"])
        self.assertTrue(
            persist.call_args.kwargs["native_message_id"].startswith("backend-failure:")
        )

    async def test_result_auth_error_prefers_oauth_recovery_message(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        controller._get_session_key = lambda context: "telegram::user::U1"
        controller.emit_agent_message = AsyncMock()
        controller.mark_turn_complete = MagicMock()
        agent = ClaudeAgent(controller)
        agent._clear_pending_reactions = AsyncMock()
        agent.emit_result_message = AsyncMock()
        # platform=None keeps the durable notify a no-op (no real-state write);
        # platform_specific is the dict the failed turn's token is adopted into.
        context = SimpleNamespace(platform=None, platform_specific=None)
        composite_key = "session-1:/tmp/work"
        # A failed turn's pending request lingers in the FIFO (preserved for resume);
        # the auth-failure path must retire it so the NEXT turn isn't desynced (#216).
        failed_req = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "Ta"}))
        agent._pending_requests[composite_key] = [failed_req]
        current_task = asyncio.current_task()
        controller.receiver_tasks[composite_key] = current_task
        controller.claude_sessions[composite_key] = _StubClient()

        ResultMessage = type("ResultMessage", (), {})
        init_message = type(
            "SystemMessage",
            (),
            {"subtype": "init", "data": {"session_id": "session-sdk"}},
        )()
        error_result = ResultMessage()
        error_result.subtype = "error"
        error_result.result = (
            'Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error",'
            '"message":"Invalid bearer token"}}'
        )
        error_result.duration_ms = 0

        class _Client:
            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    yield init_message
                    yield error_result

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once()
        controller.session_handler.cleanup_session.assert_awaited_once_with(
            composite_key,
            current_receiver_task=asyncio.current_task(),
            activation_retired=False,
            reason="transport_auth_failure",
        )
        self.assertNotIn(composite_key, controller.receiver_tasks)
        self.assertNotIn(composite_key, controller.claude_sessions)
        # #216: the failed turn's pending request was retired from the FIFO (so the
        # next successful turn won't adopt its stale token), and the streaming Chat
        # turn was released under that token instead of hanging to the timeout.
        self.assertFalse(agent._pending_requests.get(composite_key))
        self.assertEqual(context.platform_specific.get("turn_token"), "Ta")
        controller.mark_turn_complete.assert_called_once()
        agent.emit_result_message.assert_not_awaited()

    async def test_receive_init_message_attaches_native_session_id_to_runtime_client(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "slack::channel::C1"
        controller.emit_agent_message = AsyncMock()
        controller.session_handler.capture_session_id = lambda *args, **kwargs: "sesk8m4q2p7x"
        agent = ClaudeAgent(controller)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-1:/tmp/work"
        init_consumed = asyncio.Event()
        release_stream = asyncio.Event()

        init_message = type(
            "SystemMessage",
            (),
            {"subtype": "init", "data": {"session_id": "session-sdk"}},
        )()

        class _Client:
            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    yield init_message
                    init_consumed.set()
                    await release_stream.wait()

                return _iterate()

        runtime_client = _Client()
        controller.claude_sessions[composite_key] = runtime_client
        agent.emit_result_message = AsyncMock()

        receiver_task = asyncio.create_task(
            agent._receive_messages(
                runtime_client,
                "session-1",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        await init_consumed.wait()

        self.assertEqual(getattr(runtime_client, "_vibe_native_session_id"), "session-sdk")
        self.assertEqual(agent._native_session_ids[composite_key], "session-sdk")
        controller.emit_agent_message.assert_not_awaited()

        release_stream.set()
        await receiver_task

    async def test_receive_legacy_refusal_fallback_notifies_without_dropping_replacement_text(self):
        controller = _StubController()
        controller._get_session_key = lambda _context: "avibe::project::p1"
        controller.im_client.formatter = SimpleNamespace(
            escape_special_chars=lambda text: text,
            format_assistant_message=lambda parts: "\n\n".join(parts),
        )
        controller.session_handler = SimpleNamespace(
            capture_session_id=lambda *_args, **_kwargs: None,
            mark_session_idle=lambda _key: None,
            touch_session_activity=lambda _key: None,
            _t=lambda key, **kwargs: (
                (
                    "Claude's safety checks triggered for this request. "
                    f"It switched from {kwargs['originalModel']} to {kwargs['fallbackModel']} and retried. "
                    f"This session will continue on {kwargs['fallbackModel']}."
                )
                if key == "status.claudeRefusalFallback"
                else key
            ),
        )
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={
                "turn_token": "stale-turn",
                "agent_runtime_turn_token": "stale-runtime",
            },
        )
        pending_context = SimpleNamespace(
            platform_specific={
                "turn_token": "current-turn",
                "agent_runtime_turn_key": "session-1:/tmp/work",
                "agent_runtime_turn_token": "current-runtime",
            }
        )
        pending_request = SimpleNamespace(context=pending_context)
        composite_key = "session-1:/tmp/work"
        agent._pending_requests[composite_key] = [pending_request]

        emitted = []

        async def _emit(message_context, message_type, text, **kwargs):
            emitted.append(
                (
                    message_type,
                    text,
                    kwargs,
                    dict(message_context.platform_specific),
                )
            )

        controller.emit_agent_message = AsyncMock(side_effect=_emit)
        replacement_block = TextBlock.__new__(TextBlock)
        replacement_block.text = "replacement answer"
        replacement_message = type(
            "AssistantMessage",
            (),
            {
                "content": [replacement_block],
                "model": "claude-opus-4-8",
                "usage": None,
                "error": None,
            },
        )()
        fallback_message = type(
            "ModelRefusalFallbackMessage",
            (),
            {
                "subtype": "model_refusal_fallback",
                "data": {
                    "trigger": "refusal",
                    "originalModel": "claude-fable-5[1m]",
                    "fallbackModel": "claude-opus-4-8",
                },
            },
        )()
        result_message = type(
            "ResultMessage",
            (),
            {"subtype": "success", "result": None, "duration_ms": 1},
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield replacement_message
                    yield fallback_message
                    yield result_message

                return _iterate()

        await agent._receive_messages(
            _Client(),
            "session-1",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        self.assertEqual(
            emitted,
            [
                (
                    "notify",
                    "Claude's safety checks triggered for this request. It switched from claude-fable-5[1m] "
                    "to claude-opus-4-8 and retried. This session will continue on "
                    "claude-opus-4-8.",
                    {"parse_mode": "markdown"},
                    {
                        "turn_token": "current-turn",
                        "agent_runtime_turn_key": composite_key,
                        "agent_runtime_turn_token": "current-runtime",
                    },
                )
            ],
        )
        self.assertNotIn(composite_key, agent._pending_assistant_message)
        self.assertNotIn(composite_key, agent._last_assistant_text)
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "replacement answer",
            subtype="success",
            duration_ms=1,
            parse_mode="markdown",
            request=pending_request,
        )

    async def test_init_message_binds_native_session_to_existing_agent_session(self):
        controller = _StubController()
        binds = []
        controller.session_handler = SimpleNamespace(
            bind_agent_session_id=lambda **kwargs: binds.append(kwargs) or "sesk8m4q2p7x"
        )
        agent = ClaudeAgent(controller)
        context = SimpleNamespace(platform_specific={})
        init_message = type(
            "SystemMessage",
            (),
            {"subtype": "init", "data": {"session_id": "session-sdk"}},
        )()

        session_id = agent._maybe_capture_session_id(
            init_message,
            "session-1",
            "slack::channel::C1",
            context,
            working_path="/tmp/work",
        )

        self.assertEqual(session_id, "session-sdk")
        self.assertEqual(context.platform_specific["agent_session_id"], "sesk8m4q2p7x")
        self.assertEqual(
            binds,
            [
                {
                    "session_key": "slack::channel::C1",
                    "agent_name": "claude",
                    "session_anchor": "session-1",
                    "native_session_id": "session-sdk",
                    "working_path": "/tmp/work",
                }
            ],
        )

    async def test_init_message_persists_routing_subagent_native_under_namespaced_anchor(self):
        controller = _StubController()
        binds = []
        controller.session_handler = SimpleNamespace(
            bind_agent_session_id=lambda **kwargs: binds.append(kwargs) or "ses_subagent"
        )
        agent = ClaudeAgent(controller)
        context = SimpleNamespace(
            platform_specific={
                "agent_session_target": {"id": "ses_main"},
                "routing_subagent": "reviewer",
            }
        )
        init_message = type(
            "SystemMessage",
            (),
            {"subtype": "init", "data": {"session_id": "session-sdk-reviewer"}},
        )()

        session_id = agent._maybe_capture_session_id(
            init_message,
            "session-1:reviewer",
            "avibe::project::p1",
            context,
            working_path="/tmp/work",
        )

        self.assertEqual(session_id, "session-sdk-reviewer")
        self.assertEqual(context.platform_specific["agent_session_id"], "ses_main")
        self.assertEqual(
            binds,
            [
                {
                    "session_key": "avibe::project::p1",
                    "agent_name": "claude",
                    "session_anchor": "session-1:reviewer",
                    "native_session_id": "session-sdk-reviewer",
                    "working_path": "/tmp/work",
                }
            ],
        )

    async def test_result_auth_error_exits_receiver_and_disconnects_client(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        controller._get_session_key = lambda context: "telegram::user::U1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._clear_pending_reactions = AsyncMock()
        context = SimpleNamespace()
        composite_key = "session-1:/tmp/work"
        disconnect_started = asyncio.Event()

        ResultMessage = type("ResultMessage", (), {})
        init_message = type(
            "SystemMessage",
            (),
            {"subtype": "init", "data": {"session_id": "session-sdk"}},
        )()
        error_result = ResultMessage()
        error_result.subtype = "error"
        error_result.result = (
            'Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error",'
            '"message":"Invalid bearer token"}}'
        )
        error_result.duration_ms = 0

        class _Client:
            async def disconnect(self):
                disconnect_started.set()

            def receive_messages(self):
                async def _iterate():
                    yield init_message
                    yield error_result
                    await asyncio.Future()

                return _iterate()

        client = _Client()
        controller.claude_sessions[composite_key] = client
        receiver_task = asyncio.create_task(agent._receive_messages(client, "session-1", "/tmp/work", context))
        controller.receiver_tasks[composite_key] = receiver_task

        await asyncio.wait_for(receiver_task, timeout=1)
        await asyncio.wait_for(disconnect_started.wait(), timeout=1)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once()
        self.assertNotIn(composite_key, controller.receiver_tasks)
        self.assertNotIn(composite_key, controller.claude_sessions)

    async def test_receiver_eof_without_result_releases_runtime_gate(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service

        async def _emit(context, message_type, text, **_kwargs):
            if message_type == "result":
                service.release_runtime_turn(context)

        controller.emit_agent_message = AsyncMock(side_effect=_emit)
        composite_key = "session-1:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "R1",
            },
        )
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "T1",
                    "agent_runtime_turn_key": composite_key,
                    "agent_runtime_turn_token": "R1",
                }
            ),
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "R1"

        class _Client:
            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    if False:
                        yield None

                return _iterate()

        client = _Client()
        controller.claude_sessions[composite_key] = client
        receiver_task = asyncio.create_task(
            agent._receive_messages(client, "session-1", "/tmp/work", context, composite_key=composite_key)
        )
        controller.receiver_tasks[composite_key] = receiver_task

        await asyncio.wait_for(receiver_task, timeout=1)

        controller.emit_agent_message.assert_awaited_once_with(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=ANY,
            terminal_error="Claude receiver ended without a terminal result",
        )
        self.assertFalse(service._turn_gates[composite_key].lock.locked())
        self.assertNotIn(composite_key, controller.claude_sessions)
        self.assertNotIn(composite_key, agent._pending_requests)

    async def test_receiver_eof_cleans_runtime_before_terminal_emit_releases_gate(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service
        composite_key = "session-1:/tmp/work"
        release_seen_state: dict[str, bool] = {}

        async def _emit(context, message_type, text, **_kwargs):
            if message_type == "result":
                release_seen_state["client_present"] = composite_key in controller.claude_sessions
                release_seen_state["receiver_present"] = composite_key in controller.receiver_tasks
                service.release_runtime_turn(context)

        controller.emit_agent_message = AsyncMock(side_effect=_emit)
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "R1",
            },
        )
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "T1",
                    "agent_runtime_turn_key": composite_key,
                    "agent_runtime_turn_token": "R1",
                }
            ),
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "R1"

        class _Client:
            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    if False:
                        yield None

                return _iterate()

        client = _Client()
        controller.claude_sessions[composite_key] = client
        receiver_task = asyncio.create_task(
            agent._receive_messages(client, "session-1", "/tmp/work", context, composite_key=composite_key)
        )
        controller.receiver_tasks[composite_key] = receiver_task

        await asyncio.wait_for(receiver_task, timeout=1)

        self.assertEqual(release_seen_state, {"client_present": False, "receiver_present": False})
        self.assertFalse(service._turn_gates[composite_key].lock.locked())

    async def test_force_cleanup_stuck_active_session_retires_pending_turn(self):
        controller = _StubController()
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        composite_key = "session-1:/tmp/work"
        pending_context = SimpleNamespace(
            platform_specific={
                "turn_token": "current",
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "current-runtime",
            }
        )
        pending_request = SimpleNamespace(
            context=pending_context,
            ack_reaction_message_id="m1",
            ack_reaction_emoji="eyes",
        )
        next_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "next"}))
        agent._pending_requests[composite_key] = [pending_request, next_request]
        agent._pending_reactions[composite_key] = [("m1", "eyes"), ("m2", "eyes")]
        agent._last_assistant_text[composite_key] = "stale"
        agent._pending_assistant_message[composite_key] = "pending"
        agent._remove_ack_reaction = AsyncMock()

        await agent.force_cleanup_stuck_active_session(composite_key)

        controller.session_handler.cleanup_session.assert_awaited_once_with(
            composite_key,
            current_receiver_task=None,
            activation_retired=False,
            reason="stuck_active_eviction",
        )
        agent._remove_ack_reaction.assert_awaited_once_with(pending_request)
        controller.emit_agent_message.assert_awaited_once_with(
            pending_context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=ANY,
        )
        self.assertEqual(pending_context.platform_specific["turn_token"], "current")
        self.assertEqual(pending_context.platform_specific["agent_runtime_turn_token"], "current-runtime")
        self.assertEqual(agent._pending_requests[composite_key], [next_request])
        self.assertEqual(agent._pending_reactions[composite_key], [("m2", "eyes")])
        self.assertNotIn(composite_key, agent._last_assistant_text)
        self.assertNotIn(composite_key, agent._pending_assistant_message)

    async def test_receiver_eof_adopts_pending_turn_token_when_context_is_stale(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "telegram::user::U1"
        agent = ClaudeAgent(controller)
        service = AgentService(controller)
        service.register(agent)
        controller.agent_service = service

        async def _emit(context, message_type, text, **_kwargs):
            if message_type == "result":
                service.release_runtime_turn(context)

        controller.emit_agent_message = AsyncMock(side_effect=_emit)
        composite_key = "session-1:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={
                "turn_token": "old-turn",
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "old-runtime",
            },
        )
        pending_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "turn_token": "current-turn",
                    "agent_runtime_turn_key": composite_key,
                    "agent_runtime_turn_token": "current-runtime",
                }
            ),
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [pending_request]
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "current-runtime"

        class _Client:
            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    if False:
                        yield None

                return _iterate()

        client = _Client()
        controller.claude_sessions[composite_key] = client
        receiver_task = asyncio.create_task(
            agent._receive_messages(client, "session-1", "/tmp/work", context, composite_key=composite_key)
        )
        controller.receiver_tasks[composite_key] = receiver_task

        await asyncio.wait_for(receiver_task, timeout=1)

        controller.emit_agent_message.assert_awaited_once_with(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=ANY,
            terminal_error="Claude receiver ended without a terminal result",
        )
        self.assertEqual(context.platform_specific["turn_token"], "current-turn")
        self.assertEqual(context.platform_specific["agent_runtime_turn_token"], "current-runtime")
        self.assertFalse(service._turn_gates[composite_key].lock.locked())
        self.assertNotIn(composite_key, agent._pending_requests)

    async def test_assistant_auth_error_prefers_oauth_recovery_message(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        controller._get_session_key = lambda context: "telegram::user::U1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent._extract_text_blocks = lambda message, context: (
            'Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error",'
            '"message":"Invalid bearer token"}}'
        )
        context = SimpleNamespace()
        composite_key = "session-1:/tmp/work"
        current_task = asyncio.current_task()
        controller.receiver_tasks[composite_key] = current_task
        controller.claude_sessions[composite_key] = _StubClient()
        pending_request_1 = SimpleNamespace()
        pending_request_2 = SimpleNamespace()
        agent._pending_requests[composite_key] = [pending_request_1, pending_request_2]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:"), ("m2", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "error": "authentication_failed",
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once()
        controller.session_handler.cleanup_session.assert_awaited_once_with(
            composite_key,
            current_receiver_task=asyncio.current_task(),
            activation_retired=False,
            reason="transport_auth_failure",
        )
        self.assertEqual(agent._remove_ack_reaction.await_count, 2)
        self.assertEqual(agent._remove_ack_reaction.await_args_list[0].args, (pending_request_1,))
        self.assertEqual(agent._remove_ack_reaction.await_args_list[1].args, (pending_request_2,))
        self.assertNotIn(composite_key, controller.receiver_tasks)
        self.assertNotIn(composite_key, controller.claude_sessions)
        self.assertNotIn(composite_key, agent._pending_requests)
        self.assertNotIn(composite_key, agent._pending_reactions)

    async def test_assistant_auth_error_without_text_blocks_still_triggers_recovery(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        controller._get_session_key = lambda context: "telegram::user::U1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        context = SimpleNamespace()
        composite_key = "session-1:/tmp/work"
        current_task = asyncio.current_task()
        controller.receiver_tasks[composite_key] = current_task
        controller.claude_sessions[composite_key] = _StubClient()
        pending_request = SimpleNamespace()
        agent._pending_requests[composite_key] = [pending_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "error": "authentication_failed",
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once_with(
            context,
            "claude",
            "❌ Claude error: OAuth authentication failed.",
            output=ANY,
            terminal_error="OAuth authentication failed.",
        )
        agent._remove_ack_reaction.assert_awaited_once_with(pending_request)
        self.assertNotIn(composite_key, controller.receiver_tasks)
        self.assertNotIn(composite_key, controller.claude_sessions)

    async def test_synthetic_api_error_settles_turn_without_user_visible_result(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent.emit_result_message = AsyncMock()
        error_text = "The model's tool call could not be parsed (retry also failed)."
        agent._extract_text_blocks = lambda message, context: error_text
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={},
        )
        composite_key = "session-1:/tmp/work"
        pending_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        next_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T2"}))
        agent._pending_requests[composite_key] = [pending_request, next_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:"), ("m2", ":eyes:")]
        agent._pending_assistant_message[composite_key] = "stale assistant"
        agent._last_assistant_text[composite_key] = "stale text"

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "model": "<synthetic>",
                "error": None,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        self.assert_backend_failure_emitted(
            controller.emit_agent_message,
            context,
            f"❌ Claude error: {error_text}",
            error_text,
        )
        agent.emit_result_message.assert_not_awaited()
        agent._remove_ack_reaction.assert_awaited_once_with(pending_request)
        self.assertEqual(context.platform_specific["turn_token"], "T1")
        self.assertEqual(agent._pending_requests[composite_key], [next_request])
        self.assertEqual(agent._pending_reactions[composite_key], [("m2", ":eyes:")])
        self.assertNotIn(composite_key, agent._pending_assistant_message)
        self.assertNotIn(composite_key, agent._last_assistant_text)

    async def test_synthetic_api_error_suppresses_paired_result_without_popping_next_request(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent.emit_result_message = AsyncMock()
        error_text = "The model's tool call could not be parsed (retry also failed)."
        agent._extract_text_blocks = lambda message, context: error_text
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={},
        )
        composite_key = "session-1:/tmp/work"
        failed_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        next_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T2"}))
        agent._pending_requests[composite_key] = [failed_request, next_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:"), ("m2", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "model": "<synthetic>",
                "error": None,
            },
        )()
        result_message = type(
            "ResultMessage",
            (),
            {"subtype": "error", "result": error_text, "duration_ms": 1},
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message
                    yield result_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        self.assert_backend_failure_emitted(
            controller.emit_agent_message,
            context,
            f"❌ Claude error: {error_text}",
            error_text,
        )
        agent.emit_result_message.assert_not_awaited()
        self.assertEqual(agent._pending_requests[composite_key], [next_request])
        self.assertEqual(agent._pending_reactions[composite_key], [("m2", ":eyes:")])
        self.assertNotIn(composite_key, agent._suppressed_synthetic_results)

    async def test_synthetic_api_error_suppresses_next_result_even_when_text_differs(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent.emit_result_message = AsyncMock()
        agent._extract_text_blocks = lambda message, context: (
            "The model's tool call could not be parsed (retry also failed)."
        )
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={},
        )
        composite_key = "session-1:/tmp/work"
        failed_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        next_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T2"}))
        agent._pending_requests[composite_key] = [failed_request, next_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:"), ("m2", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "model": "<synthetic>",
                "error": None,
            },
        )()
        result_message = type(
            "ResultMessage",
            (),
            {
                "subtype": "error",
                "result": "The tool-use request failed after Claude Code retried parsing.",
                "is_error": True,
                "duration_ms": 1,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message
                    yield result_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        diagnostic = "The model's tool call could not be parsed (retry also failed)."
        self.assert_backend_failure_emitted(
            controller.emit_agent_message,
            context,
            f"❌ Claude error: {diagnostic}",
            diagnostic,
        )
        agent.emit_result_message.assert_not_awaited()
        self.assertEqual(agent._pending_requests[composite_key], [next_request])
        self.assertEqual(agent._pending_reactions[composite_key], [("m2", ":eyes:")])
        self.assertNotIn(composite_key, agent._suppressed_synthetic_results)

    async def test_synthetic_api_error_keeps_marker_when_followup_queues_before_paired_result(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent.emit_result_message = AsyncMock()
        agent._extract_text_blocks = lambda message, context: (
            "The model's tool call could not be parsed (retry also failed)."
        )
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={},
        )
        composite_key = "session-1:/tmp/work"
        failed_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        agent._pending_requests[composite_key] = [failed_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "model": "<synthetic>",
                "error": None,
            },
        )()
        paired_result = type(
            "ResultMessage",
            (),
            {
                "subtype": "error",
                "result": "The model's tool call could not be parsed (retry also failed).",
                "duration_ms": 1,
            },
        )()

        class _DelayedPairedResultClient:
            def __init__(self):
                self.ready = asyncio.Event()

            def receive_messages(self):
                async def _iterate():
                    yield assistant_message
                    await self.ready.wait()
                    yield paired_result

                return _iterate()

        client = _DelayedPairedResultClient()
        receiver_task = asyncio.create_task(
            agent._receive_messages(client, "session-1", "/tmp/work", context, composite_key=composite_key)
        )

        while composite_key not in agent._suppressed_synthetic_results:
            await asyncio.sleep(0)

        followup_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T2"}))
        agent._pending_requests[composite_key] = [followup_request]
        agent._pending_reactions[composite_key] = [("m2", ":eyes:")]
        client.ready.set()
        await receiver_task

        diagnostic = "The model's tool call could not be parsed (retry also failed)."
        self.assert_backend_failure_emitted(
            controller.emit_agent_message,
            context,
            f"❌ Claude error: {diagnostic}",
            diagnostic,
        )
        agent.emit_result_message.assert_not_awaited()
        self.assertEqual(agent._pending_requests[composite_key], [followup_request])
        self.assertEqual(agent._pending_reactions[composite_key], [("m2", ":eyes:")])
        self.assertNotIn(composite_key, agent._suppressed_synthetic_results)

    async def test_synthetic_marker_does_not_suppress_non_error_followup_result_in_open_receiver(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent.emit_result_message = AsyncMock()
        agent._extract_text_blocks = lambda message, context: (
            "The model's tool call could not be parsed (retry also failed)."
        )
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={},
        )
        composite_key = "session-1:/tmp/work"
        failed_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        agent._pending_requests[composite_key] = [failed_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "model": "<synthetic>",
                "error": None,
            },
        )()
        followup_result = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "result": "next turn result",
                "is_error": False,
                "duration_ms": 1,
            },
        )()

        class _OpenReceiverClient:
            def __init__(self):
                self.ready = asyncio.Event()

            def receive_messages(self):
                async def _iterate():
                    yield assistant_message
                    await self.ready.wait()
                    yield followup_result

                return _iterate()

        client = _OpenReceiverClient()
        receiver_task = asyncio.create_task(
            agent._receive_messages(client, "session-1", "/tmp/work", context, composite_key=composite_key)
        )

        while composite_key not in agent._suppressed_synthetic_results:
            await asyncio.sleep(0)

        followup_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T2"}))
        agent._pending_requests[composite_key] = [followup_request]
        agent._pending_reactions[composite_key] = [("m2", ":eyes:")]
        client.ready.set()
        await receiver_task

        diagnostic = "The model's tool call could not be parsed (retry also failed)."
        self.assert_backend_failure_emitted(
            controller.emit_agent_message,
            context,
            f"❌ Claude error: {diagnostic}",
            diagnostic,
        )
        agent.emit_result_message.assert_awaited_once_with(
            context,
            "next turn result",
            subtype="success",
            duration_ms=1,
            parse_mode="markdown",
            request=followup_request,
        )
        self.assertNotIn(composite_key, agent._pending_requests)
        self.assertNotIn(composite_key, agent._suppressed_synthetic_results)

    async def test_synthetic_api_error_without_paired_result_does_not_suppress_later_turn(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent.emit_result_message = AsyncMock()
        agent._extract_text_blocks = lambda message, context: (
            "The model's tool call could not be parsed (retry also failed)."
        )
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={},
        )
        composite_key = "session-1:/tmp/work"
        failed_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        agent._pending_requests[composite_key] = [failed_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "model": "<synthetic>",
                "error": None,
            },
        )()

        class _SyntheticOnlyClient:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message

                return _iterate()

        await agent._receive_messages(
            _SyntheticOnlyClient(),
            "session-1",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        self.assertNotIn(composite_key, agent._suppressed_synthetic_results)

        next_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T2"}))
        agent._pending_requests[composite_key] = [next_request]
        agent._pending_reactions[composite_key] = [("m2", ":eyes:")]
        normal_result = type(
            "ResultMessage",
            (),
            {"subtype": "success", "result": "next turn result", "duration_ms": 1},
        )()

        class _ResultClient:
            def receive_messages(self):
                async def _iterate():
                    yield normal_result

                return _iterate()

        await agent._receive_messages(_ResultClient(), "session-1", "/tmp/work", context, composite_key=composite_key)

        agent.emit_result_message.assert_awaited_once_with(
            context,
            "next turn result",
            subtype="success",
            duration_ms=1,
            parse_mode="markdown",
            request=next_request,
        )
        self.assertNotIn(composite_key, agent._pending_requests)

    async def test_synthetic_server_error_notifies_and_suppresses_paired_result(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        error_text = "API Error: 503 provider unavailable"
        agent._extract_text_blocks = lambda message, context: error_text
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={},
        )
        composite_key = "session-1:/tmp/work"
        pending_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        agent._pending_requests[composite_key] = [pending_request]
        agent._pending_reactions[composite_key] = [("m1", ":eyes:")]

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "isApiErrorMessage": True,
                "model": "<synthetic>",
                "error": "server_error",
                "api_error_status": 503,
            },
        )()
        result_message = type(
            "ResultMessage",
            (),
            {"subtype": "error", "result": error_text, "duration_ms": 1},
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message
                    yield result_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once_with(
            context,
            "claude",
            f"❌ Claude error: {error_text}",
            output=ANY,
            terminal_error=error_text,
        )
        self.assertEqual(controller.emit_agent_message.await_count, 2)
        notify_call, terminal_call = controller.emit_agent_message.await_args_list
        self.assertEqual(notify_call.args[:3], (context, "notify", f"❌ Claude error: {error_text}"))
        self.assertFalse(notify_call.kwargs["output"].settles_run)
        self.assertEqual(terminal_call.args[:3], (context, "result", ""))
        self.assertTrue(terminal_call.kwargs["is_error"])
        self.assertEqual(terminal_call.kwargs["level"], "silent")
        self.assertEqual(terminal_call.kwargs["terminal_error"], error_text)
        agent.emit_result_message.assert_not_awaited()
        self.assertNotIn(composite_key, agent._pending_requests)

    async def test_result_is_error_fails_without_error_subtype(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-1:/tmp/work"
        pending_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        agent._pending_requests[composite_key] = [pending_request]
        result_message = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "is_error": True,
                "result": "provider rejected the request",
                "duration_ms": 1,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield result_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        self.assertEqual(controller.emit_agent_message.await_count, 2)
        agent.emit_result_message.assert_not_awaited()
        terminal_call = controller.emit_agent_message.await_args_list[1]
        self.assertEqual(terminal_call.kwargs["terminal_error"], "provider rejected the request")

    async def test_success_result_with_error_words_remains_agent_output(self):
        controller = _StubController()
        controller._get_session_key = lambda context: "avibe::project::p1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent.emit_result_message = AsyncMock()
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-1:/tmp/work"
        pending_request = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T1"}))
        agent._pending_requests[composite_key] = [pending_request]
        result_message = type(
            "ResultMessage",
            (),
            {
                "subtype": "success",
                "is_error": False,
                "result": "The test failed with an error; I fixed it.",
                "duration_ms": 1,
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield result_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context, composite_key=composite_key)

        agent.emit_result_message.assert_awaited_once_with(
            context,
            "The test failed with an error; I fixed it.",
            subtype="success",
            duration_ms=1,
            parse_mode="markdown",
            request=pending_request,
        )

    async def test_assistant_auth_error_without_is_api_error_flag_still_triggers_recovery(self):
        """Scenario: AUTH-SETUP-902"""
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        controller._get_session_key = lambda context: "telegram::user::U1"
        controller.emit_agent_message = AsyncMock()
        agent = ClaudeAgent(controller)
        agent._remove_ack_reaction = AsyncMock()
        agent._extract_text_blocks = lambda message, context: (
            'Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error",'
            '"message":"Invalid bearer token"}}'
        )
        context = SimpleNamespace()
        composite_key = "session-1:/tmp/work"
        current_task = asyncio.current_task()
        controller.receiver_tasks[composite_key] = current_task
        controller.claude_sessions[composite_key] = _StubClient()

        assistant_message = type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "error": "authentication_failed",
            },
        )()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield assistant_message

                return _iterate()

        await agent._receive_messages(_Client(), "session-1", "/tmp/work", context)

        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once()
        self.assertNotIn(composite_key, controller.receiver_tasks)
        self.assertNotIn(composite_key, controller.claude_sessions)

    async def test_handle_auth_failure_result_requires_explicit_error_subtype(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        agent = ClaudeAgent(controller)
        context = SimpleNamespace()

        handled = await agent._handle_auth_failure_result(
            context,
            "session-1:/tmp/work",
            "",
            "Let's talk about oauth login after this task finishes.",
        )

        self.assertFalse(handled)
        controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_not_awaited()


class AdoptPendingTurnTokenTests(unittest.TestCase):
    """Pending Turn identity replaces stale reused-receiver identity."""

    def test_adopts_pending_requests_token(self):
        # Reused receiver context still carries turn-1's token; the FIFO-matched
        # pending request is turn-2 → adopt T2 so completion correlates.
        ctx = SimpleNamespace(platform_specific={"turn_token": "T1"})
        pending = SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "T2"}))
        ClaudeAgent._adopt_pending_turn_token(ctx, pending)
        self.assertEqual(ctx.platform_specific["turn_token"], "T2")

    def test_noop_without_pending_request(self):
        ctx = SimpleNamespace(platform_specific={"turn_token": "T1"})
        ClaudeAgent._adopt_pending_turn_token(ctx, None)
        self.assertEqual(ctx.platform_specific["turn_token"], "T1")

    def test_noop_when_pending_request_has_no_token(self):
        # Fail-open: nothing to adopt → leave the context untouched (completion
        # then falls back to fail-open in _stream_chunk).
        ctx = SimpleNamespace(platform_specific={"turn_token": "T1"})
        pending = SimpleNamespace(context=SimpleNamespace(platform_specific={}))
        ClaudeAgent._adopt_pending_turn_token(ctx, pending)
        self.assertEqual(ctx.platform_specific["turn_token"], "T1")

    def test_adopts_agent_run_attribution(self):
        ctx = SimpleNamespace(
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-old",
            }
        )
        accepted = ["run-new", "run-child"]
        pending = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "task_trigger_kind": "agent_run",
                    "task_execution_id": "run-new",
                    "accepted_agent_run_ids": accepted,
                }
            )
        )

        ClaudeAgent._adopt_pending_turn_token(ctx, pending)

        self.assertEqual(ctx.platform_specific["task_execution_id"], "run-new")
        self.assertEqual(ctx.platform_specific["accepted_agent_run_ids"], accepted)
        self.assertIsNot(ctx.platform_specific["accepted_agent_run_ids"], accepted)

    def test_clears_stale_agent_run_attribution_for_plain_turn(self):
        ctx = SimpleNamespace(
            platform_specific={
                "turn_token": "T1",
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-old",
                "accepted_agent_run_ids": ["run-old"],
            }
        )
        pending = SimpleNamespace(
            context=SimpleNamespace(platform_specific={"turn_token": "T2"})
        )

        ClaudeAgent._adopt_pending_turn_token(ctx, pending)

        self.assertEqual(ctx.platform_specific["turn_token"], "T2")
        self.assertNotIn("task_trigger_kind", ctx.platform_specific)
        self.assertNotIn("task_execution_id", ctx.platform_specific)
        self.assertNotIn("accepted_agent_run_ids", ctx.platform_specific)


class _FakeBaseAgent(BaseAgent):
    """Minimal concrete BaseAgent so the shared session-binding helpers resolve
    normally (proper ``self`` method lookup), without a real controller."""

    def __init__(self, sessions, name="claude"):
        self.sessions = sessions
        self.name = name

    async def handle_message(self, request):  # pragma: no cover - abstract stub
        return None


class BindReservedWorkbenchSessionTests(unittest.TestCase):
    """``BaseAgent`` keeps Claude/Codex avibe replies attributed to the OPEN Chat
    session (the reserved workbench row), instead of a freshly-minted hidden row,
    so ``message.new`` reaches the page (Codex P1/P2)."""

    @staticmethod
    def _ctx(target_id):
        spec = {"agent_session_id": "from_build"}
        if target_id:
            spec["agent_session_target"] = {"id": target_id}
        return SimpleNamespace(platform_specific=spec)

    def test_avibe_turn_binds_by_reserved_id_and_pins_agent_session_id(self):
        calls = {}

        # SessionsFacade.bind_agent_session_by_id takes (agent_session_id,
        # native_session_id) POSITIONALLY — a session_id= keyword call would
        # TypeError and silently skip recording the native id (Codex P2).
        def bind_by_id(
            agent_session_id,
            native_session_id,
            workdir=None,
            vibe_agent_id=None,
            vibe_agent_name=None,
            vibe_agent_backend=None,
        ):
            calls.update(
                session_id=agent_session_id,
                native=native_session_id,
                workdir=workdir,
                vibe_agent_id=vibe_agent_id,
                vibe_agent_name=vibe_agent_name,
                vibe_agent_backend=vibe_agent_backend,
            )
            return agent_session_id  # the reserved row exists → rowcount 1

        agent = _FakeBaseAgent(SimpleNamespace(bind_agent_session_by_id=bind_by_id))
        ctx = self._ctx("ses_workbench")
        ctx.platform_specific["resolved_vibe_agent"] = {"id": "agent-codex", "name": "codex", "backend": "codex"}
        ret = agent._bind_reserved_workbench_session(ctx, "claude-native-123", working_path="/tmp/x")
        self.assertEqual(ret, "ses_workbench")
        self.assertEqual(ctx.platform_specific["agent_session_id"], "ses_workbench")
        self.assertEqual(
            calls,
            {
                "session_id": "ses_workbench",
                "native": "claude-native-123",
                "workdir": "/tmp/x",
                "vibe_agent_id": "agent-codex",
                "vibe_agent_name": "codex",
                "vibe_agent_backend": "codex",
            },
        )

    def test_reserved_bind_uses_resolved_target_workdir(self):
        calls = {}

        def bind_by_id(agent_session_id, native_session_id, workdir=None, **kwargs):
            calls.update(session_id=agent_session_id, native=native_session_id, workdir=workdir)
            return agent_session_id

        agent = _FakeBaseAgent(SimpleNamespace(bind_agent_session_by_id=bind_by_id))
        ctx = self._ctx("ses_workbench")
        ctx.platform_specific["agent_run_target"] = {
            "workdir": "/Users/alice/vibe-remote-project",
        }

        ret = agent._bind_reserved_workbench_session(ctx, "native-123", working_path="/tmp/test")

        self.assertEqual(ret, "ses_workbench")
        self.assertEqual(calls["workdir"], "/Users/alice/vibe-remote-project")

    def test_im_run_target_binds_native_session_by_resolved_row_id(self):
        calls = {}

        def bind_by_id(agent_session_id, native_session_id, workdir=None, **kwargs):
            calls.update(session_id=agent_session_id, native=native_session_id, workdir=workdir)
            return agent_session_id

        agent = _FakeBaseAgent(SimpleNamespace(bind_agent_session_by_id=bind_by_id))
        ctx = self._ctx(None)
        ctx.platform_specific["agent_run_target"] = {
            "agent_session_id": "ses_topic",
            "workdir": "/repo/topic",
        }

        ret = agent._bind_reserved_workbench_session(ctx, "native-topic", working_path="/repo/group")

        self.assertEqual(ret, "ses_topic")
        self.assertEqual(ctx.platform_specific["agent_session_id"], "ses_topic")
        self.assertEqual(
            calls,
            {
                "session_id": "ses_topic",
                "native": "native-topic",
                "workdir": "/repo/topic",
            },
        )

    def test_routing_subagent_does_not_bind_native_to_reserved_row(self):
        agent = _FakeBaseAgent(SimpleNamespace(bind_agent_session_by_id=lambda *a, **k: "ses_wb"))
        ctx = self._ctx("ses_wb")
        ctx.platform_specific["routing_subagent"] = "reviewer"

        ret = agent._bind_reserved_workbench_session(ctx, "native-subagent")

        self.assertIsNone(ret)
        self.assertEqual(ctx.platform_specific["agent_session_id"], "from_build")

    def test_im_turn_without_target_falls_through(self):
        agent = _FakeBaseAgent(SimpleNamespace(bind_agent_session_by_id=lambda *a, **k: None))
        ctx = self._ctx(None)
        self.assertIsNone(agent._bind_reserved_workbench_session(ctx, "native"))
        # untouched → caller runs its normal binder
        self.assertEqual(ctx.platform_specific["agent_session_id"], "from_build")

    def test_pins_reserved_id_even_without_bind_by_id_support(self):
        agent = _FakeBaseAgent(SimpleNamespace())  # no bind_agent_session_by_id
        ctx = self._ctx("ses_wb")
        ret = agent._bind_reserved_workbench_session(ctx, "native")
        self.assertEqual(ret, "ses_wb")
        self.assertEqual(ctx.platform_specific["agent_session_id"], "ses_wb")

    def test_ensure_pins_reserved_id_without_minting_hidden_row(self):
        # #402: the PRE-bind ensure must also reuse the reserved id, or a setup
        # failure before the native bind would persist the notify under a hidden row.
        ensure_calls = []

        def ensure(*a, **k):
            ensure_calls.append((a, k))
            return "hidden_new_row"

        agent = _FakeBaseAgent(SimpleNamespace(ensure_agent_session_id=ensure))
        ctx = self._ctx("ses_wb")
        request = SimpleNamespace(context=ctx, base_session_id="anchor", vibe_agent_id=None, vibe_agent_name=None)
        ret = agent.ensure_agent_session_id(request)
        self.assertEqual(ret, "ses_wb")
        self.assertEqual(ctx.platform_specific["agent_session_id"], "ses_wb")
        self.assertEqual(ensure_calls, [], "must not mint a hidden row when a workbench id is reserved")


if __name__ == "__main__":
    unittest.main()
