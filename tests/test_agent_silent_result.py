from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.base import AgentRequest, BaseAgent
from modules.im import MessageContext
from modules.im.formatters.slack_formatter import SlackFormatter


class _StubController:
    def __init__(self, token_field: str = ""):
        self.config = SimpleNamespace(show_duration=True)
        self.im_client = SimpleNamespace(formatter=SlackFormatter())
        self.settings_manager = SimpleNamespace(sessions=None)
        self.messages = []
        self.result_footers = []
        self._token_field = token_field

    def session_token_field(self, context):
        return self._token_field

    async def emit_agent_message(
        self, context, message_type, text, parse_mode="markdown", *, is_error=False, level="normal", result_footer=None
    ):
        self.messages.append((message_type, text, parse_mode))
        self.result_footers.append(result_footer)


class _StubAgent(BaseAgent):
    name = "stub"

    async def handle_message(self, request: AgentRequest) -> None:
        return None


class AgentSilentResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_only_result_suppresses_duration_wrapper(self):
        controller = _StubController()
        agent = _StubAgent(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")

        await agent.emit_result_message(
            context,
            "<silent>not relevant</silent>",
            subtype="success",
            duration_ms=1234,
        )

        self.assertEqual(controller.messages, [("result", "", "markdown")])

    async def test_no_visible_result_with_duration_hidden_settles_via_outbound(self):
        # show_duration off + empty result/suffix is still a TERMINAL turn: it is
        # settled through the OUTBOUND status chokepoint — an empty terminal result
        # emit (→ dot idle/failed + releases the web-Chat stream) instead of being
        # left to hang to the 600s timeout (Codex P2).
        controller = _StubController()
        controller.config = SimpleNamespace(show_duration=False)
        agent = _StubAgent(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="avibe")

        await agent.emit_result_message(context, "", subtype="success", duration_ms=0)

        # An empty terminal result is emitted (no visible text); the dispatcher's
        # result path settles the dot + releases the stream.
        self.assertEqual(controller.messages, [("result", "", "markdown")])

    async def test_footer_only_result_is_promoted_to_visible_body(self):
        # show_duration on + no visible result/suffix: the duration/token footnote
        # is promoted to the visible body (and NOT sent as a separate footer), so a
        # timing-only completion is still delivered/persisted instead of going
        # silent (Codex P2).
        controller = _StubController(token_field="1.2k tok")
        agent = _StubAgent(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")

        await agent.emit_result_message(context, "", subtype="success", duration_ms=5000)

        self.assertEqual(controller.messages, [("result", "✅ ⏱️ 5s · 🪙 1.2k tok", "markdown")])
        # Promoted to body, so no separate subtext footer is passed.
        self.assertEqual(controller.result_footers, [None])


class AgentSessionIdContextTests(unittest.TestCase):
    def test_bind_agent_session_id_attaches_returned_public_session_id(self):
        controller = _StubController()
        controller.sessions = SimpleNamespace(
            bind_agent_session=lambda session_key, agent_name, anchor, native_id, **kwargs: "sesk8m4q2p7x"
        )
        agent = _StubAgent(controller)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"platform": "slack"},
        )
        request = AgentRequest(
            context=context,
            message="hello",
            working_path="/tmp/work",
            base_session_id="slack_171717.123",
            composite_session_id="slack_171717.123:/tmp/work",
            session_key="slack::C1",
        )

        session_id = agent.bind_agent_session_id(request, "thread-native-1")

        self.assertEqual(session_id, "sesk8m4q2p7x")
        self.assertEqual(request.context.platform_specific["agent_session_id"], "sesk8m4q2p7x")

    def test_bind_agent_session_id_passes_resolved_target_workdir(self):
        calls = {}

        def bind_agent_session(session_key, agent_name, anchor, native_id, **kwargs):
            calls.update(
                session_key=session_key,
                agent_name=agent_name,
                anchor=anchor,
                native_id=native_id,
                kwargs=kwargs,
            )
            return "sesk8m4q2p7x"

        controller = _StubController()
        controller.sessions = SimpleNamespace(bind_agent_session=bind_agent_session)
        agent = _StubAgent(controller)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={
                "platform": "slack",
                "agent_run_target": {"workdir": "/repo/original"},
            },
        )
        request = AgentRequest(
            context=context,
            message="hello",
            working_path="/tmp/test",
            base_session_id="slack_171717.123",
            composite_session_id="slack_171717.123:/repo/original",
            session_key="slack::C1",
        )

        session_id = agent.bind_agent_session_id(request, "thread-native-1")

        self.assertEqual(session_id, "sesk8m4q2p7x")
        self.assertEqual(calls["kwargs"]["workdir"], "/repo/original")

    def test_target_subagent_native_is_stored_under_namespaced_anchor(self):
        calls = {}

        def bind_agent_session(session_key, agent_name, anchor, native_id, **kwargs):
            calls.update(
                session_key=session_key,
                agent_name=agent_name,
                anchor=anchor,
                native_id=native_id,
                kwargs=kwargs,
            )
            return "ses_subagent"

        controller = _StubController()
        controller.sessions = SimpleNamespace(
            bind_agent_session=bind_agent_session,
            bind_agent_session_by_id=lambda *a, **k: "ses_main",
        )
        agent = _StubAgent(controller)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={
                "agent_session_target": {"id": "ses_main"},
                "routing_subagent": "reviewer",
            },
        )
        request = AgentRequest(
            context=context,
            message="hello",
            working_path="/tmp/work",
            base_session_id="chat-1:reviewer",
            composite_session_id="chat-1:reviewer:/tmp/work",
            session_key="avibe::project::p1",
            subagent_name="reviewer",
        )

        session_id = agent.bind_agent_session_id(request, "thread-native-reviewer")

        self.assertEqual(session_id, "ses_subagent")
        self.assertEqual(calls["anchor"], "chat-1:reviewer")
        self.assertEqual(calls["native_id"], "thread-native-reviewer")
        self.assertEqual(request.context.platform_specific["agent_session_id"], "ses_main")


if __name__ == "__main__":
    unittest.main()
