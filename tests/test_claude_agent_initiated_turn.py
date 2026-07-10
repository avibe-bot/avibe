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
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.claude_agent import ClaudeAgent
from modules.agents.service import AgentService


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
    agent._handle_synthetic_api_error_message = AsyncMock(return_value=False)
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

        async def _capture_result(ctx, *a, **k):
            guard_at_emit.append(service.emit_matches_runtime_turn(ctx))

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
        self.assertNotEqual(gate.token, "OLD")
        self.assertTrue(gate.token)
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
