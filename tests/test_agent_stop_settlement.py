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

import ast
import inspect
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


def _emitted_message_type(call: ast.Call) -> str | None:
    """The literal message type an ``emit_agent_message`` call passes, if any."""

    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    for keyword in call.keywords:
        if keyword.arg == "message_type" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


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

    async def test_codex_stop_emits_stop_semantics(self):
        """HFR-039: the live path, not just the helper — Codex as the worked example."""
        agent = object.__new__(CodexAgent)
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
        agent._transports = {
            "/tmp": SimpleNamespace(is_alive=True, send_request=AsyncMock(return_value={}))
        }
        agent._event_handler = SimpleNamespace(clear_pending=lambda turn_id: SimpleNamespace())
        agent._remove_ack_reaction = AsyncMock()
        emit = AsyncMock()
        agent.controller = SimpleNamespace(emit_agent_message=emit)
        request = SimpleNamespace(base_session_id="session-1", working_path="/tmp", context=object())

        self.assertTrue(await agent.handle_stop(request))

        semantics = emit.await_args.kwargs["output"]
        self.assertFalse(semantics.settles_run)
        self.assertEqual(semantics.settled_by, SETTLED_BY_STOPPED)

    def test_no_backend_stop_uses_the_terminal_turn_default(self):
        """HFR-040: the guard a fourth backend cannot miss.

        The defect was copied verbatim into three backends, so pinning one behavior
        test would leave the other two free to regress. Asserting on the emit *in
        every* ``handle_stop`` is what makes the shared policy enforceable: a new
        backend that reaches for ``terminal_output_for`` in its stop path fails here
        instead of silently reporting stopped runs as successes.
        """
        for agent_cls in (CodexAgent, ClaudeAgent, OpenCodeAgent):
            with self.subTest(agent=agent_cls.__name__):
                tree = ast.parse(inspect.getsource(agent_cls.handle_stop).lstrip())
                emits = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "emit_agent_message"
                    # Only the terminal ``result`` carries settlement authority. A
                    # stop path may also emit ``notify`` (Claude's "cannot be
                    # interrupted" / "interrupt failed" refusals), which settles
                    # nothing and returns False without ending the turn.
                    and _emitted_message_type(node) == "result"
                ]
                self.assertTrue(emits, "a stop must answer with a terminal result")
                for call in emits:
                    output = next(
                        (kw.value for kw in call.keywords if kw.arg == "output"), None
                    )
                    self.assertIsNotNone(output, "the stop emit must state its semantics")
                    self.assertIsInstance(output, ast.Call)
                    self.assertEqual(
                        getattr(output.func, "id", None),
                        "stop_output_for",
                        "a stop's empty result must not claim the run's terminal state",
                    )


if __name__ == "__main__":
    unittest.main()
