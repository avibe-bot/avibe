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
    from core.agent_core.tools.bash import settle_in_foreground
    from modules.agents.vibey import VibeyAgent
    from modules.agents.vibey.recovery import VibeyRecovery
    from modules.agents.vibey.tools import ToolSuite

    async def handed_over(_call, _job_id, _status, watch_id):
        return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

    suite = ToolSuite(
        jobs=jobs, create_tools=lambda *_args: [], render_recovered=handed_over,
        find_job=lambda call: "job_1" if call.tool_call_id == "call_bash" else None,
        wait_recovered=lambda call, job_id, stop: settle_in_foreground(jobs, call, job_id, stop=stop),
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
    import secrets

    from core import restart_resume

    if before_restart is not None:
        before_restart(turn.engine, turn.session_id, turn.turn_id)
    restart_resume.PROCESS_TOKEN = secrets.token_hex(8)  # a new process starts
    turn.manager._active_identity = lambda *_args: None
    turn.manager._transport_can_deliver = lambda _platform: turn.delivery != "unready"
    recovery = getattr(turn.manager.controller, "vibey_recovery", None)

    async def startup():
        # The controller's order: Vibey's T2 pass, then Turn recovery of every Session.
        if recovery is not None:
            assert await recovery.start() == []
        await turn.manager.recover_durable_delivery_state(service_restart=True)

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


def _deliveries(engine, session_id):
    with engine.connect() as conn:
        return conn.execute(
            select(message_deliveries_table.c.id).where(message_deliveries_table.c.session_id == session_id)
        ).scalars().all()


def _respond(engine, session_id, turn_id):
    """The rerun answered: one more model response (a tool call and its result) in the Turn."""
    from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
    from storage.agent_transcript import SQLiteTranscriptStore
    from tests.agent_core.fakes import assistant

    store = SQLiteTranscriptStore(engine)

    async def respond():
        call = ToolCallBlock(id="call_read", name="read", arguments={"path": "notes.md"})
        await store.append_response(session_id, assistant("", calls=(call,)), final=False, turn_id=turn_id)
        await store.append_tool_result(
            session_id, ToolResultMessage(tool_call_id=call.id, tool_name=call.name, content=(text("x"),)), details={}
        )

    asyncio.run(respond())


@pytest.mark.parametrize("step", ["model", "tool", "handover", "partial", "running", "final"])
def test_restart_reruns_a_cut_vibey_turn_in_place_without_a_notice(isolated_state, tmp_path, step):
    """MESSAGE-DELIVERY-327 / MESSAGE-DELIVERY-330 / MESSAGE-DELIVERY-333: the same Turn reruns as a
    transcript resume of itself, with the Agent it ran with: no Delivery, no successor Turn, no
    notice. A committed handover does not stop it, a command still running is left to it, not
    handed over at startup, a failed stream's partial text is not a final reply, and a committed
    final reruns too, for its own end to deliver it. The Agent and request are the ones its
    native start recorded, the Agent found by id, even when it was cut before its first response or
    renamed since."""
    from core.agent_core.tools.base import JobStatus
    from tests.agent_core.fakes import FakeJobHost

    jobs = None
    if step == "running":
        jobs = FakeJobHost()
        jobs.states["job_1"] = JobStatus("running")
    from core.vibe_agents import VibeAgent

    cut = _accepted_turn(tmp_path, backend="vibey", jobs=jobs)
    before = _deliveries(cut.engine, cut.session_id)
    agents = {
        f"agt_{name}": VibeAgent(id=f"agt_{name}", name=name, normalized_name=name, backend="vibey")
        for name in ("worker", "reviewer")
    }
    cut.manager.controller.vibe_agent_store = SimpleNamespace(
        get=lambda name: next((agent for agent in agents.values() if agent.name == name), None),
        get_by_id=agents.get,
    )
    # The Vibey run's native start, as its adapter accepts it: it names the Agent it runs with.
    cut.manager.controller._session_id_from_context = lambda ctx: (ctx.platform_specific or {}).get(
        "agent_session_id"
    )
    accepted = cut.context(cut.session_id)
    execution = {
        "agent_id": "agt_worker", "agent_name": "worker", "model": "model-accepted",
        "reasoning_effort": "high", "system_prompt": "Review carefully.",
    }
    accepted.platform_specific.update(
        {"turn_token": cut.turn_id, "agent_session_id": cut.session_id, "vibey_execution": execution}
    )
    cut.manager.on_native_start(
        accepted, backend="vibey", runtime_key=f"runtime:{cut.turn_id}", runtime_turn_id=f"runtime-turn:{cut.turn_id}"
    )

    def switch_agent(engine, session_id, _turn_id):
        # The Session moved to another Vibey Agent while the Turn ran: that reaches the next Turn.
        # The Turn's own Agent was renamed meanwhile; its id still finds it.
        with engine.begin() as conn:
            conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(agent_name="reviewer"))
        agents["agt_worker"] = VibeAgent(id="agt_worker", name="builder", normalized_name="builder", backend="vibey")

    rotate, rotated_on_loop = message_deliveries.rotate_restart_generation, []

    def rotate_where(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            rotated_on_loop.append(True)
        except RuntimeError:
            rotated_on_loop.append(False)
        return rotate(*args, **kwargs)

    message_deliveries.rotate_restart_generation = rotate_where
    try:
        _restart(cut, _then(_previous_process(step), switch_agent))
    finally:
        message_deliveries.rotate_restart_generation = rotate
    # Its writer transaction takes SQLite's write lock: off the controller loop.
    assert rotated_on_loop == [False]

    if jobs is not None:
        assert jobs.watches == {}
        with cut.engine.connect() as conn:
            assert not conn.execute(
                select(agent_events.c.id).where(
                    agent_events.c.session_id == cut.session_id, agent_events.c.event_type == "tool_result"
                )
            ).first()
    # Started once, as a transcript resume of the cut Turn itself: no input, nothing replayed.
    assert cut.starts == [(cut.session_id, "")]
    [started] = cut.contexts
    assert started.platform_specific["turn_token"] == cut.turn_id
    assert started.platform_specific["vibey_resume"] is True
    assert started.platform_specific["vibey_source_turn_id"] == cut.turn_id
    assert started.platform_specific["delivery_id"] == before[0]
    assert (started.platform_specific["vibe_agent_name"], started.platform_specific["vibe_agent_id"]) == (
        "builder", "agt_worker",
    )
    # With the request it was accepted with: Agent settings edited since reach the next Turn.
    assert started.platform_specific["vibey_execution"] == execution
    assert _deliveries(cut.engine, cut.session_id) == before
    with cut.engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, cut.turn_id)
        users = [
            row for row in messages_service.list_session_messages(conn, session_id=cut.session_id)["messages"]
            if row["type"] == "user"
        ]
    assert (turn["state"], turn["start_receipt_outcome"]) == ("active", "accepted")
    assert json.loads(turn["start_receipt_json"])["restart_streak"] == 1
    assert _notices(cut.engine, cut.session_id) == []
    assert [row["text"] for row in users] == ["完成剩余工作，不要重复提交"]


def _switch_off_vibey(engine, session_id, _turn_id):
    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(agent_backend="claude"))


def _archive(engine, session_id, _turn_id):
    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(status="archived"))


@pytest.mark.parametrize(
    "case",
    [
        "qualifies", "unready", "final_again", "stop", "switched", "archived", "agent_gone", "agent_unknown",
        "bound", "reset",
    ],
)
def test_a_cut_vibey_turn_that_no_longer_qualifies_keeps_the_restart_notice(isolated_state, tmp_path, case):
    """MESSAGE-DELIVERY-329: Stop, an Agent off Vibey, an archived Session, the Agent that ran the
    Turn no longer on Vibey or named nowhere (another Agent never continues its work), or the third
    restart in a row with no committed model response keep today's settlement and notice. A response
    the rerun committed resets that streak. A Turn whose final was committed stays open until its
    rerun ends it, so another restart before then reruns it again (MESSAGE-DELIVERY-333). A rerun
    waits for the surface it delivers to, as its notice would (MESSAGE-DELIVERY-327)."""
    from core.vibe_agents import VibeAgent

    cut = _accepted_turn(tmp_path, backend="vibey", delivery="unready" if case == "unready" else "ready")
    if case in {"agent_gone", "agent_unknown"}:
        # The Turn ran with ``worker``, archived since; the Session now selects ``reviewer``.
        agents = {
            "agt_worker": VibeAgent(
                id="agt_worker", name="worker", normalized_name="worker", backend="vibey",
                archived_at="2026-10-18T00:00:00Z",
            ),
            "agt_reviewer": VibeAgent(id="agt_reviewer", name="reviewer", normalized_name="reviewer", backend="vibey"),
        }
        cut.manager.controller.vibe_agent_store = SimpleNamespace(
            get=lambda name: next((agent for agent in agents.values() if agent.name == name), None),
            get_by_id=agents.get,
        )
    before = {
        "stop": _then(_previous_process("tool"), _request_stop),
        "switched": _then(_previous_process("tool"), _switch_off_vibey),
        "archived": _then(_previous_process("tool"), _archive),
        # An older release accepted it (no recorded request) and it was cut before any response.
        "agent_unknown": _previous_process("model"),
        "final_again": _previous_process("final"),
    }.get(case, _previous_process("tool"))
    _restart(cut, before)
    if case in {"bound", "reset"}:
        # The rerun dies before the model answers, twice; the third restart is the bound.
        _restart(cut, _respond if case == "reset" else None)
        _restart(cut)
    if case == "final_again":
        # The process dies again before the rerun ended the Turn.
        _restart(cut)
    if case == "unready":
        # Held, open, until its transport can deliver; the ready callback starts it.
        assert cut.starts == [] and _notices(cut.engine, cut.session_id) == []
        cut.manager._transport_can_deliver = lambda _platform: True
        assert asyncio.run(cut.manager.notify_transport_ready("avibe")) == 0

    reran = case in {"qualifies", "unready", "final_again", "reset"}
    assert len(cut.starts) == {"qualifies": 1, "unready": 1, "final_again": 2, "reset": 3, "bound": 2}.get(case, 0)
    with cut.engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, cut.turn_id)
    if reran:
        assert turn["state"] == "active"
        assert json.loads(turn["start_receipt_json"])["restart_streak"] == (2 if case in {"reset", "final_again"} else 1)
        assert _notices(cut.engine, cut.session_id) == []
        return
    assert (turn["terminal_outcome"], turn["settled_by"]) == ("failed", "no_terminal_result")
    assert turn["terminal_evidence_kind"] == "restart_runtime_missing"
    if case != "archived":
        [notice] = _notices(cut.engine, cut.session_id)
        assert notice["metadata"]["turn_id"] == cut.turn_id


@pytest.mark.parametrize("then", ["rerun", "stopped"])
def test_a_turn_waiting_for_its_steer_keeps_its_running_command_for_the_rerun(
    isolated_state, tmp_path, monkeypatch, then
):
    """MESSAGE-DELIVERY-330: while the Turn waits open for a steer receipt startup could not
    materialize, its running command stays with it: the retry only recovers again, and the rerun
    gets the command, with no handover in between. A Turn a Stop was asked of never reruns, so it
    does not wait for the steer: it settles with the notice at once and its command ends
    (MESSAGE-DELIVERY-329)."""
    from core.agent_core.tools.base import JobStatus
    from core.session_turns import SessionTurnManager
    from tests.agent_core.fakes import FakeJobHost

    jobs = FakeJobHost()
    jobs.states["job_1"] = JobStatus("running")
    monkeypatch.setattr(SessionTurnManager, "RESTART_RECOVERY_RETRY_DELAYS", (0.05,))
    monkeypatch.setattr(SessionTurnManager, "RESTART_RECOVERY_RETRY_PERIOD", 0.05)
    cut = _accepted_turn(tmp_path, backend="vibey", jobs=jobs)
    _previous_process("running")(cut.engine, cut.session_id, cut.turn_id)
    with cut.engine.begin() as conn:
        # A steer the dead run accepted, its receipt recorded and the Delivery not materialized.
        steer = message_deliveries.insert_delivery(
            conn, delivery_id=message_deliveries.new_delivery_id(), session_id=cut.session_id, priority="p1",
            state="reserved", dispatch_text="也看一下告警",
            snapshot=message_deliveries.message_snapshot(
                scope_id=None, session_id=cut.session_id, platform="avibe", author="user", source="user",
                text="也看一下告警",
            ),
        )
        turn = message_deliveries.get_turn(conn, cut.turn_id)
        opened = message_deliveries.open_steer_attempt(
            conn, steer["id"], expected_version=int(steer["version"]), turn_id=cut.turn_id,
            attempt_id="atm_steer", expected_native_turn_id=turn["native_turn_id"],
        )
        assert message_deliveries.mark_attempt_receipt(
            conn, steer["id"], expected_version=int(opened["version"]), outcome="accepted", receipt={},
        )
    finish, lost = cut.manager._finish_steer, []

    async def lost_once(*args, **kwargs):
        if not lost:
            lost.append(args[0])
            return SimpleNamespace(state="reconciling_steer")  # its CAS was lost
        return await finish(*args, **kwargs)

    cut.manager._finish_steer = lost_once
    cut.manager._active_identity = lambda *_args: None
    if then == "stopped":
        _request_stop(cut.engine, cut.session_id, cut.turn_id)

    async def startup():
        assert await cut.manager.controller.vibey_recovery.start() == []
        await cut.manager.recover_durable_delivery_state(service_restart=True)
        assert cut.starts == [] and lost and jobs.watches == {}
        if then == "stopped":
            with cut.engine.connect() as conn:
                turn = message_deliveries.get_turn(conn, cut.turn_id)
            assert (turn["state"], turn["terminal_evidence_kind"]) == ("terminal", "restart_runtime_missing")
            assert jobs.killed == ["job_1"] and len(_notices(cut.engine, cut.session_id)) == 1
            return
        for _ in range(100):
            if cut.starts:
                break
            await asyncio.sleep(0.02)

    asyncio.run(startup())
    if then == "stopped":
        assert cut.starts == [] and jobs.watches == {}
        return

    assert cut.starts == [(cut.session_id, "")]
    assert jobs.watches == {}
    with cut.engine.connect() as conn:
        assert message_deliveries.get_delivery(conn, steer["id"])["turn_id"] == cut.turn_id


def test_a_stop_ends_the_command_of_a_turn_held_for_its_rerun_at_once(isolated_state, tmp_path):
    """MESSAGE-DELIVERY-330: a cut Turn held open for its rerun (its transport not ready yet) has
    no runtime for a Stop to reach, so the Stop settles it directly; its retry wakes at once and
    T2 ends the surviving command, as the Stop would have live, without waiting out its delay."""
    from core.agent_core.tools.base import JobStatus
    from tests.agent_core.fakes import FakeJobHost

    jobs = FakeJobHost()
    jobs.states["job_1"] = JobStatus("running")
    cut = _accepted_turn(tmp_path, backend="vibey", delivery="unready", jobs=jobs)
    _previous_process("running")(cut.engine, cut.session_id, cut.turn_id)
    cut.manager._active_identity = lambda *_args: None
    cut.manager._transport_can_deliver = lambda _platform: False

    async def startup_then_stop():
        assert await cut.manager.controller.vibey_recovery.start() == []
        await cut.manager.recover_durable_delivery_state(service_restart=True)
        # Held: the command stays with the Turn.
        assert cut.starts == [] and jobs.killed == [] and jobs.watches == {}
        cut.manager._terminalize_durable_turn(
            cut.turn_id, "canceled", settled_by="adapter_not_active", evidence_kind="stop_not_active"
        )
        for _ in range(50):
            if jobs.killed:
                break
            await asyncio.sleep(0.02)

    asyncio.run(startup_then_stop())

    assert jobs.killed == ["job_1"] and jobs.watches == {} and cut.starts == []


def test_concurrent_recovery_passes_rerun_a_cut_turn_once(isolated_state, tmp_path):
    """MESSAGE-DELIVERY-327: two recovery passes over the same cut Turn (a held rerun's transport
    callback and its retry) both decide to rerun it; one rotates and reruns it, and the other,
    whose rotation lands after that rerun bound its generation, releases nothing and starts
    nothing."""
    import threading

    cut = _accepted_turn(tmp_path, backend="vibey")
    _previous_process("tool")(cut.engine, cut.session_id, cut.turn_id)
    bound, calls, lock = threading.Event(), [], threading.Lock()
    rotate = cut.manager._rotate_for_rerun

    def second_after_the_first_bound(*args):
        with lock:
            calls.append(args)
            later = len(calls) > 1
        if later:
            assert bound.wait(5)  # the other pass's rerun has bound its generation
        return rotate(*args)

    async def accepted(sid, ctx, text, **_kwargs):
        # The rerun's native start binds this process's generation, as Vibey's does.
        cut.starts.append((sid, text))
        cut.manager.on_native_start(
            ctx, backend="vibey", runtime_key=f"runtime:{sid}", runtime_turn_id=f"runtime-turn:{sid}"
        )
        bound.set()

    cut.manager.controller._session_id_from_context = lambda ctx: (ctx.platform_specific or {}).get(
        "agent_session_id"
    )
    cut.manager._rotate_for_rerun = second_after_the_first_bound
    cut.manager._run = accepted
    cut.manager._active_identity = lambda *_args: None
    cut.manager._transport_can_deliver = lambda _platform: True

    async def two_passes():
        await asyncio.gather(
            *(cut.manager.recover_durable_delivery_state(cut.session_id, service_restart=True) for _ in range(2))
        )

    asyncio.run(two_passes())

    assert len(calls) == 2 and cut.starts == [(cut.session_id, "")]
    with cut.engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, cut.turn_id)
    assert turn["state"] == "active" and turn["runtime_key"] == f"runtime:{cut.session_id}"


def test_a_rerun_whose_setup_fails_is_retried_not_settled(isolated_state, tmp_path, monkeypatch):
    """MESSAGE-DELIVERY-327: a transient failure setting the rerun up (its routing context, here)
    is not the Turn's: the Turn stays open, recovery retries and reruns it, and the restart counts
    once in its streak."""
    from core.session_turns import SessionTurnManager

    monkeypatch.setattr(SessionTurnManager, "RESTART_RECOVERY_RETRY_DELAYS", (0.05,))
    monkeypatch.setattr(SessionTurnManager, "RESTART_RECOVERY_RETRY_PERIOD", 0.05)
    cut = _accepted_turn(tmp_path, backend="vibey")
    _previous_process("tool")(cut.engine, cut.session_id, cut.turn_id)
    build, failed = cut.manager._delivery_context, []

    def locked_once(session_id):
        if not failed:
            failed.append(session_id)
            raise RuntimeError("database is locked")
        return build(session_id)

    cut.manager._delivery_context = locked_once
    cut.manager._active_identity = lambda *_args: None

    async def startup():
        await cut.manager.recover_durable_delivery_state(service_restart=True)
        assert cut.starts == [] and failed and _notices(cut.engine, cut.session_id) == []
        for _ in range(100):
            if cut.starts:
                break
            await asyncio.sleep(0.02)

    asyncio.run(startup())

    assert cut.starts == [(cut.session_id, "")]
    with cut.engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, cut.turn_id)
    assert turn["state"] == "active" and json.loads(turn["start_receipt_json"])["restart_streak"] == 1
    assert _notices(cut.engine, cut.session_id) == []


@pytest.mark.parametrize("ended", ["cancelled", "failed"])
def test_a_rerun_cancelled_before_its_adapter_owns_the_command_still_ends_it(isolated_state, tmp_path, ended):
    """MESSAGE-DELIVERY-330: the rerun is published before Vibey takes the command its restart
    left running; a backend disable that cancels it in that interval still ends the command with
    the canceled Turn, and a failure there (a pre-turn step raising, which the runner turns into a
    failed Turn) hands the command to a Watch as a failed run's is: never left running unowned."""
    from core.agent_core.tools.base import JobStatus
    from core.run_settlement import SETTLED_BY_BACKEND_DISABLED
    from core.session_turns import Turn
    from tests.agent_core.fakes import FakeJobHost
    from tests.test_internal_server import _bind_test_native_start

    jobs = FakeJobHost()
    jobs.states["job_1"] = JobStatus("running")
    cut = _accepted_turn(tmp_path, backend="vibey", jobs=jobs)

    release = asyncio.Event()

    async def fails_before_its_adapter(turn_id):
        await release.wait()
        # The runner catches the failure and settles the Turn; its task ends normally.
        cut.manager._terminalize_durable_turn(
            turn_id, "failed", settled_by="no_terminal_result", evidence_kind="test_pre_turn_failure"
        )

    async def published_not_yet_adapted(sid, ctx, text, **_kwargs):
        # ``_run`` publishes the in-flight Turn; the adapter has not started yet.
        cut.starts.append((sid, text))
        _bind_test_native_start(cut.engine, ctx)
        turn_id = ctx.platform_specific["turn_token"]
        cut.manager.in_flight[sid] = Turn(
            task=asyncio.ensure_future(
                fails_before_its_adapter(turn_id) if ended == "failed" else asyncio.Event().wait()
            ),
            context=ctx, logical_turn_id=turn_id,
        )

    cut.manager._run = published_not_yet_adapted
    _previous_process("running")(cut.engine, cut.session_id, cut.turn_id)
    cut.manager._active_identity = lambda *_args: None

    async def startup():
        assert await cut.manager.controller.vibey_recovery.start() == []
        await cut.manager.recover_durable_delivery_state(service_restart=True)
        assert cut.starts == [(cut.session_id, "")] and jobs.killed == []
        if ended == "failed":
            release.set()
        else:
            # The backend is disabled: its Turns are settled and their tasks cancelled.
            cut.manager._terminalize_durable_turn(
                cut.turn_id, "canceled", settled_by=SETTLED_BY_BACKEND_DISABLED, evidence_kind="backend_disabled"
            )
            cut.manager.in_flight.pop(cut.session_id).task.cancel()
        for _ in range(100):
            if jobs.killed or jobs.watches:
                break
            await asyncio.sleep(0.02)

    asyncio.run(startup())

    if ended == "failed":
        assert jobs.killed == [] and jobs.watches == {"job_1": "watch_job_1"}
    else:
        assert jobs.killed == ["job_1"] and jobs.watches == {}


def test_a_rerun_that_exits_before_dispatching_fails_its_turn(isolated_state, tmp_path):
    """MESSAGE-DELIVERY-327: the rerun's runner finds no way to dispatch (its definitive no-write
    exit). The Turn's start was accepted before the restart and nothing else runs it, so it fails
    instead of staying open with no runner."""
    from core.run_settlement import SETTLED_BY_NO_TERMINAL_RESULT

    cut = _accepted_turn(tmp_path, backend="vibey")

    async def exits_before_dispatch(sid, ctx, text, **_kwargs):
        cut.starts.append((sid, text))
        cut.manager._settle_durable_prewrite_failure(
            ctx.platform_specific["turn_token"], outcome=SETTLED_BY_NO_TERMINAL_RESULT
        )

    cut.manager._run = exits_before_dispatch
    _restart(cut, _previous_process("tool"))

    assert cut.starts == [(cut.session_id, "")]
    with cut.engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, cut.turn_id)
    assert (turn["state"], turn["terminal_outcome"]) == ("terminal", "failed")
    assert turn["terminal_evidence_kind"] == "rerun_failed_before_dispatch"


def test_restart_rerun_runs_before_input_queued_while_the_service_was_down(isolated_state, tmp_path):
    """MESSAGE-DELIVERY-331: the Turn stays the Session's active Turn, so the queued input waits
    behind its rerun, then runs."""
    cut = _accepted_turn(tmp_path, backend="vibey")

    def queue_input(engine, session_id, _turn_id):
        with engine.begin() as conn:
            message_deliveries.enqueue_queued(conn, scope_id=None, session_id=session_id, text="然后再看看日志")

    _restart(cut, _then(_previous_process("tool"), queue_input))

    assert cut.starts == [(cut.session_id, "")]
    with cut.engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, cut.session_id)] == ["然后再看看日志"]
    assert asyncio.run(cut.manager.terminalize_turn(cut.turn_id))
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
        tmp_path, backend="vibey", jobs=jobs, before_restart=_then(_previous_process("running"), _switch_off_vibey)
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


@pytest.mark.parametrize("then", ["handed_over", "stopped"])
def test_a_restart_notice_waits_until_its_running_command_is_handed_over(isolated_state, tmp_path, monkeypatch, then):
    """MESSAGE-DELIVERY-322: with no Watch to take the command yet, the Turn stays open (T2 before
    T4): no notice without the Watch, nothing failed under a running command; the retry hands it
    over and the notice then names it. A Stop that settles the waiting Turn ends the command."""
    from core.agent_core.tools.base import JobStatus
    from core.session_turns import SessionTurnManager
    from tests.agent_core.fakes import FakeJobHost

    jobs = FakeJobHost()
    jobs.states["job_1"] = JobStatus("running")
    attempts = []
    hand_over = jobs.hand_over

    async def locked_once(job_id):
        attempts.append(job_id)
        if len(attempts) == 1 or then == "stopped":
            raise RuntimeError("the Watch store is locked")
        return await hand_over(job_id)

    jobs.hand_over = locked_once
    monkeypatch.setattr(SessionTurnManager, "RESTART_RECOVERY_RETRY_DELAYS", (0.05,))
    monkeypatch.setattr(SessionTurnManager, "RESTART_RECOVERY_RETRY_PERIOD", 0.05)
    cut = _accepted_turn(tmp_path, backend="vibey", jobs=jobs)
    _then(_previous_process("running"), _switch_off_vibey)(cut.engine, cut.session_id, cut.turn_id)
    cut.manager._active_identity = lambda *_args: None

    async def startup():
        assert await cut.manager.controller.vibey_recovery.start() == []
        await cut.manager.recover_durable_delivery_state(cut.session_id, service_restart=True)
        with cut.engine.connect() as conn:
            assert message_deliveries.get_turn(conn, cut.turn_id)["state"] == "active"
        assert _notices(cut.engine, cut.session_id) == []
        if then == "stopped":
            # The user stops the Turn while it waits; no runtime holds it, so the Stop settles it.
            with cut.engine.begin() as conn:
                message_deliveries.terminalize_turn(
                    conn, cut.turn_id, outcome="canceled", settled_by="adapter_not_active",
                    evidence_kind="stop_not_active",
                )
        for _ in range(100):
            if _notices(cut.engine, cut.session_id) or jobs.killed:
                break
            await asyncio.sleep(0.02)

    asyncio.run(startup())

    if then == "stopped":
        # Ended as the Stop would end it, never left running as a Watch, and no notice owed.
        assert jobs.killed == ["job_1"] and jobs.watches == {}
        assert _notices(cut.engine, cut.session_id) == []
        return
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


@pytest.mark.parametrize("bound", [False, True])
def test_continue_on_vibey_moves_the_session_and_runs_the_unwritten_message(isolated_state, tmp_path, bound):
    """A message that never reached its disabled Agent continues on the built-in Vibey
    Agent: one request moves the session (the same switch, with its handoff when the
    session had a conversation) and runs that exact message there. The move and the
    retry commit together, so a notice whose input did reach the old backend refuses
    both and leaves the session where it was."""
    from core.vibe_agents import VibeAgentStore
    from vibe.ui_server import app

    store = VibeAgentStore()
    try:
        vibey = store.ensure_builtin_default_agent(backend="vibey")
    finally:
        store.close()
    session_id, notice, _turn, _inputs = _retry_notice(tmp_path, backend="codex", not_written=True)
    if bound:
        with create_sqlite_engine().begin() as conn:
            conn.execute(
                update(agent_sessions).where(agent_sessions.c.id == session_id).values(native_session_id="codex-thread")
            )
    client = app.test_client()
    headers = csrf_headers(client)
    url = f"/api/sessions/{session_id}/messages"
    idle = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": False}})

    assert client.post(url, headers=headers, json={"continue_on_backend": "vibey", "text": "x"}).status_code == 400
    continue_body = {"retry_for": notice["id"], "continue_on_backend": "vibey"}
    from core.vibe_agents import VibeAgentAccessError

    with patch("core.vibe_agents.ensure_agent_selection_access", side_effect=VibeAgentAccessError("denied")):
        assert client.post(url, headers=headers, json=continue_body).status_code == 403
    dispatch = AsyncMock(return_value={"status_code": 202, "body": {"delivery_state": "queued", "queued": True}})
    with (
        patch("vibe.internal_client.turn_state", idle),
        patch("vibe.internal_client.dispatch_async", dispatch),
        # The session's own Agent may be deleted and unusable by this caller: the
        # continuation never runs it, so only the target Agent's access counts.
        patch("core.vibe_agents.ensure_session_agent_access", side_effect=PermissionError("deleted Agent")),
    ):
        response = client.post(url, headers=headers, json=continue_body)
    assert response.status_code == 202
    assert dispatch.await_args.args[0]["message_id"] == _inputs[0]["id"]
    assert dispatch.await_args.args[0]["text"] == "检查这张图"
    with create_sqlite_engine().connect() as conn:
        session = conn.execute(select(agent_sessions).where(agent_sessions.c.id == session_id)).mappings().one()
    assert (session["agent_backend"], session["agent_name"]) == ("vibey", vibey.name)
    assert ("backend_handoff" in json.loads(session["metadata_json"])) is bound


def test_continue_on_vibey_refuses_a_message_the_old_backend_already_ran(isolated_state, tmp_path):
    """The move and the retry commit together: a continuation of a turn that reached
    the old backend belongs to it, so both are refused and the session stays put."""
    from core.vibe_agents import VibeAgentStore
    from vibe.ui_server import app

    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agent(backend="vibey")
    finally:
        store.close()
    failed_session, failed_notice, _turn, _inputs = _retry_notice(tmp_path, backend="codex")
    client = app.test_client()
    headers = csrf_headers(client)
    idle = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": False}})
    with patch("vibe.internal_client.turn_state", idle), patch("vibe.internal_client.dispatch_async", AsyncMock()) as never:
        refused = client.post(
            f"/api/sessions/{failed_session}/messages",
            headers=headers,
            json={"retry_for": failed_notice["id"], "continue_on_backend": "vibey"},
        )
    assert refused.status_code == 409
    assert refused.get_json()["code"] == "retry_not_available"
    never.assert_not_awaited()
    with create_sqlite_engine().connect() as conn:
        unchanged = conn.execute(
            select(agent_sessions.c.agent_backend).where(agent_sessions.c.id == failed_session)
        ).scalar_one()
    assert unchanged == "codex"
