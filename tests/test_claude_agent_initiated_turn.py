"""Regression: backend output produced WITHOUT an Avibe-initiated turn (a Claude
Code background-task completion or ScheduleWakeup re-invoking the agent loop
inside the same SDK process) must still be delivered to the user.

Before the fix, that output streamed onto ``ClaudeAgent._receive_messages`` after
the previous turn had already released the runtime gate, so the receiver's reused
context carried a stale runtime-turn token and ``_pending_requests`` was empty.
The outbound active-turn guard (``AgentService.emit_matches_runtime_turn``) then
dropped every assistant/tool/result emit as a superseded straggler — the reply
was never persisted, delivered, or pushed (verified in production logs for
session ``ses6efm3vrtnw``).

The fix opens an *agent-initiated* turn when content arrives with no pending
request, so the reply rides the same persist + deliver + notify chokepoints as a
user turn.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.claude_agent import ClaudeAgent
from modules.agents.service import AgentService
from core.message_output import terminal_output_for


# Class names mirror the Claude SDK frames so ``_detect_message_type`` (which maps
# on ``__class__.__name__``) classifies them as assistant / result.
class AssistantMessage:
    """Minimal assistant frame (no text/toolcalls) — just enough to be detected
    as the FIRST content message of an agent-initiated run."""

    error = ""
    content: list = []
    usage = None


class ResultMessage:
    subtype = "success"
    result = "✅ master 回归环境已就绪"
    duration_ms = 1
    duration_api_ms = 1
    session_id = "claude-native-session"

    def __init__(self, *, num_turns: int = 1):
        self.num_turns = num_turns


class TaskStartedMessage:
    subtype = "task_started"
    task_id = "task-690"
    description = "Background verification"
    task_type = "local_agent"
    tool_use_id = "tool-690"
    data = {}


class TaskNotificationMessage:
    subtype = "task_notification"
    task_id = "task-690"
    status = "completed"
    output_file = "/tmp/task-690.output"
    summary = "Background verification finished"
    data = {}


def _assistant_then_result_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield AssistantMessage()
                yield ResultMessage()

            return _iterate()

    return _Client()


def _one_result_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield ResultMessage()

            return _iterate()

    return _Client()


def _two_result_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield ResultMessage(num_turns=1)
                yield ResultMessage(num_turns=2)

            return _iterate()

    return _Client()


def _completed_task_then_result_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield TaskStartedMessage()
                yield TaskNotificationMessage()
                yield ResultMessage()

            return _iterate()

    return _Client()


def _task_notification_then_result_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield TaskNotificationMessage()
                yield ResultMessage()

            return _iterate()

    return _Client()


def _completed_task_notification_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield TaskStartedMessage()
                yield TaskNotificationMessage()

            return _iterate()

    return _Client()


def _completed_task_notification_then_wait_client(release: asyncio.Event):
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield TaskStartedMessage()
                yield TaskNotificationMessage()
                await release.wait()

            return _iterate()

    return _Client()


def _fallback_client(data):
    class ModelRefusalFallbackMessage:
        subtype = "model_refusal_fallback"

        def __init__(self):
            self.data = data

    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield ModelRefusalFallbackMessage()

            return _iterate()

    return _Client()


def _build_agent(*, running_calls=None, mark_idle_calls=None, active_calls=None):
    mark_idle_calls = mark_idle_calls if mark_idle_calls is not None else []
    controller = SimpleNamespace(
        config=SimpleNamespace(platform="slack"),
        im_client=SimpleNamespace(formatter=None),
        settings_manager=SimpleNamespace(sessions=None),
        session_manager=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_active={})),
        ),
        receiver_tasks={},
        claude_sessions={},
        claude_client=SimpleNamespace(_is_skip_message=lambda message: False),
        session_handler=SimpleNamespace(
            mark_session_idle=lambda key: mark_idle_calls.append(key),
            mark_session_active=lambda key: (active_calls.append(key) if active_calls is not None else None),
            touch_session_activity=lambda key: None,
        ),
        note_session_tokens=lambda *a, **k: None,
        emit_agent_message=AsyncMock(return_value=None),
    )
    # INBOUND chokepoint recorder for the agent-initiated turn open.
    controller.session_turns = SimpleNamespace(
        on_running=lambda ctx: (running_calls.append(ctx) if running_calls is not None else None)
    )
    controller._get_session_key = lambda context: "session-key"

    service = AgentService(controller)
    agent = ClaudeAgent(controller)
    service.register(agent)
    controller.agent_service = service

    # Stub the external bits the assistant/result branches touch so the test
    # isolates the agent-initiated-turn contract (mirrors the result-settle test).
    agent._maybe_capture_session_id = lambda *a, **k: None
    agent._consume_suppressed_synthetic_result = lambda *a, **k: False
    agent._handle_auth_failure_result = AsyncMock(return_value=False)
    agent._handle_assistant_terminal_failure = AsyncMock(return_value=False)
    agent._reserved_native_session_id = lambda *a, **k: None
    agent._adopt_pending_turn_token = lambda *a, **k: None
    agent._get_formatter = lambda context: None
    agent._handle_receiver_eof = AsyncMock()
    return agent, service


class BeginAgentInitiatedTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_opens_turn_on_free_gate_so_guard_passes(self):
        running_calls: list = []
        agent, service = _build_agent(running_calls=running_calls)
        context = SimpleNamespace(
            platform_specific={
                "agent_runtime_turn_key": "S:/w",
                "agent_runtime_turn_token": "OLD",
                "agent_session_id": "sess-1",
            }
        )
        # Precondition: the prior turn released the gate, so the stale token on the
        # context fails the guard — exactly the state that dropped the reply.
        self.assertFalse(service.emit_matches_runtime_turn(context))

        token = await service.begin_agent_initiated_turn("claude", context, "S:/w")

        self.assertTrue(token)
        self.assertNotEqual(token, "OLD")
        gate = service._get_turn_gate("S:/w")
        self.assertTrue(gate.lock.locked())
        self.assertEqual(gate.token, token)
        self.assertTrue(gate.runtime_started)
        self.assertEqual(context.platform_specific["agent_runtime_turn_token"], token)
        # The guard now passes — the reply will NOT be dropped.
        self.assertTrue(service.emit_matches_runtime_turn(context))
        # INBOUND chokepoint fired (session → running).
        self.assertEqual(running_calls, [context])

    async def test_noop_when_a_real_turn_holds_the_gate(self):
        agent, service = _build_agent()
        gate = service._get_turn_gate("S:/w")
        await gate.lock.acquire()
        gate.token = "R1"
        gate.backend = "claude"
        context = SimpleNamespace(platform_specific={"agent_session_id": "sess-1"})

        token = await service.begin_agent_initiated_turn("claude", context, "S:/w")

        self.assertIsNone(token)
        # Untouched: the real turn still owns the gate.
        self.assertEqual(gate.token, "R1")

    async def test_does_not_block_when_a_user_turn_is_queued_on_the_gate(self):
        # Regression (Codex P1): an asyncio.Lock can be momentarily unlocked while
        # it still has QUEUED WAITERS (a user turn that blocked on the gate while
        # the previous turn held it). locked() is False then, but awaiting acquire()
        # would suspend the long-lived receiver behind the queued user turn —
        # deadlocking the session (the receiver is what reads that turn's result to
        # release the gate). begin must be strictly non-blocking when a waiter exists.
        agent, service = _build_agent()
        runtime_key = "S:/w"
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()  # "turn A" holds the gate
        waiter = asyncio.ensure_future(gate.lock.acquire())  # "turn B" queues
        await asyncio.sleep(0)  # let B register as a waiter
        try:
            # The deadlock window: A releases → lock unlocked but B still queued.
            gate.lock.release()
            context = SimpleNamespace(platform_specific={"agent_session_id": "s"})
            token = await asyncio.wait_for(
                service.begin_agent_initiated_turn("claude", context, runtime_key),
                timeout=1.0,  # wait_for proves it returned without suspending behind B
            )
            self.assertIsNone(token)
        finally:
            await waiter  # B acquires
            gate.lock.release()

    async def test_suppressed_synthetic_result_does_not_open_a_turn(self):
        # Regression (Codex P1): a malformed-tool-use synthetic error pops the
        # real pending request and arms _suppressed_synthetic_results for the
        # PAIRED ResultMessage, which is skipped with no terminal emit. Opening an
        # agent-initiated turn for it would leak the gate/pending/active flag and
        # block the next user message. The detection must skip while suppression
        # is pending.
        active_calls: list[str] = []
        agent, service = _build_agent(active_calls=active_calls)
        composite_key = "session-3:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD",
                "agent_session_id": "sess-3",
            },
        )
        agent._suppressed_synthetic_results.add(composite_key)

        await agent._maybe_begin_agent_initiated_turn(
            context, composite_key, "session-3", "/tmp/work", "session-key"
        )

        gate = service._get_turn_gate(composite_key)
        self.assertFalse(gate.lock.locked())  # no turn opened
        self.assertEqual(gate.token, "")  # no token minted
        self.assertEqual(active_calls, [])  # session not marked active
        self.assertFalse(agent._has_pending_requests(composite_key))  # no synthetic request


class ReceiverOpensAgentInitiatedTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_query_waits_until_background_output_is_consumed(self):
        agent, service = _build_agent()
        composite_key = "session-serialized:/tmp/work"
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-serialized",
            activity_id="task-690",
            kind="local_agent",
        )

        waiter = asyncio.create_task(agent._wait_for_activity_output(composite_key))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            expects_output=True,
        )
        agent._signal_activity_output_settled(composite_key)
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        claimed = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(claimed)
        self.assertTrue(service.activities.ack_completed_output(claimed))
        await asyncio.wait_for(waiter, timeout=1)

    async def test_terminal_only_task_event_keeps_current_turn_origin(self):
        agent, service = _build_agent()
        composite_key = "session-terminal-only:/tmp/work"
        pending_context = SimpleNamespace(
            platform_specific={"turn_token": "current-turn"},
        )
        agent._pending_requests[composite_key] = [SimpleNamespace(context=pending_context)]
        context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-terminal-only"},
        )

        self.assertTrue(
            agent._handle_activity_message(
                TaskNotificationMessage(),
                composite_key,
                context,
            )
        )

        activity = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(activity)
        self.assertEqual(activity.turn_id, "current-turn")

    async def test_failed_activity_snapshot_waits_for_run_owner_ack(self):
        agent, service = _build_agent()
        composite_key = "session-failed-activity:/tmp/work"
        context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-failed-activity"},
        )
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-failed-activity",
            activity_id="task-failed",
            kind="local_agent",
            run_id="run-failed",
        )
        agent.controller.scheduled_task_service = SimpleNamespace(
            settle_activity_runs=Mock(side_effect=RuntimeError("store unavailable")),
        )
        original_complete = service.activities.complete
        service.activities.complete = Mock(wraps=original_complete)
        original_ack = service.activities.ack_recovered_terminal
        service.activities.ack_recovered_terminal = Mock(wraps=original_ack)
        failed = SimpleNamespace(
            subtype="task_notification",
            task_id="task-failed",
            status="failed",
            summary="Background verification failed",
            data={},
        )

        self.assertTrue(
            agent._handle_activity_message(
                failed,
                composite_key,
                context,
            )
        )

        call = service.activities.complete.call_args
        self.assertTrue(call.kwargs["retain_terminal_snapshot"])
        completed = agent.controller.scheduled_task_service.settle_activity_runs.call_args.args[0]
        self.assertEqual(completed.status, "failed")
        service.activities.ack_recovered_terminal.assert_not_called()

    async def test_activity_keeps_origin_delivery_target_when_a_newer_turn_arrives(self):
        agent, service = _build_agent()
        composite_key = "session-delivery-origin:/tmp/work"
        origin_context = SimpleNamespace(
            platform_specific={
                "turn_token": "origin-turn",
                "delivery_key_external": "slack::channel::C-ORIGIN",
            },
        )
        agent._pending_requests[composite_key] = [SimpleNamespace(context=origin_context)]
        receiver_context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-delivery-origin"},
        )

        self.assertTrue(
            agent._handle_activity_message(
                TaskStartedMessage(),
                composite_key,
                receiver_context,
            )
        )
        agent._pending_requests[composite_key] = [
            SimpleNamespace(
                context=SimpleNamespace(
                    platform_specific={
                        "turn_token": "newer-turn",
                        "delivery_key_external": "slack::channel::C-NEWER",
                    },
                )
            )
        ]

        self.assertTrue(
            agent._handle_activity_message(
                TaskNotificationMessage(),
                composite_key,
                receiver_context,
            )
        )

        activity = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(activity)
        self.assertEqual(
            activity.metadata["delivery_key_external"],
            "slack::channel::C-ORIGIN",
        )

    async def test_completed_task_notification_at_eof_delivers_summary(self):
        agent, service = _build_agent()
        composite_key = "session-eof:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "old-turn",
                "turn_token": "origin-turn",
                "agent_session_id": "sess-eof",
            },
        )
        agent.emit_result_message = AsyncMock(return_value="message-id")

        await agent._receive_messages(
            _completed_task_notification_client(),
            "session-eof",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(
            agent.emit_result_message.await_args.args[1],
            "Background verification finished",
        )
        output = agent.emit_result_message.await_args.kwargs["output"]
        self.assertTrue(output.detached)
        self.assertFalse(output.completes_turn)
        self.assertTrue(output.completes_run)
        self.assertIsNone(service.activities.claim_completed_output("claude", composite_key))

    async def test_completed_task_notification_flushes_while_receiver_stays_open(self):
        agent, service = _build_agent()
        agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 0
        composite_key = "session-live-flush:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "old-turn",
                "turn_token": "origin-turn",
                "agent_session_id": "sess-live-flush",
            },
        )
        emitted = asyncio.Event()

        async def _emit(*_args, **_kwargs):
            emitted.set()
            return "message-id"

        agent.emit_result_message = AsyncMock(side_effect=_emit)
        release = asyncio.Event()
        receiver = asyncio.create_task(
            agent._receive_messages(
                _completed_task_notification_then_wait_client(release),
                "session-live-flush",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        try:
            await asyncio.wait_for(emitted.wait(), timeout=1)
            self.assertFalse(receiver.done())
            output = agent.emit_result_message.await_args.kwargs["output"]
            self.assertTrue(output.detached)
            self.assertFalse(output.completes_turn)
            self.assertTrue(output.completes_run)
            self.assertIsNone(
                service.activities.claim_completed_output("claude", composite_key)
            )
        finally:
            release.set()
            await receiver

    async def test_summaryless_completed_activity_settles_silently(self):
        agent, service = _build_agent()
        composite_key = "session-silent-activity:/tmp/work"
        context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-silent-activity"}
        )
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-silent-activity",
            activity_id="task-silent",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-silent",
            status="completed",
            expects_output=True,
        )
        agent.emit_result_message = AsyncMock()
        agent.controller.emit_agent_message = AsyncMock(return_value=None)

        should_retry = await agent._flush_completed_activity_outputs(
            composite_key,
            context,
        )

        self.assertFalse(should_retry)
        agent.emit_result_message.assert_not_awaited()
        agent.controller.emit_agent_message.assert_awaited_once()
        silent_call = agent.controller.emit_agent_message.await_args
        self.assertEqual(silent_call.args[:3], (context, "result", ""))
        output = silent_call.kwargs["output"]
        self.assertTrue(output.detached)
        self.assertFalse(output.completes_turn)
        self.assertTrue(output.completes_run)
        self.assertFalse(service.activities.has_completed_output("claude", composite_key))

    async def test_silent_only_completed_activity_settles_without_retry(self):
        agent, service = _build_agent()
        composite_key = "session-silent-directive:/tmp/work"
        context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-silent-directive"}
        )
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-silent-directive",
            activity_id="task-silent",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-silent",
            status="completed",
            metadata={"summary": "<silent>no visible follow-up</silent>"},
            expects_output=True,
        )
        agent.emit_result_message = AsyncMock()
        agent.controller.emit_agent_message = AsyncMock(return_value=None)

        should_retry = await agent._flush_completed_activity_outputs(
            composite_key,
            context,
        )

        self.assertFalse(should_retry)
        agent.emit_result_message.assert_not_awaited()
        silent_call = agent.controller.emit_agent_message.await_args
        self.assertEqual(silent_call.args[:3], (context, "result", ""))
        self.assertFalse(service.activities.has_completed_output("claude", composite_key))

    async def test_timed_flush_keeps_activity_for_a_newer_pending_turn(self):
        agent, service = _build_agent()
        composite_key = "session-deferred-flush:/tmp/work"
        pending_context = SimpleNamespace(platform_specific={"turn_token": "newer-turn"})
        agent._pending_requests[composite_key] = [SimpleNamespace(context=pending_context)]
        context = SimpleNamespace(platform_specific={"agent_session_id": "sess-deferred"})
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-deferred",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            metadata={"summary": "Background verification finished"},
            expects_output=True,
        )
        agent.emit_result_message = AsyncMock()

        should_retry = await agent._flush_completed_activity_outputs(composite_key, context)

        self.assertTrue(should_retry)
        agent.emit_result_message.assert_not_awaited()
        claimed = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.turn_id, "origin-turn")

    async def test_detached_activity_output_requeues_when_delivery_returns_none(self):
        agent, service = _build_agent()
        composite_key = "session-delivery-failed:/tmp/work"
        context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-delivery-failed"}
        )
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-delivery-failed",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            metadata={"summary": "Background verification finished"},
            expects_output=True,
        )
        activity = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(activity)
        agent._detached_activity_outputs[composite_key] = activity
        agent._detached_assistant_text[composite_key] = "Full background result"
        agent.emit_result_message = AsyncMock(return_value=None)

        with self.assertRaisesRegex(RuntimeError, "was not persisted or delivered"):
            await agent._flush_detached_activity_output(composite_key, context)

        claimed = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "task-690")

    async def test_requeued_request_activity_restores_terminal_turn_policy(self):
        agent, service = _build_agent()
        composite_key = "session-requeued-policy:/tmp/work"
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-requeued-policy",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            expects_output=True,
        )
        activity = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(activity)
        request = SimpleNamespace(
            output_activity=activity,
            output=agent._activity_message_output(
                activity,
                detached=False,
                completes_turn=True,
            ),
        )

        agent._requeue_request_activity(request)

        self.assertIsNone(request.output_activity)
        restored = terminal_output_for(request)
        self.assertTrue(restored.completes_turn)
        self.assertTrue(restored.settles_run)
        self.assertIsNone(restored.activity_id)
        self.assertIsNotNone(
            service.activities.claim_completed_output("claude", composite_key)
        )

    async def test_requeued_terminal_only_activity_flushes_after_pending_turn(self):
        agent, service = _build_agent()
        agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 0
        composite_key = "session-retry-flush:/tmp/work"
        agent._pending_requests[composite_key] = [
            SimpleNamespace(
                context=SimpleNamespace(platform_specific={"turn_token": "newer-turn"})
            )
        ]
        context = SimpleNamespace(platform_specific={"agent_session_id": "sess-retry"})
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-retry",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            metadata={"summary": "Background verification finished"},
            expects_output=True,
        )
        requeued = asyncio.Event()
        original_requeue = service.activities.requeue_completed_output

        def _requeue(activity, *, front=True):
            original_requeue(activity, front=front)
            requeued.set()

        service.activities.requeue_completed_output = _requeue
        emitted = asyncio.Event()
        async def _emit(*_args, **_kwargs):
            emitted.set()
            return "message-id"

        agent.emit_result_message = AsyncMock(side_effect=_emit)

        agent._schedule_completed_activity_flush(composite_key, context)
        await asyncio.wait_for(requeued.wait(), timeout=1)
        agent._pending_requests.pop(composite_key)
        await asyncio.wait_for(emitted.wait(), timeout=1)

        self.assertFalse(service.activities.has_completed_output("claude", composite_key))
        self.assertFalse(agent._activity_output_pending(composite_key))

    async def test_same_turn_summary_delivery_failure_retries_detached(self):
        agent, service = _build_agent()
        composite_key = "session-same-turn-retry:/tmp/work"
        pending_request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={"turn_token": "origin-turn"})
        )
        agent._pending_requests[composite_key] = [pending_request]
        context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-same-turn-retry"}
        )
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-same-turn-retry",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            metadata={"summary": "Background verification finished"},
            expects_output=True,
        )
        agent.emit_result_message = AsyncMock(
            side_effect=[None, "delivered-message-id"],
        )
        agent._remove_result_pending_reaction = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "was not persisted or delivered"):
            await agent._flush_completed_activity_outputs(composite_key, context)

        self.assertFalse(agent._has_pending_requests(composite_key))
        agent._remove_result_pending_reaction.assert_awaited_once_with(
            composite_key,
            context,
            pending_request,
        )
        self.assertTrue(service.activities.has_completed_output("claude", composite_key))
        tidy_output = agent.controller.emit_agent_message.await_args.kwargs["output"]
        self.assertTrue(tidy_output.completes_turn)
        self.assertFalse(tidy_output.settles_run)

        await agent._flush_completed_activity_outputs(composite_key, context)

        self.assertFalse(agent._has_pending_requests(composite_key))
        first_output = agent.emit_result_message.await_args_list[0].kwargs["output"]
        second_output = agent.emit_result_message.await_args_list[1].kwargs["output"]
        self.assertFalse(first_output.detached)
        self.assertTrue(first_output.completes_turn)
        self.assertTrue(second_output.detached)
        self.assertFalse(second_output.completes_turn)
        self.assertFalse(service.activities.has_completed_output("claude", composite_key))

    async def test_result_frame_activity_delivery_failure_retries_detached(self):
        agent, service = _build_agent()
        agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 3600
        composite_key = "session-result-retry:/tmp/work"
        pending_context = SimpleNamespace(
            platform_specific={"turn_token": "origin-turn"},
        )
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "sess-result-retry",
                "turn_token": "origin-turn",
            },
        )
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-result-retry",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            metadata={"summary": "Background verification finished"},
            expects_output=True,
        )
        activity = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(activity)
        pending_request = SimpleNamespace(
            context=pending_context,
            output_activity=activity,
        )
        agent._pending_requests[composite_key] = [pending_request]
        agent.emit_result_message = AsyncMock(
            side_effect=[None, "delivered-message-id"],
        )
        agent._remove_result_pending_reaction = AsyncMock()
        result_processed = asyncio.Event()
        release_receiver = asyncio.Event()

        class _Client:
            def receive_messages(self):
                async def _iterate():
                    yield ResultMessage()
                    result_processed.set()
                    await release_receiver.wait()

                return _iterate()

        receiver = asyncio.create_task(
            agent._receive_messages(
                _Client(),
                "session-result-retry",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        await asyncio.wait_for(result_processed.wait(), timeout=1)

        self.assertFalse(agent._has_pending_requests(composite_key))
        agent._remove_result_pending_reaction.assert_awaited_once_with(
            composite_key,
            context,
            pending_request,
        )
        self.assertTrue(service.activities.has_completed_output("claude", composite_key))

        release_receiver.set()
        await receiver

        self.assertFalse(agent._has_pending_requests(composite_key))
        self.assertEqual(agent.emit_result_message.await_count, 2)
        first_output = agent.emit_result_message.await_args_list[0].kwargs["output"]
        second_output = agent.emit_result_message.await_args_list[1].kwargs["output"]
        self.assertFalse(first_output.detached)
        self.assertTrue(first_output.completes_turn)
        self.assertTrue(second_output.detached)
        self.assertFalse(second_output.completes_turn)
        self.assertFalse(service.activities.has_completed_output("claude", composite_key))

    async def test_runtime_disconnect_marks_session_idle_after_activity_cleanup(self):
        mark_idle_calls: list[str] = []
        agent, service = _build_agent(mark_idle_calls=mark_idle_calls)
        composite_key = "session-disconnected:/tmp/work"
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-disconnected",
            activity_id="task-690",
            kind="local_agent",
        )

        agent._end_activity_runtime(composite_key)

        self.assertFalse(service.activities.has_active("claude", composite_key))
        self.assertEqual(mark_idle_calls, [composite_key])

    async def test_task_start_uses_current_request_run_not_stale_receiver_run(self):
        agent, service = _build_agent()
        composite_key = "session-lineage:/tmp/work"
        pending_context = SimpleNamespace(
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-current",
                "turn_token": "current-turn",
            }
        )
        agent._pending_requests[composite_key] = [SimpleNamespace(context=pending_context)]
        receiver_context = SimpleNamespace(
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-stale",
                "turn_token": "stale-turn",
                "agent_session_id": "sess-lineage",
            }
        )

        self.assertTrue(
            agent._handle_activity_message(
                TaskStartedMessage(),
                composite_key,
                receiver_context,
            )
        )

        activity = service.activities.active_for_runtime("claude", composite_key)[0]
        self.assertEqual(activity.run_id, "run-current")
        self.assertEqual(activity.turn_id, "current-turn")
        self.assertEqual(activity.metadata["run_ids"], ["run-current"])

    async def test_task_progress_keeps_original_run_lineage(self):
        agent, service = _build_agent()
        composite_key = "session-progress-lineage:/tmp/work"
        origin_context = SimpleNamespace(
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-origin",
                "turn_token": "turn-origin",
            }
        )
        agent._pending_requests[composite_key] = [SimpleNamespace(context=origin_context)]
        receiver_context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-progress-lineage"},
        )
        self.assertTrue(
            agent._handle_activity_message(
                TaskStartedMessage(),
                composite_key,
                receiver_context,
            )
        )

        newer_context = SimpleNamespace(
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-newer",
                "turn_token": "turn-newer",
            }
        )
        agent._pending_requests[composite_key] = [SimpleNamespace(context=newer_context)]
        progress = SimpleNamespace(
            subtype="task_progress",
            task_id="task-690",
            description="Still working",
            data={},
        )
        self.assertTrue(
            agent._handle_activity_message(
                progress,
                composite_key,
                receiver_context,
            )
        )

        activity = service.activities.active_for_runtime("claude", composite_key)[0]
        self.assertEqual(activity.run_id, "run-origin")
        self.assertEqual(activity.turn_id, "turn-origin")
        self.assertEqual(activity.metadata["run_ids"], ["run-origin"])

        self.assertTrue(
            agent._handle_activity_message(
                TaskNotificationMessage(),
                composite_key,
                receiver_context,
            )
        )
        completed = service.activities.claim_completed_output("claude", composite_key)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.run_id, "run-origin")
        self.assertEqual(completed.turn_id, "turn-origin")
        self.assertEqual(completed.metadata["run_ids"], ["run-origin"])

    async def test_failed_task_does_not_claim_a_missing_followup_output(self):
        mark_idle_calls: list[str] = []
        agent, service = _build_agent(mark_idle_calls=mark_idle_calls)
        composite_key = "session-failed:/tmp/work"
        context = SimpleNamespace(
            platform_specific={"agent_session_id": "sess-failed"},
        )
        failed = TaskNotificationMessage()
        failed.status = "failed"

        self.assertTrue(agent._handle_activity_message(TaskStartedMessage(), composite_key, context))
        self.assertTrue(agent._handle_activity_message(failed, composite_key, context))

        self.assertIsNone(service.activities.claim_completed_output("claude", composite_key))
        self.assertEqual(mark_idle_calls, [composite_key])

    async def test_completed_background_task_output_does_not_settle_newer_user_turn(self):
        agent, service = _build_agent()
        composite_key = "session-690:/tmp/work"
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "USER-TURN"
        gate.backend = "claude"
        gate.runtime_started = True
        user_context = SimpleNamespace(
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "USER-TURN",
                "turn_token": "user-turn",
            }
        )
        user_request = SimpleNamespace(context=user_context)
        agent._pending_requests[composite_key] = [user_request]
        agent._pending_assistant_message[composite_key] = "newer turn draft"
        receiver_context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD-TURN",
                "turn_token": "old-turn",
                "agent_session_id": "sess-690",
            },
        )
        agent.emit_result_message = AsyncMock(return_value="message-id")
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-690",
            activity_id="task-690",
            kind="local_agent",
            parent_activity_id="tool-690",
            turn_id="origin-turn",
        )

        await agent._receive_messages(
            _task_notification_then_result_client(),
            "session-690",
            "/tmp/work",
            receiver_context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once()
        output = agent.emit_result_message.await_args.kwargs["output"]
        self.assertTrue(output.detached)
        self.assertFalse(output.completes_turn)
        self.assertTrue(output.completes_run)
        self.assertEqual(output.activity_id, "task-690")
        self.assertEqual(output.causation_id, "tool-690")
        self.assertEqual(output.provenance(receiver_context)["turn_id"], "origin-turn")
        self.assertEqual(agent._pending_requests[composite_key], [user_request])
        self.assertEqual(agent._pending_assistant_message[composite_key], "newer turn draft")
        # Hard #862 invariant: background delivery cannot settle a newer Turn.
        self.assertEqual(gate.token, "USER-TURN")
        self.assertTrue(gate.lock.locked())
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_title_backfill_failure_does_not_requeue_delivered_activity(self):
        agent, service = _build_agent()
        composite_key = "session-title-backfill:/tmp/work"
        pending_context = SimpleNamespace(
            platform_specific={"turn_token": "origin-turn"},
        )
        pending_request = SimpleNamespace(context=pending_context)
        agent._pending_requests[composite_key] = [pending_request]
        receiver_context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_session_id": "sess-title-backfill",
                "turn_token": "origin-turn",
            },
        )
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-title-backfill",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            metadata={"summary": "Background verification finished"},
            expects_output=True,
        )
        agent.emit_result_message = AsyncMock(return_value="message-id")
        agent._native_session_ids[composite_key] = "claude-native-session"
        agent._maybe_backfill_session_title = Mock(
            side_effect=RuntimeError("title store unavailable")
        )

        await agent._receive_messages(
            _one_result_client(),
            "session-title-backfill",
            "/tmp/work",
            receiver_context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once()
        agent._maybe_backfill_session_title.assert_called_once_with(
            pending_request,
            "claude-native-session",
        )
        self.assertFalse(service.activities.has_completed_output("claude", composite_key))
        self.assertIsNone(
            service.activities.claim_completed_output("claude", composite_key)
        )

    async def test_contended_activity_result_does_not_poison_next_user_result(self):
        agent, service = _build_agent()
        composite_key = "session-contended:/tmp/work"
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "USER-TURN"
        gate.backend = "claude"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD-TURN",
                "turn_token": "old-turn",
                "agent_session_id": "sess-contended",
            },
        )
        agent.emit_result_message = AsyncMock(return_value="message-id")
        service.activities.start(
            backend="claude",
            runtime_key=composite_key,
            session_id="sess-contended",
            activity_id="task-690",
            kind="local_agent",
            turn_id="origin-turn",
        )
        service.activities.complete(
            backend="claude",
            runtime_key=composite_key,
            activity_id="task-690",
            status="completed",
            metadata={"summary": "Background verification finished"},
            expects_output=True,
        )

        mode = await agent._maybe_begin_agent_initiated_turn(
            context,
            composite_key,
            "session-contended",
            "/tmp/work",
            "session-key",
            message_type="result",
        )

        self.assertEqual(mode, "activity")
        agent.emit_result_message.assert_not_awaited()
        self.assertIn(composite_key, agent._detached_activity_outputs)
        self.assertEqual(gate.token, "USER-TURN")

        await agent._receive_messages(
            _one_result_client(),
            "session-contended",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        first_call = agent.emit_result_message.await_args
        activity_output = first_call.kwargs["output"]
        self.assertTrue(activity_output.detached)
        self.assertFalse(activity_output.completes_turn)
        self.assertNotIn(composite_key, agent._detached_activity_outputs)
        self.assertEqual(gate.token, "USER-TURN")

        user_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "agent_runtime_turn_key": composite_key,
                    "agent_runtime_turn_token": "USER-TURN",
                    "turn_token": "user-turn",
                }
            )
        )
        agent._pending_requests[composite_key] = [user_request]
        await agent._receive_messages(
            _one_result_client(),
            "session-contended",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        self.assertEqual(agent.emit_result_message.await_count, 2)
        user_result_call = agent.emit_result_message.await_args
        self.assertIs(user_result_call.kwargs["request"], user_request)
        self.assertNotIn("output", user_result_call.kwargs)
        self.assertFalse(agent._has_pending_requests(composite_key))
        gate.lock.release()

    async def test_contended_schedule_wakeup_result_is_delivered_detached(self):
        agent, service = _build_agent()
        composite_key = "session-wakeup:/tmp/work"
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "USER-TURN"
        gate.backend = "claude"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD-TURN",
                "agent_session_id": "sess-wakeup",
            },
        )
        agent.emit_result_message = AsyncMock(return_value="message-id")
        try:
            await agent._receive_messages(
                _one_result_client(),
                "session-wakeup",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        finally:
            gate.lock.release()

        agent.emit_result_message.assert_awaited_once()
        output = agent.emit_result_message.await_args.kwargs["output"]
        self.assertTrue(output.detached)
        self.assertFalse(output.completes_turn)
        self.assertFalse(output.completes_run)
        self.assertEqual(gate.token, "USER-TURN")
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_identical_schedule_wakeup_results_get_distinct_output_ids(self):
        agent, service = _build_agent()
        composite_key = "session-repeat-wakeup:/tmp/work"
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "USER-TURN"
        gate.backend = "claude"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD-TURN",
                "agent_session_id": "sess-repeat-wakeup",
            },
        )
        agent.emit_result_message = AsyncMock()
        try:
            await agent._receive_messages(
                _two_result_client(),
                "session-repeat-wakeup",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        finally:
            gate.lock.release()

        self.assertEqual(agent.emit_result_message.await_count, 2)
        output_ids = [
            call.kwargs["output"].idempotency_key
            for call in agent.emit_result_message.await_args_list
        ]
        self.assertEqual(len(set(output_ids)), 2)

    async def test_task_completed_inside_its_origin_turn_remains_attached(self):
        agent, service = _build_agent()
        composite_key = "session-current:/tmp/work"
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "CURRENT-TURN"
        gate.backend = "claude"
        user_context = SimpleNamespace(
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "CURRENT-TURN",
                "turn_token": "current-turn",
            }
        )
        agent._pending_requests[composite_key] = [SimpleNamespace(context=user_context)]
        receiver_context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "CURRENT-TURN",
                "turn_token": "current-turn",
                "agent_session_id": "sess-current",
            },
        )
        agent.emit_result_message = AsyncMock()

        await agent._receive_messages(
            _completed_task_then_result_client(),
            "session-current",
            "/tmp/work",
            receiver_context,
            composite_key=composite_key,
        )

        output = agent.emit_result_message.await_args.kwargs["request"].output
        self.assertFalse(output.detached)
        self.assertTrue(output.completes_turn)
        self.assertTrue(output.completes_run)
        self.assertEqual(output.activity_id, "task-690")
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_non_actionable_fallback_does_not_open_agent_initiated_turn(self):
        active_calls: list[str] = []
        agent, service = _build_agent(active_calls=active_calls)
        composite_key = "session-fallback:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD",
                "agent_session_id": "sess-fallback",
            },
        )

        await agent._receive_messages(
            _fallback_client(
                {
                    "direction": "declined",
                    "original_model": "claude-fable-5",
                    "fallback_model": "claude-opus-4-8",
                }
            ),
            "session-fallback",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        gate = service._get_turn_gate(composite_key)
        self.assertFalse(gate.lock.locked())
        self.assertEqual(gate.token, "")
        self.assertFalse(agent._has_pending_requests(composite_key))
        self.assertEqual(active_calls, [])
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_fallback_notice_is_dropped_when_agent_initiated_turn_is_contended(self):
        agent, service = _build_agent()
        composite_key = "session-fallback:/tmp/work"
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "USER-TURN"
        gate.backend = "claude"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD",
                "agent_session_id": "sess-fallback",
            },
        )
        try:
            await agent._receive_messages(
                _fallback_client(
                    {
                        "originalModel": "claude-fable-5",
                        "fallbackModel": "claude-opus-4-8",
                    }
                ),
                "session-fallback",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        finally:
            gate.lock.release()

        self.assertEqual(gate.token, "USER-TURN")
        self.assertFalse(agent._has_pending_requests(composite_key))
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_unsolicited_reply_is_delivered_not_dropped(self):
        running_calls: list = []
        mark_idle_calls: list[str] = []
        active_calls: list[str] = []
        agent, service = _build_agent(
            running_calls=running_calls, mark_idle_calls=mark_idle_calls, active_calls=active_calls
        )
        composite_key = "session-1:/tmp/work"
        # Receiver context as it looks AFTER the previous turn completed: a stale
        # runtime-turn token, no pending request, and the gate already released.
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "OLD",
                "agent_session_id": "sess-1",
            },
        )
        self.assertFalse(service.emit_matches_runtime_turn(context))

        # Capture whether the guard would pass at the moment the result is emitted.
        guard_at_emit: list[bool] = []
        token_at_emit: list[str] = []

        async def _capture_result(ctx, *a, **k):
            guard_at_emit.append(service.emit_matches_runtime_turn(ctx))
            token_at_emit.append(service._get_turn_gate(composite_key).token)
            service.release_runtime_turn(ctx)

        agent.emit_result_message = AsyncMock(side_effect=_capture_result)

        await agent._receive_messages(
            _assistant_then_result_client(),
            "session-1",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        # The reply reached emit (was NOT dropped), and the guard passed.
        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(guard_at_emit, [True])
        # A real agent-initiated turn was opened (fresh token, not the stale one).
        gate = service._get_turn_gate(composite_key)
        self.assertNotEqual(token_at_emit, ["OLD"])
        self.assertTrue(token_at_emit[0])
        self.assertEqual(gate.token, "")
        # INBOUND chokepoint fired once and the turn settled (synthetic request popped).
        self.assertEqual(len(running_calls), 1)
        # Marked active on open (eviction parity with a user turn) then idle on settle.
        self.assertEqual(active_calls, [composite_key])
        self.assertEqual(mark_idle_calls, [composite_key])
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_normal_turn_with_pending_request_does_not_reopen(self):
        agent, service = _build_agent()
        composite_key = "session-2:/tmp/work"
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "R1"
        gate.backend = "claude"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "R1",
                "agent_session_id": "sess-2",
            },
        )
        # A real turn is in flight: one pending request already enqueued.
        agent._pending_requests[composite_key] = [SimpleNamespace(context=context)]
        agent.emit_result_message = AsyncMock(return_value=None)

        await agent._receive_messages(
            _one_result_client(),
            "session-2",
            "/tmp/work",
            context,
            composite_key=composite_key,
        )

        agent.emit_result_message.assert_awaited_once()
        # The gate token was NOT replaced by a fresh agent-initiated token: the
        # detection correctly no-ops when a real turn already owns the session.
        self.assertEqual(gate.token, "R1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
