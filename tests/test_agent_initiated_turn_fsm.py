"""Agent-initiated turns are first-class FSM citizens (#688).

``SessionTurnManager.register_agent_initiated_turn`` registers a backend-started
turn (a Claude background-task completion / ScheduleWakeup reply) in ``in_flight``
+ a no-op sink + ``turn.start``, held open by a small task until the terminal
result's ``done_event`` (or a Stop). This makes the Workbench Stop button work
(``cancel`` interrupts the backend via ``handle_stop`` and cancels the holder) and
keeps the browser turn lifecycle consistent — without a query-sending dispatch
task, since the backend already started.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import session_turns


def _ctx(session_id: str = "s1"):
    return SimpleNamespace(
        user_id="U1",
        channel_id="C1",
        platform="avibe",
        platform_specific={"agent_session_id": session_id, "turn_token": "T-ai"},
    )


def _manager():
    controller = SimpleNamespace(
        _session_id_from_context=lambda ctx: (getattr(ctx, "platform_specific", None) or {}).get("agent_session_id"),
        _get_session_key=lambda ctx: f"avibe::{(getattr(ctx, 'platform_specific', None) or {}).get('agent_session_id')}",
        set_agent_status=lambda sid, status: None,
        command_handler=SimpleNamespace(handle_stop=AsyncMock(return_value=True)),
    )
    mgr = session_turns.SessionTurnManager(controller=controller)
    # The holder flushes the send-while-busy queue on natural completion; stub it so
    # the test doesn't reach the DB.
    mgr.flush_queue = AsyncMock(return_value=False)
    return mgr, controller


async def _settle(times: int = 3) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


class RegisterAgentInitiatedTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_in_flight_then_terminal_result_settles(self):
        mgr, _ = _manager()
        ctx = _ctx("s1")
        events: list[tuple[str, str]] = []
        with patch(
            "core.inbox_events.bus.publish",
            side_effect=lambda topic, payload: events.append((topic, (payload or {}).get("session_id"))),
        ):
            ok = mgr.register_agent_initiated_turn(ctx)
            self.assertTrue(ok)
            # Stop target + sink exist synchronously (before any further receiver emit).
            self.assertIn("s1", mgr.in_flight)
            sink = mgr.get_turn_sink("avibe::s1")
            self.assertIsNotNone(sink)
            self.assertIn(("turn.start", "s1"), events)

            # The terminal result sets the sink's done_event → the holder settles.
            sink["done_event"].set()
            await _settle()

            self.assertNotIn("s1", mgr.in_flight)
            self.assertIsNone(mgr.get_turn_sink("avibe::s1"))
            self.assertIn(("turn.end", "s1"), events)
        # Natural completion flushes the send-while-busy queue (mirrors _run).
        mgr.flush_queue.assert_awaited()

    async def test_cancel_interrupts_backend_and_settles_without_flush(self):
        mgr, controller = _manager()
        ctx = _ctx("s2")
        with patch("core.inbox_events.bus.publish"):
            self.assertTrue(mgr.register_agent_initiated_turn(ctx))
            self.assertIn("s2", mgr.in_flight)
            holder = mgr.in_flight["s2"].task
            await _settle()  # let the holder park in done.wait() (as in production)

            result = await mgr.cancel("s2")
            await asyncio.gather(holder, return_exceptions=True)  # drive the holder's finally

            # Stop interrupted the backend via the shared /stop path, and the turn settled.
            controller.command_handler.handle_stop.assert_awaited()
            self.assertTrue(result.get("ok"))
            self.assertNotIn("s2", mgr.in_flight)
            # A plain Stop keeps the queue (no flush on cancellation).
            mgr.flush_queue.assert_not_awaited()

    async def test_noop_without_workbench_session_id(self):
        mgr, _ = _manager()
        ctx = SimpleNamespace(platform="slack", channel_id="C", platform_specific={})  # no agent_session_id
        with patch("core.inbox_events.bus.publish"):
            ok = mgr.register_agent_initiated_turn(ctx)
        self.assertFalse(ok)
        self.assertEqual(mgr.in_flight, {})

    async def test_noop_when_a_turn_already_streams(self):
        mgr, _ = _manager()
        ctx = _ctx("s3")
        # A streaming turn already owns this session's sink.
        mgr.register_turn_sink("avibe::s3", on_chunk=AsyncMock(), done_event=asyncio.Event())
        with patch("core.inbox_events.bus.publish"):
            ok = mgr.register_agent_initiated_turn(ctx)
        self.assertFalse(ok)
        self.assertNotIn("s3", mgr.in_flight)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
