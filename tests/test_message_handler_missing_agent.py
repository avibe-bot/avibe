from __future__ import annotations

from types import SimpleNamespace

import anyio

from core.handlers.message_handler import MessageHandler
from modules.agents.base import AgentRequest
from modules.im import MessageContext


class _StubIMClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, context, text, parse_mode=None):
        self.sent.append(text)
        return "msg-1"


class _StubController:
    def __init__(self) -> None:
        self.config = SimpleNamespace(language="zh")
        self.im_client = _StubIMClient()
        self.settings_manager = SimpleNamespace(sessions={})
        self.sessions = self.settings_manager.sessions
        self.session_manager = SimpleNamespace()
        self.receiver_tasks = {}
        self.agent_service = SimpleNamespace(default_agent="claude", agents={})
        self.agent_auth_service = SimpleNamespace()
        self.vibe_agent_store = SimpleNamespace(get=lambda _name: None)
        self.emissions = []

    def get_im_client_for_context(self, context):
        return self.im_client

    def _get_lang(self) -> str:
        return "zh"

    async def emit_agent_message(self, context, message_type, text, **kwargs):
        self.emissions.append((message_type, text, kwargs))
        evidence = kwargs.get("delivery")
        if message_type == "notify" and evidence is not None:
            evidence.persisted_row = {"id": "msg-primary"}
            return "msg-primary"
        return None


def test_missing_opencode_agent_hint_does_not_mention_codex():
    controller = _StubController()
    handler = MessageHandler(controller)

    async def _noop_stream(_context, _text):
        return None

    handler._stream_terminal_error = _noop_stream
    context = MessageContext(user_id="U1", channel_id="C1", platform="telegram")

    anyio.run(handler._handle_missing_agent, context, "opencode")

    sent = controller.im_client.sent[-1]
    assert "OpenCode" in sent
    assert "OPENCODE_CLI_PATH" in sent
    assert "Codex" not in sent


def test_dispatched_missing_agent_preserves_primary_delivery_in_terminal_contract():
    controller = _StubController()
    handler = MessageHandler(controller)
    context = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform="avibe",
        platform_specific={
            "turn_token": "turn-missing-agent",
            "task_execution_id": "run-missing-agent",
            "task_trigger_kind": "scheduled",
        },
    )
    request = AgentRequest(
        context=context,
        message="work",
        user_message="work",
        working_path="/tmp/work",
        base_session_id="session-1",
        composite_session_id="session-1:/tmp/work",
        session_key="avibe::project::p1",
    )

    async def run() -> None:
        await handler._handle_missing_agent(
            context,
            "opencode",
            request=request,
        )

    anyio.run(run)

    assert [message_type for message_type, _text, _kwargs in controller.emissions] == [
        "notify",
        "result",
    ]
    terminal = controller.emissions[1][2]["output"]
    assert terminal.metadata["turn_failure_notification"] == {
        "failure_id": "turn:turn-missing-agent",
        "ack_evidence": "receipt",
        "delivered": True,
    }
    assert controller.im_client.sent == []
