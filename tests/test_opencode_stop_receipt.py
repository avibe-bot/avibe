"""OpenCode's /stop must leave a ⏹️ receipt like the other two backends.

OpenCode has no interrupt RPC: a stop cancels the in-flight request task, and
the cancellation lands inside the request coroutine, which is what owns the 👀.
That coroutine cannot tell a user stop from a shutdown or a ``clear_sessions``
sweep on its own, so ``handle_stop`` publishes the intent for the duration of
the cancellation. Without it, an OpenCode stop clears the reaction and emits a
silent result — a turn that ends with no trace at all.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.processing_indicator import STOPPED_REACTION_EMOJI
from modules.agents.opencode.agent import OpenCodeAgent


def _agent():
    agent = object.__new__(OpenCodeAgent)
    agent._active_requests = {}
    agent._user_stopped_sessions = set()
    agent._session_manager = SimpleNamespace(get_request_session=lambda _base: None)
    agent._abort_active_request = AsyncMock()
    agent.sessions = SimpleNamespace(remove_active_poll=Mock())
    agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())
    return agent


def _request(session_id="session-1"):
    return SimpleNamespace(
        base_session_id=session_id,
        working_path="/tmp",
        context=SimpleNamespace(platform_specific={}),
        output=None,
    )


class OpenCodeStopIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_sees_the_user_stop_intent(self):
        agent = _agent()
        observed: list[bool] = []
        entered = asyncio.Event()

        async def in_flight():
            try:
                entered.set()
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # This is the branch that decides between a plain removal and
                # the receipt; it must be able to see the intent from here.
                observed.append("session-1" in agent._user_stopped_sessions)
                raise

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        await entered.wait()

        result = await agent.handle_stop(_request())

        self.assertTrue(result)
        self.assertEqual(observed, [True])
        # Cleared afterwards: a later shutdown of the same session is not a stop.
        self.assertEqual(agent._user_stopped_sessions, set())

    async def test_intent_is_released_even_when_the_abort_fails(self):
        agent = _agent()
        agent._abort_active_request = AsyncMock(side_effect=RuntimeError("server gone"))

        async def in_flight():
            await asyncio.sleep(3600)

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        await asyncio.sleep(0)

        result = await agent.handle_stop(_request())

        self.assertTrue(result)
        self.assertEqual(agent._user_stopped_sessions, set())

    async def test_stop_of_an_idle_session_publishes_no_intent(self):
        agent = _agent()

        result = await agent.handle_stop(_request())

        self.assertFalse(result)
        self.assertEqual(agent._user_stopped_sessions, set())


class OpenCodeStopReceiptTests(unittest.TestCase):
    """The intent is only useful if the cancellation branch actually spends it.

    ``_process_message`` is a several-hundred-line coroutine whose cancellation
    branch sits behind server startup, session creation and the poll loop, so
    driving it here would mostly be a test of the stubs. Pin the coupling
    instead: the branch must read the intent set and ask for the receipt.
    """

    def test_cancellation_branch_reads_the_intent(self):
        source = inspect.getsource(OpenCodeAgent._process_message)
        _, _, after = source.partition("except asyncio.CancelledError:")
        branch = after.partition("raise")[0]

        self.assertTrue(branch, "_process_message no longer handles cancellation")
        self.assertIn("_user_stopped_sessions", branch)
        self.assertIn("STOPPED_REACTION_EMOJI", branch)


if __name__ == "__main__":
    unittest.main()
