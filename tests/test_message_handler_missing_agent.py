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


def test_an_agent_that_cannot_run_offers_to_continue_on_vibey():
    """A disabled or deleted Agent ends the turn with a notice that names it and
    offers Vibey: the Workbench draws the offer as a button from data, a chat app
    is told where to switch. Without a built-in Vibey there is nothing to offer,
    and a backend that is missing is offered Vibey unless Vibey is what is missing."""
    from unittest.mock import AsyncMock, patch

    from core.backend_failure import CONTINUE_ON_VIBEY_NEXT_STEP
    from core.vibe_agents import AgentUnavailableError

    controller = _StubController()
    controller.im_client.formatter = SimpleNamespace(escape_special_chars=lambda text: text)
    vibey = SimpleNamespace(name="vibey")
    controller.vibe_agent_store = SimpleNamespace(
        get=lambda _name: None, get_builtin_default_agent_for_backend=lambda backend: vibey
    )
    handler = MessageHandler(controller)
    workbench = MessageContext(user_id="U1", channel_id="ses-1", platform="avibe")
    telegram = MessageContext(user_id="U1", channel_id="C1", platform="telegram")
    disabled = AgentUnavailableError("agent 'codex' is disabled", agent_name="codex", reason="disabled")
    deleted = AgentUnavailableError("agent 'reviewer' not found", agent_name="reviewer", reason="missing")

    def emitted_for(*calls):
        emitted = AsyncMock()
        with patch.dict(MessageHandler._emit_agent_cannot_run.__globals__, emit_backend_failure=emitted):
            for call in calls:
                anyio.run(*call)
        return emitted.await_args_list

    workbench_call, im_call, deleted_call = emitted_for(
        (handler._emit_agent_dispatch_failure, workbench, None, disabled),
        (handler._emit_agent_dispatch_failure, telegram, None, disabled),
        (handler._emit_agent_dispatch_failure, workbench, None, deleted),
    )
    disabled_text = handler._t("error.agentUnavailable.disabled", agent="Codex")
    assert workbench_call.kwargs["display_text"] == (
        f"❌ {disabled_text}\n{handler._t('error.continueOnVibey.workbench')}"
    )
    assert workbench_call.kwargs["output"].metadata["failure_next_step"] == CONTINUE_ON_VIBEY_NEXT_STEP
    assert workbench_call.kwargs["cause"] is disabled
    assert im_call.kwargs["display_text"] == f"❌ {disabled_text}\n{handler._t('error.continueOnVibey.im')}"
    assert im_call.kwargs["output"] is None
    assert handler._t("error.agentUnavailable.missing", agent="reviewer") in deleted_call.kwargs["display_text"]
    assert "agent 'codex' is disabled" not in workbench_call.kwargs["display_text"]

    controller.vibe_agent_store = SimpleNamespace(
        get=lambda _name: None, get_builtin_default_agent_for_backend=lambda backend: None
    )
    (no_vibey,) = emitted_for((handler._emit_agent_dispatch_failure, workbench, None, disabled))
    assert no_vibey.kwargs["display_text"] == f"❌ {disabled_text}"
    assert no_vibey.kwargs["output"] is None

    controller.vibe_agent_store = SimpleNamespace(
        get=lambda _name: None, get_builtin_default_agent_for_backend=lambda backend: vibey
    )
    request = AgentRequest(
        context=workbench,
        message="hello",
        user_message="hello",
        working_path="/tmp",
        base_session_id="base",
        composite_session_id="base:/tmp",
        session_key="avibe::ses-1",
    )
    for target, offered in (("codex", True), ("vibey", False)):
        (call,) = emitted_for((lambda t=target: handler._handle_missing_agent(workbench, t, request=request),))
        assert (call.kwargs["output"] is not None) is offered
        assert (handler._t("error.continueOnVibey.workbench") in call.kwargs["display_text"]) is offered
