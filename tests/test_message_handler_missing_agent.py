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


def test_an_agent_that_cannot_run_offers_to_continue_on_vibey(monkeypatch):
    """A disabled or deleted Agent ends the turn with a notice that names it and
    offers Vibey: the Workbench draws the offer as a button from data, a chat app
    is told where to switch. Without a built-in Vibey there is nothing to offer,
    and a backend that is missing is offered Vibey unless Vibey is what is missing."""
    from unittest.mock import AsyncMock, patch

    from core.backend_failure import CONTINUE_ON_VIBEY_NEXT_STEP
    from core.vibe_agents import AgentUnavailableError

    from modules.agents.model_hub import ModelSupplyRefusal

    # The Model Hub supplies Vibey a model unless a case below says otherwise.
    monkeypatch.setattr("modules.agents.model_hub.hub_supply_refusal", lambda _controller, _backend, _lang: None)
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
    # Resolved by id, a deleted Agent names only its id; the session still knows its name.
    by_id = AgentUnavailableError("agent id 'a1b2' not found", agent_name="a1b2", reason="missing")
    targeted = MessageContext(
        user_id="U1",
        channel_id="ses-1",
        platform="avibe",
        platform_specific={"agent_session_target": {"id": "ses-1", "agent_id": "a1b2", "agent_name": "reviewer"}},
    )
    (by_id_call,) = emitted_for((handler._emit_agent_dispatch_failure, targeted, None, by_id))
    assert handler._t("error.agentUnavailable.missing", agent="reviewer") in by_id_call.kwargs["display_text"]
    assert "a1b2" not in by_id_call.kwargs["display_text"]
    assert "agent 'codex' is disabled" not in workbench_call.kwargs["display_text"]

    controller.vibe_agent_store = SimpleNamespace(
        get=lambda _name: None, get_builtin_default_agent_for_backend=lambda backend: None
    )
    (no_vibey,) = emitted_for((handler._emit_agent_dispatch_failure, workbench, None, disabled))
    assert no_vibey.kwargs["display_text"] == f"❌ {disabled_text}"
    assert no_vibey.kwargs["output"] is None

    # Vibey exists but the Model Hub supplies it no model: the continuation would only
    # fail again, so it is not offered either.
    controller.vibe_agent_store = SimpleNamespace(
        get=lambda _name: None, get_builtin_default_agent_for_backend=lambda backend: vibey
    )
    monkeypatch.setattr(
        "modules.agents.model_hub.hub_supply_refusal",
        lambda _controller, backend, _lang: ModelSupplyRefusal("no source", fixed_in_models=True),
    )
    (no_supply,) = emitted_for((handler._emit_agent_dispatch_failure, workbench, None, disabled))
    assert no_supply.kwargs["display_text"] == f"❌ {disabled_text}"
    assert no_supply.kwargs["output"] is None
    monkeypatch.setattr("modules.agents.model_hub.hub_supply_refusal", lambda _controller, _backend, _lang: None)

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


def test_an_agent_that_cannot_run_holds_its_input_only_where_it_can_be_retried():
    """Retrying on its own would fail the same way. In the Workbench an input that never
    reached the backend is held for an explicit retry: continue on Vibey, or Retry once
    the Agent is back. A chat app has no Retry, so once its notice is delivered its
    input is retired rather than holding every later message behind it. One that
    reached the backend is left alone."""
    from unittest.mock import patch

    from core.native_dispatch_phase import (
        DISPATCH_PHASE_ATTEMPTING,
        DISPATCH_PHASE_PREWRITE,
        backend_dispatch_attempted,
        prewrite_failure_evidence,
        set_dispatch_phase,
    )
    from core.vibe_agents import AgentUnavailableError

    controller = _StubController()
    controller.im_client.formatter = SimpleNamespace(escape_special_chars=lambda text: text)
    handler = MessageHandler(controller)
    disabled = AgentUnavailableError("agent 'codex' is disabled", agent_name="codex", reason="disabled")

    def failed(platform, phase, *, delivered=True):
        context = MessageContext(user_id="U1", channel_id="ses-1", platform=platform)
        set_dispatch_phase(context, phase)

        async def notify(*_args, delivery=None, **_kwargs):
            if delivered and delivery is not None:
                delivery.delivered_id = "notice-1"
            return False

        with patch.dict(MessageHandler._emit_agent_cannot_run.__globals__, emit_backend_failure=notify):
            anyio.run(handler._emit_agent_dispatch_failure, context, None, disabled)
        return context

    workbench = failed("avibe", DISPATCH_PHASE_PREWRITE)
    assert backend_dispatch_attempted(workbench) is False
    assert prewrite_failure_evidence(workbench) == {"reason": "agent_unavailable", "requires_explicit_retry": True}
    chat_app = failed("telegram", DISPATCH_PHASE_PREWRITE)
    assert prewrite_failure_evidence(chat_app) == {"reason": "agent_unavailable", "retire_input": True}
    # Not told to send it again, so the input is left to the ordinary retries.
    assert prewrite_failure_evidence(failed("telegram", DISPATCH_PHASE_PREWRITE, delivered=False)) == {}
    for platform in ("avibe", "telegram"):
        assert prewrite_failure_evidence(failed(platform, DISPATCH_PHASE_ATTEMPTING)) == {}


def test_a_chat_app_is_told_to_resend_when_vibey_is_not_offered():
    """Its input is retired, so with nothing to continue on it must be sent again."""
    from unittest.mock import AsyncMock, patch

    from core.vibe_agents import AgentUnavailableError

    controller = _StubController()
    controller.im_client.formatter = SimpleNamespace(escape_special_chars=lambda text: text)
    controller.vibe_agent_store = SimpleNamespace(
        get=lambda _name: None, get_builtin_default_agent_for_backend=lambda backend: None
    )
    handler = MessageHandler(controller)
    disabled = AgentUnavailableError("agent 'codex' is disabled", agent_name="codex", reason="disabled")
    calls = {}
    for platform in ("telegram", "avibe"):
        emitted = AsyncMock()
        with patch.dict(MessageHandler._emit_agent_cannot_run.__globals__, emit_backend_failure=emitted):
            anyio.run(
                handler._emit_agent_dispatch_failure,
                MessageContext(user_id="U1", channel_id="C1", platform=platform),
                None,
                disabled,
            )
        calls[platform] = emitted.await_args.kwargs["display_text"]
    resend = handler._t("error.agentUnavailable.resendLater")
    assert calls["telegram"].endswith(f"\n{resend}")
    # The Workbench holds the input for Retry, so it says nothing of the kind.
    assert resend not in calls["avibe"]
