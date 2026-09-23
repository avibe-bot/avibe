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
