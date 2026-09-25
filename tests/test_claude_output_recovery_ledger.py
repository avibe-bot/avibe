"""Consumer boundaries for phase-owned recovery; no native service or SDK calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.runtime_activation import RuntimeActivationRegistry
from core.session_activities import SessionActivityRegistry, activity_completion_output
from modules.agents.base import AgentRequest
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.service import AgentService
from modules.claude_sdk_compat import TextBlock
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.session_activities import SQLiteSessionActivityStore
from tests.test_claude_agent_initiated_turn import (
    AssistantMessage,
    ResultMessage,
    _build_agent,
    _ActivityDeliveryClient,
    _install_activity_dispatcher,
    _wait_until,
)


def context_for(key, token=""):
    return SimpleNamespace(
        user_id="U1",
        channel_id="C1",
        platform="avibe",
        platform_specific={
            "agent_runtime_turn_key": key,
            "agent_runtime_turn_token": token,
            "turn_token": token,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stop", "eof", "error", "replacement"])
async def test_generation_end_preserves_terminal_recovery_owner_and_gate(ending):
    agent, service = _build_agent()
    key = "recovery-stop:/tmp/work"
    context = context_for(key)
    agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 0.01
    agent._get_formatter = lambda _ctx: SimpleNamespace(
        format_assistant_message=lambda parts: "\n".join(parts),
    )
    terminal_seen, stream_wait, allow_delivery = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    attempts = []

    async def emit(_context, text, **kwargs):
        attempts.append((text, kwargs["output"]))
        if not allow_delivery.is_set():
            raise RuntimeError("outbound unavailable")
        return "durably-accepted"

    async def controller_emit(ctx, _kind, _text, **kwargs):
        if kwargs["output"].completes_turn:
            service.release_runtime_turn(ctx)
        return "terminal"

    agent.emit_result_message = AsyncMock(side_effect=emit)
    agent.controller.emit_agent_message = AsyncMock(side_effect=controller_emit)
    agent._handle_receiver_exception = ClaudeAgent._handle_receiver_exception.__get__(agent)

    class Client:
        interrupt = AsyncMock()
        disconnect = AsyncMock()

        def receive_messages(self):
            async def frames():
                assistant = AssistantMessage()
                assistant.content = [TextBlock(text="retained result")]
                yield assistant
                result = ResultMessage()
                result.result = "retained result"
                result.origin = {"kind": "task-notification"}
                yield result
                terminal_seen.set()
                await stream_wait.wait()
                if ending == "error":
                    raise RuntimeError("native connection lost")

            return frames()

    client = Client()
    agent.claude_sessions[key] = client
    receiver = asyncio.create_task(
        agent._receive_messages(client, "recovery-stop", "/tmp/work", context, composite_key=key)
    )
    agent.receiver_tasks[key] = receiver
    try:
        await asyncio.wait_for(terminal_seen.wait(), timeout=1)
        owner = agent._pending_requests[key][0]
        original_token = service._get_turn_gate(key).token
        stop = AgentRequest(
            context=context_for(key),
            message="",
            user_message="",
            working_path="/tmp/work",
            base_session_id="recovery-stop",
            composite_session_id=key,
            session_key="session-key",
        )
        if ending == "stop":
            assert await service.handle_stop("claude", stop)
        elif ending == "replacement":
            await agent._cleanup_runtime_session(key, expected_client=client)
        else:
            stream_wait.set()
            await asyncio.wait_for(receiver, timeout=1)
        assert key not in agent.claude_sessions
        assert service.runtime_turn_active(key)
        assert service._get_turn_gate(key).token == original_token
        assert agent._pending_requests[key] == [owner]
        agent.controller.emit_agent_message.assert_not_awaited()
        allow_delivery.set()
        await asyncio.wait_for(_wait_until(lambda: not agent._has_pending_requests(key)), timeout=1)
        assert not service.runtime_turn_active(key)
        assert not agent._output_records_for_runtime(key)
        assert {text for text, _ in attempts} == {"retained result"}
        assert len({output.idempotency_key for _, output in attempts}) == 1
        successor = context_for(key)
        assert await service.begin_agent_initiated_turn("claude", successor, key)
        service.release_runtime_turn(successor)
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        retry = agent._activity_flush_tasks.pop(key, None)
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic_successor", [False, True])
async def test_human_phase_and_successor_cannot_overwrite_or_be_released_by_old_retry(synthetic_successor):
    agent, service = _build_agent()
    key = "retained-with-human:/tmp/work"
    context = context_for(key)
    await service.begin_agent_initiated_turn("claude", context, key)
    human = AgentRequest(
        context=context,
        message="human input",
        user_message="human input",
        working_path="/tmp/work",
        base_session_id="retained-with-human",
        composite_session_id=key,
        session_key="session-key",
    )
    agent._pending_requests[key] = [human]
    agent._adopt_pending_turn_token = ClaudeAgent._adopt_pending_turn_token
    agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 0.01
    agent._get_formatter = lambda _: SimpleNamespace(
        format_assistant_message=lambda parts: "\n".join(parts),
    )
    frames_done, release, recover = asyncio.Event(), asyncio.Event(), asyncio.Event()
    detached_attempts, human_emits = [], []

    async def emit(ctx, text, **kwargs):
        output = kwargs.get("output")
        if output is not None and output.detached:
            detached_attempts.append((text, output))
            if not recover.is_set():
                raise RuntimeError("detached delivery unavailable")
        else:
            human_emits.append(text)
            service.release_runtime_turn(ctx)
        return "accepted"

    agent.emit_result_message = AsyncMock(side_effect=emit)

    class Client:
        def receive_messages(self):
            async def frames():
                detached = ResultMessage()
                detached.origin = {"kind": "task-notification"}
                detached.result = "old detached payload"
                yield detached
                assistant = AssistantMessage()
                assistant.content = [TextBlock(text="new human answer")]
                yield assistant
                human_result = ResultMessage()
                human_result.result = "new human answer"
                yield human_result
                frames_done.set()
                await release.wait()

            return frames()

    client = Client()
    agent.claude_sessions[key] = client
    receiver = asyncio.create_task(
        agent._receive_messages(client, "retained-with-human", "/tmp/work", context, composite_key=key)
    )
    try:
        await asyncio.wait_for(frames_done.wait(), timeout=1)
        assert human_emits == ["new human answer"]
        assert not agent._has_pending_requests(key)
        successor_context = context_for(key)
        token = await service.begin_agent_initiated_turn("claude", successor_context, key)
        assert token
        successor = SimpleNamespace(context=successor_context)
        agent._pending_requests[key] = [successor]
        if synthetic_successor:
            successor._claude_synthetic_owner = True
            agent._synthetic_pending_owners[key] = successor
        recover.set()
        await asyncio.wait_for(
            _wait_until(lambda: not agent._output_records_for_runtime(key)),
            timeout=1,
        )
        assert {text for text, _ in detached_attempts} == {"old detached payload"}
        assert len({output.idempotency_key for _, output in detached_attempts}) == 1
        assert agent._pending_requests[key] == [successor]
        assert service._get_turn_gate(key).token == token
        assert service.runtime_turn_active(key)
        service.release_runtime_turn(successor_context)
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        retry = agent._activity_flush_tasks.pop(key, None)
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


@pytest.fixture
def activity_store(tmp_path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    yield SQLiteSessionActivityStore(engine)
    engine.dispose()


@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "killed"])
def test_later_completion_retires_failed_active_provenance_write(
    activity_store, monkeypatch, status,
):
    registry = SessionActivityRegistry(activity_store)
    key = "provenance-lifecycle"
    registry.start(
        backend="claude", runtime_key=key, session_id="ses-1",
        activity_id="task-one", kind="background_task",
        metadata={"provenance_pending": True},
    )
    original = activity_store.upsert_activity
    fault = "classification"

    def persist(activity, *, phase):
        if fault == "classification" and activity["metadata"].get("provenance_human"):
            raise RuntimeError("classification unavailable")
        if fault == "binding" and activity["metadata"].get("output_batch_id"):
            raise RuntimeError("binding unavailable")
        original(activity, phase=phase)

    monkeypatch.setattr(activity_store, "upsert_activity", persist)
    registry.classify_provisional_provenance(
        "claude", key, activity_ids={"task-one"}, parent_activity_ids=set(),
        turn_id="human-turn", run_ids=["human-run"], detached=False,
    )
    assert registry.provenance_persistence_recovery("claude", key)
    fault = ""
    registry.complete(
        backend="claude", runtime_key=key, activity_id="task-one", status=status,
        expects_output=status == "completed", retain_terminal_snapshot=True,
    )
    assert registry.provenance_persistence_recovery("claude", key) == {}
    expected_phase = "awaiting_output" if status == "completed" else "terminal"
    assert activity_store.list_activities()[0]["phase"] == expected_phase
    if status == "completed":
        fault = "binding"
        with pytest.raises(RuntimeError, match="binding unavailable"):
            registry.claim_completed_output_batch("claude", key)
        assert activity_store.list_activities()[0]["phase"] == "awaiting_output"
    fault = ""
    restarted = SessionActivityRegistry(activity_store)
    if status == "completed":
        restored = restarted.claim_completed_output_batch("claude", key)
        assert [item.id for item in restored] == ["task-one"]
        output = activity_completion_output(
            restored[0], activities=restored, detached=True, completes_turn=False,
        )
        assert restarted.settle_completed_output_batch(output, accepted_message_exists=True)
    else:
        restored = restarted.drain_recovered_terminals()
        assert [item.status for item in restored] == [status]
        restarted.ack_recovered_terminal(restored[0])
    assert activity_store.list_activities() == []


@pytest.mark.parametrize("owner", ["active", "queued", "claimed", "terminal"])
def test_provenance_retry_phase_comes_from_current_owner_not_error_text(
    activity_store, owner,
):
    registry = SessionActivityRegistry(activity_store)
    key = "provenance-owner"
    activity = registry.start(
        backend="claude", runtime_key=key, session_id="ses-1",
        activity_id="task-one", kind="background_task",
    )
    if owner != "active":
        activity = registry.complete(
            backend="claude", runtime_key=key, activity_id=activity.id,
            status="failed" if owner == "terminal" else "completed",
            expects_output=owner != "terminal", retain_terminal_snapshot=True,
        )
    if owner == "claimed":
        activity = registry.claim_completed_output_batch("claude", key)[0]
    # A recorded attempt is diagnostic evidence, never lifecycle authority.
    registry._record_provenance_persistence_recovery(
        activity, phase="active", error=RuntimeError("older attempt"),
    )
    registry.classify_provisional_provenance(
        "claude", key, activity_ids=set(), parent_activity_ids=set(), detached=False,
    )
    expected = {
        "active": "active", "queued": "awaiting_output",
        "claimed": "awaiting_output", "terminal": "terminal",
    }[owner]
    assert activity_store.list_activities()[0]["phase"] == expected
    assert registry.provenance_persistence_recovery("claude", key) == {}


@pytest.mark.parametrize("owner", ["active", "claimed", "terminal"])
def test_authoritative_deletion_clears_provenance_retry_without_resurrection(
    activity_store, monkeypatch, owner,
):
    registry = SessionActivityRegistry(activity_store)
    key = "provenance-delete"
    registry.start(
        backend="claude", runtime_key=key, session_id="ses-1",
        activity_id="task-one", kind="background_task",
        metadata={"provenance_pending": True},
    )
    if owner != "active":
        registry.complete(
            backend="claude", runtime_key=key, activity_id="task-one",
            status="failed" if owner == "terminal" else "completed",
            expects_output=owner == "claimed", retain_terminal_snapshot=True,
        )
    if owner == "claimed":
        registry.claim_completed_output_batch("claude", key)
    original = activity_store.upsert_activity

    def fail_classification(activity, *, phase):
        if activity["metadata"].get("provenance_human"):
            raise RuntimeError("classification unavailable")
        original(activity, phase=phase)

    monkeypatch.setattr(activity_store, "upsert_activity", fail_classification)
    classified = registry.classify_provisional_provenance(
        "claude", key, activity_ids={"task-one"}, parent_activity_ids=set(),
        turn_id="human-turn", run_ids=["human-run"], detached=False,
    )[0]
    assert registry.provenance_persistence_recovery("claude", key)
    if owner == "active":
        registry.complete(
            backend="claude", runtime_key=key, activity_id=classified.id,
            status="killed",
        )
    elif owner == "terminal":
        registry.ack_recovered_terminal(classified)
    else:
        output = activity_completion_output(
            classified, activities=[classified], detached=True, completes_turn=False,
        )
        assert registry.settle_completed_output_batch(output, accepted_message_exists=True)
    assert registry.provenance_persistence_recovery("claude", key) == {}
    monkeypatch.setattr(activity_store, "upsert_activity", original)
    assert registry.terminal_snapshots_for_runtime("claude", key) == []
    assert activity_store.list_activities() == []
    assert not SessionActivityRegistry(activity_store).has_backend_work("claude")


def test_atomic_batch_binding_supersedes_only_successful_provenance_retries(
    activity_store, monkeypatch,
):
    registry = SessionActivityRegistry(activity_store)
    key = "provenance-batch"
    for activity_id in ("one", "two"):
        registry.start(
            backend="claude", runtime_key=key, session_id="ses-1",
            activity_id=activity_id, kind="background_task",
            metadata={"provenance_pending": True},
        )
        registry.complete(
            backend="claude", runtime_key=key, activity_id=activity_id,
            status="completed", expects_output=True,
        )
    original_one = activity_store.upsert_activity
    original_batch = activity_store.upsert_activities

    def failed_one(*_args, **_kwargs):
        raise RuntimeError("single write failed")

    monkeypatch.setattr(activity_store, "upsert_activity", failed_one)
    registry.classify_provisional_provenance(
        "claude", key, activity_ids={"one", "two"}, parent_activity_ids=set(),
        turn_id="human-turn", run_ids=["human-run"], detached=False,
    )
    assert set(registry.provenance_persistence_recovery("claude", key)) == {"one", "two"}

    def failed_batch(*_args, **_kwargs):
        raise RuntimeError("batch failed")

    monkeypatch.setattr(activity_store, "upsert_activities", failed_batch)
    with pytest.raises(RuntimeError, match="batch failed"):
        registry.claim_completed_output_batch("claude", key)
    assert set(registry.provenance_persistence_recovery("claude", key)) == {"one", "two"}
    monkeypatch.setattr(activity_store, "upsert_activities", original_batch)
    batch = registry.claim_completed_output_batch("claude", key)
    assert [item.id for item in batch] == ["one", "two"]
    assert registry.provenance_persistence_recovery("claude", key) == {}
    monkeypatch.setattr(activity_store, "upsert_activity", original_one)
    restarted = SessionActivityRegistry(activity_store)
    restored = restarted.claim_completed_output_batch("claude", key)
    assert [item.id for item in restored] == ["one", "two"]
    assert {item.run_id for item in restored} == {"human-run"}
    assert {item.metadata["output_batch_id"] for item in restored} == {
        batch[0].metadata["output_batch_id"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["eof", "error"])
@pytest.mark.parametrize("fault", ["before_emit", "after_pop", "none"])
async def test_buffered_failure_replay_settles_owned_turn_once_before_release(ending, fault):
    from tests.test_claude_result_provenance import TaskStartedMessage, _failure_assistant

    agent, service = _build_agent()
    key = f"failure-replay-{ending}-{fault}:/tmp/work"
    context = context_for(key)
    assert await service.begin_agent_initiated_turn("claude", context, key)
    request = SimpleNamespace(context=context, output=None, output_activities=[])
    agent._pending_requests[key] = [request]
    agent._handle_receiver_eof = ClaudeAgent._handle_receiver_eof.__get__(agent)
    agent._handle_assistant_terminal_failure = (
        ClaudeAgent._handle_assistant_terminal_failure.__get__(agent)
    )
    agent.controller.agent_auth_service = SimpleNamespace(
        maybe_emit_auth_recovery_message=AsyncMock(return_value=False),
    )
    agent.session_handler.handle_session_error = AsyncMock(return_value=True)
    agent.session_handler.cleanup_session = AsyncMock()
    agent.record_model_hub_native_failure = AsyncMock(
        side_effect=[RuntimeError("record failed"), None]
        if fault == "before_emit" else None,
    )
    agent._remove_ack_reaction = AsyncMock(
        side_effect=[RuntimeError("reaction failed"), None]
        if fault == "after_pop" else None,
    )
    terminal = []

    async def accepted(ctx, kind, text, **kwargs):
        if kind == "result" and kwargs["output"].completes_turn:
            terminal.append((ctx.platform_specific.copy(), kwargs))
            assert service.runtime_turn_active(key)
            service.release_runtime_turn(ctx)
        return "accepted-output"

    agent.controller.emit_agent_message = AsyncMock(side_effect=accepted)

    class Client:
        def receive_messages(self):
            async def frames():
                yield TaskStartedMessage("competing-task")
                yield _failure_assistant("backend exploded")
                if ending == "error":
                    raise RuntimeError("receiver disconnected")
            return frames()

    client = Client()
    agent.claude_sessions[key] = client
    await agent._receive_messages(
        client, "failure-replay", "/tmp/work", context, composite_key=key,
    )
    assert len(terminal) == 1
    assert terminal[0][0]["agent_runtime_turn_token"] == context.platform_specific[
        "agent_runtime_turn_token"
    ]
    assert terminal[0][1]["is_error"]
    assert not agent._has_pending_requests(key)
    assert not service.runtime_turn_active(key)
    # Repeated EOF cleanup has no terminal owner left and cannot emit again.
    await agent._handle_receiver_eof(key, context)
    assert len(terminal) == 1
    successor = context_for(key)
    assert await service.begin_agent_initiated_turn("claude", successor, key)
    service.release_runtime_turn(successor)


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["stop", "replacement"])
async def test_retired_receiver_cannot_replay_buffered_failure_against_successor(retirement):
    from tests.test_claude_result_provenance import TaskStartedMessage, _failure_assistant

    agent, service = _build_agent()
    key = f"replay-retired-{retirement}:/tmp/work"
    context = context_for(key)
    assert await service.begin_agent_initiated_turn("claude", context, key)
    request = AgentRequest(
        context=context, message="human", user_message="human", working_path="/tmp/work",
        base_session_id="replay-retired", composite_session_id=key, session_key="session-key",
    )
    agent._pending_requests[key] = [request]
    agent._handle_receiver_eof = ClaudeAgent._handle_receiver_eof.__get__(agent)
    agent._process_assistant_terminal_frame = AsyncMock(
        wraps=agent._process_assistant_terminal_frame,
    )
    waiting, finish = asyncio.Event(), asyncio.Event()
    terminals = []

    async def accepted(ctx, kind, text, **kwargs):
        if kind == "result" and kwargs["output"].completes_turn:
            terminals.append(kwargs["output"])
            service.release_runtime_turn(ctx)
        return "accepted-output"

    agent.controller.emit_agent_message = AsyncMock(side_effect=accepted)

    class Client:
        interrupt = AsyncMock()
        disconnect = AsyncMock()

        def receive_messages(self):
            async def frames():
                yield TaskStartedMessage("competing-task")
                yield _failure_assistant("obsolete backend failure")
                waiting.set()
                await finish.wait()
            return frames()

    old = Client()
    agent.claude_sessions[key] = old
    receiver = asyncio.create_task(
        agent._receive_messages(old, "replay-retired", "/tmp/work", context, composite_key=key)
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert agent._buffered_assistant_messages[key]
        if retirement == "stop":
            assert await service.handle_stop("claude", request)
            assert len(terminals) == 1
        else:
            service.release_runtime_turn(context)
        successor_context = context_for(key)
        assert await service.begin_agent_initiated_turn("claude", successor_context, key)
        successor = SimpleNamespace(context=successor_context)
        new_client = SimpleNamespace()
        agent.claude_sessions[key] = new_client
        agent._pending_requests[key] = [successor]
        token = service._get_turn_gate(key).token
        finish.set()
        await asyncio.wait_for(receiver, timeout=1)
        assert agent.claude_sessions[key] is new_client
        assert agent._pending_requests[key] == [successor]
        assert service.runtime_turn_active(key)
        assert service._get_turn_gate(key).token == token
        assert len(terminals) == (1 if retirement == "stop" else 0)
        agent._process_assistant_terminal_frame.assert_not_awaited()
        service.release_runtime_turn(successor_context)
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)


def test_classification_run_ack_does_not_remove_output_receipt_across_restart(activity_store):
    settled = []
    service = AgentService(
        SimpleNamespace(scheduled_task_service=SimpleNamespace(settle_activity_runs=settled.append)),
        activities=SessionActivityRegistry(activity_store),
    )
    registry = service.activities
    registry.start(
        backend="claude",
        runtime_key="runtime",
        session_id="ses-1",
        activity_id="queued",
        kind="background_task",
        metadata={"provenance_pending": True},
    )
    registry.complete(
        backend="claude",
        runtime_key="runtime",
        activity_id="queued",
        status="completed",
        expects_output=True,
    )
    classified = registry.classify_provisional_provenance(
        "claude",
        "runtime",
        activity_ids={"queued"},
        parent_activity_ids=set(),
        turn_id="human-turn",
        run_ids=["human-run"],
        delivery_key_external="human-delivery",
        phase_id="human-phase",
        detached=False,
    )
    assert service.on_activity_terminal(classified[0])
    assert settled[0].run_id == "human-run"
    assert activity_store.list_activities()[0]["phase"] == "awaiting_output"
    batch = registry.claim_completed_output_batch("claude", "runtime")
    output = activity_completion_output(batch[-1], activities=batch, detached=True, completes_turn=False)
    restarted = SessionActivityRegistry(activity_store)
    restored = restarted.claim_completed_output_batch("claude", "runtime")
    restored_output = activity_completion_output(
        restored[-1],
        activities=restored,
        detached=True,
        completes_turn=False,
    )
    assert restored_output.idempotency_key == output.idempotency_key
    assert restored_output.activity_ids == output.activity_ids
    assert restored[0].run_id == "human-run"
    assert restarted.settle_completed_output_batch(restored_output, accepted_message_exists=True)
    assert activity_store.list_activities() == []


def test_force_end_service_ack_removes_durable_terminal_once(activity_store):
    settled = []
    service = AgentService(
        SimpleNamespace(scheduled_task_service=SimpleNamespace(settle_activity_runs=settled.append)),
        activities=SessionActivityRegistry(activity_store),
    )
    for runtime in ("first", "second"):
        service.activities.start(
            backend="claude",
            runtime_key=runtime,
            session_id="ses-1",
            activity_id="same-native-id",
            kind="background_task",
            run_id=runtime,
        )
    service.force_end_backend_activities("claude")
    assert sorted(activity.run_id for activity in settled) == ["first", "second"]
    assert not service.activities.has_backend_work("claude")
    assert activity_store.list_activities() == []
    assert SessionActivityRegistry(activity_store).drain_recovered_terminals() == []


@pytest.mark.asyncio
async def test_ledger_retries_local_receipt_without_external_redelivery(activity_store):
    agent, service = _build_agent()
    service.activities = SessionActivityRegistry(activity_store)
    delivery = _ActivityDeliveryClient()
    _install_activity_dispatcher(agent, delivery)
    context = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform="discord",
        platform_specific={"agent_session_id": "ses-local-only"},
    )
    key = "receipt-local-only:/tmp/work"
    service.activities.start(
        backend="claude",
        runtime_key=key,
        session_id="ses-local-only",
        activity_id="task",
        kind="background_task",
        run_id="run-origin",
    )
    service.activities.complete(
        backend="claude",
        runtime_key=key,
        activity_id="task",
        status="completed",
        expects_output=True,
    )
    batch = service.activities.claim_completed_output_batch("claude", key)
    record = agent._retain_activity_output_record(key, batch)
    agent._classify_output_record(record, context, text="selected payload")
    receipt = record.output.idempotency_key
    agent.controller.scheduled_task_service = SimpleNamespace(settle_activity_runs=lambda _: None)

    class RunStore:
        def get_run(self, _run_id):
            return {"status": "running"}

        def record_run_output(self, *_args, **_kwargs):
            raise RuntimeError("Run storage unavailable")

        def close(self):
            pass

    upsert = activity_store.upsert_activity

    def fail_terminal(payload, *, phase):
        if phase == "terminal":
            raise RuntimeError("terminal receipt storage unavailable")
        return upsert(payload, phase=phase)

    try:
        with (
            patch("core.message_dispatcher.persist_agent_message", return_value=None),
            patch("core.message_dispatcher.agent_message_exists", return_value=False),
            patch("core.message_dispatcher.SQLiteBackgroundTaskStore", return_value=RunStore()),
        ):
            with (
                patch.object(activity_store, "upsert_activity", side_effect=fail_terminal),
                patch.object(activity_store, "delete_activity", side_effect=RuntimeError("delete unavailable")),
            ):
                assert await agent._flush_output_recovery(key, context)
                assert await agent._flush_output_recovery(key, context)
                assert delivery.sent == ["selected payload"]
                assert service.activities.has_claimed_output("claude", key)
            assert not await agent._flush_output_recovery(key, context)
        assert delivery.sent == ["selected payload"]
        assert record.output.idempotency_key == receipt
        assert not service.activities.has_claimed_output("claude", key)
        assert not agent._output_records_for_runtime(key)
    finally:
        retry = agent._activity_flush_tasks.pop(key, None)
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


@pytest.mark.asyncio
async def test_result_arriving_during_managed_retry_keeps_worker_responsibility():
    agent, service = _build_agent()
    key = "retry-interleaving:/tmp/work"
    context = context_for(key)
    agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 0.01
    agent._get_formatter = lambda _: SimpleNamespace(format_assistant_message=lambda parts: "\n".join(parts))
    retry_entered, release_retry, later_seen, stay_open = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    attempts, delivered = [], []

    async def emit(_ctx, text, **kwargs):
        attempts.append((text, kwargs["output"].idempotency_key))
        if len(attempts) == 1:
            raise RuntimeError("first delivery failed")
        if len(attempts) == 2:
            retry_entered.set()
            await release_retry.wait()
        delivered.append(text)
        return "accepted"

    agent.emit_result_message = AsyncMock(side_effect=emit)

    class Client:
        def receive_messages(self):
            async def frames():
                assistant = AssistantMessage()
                assistant.content = [TextBlock(text="first output")]
                yield assistant
                first = ResultMessage()
                first.origin = {"kind": "task-notification"}
                first.result = "first output"
                yield first
                await retry_entered.wait()
                later = ResultMessage(num_turns=2)
                later.origin = {"kind": "task-notification"}
                later.result = "later output"
                yield later
                later_seen.set()
                await stay_open.wait()

            return frames()

    client = Client()
    agent.claude_sessions[key] = client
    receiver = asyncio.create_task(
        agent._receive_messages(
            client,
            "retry-interleaving",
            "/tmp/work",
            context,
            composite_key=key,
        )
    )
    try:
        await asyncio.wait_for(later_seen.wait(), timeout=1)
        assert service.runtime_turn_active(key)
        release_retry.set()
        await asyncio.wait_for(
            _wait_until(lambda: not agent._has_pending_requests(key)),
            timeout=1,
        )
        assert delivered == ["first output", "later output"]
        assert attempts[0][1] == attempts[1][1] != attempts[-1][1]
        assert not agent._output_records_for_runtime(key)
        assert not service.runtime_turn_active(key)
        assert not receiver.done()
    finally:
        release_retry.set()
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        retry = agent._activity_flush_tasks.pop(key, None)
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


@pytest.mark.parametrize("status", ["failed", "stopped", "killed", "disconnected"])
def test_restart_resolves_unclassified_terminal_without_borrowing_new_generation(activity_store, status):
    activations = RuntimeActivationRegistry()
    identity = activations.attach("claude", "runtime")
    registry = SessionActivityRegistry(activity_store, activation_registry=activations)
    registry.start(
        backend="claude",
        runtime_key="runtime",
        session_id="ses-1",
        activity_id="unresolved",
        kind="background_task",
        activation_identity=identity,
        metadata={"provenance_pending": True, "provenance_phase_id": "old-phase"},
    )
    registry.complete(
        backend="claude",
        runtime_key="runtime",
        activity_id="unresolved",
        status=status,
        retain_terminal_snapshot=True,
        activation_identity=identity,
    )
    restarted_activations = RuntimeActivationRegistry()
    restarted_activations.attach("claude", "runtime")  # generation counter repeats
    restarted = SessionActivityRegistry(activity_store, activation_registry=restarted_activations)
    terminals = restarted.drain_recovered_terminals()
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal.status == status
    assert terminal.metadata["provenance_generation_ended"]
    assert terminal.metadata["provenance_unresolved"]
    assert terminal.run_id is None and terminal.turn_id is None
    restarted.ack_recovered_terminal(terminal)
    assert not restarted.has_backend_work("claude")
    assert not activity_store.list_activities()


@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "killed"])
@pytest.mark.parametrize("new_human", [False, True])
def test_provisional_task_can_finish_after_pending_human_has_retired(status, new_human):
    agent, service = _build_agent()
    key = "unclassified-after-human:/tmp/work"
    service.activities.start(
        backend="claude",
        runtime_key=key,
        session_id="ses-1",
        activity_id="unbound-task",
        kind="background_task",
        metadata={"provenance_pending": True, "provenance_phase_id": "prior-phase"},
    )
    if new_human:
        agent._pending_requests[key] = [SimpleNamespace(context=context_for(key, "new-human"))]
        agent._ensure_provisional_phase(key)
    terminal = SimpleNamespace(
        subtype="task_notification",
        task_id="unbound-task",
        status=status,
        data={},
    )
    assert agent._handle_activity_message(terminal, key, context_for(key))
    assert not service.activities.active_for_runtime("claude", key)
    assert agent._has_pending_requests(key) is new_human
    assert "unbound-task" not in agent._provisional_activity_ids.get(key, set())
    classified = service.activities.classify_provisional_provenance(
        "claude",
        key,
        activity_ids={"unbound-task"},
        parent_activity_ids=set(),
        turn_id=None,
        run_ids=[],
        delivery_key_external=None,
        phase_id="prior-phase",
        detached=True,
    )
    assert len(classified) == 1
    assert classified[0].status == status
    assert classified[0].turn_id is None and classified[0].run_id is None


@pytest.mark.asyncio
async def test_replaced_receiver_late_result_and_eof_cannot_consume_new_request():
    agent, service = _build_agent()
    key = "recovery-replaced:/tmp/work"
    activations = RuntimeActivationRegistry()
    service.activation_registry = activations
    old_identity = activations.attach("claude", key)
    waiting, release = asyncio.Event(), asyncio.Event()

    class OldClient:
        _vibe_runtime_activation_identity = old_identity

        def receive_messages(self):
            async def frames():
                waiting.set()
                await release.wait()
                result = ResultMessage()
                result.result = "OLD human result"
                yield result

            return frames()

    old = OldClient()
    agent.claude_sessions[key] = old
    agent.emit_result_message = AsyncMock(return_value="delivered")
    receiver = asyncio.create_task(
        agent._receive_messages(old, "recovery-replaced", "/tmp/work", context_for(key), composite_key=key)
    )
    await asyncio.wait_for(waiting.wait(), timeout=1)
    new_identity = activations.attach("claude", key)
    new_client = SimpleNamespace(_vibe_runtime_activation_identity=new_identity)
    agent.claude_sessions[key] = new_client
    new_context = context_for(key, "new-token")
    successor = SimpleNamespace(context=new_context)
    agent._pending_requests[key] = [successor]
    agent._last_assistant_text[key] = "NEW human draft"
    gate = service._get_turn_gate(key)
    await gate.lock.acquire()
    gate.token = "new-token"
    gate.backend = "claude"
    release.set()
    await asyncio.wait_for(receiver, timeout=1)
    assert agent.claude_sessions[key] is new_client
    assert agent._pending_requests[key] == [successor]
    assert agent._last_assistant_text[key] == "NEW human draft"
    assert gate.token == "new-token" and gate.lock.locked()
    agent.emit_result_message.assert_not_awaited()
    agent._handle_receiver_eof.assert_not_awaited()
    service.release_runtime_turn(new_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["human", "task-notification"])
async def test_stop_interrupt_claim_precedes_every_late_result_route(origin):
    agent, service = _build_agent()
    key = "stop-interrupt:/tmp/work"
    context = context_for(key)
    await service.begin_agent_initiated_turn("claude", context, key)
    pending = AgentRequest(
        context=context,
        message="input",
        user_message="input",
        working_path="/tmp/work",
        base_session_id="stop-interrupt",
        composite_session_id=key,
        session_key="session-key",
    )
    agent._pending_requests[key] = [pending]
    agent._adopt_pending_turn_token = ClaudeAgent._adopt_pending_turn_token
    stream_waiting, result_ready, interrupt_entered, interrupt_release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    stay_open = asyncio.Event()

    class Client:
        disconnect = AsyncMock()

        async def interrupt(self):
            interrupt_entered.set()
            await interrupt_release.wait()

        def receive_messages(self):
            async def frames():
                stream_waiting.set()
                await result_ready.wait()
                result = ResultMessage()
                result.origin = {"kind": origin}
                result.result = "late result must not become unsolicited"
                yield result
                await stay_open.wait()

            return frames()

    async def emit(ctx, _kind, _text, **kwargs):
        if kwargs["output"].completes_turn:
            service.release_runtime_turn(ctx)
        return "settled"

    agent.controller.emit_agent_message = AsyncMock(side_effect=emit)
    agent.emit_result_message = AsyncMock(return_value="must-not-deliver")
    client = Client()
    agent.claude_sessions[key] = client
    receiver = asyncio.create_task(
        agent._receive_messages(
            client,
            "stop-interrupt",
            "/tmp/work",
            context,
            composite_key=key,
        )
    )
    agent.receiver_tasks[key] = receiver
    await asyncio.wait_for(stream_waiting.wait(), timeout=1)
    stop = AgentRequest(
        context=context_for(key),
        message="",
        user_message="",
        working_path="/tmp/work",
        base_session_id="stop-interrupt",
        composite_session_id=key,
        session_key="session-key",
    )
    stopping = asyncio.create_task(service.handle_stop("claude", stop))
    try:
        await asyncio.wait_for(interrupt_entered.wait(), timeout=1)
        result_ready.set()
        await asyncio.sleep(0)
        agent.emit_result_message.assert_not_awaited()
        assert service.runtime_turn_active(key)
        interrupt_release.set()
        assert await asyncio.wait_for(stopping, timeout=1)
        agent.emit_result_message.assert_not_awaited()
        agent.controller.emit_agent_message.assert_awaited_once()
        call = agent.controller.emit_agent_message.await_args
        assert call.args[2] == "" and call.kwargs["level"] == "silent"
        assert not service.runtime_turn_active(key)
        assert not agent._has_pending_requests(key)
        assert not agent._output_records_for_runtime(key)
    finally:
        interrupt_release.set()
        receiver.cancel()
        await asyncio.gather(receiver, stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_provisional_detached_payload_is_frozen_before_generation_replacement():
    agent, service = _build_agent()
    key = "provisional-replacement:/tmp/work"
    context = context_for(key)
    # A queued human owns the gate but has not registered with the adapter yet.
    await service.begin_agent_initiated_turn("claude", context, key)
    receiver_context = context_for(key)
    assistant_seen, stay_open = asyncio.Event(), asyncio.Event()
    agent._get_formatter = lambda _: SimpleNamespace(format_assistant_message=lambda parts: "\n".join(parts))

    class Client:
        disconnect = AsyncMock()

        def receive_messages(self):
            async def frames():
                assistant = AssistantMessage()
                assistant.content = [TextBlock(text="old provisional notification")]
                yield assistant
                assistant_seen.set()
                await stay_open.wait()

            return frames()

    old = Client()
    agent.claude_sessions[key] = old
    receiver = asyncio.create_task(
        agent._receive_messages(
            old,
            "provisional-replacement",
            "/tmp/work",
            receiver_context,
            composite_key=key,
        )
    )
    agent.receiver_tasks[key] = receiver
    try:
        await asyncio.wait_for(assistant_seen.wait(), timeout=1)
        original = agent._output_records_for_runtime(key)[0]
        await agent._cleanup_runtime_session(key, expected_client=old)
        assert original.lifecycle == "delivery_pending"
        assert original.text == "old provisional notification"
        agent.claude_sessions[key] = SimpleNamespace()
        assert agent._provisional_output_record(key, "detached") is None
        replacement = agent._detached_phase(key)
        replacement.text = "new generation text"
        assert replacement.phase_id != original.phase_id
        assert original.text == "old provisional notification"
        assert service.runtime_turn_active(key)
    finally:
        service.release_runtime_turn(context)
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        retry = agent._activity_flush_tasks.pop(key, None)
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
