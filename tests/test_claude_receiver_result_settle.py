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

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.claude_agent import ClaudeAgent
from modules.agents.service import AgentService


class _ResultMessage:
    subtype = "success"
    result = "done"
    duration_ms = 1


def _one_result_client():
    class _Client:
        def receive_messages(self):
            async def _iterate():
                yield _ResultMessage()

            return _iterate()

    return _Client()


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
