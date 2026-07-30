"""Regression: a terminal ResultMessage must settle the turn (release the
per-turn ``active`` flag) even if emitting the result raises.

Before the hardening, the result branch in ``ClaudeAgent._receive_messages``
popped the pending request and then called ``emit_result_message`` /
``_maybe_backfill_session_title`` BEFORE marking the session idle. If either
raised, the inner ``except Exception: … continue`` swallowed it and skipped the
mark-idle, so the long-lived receiver looped back and blocked with ``active``
still set — pinning the session in ``active_sessions`` (exempt from idle
eviction) until the next service restart. The mark-idle now runs in a
``finally`` so the turn always settles.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.services.agent_steering import ActiveSteerTarget, SteerOutcome, SteerRequest
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.service import AgentService


class _ResultMessage:
    subtype = "success"
    result = "done"
    duration_ms = 1


class _AssistantFailureMessage:
    content: list = []
    error = "primary failed"
    is_error = True


def _one_result_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield _ResultMessage()

            return _iterate()

    return _Client()


def _with_input_closer(client):
    client._transport = SimpleNamespace(end_input=AsyncMock())
    return client


def _build_agent(mark_idle_calls):
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
            touch_session_activity=lambda key: None,
        ),
    )
    controller._get_session_key = lambda context: "session-key"

    agent = ClaudeAgent(controller)
    # Stub the external bits the result branch touches so the test isolates the
    # settle-on-failure contract.
    agent._detect_message_type = lambda message: "result"
    agent._maybe_capture_session_id = lambda *a, **k: None
    agent._consume_suppressed_synthetic_result = lambda *a, **k: False
    agent._handle_auth_failure_result = AsyncMock(return_value=False)
    agent._reserved_native_session_id = lambda *a, **k: None
    agent._adopt_pending_turn_token = lambda *a, **k: None
    agent._discard_pending_reaction = lambda key: None
    agent._get_formatter = lambda context: None
    agent._handle_receiver_eof = AsyncMock()
    return agent


class ResultSettlesTurnOnEmitFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_interrupt_stays_nonsteerable_while_result_settles(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-interrupt-unknown:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        agent.controller.emit_agent_message = AsyncMock(return_value=None)
        result_ready = asyncio.Event()

        class _Client:
            async def interrupt(self):
                raise TimeoutError("interrupt acknowledgement timed out")

            async def disconnect(self):
                return None

            def receive_messages(self):
                async def _iterate():
                    await result_ready.wait()
                    yield _ResultMessage()

                return _iterate()

        client = _Client()
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-interrupt-unknown",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        stop_request = SimpleNamespace(
            context=context,
            composite_session_id=composite_key,
            stop_failure_reason=None,
        )

        self.assertFalse(await agent.handle_stop(stop_request))
        self.assertIn(composite_key, agent._ambiguous_interrupt_keys())
        self.assertIsNone(agent.steering_native_turn_id(target))

        result_ready.set()
        await receiver_task

        agent.emit_result_message.assert_awaited_once()
        self.assertFalse(agent._has_pending_requests(composite_key))
        self.assertNotIn(composite_key, agent._ambiguous_interrupt_keys())

    async def test_successful_steer_supersedes_a_concurrent_primary_result(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-steer:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        first_result_ready = asyncio.Event()
        first_result_yielded = asyncio.Event()
        second_result_ready = asyncio.Event()

        class _SteeringClient:
            async def query(self, text, *, session_id):
                self.query_call = (text, session_id)
                query_started.set()
                await release_query.wait()

            def receive_messages(self):
                async def _iterate():
                    await first_result_ready.wait()
                    first_result_yielded.set()
                    first = _ResultMessage()
                    first.result = "primary result"
                    yield first
                    await second_result_ready.wait()
                    second = _ResultMessage()
                    second.result = "steered result"
                    yield second

                return _iterate()

        client = _with_input_closer(_SteeringClient())
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-steer",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-steer",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="补充：`exact`",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        first_result_ready.set()
        await first_result_yielded.wait()
        await asyncio.sleep(0)
        release_query.set()

        receipt = await steer_task
        await asyncio.sleep(0)
        self.assertIs(receipt.outcome, SteerOutcome.ACCEPTED)
        self.assertEqual(client.query_call, ("补充：`exact`", composite_key))
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        agent.emit_result_message.assert_not_awaited()

        second_result_ready.set()
        await receiver_task

        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(agent.emit_result_message.await_args.args[1], "steered result")
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_successful_steer_supersedes_a_buffered_primary_result(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-buffered-result:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        consume_buffered_result = asyncio.Event()
        buffered_result_processed = asyncio.Event()
        final_result_ready = asyncio.Event()

        class _BufferedClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()

            def receive_messages(self):
                async def _iterate():
                    await consume_buffered_result.wait()
                    buffered = _ResultMessage()
                    buffered.result = "buffered primary result"
                    yield buffered
                    await final_result_ready.wait()
                    final = _ResultMessage()
                    final.result = "steered result"
                    yield final

                return _iterate()

        client = _with_input_closer(_BufferedClient())
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-buffered-result",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        terminal_claim_superseded = agent._terminal_claim_superseded

        def _observe_terminal_claim(*args):
            superseded = terminal_claim_superseded(*args)
            if asyncio.current_task() is receiver_task:
                buffered_result_processed.set()
            return superseded

        agent._terminal_claim_superseded = _observe_terminal_claim
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-buffered-result",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="continue after buffered result",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        release_query.set()
        receipt = await steer_task
        self.assertIs(receipt.outcome, SteerOutcome.ACCEPTED)

        consume_buffered_result.set()
        await buffered_result_processed.wait()
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        agent.emit_result_message.assert_not_awaited()

        final_result_ready.set()
        await receiver_task
        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(agent.emit_result_message.await_args.args[1], "steered result")
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_ambiguous_steer_retains_owner_past_a_concurrent_primary_result(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-ambiguous:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        first_result_ready = asyncio.Event()
        first_result_yielded = asyncio.Event()
        second_result_ready = asyncio.Event()

        class _AmbiguousClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()
                raise TimeoutError(f"ambiguous write for {session_id}")

            def receive_messages(self):
                async def _iterate():
                    await first_result_ready.wait()
                    first_result_yielded.set()
                    first = _ResultMessage()
                    first.result = "primary result"
                    yield first
                    await second_result_ready.wait()
                    second = _ResultMessage()
                    second.result = "reconciled result"
                    yield second

                return _iterate()

        client = _AmbiguousClient()
        client._transport = SimpleNamespace(end_input=AsyncMock())
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-ambiguous",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-ambiguous",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="ambiguous input",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        first_result_ready.set()
        await first_result_yielded.wait()
        await asyncio.sleep(0)
        release_query.set()

        receipt = await steer_task
        await asyncio.sleep(0)
        self.assertIs(receipt.outcome, SteerOutcome.UNKNOWN)
        client._transport.end_input.assert_awaited_once_with()
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        agent.emit_result_message.assert_not_awaited()

        second_result_ready.set()
        await receiver_task

        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(agent.emit_result_message.await_args.args[1], "reconciled result")
        self.assertFalse(agent._has_pending_requests(composite_key))
        self.assertNotIn(composite_key, agent.claude_sessions)
        self.assertNotIn(composite_key, agent.receiver_tasks)

    async def test_ambiguous_steer_without_followup_settles_primary_and_retires_runtime(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-ambiguous-undelivered:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        result_ready = asyncio.Event()
        input_ended = asyncio.Event()

        class _UndeliveredClient:
            async def query(self, _text, *, session_id):
                raise TimeoutError(f"ambiguous write for {session_id}")

            def receive_messages(self):
                async def _iterate():
                    await result_ready.wait()
                    primary = _ResultMessage()
                    primary.result = "primary result"
                    yield primary
                    await input_ended.wait()

                return _iterate()

        client = _UndeliveredClient()
        client._transport = SimpleNamespace(
            end_input=AsyncMock(side_effect=lambda: input_ended.set())
        )
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-ambiguous-undelivered",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-ambiguous-undelivered",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="ambiguous input",
        )

        receipt = await agent.steer_active_turn(request, target)
        self.assertIs(receipt.outcome, SteerOutcome.UNKNOWN)
        client._transport.end_input.assert_awaited_once_with()
        self.assertTrue(input_ended.is_set())

        result_ready.set()
        await receiver_task
        await asyncio.sleep(0)

        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(agent.emit_result_message.await_args.args[1], "primary result")
        self.assertFalse(agent._has_pending_requests(composite_key))
        self.assertNotIn(composite_key, agent.claude_sessions)
        self.assertNotIn(composite_key, agent.receiver_tasks)

    async def test_failed_ambiguous_half_close_preserves_work_until_later_result(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-ambiguous-close-failed:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        result_ready = asyncio.Event()
        result_yielded = asyncio.Event()
        second_result_ready = asyncio.Event()

        class _CloseFailedClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()
                raise TimeoutError(f"ambiguous write for {session_id}")

            def receive_messages(self):
                async def _iterate():
                    await result_ready.wait()
                    primary = _ResultMessage()
                    primary.result = "primary result"
                    result_yielded.set()
                    yield primary
                    await second_result_ready.wait()
                    second = _ResultMessage()
                    second.result = "reconciled result"
                    yield second

                return _iterate()

        client = _CloseFailedClient()
        client._transport = SimpleNamespace(
            end_input=AsyncMock(side_effect=RuntimeError("stdin close failed"))
        )
        client.disconnect = AsyncMock()
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-ambiguous-close-failed",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-ambiguous-close-failed",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="ambiguous input",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        result_ready.set()
        await result_yielded.wait()
        release_query.set()

        receipt = await steer_task
        client.disconnect.assert_not_awaited()
        second_result_ready.set()
        await receiver_task

        self.assertIs(receipt.outcome, SteerOutcome.UNKNOWN)
        client._transport.end_input.assert_awaited_once_with()
        client.disconnect.assert_awaited_once_with()
        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(agent.emit_result_message.await_args.args[1], "reconciled result")
        self.assertFalse(agent._has_pending_requests(composite_key))
        self.assertNotIn(composite_key, agent.claude_sessions)
        self.assertNotIn(composite_key, agent.receiver_tasks)

    async def test_successful_steer_supersedes_a_concurrent_activity_flush(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-activity-steer:/tmp/work"
        primary_request = SimpleNamespace(context=context, output_activities=[])
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        query_started = asyncio.Event()
        release_query = asyncio.Event()

        class _SteeringClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()

        client = _with_input_closer(_SteeringClient())
        receiver_task = asyncio.create_task(asyncio.Event().wait())
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        activity = SimpleNamespace()
        registry = SimpleNamespace(
            has_completed_output=Mock(return_value=True),
            requeue_completed_outputs=Mock(),
        )
        agent._activity_registry = lambda: registry
        agent._claim_activity_batch_for_turns = Mock(return_value=[activity])
        agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 0
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-activity-steer",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="continue after activity",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        agent._schedule_completed_activity_flush(composite_key, context)
        flush_task = agent._activity_flush_tasks[composite_key]
        await asyncio.sleep(0)
        release_query.set()

        receipt = await steer_task
        await flush_task
        self.assertIs(receipt.outcome, SteerOutcome.ACCEPTED)
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        agent._claim_activity_batch_for_turns.assert_called_once()
        registry.requeue_completed_outputs.assert_called_once_with([activity])
        agent.emit_result_message.assert_not_awaited()

        receiver_task.cancel()
        await asyncio.gather(receiver_task, return_exceptions=True)

    async def test_unmatched_activity_flush_does_not_consume_terminal_barrier(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-unmatched-activity:/tmp/work"
        primary_request = SimpleNamespace(context=context, output_activities=[])
        agent._pending_requests[composite_key] = [primary_request]
        registry = SimpleNamespace(has_completed_output=Mock(return_value=True))
        agent._activity_registry = lambda: registry
        agent._claim_activity_batch_for_turns = Mock(return_value=[])
        expected_generation = agent._steering_generation(composite_key)
        agent._advance_steering_generation(composite_key)

        retry = await agent._flush_completed_activity_outputs(
            composite_key,
            context,
            expected_steering_generation=expected_generation,
        )

        self.assertTrue(retry)
        self.assertEqual(agent._next_terminal_barrier(composite_key), "accepted")
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])

    async def test_concurrent_receiver_eof_makes_steering_ack_ambiguous(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        agent._handle_receiver_eof = ClaudeAgent._handle_receiver_eof.__get__(agent)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-eof-steer:/tmp/work"
        primary_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={"agent_runtime_turn_token": "runtime-turn"}
            )
        )
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        agent.controller.emit_agent_message = AsyncMock(return_value=None)
        agent._cleanup_runtime_session = AsyncMock()
        agent._release_service_runtime_turn = Mock()
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        finish_receiver = asyncio.Event()
        eof_started = asyncio.Event()

        class _SteeringClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()

            def receive_messages(self):
                async def _iterate():
                    await finish_receiver.wait()
                    if False:
                        yield None

                return _iterate()

        original_eof = agent._handle_receiver_eof

        async def _observed_eof(*args, **kwargs):
            eof_started.set()
            return await original_eof(*args, **kwargs)

        agent._handle_receiver_eof = _observed_eof
        client = _with_input_closer(_SteeringClient())
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-eof-steer",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-eof-steer",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="continue before eof",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        finish_receiver.set()
        await eof_started.wait()
        release_query.set()

        receipt = await steer_task
        await receiver_task
        self.assertIs(receipt.outcome, SteerOutcome.UNKNOWN)
        self.assertEqual(receipt.reason, "receiver_generation_changed")
        self.assertNotIn(composite_key, agent._pending_requests)
        agent.emit_result_message.assert_not_awaited()
        agent.controller.emit_agent_message.assert_awaited_once()
        agent._cleanup_runtime_session.assert_awaited_once_with(
            composite_key,
            current_receiver_task=receiver_task,
            preserve_pending_request_state=True,
        )
        agent._release_service_runtime_turn.assert_called_once_with(context)

    async def test_concurrent_receiver_crash_makes_steering_ack_ambiguous(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-crash-steer:/tmp/work"
        primary_request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={"agent_runtime_turn_token": "runtime-turn"}
            )
        )
        agent._pending_requests[composite_key] = [primary_request]
        agent.controller.emit_agent_message = AsyncMock(return_value=None)
        agent.controller.agent_auth_service = SimpleNamespace(
            maybe_emit_auth_recovery_message=AsyncMock(return_value=False)
        )
        agent.session_handler.handle_session_error = AsyncMock()
        agent._clear_pending_reactions = AsyncMock()
        agent.record_model_hub_native_failure = AsyncMock()
        agent._release_service_runtime_turn = Mock()
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        crash_receiver = asyncio.Event()
        crash_handler_started = asyncio.Event()

        class _SteeringClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()

            def receive_messages(self):
                async def _iterate():
                    await crash_receiver.wait()
                    raise RuntimeError("receiver disconnected")
                    yield None  # pragma: no cover

                return _iterate()

        original_handler = agent._handle_receiver_exception

        async def _observed_handler(*args, **kwargs):
            crash_handler_started.set()
            return await original_handler(*args, **kwargs)

        agent._handle_receiver_exception = _observed_handler
        client = _with_input_closer(_SteeringClient())
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-crash-steer",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-crash-steer",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="continue before crash",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        crash_receiver.set()
        await crash_handler_started.wait()
        release_query.set()

        receipt = await steer_task
        await receiver_task
        self.assertIs(receipt.outcome, SteerOutcome.UNKNOWN)
        self.assertEqual(receipt.reason, "receiver_generation_changed")
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        agent._clear_pending_reactions.assert_awaited_once_with(composite_key, context)
        agent.record_model_hub_native_failure.assert_awaited_once_with(
            primary_request.context,
            "receiver disconnected",
        )
        agent.session_handler.handle_session_error.assert_awaited_once()
        agent.controller.emit_agent_message.assert_awaited_once()
        agent._release_service_runtime_turn.assert_called_once_with(context)

    async def test_end_runtime_session_serializes_with_steering_write(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-end-steer:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        interrupt_called = asyncio.Event()
        events: list[str] = []

        class _SteeringClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()
                events.append("query")

            async def interrupt(self):
                events.append("interrupt")
                interrupt_called.set()

        client = _with_input_closer(_SteeringClient())
        receiver_task = asyncio.create_task(asyncio.Event().wait())
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        agent._cleanup_runtime_session = AsyncMock()
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-end-steer",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="continue before end",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        end_task = asyncio.create_task(agent.end_runtime_session(composite_key))
        await asyncio.sleep(0)
        self.assertFalse(interrupt_called.is_set())
        release_query.set()

        receipt = await steer_task
        ended = await end_task
        self.assertIs(receipt.outcome, SteerOutcome.ACCEPTED)
        self.assertTrue(ended)
        self.assertEqual(events, ["query", "interrupt"])
        agent._cleanup_runtime_session.assert_awaited_once_with(composite_key)
        self.assertIn(composite_key, agent._steering_closing_keys())

        receiver_task.cancel()
        await asyncio.gather(receiver_task, return_exceptions=True)

    async def test_stop_claims_result_owner_before_waiting_terminal_frame(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-stop-result:/tmp/work"
        primary_request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            ack_reaction_message_id=None,
            ack_reaction_emoji=None,
        )
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        agent.controller.emit_agent_message = AsyncMock(return_value=None)
        agent._cleanup_runtime_session = AsyncMock()
        agent._remove_specific_pending_reaction = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        interrupt_started = asyncio.Event()
        release_interrupt = asyncio.Event()
        result_ready = asyncio.Event()
        result_generation_captured = asyncio.Event()

        class _Client:
            async def interrupt(self):
                interrupt_started.set()
                await release_interrupt.wait()

            def receive_messages(self):
                async def _iterate():
                    await result_ready.wait()
                    yield _ResultMessage()
                    await asyncio.Event().wait()

                return _iterate()

        client = _Client()
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-stop-result",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        steering_generation = agent._steering_generation

        def _capture_receiver_generation(key):
            generation = steering_generation(key)
            if asyncio.current_task() is receiver_task:
                result_generation_captured.set()
            return generation

        agent._steering_generation = _capture_receiver_generation
        stop_request = SimpleNamespace(
            context=SimpleNamespace(platform_specific={}),
            composite_session_id=composite_key,
            stop_failure_reason=None,
        )

        stop_task = asyncio.create_task(agent.handle_stop(stop_request))
        await interrupt_started.wait()
        result_ready.set()
        await result_generation_captured.wait()
        release_interrupt.set()

        self.assertTrue(await stop_task)
        await asyncio.sleep(0)
        self.assertNotIn(composite_key, agent._pending_requests)
        agent.emit_result_message.assert_not_awaited()
        agent.controller.emit_agent_message.assert_awaited_once()

        receiver_task.cancel()
        await asyncio.gather(receiver_task, return_exceptions=True)

    async def test_successful_steer_supersedes_concurrent_system_auth_failure(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-system-auth:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent._detect_message_type = lambda _message: "system"
        agent.claude_client.format_message = (
            lambda *args, **kwargs: "Failed to authenticate. API Error: 401 Invalid bearer token"
        )
        agent._handle_auth_failure_result = AsyncMock(return_value=True)
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        auth_ready = asyncio.Event()
        auth_generation_captured = asyncio.Event()

        class _Client:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()

            def receive_messages(self):
                async def _iterate():
                    await auth_ready.wait()
                    yield SimpleNamespace(subtype="error")
                    await asyncio.Event().wait()

                return _iterate()

        client = _with_input_closer(_Client())
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-system-auth",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        steering_generation = agent._steering_generation

        def _capture_receiver_generation(key):
            generation = steering_generation(key)
            if asyncio.current_task() is receiver_task:
                auth_generation_captured.set()
            return generation

        agent._steering_generation = _capture_receiver_generation
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-system-auth",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="continue after system error",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        auth_ready.set()
        await auth_generation_captured.wait()
        release_query.set()

        receipt = await steer_task
        await asyncio.sleep(0)
        self.assertIs(receipt.outcome, SteerOutcome.ACCEPTED)
        agent._handle_auth_failure_result.assert_not_awaited()
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])

        receiver_task.cancel()
        await asyncio.gather(receiver_task, return_exceptions=True)

    async def test_successful_steer_supersedes_concurrent_assistant_terminal_failure(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-assistant-failure:/tmp/work"
        primary_request = SimpleNamespace(context=context)
        agent._pending_requests[composite_key] = [primary_request]
        agent.emit_result_message = AsyncMock(return_value=None)
        agent._detect_message_type = lambda message: (
            "assistant" if isinstance(message, _AssistantFailureMessage) else "result"
        )
        agent._handle_assistant_terminal_failure = AsyncMock(return_value="failure")
        consume_suppressed_result = ClaudeAgent._consume_suppressed_synthetic_result.__get__(agent)
        paired_result_consumed = asyncio.Event()

        def _consume_suppressed_result(*args):
            consumed = consume_suppressed_result(*args)
            if consumed:
                paired_result_consumed.set()
            return consumed

        agent._consume_suppressed_synthetic_result = _consume_suppressed_result
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        failure_ready = asyncio.Event()
        failure_generation_captured = asyncio.Event()
        paired_result_ready = asyncio.Event()
        first_steered_result_ready = asyncio.Event()
        second_steered_result_ready = asyncio.Event()

        class _SteeringClient:
            async def query(self, _text, *, session_id):
                query_started.set()
                await release_query.wait()

            def receive_messages(self):
                async def _iterate():
                    await failure_ready.wait()
                    yield _AssistantFailureMessage()
                    await paired_result_ready.wait()
                    failed = _ResultMessage()
                    failed.result = "primary failed"
                    yield failed
                    await first_steered_result_ready.wait()
                    first_steered = _ResultMessage()
                    first_steered.result = "first steered result"
                    yield first_steered
                    await second_steered_result_ready.wait()
                    second_steered = _ResultMessage()
                    second_steered.result = "second steered result"
                    yield second_steered

                return _iterate()

        client = _with_input_closer(_SteeringClient())
        receiver_task = asyncio.create_task(
            agent._receive_messages(
                client,
                "session-assistant-failure",
                "/tmp/work",
                context,
                composite_key=composite_key,
            )
        )
        agent.claude_sessions[composite_key] = client
        agent.receiver_tasks[composite_key] = receiver_task
        agent.session_handler.active_sessions = {composite_key}
        steering_generation = agent._steering_generation

        def _capture_receiver_generation(key):
            generation = steering_generation(key)
            if asyncio.current_task() is receiver_task:
                failure_generation_captured.set()
            return generation

        agent._steering_generation = _capture_receiver_generation
        target = ActiveSteerTarget(
            runtime_key=composite_key,
            logical_turn_id="logical-turn",
            context=context,
            agent_request=primary_request,
            agent=agent,
        )
        request = SteerRequest(
            target_session_id="session-assistant-failure",
            expected_logical_turn_id="logical-turn",
            expected_native_turn_id=(
                f"claude:{composite_key}:{id(client)}:{id(receiver_task)}"
            ),
            text="continue after failure",
        )

        steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
        await query_started.wait()
        failure_ready.set()
        await failure_generation_captured.wait()
        release_query.set()

        receipt = await steer_task
        await asyncio.sleep(0)
        self.assertIs(receipt.outcome, SteerOutcome.ACCEPTED)
        agent._handle_assistant_terminal_failure.assert_not_awaited()
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        self.assertIn(composite_key, agent._suppressed_synthetic_results)

        second_receipt = await agent.steer_active_turn(request, target)
        self.assertIs(second_receipt.outcome, SteerOutcome.ACCEPTED)

        paired_result_ready.set()
        await paired_result_consumed.wait()
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        agent.emit_result_message.assert_not_awaited()

        first_steered_result_ready.set()
        await asyncio.sleep(0)
        self.assertEqual(agent._pending_requests[composite_key], [primary_request])
        agent.emit_result_message.assert_not_awaited()

        second_steered_result_ready.set()
        await receiver_task

        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(agent.emit_result_message.await_args.args[1], "second steered result")
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_emit_failure_still_marks_session_idle(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-1:/tmp/work"

        # A turn is in flight: one pending request for this session.
        agent._pending_requests[composite_key] = [SimpleNamespace(context=context)]
        # Emitting the terminal result fails.
        agent.emit_result_message = AsyncMock(side_effect=RuntimeError("boom"))

        await agent._receive_messages(
            _one_result_client(), "session-1", "/tmp/work", context, composite_key=composite_key
        )

        # Despite the emit failure, the turn settled: the active flag was released
        # and the pending request was popped.
        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(mark_idle_calls, [composite_key])
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_emit_failure_releases_runtime_gate(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        service = AgentService(agent.controller)
        service.register(agent)
        agent.controller.agent_service = service
        composite_key = "session-1:/tmp/work"
        context = SimpleNamespace(
            user_id="U1",
            channel_id="C1",
            platform_specific={
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "R1",
            },
        )
        pending_context = SimpleNamespace(
            platform_specific={
                "turn_token": "T1",
                "agent_runtime_turn_key": composite_key,
                "agent_runtime_turn_token": "R1",
            },
        )
        agent._pending_requests[composite_key] = [SimpleNamespace(context=pending_context)]
        gate = service._get_turn_gate(composite_key)
        await gate.lock.acquire()
        gate.token = "R1"
        agent.emit_result_message = AsyncMock(side_effect=RuntimeError("boom"))

        await agent._receive_messages(
            _one_result_client(), "session-1", "/tmp/work", context, composite_key=composite_key
        )

        agent.emit_result_message.assert_awaited_once()
        self.assertFalse(gate.lock.locked())
        self.assertEqual(mark_idle_calls, [composite_key])
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_emit_success_marks_session_idle(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        composite_key = "session-2:/tmp/work"

        agent._pending_requests[composite_key] = [SimpleNamespace(context=context)]
        agent.emit_result_message = AsyncMock(return_value=None)

        await agent._receive_messages(
            _one_result_client(), "session-2", "/tmp/work", context, composite_key=composite_key
        )

        agent.emit_result_message.assert_awaited_once()
        self.assertEqual(mark_idle_calls, [composite_key])
        self.assertFalse(agent._has_pending_requests(composite_key))

    async def test_force_cleanup_suppresses_receiver_release_until_terminal_emit(self):
        mark_idle_calls: list[str] = []
        agent = _build_agent(mark_idle_calls)
        composite_key = "session-3:/tmp/work"
        context = SimpleNamespace(user_id="U1", channel_id="C1", platform_specific={})
        agent._pending_requests[composite_key] = [SimpleNamespace(context=context)]
        events: list[tuple[str, bool]] = []

        async def _cleanup_runtime_session(key, **_kwargs):
            events.append(("cleanup", key in agent._suppress_receiver_runtime_release))
            agent._release_service_runtime_turn(context)

        agent._cleanup_runtime_session = _cleanup_runtime_session
        agent.controller.emit_agent_message = AsyncMock(
            side_effect=lambda *_args, **_kwargs: events.append(("emit", False))
        )
        agent._remove_result_pending_reaction = AsyncMock()
        agent._release_service_runtime_turn = lambda _context: events.append(
            ("release", composite_key in agent._suppress_receiver_runtime_release)
        )

        await agent.force_cleanup_stuck_active_session(composite_key)

        self.assertEqual(events, [("cleanup", True), ("release", True), ("emit", False)])
        agent.controller.emit_agent_message.assert_awaited_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
