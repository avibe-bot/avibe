from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from core.session_turns import DeliveryRequest
from modules.agents.catalog import AGENT_BACKENDS
from storage import message_deliveries, messages_service
from storage.agent_session_rows import reserve_write_lock
from storage.db import create_sqlite_engine
from storage.models import (
    agent_events,
    agent_sessions,
    message_deliveries as message_deliveries_table,
    messages,
    session_turns,
)
from tests.backend_failure_retry_helpers import previous_process as _previous_process
from tests.backend_failure_retry_helpers import reserve_failure_retry, seed_failed_notice
from tests.test_session_delivery_fsm import _context, _fsm_schema_template, managers  # noqa: F401
from tests.test_ui_session_stream import _accepted_dispatch, _make_session, isolated_state  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers


def _with_vibey_recovery(controller, engine, tmp_path, jobs):
    """The registered Vibey adapter over ``jobs`` and its single recovery owner, as at startup."""
    from core.agent_core.messages import text
    from core.agent_core.tools.base import ToolResult
    from modules.agents.vibey import VibeyAgent
    from modules.agents.vibey.recovery import VibeyRecovery
    from modules.agents.vibey.tools import ToolSuite

    async def handed_over(_call, _job_id, _status, watch_id):
        return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

    suite = ToolSuite(
        jobs=jobs, create_tools=lambda *_args: [], render_recovered=handed_over,
        find_job=lambda call: "job_1" if call.tool_call_id == "call_bash" else None,
    )
    controller.agent_service.agents["vibey"] = VibeyAgent(
        controller, engine=engine, tool_suite=suite, state_dir=tmp_path / "agent_core"
    )
    controller.vibey_recovery = VibeyRecovery(controller)


def _accepted_turn(tmp_path, *, backend, delivery="ready", native_message_id=None, jobs=None):
    """One accepted Turn through the real Delivery owner, notice dispatcher, and storage.

    With ``jobs``, startup also runs Vibey's own recovery pass first, over those jobs."""
    from core.message_dispatcher import ConsolidatedMessageDispatcher
    from core.session_turns import SessionTurnManager
    from tests.scenario_harness.message_delivery import MessageDeliveryController
    from tests.test_internal_server import _bind_test_native_start

    _scope_id, session_id = _make_session(tmp_path, agent_backend=backend)
    engine = create_sqlite_engine()
    controller = MessageDeliveryController(platform="avibe")
    controller.config.language = "zh"

    def context(sid):
        value = _context(sid)
        value.platform_specific["agent_session_target"]["agent_backend"] = backend
        return value

    manager = SessionTurnManager(controller, build_context=context)
    manager._engine = engine
    controller.session_turns = manager
    dispatcher = ConsolidatedMessageDispatcher(controller)
    controller.emit_agent_message = dispatcher.emit_agent_message
    if jobs is not None:
        _with_vibey_recovery(controller, engine, tmp_path, jobs)
    if delivery == "send_failed":
        controller.im_client.send_message = AsyncMock(side_effect=[OSError("transport offline"), "msg-recovered"])
    starts, contexts = [], []

    async def accepted(sid, ctx, text, **_kwargs):
        starts.append((sid, text))
        contexts.append(ctx)
        _bind_test_native_start(engine, ctx)

    manager._run = accepted
    original = asyncio.run(manager.deliver(
        DeliveryRequest(
            session_id=session_id, priority="p3", content="完成剩余工作，不要重复提交",
            **({"platform": "slack", "native_message_id": native_message_id} if native_message_id else {}),
        ),
        context=context(session_id),
    ))
    starts.clear()
    contexts.clear()
    return SimpleNamespace(
        manager=manager, engine=engine, session_id=session_id, turn_id=str(original.turn_id),
        context=context, starts=starts, contexts=contexts, delivery=delivery,
    )


def _restart(turn, before_restart=None):
    """``before_restart(engine, session_id, turn_id)`` writes what the previous process
    and the startup steps before Turn recovery left behind."""
    if before_restart is not None:
        before_restart(turn.engine, turn.session_id, turn.turn_id)
    turn.manager._active_identity = lambda *_args: None
    turn.manager._transport_can_deliver = lambda _platform: turn.delivery != "unready"
    recovery = getattr(turn.manager.controller, "vibey_recovery", None)

    async def startup():
        # The controller's order: Vibey's T2 pass, then Turn recovery.
        if recovery is not None:
            assert await recovery.start() == []
        await turn.manager.recover_durable_delivery_state(turn.session_id, service_restart=True)

    asyncio.run(startup())


def _notices(engine, session_id):
    with engine.connect() as conn:
        return [
            row for row in messages_service.list_session_messages(conn, session_id=session_id)["messages"]
            if row["type"] == "notify"
        ]


def _restart_failure_notice(tmp_path, *, backend, delivery="ready", before_restart=None, jobs=None):
    """Recover an accepted Turn through the real notice dispatcher and storage."""
    cut = _accepted_turn(tmp_path, backend=backend, delivery=delivery, jobs=jobs)
    manager, engine, session_id = cut.manager, cut.engine, cut.session_id
    _restart(cut, before_restart)
    assert cut.starts == []
    if delivery != "ready":
        assert _notices(engine, session_id) == []
        assert asyncio.run(manager.notify_transport_ready("avibe")) == 1
    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, cut.turn_id)
    notices = _notices(engine, session_id)
    assert turn["terminal_evidence_kind"] == "restart_runtime_missing"
    assert turn["start_receipt_outcome"] == "accepted"
    assert len(notices) == 1
    notice = notices[0]
    assert "服务在它运行期间重启" in notice["text"]
    assert notice["metadata"]["event"] == "backend_failure"
    assert notice["metadata"]["turn_id"] == turn["id"]
    assert notice["metadata"]["failure_id"] == f"turn:{turn['id']}"
    assert notice["metadata"]["backend"] == backend
    assert notice["metadata"]["detached"] is False
    assert notice["metadata"]["replayed"] is True
    # Neither another recovery pass nor another readiness hook emits a duplicate.
    asyncio.run(manager.recover_durable_delivery_state(session_id, service_restart=True))
    assert asyncio.run(manager.notify_transport_ready("avibe")) == 0
    return session_id, notice


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("delivery", ["ready", "unready", "send_failed"])
def test_restart_notice_retries_through_web_and_controller(isolated_state, tmp_path, monkeypatch, backend, delivery):
    """MESSAGE-DELIVERY-030: restart -> persisted notice -> Web -> one continuation."""
    import httpx
    from core import internal_server
    from tests.test_internal_server import _bind_test_native_start, _build_controller_double
    from vibe import ui_server

    session_id, notice = _restart_failure_notice(tmp_path, backend=backend, delivery=delivery)
    engine = create_sqlite_engine()
    controller = _build_controller_double()
    internal_app = internal_server.create_app(controller)
    manager = controller.session_turns
    starts = []

    async def capture_run(sid, context, text, *, logical_turn_id=None, **_kwargs):
        starts.append((sid, text))
        _bind_test_native_start(engine, context)
        manager._terminalize_durable_turn(
            logical_turn_id, "completed", settled_by="terminal_result", evidence_kind="test_terminal",
        )

    manager._run = capture_run
    transport = httpx.ASGITransport(app=internal_app)

    async def dispatch(payload):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as internal:
            response = await internal.post("/internal/dispatch_async", json=payload)
        return {"status_code": response.status_code, "body": response.json()}

    monkeypatch.setattr("vibe.internal_client.dispatch_async", dispatch)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    draft = client.put(
        f"/api/sessions/{session_id}/draft", headers=headers,
        json={"text": "未发送的草稿", "expected_updated_at": None},
    ).get_json()["draft"]
    url = f"/api/sessions/{session_id}/messages"
    first = client.post(url, json={"retry_for": notice["id"], "text": "repeat everything"}, headers=headers)
    duplicate = client.post(url, json={"retry_for": notice["id"]}, headers=headers)
    assert first.status_code == duplicate.status_code == 201
    assert first.get_json()["id"] == duplicate.get_json()["id"]
    assert first.get_json()["draft"] == draft
    assert first.get_json()["draft_advanced"] is False
    assert first.get_json()["retry_notice"]["content"]["failure_retry"]["state"] == "accepted"
    assert starts == [(session_id, "continue")]
    with engine.connect() as conn:
        reloaded = messages_service.get_message(conn, notice["id"])
    assert reloaded["text"] == notice["text"]
    assert reloaded["content"]["failure_retry"]["state"] == "accepted"


def _request_stop(engine, _session_id, turn_id):
    """The user pressed Stop as the service went down."""
    with engine.begin() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
        assert message_deliveries.cas_turn(
            conn, turn_id, expected_version=int(turn["version"]), expected_states=("active",),
            values={"control_mode": "stop_only"},
        )


def _then(*steps):
    def run(engine, session_id, turn_id):
        for step in steps:
            step(engine, session_id, turn_id)

    return run


def _continuations(engine, session_id):
    """The Session's restart continuations, by the binding their durable history carries."""
    with engine.connect() as conn:
        return [
            dict(row) for row in conn.execute(
                select(message_deliveries_table).where(message_deliveries_table.c.session_id == session_id)
            ).mappings()
            if any(
                event.get("kind") == "restart_resume"
                for event in json.loads(row["delivery_history_json"])["events"]
            )
        ]


@pytest.mark.parametrize("step", ["tool", "handover", "partial", "running"])
def test_restart_continues_a_cut_vibey_turn_from_its_transcript_without_a_notice(isolated_state, tmp_path, step):
    """MESSAGE-DELIVERY-327 / MESSAGE-DELIVERY-330: one hidden continuation, no notice;
    a committed handover does not stop it, a command still running is left to it, not
    handed over at startup, and a failed stream's partial text is not a final reply."""
    from core.agent_core.tools.base import JobStatus
    from tests.agent_core.fakes import FakeJobHost

    jobs = None
    if step == "running":
        jobs = FakeJobHost()
        jobs.states["job_1"] = JobStatus("running")
    cut = _accepted_turn(tmp_path, backend="vibey", jobs=jobs)

    _restart(cut, _previous_process(step))

    if jobs is not None:
        assert jobs.watches == {}
        with cut.engine.connect() as conn:
            assert not conn.execute(
                select(agent_events.c.id).where(
                    agent_events.c.session_id == cut.session_id, agent_events.c.event_type == "tool_result"
                )
            ).first()

    [continuation] = _continuations(cut.engine, cut.session_id)
    with cut.engine.connect() as conn:
        payload = message_deliveries.execution_delivery_payload(conn, continuation)
    assert message_deliveries.is_vibey_resume_payload(payload)
    assert payload["metadata"]["vibey_source_turn_id"] == cut.turn_id
    assert payload["text"] == ""
    # Started once, as a transcript resume of the cut Turn: no input, nothing replayed.
    assert cut.starts == [(cut.session_id, "")]
    [started] = cut.contexts
    assert started.platform_specific["vibey_resume"] is True
    assert started.platform_specific["vibey_source_turn_id"] == cut.turn_id
    assert started.platform_specific["delivery_id"] == continuation["id"]
    with cut.engine.connect() as conn:
        source = message_deliveries.get_turn(conn, cut.turn_id)
        resumed = message_deliveries.get_turn(conn, started.platform_specific["turn_token"])
        users = [
            row for row in messages_service.list_session_messages(conn, session_id=cut.session_id)["messages"]
            if row["type"] == "user"
        ]
    assert source["settled_by"] == "restart_resumed"
    assert source["terminal_outcome"] == "completed"
    assert resumed["state"] == "active" and resumed["initial_delivery_id"] == continuation["id"]
    assert _notices(cut.engine, cut.session_id) == []
    assert [row["text"] for row in users] == ["完成剩余工作，不要重复提交"]


@pytest.mark.parametrize(
    "origin,delivery,late_stop",
    [
        (None, "ready", False),
        ("1760000000.000100", "ready", False),
        ("1760000000.000100", "unready", False),
        ("1760000000.000100", "failing", False),
        (None, "ready", True),
    ],
)
def test_restart_completes_a_cut_vibey_turn_whose_final_reply_was_committed(
    isolated_state, tmp_path, origin, delivery, late_stop
):
    """MESSAGE-DELIVERY-327: nothing is left to continue, and nothing failed, even when a Stop
    landed after the final (it loses the race, as live). The 👀 the dead process left on an IM
    message is retired once its transport can deliver, and a failed attempt is kept for the next."""
    cut = _accepted_turn(tmp_path, backend="vibey", delivery=delivery, native_message_id=origin)
    clear = AsyncMock(side_effect=[False, True] if delivery == "failing" else None, return_value=True)
    cut.manager.controller.processing_indicator = SimpleNamespace(clear_orphaned_running_reaction=clear)

    _restart(cut, _then(_previous_process("final"), _request_stop) if late_stop else _previous_process("final"))
    if delivery in {"unready", "failing"}:
        assert clear.await_count == (1 if delivery == "failing" else 0)
        cut.manager._transport_can_deliver = lambda _platform: True
        assert asyncio.run(cut.manager.notify_transport_ready("avibe")) == 0

    expected = [origin] * (2 if delivery == "failing" else 1) if origin else []
    assert [call.args[1] for call in clear.await_args_list] == expected
    assert cut.manager._pending_orphan_reaction_clears.get("avibe", []) == []

    with cut.engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, cut.turn_id)
    assert (turn["terminal_outcome"], turn["settled_by"]) == ("completed", "terminal_result")
    assert cut.starts == []
    assert _continuations(cut.engine, cut.session_id) == []
    assert _notices(cut.engine, cut.session_id) == []


def _switch_off_vibey(engine, session_id, _turn_id):
    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(agent_backend="claude"))


def _archive(engine, session_id, _turn_id):
    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(status="archived"))


@pytest.mark.parametrize("case", ["qualifies", "stop", "switched", "archived", "bound", "not_admitted"])
def test_a_cut_vibey_turn_that_no_longer_qualifies_keeps_the_restart_notice(isolated_state, tmp_path, case):
    """MESSAGE-DELIVERY-329: Stop, an Agent off Vibey, an archived Session, a third restart
    in a row without a model response, or a continuation that cannot start now (its runtime
    not admitted) keep today's settlement and notice; no continuation waits unclaimed."""
    from core.runtime_activation import RuntimeActivationRegistry, RuntimeActivationResolution

    cut = _accepted_turn(tmp_path, backend="vibey")
    if case == "not_admitted":
        service = cut.manager.controller.agent_service
        service.activation_registry = RuntimeActivationRegistry()
        service.runtime_activation_identity_for_session_binding = (
            lambda *_args, **_kwargs: RuntimeActivationResolution(authoritative=False)
        )
    restarts = 1
    before = {
        "stop": _then(_previous_process("tool"), _request_stop),
        "switched": _then(_previous_process("tool"), _switch_off_vibey),
        "archived": _then(_previous_process("tool"), _archive),
    }.get(case, _previous_process("tool"))
    if case == "bound":
        # Each continuation dies before the model answers: three are allowed.
        restarts = 4
    _restart(cut, before)
    for _ in range(restarts - 1):
        cut.turn_id = cut.contexts[-1].platform_specific["turn_token"]
        _restart(cut)

    continued = case in {"qualifies", "bound"}
    assert len(_continuations(cut.engine, cut.session_id)) == (3 if case == "bound" else int(continued))
    with cut.engine.connect() as conn:
        last = message_deliveries.get_turn(conn, cut.turn_id)
    if case == "qualifies":
        assert last["settled_by"] == "restart_resumed"
        assert _notices(cut.engine, cut.session_id) == []
        return
    assert (last["terminal_outcome"], last["settled_by"]) == ("failed", "no_terminal_result")
    assert last["terminal_evidence_kind"] == "restart_runtime_missing"
    if case != "archived":
        [notice] = _notices(cut.engine, cut.session_id)
        assert notice["metadata"]["turn_id"] == cut.turn_id


def test_restart_continuation_runs_before_input_queued_while_the_service_was_down(isolated_state, tmp_path):
    """MESSAGE-DELIVERY-331: the queued input waits behind the continuation, then runs."""
    cut = _accepted_turn(tmp_path, backend="vibey")

    def queue_input(engine, session_id, _turn_id):
        with engine.begin() as conn:
            message_deliveries.enqueue_queued(
                conn, scope_id=None, session_id=session_id, text="然后再看看日志",
            )

    _restart(cut, _then(_previous_process("tool"), queue_input))

    assert cut.starts == [(cut.session_id, "")]
    resumed = cut.contexts[0].platform_specific["turn_token"]
    with cut.engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, cut.session_id)] == ["然后再看看日志"]
    assert asyncio.run(cut.manager.terminalize_turn(resumed))
    assert cut.starts == [(cut.session_id, ""), (cut.session_id, "然后再看看日志")]


def test_restart_notice_names_the_watch_running_the_command_and_offers_no_retry(isolated_state, tmp_path):
    """MESSAGE-DELIVERY-322: resending a command that still runs as a Watch would run its effects twice."""
    from vibe.ui_server import app

    from core.agent_core.tools.base import JobStatus
    from tests.agent_core.fakes import FakeJobHost

    jobs = FakeJobHost()
    jobs.states["job_1"] = JobStatus("running")
    # Startup's pass leaves the command to the Turn's owner, which hands it over before its notice.
    session_id, notice = _restart_failure_notice(
        tmp_path, backend="vibey", jobs=jobs, before_restart=_then(_previous_process("running"), _request_stop)
    )
    assert jobs.watches == {"job_1": "watch_job_1"}

    assert "watch_job_1" in notice["text"] and "重新发送" not in notice["text"]
    assert notice["metadata"]["watch_ids"] == ["watch_job_1"]
    client = app.test_client()
    with patch("vibe.internal_client.dispatch_async", AsyncMock()) as dispatch:
        response = client.post(
            f"/api/sessions/{session_id}/messages", headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 409
    dispatch.assert_not_awaited()


def test_a_restart_notice_waits_until_its_running_command_is_handed_over(isolated_state, tmp_path, monkeypatch):
    """MESSAGE-DELIVERY-322: with no Watch to take the command yet, the Turn stays open (T2 before
    T4): no notice without the Watch, nothing failed under a running command; the retry hands it
    over and the notice then names it."""
    from core.agent_core.tools.base import JobStatus
    from core.session_turns import SessionTurnManager
    from tests.agent_core.fakes import FakeJobHost

    jobs = FakeJobHost()
    jobs.states["job_1"] = JobStatus("running")
    attempts = []
    hand_over = jobs.hand_over

    async def locked_once(job_id):
        attempts.append(job_id)
        if len(attempts) == 1:
            raise RuntimeError("the Watch store is locked")
        return await hand_over(job_id)

    jobs.hand_over = locked_once
    monkeypatch.setattr(SessionTurnManager, "RESTART_FALLBACK_RETRY_DELAYS", (0.05,))
    cut = _accepted_turn(tmp_path, backend="vibey", jobs=jobs)
    _then(_previous_process("running"), _request_stop)(cut.engine, cut.session_id, cut.turn_id)
    cut.manager._active_identity = lambda *_args: None

    async def startup():
        assert await cut.manager.controller.vibey_recovery.start() == []
        await cut.manager.recover_durable_delivery_state(cut.session_id, service_restart=True)
        with cut.engine.connect() as conn:
            assert message_deliveries.get_turn(conn, cut.turn_id)["state"] == "active"
        assert _notices(cut.engine, cut.session_id) == []
        for _ in range(100):
            if _notices(cut.engine, cut.session_id):
                break
            await asyncio.sleep(0.02)

    asyncio.run(startup())

    [notice] = _notices(cut.engine, cut.session_id)
    assert notice["metadata"]["watch_ids"] == ["watch_job_1"] and jobs.watches == {"job_1": "watch_job_1"}
    with cut.engine.connect() as conn:
        assert message_deliveries.get_turn(conn, cut.turn_id)["settled_by"] == "no_terminal_result"


@pytest.mark.parametrize("change", ["newer", "busy", "unlinked"])
def test_restart_notice_keeps_retry_boundary_guards(isolated_state, tmp_path, change):
    from vibe.ui_server import app

    session_id, notice = _restart_failure_notice(tmp_path, backend="codex")
    with create_sqlite_engine().begin() as conn:
        if change == "newer":
            seed_failed_notice(conn, session_id=session_id, scope_id=notice["scope_id"])
        elif change == "busy":
            message_deliveries.insert_delivery(
                conn, delivery_id=message_deliveries.new_delivery_id(),
                session_id=session_id, priority="p3", state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=notice["scope_id"], session_id=session_id, platform="avibe",
                    author="user", source="user", text="另一个请求",
                ),
                dispatch_text="另一个请求",
            )
        else:
            conn.execute(update(messages).where(messages.c.id == notice["id"]).values(metadata_json="{}"))
    client = app.test_client()
    with patch("vibe.internal_client.dispatch_async", AsyncMock()) as dispatch:
        response = client.post(
            f"/api/sessions/{session_id}/messages", headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 409
    dispatch.assert_not_awaited()


def _retry_notice(tmp_path, *, backend="claude", not_written=False, content=None):
    scope_id, session_id = _make_session(tmp_path, agent_backend=backend)
    with create_sqlite_engine().begin() as conn:
        notice, turn, inputs = seed_failed_notice(
            conn,
            session_id=session_id,
            scope_id=scope_id,
            backend=backend,
            not_written=not_written,
            content=content,
        )
    return session_id, notice, turn, inputs


@pytest.mark.parametrize("can_manage", [True, False])
def test_retry_notice_preserves_authorized_local_details(isolated_state, tmp_path, monkeypatch, can_manage):
    # Retry replaces the existing UI row from its POST reply/event. A default
    # public reload used to erase the only snapshot when provenance is absent.
    from types import SimpleNamespace
    from vibe import ui_server
    from vibe.sse_broker import broker

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path)
    detail = "[Errno 28] No space left on device"
    with create_sqlite_engine().begin() as conn:
        conn.execute(
            update(messages).where(messages.c.id == notice["id"]).values(
                metadata_json=json.dumps({**notice["metadata"], "local_error_detail": detail}),
            )
        )
    monkeypatch.setattr(ui_server, "_has_runtime_management_access", lambda _context: can_manage)
    published = []
    monkeypatch.setattr(broker, "publish", lambda event, data: published.append((event, data)))
    client = ui_server.app.test_client()
    with patch("vibe.internal_client.dispatch_async", _accepted_dispatch(session_id)):
        response = client.post(
            f"/api/sessions/{session_id}/messages", headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 201
    returned = response.get_json()["retry_notice"]
    assert ("local_error_detail" in returned["metadata"]) is can_manage
    if can_manage:
        assert returned["metadata"]["local_error_detail"] == detail
    event, internal = next((event, data) for event, data in published if event == "message.updated")
    assert internal["metadata"]["local_error_detail"] == detail
    projected = ui_server._workbench_event_payload_for_context(
        SimpleNamespace(), event, json.dumps({"type": event, "data": internal}),
    )
    assert ("local_error_detail" in json.loads(projected)["data"]["metadata"]) is can_manage


@pytest.mark.parametrize("backend", AGENT_BACKENDS)
def test_failure_retry_canonical_continue_and_draft(isolated_state, tmp_path, backend):
    from vibe.ui_server import app

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path, backend=backend)
    client = app.test_client()
    headers = csrf_headers(client)
    draft = client.put(
        f"/api/sessions/{session_id}/draft",
        headers=headers,
        json={"text": "不要丢掉这段草稿", "expected_updated_at": None},
    ).get_json()["draft"]
    dispatch = _accepted_dispatch(session_id)
    with patch("vibe.internal_client.dispatch_async", dispatch):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=headers,
            json={
                "retry_for": notice["id"],
                "text": "replay everything",
                "content": {"attachments": [{"token": "forged"}]},
                "metadata": {"quick_reply_for": "forged", "resource_user_context": {}},
            },
        )
    assert response.status_code == 201
    body = response.get_json()
    expected_text = "" if backend == "vibey" else "continue"
    assert body["text"] == expected_text
    if backend == "vibey":
        assert body["type"] == "agent_initiated"
        assert body["metadata"]["vibey_resume"] is True
    assert body["draft"] == draft
    assert body["retry_notice"]["content"]["failure_retry"]["state"] == "accepted"
    assert dispatch.await_args.args[0]["files"] == []
    with create_sqlite_engine().connect() as conn:
        reloaded = messages_service.list_session_messages(conn, session_id=session_id)
    persisted = next(row for row in reloaded["messages"] if row["id"] == notice["id"])
    assert persisted["content"]["failure_retry"]["state"] == "accepted"


@pytest.mark.parametrize("exception_name", ["InternalServerTimeout", "InternalServerUnavailable"])
def test_failure_retry_dispatch_loss(isolated_state, tmp_path, exception_name):
    from vibe import internal_client
    from vibe.ui_server import app

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path)
    client = app.test_client()
    headers = csrf_headers(client)
    url = f"/api/sessions/{session_id}/messages"
    with patch("vibe.internal_client.dispatch_async", side_effect=getattr(internal_client, exception_name)("offline")):
        failed = client.post(url, headers=headers, json={"retry_for": notice["id"]})
    assert failed.status_code in {502, 504}
    first_id = failed.get_json()["id"]
    with patch("vibe.internal_client.dispatch_async", _accepted_dispatch(session_id)):
        recovered = client.post(url, headers=headers, json={"retry_for": notice["id"]})
    assert recovered.status_code == 201
    assert (recovered.get_json()["id"] == first_id) is (exception_name == "InternalServerTimeout")
    with create_sqlite_engine().connect() as conn:
        visible = messages_service.list_session_messages(conn, session_id=session_id)["messages"]
    assert sum(row["text"] == "continue" for row in visible) == 1


@pytest.mark.parametrize("change", ["ordinary", "detached", "unlinked", "wrong_session", "stopped", "unknown", "newer"])
def test_failure_retry_invalid_or_stale_notice(isolated_state, tmp_path, change):
    from vibe.ui_server import app

    session_id, notice, turn, _inputs = _retry_notice(tmp_path)
    with create_sqlite_engine().begin() as conn:
        meta = dict(notice["metadata"])
        if change == "ordinary":
            meta["event"] = "progress"
        elif change == "detached":
            meta["detached"] = True
        elif change == "unlinked":
            meta.pop("turn_id")
        elif change == "wrong_session":
            meta["turn_id"] = "trn_missing"
        elif change == "stopped":
            conn.execute(
                update(session_turns).where(session_turns.c.id == turn["id"]).values(terminal_outcome="canceled")
            )
        elif change == "unknown":
            conn.execute(
                update(session_turns).where(session_turns.c.id == turn["id"]).values(start_receipt_outcome="unknown")
            )
        elif change == "newer":
            seed_failed_notice(conn, session_id=session_id, scope_id=notice["scope_id"], backend="claude")
        conn.execute(update(messages).where(messages.c.id == notice["id"]).values(metadata_json=json.dumps(meta)))
    client = app.test_client()
    with patch("vibe.internal_client.dispatch_async", AsyncMock()) as dispatch:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 409
    dispatch.assert_not_awaited()


def test_failure_retry_retains_prompt_and_attachment(isolated_state, tmp_path):
    from vibe.ui_server import app

    session_id, notice, _turn, inputs = _retry_notice(
        tmp_path,
        not_written=True,
        content={"attachments": [{"token": "server-owned-token", "name": "中文.png"}]},
    )

    async def dispatch(payload):
        assert payload["message_id"] == inputs[0]["id"]
        assert payload["text"] == "检查这张图"
        assert payload["files"] == [{"path": "/isolated/中文.png"}]
        return {"status_code": 202, "body": {"delivery_state": "queued", "queued": True}}

    client = app.test_client()
    with (
        patch("vibe.internal_client.dispatch_async", side_effect=dispatch),
        patch(
            "core.workbench_media.resolve_attachment_specs", return_value=[{"path": "/isolated/中文.png"}]
        ) as resolve,
    ):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 202
    assert resolve.call_args.kwargs["attachments"][0]["token"] == "server-owned-token"
    with create_sqlite_engine().connect() as conn:
        retained = message_deliveries.get_delivery(conn, inputs[0]["id"])
    assert retained["snapshot_json"] == inputs[0]["snapshot_json"]


def test_failure_retry_concurrent_clicks_have_one_owner(managers):
    """MESSAGE-DELIVERY-027: duplicate notice clicks share one native start."""
    manager_a, manager_b, engine_a, engine_b, starts = managers
    with engine_a.begin() as conn:
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm")

    def reserve(engine):
        with engine.begin() as conn:
            reserve_write_lock(conn)
            return reserve_failure_retry(conn, notice)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, [engine_a, engine_b]))
    assert results[0]["id"] == results[1]["id"]

    async def admit_both():
        await asyncio.gather(
            *[
                manager.deliver(
                    DeliveryRequest(
                        session_id="ses_fsm",
                        priority="p3",
                        content="forged",
                        delivery_id=results[0]["id"],
                    ),
                    context=_context(),
                )
                for manager in [manager_a, manager_b]
            ]
        )

    asyncio.run(admit_both())
    assert len(starts) == 1
    assert starts[0][1] == "continue"


@pytest.mark.parametrize("new_work", ["reserved", "active", "completed"])
def test_failure_retry_admission_rejects_newer_work(managers, new_work):
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm")
        retry = reserve_failure_retry(conn, notice)
        if new_work == "reserved":
            message_deliveries.insert_delivery(
                conn,
                delivery_id=message_deliveries.new_delivery_id(),
                session_id="ses_fsm",
                priority="p3",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=None, session_id="ses_fsm", platform="avibe", author="user", source="user", text="new task"
                ),
                dispatch_text="new task",
            )
        else:
            _notice, newer, _inputs = seed_failed_notice(conn, session_id="ses_fsm")
            if new_work == "active":
                conn.execute(
                    update(session_turns)
                    .where(session_turns.c.id == newer["id"])
                    .values(
                        state="active",
                        terminal_outcome=None,
                        terminal_at=None,
                    )
                )
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry["id"]),
            context=_context(),
        )
    )
    assert result.state == "retired"
    assert result.reason in {"retry_stale", "retry_pending_input"}
    assert starts == []


@pytest.mark.parametrize("texts", [["原输入"], ["原输入一", "原输入二"]])
def test_failure_retry_unwritten_original_batch(managers, texts):
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        notice, _turn, originals = seed_failed_notice(
            conn,
            session_id="ses_fsm",
            not_written=True,
            texts=texts,
        )
        retry = reserve_failure_retry(conn, notice)
    assert retry["id"] == originals[0]["id"]
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry["id"]),
            context=_context(),
        )
    )
    assert result.state == "claimed"
    assert len(starts) == 1
    assert all(text in starts[0][1] for text in texts)
    assert "continue" not in starts[0][1]


@pytest.mark.parametrize("backend", AGENT_BACKENDS)
@pytest.mark.parametrize("not_written", [False, True])
def test_failure_retry_web_controller_round_trip(isolated_state, tmp_path, monkeypatch, not_written, backend):
    """MESSAGE-DELIVERY-026: include hidden resume across both real HTTP consumers.

    A mocked internal client misses empty-input rejection and loss of durable
    provenance when submit constructs a user DeliveryRequest.
    """
    import httpx
    from core import internal_server
    from tests.test_internal_server import _bind_test_native_start, _build_controller_double
    from vibe import ui_server

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path, backend=backend, not_written=not_written)
    engine = create_sqlite_engine()
    controller = _build_controller_double()
    internal_app = internal_server.create_app(controller)
    manager = controller.session_turns
    starts = []
    contexts = []

    async def capture_run(sid, context, text, *, logical_turn_id=None, **kwargs):
        starts.append((sid, text))
        contexts.append(context)
        _bind_test_native_start(engine, context)
        manager._terminalize_durable_turn(
            logical_turn_id,
            "completed",
            settled_by="terminal_result",
            evidence_kind="test_terminal",
        )

    manager._run = capture_run
    transport = httpx.ASGITransport(app=internal_app)

    async def dispatch(payload):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as internal:
            response = await internal.post("/internal/dispatch_async", json=payload)
        return {"status_code": response.status_code, "body": response.json()}

    monkeypatch.setattr("vibe.internal_client.dispatch_async", dispatch)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    url = f"/api/sessions/{session_id}/messages"
    first = client.post(
        url, json={"retry_for": notice["id"], "text": "不要重放工具调用", "metadata": {"vibey_resume": False}},
        headers=headers,
    )
    duplicate = client.post(url, json={"retry_for": notice["id"]}, headers=headers)
    assert first.status_code == duplicate.status_code == 201
    assert first.get_json()["id"] == duplicate.get_json()["id"]
    resume = backend == "vibey" and not not_written
    assert starts == [(session_id, "检查这张图" if not_written else "" if resume else "continue")]
    assert contexts[0].platform_specific["vibey_resume"] is resume
    assert contexts[0].platform_specific["vibey_source_turn_id"] == (_turn["id"] if resume else None)
    with engine.connect() as conn:
        accepted = messages_service.get_message(conn, first.get_json()["id"])
        retry = message_deliveries.get_delivery(conn, first.get_json()["id"])
    assert retry["state"] == "accepted"
    assert accepted["type"] == ("agent_initiated" if resume else "user")


@pytest.mark.parametrize("change", ["newer_turn", "backend_after_unwritten_start"])
def test_failure_retry_queue_drain_rechecks_the_boundary(managers, change):
    """MESSAGE-DELIVERY-028: revalidate a real retry after its queue wait.

    Once a start was claimed, no-write recovery belongs to the Turn FSM rather
    than the notice's unclaimed-retry guard. That path still must not dispatch a
    hidden resume to a newly selected native backend.
    """
    manager, _other, engine, _engine_b, starts = managers
    backend = "codex" if change == "newer_turn" else "vibey"

    def context(sid):
        value = _context(sid)
        value.platform_specific["agent_session_target"]["agent_backend"] = backend
        return value

    manager.bind_context(context)
    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == "ses_fsm").values(agent_backend=backend))
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm", backend=backend)
        retry = reserve_failure_retry(conn, notice)

    async def queue_then_drain():
        nonlocal backend
        if change == "newer_turn":
            manager.begin_backend_drain(backend)
        admitted = await manager.deliver(manager._request_from_delivery(retry), context=context("ses_fsm"))
        if change == "newer_turn":
            assert admitted.state == "queued"
            with engine.begin() as conn:
                seed_failed_notice(conn, session_id="ses_fsm")
            await manager.end_backend_drain(backend)
        else:
            assert admitted.state == "claimed"
            assert [text for _turn, text in starts] == [""]
            manager._terminalize_durable_turn(
                admitted.turn_id, "not_written", settled_by="pre_write_failure",
                evidence_kind="definitive_prewrite_failure",
            )
            starts.clear()
            backend = "opencode"
            with engine.begin() as conn:
                queued = message_deliveries.get_delivery(conn, retry["id"])
                assert queued["state"] == "queued"
                assert message_deliveries.failure_retry_binding(queued, unclaimed_only=True) is None
                conn.execute(update(agent_sessions).where(agent_sessions.c.id == "ses_fsm").values(agent_backend=backend))
            await manager.drain_delivery_queue("ses_fsm")

    asyncio.run(queue_then_drain())
    with engine.connect() as conn:
        assert message_deliveries.get_delivery(conn, retry["id"])["state"] == "retired"
    assert starts == []


@pytest.mark.parametrize("restriction", ["archived", "access", "cross_session"])
def test_failure_retry_keeps_session_access_gates(isolated_state, tmp_path, restriction):
    from core.services import sessions
    from storage.models import agent_sessions
    from vibe.ui_server import app

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path)
    with create_sqlite_engine().begin() as conn:
        if restriction == "archived":
            conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(status="archived"))
        elif restriction == "cross_session":
            original = sessions.get_session(conn, session_id)
            session_id = sessions.create_session(
                conn,
                scope_id=notice["scope_id"],
                agent_backend="claude",
                agent_id=original["agent_id"],
                agent_name=original["agent_name"],
            )["id"]
    client = app.test_client()
    with (
        patch("vibe.internal_client.dispatch_async", AsyncMock()) as dispatch,
        patch(
            "core.vibe_agents.ensure_session_agent_access",
            side_effect=PermissionError("denied") if restriction == "access" else None,
        ),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == (403 if restriction == "access" else 409)
    dispatch.assert_not_awaited()


def test_unwritten_retry_remains_clickable_when_controller_cannot_be_reached(isolated_state, tmp_path):
    from vibe import internal_client, ui_server

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path, not_written=True)
    client = ui_server.app.test_client()
    with patch("vibe.internal_client.dispatch_async", side_effect=internal_client.InternalServerUnavailable("offline")):
        response = client.post(
            f"/api/sessions/{session_id}/messages", headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 502
    assert response.get_json()["retry_notice"]["content"]["failure_retry"]["state"] == "reserved"
    with create_sqlite_engine().connect() as conn:
        persisted = messages_service.get_message(conn, notice["id"])
    assert persisted["content"]["failure_retry"]["state"] == "reserved"


def test_stale_retry_releases_a_newer_input_waiting_behind_its_reservation(managers):
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm")
        retry = reserve_failure_retry(conn, notice)

    async def admit():
        newer = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="new task"),
            context=_context(),
        )
        assert newer.state == "queued"  # the reserved retry is an ordering fence
        refused = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry["id"]),
            context=_context(),
        )
        assert refused.state == "retired"
        assert refused.turn_id is None

    asyncio.run(admit())
    assert len(starts) == 1
    assert starts[0][1] == "new task"
