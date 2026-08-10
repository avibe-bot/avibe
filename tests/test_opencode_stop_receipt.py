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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_sessions import ActivePollInfo
from core.processing_indicator import STOPPED_REACTION_EMOJI, ProcessingIndicatorService
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.poll_loop import OpenCodePollLoop


def _agent():
    agent = object.__new__(OpenCodeAgent)
    agent._active_requests = {}
    agent._user_stopped_sessions = set()
    session_lock = asyncio.Lock()
    agent._session_manager = SimpleNamespace(
        get_request_session=lambda _base: None,
        get_session_lock=lambda _base: session_lock,
    )

    async def _abort(_base, task, _request_session, *, cancel_before_abort=False):
        if cancel_before_abort and not task.done():
            task.cancel()
        return True

    agent._abort_active_request = AsyncMock(side_effect=_abort)
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
    async def test_native_abort_settles_before_request_releases_turn_ownership(self):
        agent = _agent()
        request = _request()
        request_started = asyncio.Event()
        abort_started = asyncio.Event()
        release_abort = asyncio.Event()
        agent.controller.mark_turn_complete = Mock()

        async def in_flight(_request):
            request_started.set()
            await asyncio.Event().wait()

        async def abort(_base, task, _request_session, *, cancel_before_abort=False):
            self.assertTrue(cancel_before_abort)
            task.cancel()
            abort_started.set()
            await release_abort.wait()
            return True

        agent._process_message = in_flight
        agent._abort_active_request = abort
        agent._session_manager.pop_request_session = Mock()

        message_task = asyncio.create_task(agent.handle_message(request))
        await request_started.wait()
        stop_task = asyncio.create_task(agent.handle_stop(request))
        await abort_started.wait()
        await asyncio.sleep(0)

        self.assertFalse(message_task.done())
        agent.controller.mark_turn_complete.assert_not_called()

        release_abort.set()
        self.assertTrue(await stop_task)
        await message_task

        agent.controller.mark_turn_complete.assert_called_once_with(request.context)

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

        async def in_flight():
            await asyncio.sleep(3600)

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        await asyncio.sleep(0)

        async def abort_then_fail(*_args, cancel_before_abort=False, **_kwargs):
            self.assertTrue(cancel_before_abort)
            task.cancel()
            raise RuntimeError("server gone")

        agent._abort_active_request = AsyncMock(side_effect=abort_then_fail)

        result = await agent.handle_stop(_request())

        self.assertTrue(result)
        self.assertEqual(agent._user_stopped_sessions, set())

    async def test_stop_cancels_before_native_abort_can_release_normal_completion(self):
        """Once stop accepts an active task, normal completion cannot win."""

        agent = _agent()
        release_normal_completion = asyncio.Event()
        outcomes: list[str] = []
        abort_observed_cancelling: list[bool] = []

        async def in_flight():
            try:
                await release_normal_completion.wait()
                outcomes.append("result")
            except asyncio.CancelledError:
                outcomes.append("stopped")
                self.assertTrue(agent.consume_user_stop_intent("session-1"))
                raise

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        await asyncio.sleep(0)

        async def abort(*_args, cancel_before_abort=False, **_kwargs):
            self.assertTrue(cancel_before_abort)
            task.cancel()
            abort_observed_cancelling.append(task.cancelling() > 0)
            release_normal_completion.set()
            await asyncio.sleep(0)
            return True

        agent._abort_active_request = abort

        self.assertTrue(await agent.handle_stop(_request()))

        self.assertEqual(abort_observed_cancelling, [True])
        self.assertEqual(outcomes, ["stopped"])
        self.assertEqual(agent._user_stopped_sessions, set())

    async def test_result_that_wins_while_stop_waits_for_steering_is_authoritative(self):
        agent = _agent()
        release_result = asyncio.Event()
        outcomes: list[str] = []

        async def in_flight():
            await release_result.wait()
            outcomes.append("result")

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        await asyncio.sleep(0)

        async def abort(*_args, cancel_before_abort=False, **_kwargs):
            self.assertTrue(cancel_before_abort)
            release_result.set()
            await task
            return False

        agent._abort_active_request = abort

        self.assertTrue(await agent.handle_stop(_request()))

        self.assertEqual(outcomes, ["result"])
        agent.controller.emit_agent_message.assert_not_awaited()
        self.assertEqual(agent._user_stopped_sessions, set())

    async def test_the_intent_is_claimed_once(self):
        # The request coroutine may encounter nested cancellation cleanup, but
        # only its first claim may stamp the terminal receipt.
        agent = _agent()
        agent._user_stopped_sessions.add("session-1")

        self.assertTrue(agent.consume_user_stop_intent("session-1"))
        self.assertFalse(agent.consume_user_stop_intent("session-1"))
        self.assertFalse(agent.consume_user_stop_intent(""))

    async def test_stop_of_an_idle_session_publishes_no_intent(self):
        agent = _agent()

        result = await agent.handle_stop(_request())

        self.assertFalse(result)
        self.assertEqual(agent._user_stopped_sessions, set())

    async def test_restored_poll_cancellation_stamps_its_retained_request(self):
        entered = asyncio.Event()
        cleanups: list[tuple[object, object]] = []

        class _Controller:
            def __init__(self):
                self.config = SimpleNamespace(
                    platform="slack",
                    ack_mode="reaction",
                    language="en",
                )
                self.processing_indicator = ProcessingIndicatorService(self)

            async def emit_agent_message(self, *_args, **_kwargs):
                return None

        class _Server:
            async def list_messages(self, **_kwargs):
                entered.set()
                await asyncio.Event().wait()

        agent = SimpleNamespace(
            controller=_Controller(),
            opencode_config=SimpleNamespace(active_turn_timeout_seconds=60),
            sessions=SimpleNamespace(remove_active_poll=Mock()),
            _user_stopped_sessions={"session-1"},
        )

        async def _get_server():
            return _Server()

        async def _remove_ack_reaction(request, *, terminal_emoji=None):
            cleanups.append((request, terminal_emoji))

        def _consume(session_id):
            return OpenCodeAgent.consume_user_stop_intent(agent, session_id)

        agent._get_server = _get_server
        agent._remove_ack_reaction = _remove_ack_reaction
        agent.consume_user_stop_intent = _consume
        poll = ActivePollInfo(
            opencode_session_id="oc-1",
            base_session_id="session-1",
            channel_id="c1",
            thread_id="t1",
            settings_key="c1",
            working_path="/tmp/work",
            processing_indicator={
                "platform": "slack",
                "user_id": "u1",
                "channel_id": "c1",
                "message_id": "m1",
            },
        )
        task = asyncio.create_task(OpenCodePollLoop(agent).run_restored_poll_loop(poll))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        self.assertEqual(len(cleanups), 1)
        self.assertEqual(cleanups[0][0].base_session_id, poll.base_session_id)
        self.assertEqual(cleanups[0][0].terminal_reaction_message_id, "m1")
        self.assertEqual(cleanups[0][1], STOPPED_REACTION_EMOJI)
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
        self.assertIn("consume_user_stop_intent", branch)
        self.assertIn("STOPPED_REACTION_EMOJI", branch)

    def test_restored_cancellation_branch_reads_the_intent(self):
        source = inspect.getsource(OpenCodePollLoop.run_restored_poll_loop)
        _, _, after = source.partition("except asyncio.CancelledError:")
        branch = after.partition("raise")[0]

        self.assertTrue(branch, "run_restored_poll_loop no longer handles cancellation")
        self.assertIn("consume_user_stop_intent", branch)
        self.assertIn("STOPPED_REACTION_EMOJI", branch)
        self.assertIn("restored_request", branch)


if __name__ == "__main__":
    unittest.main()
