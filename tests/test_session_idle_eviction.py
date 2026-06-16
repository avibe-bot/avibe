"""Regression test for the active-session state leak (fix/active-session-leak).

``ClaudeAgent._receive_messages`` must release the per-turn ``active`` flag in
its ``finally`` even when the receive stream is exhausted WITHOUT a terminal
ResultMessage (and with no other turn queued). Before the fix this path left
the session pinned active forever, which permanently exempted it from idle
eviction (``SessionHandler.evict_idle_sessions`` skips active sessions), so the
resident Claude CLI process survived until the next service restart.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.claude_agent import ClaudeAgent


def _empty_stream_client():
    """A Claude SDK client whose receive stream ends without any message."""

    class _Client:
        def receive_messages(self):
            async def _iterate():
                return
                yield  # pragma: no cover - makes this an async generator

            return _iterate()

    return _Client()


def _agent_controller():
    controller = SimpleNamespace(
        config=SimpleNamespace(platform="slack"),
        im_client=SimpleNamespace(formatter=None),
        settings_manager=SimpleNamespace(sessions=None),
        session_manager=SimpleNamespace(),
        receiver_tasks={},
        claude_sessions={},
        claude_client=SimpleNamespace(_is_skip_message=lambda message: False),
    )
    controller._get_session_key = lambda context: "session-key"
    return controller


class ReceiverReleasesActiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_stream_without_result_marks_idle(self):
        controller = _agent_controller()
        mark_idle_calls = []
        controller.session_handler = SimpleNamespace(
            mark_session_idle=lambda key: mark_idle_calls.append(key),
        )
        agent = ClaudeAgent(controller)
        context = SimpleNamespace(user_id="U1", channel_id="C1")
        composite_key = "session-1:/tmp/work"

        await agent._receive_messages(
            _empty_stream_client(), "session-1", "/tmp/work", context, composite_key=composite_key
        )

        # The guaranteed-release guard in the finally must have fired.
        self.assertEqual(mark_idle_calls, [composite_key])

    async def test_exhausted_stream_keeps_active_when_request_queued(self):
        controller = _agent_controller()
        mark_idle_calls = []
        controller.session_handler = SimpleNamespace(
            mark_session_idle=lambda key: mark_idle_calls.append(key),
        )
        agent = ClaudeAgent(controller)
        context = SimpleNamespace(user_id="U1", channel_id="C1")
        composite_key = "session-1:/tmp/work"
        # A follow-up turn is still queued; the finally guard must be a no-op so
        # an in-flight request is never wrongly demoted to idle.
        agent._pending_requests[composite_key] = [SimpleNamespace(started_at=None)]

        await agent._receive_messages(
            _empty_stream_client(), "session-1", "/tmp/work", context, composite_key=composite_key
        )

        self.assertEqual(mark_idle_calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
