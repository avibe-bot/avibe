"""Turn-pipeline wiring for the Harness prompt echo (MESSAGE-DELIVERY-018).

The dispatcher decides WHETHER to echo; these tests pin down WHERE the turn
pipeline asks it to, because the ordering is the whole feature: after every
``suppress_delivery`` resolution, before dispatch, and never for a human turn
whose prompt is already the IM message on screen.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    handler._prepend_message_metadata = AsyncMock(side_effect=lambda context, message, **_kw: message)
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


class MessageHandlerHarnessEchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_turn_echoes_the_raw_prompt_before_dispatch(self):
        """Scenario: MESSAGE-DELIVERY-018"""
        controller, handler = _build_handler()
        order = []

        async def _echo(context, text):
            order.append(("echo", text))
            return "echo-1"

        original_dispatch = controller.agent_service.handle_message

        async def _dispatch(agent_name, request):
            order.append(("dispatch", request.message))
            return await original_dispatch(agent_name, request)

        controller.message_dispatcher.emit_harness_prompt = _echo
        controller.agent_service.handle_message = _dispatch

        await handler.handle_scheduled_message(_scheduled_context(), "summarize open PRs")

        # The channel sees the question first, and it is the RAW prompt: the echo runs
        # before ``_prepend_message_metadata`` decorates the text sent to the backend.
        self.assertEqual(order, [("echo", "summarize open PRs"), ("dispatch", "summarize open PRs")])
        self.assertEqual(len(controller.agent_service.requests), 1)

    async def test_subagent_prefixed_prompt_is_echoed_unstripped(self):
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

        emit = controller.message_dispatcher.emit_harness_prompt
        emit.assert_awaited_once()
        self.assertEqual(emit.await_args.args[1], "echo-probe: audit the queue")
        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(request.subagent_name, "echo-probe")
        self.assertEqual(request.message, "audit the queue")

    async def test_human_turn_never_echoes(self):
        controller, handler = _build_handler()

        await handler.handle_user_message(
            MessageContext(user_id="U1", channel_id="C1", message_id="m1", platform="slack"),
            "hello",
        )

        controller.message_dispatcher.emit_harness_prompt.assert_not_awaited()

    async def test_backgrounded_thread_resolution_is_visible_to_the_echo(self):
        """A session backgrounded via CLI must silence the echo too.

        ``suppress_delivery`` for an IM thread is resolved inside the turn pipeline
        from the thread's own session row, not by the caller that built the context,
        so an echo asked for earlier would announce a background run in the channel.
        """
        controller, handler = _build_handler()
        controller.sessions = types.SimpleNamespace(
            find_session_for_anchor=lambda session_key, base_session_id: {"visibility": "background"}
        )
        handler.sessions = controller.sessions
        controller.resolve_vibe_agent_for_context = Mock(return_value=None)

        await handler.handle_scheduled_message(_scheduled_context(), "investigate the flake")

        emit = controller.message_dispatcher.emit_harness_prompt
        emit.assert_awaited_once()
        echoed_context = emit.await_args.args[0]
        self.assertTrue((echoed_context.platform_specific or {}).get("suppress_delivery"))

    async def test_dispatcher_without_the_echo_hook_still_runs_the_turn(self):
        controller, handler = _build_handler()
        controller.message_dispatcher = types.SimpleNamespace()

        await handler.handle_scheduled_message(_scheduled_context(), "do the thing")

        self.assertEqual(len(controller.agent_service.requests), 1)

    async def test_failed_echo_never_blocks_the_turn(self):
        controller, handler = _build_handler()
        controller.message_dispatcher.emit_harness_prompt = AsyncMock(side_effect=RuntimeError("send failed"))

        await handler.handle_scheduled_message(_scheduled_context(), "do the thing")

        self.assertEqual(len(controller.agent_service.requests), 1)


if __name__ == "__main__":
    unittest.main()
