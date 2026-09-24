"""Every backend's acknowledged stop must settle its Run as ``canceled``.

A stop is answered by a synthetic empty ``result``: it ends the turn so the dot goes
idle and the SSE waiter closes, but nobody produced an answer — the user called the
work off. Emitting it with the terminal-turn default made the dispatcher record that
empty body as the run's ``succeeded`` terminal, and because that write lands before
the stop's own guarded write, first-writer-wins reported user-ended runs as
successes (``docs/plans/agent-run-zombie-settlement.md`` §5.10).

These are cross-backend on purpose. The bug was identical in Codex, Claude, and
OpenCode because all three copied the same emit, so the guard has to be the kind a
fourth backend cannot miss.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_output import MessageOutput, stop_output_for
from core.run_settlement import SETTLED_BY_STOPPED, SETTLEMENT_TERMINAL_STATUS
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.agent import OpenCodeAgent


class _StubTurnRegistry:
    def __init__(self) -> None:
        self._active_turns: dict[str, str] = {}

    def get_active_turn(self, base_session_id):
        return self._active_turns.get(base_session_id)

    def hide_turn(self, *_args, **_kwargs):
        return None


class AgentStopSettlementTests(unittest.IsolatedAsyncioTestCase):
    def test_stop_output_ends_the_turn_without_owning_the_run(self):
        """HFR-038: the shared stop policy, stated once.

        ``completes_turn`` is what settles the dot and releases the waiter;
        ``completes_run=False`` keeps an empty body out of the run's terminal state;
        ``settled_by`` is what still lets the settlement lanes reach the ``canceled``
        writer instead of reading "does not settle the run" as the Activity case.
        """
        semantics = stop_output_for(None)

        self.assertTrue(semantics.completes_turn)
        self.assertFalse(semantics.settles_run)
        self.assertEqual(semantics.settled_by, SETTLED_BY_STOPPED)
        self.assertEqual(SETTLEMENT_TERMINAL_STATUS[SETTLED_BY_STOPPED], "canceled")

    def test_stop_output_keeps_a_requests_own_output_policy(self):
        """HFR-038 (other half): only the lifecycle is overridden.

        A request carrying Activity lineage or an explicit ``run_id`` must keep it, or
        the settlement lands on the wrong row.
        """
        request = SimpleNamespace(
            output=MessageOutput(
                completes_turn=True,
                completes_run=True,
                run_id="run-7",
                activity_id="act-3",
            )
        )

        semantics = stop_output_for(request)

        self.assertEqual(semantics.run_id, "run-7")
        self.assertEqual(semantics.activity_id, "act-3")
        self.assertFalse(semantics.settles_run)
        self.assertEqual(semantics.settled_by, SETTLED_BY_STOPPED)

    async def test_every_backend_stop_emits_stop_semantics(self):
        """HFR-039/HFR-040: each backend's live stop path, not just the helper.

        The defect was copied verbatim into three backends, so pinning one of them
        would leave the other two free to regress. Each case drives the real
        ``handle_stop`` to its silent ``result`` and reads the output it emitted: a
        backend that reaches for ``terminal_output_for`` in its stop path fails here
        instead of silently reporting stopped runs as successes.
        """
        for name, build in (
            ("codex", _codex_stop_case),
            ("claude", _claude_stop_case),
            ("opencode", _opencode_stop_case),
        ):
            with self.subTest(backend=name):
                agent, request, emit = build()

                self.assertTrue(await agent.handle_stop(request))

                results = [
                    call for call in emit.await_args_list if call.args[1] == "result"
                ]
                self.assertEqual(len(results), 1, "a stop must answer with one terminal result")
                semantics = results[0].kwargs["output"]
                self.assertFalse(semantics.settles_run)
                self.assertEqual(semantics.settled_by, SETTLED_BY_STOPPED)


def _codex_stop_case():
    agent = object.__new__(CodexAgent)
    agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
    agent._turn_registry = _StubTurnRegistry()
    agent._turn_registry._active_turns["session-1"] = "turn-1"
    agent._transports = {
        "/tmp": SimpleNamespace(is_alive=True, send_request=AsyncMock(return_value={}))
    }
    agent._event_handler = SimpleNamespace(clear_pending=lambda turn_id: SimpleNamespace())
    agent._user_stopped_turn_ids = set()
    agent._remove_ack_reaction = AsyncMock()
    emit = AsyncMock()
    agent.controller = SimpleNamespace(emit_agent_message=emit)
    request = SimpleNamespace(base_session_id="session-1", working_path="/tmp", context=object())
    return agent, request, emit


def _claude_stop_case():
    composite_key = "session-1:/tmp"
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {composite_key: SimpleNamespace(interrupt=AsyncMock())}
    agent._pending_requests = {}
    agent._suppress_receiver_runtime_release = set()
    agent._cleanup_runtime_session = AsyncMock()
    agent._mark_session_idle_if_no_pending_requests = lambda _key: True
    emit = AsyncMock()
    agent.controller = SimpleNamespace(emit_agent_message=emit)
    request = SimpleNamespace(
        base_session_id="session-1",
        composite_session_id=composite_key,
        working_path="/tmp",
        context=SimpleNamespace(platform_specific={}),
        output=None,
        stop_failure_reason=None,
    )
    return agent, request, emit


def _opencode_stop_case():
    agent = object.__new__(OpenCodeAgent)
    lock = asyncio.Lock()
    agent._session_manager = SimpleNamespace(
        get_session_lock=lambda _base: lock,
        get_request_session=lambda _base: None,
    )
    agent._user_stopped_sessions = set()

    async def _in_flight():
        await asyncio.Event().wait()

    task = asyncio.get_running_loop().create_task(_in_flight())
    agent._active_requests = {"session-1": task}

    async def _abort(_base, task, _request_session, *, cancel_before_abort=False):
        task.cancel()
        return True

    agent._abort_active_request = _abort
    emit = AsyncMock()
    agent.controller = SimpleNamespace(emit_agent_message=emit)
    request = SimpleNamespace(
        base_session_id="session-1",
        working_path="/tmp",
        context=SimpleNamespace(platform_specific={}),
        output=None,
    )
    return agent, request, emit


if __name__ == "__main__":
    unittest.main()
