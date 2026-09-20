"""Turn-pipeline wiring for the Harness prompt echo (MESSAGE-DELIVERY-018).

The dispatcher decides WHETHER to echo and ``AgentService`` decides WHEN (at the
real turn start, once the runtime gate is held). These tests pin down WHAT the turn
pipeline stages: the stored prompt, past every ``suppress_delivery`` resolution, and
never for a human turn whose prompt is already the IM message on screen.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.message_output import HARNESS_PROMPT_ECHO_SPEC_KEY
from modules.im import MessageContext

# Reuses the isolated module loader (and controller/session stubs) from the
# typing test rather than a second copy of that 80-line import dance.
from tests.test_message_handler_typing import (
    MessageHandler,
    _StubController,
    _StubSessionHandler,
)


def _build_handler():
    controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
    controller.session_turns = types.SimpleNamespace(deliver=AsyncMock())
    controller.message_dispatcher = types.SimpleNamespace(emit_harness_prompt=AsyncMock(return_value="echo-1"))
    handler = MessageHandler(controller)
    handler.set_session_handler(_StubSessionHandler())
    handler._admit_human_delivery = AsyncMock(return_value=False)
    handler._is_duplicate_human_delivery = Mock(return_value=False)
    return controller, handler


def _scheduled_context(**spec):
    payload = {
        "task_trigger_kind": "scheduled",
        "task_execution_id": "run-echo",
        "task_definition_id": "task-echo",
    }
    payload.update(spec)
    return MessageContext(
        user_id="scheduled",
        channel_id="C1",
        message_id="scheduled:run-echo",
        platform="slack",
        platform_specific=payload,
    )


def _staged_prompt(controller):
    _agent_name, request = controller.agent_service.requests[0]
    return (request.context.platform_specific or {}).get(HARNESS_PROMPT_ECHO_SPEC_KEY)


class MessageHandlerHarnessEchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_turn_stages_the_raw_prompt_for_turn_start(self):
        """Scenario: MESSAGE-DELIVERY-018"""
        controller, handler = _build_handler()

        await handler.handle_scheduled_message(_scheduled_context(), "summarize open PRs")

        # The staged text is the raw prompt, independent of execution metadata.
        self.assertEqual(_staged_prompt(controller), "summarize open PRs")
        # And nothing is posted from here: the send waits for the runtime gate in
        # ``AgentService._begin_turn_status``, so a queued turn stays quiet.
        controller.message_dispatcher.emit_harness_prompt.assert_not_awaited()

    async def test_scheduled_input_carries_provenance_separately_from_text(self):
        controller, handler = _build_handler()
        context = _scheduled_context(source_session_id="source")

        await handler.handle_scheduled_message(context, "work")

        _agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(request.message, "work")
        self.assertEqual(request.input_metadata.source_session_id, "source")
        self.assertIsNone(request.input_metadata.user_id)

    async def test_subagent_prefixed_prompt_is_staged_unstripped(self):
        """Scenario: MESSAGE-DELIVERY-018

        Subagent routing rewrites ``message`` to the prefix-stripped body before
        dispatch. The echo must still show the stored prompt — the prefix names which
        subagent was asked, and dropping it makes the visible trigger differ from the
        Workbench row (Codex P2).
        """
        controller, handler = _build_handler()
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            agents_dir = project / ".codex" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "echo-probe.toml").write_text(
                "\n".join(
                    [
                        'name = "echo-probe"',
                        'description = "probe agent"',
                        'developer_instructions = "probe"',
                    ]
                ),
                encoding="utf-8",
            )
            handler.session_handler.get_session_info = (
                lambda context, source="human": ("base-session", str(project), f"base-session:{project}")
            )

            await handler.handle_scheduled_message(
                _scheduled_context(), "echo-probe: audit the queue"
            )

        self.assertEqual(_staged_prompt(controller), "echo-probe: audit the queue")
        _agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(request.subagent_name, "echo-probe")
        self.assertEqual(request.message, "audit the queue")

    async def test_scheduled_subagent_prefix_routes_before_hidden_provenance(self):
        controller, handler = _build_handler()
        context = _scheduled_context(
            source_kind="agent",
            source_actor="source-session",
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            agents_dir = project / ".codex" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "echo-probe.toml").write_text(
                "\n".join(
                    [
                        'name = "echo-probe"',
                        'description = "probe agent"',
                        'developer_instructions = "probe"',
                    ]
                ),
                encoding="utf-8",
            )
            handler.session_handler.get_session_info = (
                lambda context, source="human": (
                    "base-session",
                    str(project),
                    f"base-session:{project}",
                )
            )

            await handler.handle_scheduled_message(context, "echo-probe: audit the queue")

        _agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(request.subagent_name, "echo-probe")
        self.assertEqual(
            request.message,
            "audit the queue",
        )
        self.assertEqual(request.input_metadata.source_session_id, "source-session")

    async def test_human_turn_never_stages_a_prompt(self):
        controller, handler = _build_handler()

        await handler.handle_user_message(
            MessageContext(user_id="U1", channel_id="C1", message_id="m1", platform="slack"),
            "hello",
        )

        self.assertIsNone(_staged_prompt(controller))
        controller.message_dispatcher.emit_harness_prompt.assert_not_awaited()

    async def test_backgrounded_thread_resolution_is_visible_to_the_echo(self):
        """A session backgrounded via CLI must silence the echo too.

        ``suppress_delivery`` for an IM thread is resolved inside the turn pipeline
        from the thread's own session row, not by the caller that built the context,
        so staging earlier would hand the dispatcher a context that still looks
        deliverable.
        """
        controller, handler = _build_handler()
        controller.sessions = types.SimpleNamespace(
            find_session_for_anchor=lambda session_key, base_session_id: {"visibility": "background"}
        )
        handler.sessions = controller.sessions
        controller.resolve_vibe_agent_for_context = Mock(return_value=None)

        await handler.handle_scheduled_message(_scheduled_context(), "investigate the flake")

        _agent_name, request = controller.agent_service.requests[0]
        spec = request.context.platform_specific or {}
        self.assertEqual(spec.get(HARNESS_PROMPT_ECHO_SPEC_KEY), "investigate the flake")
        self.assertTrue(spec.get("suppress_delivery"))

    async def test_blank_prompt_stages_nothing(self):
        # A whitespace-only prompt never dispatches a turn, and it must not leave a
        # staged key behind for a later turn on the same context either.
        _controller, handler = _build_handler()
        context = _scheduled_context()

        handler._stage_harness_prompt_echo(context, "   ")

        self.assertNotIn(HARNESS_PROMPT_ECHO_SPEC_KEY, context.platform_specific or {})


if __name__ == "__main__":
    unittest.main()
