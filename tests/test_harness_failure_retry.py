"""MESSAGE-DELIVERY-320: Harness recovery uses the existing notice-bound Retry."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from core import internal_server
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.scheduled_tasks import ScheduledTaskService, TaskExecutionStore
from core.session_turns import DeliveryRequest, SessionTurnManager
from storage import message_deliveries, messages_service
from storage.background import attach_agent_run_delivery_in_connection
from storage.db import create_sqlite_engine
from storage.models import agent_runs
from tests.backend_failure_retry_helpers import seed_failed_notice
from tests.scenario_harness.message_delivery import MessageDeliveryController
from tests.test_harness_failure_visibility import _drain_service
from tests.test_internal_server import _bind_test_native_start, _build_controller_double
from tests.test_session_delivery_fsm import _context
from tests.test_ui_session_stream import _make_session, isolated_state  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers


@pytest.fixture(autouse=True)
def _isolate_web_push(monkeypatch):
    monkeypatch.setattr("core.web_push_notifications.maybe_notify_inbox_message", lambda *_args: None)


def _harness_restart_notice(tmp_path, *, backend="codex", target="same", linked=True):
    """Real Run/Delivery/Turn settlement, owed-notice drain, and message storage."""
    scope_id, session_id = _make_session(tmp_path, agent_backend=backend)
    engine = create_sqlite_engine()
    controller = MessageDeliveryController(platform="avibe")
    controller.config.language = "zh"
    request_store = TaskExecutionStore()
    service = _drain_service(tmp_path, controller, request_store.sqlite_backend, request_store)
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)
    controller.scheduled_task_service = service
    controller.emit_agent_message = ConsolidatedMessageDispatcher(controller).emit_agent_message

    def context(sid):
        value = _context(sid)
        value.platform_specific["agent_session_target"]["agent_backend"] = backend
        return value

    manager = SessionTurnManager(controller, build_context=context)
    manager._engine = engine
    controller.session_turns = manager
    run = request_store.enqueue_agent_run(
        message="检查剩余工作，不要重复提交", session_id=session_id,
        agent_name="worker", agent_backend=backend,
    )
    assert request_store.claim(run.id) is not None

    async def accept(_sid, ctx, _text, **_kwargs):
        _bind_test_native_start(engine, ctx)

    manager._run = accept
    original = asyncio.run(manager.deliver(
        DeliveryRequest(
            session_id=session_id, priority="p3", content=run.prompt,
            source="harness", author="harness", message_type="harness",
        ),
        context=context(session_id),
    ))
    with engine.begin() as conn:
        assert attach_agent_run_delivery_in_connection(
            conn, run.id, session_id=session_id, delivery_id=original.delivery_id,
        )
    manager._terminalize_durable_turn(
        original.turn_id, "failed", settled_by="restarted", evidence_kind="service_shutdown",
    )
    stored_run = request_store.get_run(run.id)
    owed = request_store.sqlite_backend.owed_failure_notice(run.id)
    assert stored_run["status"] == "failed"
    assert owed["turn_id"] == original.turn_id
    assert owed["failure_id"] == f"turn:{original.turn_id}"
    if not linked:
        # Legacy and command-only reports do not become linked by parsing their ID.
        owed = {**owed, "turn_id": None}
    newer = None
    if target == "newer":
        newer = asyncio.run(manager.deliver(
            DeliveryRequest(session_id=session_id, priority="p3", content="新的一轮工作"),
            context=context(session_id),
        ))
    if target == "other":
        from core.services import sessions

        with engine.begin() as conn:
            other = sessions.create_session(
                conn, scope_id=scope_id, agent_backend=backend,
                agent_name="worker",
            )
        notice_session = other["id"]
        from core.scheduled_tasks import parse_scope_id

        service._failure_notice_targets = lambda _run: [
            (parse_scope_id(scope_id), notice_session),
        ]
    else:
        notice_session = session_id
    from core.delivery_evidence import DeliveryEvidence

    evidence = DeliveryEvidence()
    assert asyncio.run(service._emit_failure_notice(stored_run, owed, evidence))
    assert evidence.persisted_row is not None
    # Same authoritative identity survives another delivery attempt.
    assert asyncio.run(service._emit_failure_notice(stored_run, owed, DeliveryEvidence()))
    with engine.connect() as conn:
        notices = [
            row for row in messages_service.list_session_messages(conn, session_id=notice_session)["messages"]
            if row["type"] == "notify"
        ]
        turn = message_deliveries.get_turn(conn, original.turn_id)
        if newer is not None:
            assert message_deliveries.get_turn(conn, newer.turn_id)["state"] == "active"
    assert len(notices) == 1
    assert turn["terminal_outcome"] == "failed"
    assert turn["start_receipt_outcome"] == "accepted"
    assert controller.runtime_release_calls == controller.stream_completion_calls == 0
    from vibe.message_types import activity_role_for

    assert activity_role_for("notify", notices[0]["metadata"]) == "none"
    return session_id, notices[0], turn


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_harness_restart_notice_retries_through_web_and_controller(isolated_state, tmp_path, monkeypatch, backend):
    """MESSAGE-DELIVERY-320: a Harness report admits one continue, not a Run rerun."""
    from vibe import ui_server

    session_id, notice, turn = _harness_restart_notice(tmp_path, backend=backend)
    assert "[Avibe Harness]" in notice["text"]
    assert notice["metadata"]["turn_id"] == turn["id"]
    assert notice["metadata"]["replayed"] is True
    assert notice["metadata"]["detached"] is False
    controller = _build_controller_double()
    internal_app = internal_server.create_app(controller)
    manager = controller.session_turns
    engine = create_sqlite_engine()
    starts = []

    async def accept(sid, context, text, *, logical_turn_id=None, **_kwargs):
        starts.append((sid, text))
        _bind_test_native_start(engine, context)
        manager._terminalize_durable_turn(
            logical_turn_id, "completed", settled_by="terminal_result", evidence_kind="test_terminal",
        )

    manager._run = accept

    async def dispatch(payload):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://testserver",
        ) as internal:
            response = await internal.post("/internal/dispatch_async", json=payload)
        return {"status_code": response.status_code, "body": response.json()}

    monkeypatch.setattr("vibe.internal_client.dispatch_async", dispatch)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    draft = client.put(
        f"/api/sessions/{session_id}/draft", headers=headers,
        json={"text": "保留这份草稿", "expected_updated_at": None},
    ).get_json()["draft"]
    url = f"/api/sessions/{session_id}/messages"
    first = client.post(url, headers=headers, json={"retry_for": notice["id"], "text": "replay original tools"})
    duplicate = client.post(url, headers=headers, json={"retry_for": notice["id"]})
    assert first.status_code == duplicate.status_code == 201
    assert first.get_json()["id"] == duplicate.get_json()["id"]
    assert first.get_json()["draft"] == draft
    assert first.get_json()["draft_advanced"] is False
    assert starts == [(session_id, "continue")]
    with engine.connect() as conn:
        reloaded = messages_service.get_message(conn, notice["id"])
        runs = conn.execute(select(agent_runs.c.id, agent_runs.c.status)).all()
    assert reloaded["content"]["failure_retry"]["state"] == "accepted"
    assert runs == [(notice["metadata"]["run_id"], "failed")]


@pytest.mark.parametrize("change", ["newer", "delayed", "pending", "unlinked", "other_session"])
def test_harness_notice_keeps_retry_boundary_guards(isolated_state, tmp_path, change):
    """MESSAGE-DELIVERY-320: no cross-Session, inferred, busy, or stale retry."""
    from vibe import ui_server

    _source_session, notice, _turn = _harness_restart_notice(
        tmp_path,
        target={"other_session": "other", "delayed": "newer"}.get(change, "same"),
        linked=change != "unlinked",
    )
    assert notice["metadata"]["replayed"] is True
    if change in {"unlinked", "other_session"}:
        assert not notice["metadata"].get("turn_id")
    with create_sqlite_engine().begin() as conn:
        if change == "newer":
            seed_failed_notice(conn, session_id=notice["session_id"], scope_id=notice["scope_id"])
        elif change == "pending":
            message_deliveries.insert_delivery(
                conn, delivery_id=message_deliveries.new_delivery_id(),
                session_id=notice["session_id"], priority="p3", state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=notice["scope_id"], session_id=notice["session_id"], platform="avibe",
                    author="user", source="user", text="另一个任务",
                ),
                dispatch_text="另一个任务",
            )
    client = ui_server.app.test_client()
    with patch("vibe.internal_client.dispatch_async", AsyncMock()) as dispatch:
        response = client.post(
            f"/api/sessions/{notice['session_id']}/messages", headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 409
    dispatch.assert_not_awaited()
