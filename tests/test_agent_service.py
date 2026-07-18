from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.message_output import terminal_turn_output
from core.session_activities import SessionActivityRegistry
from modules.agents.service import AgentService
from modules.agents.codex.transport import CodexTransport
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.session_activities import SQLiteSessionActivityStore


class _RuntimeAgent:
    name = "claude"

    def __init__(self, release_first: asyncio.Event | None = None):
        self.started: list[str] = []
        self.release_first = release_first

    def runtime_turn_key(self, request):
        return request.composite_session_id

    async def handle_message(self, request):
        self.started.append(request.message)
        if request.message == "first" and self.release_first is not None:
            await self.release_first.wait()

    async def clear_sessions(self, _session_key):
        return 0

    async def handle_stop(self, _request):
        return False


class _RaisingRuntimeAgent(_RuntimeAgent):
    async def handle_message(self, _request):
        raise RuntimeError("backend failed")


class _ClearingRuntimeAgent:
    name = "codex"

    def __init__(self, runtime_keys: set[str], on_clear=None):
        self.runtime_keys = runtime_keys
        self.clear_calls: list[str] = []
        self.on_clear = on_clear

    async def clear_sessions(self, session_key):
        self.clear_calls.append(session_key)
        if self.on_clear is not None:
            await self.on_clear()
        return 1

    def runtime_turn_keys_for_session_key(self, _session_key):
        return self.runtime_keys

    def runtime_turn_keys(self):
        return self.runtime_keys


class _StopRuntimeAgent(_RuntimeAgent):
    def __init__(self, reason: str):
        super().__init__()
        self.reason = reason

    async def handle_stop(self, request):
        request.stop_failure_reason = self.reason
        return False


class _RefreshingRuntimeAgent(_ClearingRuntimeAgent):
    def __init__(self, runtime_keys: set[str]):
        super().__init__(runtime_keys)
        self.refresh_calls: list[object] = []

    async def refresh_runtime_config(self, runtime_config):
        self.refresh_calls.append(runtime_config)


class _Controller:
    def __init__(self):
        self.session_turns = None


class _OrderRecordingTurnManager:
    def __init__(self, log: list[str]):
        self._log = log

    def on_running(self, _context):
        self._log.append("on_running")


class _OrderRecordingDispatcher:
    def __init__(self, log: list[str]):
        self._log = log

    async def begin_status_bubble(self, _context):
        self._log.append("begin_status_bubble")


class _TurnStartController:
    """Records the relative order of the turn-start hooks vs the agent run."""

    def __init__(self, log: list[str]):
        self.session_turns = _OrderRecordingTurnManager(log)
        self.message_dispatcher = _OrderRecordingDispatcher(log)
        self._log = log

    def update_thread_message_id(self, _context):
        self._log.append("update_thread_message_id")


class _OrderRecordingAgent(_RuntimeAgent):
    def __init__(self, log: list[str]):
        super().__init__()
        self._log = log

    async def handle_message(self, request):
        self._log.append("handle_message")
        await super().handle_message(request)


def test_agent_service_runs_turn_start_hooks_after_gate_before_agent() -> None:
    async def _run():
        log: list[str] = []
        controller = _TurnStartController(log)
        service = AgentService(controller=controller)
        controller.agent_service = service
        agent = _OrderRecordingAgent(log)
        service.register(agent)

        await service.handle_message("claude", _request("hi"))

        # on_running (gate confirmed) must precede the bubble hooks, which in turn
        # precede the agent run. This is what keeps a queued turn from claiming
        # the trigger id / posting a premature bubble before it actually starts.
        assert log == [
            "on_running",
            "update_thread_message_id",
            "begin_status_bubble",
            "handle_message",
        ]

    asyncio.run(_run())


def test_agent_service_turn_start_hooks_optional_and_guarded() -> None:
    async def _run():
        # A controller WITHOUT the turn-start hooks (and a dispatcher whose
        # bubble raises) must not break the turn.
        class _NoHookController:
            session_turns = None
            message_dispatcher = SimpleNamespace(
                begin_status_bubble=AsyncMock(side_effect=RuntimeError("bubble boom"))
            )

        service = AgentService(controller=_NoHookController())
        agent = _RuntimeAgent()
        service.register(agent)

        await service.handle_message("claude", _request("hi"))

        assert agent.started == ["hi"]

    asyncio.run(_run())


class _FailingTurnManager:
    def on_running(self, _context):
        raise RuntimeError("status failed")


def _request(message: str, runtime_key: str = "session:/repo"):
    return SimpleNamespace(
        context=SimpleNamespace(platform_specific={}),
        message=message,
        composite_session_id=runtime_key,
    )


def test_agent_service_dispatches_runtime_config_refresh() -> None:
    service = AgentService(controller=SimpleNamespace())
    runtime_config = object()
    agent = SimpleNamespace(name="codex", refresh_runtime_config=AsyncMock())
    service.register(agent)

    handled = asyncio.run(service.refresh_runtime_config("codex", runtime_config))

    assert handled is True
    agent.refresh_runtime_config.assert_awaited_once_with(runtime_config)


def test_agent_service_reports_missing_runtime_refresh_contract() -> None:
    service = AgentService(controller=SimpleNamespace())
    service.register(SimpleNamespace(name="codex"))

    assert asyncio.run(service.refresh_runtime_config("codex", object())) is False
    assert asyncio.run(service.refresh_runtime_config("claude", object())) is False


def test_agent_service_notifies_run_owner_when_activity_runtime_disconnects() -> None:
    settle = Mock()
    controller = SimpleNamespace(
        scheduled_task_service=SimpleNamespace(settle_activity_runs=settle),
    )
    service = AgentService(controller)
    service.activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="session-1",
        activity_id="task-1",
        kind="background_task",
        run_id="run-1",
    )
    original_ack = service.activities.ack_recovered_terminal
    service.activities.ack_recovered_terminal = Mock(wraps=original_ack)

    service.end_activity_runtime("claude", "runtime-1")

    assert service.activities.active_for_runtime("claude", "runtime-1") == []
    activity = settle.call_args.args[0]
    assert activity.id == "task-1"
    assert activity.run_id == "run-1"
    assert activity.status == "disconnected"
    service.activities.ack_recovered_terminal.assert_called_once_with(activity)


def test_activity_runtime_disconnect_retains_snapshot_until_run_settles(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    controller = SimpleNamespace(
        scheduled_task_service=SimpleNamespace(
            settle_activity_runs=Mock(side_effect=RuntimeError("store unavailable")),
        ),
    )
    service = AgentService(
        controller,
        activities=SessionActivityRegistry(store),
    )
    service.activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="session-1",
        activity_id="task-1",
        kind="background_task",
        run_id="run-1",
    )

    service.end_activity_runtime("claude", "runtime-1")

    records = store.list_activities()
    assert len(records) == 1
    assert records[0]["phase"] == "terminal"
    assert records[0]["activity"]["status"] == "disconnected"
    recovered = SessionActivityRegistry(store)
    terminal = recovered.drain_recovered_terminals()
    assert [(item.id, item.run_id, item.status) for item in terminal] == [
        ("task-1", "run-1", "disconnected"),
    ]
    engine.dispose()


def test_agent_service_serializes_same_runtime_until_terminal_release() -> None:
    async def _run():
        controller = _Controller()
        service = AgentService(controller=controller)
        controller.agent_service = service
        release_first = asyncio.Event()
        agent = _RuntimeAgent(release_first)
        service.register(agent)

        first_request = _request("first")
        first = asyncio.create_task(service.handle_message("claude", first_request))
        await asyncio.sleep(0)
        assert agent.started == ["first"]

        second_request = _request("second")
        second = asyncio.create_task(service.handle_message("claude", second_request))
        await asyncio.sleep(0.05)
        assert agent.started == ["first"]

        service.release_runtime_turn(first_request.context)
        release_first.set()
        await asyncio.wait_for(first, timeout=3)
        await asyncio.wait_for(second, timeout=3)

        assert agent.started == ["first", "second"]

    asyncio.run(_run())


def test_agent_service_restart_barrier_preserves_same_runtime_fifo() -> None:
    async def _run():
        controller = _Controller()
        service = AgentService(controller=controller)
        controller.agent_service = service
        agent = _RuntimeAgent()
        service.register(agent)
        service.begin_backend_drain("claude")

        first_request = _request("first")
        second_request = _request("second")
        first = asyncio.create_task(service.handle_message("claude", first_request))
        await asyncio.sleep(0)
        second = asyncio.create_task(service.handle_message("claude", second_request))
        await asyncio.sleep(0.01)

        assert agent.started == []
        assert service.runtime_turn_tokens_for_backend("claude") == {}

        service.end_backend_drain("claude")
        await asyncio.sleep(0)
        assert agent.started == ["first"]

        service.release_runtime_turn(first_request.context)
        await asyncio.wait_for(first, timeout=3)
        await asyncio.wait_for(second, timeout=3)
        assert agent.started == ["first", "second"]

    asyncio.run(_run())


def test_agent_service_restart_wait_releases_gate_when_backend_disappears() -> None:
    async def _run():
        controller = _Controller()
        service = AgentService(controller=controller)
        controller.agent_service = service
        service.register(_RuntimeAgent())
        service.begin_backend_drain("claude")

        request = _request("waiting")
        task = asyncio.create_task(service.handle_message("claude", request))
        await asyncio.sleep(0.01)
        gate = service._turn_gates[request.composite_session_id]
        assert gate.lock.locked() is True
        assert gate.token == ""

        service.agents.pop("claude")
        service.end_backend_drain("claude")
        with pytest.raises(KeyError):
            await task
        assert gate.lock.locked() is False

    asyncio.run(_run())


def test_force_end_backend_activities_settles_pending_runs() -> None:
    settled = []
    controller = SimpleNamespace(
        scheduled_task_service=SimpleNamespace(settle_activity_runs=settled.append),
    )
    service = AgentService(controller=controller)
    service.activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-active",
        kind="background_task",
        run_id="run-active",
    )
    service.activities.start(
        backend="claude",
        runtime_key="runtime-2",
        session_id="ses-2",
        activity_id="task-pending",
        kind="background_task",
        run_id="run-pending",
    )
    service.activities.complete(
        backend="claude",
        runtime_key="runtime-2",
        activity_id="task-pending",
        status="completed",
        expects_output=True,
    )
    original_ack = service.activities.ack_recovered_terminal
    service.activities.ack_recovered_terminal = Mock(wraps=original_ack)

    service.force_end_backend_activities("claude")

    assert sorted((item.id, item.status) for item in settled) == [
        ("task-active", "killed"),
        ("task-pending", "killed"),
    ]
    assert service.activities.ack_recovered_terminal.call_count == 2


def test_force_end_backend_activities_waits_for_run_settlement_before_ack() -> None:
    controller = SimpleNamespace(
        scheduled_task_service=SimpleNamespace(
            settle_activity_runs=Mock(side_effect=RuntimeError("store unavailable")),
        ),
    )
    service = AgentService(controller=controller)
    service.activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-active",
        kind="background_task",
        run_id="run-active",
    )
    original_ack = service.activities.ack_recovered_terminal
    service.activities.ack_recovered_terminal = Mock(wraps=original_ack)

    completed = service.force_end_backend_activities("claude")

    assert [(item.id, item.status) for item in completed] == [
        ("task-active", "killed"),
    ]
    service.activities.ack_recovered_terminal.assert_not_called()


def test_force_cancel_backend_turns_emits_terminal_before_release() -> None:
    async def _run():
        controller = _Controller()
        controller.emit_agent_message = AsyncMock()
        service = AgentService(controller=controller)
        controller.agent_service = service
        agent = _RuntimeAgent(asyncio.Event())
        service.register(agent)
        request = _request("first")
        task = asyncio.create_task(service.handle_message("claude", request))
        await asyncio.sleep(0)
        assert service.runtime_turn_tokens_for_backend("claude")

        await service.force_cancel_backend_turns("claude")

        assert task.cancelled()
        controller.emit_agent_message.assert_awaited_once_with(
            request.context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=terminal_turn_output(),
        )
        assert service.runtime_turn_tokens_for_backend("claude") == {}

    asyncio.run(_run())


class _RecordingIndicator:
    """Records the queued/promote/finish reaction calls the gate drives."""

    def __init__(self, log: list[str]):
        self._log = log

    async def show_queued_reaction(self, _request):
        self._log.append("show_queued")
        return True

    async def promote_reaction_to_running(self, _request, *, agent_name=None):
        self._log.append("promote")

    async def finish(self, _request_or_handle):
        self._log.append("finish")


class _IndicatorController(_Controller):
    def __init__(self, log: list[str]):
        super().__init__()
        self.processing_indicator = _RecordingIndicator(log)


def test_agent_service_shows_queued_reaction_then_promotes_on_start() -> None:
    async def _run():
        log: list[str] = []
        controller = _IndicatorController(log)
        service = AgentService(controller=controller)
        controller.agent_service = service
        release_first = asyncio.Event()
        agent = _RuntimeAgent(release_first)
        service.register(agent)

        first_request = _request("first")
        first = asyncio.create_task(service.handle_message("claude", first_request))
        await asyncio.sleep(0)
        assert agent.started == ["first"]

        second = asyncio.create_task(service.handle_message("claude", _request("second")))
        await asyncio.sleep(0.05)
        # Only the SECOND message is queued (the first holds the gate): it shows the
        # queued 👌 and is blocked on acquire(). The first ran uncontended, so it
        # never showed 👌 and has already promoted to 👀.
        assert "show_queued" in log
        assert log.count("promote") == 1

        service.release_runtime_turn(first_request.context)
        release_first.set()
        await asyncio.wait_for(first, timeout=3)
        await asyncio.wait_for(second, timeout=3)

        assert log.count("show_queued") == 1  # only the genuinely-queued turn shows 👌
        assert log.count("promote") == 2  # both turns promoted when they started
        assert "finish" not in log  # no cancellation occurred

    asyncio.run(_run())


def test_agent_service_cleans_queued_reaction_when_cancelled_while_waiting() -> None:
    async def _run():
        log: list[str] = []
        controller = _IndicatorController(log)
        service = AgentService(controller=controller)
        controller.agent_service = service
        release_first = asyncio.Event()
        agent = _RuntimeAgent(release_first)
        service.register(agent)

        first_request = _request("first")
        first = asyncio.create_task(service.handle_message("claude", first_request))
        await asyncio.sleep(0)
        assert agent.started == ["first"]

        second = asyncio.create_task(service.handle_message("claude", _request("second")))
        await asyncio.sleep(0.05)
        assert "show_queued" in log

        # Cancel the queued message while it is still blocked on gate.acquire():
        # its queued 👌 must be cleaned up (P0.1), and it must never promote.
        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass

        assert "finish" in log
        assert log.count("promote") == 1

        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    asyncio.run(_run())


class _ReactionIM:
    """Records the real reaction add/remove calls per message id."""

    def __init__(self):
        self.events: list[tuple[str, str, str]] = []

    async def add_reaction(self, context, message_id, emoji):
        self.events.append(("add", message_id, emoji))
        return True

    async def remove_reaction(self, context, message_id, emoji):
        self.events.append(("remove", message_id, emoji))
        return True


class _RealIndicatorController(_Controller):
    """Wires the REAL ProcessingIndicatorService so the gate exercises the actual
    show_queued/promote path (not a recording stub)."""

    def __init__(self, im: _ReactionIM):
        super().__init__()
        from core.processing_indicator import ProcessingIndicatorService

        self.config = SimpleNamespace(ack_mode="reaction", language="en")
        self._im = im
        self.processing_indicator = ProcessingIndicatorService(self)

    def get_im_client_for_context(self, _context):
        return self._im


def _reaction_request(message: str, message_id: str, runtime_key: str = "session:/repo"):
    from core.processing_indicator import ProcessingIndicatorHandle

    context = SimpleNamespace(
        platform_specific={},
        message_id=message_id,
        platform="slack",
        channel_id="C1",
    )
    handle = ProcessingIndicatorHandle(context=context, reaction_indicator_selected=True)
    return SimpleNamespace(
        context=context,
        message=message,
        composite_session_id=runtime_key,
        processing_indicator=handle,
        ack_reaction_message_id=None,
        ack_reaction_emoji=None,
        ack_message_id=None,
        typing_indicator_active=False,
        typing_indicator_task=None,
    )


def test_real_indicator_shows_queued_reaction_on_second_message_while_first_holds_gate() -> None:
    """High-fidelity: the REAL ProcessingIndicatorService must put 👌 on the SECOND
    message while the FIRST still holds the runtime gate, then promote it to 👀."""

    async def _run():
        from core.processing_indicator import ACK_REACTION_EMOJI, QUEUED_REACTION_EMOJI

        im = _ReactionIM()
        controller = _RealIndicatorController(im)
        service = AgentService(controller=controller)
        controller.agent_service = service
        release_first = asyncio.Event()
        agent = _RuntimeAgent(release_first)
        service.register(agent)

        req1 = _reaction_request("first", "m1")
        first = asyncio.create_task(service.handle_message("claude", req1))
        for _ in range(200):
            if ("add", "m1", ACK_REACTION_EMOJI) in im.events:
                break
            await asyncio.sleep(0)
        # First (uncontended) has started and shows the running 👀 directly — it must
        # NOT have shown the queued 👌 (no 👌→👀 flash for a non-queued message).
        assert ("add", "m1", ACK_REACTION_EMOJI) in im.events, im.events
        assert ("add", "m1", QUEUED_REACTION_EMOJI) not in im.events, im.events

        req2 = _reaction_request("second", "m2")
        second = asyncio.create_task(service.handle_message("claude", req2))
        for _ in range(200):
            if ("add", "m2", QUEUED_REACTION_EMOJI) in im.events:
                break
            await asyncio.sleep(0)

        # THE KEY ASSERTION: the queued 👌 is on m2 while m1's turn still holds the
        # gate (m2 has not been promoted to 👀 yet).
        assert ("add", "m2", QUEUED_REACTION_EMOJI) in im.events, im.events
        assert ("add", "m2", ACK_REACTION_EMOJI) not in im.events, im.events

        release_first.set()
        service.release_runtime_turn(req1.context)
        await asyncio.wait_for(first, timeout=3)
        await asyncio.wait_for(second, timeout=3)

        # Once m2's turn starts, 👌 is removed and 👀 added on m2.
        assert ("remove", "m2", QUEUED_REACTION_EMOJI) in im.events, im.events
        assert ("add", "m2", ACK_REACTION_EMOJI) in im.events, im.events

    asyncio.run(_run())


def test_processing_indicator_tracked_before_promote() -> None:
    """Regression (Codex P2): the indicator handle must be tracked (registered for
    terminal/cancel cleanup) BEFORE the promote await, so a cancel during promote
    can still find and finish it instead of leaking the queued/fallback indicator."""

    async def _run():
        log: list[str] = []

        class _Indicator:
            def track_turn(self, _context, _request):
                log.append("track")

            async def show_queued_reaction(self, _request):
                return False

            async def promote_reaction_to_running(self, _request, *, agent_name=None):
                log.append("promote")

            async def finish(self, _request_or_handle):
                log.append("finish")

        controller = _Controller()
        controller.processing_indicator = _Indicator()
        service = AgentService(controller=controller)
        controller.agent_service = service
        service.register(_RuntimeAgent())

        request = _request("first")
        request.processing_indicator = object()  # truthy so _track_processing_indicator_turn runs
        await service.handle_message("claude", request)

        assert log[:2] == ["track", "promote"], log

    asyncio.run(_run())


def test_gate_released_when_cancelled_during_promote() -> None:
    """Regression (Codex P1): a cancel after acquire() but during the
    settle/promote awaits must still release the runtime gate, or the token + lock
    leak and later prompts for the same runtime key block forever."""

    async def _run():
        promote_started = asyncio.Event()
        block = asyncio.Event()

        class _BlockingIndicator:
            async def show_queued_reaction(self, _request):
                return False

            async def promote_reaction_to_running(self, _request, *, agent_name=None):
                promote_started.set()
                await block.wait()  # cancel hits here, inside the managed try

            async def finish(self, _request_or_handle):
                return None

        controller = _Controller()
        controller.processing_indicator = _BlockingIndicator()
        controller.emit_agent_message = AsyncMock()  # used by the cancel tidy
        service = AgentService(controller=controller)
        controller.agent_service = service
        service.register(_RuntimeAgent())

        request = _request("first")
        task = asyncio.create_task(service.handle_message("claude", request))
        await asyncio.wait_for(promote_started.wait(), timeout=2)
        gate = service._get_turn_gate("session:/repo")
        assert gate.lock.locked()  # acquired, currently in promote

        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2)
        except BaseException:
            pass

        # The CancelledError handler schedules a tidy task that releases the gate.
        released = False
        for _ in range(200):
            await asyncio.sleep(0)
            if not gate.lock.locked() and gate.token == "":
                released = True
                break
        assert released  # gate token + lock released despite cancel during promote

    asyncio.run(_run())


def test_queued_reaction_does_not_delay_gate_acquire_fifo() -> None:
    """Regression (Codex P1): a contended turn must reserve its FIFO place on the
    runtime gate WITHOUT waiting for the (network) queued-reaction call. If the 👌
    add were awaited before acquire(), a later same-runtime message could acquire
    first and reorder prompts."""

    async def _run():
        log: list[str] = []
        block = asyncio.Event()

        class _SlowIndicator:
            async def show_queued_reaction(self, _request):
                log.append("show_queued_start")
                await block.wait()  # simulate slow Slack/Feishu reaction API
                return True

            async def promote_reaction_to_running(self, _request, *, agent_name=None):
                log.append("promote")

            async def finish(self, _request_or_handle):
                log.append("finish")

        controller = _Controller()
        controller.processing_indicator = _SlowIndicator()
        service = AgentService(controller=controller)
        controller.agent_service = service
        release_first = asyncio.Event()
        agent = _RuntimeAgent(release_first)
        service.register(agent)

        first_request = _request("first")
        first = asyncio.create_task(service.handle_message("claude", first_request))
        await asyncio.sleep(0)
        assert agent.started == ["first"]
        gate = service._get_turn_gate("session:/repo")

        second = asyncio.create_task(service.handle_message("claude", _request("second")))
        # Let the second turn run far enough to fire the queued-reaction task and
        # reach acquire(). Its show_queued is blocked forever, so if acquire() were
        # gated behind it the gate would have NO waiter.
        entered_queue = False
        for _ in range(200):
            await asyncio.sleep(0)
            if not entered_queue and gate.lock._waiters and len(gate.lock._waiters) >= 1:
                entered_queue = True
            if entered_queue and "show_queued_start" in log:
                break
        # The turn reserved its FIFO place on the gate (it is a waiter) even though
        # its queued reaction is still blocked — proving acquire() was NOT gated
        # behind the reaction network call.
        assert entered_queue
        assert "show_queued_start" in log  # the queued reaction was kicked off concurrently

        # Cleanup: unblock everything and let the tasks settle.
        block.set()
        release_first.set()
        service.release_runtime_turn(first_request.context)
        for task in (first, second):
            try:
                await asyncio.wait_for(task, timeout=3)
            except Exception:
                pass

    asyncio.run(_run())


def test_agent_service_allows_distinct_runtime_keys_in_parallel() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _RuntimeAgent()
        service.register(agent)

        await asyncio.gather(
            service.handle_message("claude", _request("one", "one:/repo")),
            service.handle_message("claude", _request("two", "two:/repo")),
        )

        assert sorted(agent.started) == ["one", "two"]

    asyncio.run(_run())


def test_agent_service_falls_back_to_request_runtime_key_for_legacy_agent_stubs() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        handled = []

        async def _handle_message(request):
            handled.append(request)

        service.register(SimpleNamespace(name="legacy", handle_message=_handle_message))
        request = _request("hello", "legacy:/repo")

        await service.handle_message("legacy", request)

        assert handled == [request]
        assert request.context.platform_specific["agent_runtime_turn_key"] == "legacy:/repo"
        service.release_runtime_turn(request.context)

    asyncio.run(_run())


def test_agent_service_runtime_guard_drops_stale_emits_after_next_turn_starts() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _RuntimeAgent()
        service.register(agent)
        first = _request("first")
        second = _request("second")

        await service.handle_message("claude", first)
        assert service.emit_matches_runtime_turn(first.context)
        service.release_runtime_turn(first.context)

        await service.handle_message("claude", second)

        assert service.emit_matches_runtime_turn(second.context)
        assert not service.emit_matches_runtime_turn(first.context)

    asyncio.run(_run())


def test_agent_service_clear_sessions_releases_cleared_runtime_gates() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _ClearingRuntimeAgent({"session:/repo", "session:/other"})
        service.register(agent)

        for runtime_key in ("session:/repo", "session:/other", "unrelated:/repo"):
            gate = service._get_turn_gate(runtime_key)
            await gate.lock.acquire()
            gate.token = f"{runtime_key}-token"
            gate.backend = "codex"

        cleared = await service.clear_sessions("scope-1")

        assert cleared == {"codex": 1}
        assert agent.clear_calls == ["scope-1"]
        assert not service._turn_gates["session:/repo"].lock.locked()
        assert not service._turn_gates["session:/other"].lock.locked()
        assert service._turn_gates["unrelated:/repo"].lock.locked()

        service.release_runtime_turn_key("unrelated:/repo")

    asyncio.run(_run())


def test_agent_service_clear_sessions_does_not_release_new_turn_token() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        runtime_key = "session:/repo"
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "old-token"
        gate.backend = "codex"

        async def _on_clear():
            service.release_runtime_turn_key(runtime_key, "old-token")
            await gate.lock.acquire()
            gate.token = "new-token"
            gate.backend = "codex"

        agent = _ClearingRuntimeAgent({runtime_key}, on_clear=_on_clear)
        service.register(agent)

        cleared = await service.clear_sessions("scope-1")

        assert cleared == {"codex": 1}
        assert service._turn_gates[runtime_key].lock.locked()
        assert service._turn_gates[runtime_key].token == "new-token"

        service.release_runtime_turn_key(runtime_key, "new-token")

    asyncio.run(_run())


def test_agent_service_marks_runtime_started_from_matching_context_only() -> None:
    service = AgentService(controller=_Controller())
    runtime_key = "session:/repo"
    gate = service._get_turn_gate(runtime_key)
    gate.token = "runtime-token"
    gate.backend = "claude"
    context = SimpleNamespace(
        platform_specific={
            "agent_runtime_turn_key": runtime_key,
            "agent_runtime_turn_token": "runtime-token",
        }
    )

    service.mark_runtime_turn_started(context)

    assert gate.runtime_started is True

    stale_context = SimpleNamespace(
        platform_specific={
            "agent_runtime_turn_key": runtime_key,
            "agent_runtime_turn_token": "old-token",
        }
    )
    gate.runtime_started = False

    service.mark_runtime_turn_started(stale_context)

    assert gate.runtime_started is False


def test_agent_service_clear_backend_sessions_does_not_release_other_backend_gate() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        runtime_key = "session:/repo"
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "claude-token"
        gate.backend = "claude"
        agent = _ClearingRuntimeAgent({runtime_key})
        service.register(agent)

        count = await service.clear_backend_sessions("codex", "scope-1")

        assert count == 1
        assert gate.lock.locked()
        assert gate.token == "claude-token"
        service.release_runtime_turn_key(runtime_key, "claude-token")

    asyncio.run(_run())


def test_agent_service_refresh_runtime_config_releases_backend_gates() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        runtime_key = "session:/repo"
        gate = service._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = "refresh-token"
        gate.backend = "codex"
        agent = _RefreshingRuntimeAgent({runtime_key})
        service.register(agent)
        runtime_config = object()

        handled = await service.refresh_runtime_config("codex", runtime_config)

        assert handled is True
        assert agent.refresh_calls == [runtime_config]
        assert not gate.lock.locked()
        assert gate.token == ""
        assert gate.backend == ""

    asyncio.run(_run())


def test_agent_service_releases_runtime_gate_for_stale_stop() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _StopRuntimeAgent("not_active")
        service.register(agent)
        request = _request("stop")
        gate = service._get_turn_gate("session:/repo")
        await gate.lock.acquire()
        gate.token = "stop-token"
        gate.backend = "claude"
        gate.runtime_started = True

        handled = await service.handle_stop("claude", request)

        assert handled is False
        assert not gate.lock.locked()
        assert request.context.platform_specific["agent_runtime_turn_token"] == "stop-token"

    asyncio.run(_run())


def test_agent_service_keeps_runtime_gate_for_startup_window_stop() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _StopRuntimeAgent("not_active")
        service.register(agent)
        request = _request("stop")
        gate = service._get_turn_gate("session:/repo")
        await gate.lock.acquire()
        gate.token = "stop-token"
        gate.backend = "codex"
        gate.runtime_started = False

        handled = await service.handle_stop("claude", request)

        assert handled is False
        assert gate.lock.locked()
        assert gate.token == "stop-token"
        service.release_runtime_turn_key("session:/repo", "stop-token")

    asyncio.run(_run())


def test_agent_service_keeps_runtime_gate_for_interrupt_failure_stop() -> None:
    async def _run():
        service = AgentService(controller=_Controller())
        agent = _StopRuntimeAgent("interrupt_failed")
        service.register(agent)
        request = _request("stop")
        gate = service._get_turn_gate("session:/repo")
        await gate.lock.acquire()
        gate.token = "stop-token"
        gate.backend = "claude"
        gate.runtime_started = True

        handled = await service.handle_stop("claude", request)

        assert handled is False
        assert gate.lock.locked()
        service.release_runtime_turn_key("session:/repo", "stop-token")

    asyncio.run(_run())


def test_agent_service_releases_gate_when_on_running_fails() -> None:
    async def _run():
        controller = _Controller()
        controller.session_turns = _FailingTurnManager()
        service = AgentService(controller=controller)
        agent = _RuntimeAgent()
        service.register(agent)
        request = _request("hello")

        try:
            await service.handle_message("claude", request)
        except RuntimeError as err:
            assert str(err) == "status failed"
        else:
            raise AssertionError("on_running failure should escape")

        gate = service._turn_gates["session:/repo"]
        assert not gate.lock.locked()
        assert gate.token == ""
        assert gate.runtime_started is False
        assert agent.started == []

    asyncio.run(_run())


def test_agent_service_schedules_terminal_tidy_on_cancellation() -> None:
    # C3: a turn cancelled mid-flight (shutdown / SIGTERM) must SCHEDULE a silent
    # terminal result so the outbound chokepoint collapses the stuck status bubble
    # + settles the dot, then re-raise CancelledError.
    async def _run():
        controller = _Controller()
        emit_calls: list[tuple[tuple, dict]] = []
        token_at_emit: list[str] = []
        emitted = asyncio.Event()

        async def _emit(*args, **kwargs):
            # Capture the runtime-turn token AT EMIT TIME. The fix requires the
            # tidy emit to run while the turn is STILL current (token not yet
            # cleared) — otherwise the real result branch drops it as stale and
            # the bubble never collapses. An early release would make this "".
            gate = service._turn_gates.get("session:/repo")
            token_at_emit.append(gate.token if gate else "")
            emit_calls.append((args, kwargs))
            emitted.set()

        controller.emit_agent_message = _emit
        service = AgentService(controller=controller)
        controller.agent_service = service

        hanging = asyncio.Event()

        class _HangingAgent(_RuntimeAgent):
            async def handle_message(self, _request):
                await hanging.wait()  # block until cancelled

        agent = _HangingAgent()
        service.register(agent)
        request = _request("cancel-me")

        task = asyncio.create_task(service.handle_message("claude", request))
        await asyncio.sleep(0.05)  # let it reach the hanging agent
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("CancelledError should propagate")

        # The terminal tidy emit was scheduled (create_task) → let it run.
        await asyncio.wait_for(emitted.wait(), timeout=2.0)
        assert len(emit_calls) == 1
        args, kwargs = emit_calls[0]
        # emit(context, "result", "", is_error=True, level="silent")
        assert args[0] is request.context
        assert args[1] == "result"
        assert kwargs.get("is_error") is True
        assert kwargs.get("level") == "silent"
        # The turn was STILL current at emit time (token not cleared first) so the
        # tidy isn't dropped as stale — this is the ordering the C3 fix guarantees.
        assert token_at_emit and token_at_emit[0]
        # Gate released AFTER the tidy emit so a later prompt can't hang behind the
        # cancelled turn.
        assert not service._turn_gates["session:/repo"].token

    asyncio.run(_run())


def test_agent_service_releases_gate_when_exception_terminal_emit_fails() -> None:
    """HFR-001: pre-accept failure releases ownership even if terminal emit fails."""

    async def _run():
        controller = _Controller()

        async def _emit(*_args, **_kwargs):
            raise RuntimeError("send failed")

        controller.emit_agent_message = _emit
        service = AgentService(controller=controller)
        agent = _RaisingRuntimeAgent()
        service.register(agent)
        request = _request("boom")

        try:
            await service.handle_message("claude", request)
        except RuntimeError as err:
            assert str(err) == "backend failed"
        else:
            raise AssertionError("backend exception should escape")

        assert not service._turn_gates["session:/repo"].lock.locked()

    asyncio.run(_run())


def test_agent_service_recovers_accepted_turn_when_owned_backend_dies() -> None:
    """A dead accepted backend must release the runtime FIFO without a timeout."""

    async def _run() -> None:
        controller = _Controller()
        recovered = asyncio.Event()
        service = AgentService(controller=controller)
        controller.agent_service = service

        class _AcceptedThenDeadAgent(_RuntimeAgent):
            def __init__(self) -> None:
                super().__init__()
                self.alive = True

            async def handle_message(self, request) -> None:
                self.started.append(request.message)
                service.mark_runtime_turn_started(request.context)

            def backend_alive(self, _context):
                return self.alive

        agent = _AcceptedThenDeadAgent()
        service.register(agent)
        service._liveness_probe_interval_seconds = 0.01
        service._liveness_failure_grace_seconds = 0.02

        async def _emit(context, message_type, text, **kwargs):
            assert message_type == "result"
            assert text == ""
            assert kwargs["is_error"] is True
            assert kwargs["level"] == "silent"
            assert kwargs["terminal_error"] == "backend_runtime_exited_before_terminal"
            service.release_runtime_turn(context)
            recovered.set()

        controller.emit_agent_message = _emit

        first = _request("first")
        second = _request("second")
        await service.handle_message("claude", first)
        assert service._turn_gates["session:/repo"].lock.locked()

        successor = asyncio.create_task(service.handle_message("claude", second))
        await asyncio.sleep(0)
        assert agent.started == ["first"]

        agent.alive = False
        try:
            await asyncio.wait_for(recovered.wait(), timeout=0.5)
            await asyncio.wait_for(successor, timeout=0.5)
            assert agent.started == ["first", "second"]
        finally:
            if not successor.done():
                successor.cancel()
                await asyncio.gather(successor, return_exceptions=True)
            service.release_runtime_turn(second.context)

    asyncio.run(_run())


def test_agent_service_recovery_uses_the_accepted_backend_generation() -> None:
    """HFR-002: a replacement runtime cannot hide the accepted owner's death."""

    async def _run() -> None:
        controller = _Controller()
        recovered = asyncio.Event()
        service = AgentService(controller=controller)
        controller.agent_service = service

        class _GenerationAgent(_RuntimeAgent):
            def __init__(self) -> None:
                super().__init__()
                self.current = SimpleNamespace(alive=True)

            async def handle_message(self, request) -> None:
                self.started.append(request.message)
                service.mark_runtime_turn_started(request.context)

            def backend_alive(self, _context):
                return self.current.alive

            def capture_backend_liveness(self, _context):
                accepted = self.current
                return lambda: accepted.alive

        agent = _GenerationAgent()
        service.register(agent)
        service._liveness_probe_interval_seconds = 0.005
        service._liveness_failure_grace_seconds = 0.005

        async def _emit(context, *_args, **_kwargs):
            service.release_runtime_turn(context)
            recovered.set()

        controller.emit_agent_message = _emit
        first = _request("first")
        second = _request("second")
        await service.handle_message("claude", first)
        accepted = agent.current
        successor = asyncio.create_task(service.handle_message("claude", second))
        await asyncio.sleep(0)

        agent.current = SimpleNamespace(alive=True)
        accepted.alive = False
        try:
            await asyncio.wait_for(recovered.wait(), timeout=0.5)
            await asyncio.wait_for(successor, timeout=0.5)
            assert agent.started == ["first", "second"]
            assert service.backend_alive(second.context) is True
        finally:
            if not successor.done():
                successor.cancel()
                await asyncio.gather(successor, return_exceptions=True)
            service.release_runtime_turn(second.context)

    asyncio.run(_run())


@pytest.mark.parametrize("liveness", [True, None])
def test_agent_service_never_steals_live_or_unknown_long_running_owner(liveness) -> None:
    """HFR-005/HFR-006: age alone never releases a live or unknown owner."""

    async def _run() -> None:
        controller = _Controller()
        service = AgentService(controller=controller)
        controller.agent_service = service
        controller.emit_agent_message = AsyncMock(side_effect=AssertionError("live owner was stolen"))

        class _LongRunningAgent(_RuntimeAgent):
            async def handle_message(self, request) -> None:
                self.started.append(request.message)
                service.mark_runtime_turn_started(request.context)

            def backend_alive(self, _context):
                return liveness

        agent = _LongRunningAgent()
        service.register(agent)
        service._liveness_probe_interval_seconds = 0.005
        service._liveness_failure_grace_seconds = 0.005

        first = _request("first")
        second = _request("second")
        await service.handle_message("claude", first)
        successor = asyncio.create_task(service.handle_message("claude", second))
        try:
            await asyncio.sleep(0.06)
            assert agent.started == ["first"]
            assert not successor.done()
            controller.emit_agent_message.assert_not_awaited()

            service.release_runtime_turn(first.context)
            await asyncio.wait_for(successor, timeout=0.5)
            assert agent.started == ["first", "second"]
        finally:
            if not successor.done():
                successor.cancel()
                await asyncio.gather(successor, return_exceptions=True)
            service.release_runtime_turn(second.context)

    asyncio.run(_run())


def test_agent_service_requires_dead_backend_after_grace_before_recovery() -> None:
    """HFR-006: a transient false probe cannot release the accepted owner."""

    async def _run() -> None:
        controller = _Controller()
        service = AgentService(controller=controller)
        controller.agent_service = service
        controller.emit_agent_message = AsyncMock(side_effect=AssertionError("false blip stole the owner"))

        class _FlappingAgent(_RuntimeAgent):
            alive = True

            async def handle_message(self, request) -> None:
                self.started.append(request.message)
                service.mark_runtime_turn_started(request.context)

            def backend_alive(self, _context):
                return self.alive

        agent = _FlappingAgent()
        service.register(agent)
        service._liveness_probe_interval_seconds = 0.005
        service._liveness_failure_grace_seconds = 0.04

        request = _request("first")
        await service.handle_message("claude", request)
        agent.alive = False
        await asyncio.sleep(0.015)
        agent.alive = True
        await asyncio.sleep(0.06)

        assert service._turn_gates["session:/repo"].lock.locked()
        assert service.runtime_turn_started(request.context) is True
        controller.emit_agent_message.assert_not_awaited()
        service.release_runtime_turn(request.context)

    asyncio.run(_run())


def test_late_terminal_from_recovered_owner_cannot_release_successor() -> None:
    """HFR-007: token identity makes dead-owner recovery and late results idempotent."""

    async def _run() -> None:
        controller = _Controller()
        recovered = asyncio.Event()
        service = AgentService(controller=controller)
        controller.agent_service = service

        class _RecoveringAgent(_RuntimeAgent):
            alive = True

            async def handle_message(self, request) -> None:
                self.started.append(request.message)
                service.mark_runtime_turn_started(request.context)

            def backend_alive(self, _context):
                return self.alive

        agent = _RecoveringAgent()
        service.register(agent)
        service._liveness_probe_interval_seconds = 0.005
        service._liveness_failure_grace_seconds = 0.005
        terminal_calls = 0

        async def _emit(context, *_args, **_kwargs):
            nonlocal terminal_calls
            terminal_calls += 1
            service.release_runtime_turn(context)
            recovered.set()

        controller.emit_agent_message = _emit
        first = _request("first")
        second = _request("second")
        await service.handle_message("claude", first)
        first_token = first.context.platform_specific["agent_runtime_turn_token"]
        successor = asyncio.create_task(service.handle_message("claude", second))
        agent.alive = False
        try:
            await asyncio.wait_for(recovered.wait(), timeout=0.5)
            agent.alive = True
            await asyncio.wait_for(successor, timeout=0.5)
            gate = service._turn_gates["session:/repo"]
            successor_token = gate.token
            assert successor_token and successor_token != first_token

            # The old context still carries the old token. A delayed result's
            # normal release hook must not touch the successor owner.
            service.release_runtime_turn(first.context)
            assert gate.token == successor_token
            assert gate.lock.locked()
            assert terminal_calls == 1
        finally:
            if not successor.done():
                successor.cancel()
                await asyncio.gather(successor, return_exceptions=True)
            service.release_runtime_turn(second.context)

    asyncio.run(_run())


def test_real_child_exit_after_acceptance_recovers_runtime_fifo() -> None:
    """HFR-002: an owned OS child exit drives the same shared recovery path."""

    async def _run() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        transport = CodexTransport(binary=sys.executable, cwd="/")
        transport._process = process
        transport._reader_task = asyncio.create_task(transport._reader_loop())

        controller = _Controller()
        recovered = asyncio.Event()
        service = AgentService(controller=controller)
        controller.agent_service = service

        class _ProcessBackedAgent(_RuntimeAgent):
            name = "codex"

            async def handle_message(self, request) -> None:
                self.started.append(request.message)
                service.mark_runtime_turn_started(request.context)

            def backend_alive(self, _context):
                return transport.is_alive

        agent = _ProcessBackedAgent()
        service.register(agent)
        service._liveness_probe_interval_seconds = 0.005
        service._liveness_failure_grace_seconds = 0.005
        terminal_calls = 0

        async def _emit(context, *_args, **_kwargs):
            nonlocal terminal_calls
            terminal_calls += 1
            service.release_runtime_turn(context)
            recovered.set()

        controller.emit_agent_message = _emit
        first = _request("first", runtime_key="codex:/repo")
        second = _request("second", runtime_key="codex:/repo")
        successor = None
        try:
            await service.handle_message("codex", first)
            successor = asyncio.create_task(service.handle_message("codex", second))
            process.kill()
            await process.wait()
            await transport._reader_task

            await asyncio.wait_for(recovered.wait(), timeout=0.5)
            await asyncio.wait_for(successor, timeout=0.5)
            assert agent.started == ["first", "second"]
            assert terminal_calls == 1
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if successor is not None and not successor.done():
                successor.cancel()
                await asyncio.gather(successor, return_exceptions=True)
            service.release_runtime_turn(second.context)

    asyncio.run(_run())
