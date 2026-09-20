"""MESSAGE-DELIVERY-035: background work cannot strand a new Claude input."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.handlers.session_handler import ClaudeInputNotSentError, SessionHandler
from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    backend_dispatch_attempted,
    prewrite_failure_evidence,
    set_dispatch_phase,
)
from modules.agents.base import AgentRequest
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.service import AgentService
from modules.im import MessageContext
from vibe.i18n import t


@pytest.fixture
def waiting_input(monkeypatch):
    runtime_key = "fixture:/fixture/work"
    client = SimpleNamespace(query=AsyncMock(), disconnect=AsyncMock())
    handler = SimpleNamespace(
        get_or_create_claude_session=AsyncMock(return_value=client),
        mark_session_active=Mock(),
        mark_session_idle=Mock(),
        touch_session_activity=Mock(),
        handle_session_error=AsyncMock(return_value=False),
    )
    controller = SimpleNamespace(
        config=SimpleNamespace(language="zh"),
        im_client=SimpleNamespace(formatter=SimpleNamespace()),
        settings_manager=SimpleNamespace(),
        session_handler=handler,
        session_manager=SimpleNamespace(),
        receiver_tasks={},
        claude_sessions={runtime_key: client},
        claude_client=SimpleNamespace(),
        emit_agent_message=AsyncMock(),
        agent_auth_service=SimpleNamespace(maybe_emit_auth_recovery_message=AsyncMock(return_value=False)),
    )
    agent = ClaudeAgent(controller)
    agent.ACTIVITY_OUTPUT_WAIT_SECONDS = 0.04
    agent.ACTIVITY_OUTPUT_POLL_SECONDS = 0.005
    agent._delete_ack = AsyncMock()
    agent._receive_messages = AsyncMock()
    agent._remove_ack_reaction = AsyncMock()
    agent.record_model_hub_native_failure = AsyncMock()
    agent._prepare_message_with_files = lambda request: request.message
    service = AgentService(controller)
    service.register(agent)
    controller.agent_service = service
    service.activities.start(
        backend="claude", runtime_key=runtime_key, session_id="fixture-session",
        activity_id="background", kind="local_agent",
    )
    context = MessageContext(user_id="fixture-user", channel_id="fixture-channel", platform="avibe")
    context.platform_specific = {"agent_session_id": "fixture-session", "turn_token": "new-input"}
    set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
    request = AgentRequest(
        context=context, message="保留会话", user_message="保留会话", working_path="/fixture/work",
        base_session_id="fixture", composite_session_id=runtime_key, session_key="fixture-user",
        vibe_agent_model="claude-fixture",
    )
    persist = Mock()
    monkeypatch.setattr("core.message_mirror.persist_agent_message", persist)
    return SimpleNamespace(
        agent=agent, service=service, controller=controller, handler=handler, client=client,
        key=runtime_key, request=request, context=context, persist=persist,
    )


def settle_background(fixture):
    fixture.service.activities.complete(
        backend="claude", runtime_key=fixture.key, activity_id="background",
        status="completed", expects_output=True,
    )
    claimed = fixture.service.activities.claim_completed_output("claude", fixture.key)
    assert claimed is not None
    fixture.service.activities.ack_completed_output(claimed)


async def test_background_wait_timeout_preserves_work_and_releases_gate(waiting_input):
    f = waiting_input
    await asyncio.wait_for(f.service.handle_message("claude", f.request), timeout=1)

    f.client.query.assert_not_awaited()
    f.client.disconnect.assert_not_awaited()
    assert f.controller.claude_sessions[f.key] is f.client
    assert f.service.activities.has_active("claude", f.key)
    assert backend_dispatch_attempted(f.context) is False
    assert prewrite_failure_evidence(f.context) == {
        "reason": "claude_background_activity_timeout", "requires_explicit_retry": True,
    }
    assert not f.service._get_turn_gate(f.key).lock.locked()
    assert not f.agent._pending_requests.get(f.key)
    f.agent.record_model_hub_native_failure.assert_not_awaited()
    f.controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_not_awaited()
    assert "尚未发送" in f.persist.call_args.args[2]
    assert "result" in [call.args[1] for call in f.controller.emit_agent_message.await_args_list]


@pytest.mark.parametrize("replace,settle", [(False, False), (True, False), (False, True), (True, True)])
async def test_retired_client_is_never_written_after_background_wait(waiting_input, replace, settle):
    f = waiting_input
    f.agent.ACTIVITY_OUTPUT_WAIT_SECONDS = 10
    task = asyncio.create_task(f.service.handle_message("claude", f.request))
    try:
        for _ in range(100):
            if f.key in f.agent._activity_settle_events:
                break
            await asyncio.sleep(0.001)
        assert f.key in f.agent._activity_settle_events
        replacement = SimpleNamespace(query=AsyncMock(), disconnect=AsyncMock())
        if replace:
            f.controller.claude_sessions[f.key] = replacement
        else:
            f.controller.claude_sessions.pop(f.key)
        if settle:
            settle_background(f)
        await asyncio.wait_for(task, timeout=1)
        f.client.query.assert_not_awaited()
        replacement.query.assert_not_awaited()
        replacement.disconnect.assert_not_awaited()
        if replace:
            assert f.controller.claude_sessions[f.key] is replacement
        assert backend_dispatch_attempted(f.context) is False
        assert prewrite_failure_evidence(f.context)["reason"] == "claude_runtime_changed_before_write"
        assert not f.service._get_turn_gate(f.key).lock.locked()
        f.agent.record_model_hub_native_failure.assert_not_awaited()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_background_settlement_allows_exactly_one_native_write(waiting_input):
    f = waiting_input
    f.agent._receive_messages = AsyncMock()
    task = asyncio.create_task(f.agent.handle_message(f.request))
    try:
        await asyncio.sleep(0.005)
        f.client.query.assert_not_awaited()
        settle_background(f)
        await asyncio.wait_for(task, timeout=1)
        f.client.query.assert_awaited_once()
        assert backend_dispatch_attempted(f.context) is True
        assert not prewrite_failure_evidence(f.context)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *f.controller.receiver_tasks.values(), return_exceptions=True)


async def test_cancelled_wait_preserves_background_and_never_writes(waiting_input):
    f = waiting_input
    task = asyncio.create_task(f.agent.handle_message(f.request))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    f.client.query.assert_not_awaited()
    f.client.disconnect.assert_not_awaited()
    assert f.service.activities.has_active("claude", f.key)
    assert backend_dispatch_attempted(f.context) is False
    assert not prewrite_failure_evidence(f.context)


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("message_key", ["error.claudeBackgroundInputWaitTimedOut", "error.claudeInputRuntimeChanged"])
async def test_admission_error_is_localized_without_cleaning_runtime(waiting_input, language, message_key):
    f = waiting_input
    handler = object.__new__(SessionHandler)
    im = SimpleNamespace(send_message=AsyncMock())
    handler._get_im_client = lambda _context: im
    handler._get_formatter = lambda _context: SimpleNamespace(format_error=lambda text: text)
    handler._t = lambda key: t(key, language)
    handler.cleanup_session = AsyncMock()
    error = ClaudeInputNotSentError("fixture_reason", message_key)
    assert await handler.handle_session_error(f.key, f.context, error, client=f.client) is False
    im.send_message.assert_awaited_once_with(f.context, t(message_key, language))
    handler.cleanup_session.assert_not_awaited()
    f.client.disconnect.assert_not_awaited()


async def test_retry_after_background_settlement_keeps_same_client(waiting_input):
    f = waiting_input
    await asyncio.wait_for(f.service.handle_message("claude", f.request), timeout=1)
    assert prewrite_failure_evidence(f.context)["requires_explicit_retry"] is True
    settle_background(f)
    # The explicit retry uses the original input and native client, not /new.
    await asyncio.wait_for(f.agent.handle_message(f.request), timeout=1)
    await asyncio.gather(*f.controller.receiver_tasks.values())
    f.client.query.assert_awaited_once()
    assert "保留会话" in f.client.query.call_args.args[0]
    assert f.controller.claude_sessions[f.key] is f.client
    f.client.disconnect.assert_not_awaited()
