from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.handlers.session_handler import ClaudeInputNotSentError
from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    backend_dispatch_attempted,
    prewrite_failure_evidence,
    set_dispatch_phase,
)
from modules.claude_sdk_compat import TextBlock, ToolUseBlock
from modules.agents.claude_agent import ClaudeAgent

from tests.test_claude_agent_initiated_turn import _build_agent, _dispatcher_owned_emit


class TaskStartedMessage:
    subtype = "task_started"

    def __init__(self, task_id: str, *, tool_use_id: str | None = None):
        self.task_id = task_id
        self.description = f"Run {task_id}"
        self.task_type = "local_agent"
        self.tool_use_id = tool_use_id
        self.data = {}


class TaskNotificationMessage:
    subtype = "task_notification"

    def __init__(self, task_id: str, summary: str):
        self.task_id = task_id
        self.status = "completed"
        self.summary = summary
        self.output_file = f"/tmp/{task_id}.output"
        self.data = {}


_MISSING = object()


class ResultMessage:
    subtype = "success"
    duration_ms = 1
    session_id = "claude-native-session"

    def __init__(self, text: str, *, origin=_MISSING):
        self.result = text
        if origin is not _MISSING:
            self.origin = origin


class AssistantMessage:
    error = ""
    usage = None

    def __init__(self, *content):
        self.content = list(content)


def _block(block_type, **values):
    block = object.__new__(block_type)
    for name, value in values.items():
        setattr(block, name, value)
    return block


def _failure_assistant(text: str = "transport failed"):
    message = AssistantMessage(_block(TextBlock, text=text))
    message.is_error = True
    message.error = text
    return message


def _context(composite_key: str, *, turn_token: str = "human-turn"):
    return SimpleNamespace(
        user_id="U1",
        channel_id="C1",
        platform="avibe",
        platform_specific={
            "agent_runtime_turn_key": composite_key,
            "agent_runtime_turn_token": "runtime-token",
            "turn_token": turn_token,
            "agent_session_id": "sess-provenance",
        },
    )


def _pending_request(composite_key: str):
    payload = dict(_context(composite_key).platform_specific)
    return SimpleNamespace(
        context=SimpleNamespace(platform_specific=payload),
        output=None,
        output_activities=[],
    )


def _client(messages):
    class _Client:
        def receive_messages(self):
            async def _iterate():
                for message in messages:
                    yield message

            return _iterate()

    return _Client()


def _task_pair(task_id: str):
    return [
        TaskStartedMessage(task_id),
        TaskNotificationMessage(task_id, f"{task_id} finished"),
    ]


class ClaudeResultProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def _receive(self, messages, *, key="session-provenance:/tmp/work"):
        agent, service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.emit_result_message = AsyncMock(return_value="message-id")
        await agent._receive_messages(
            _client(messages),
            "sess-provenance",
            "/tmp/work",
            context,
            composite_key=key,
        )
        return agent, service, request

    async def test_notification_before_human_result_does_not_claim_human_turn(self):
        messages = [
            *_task_pair("task-before-human"),
            ResultMessage("human reply", origin={"kind": "human"}),
            ResultMessage("background reply", origin={"kind": "task-notification"}),
        ]

        agent, _service, request = await self._receive(messages)

        self.assertEqual(agent.emit_result_message.await_count, 2)
        human_call, activity_call = agent.emit_result_message.await_args_list
        self.assertIs(human_call.kwargs["request"], request)
        self.assertTrue(activity_call.kwargs["output"].detached)
        self.assertEqual(
            activity_call.kwargs["output"].activity_ids,
            ("task-before-human",),
        )
        self.assertFalse(agent._has_pending_requests("session-provenance:/tmp/work"))

    async def test_task_result_before_human_result_is_detached_then_human_settles(self):
        messages = [
            *_task_pair("task-before-human"),
            ResultMessage("background reply", origin={"kind": "task-notification"}),
            ResultMessage("human reply", origin={"kind": "human"}),
        ]

        agent, _service, request = await self._receive(messages)

        self.assertEqual(agent.emit_result_message.await_count, 2)
        activity_call, human_call = agent.emit_result_message.await_args_list
        self.assertTrue(activity_call.kwargs["output"].detached)
        self.assertIs(human_call.kwargs["request"], request)
        self.assertFalse(agent._has_pending_requests("session-provenance:/tmp/work"))

    async def test_buffered_assistant_frames_replay_after_human_result_classification(self):
        key = "session-buffered-provenance:/tmp/work"
        agent, service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.emit_result_message = AsyncMock(return_value="message-id")

        class _Formatter:
            @staticmethod
            def format_assistant_message(parts):
                return "\n".join(parts)

            @staticmethod
            def format_toolcall(name, input_data, **_kwargs):
                return f"{name}({input_data['command']})"

            @staticmethod
            def format_toolcall_label(name, input_data, **_kwargs):
                return f"{name}: {input_data['command']}"

        agent._get_formatter = lambda _context: _Formatter()
        messages = [
            TaskStartedMessage("task-buffer"),
            AssistantMessage(
                _block(TextBlock, text="progress from the foreground phase"),
            ),
            AssistantMessage(
                _block(
                    ToolUseBlock,
                    id="tool-buffer",
                    name="Bash",
                    input={"command": "pwd"},
                ),
                _block(TextBlock, text="final foreground phase"),
            ),
            ResultMessage("human reply", origin={"kind": "human"}),
        ]

        await agent._receive_messages(
            _client(messages),
            "sess-buffered-provenance",
            "/tmp/work",
            context,
            composite_key=key,
        )

        assistant_calls = [
            call
            for call in agent.controller.emit_agent_message.await_args_list
            if len(call.args) > 1 and call.args[1] in {"assistant", "toolcall"}
        ]
        self.assertEqual(
            [(call.args[1], call.args[2]) for call in assistant_calls],
            [
                ("assistant", "progress from the foreground phase"),
                ("toolcall", "Bash(pwd)"),
            ],
        )
        self.assertEqual(agent.emit_result_message.await_count, 1)
        self.assertIs(agent.emit_result_message.await_args.kwargs["request"], request)
        self.assertFalse(service.activities.has_completed_output("claude", key))

    async def test_buffered_foreground_tool_is_owned_before_task_started(self):
        key = "session-buffered-foreground-tool:/tmp/work"
        agent, service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.emit_result_message = AsyncMock(return_value="message-id")
        service.activities.start(
            backend="claude",
            runtime_key=key,
            session_id="sess-buffered-foreground-tool",
            activity_id="background-task",
            kind="local_agent",
            turn_id="background-turn",
        )

        await agent._receive_messages(
            _client(
                [
                    AssistantMessage(
                        _block(
                            ToolUseBlock,
                            id="foreground-tool",
                            name="Bash",
                            input={"command": "pwd"},
                        )
                    ),
                    TaskStartedMessage(
                        "foreground-task",
                        tool_use_id="foreground-tool",
                    ),
                    TaskNotificationMessage(
                        "foreground-task",
                        "foreground finished",
                    ),
                    ResultMessage("human reply", origin={"kind": "human"}),
                ]
            ),
            "sess-buffered-foreground-tool",
            "/tmp/work",
            context,
            composite_key=key,
        )

        self.assertFalse(service.activities.has_completed_output("claude", key))
        self.assertEqual(agent.emit_result_message.await_count, 1)
        self.assertIs(agent.emit_result_message.await_args.kwargs["request"], request)

    async def test_activity_flush_defers_until_buffered_phase_has_terminal_owner(self):
        key = "session-provenance-flush-race:/tmp/work"
        agent, service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.emit_result_message = _dispatcher_owned_emit(service)
        service.activities.start(
            backend="claude",
            runtime_key=key,
            session_id="sess-provenance-flush-race",
            activity_id="task-flush-race",
            kind="local_agent",
            turn_id="task-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=key,
            activity_id="task-flush-race",
            status="completed",
            metadata={"summary": "background finished"},
            expects_output=True,
        )
        agent._buffered_assistant_messages[key] = [
            (
                AssistantMessage(
                    _block(TextBlock, text="ambiguous assistant phase"),
                ),
                agent._steering_generation(key),
            )
        ]

        should_retry = await agent._flush_completed_activity_outputs(key, context)

        self.assertTrue(should_retry)
        self.assertIs(agent._pending_requests[key][0], request)
        self.assertTrue(service.activities.has_completed_output("claude", key))
        agent.emit_result_message.assert_not_awaited()

        await agent._receive_messages(
            _client(
                [
                    ResultMessage("human reply", origin={"kind": "human"}),
                ]
            ),
            "sess-provenance-flush-race",
            "/tmp/work",
            context,
            composite_key=key,
        )

        self.assertFalse(agent._has_pending_requests(key))
        self.assertGreaterEqual(agent.emit_result_message.await_count, 1)
        self.assertIs(
            next(
                call.kwargs["request"]
                for call in agent.emit_result_message.await_args_list
                if call.kwargs.get("request") is request
            ),
            request,
        )
        self.assertFalse(service.activities.has_completed_output("claude", key))

    async def test_activity_flush_defers_without_assistant_frame_until_terminal_owner(self):
        key = "session-provenance-flush-without-assistant:/tmp/work"
        agent, service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.emit_result_message = _dispatcher_owned_emit(service)
        service.activities.start(
            backend="claude",
            runtime_key=key,
            session_id="sess-provenance-flush-without-assistant",
            activity_id="task-flush-without-assistant",
            kind="local_agent",
            turn_id="task-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=key,
            activity_id="task-flush-without-assistant",
            status="completed",
            metadata={"summary": "background finished"},
            expects_output=True,
        )

        should_retry = await agent._flush_completed_activity_outputs(key, context)

        self.assertTrue(should_retry)
        self.assertIn(key, agent._activity_provenance_barriers)
        self.assertIs(agent._pending_requests[key][0], request)
        self.assertTrue(service.activities.has_completed_output("claude", key))
        agent.emit_result_message.assert_not_awaited()

    async def test_buffered_assistant_replay_failure_still_settles_terminal_result(self):
        key = "session-buffered-replay-failure:/tmp/work"
        agent, service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.emit_result_message = AsyncMock(return_value="message-id")

        class _Formatter:
            format_assistant_message = Mock(side_effect=RuntimeError("replay failed"))

        agent._get_formatter = lambda _context: _Formatter()

        await agent._receive_messages(
            _client(
                [
                    TaskStartedMessage("task-replay-failure"),
                    AssistantMessage(
                        _block(TextBlock, text="buffered before terminal"),
                    ),
                    ResultMessage("human reply", origin={"kind": "human"}),
                ]
            ),
            "sess-buffered-replay-failure",
            "/tmp/work",
            context,
            composite_key=key,
        )

        self.assertFalse(agent._has_pending_requests(key))
        agent.emit_result_message.assert_awaited_once()
        self.assertIs(agent.emit_result_message.await_args.kwargs["request"], request)
        self.assertFalse(service.activities.has_completed_output("claude", key))

    async def test_multiple_task_completions_are_one_detached_delivery_batch(self):
        messages = [
            TaskStartedMessage("task-a"),
            TaskStartedMessage("task-b"),
            TaskNotificationMessage("task-a", "A finished"),
            TaskNotificationMessage("task-b", "B finished"),
            ResultMessage("combined background reply", origin={"kind": "task-notification"}),
        ]

        agent, _service, request = await self._receive(messages)

        self.assertEqual(agent.emit_result_message.await_count, 1)
        call = agent.emit_result_message.await_args
        self.assertIsNone(call.kwargs["request"])
        self.assertEqual(
            call.kwargs["output"].activity_ids,
            ("task-a", "task-b"),
        )
        self.assertTrue(call.kwargs["output"].detached)
        self.assertTrue(agent._has_pending_requests("session-provenance:/tmp/work"))
        self.assertIs(agent._pending_requests["session-provenance:/tmp/work"][0], request)

    async def test_assistant_after_detached_activity_flag_stays_detached(self):
        key = "session-live-detached-flag:/tmp/work"
        agent, service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        service.activities.start(
            backend="claude",
            runtime_key=key,
            session_id="sess-live-detached-flag",
            activity_id="task-live-detached",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=key,
            activity_id="task-live-detached",
            status="completed",
            metadata={"summary": "background task finished"},
            expects_output=True,
        )
        activity = service.activities.claim_completed_output("claude", key)
        self.assertIsNotNone(activity)
        agent._detached_activity_outputs[key] = [activity]
        agent.emit_result_message = AsyncMock(return_value="message-id")

        assistant = AssistantMessage(
            _block(TextBlock, text="detached progress"),
        )
        await agent._receive_messages(
            _client(
                [
                    assistant,
                    ResultMessage(
                        "detached final",
                        origin={"kind": "task-notification"},
                    ),
                ]
            ),
            "sess-live-detached-flag",
            "/tmp/work",
            context,
            composite_key=key,
        )

        self.assertEqual(agent.emit_result_message.await_count, 1)
        self.assertEqual(
            agent.emit_result_message.await_args.args[1],
            "detached progress",
        )
        self.assertTrue(agent.emit_result_message.await_args.kwargs["output"].detached)
        self.assertNotIn("detached progress", agent._last_assistant_text.values())
        self.assertIs(agent._pending_requests[key][0], request)

    async def test_absent_and_unknown_origin_never_claim_a_human_turn_with_active_task(self):
        messages = [
            TaskStartedMessage("task-ambiguous"),
            ResultMessage("absent origin"),
            ResultMessage("unknown origin", origin={"kind": "mystery"}),
            ResultMessage("human reply", origin={"kind": "human"}),
        ]

        agent, _service, request = await self._receive(messages)

        self.assertEqual(agent.emit_result_message.await_count, 3)
        first, second, human = agent.emit_result_message.await_args_list
        self.assertIsNone(first.kwargs.get("request"))
        self.assertIsNone(second.kwargs.get("request"))
        self.assertTrue(first.kwargs["output"].detached)
        self.assertTrue(second.kwargs["output"].detached)
        self.assertIs(human.kwargs["request"], request)
        self.assertFalse(agent._has_pending_requests("session-provenance:/tmp/work"))

    async def test_write_fence_rejects_replaced_or_stopping_client_before_native_write(self):
        key = "session-write-fence:/tmp/work"
        agent, _service = _build_agent()
        context = SimpleNamespace(platform_specific={})
        set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
        old_client = SimpleNamespace(query=AsyncMock())
        replacement = SimpleNamespace()
        agent.claude_sessions[key] = replacement

        with self.assertRaises(ClaudeInputNotSentError):
            await agent._write_human_query(old_client, key, "hello", context)

        old_client.query.assert_not_awaited()
        self.assertIs(backend_dispatch_attempted(context), False)

        agent.claude_sessions[key] = old_client
        agent._steering_closing_keys().add(key)
        with self.assertRaises(ClaudeInputNotSentError):
            await agent._write_human_query(old_client, key, "stop race", context)
        old_client.query.assert_not_awaited()
        self.assertIs(backend_dispatch_attempted(context), False)

    async def test_native_transport_failure_is_attempted_not_definitely_unsent(self):
        key = "session-write-attempt:/tmp/work"
        agent, _service = _build_agent()
        context = SimpleNamespace(platform_specific={})
        set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
        client = SimpleNamespace(
            query=AsyncMock(side_effect=RuntimeError("transport failed"))
        )
        agent.claude_sessions[key] = client

        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            await agent._write_human_query(client, key, "hello", context)

        self.assertIs(backend_dispatch_attempted(context), True)
        self.assertEqual(prewrite_failure_evidence(context), {})

    async def test_buffered_failure_after_steer_is_suppressed_and_does_not_pop_new_turn(self):
        key = "session-buffered-failure-steer:/tmp/work"
        agent, _service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent._get_formatter = lambda _context: None
        agent.emit_result_message = AsyncMock(return_value="message-id")

        failure_seen = asyncio.Event()
        release_result = asyncio.Event()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield TaskStartedMessage("task-failure-steer")
                    yield _failure_assistant()
                    failure_seen.set()
                    await release_result.wait()
                    yield ResultMessage("steered result", origin={"kind": "human"})

                return _iterate()

        steer_receipt = SimpleNamespace(
            kind="steer",
            state="accepted",
            text="continue",
        )
        agent._native_input_receipt_map()[key] = [steer_receipt]

        receiver = asyncio.create_task(
            agent._receive_messages(
                _Client(),
                "sess-buffered-failure-steer",
                "/tmp/work",
                context,
                composite_key=key,
            )
        )
        await asyncio.wait_for(failure_seen.wait(), timeout=1)
        agent._advance_steering_generation(key)
        release_result.set()
        await receiver

        self.assertFalse(agent.emit_result_message.await_args_list)
        self.assertIs(agent._pending_requests[key][0], request)
        # The paired human Result consumes the suppression marker without
        # settling/popping the still-current request.
        self.assertNotIn(key, agent._suppressed_synthetic_results)
        self.assertNotIn(key, agent._suppressed_synthetic_error_text)
        self.assertNotIn(key, agent._native_input_receipts)

    async def test_buffered_failure_after_stop_is_ignored_without_failure_settlement(self):
        key = "session-buffered-failure-stop:/tmp/work"
        agent, _service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.emit_result_message = AsyncMock(return_value="message-id")
        agent._steering_closing_keys().add(key)

        await agent._receive_messages(
            _client(
                [
                    TaskStartedMessage("task-failure-stop"),
                    _failure_assistant(),
                    ResultMessage("stopped", origin={"kind": "human"}),
                ]
            ),
            "sess-buffered-failure-stop",
            "/tmp/work",
            context,
            composite_key=key,
        )

        agent.emit_result_message.assert_not_awaited()
        self.assertIs(agent._pending_requests[key][0], request)

    async def test_eof_replays_buffered_failure_before_human_fallback_and_detached_flush(self):
        key = "session-buffered-failure-eof:/tmp/work"
        agent, service = _build_agent()
        agent._handle_receiver_eof = ClaudeAgent._handle_receiver_eof.__get__(agent)
        agent._handle_assistant_terminal_failure = (
            ClaudeAgent._handle_assistant_terminal_failure.__get__(agent)
        )
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.controller.emit_agent_message = _dispatcher_owned_emit(service)
        agent.record_model_hub_native_failure = AsyncMock()
        agent.controller.agent_auth_service = SimpleNamespace(
            maybe_emit_auth_recovery_message=AsyncMock(return_value=False),
        )
        agent.controller.claude_sessions[key] = SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(returncode=None)),
        )
        service.activities.start(
            backend="claude",
            runtime_key=key,
            session_id="sess-buffered-failure-eof",
            activity_id="task-buffered-failure-eof",
            kind="local_agent",
            turn_id="task-turn",
        )
        await agent._receive_messages(
            _client(
                [
                    TaskStartedMessage("task-buffered-failure-eof"),
                    TaskNotificationMessage(
                        "task-buffered-failure-eof",
                        "background finished",
                    ),
                    _failure_assistant("backend exploded"),
                ]
            ),
            "sess-buffered-failure-eof",
            "/tmp/work",
            context,
            composite_key=key,
        )

        event_types = [
            call.args[1]
            for call in agent.controller.emit_agent_message.await_args_list
        ]
        self.assertEqual(event_types, ["notify", "result", "result"])
        self.assertIn(
            "backend exploded",
            agent.controller.emit_agent_message.await_args_list[1].kwargs[
                "terminal_error"
            ],
        )
        self.assertTrue(
            agent.controller.emit_agent_message.await_args_list[2]
            .kwargs["output"]
            .detached
        )
        self.assertFalse(service.activities.has_completed_output("claude", key))

    async def test_receiver_error_replays_buffered_failure_before_detached_flush(self):
        key = "session-buffered-failure-error:/tmp/work"
        agent, service = _build_agent()
        agent._handle_receiver_eof = ClaudeAgent._handle_receiver_eof.__get__(agent)
        agent._handle_receiver_exception = ClaudeAgent._handle_receiver_exception.__get__(
            agent
        )
        agent._handle_assistant_terminal_failure = (
            ClaudeAgent._handle_assistant_terminal_failure.__get__(agent)
        )
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.controller.emit_agent_message = _dispatcher_owned_emit(service)
        agent.record_model_hub_native_failure = AsyncMock()
        agent.controller.agent_auth_service = SimpleNamespace(
            maybe_emit_auth_recovery_message=AsyncMock(return_value=False),
        )
        agent.session_handler.handle_session_error = AsyncMock(return_value=False)
        cleanup_lock_states = []

        async def cleanup_session(*_args, **_kwargs):
            cleanup_lock_states.append(agent._steering_lock(key).locked())

        agent.session_handler.cleanup_session = AsyncMock(
            side_effect=cleanup_session,
        )
        service.activities.start(
            backend="claude",
            runtime_key=key,
            session_id="sess-buffered-failure-error",
            activity_id="task-buffered-failure-error",
            kind="local_agent",
            turn_id="task-turn",
        )

        class _FailingClient:
            def receive_messages(self):
                async def _iterate():
                    yield TaskStartedMessage("task-buffered-failure-error")
                    yield TaskNotificationMessage(
                        "task-buffered-failure-error",
                        "background finished",
                    )
                    yield _failure_assistant("backend exploded")
                    raise RuntimeError("receiver disconnected")

                return _iterate()

        await agent._receive_messages(
            _FailingClient(),
            "sess-buffered-failure-error",
            "/tmp/work",
            context,
            composite_key=key,
        )

        event_types = [
            call.args[1]
            for call in agent.controller.emit_agent_message.await_args_list
        ]
        self.assertEqual(event_types, ["notify", "result", "result"])
        self.assertIn(
            "backend exploded",
            agent.controller.emit_agent_message.await_args_list[1].kwargs[
                "terminal_error"
            ],
        )
        self.assertTrue(
            agent.controller.emit_agent_message.await_args_list[2]
            .kwargs["output"]
            .detached
        )
        self.assertFalse(service.activities.has_completed_output("claude", key))
        agent.session_handler.handle_session_error.assert_not_awaited()
        agent.session_handler.cleanup_session.assert_awaited_once()
        self.assertEqual(cleanup_lock_states, [False])

    async def test_receiver_error_contains_buffered_failure_replay_exception(self):
        key = "session-buffered-failure-replay-error:/tmp/work"
        agent, _service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        agent.controller.agent_auth_service = SimpleNamespace(
            maybe_emit_auth_recovery_message=AsyncMock(return_value=False),
        )
        agent.session_handler.handle_session_error = AsyncMock(return_value=True)
        agent.record_model_hub_native_failure = AsyncMock()
        agent._emit_no_result_settlement = AsyncMock()
        agent._release_service_runtime_turn = Mock()
        agent._process_assistant_terminal_frame = AsyncMock(
            side_effect=RuntimeError("replay emit failed"),
        )

        class _FailingClient:
            def receive_messages(self):
                async def _iterate():
                    yield TaskStartedMessage("task-replay-error")
                    yield _failure_assistant("backend exploded")
                    raise RuntimeError("receiver disconnected")

                return _iterate()

        await agent._receive_messages(
            _FailingClient(),
            "sess-buffered-failure-replay-error",
            "/tmp/work",
            context,
            composite_key=key,
        )

        agent.session_handler.handle_session_error.assert_awaited_once()
        agent._emit_no_result_settlement.assert_awaited_once()
        agent._release_service_runtime_turn.assert_called_once_with(context)

    async def test_eof_closes_write_admission_before_buffered_failure_replay(self):
        key = "session-eof-write-fence:/tmp/work"
        agent, _service = _build_agent()
        context = _context(key)
        request = _pending_request(key)
        agent._pending_requests[key] = [request]
        replay_started = asyncio.Event()
        release_replay = asyncio.Event()
        eof_entered = asyncio.Event()
        release_eof = asyncio.Event()
        client = SimpleNamespace(query=AsyncMock())
        agent.claude_sessions[key] = client

        async def hold_eof(*_args, **_kwargs):
            eof_entered.set()
            await release_eof.wait()

        agent._handle_receiver_eof = AsyncMock(side_effect=hold_eof)

        async def replay_failure(*_args, **_kwargs):
            replay_started.set()
            await release_replay.wait()
            return "failure"

        agent._process_assistant_terminal_frame = replay_failure

        receiver = asyncio.create_task(
            agent._receive_messages(
                _client(
                    [
                        TaskStartedMessage("task-eof-write-fence"),
                        _failure_assistant("backend exploded"),
                    ]
                ),
                "sess-eof-write-fence",
                "/tmp/work",
                context,
                composite_key=key,
            )
        )
        await asyncio.wait_for(replay_started.wait(), timeout=1)
        self.assertIn(key, agent._steering_closing_keys())

        async def fenced_write():
            async with agent._steering_lock(key):
                await agent._write_human_query(
                    client,
                    key,
                    "new input",
                    context,
                )

        write = asyncio.create_task(fenced_write())
        release_replay.set()
        await asyncio.wait_for(eof_entered.wait(), timeout=1)
        with self.assertRaises(ClaudeInputNotSentError):
            await asyncio.wait_for(write, timeout=1)

        release_eof.set()
        await receiver

    async def test_primary_write_and_stop_share_the_native_write_fence(self):
        key = "session-primary-stop-fence:/tmp/work"
        agent, _service = _build_agent()
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        interrupt_called = asyncio.Event()

        class _Client:
            _vibe_runtime_base_session_id = "sess-primary-stop-fence"
            _vibe_runtime_session_key = key

            async def query(self, _messages, *, session_id):
                self.assert_session_id = session_id
                query_started.set()
                await release_query.wait()

            async def interrupt(self):
                interrupt_called.set()

            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    await asyncio.Future()
                    yield None

                return _iterate()

        client = _Client()
        agent.claude_sessions[key] = client
        receiver_task = asyncio.create_task(asyncio.Event().wait())
        agent.receiver_tasks[key] = receiver_task
        agent.session_handler.get_or_create_claude_session = AsyncMock(
            return_value=client,
        )
        agent.session_handler.mark_session_active = Mock()
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        agent.mark_runtime_turn_started = Mock()
        context = _context(key)
        request = SimpleNamespace(
            context=context,
            message="primary input",
            working_path="/tmp/work",
            base_session_id="sess-primary-stop-fence",
            composite_session_id=key,
            session_key="session-key",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
            vibe_agent_system_prompt=None,
            input_metadata=None,
            ack_message_id=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            files=None,
        )
        stop_request = SimpleNamespace(
            context=_context(key, turn_token="stop-turn"),
            composite_session_id=key,
            stop_failure_reason=None,
        )

        primary_task = asyncio.create_task(agent.handle_message(request))
        await asyncio.wait_for(query_started.wait(), timeout=1)
        stop_task = asyncio.create_task(agent.handle_stop(stop_request))
        await asyncio.sleep(0)
        self.assertFalse(interrupt_called.is_set())

        release_query.set()
        await primary_task
        self.assertTrue(await asyncio.wait_for(stop_task, timeout=1))
        self.assertTrue(interrupt_called.is_set())
        receiver_task.cancel()
        await asyncio.gather(receiver_task, return_exceptions=True)

    async def test_generation_retirement_waits_for_write_without_lock_cycle(self):
        key = "session-generation-eviction-watchdog:/tmp/work"
        agent, _service = _build_agent()
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        generation_lock = asyncio.Lock()

        class _Client:
            async def query(self, _messages, *, session_id):
                query_started.set()
                await release_query.wait()

            async def disconnect(self):
                return None

        client = _Client()
        agent.claude_sessions[key] = client
        agent._pending_requests[key] = [_pending_request(key)]
        agent.session_handler._claude_runtime_generation_lock = (
            lambda _key: generation_lock
        )
        cleanup_session = AsyncMock()
        agent.session_handler._cleanup_session_locked = cleanup_session

        async def _primary_write():
            async with agent._steering_lock(key):
                await agent._write_human_query(
                    client,
                    key,
                    "primary input",
                    _context(key),
                )

        await generation_lock.acquire()
        writer = asyncio.create_task(_primary_write())
        await asyncio.wait_for(query_started.wait(), timeout=1)
        eviction = asyncio.create_task(
            agent.force_cleanup_stuck_active_session(
                key,
                runtime_lock_held=True,
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(eviction.done())
        cleanup_session.assert_not_awaited()

        release_query.set()
        await writer
        await asyncio.wait_for(eviction, timeout=1)
        cleanup_session.assert_awaited_once()
        self.assertTrue(generation_lock.locked())
        generation_lock.release()

    async def test_handle_message_persists_definitely_unsent_input_recovery_evidence(self):
        key = "session-unsent-recovery:/tmp/work"
        agent, _service = _build_agent()
        context = _context(key)
        set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
        old_client = SimpleNamespace(query=AsyncMock())
        agent.claude_sessions[key] = SimpleNamespace()
        agent.session_handler.get_or_create_claude_session = AsyncMock(
            return_value=old_client
        )
        agent.session_handler.handle_session_error = AsyncMock(return_value=True)
        agent._prepare_message_with_files = lambda request: request.message
        agent._delete_ack = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        request = SimpleNamespace(
            context=context,
            message="retry me",
            working_path="/tmp/work",
            base_session_id="sess-unsent-recovery",
            composite_session_id=key,
            session_key="session-key",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model="claude-fixture",
            input_metadata=None,
            ack_message_id=None,
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
            files=None,
        )

        await agent.handle_message(request)

        old_client.query.assert_not_awaited()
        self.assertIs(backend_dispatch_attempted(context), False)
        self.assertEqual(
            prewrite_failure_evidence(context),
            {
                "reason": "claude_runtime_changed_before_write",
                "requires_explicit_retry": True,
            },
        )
        self.assertFalse(agent._has_pending_requests(key))
        agent.controller.emit_agent_message.assert_awaited()


if __name__ == "__main__":
    unittest.main()
