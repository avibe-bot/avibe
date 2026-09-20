"""Missing native transcripts preserve durable input until explicit recovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from core.handlers.session_handler import ClaudeSessionNotFoundError, SessionHandler
from core.native_dispatch_phase import (
    DISPATCH_PHASE_ATTEMPTING,
    DISPATCH_PHASE_PREWRITE,
    backend_dispatch_attempted,
    mark_prewrite_recovery_required,
    prewrite_failure_evidence,
    set_dispatch_phase,
)
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from core.services.dispatch import TurnDispatchOutcome
from core.session_turns import DeliveryRequest, SessionTurnManager
from modules.agents.base import AgentRequest
from modules.agents.claude_agent import ClaudeAgent
from storage import message_deliveries as deliveries, messages_service
from storage.models import agent_sessions, session_turns
from tests.backend_failure_retry_helpers import reserve_failure_retry
from tests.test_claude_agent_sessions import _StubController
from tests.test_session_delivery_fsm import _context, _fsm_schema_template, managers  # noqa: F401
from vibe.i18n import t


NATIVE_ID = "11111111-2222-4016-8444-555555555555"


def _agent_fixture(monkeypatch, engine, *, language, working_path):
    controller = _StubController()
    controller.config.language = language
    controller.stored_session_mappings = {}
    controller.im_client.send_message = AsyncMock()
    controller.im_client.formatter.format_error = lambda text: f"❌ {text}"
    handler = SessionHandler(controller)
    handler.get_or_create_claude_session = AsyncMock(
        side_effect=ClaudeSessionNotFoundError(NATIVE_ID, working_path)
    )
    handler.cleanup_session = AsyncMock()
    controller.session_handler = handler
    controller.emit_agent_message = AsyncMock()
    # Force the auth branch to accept if called: typed local failure must win
    # even with "login"/"oauth" in a cwd, independently of the text classifier.
    controller.agent_auth_service.maybe_emit_auth_recovery_message.return_value = True
    controller.agent_service = SimpleNamespace(release_runtime_turn=lambda _context: None)
    agent = ClaudeAgent(controller)
    agent._delete_ack = AsyncMock()
    agent.record_model_hub_native_failure = AsyncMock()
    monkeypatch.setattr("core.message_mirror.get_cached_sqlite_engine", lambda: engine)
    return agent, controller


def _request(context, text, working_path):
    return AgentRequest(
        context=context,
        message=text,
        user_message=text,
        working_path=working_path,
        base_session_id="ses_fsm",
        composite_session_id=f"ses_fsm:{working_path}",
        session_key="avibe::ses_fsm",
    )


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_missing_session_delivers_localized_notice_and_terminal_failure(
    managers, monkeypatch, tmp_path, language
):
    _manager, _restarted, engine, _other, _starts = managers
    working_path = str(tmp_path / "oauth-login-401" / "原目录")
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions).where(agent_sessions.c.id == "ses_fsm").values(
                agent_backend="claude", native_session_id=NATIVE_ID, workdir=working_path
            )
        )
    agent, controller = _agent_fixture(monkeypatch, engine, language=language, working_path=working_path)
    context = _context()
    context.platform_specific["turn_token"] = "trn_missing"
    set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
    request = _request(context, "请继续，保留这段输入", working_path)

    await agent.handle_message(request)

    controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_not_awaited()
    controller.session_handler.cleanup_session.assert_not_awaited()
    assert agent._pending_requests == {}
    assert controller.receiver_tasks == {}
    assert backend_dispatch_attempted(context) is False
    assert request.message == "请继续，保留这段输入"
    expected = f"❌ {t('error.claudeSessionNotFound', language, sessionId=NATIVE_ID, path=working_path)}"
    controller.im_client.send_message.assert_awaited_once_with(context, expected)
    terminal = controller.emit_agent_message.await_args
    assert terminal.args == (context, "result", "")
    assert terminal.kwargs["is_error"] is True
    assert terminal.kwargs["level"] == "silent"
    assert terminal.kwargs["output"].completes_turn is True
    assert terminal.kwargs["output"].settles_run is True
    assert NATIVE_ID in terminal.kwargs["terminal_error"]
    with engine.connect() as conn:
        notices = messages_service.list_session_messages(conn, session_id="ses_fsm")["messages"]
        binding = conn.execute(select(agent_sessions).where(agent_sessions.c.id == "ses_fsm")).mappings().one()
    assert len(notices) == 1
    assert notices[0]["text"] == expected
    assert notices[0]["type"] == "notify"
    assert notices[0]["metadata"]["event"] == "backend_failure"
    assert notices[0]["metadata"]["turn_id"] == "trn_missing"
    assert binding["native_session_id"] == NATIVE_ID
    assert binding["workdir"] == working_path


@pytest.mark.parametrize("retry", ["failure_notice", "send_now", "still_missing"])
async def test_missing_session_queue_waits_for_explicit_recovery(
    managers, monkeypatch, tmp_path, retry
):
    """MESSAGE-DELIVERY-029: failed startup -> retained input -> explicit retry."""
    manager, restarted, engine, _other, starts = managers
    working_path = str(tmp_path / "原目录")
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions).where(agent_sessions.c.id == "ses_fsm").values(
                agent_backend="claude", native_session_id=NATIVE_ID, workdir=working_path
            )
        )
        messages_service.append(
            conn, session_id="ses_fsm", scope_id=None, platform="avibe",
            author="agent", source="agent", message_type="result", text="历史回复",
        )
    agent, controller = _agent_fixture(monkeypatch, engine, language="zh", working_path=working_path)
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    manager.controller.emit_agent_message = AsyncMock()

    async def dispatch(_controller, context, text, **_kwargs):
        # The shared dispatch phase is also shared by the adapter's context copy.
        evidence = set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
        copied = _context()
        copied.platform_specific = dict(context.platform_specific)
        assert copied.platform_specific["agent_dispatch_evidence"] is evidence
        await agent.handle_message(_request(copied, text, working_path))
        return TurnDispatchOutcome(None, SETTLED_BY_TERMINAL_RESULT, backend_dispatch_attempted(context))

    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", dispatch)
    admitted = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content="请继续原来的工作"),
        context=_context(),
    )
    await manager.in_flight["ses_fsm"].task
    with engine.connect() as conn:
        retained = deliveries.get_delivery(conn, admitted.delivery_id)
        turn = deliveries.get_turn(conn, admitted.turn_id)
        notices = messages_service.list_session_messages(conn, session_id="ses_fsm")["messages"]
    assert retained["state"] == "queued"
    assert retained["message_id"] is None
    assert retained["current_attempt_id"] is None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "not_written"
    assert turn["start_receipt_outcome"] == "not_written"
    notice = next(row for row in notices if row["type"] == "notify")
    assert notice["metadata"]["turn_id"] == admitted.turn_id
    assert any(row["text"] == "历史回复" for row in notices)

    # Repeated timer drains and a new manager after restart cannot replay.
    for _ in range(3):
        assert not await manager.drain_delivery_queue("ses_fsm")
        await restarted.recover_durable_delivery_state("ses_fsm")
    with engine.connect() as conn:
        unchanged = deliveries.get_delivery(conn, admitted.delivery_id)
        turns = list(conn.execute(select(session_turns.c.id)))
    assert unchanged == retained
    assert len(turns) == 1
    assert starts == []
    controller.session_handler.get_or_create_claude_session.assert_awaited_once()

    # The same durable input and snapshot remain owned by the retry.
    if retry == "still_missing":
        result = await manager.send_now("ses_fsm", expected_delivery_id=admitted.delivery_id)
        assert result["status"] == "claimed"
        await manager.in_flight["ses_fsm"].task
        assert controller.session_handler.get_or_create_claude_session.await_count == 2
        for _ in range(3):
            await restarted.recover_durable_delivery_state("ses_fsm")
        with engine.connect() as conn:
            retried = deliveries.get_delivery(conn, admitted.delivery_id)
        assert retried["snapshot_json"] == retained["snapshot_json"]
        assert retried["state"] == "queued"
        assert starts == []
        return
    elif retry == "failure_notice":
        with engine.begin() as conn:
            reserved = reserve_failure_retry(conn, notice)
        assert reserved["id"] == admitted.delivery_id
        result = await restarted.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="ignored", delivery_id=reserved["id"]),
            context=_context(),
        )
        assert result.state == "claimed"
    else:
        result = await restarted.send_now("ses_fsm", expected_delivery_id=admitted.delivery_id)
        assert result["status"] == "claimed"
    assert len(starts) == 1
    assert "请继续原来的工作" in starts[0][1]
    with engine.connect() as conn:
        retried = deliveries.get_delivery(conn, admitted.delivery_id)
    assert retried["snapshot_json"] == retained["snapshot_json"]
    assert retried["turn_id"] != admitted.turn_id


@pytest.mark.parametrize("phase", [None, DISPATCH_PHASE_ATTEMPTING])
def test_unknown_or_attempted_native_input_cannot_require_prewrite_retry(phase):
    context = _context()
    if phase is not None:
        set_dispatch_phase(context, phase)
    before = dict(context.platform_specific)
    mark_prewrite_recovery_required(context, "native_session_not_found")
    assert prewrite_failure_evidence(context) == {}
    assert context.platform_specific == before
    assert backend_dispatch_attempted(context) is (None if phase is None else True)


async def test_query_failure_does_not_mark_input_as_unwritten(managers, monkeypatch, tmp_path):
    _manager, _restarted, engine, _other, _starts = managers
    agent, controller = _agent_fixture(monkeypatch, engine, language="en", working_path=str(tmp_path))
    client = SimpleNamespace(
        query=AsyncMock(side_effect=ClaudeSessionNotFoundError(NATIVE_ID, str(tmp_path))),
    )
    controller.session_handler.get_or_create_claude_session = AsyncMock(return_value=client)
    agent._prepare_message_with_files = lambda request: request.message
    context = _context()
    set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
    await agent.handle_message(_request(context, "native may have received this", str(tmp_path)))
    client.query.assert_awaited_once()
    assert backend_dispatch_attempted(context) is True
    assert prewrite_failure_evidence(context) == {}
    assert "failure" not in context.platform_specific["agent_dispatch_evidence"]


async def test_ordinary_queue_and_transient_prewrite_failure_still_drain(managers):
    manager, restarted, engine, _other, starts = managers
    initial = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content="ordinary input"),
        context=_context(),
    )
    assert len(starts) == 1
    manager._settle_durable_prewrite_failure(initial.turn_id, outcome="transient_start_failure")
    with engine.connect() as conn:
        retained = deliveries.get_delivery(conn, initial.delivery_id)
    assert retained["state"] == "queued"
    assert not deliveries.requires_explicit_start_retry(retained)
    await restarted.recover_durable_delivery_state("ses_fsm")
    assert len(starts) == 2
    assert starts[1][1] == starts[0][1]
