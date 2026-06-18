from __future__ import annotations

from types import SimpleNamespace

import anyio

from core.handlers.message_handler import MessageHandler
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
        self.vibe_agent_store = SimpleNamespace(get=lambda _name: None)

    def get_im_client_for_context(self, context):
        return self.im_client

    def _get_lang(self) -> str:
        return "zh"


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
