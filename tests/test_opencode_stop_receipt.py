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
from modules.agents.opencode.poll_loop import OpenCodePollLoop, restored_request_from_poll_info


def _agent():
    agent = object.__new__(OpenCodeAgent)
    agent._active_requests = {}
    agent._active_ack_requests = {}
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

    async def test_task_that_settles_during_the_abort_still_gets_the_receipt(self):
        """The abort can end the poll loop before ``task.cancel()`` is reached.

        Then the cancellation handler — the only other receipt-stamping path —
        never runs, ``cancel()`` is a no-op on a finished task, and the silent
        stop result that follows would leave the turn with no trace at all.
        """
        agent = _agent()
        receipts: list[tuple[object, object]] = []
        agent._remove_ack_reaction = AsyncMock(
            side_effect=lambda request, *, terminal_emoji=None: receipts.append(
                (request, terminal_emoji)
            )
        )

        async def in_flight():
            await asyncio.sleep(3600)

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        turn_request = _request()
        agent._active_ack_requests["session-1"] = turn_request
        await asyncio.sleep(0)

        async def abort(*_args, **_kwargs):
            # Whatever the abort does upstream, the effect here is a task that is
            # already finished by the time control comes back.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        agent._abort_active_request = abort

        stop_request = _request()
        self.assertTrue(await agent.handle_stop(stop_request))

        # The receipt belongs on the TURN's request. ``stop_request`` describes
        # the /stop message, so stamping that one would put the ⏹️ on the command
        # and leave the real 👀 in place.
        self.assertEqual(receipts, [(turn_request, STOPPED_REACTION_EMOJI)])
        self.assertIsNot(receipts[0][0], stop_request)
        self.assertEqual(agent._user_stopped_sessions, set())

    async def test_receipt_survives_handle_messages_cleanup_racing_the_abort(self):
        """The waiter that owns the turn can finish its ``finally`` mid-abort.

        ``handle_message`` is blocked on ``await task``; a task settled by the
        abort wakes it, and it pops both registries before ``handle_stop``
        resumes. Looking the request up after the abort finds nothing, so the
        receipt has to be captured up front.
        """
        agent = _agent()
        receipts: list[tuple[object, object]] = []
        agent._remove_ack_reaction = AsyncMock(
            side_effect=lambda request, *, terminal_emoji=None: receipts.append(
                (request, terminal_emoji)
            )
        )

        async def in_flight():
            await asyncio.sleep(3600)

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        turn_request = _request()
        agent._active_ack_requests["session-1"] = turn_request
        await asyncio.sleep(0)

        async def abort(*_args, **_kwargs):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Exactly what handle_message's finally does once its await returns.
            if agent._active_requests.get("session-1") is task:
                agent._active_requests.pop("session-1", None)
                agent._active_ack_requests.pop("session-1", None)

        agent._abort_active_request = abort

        self.assertTrue(await agent.handle_stop(_request()))

        self.assertEqual(receipts, [(turn_request, STOPPED_REACTION_EMOJI)])
        self.assertEqual(agent._active_ack_requests, {})

    async def test_no_receipt_when_the_turns_request_was_not_retained(self):
        # Better no receipt than one stamped on the wrong message: a turn with
        # nothing retained (restored poll already cleaned up) just ends silently.
        agent = _agent()
        receipts: list[object] = []
        agent._remove_ack_reaction = AsyncMock(
            side_effect=lambda request, *, terminal_emoji=None: receipts.append(terminal_emoji)
        )

        async def in_flight():
            await asyncio.sleep(3600)

        task = asyncio.create_task(in_flight())
        agent._active_requests["session-1"] = task
        await asyncio.sleep(0)

        async def abort(*_args, **_kwargs):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        agent._abort_active_request = abort

        self.assertTrue(await agent.handle_stop(_request()))

        self.assertEqual(receipts, [])

    async def test_the_intent_is_claimed_once(self):
        # Both paths call consume; the second must see nothing, or a turn whose
        # coroutine already stamped would get a second ⏹️ from handle_stop.
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
            _active_ack_requests={},
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
        restored_request = restored_request_from_poll_info(agent, poll)
        agent._active_ack_requests[poll.base_session_id] = restored_request

        task = asyncio.create_task(OpenCodePollLoop(agent).run_restored_poll_loop(poll))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        self.assertEqual(cleanups, [(restored_request, STOPPED_REACTION_EMOJI)])
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
