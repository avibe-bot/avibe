"""A terminal emit's Turn commit against the real runner (#2714).

Each case runs the real ``SessionTurnManager._run``, the real ``dispatch_turn`` and
the real dispatcher terminal emit, so the race between the emit's evidence-carrying
Turn write and the runner's own release is the one production has.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.controller import Controller
from core.message_dispatcher import ActivityOutputDeliveryError, ConsolidatedMessageDispatcher
from core.message_output import MessageOutput
from core.native_dispatch_phase import DISPATCH_PHASE_ATTEMPTING, set_dispatch_phase
from core.session_turns import SessionTurnManager
from sqlalchemy import update

from storage import message_deliveries as delivery_store
from storage.models import agent_sessions, messages
from tests.test_message_dispatcher_result_fallback import (
    _StubIMClient,
    _StubSessionHandler,
    _StubSettingsManager,
)
from core.session_turns import DeliveryRequest
from core.message_context import resolve_turn_sink_key
from tests.test_session_delivery_fsm import (  # noqa: F401 - pytest fixtures
    _activate,
    _context,
    _fsm_schema_template,
    managers,
)

RESULT = "the answer"
# Bounds every wait on the runner or the emit: a Turn whose runner is never
# released fails the test instead of hanging it.
WAIT_S = 5


class _Indicator:
    """An IM processing indicator whose terminal cleanup awaits the network."""

    def __init__(self) -> None:
        self.finished = 0

    async def finish_terminal_turn(self, _context) -> None:
        await asyncio.sleep(0)
        self.finished += 1


class _IMClient(_StubIMClient):
    """Accepts the done-footer subtext a status-bubble Turn delivers its result with."""

    async def send_message(self, context, text, parse_mode=None, reply_to=None, **_kwargs):
        return await super().send_message(context, text, parse_mode=parse_mode, reply_to=reply_to)


class _Harness:
    def __init__(self, manager: SessionTurnManager, engine, monkeypatch) -> None:
        self.manager = manager
        self.engine = engine
        self.receivers: list[asyncio.Task] = []
        self.emit: dict = {"text": RESULT}
        controller = manager.controller
        controller.session_turns = manager
        controller.register_turn_sink = manager.register_turn_sink
        controller.get_turn_sink = manager.get_turn_sink
        controller.pop_turn_sink = manager.pop_turn_sink
        controller.mark_turn_complete = Controller.mark_turn_complete.__get__(controller)
        controller.emit_agent_message = Controller.emit_agent_message.__get__(controller)
        controller.processing_indicator = _Indicator()
        controller.im_client = _IMClient()
        controller.get_im_client_for_context = lambda _context: controller.im_client
        controller.get_settings_manager_for_context = lambda _context: _StubSettingsManager()
        controller._get_settings_key = lambda context: context.channel_id
        controller.config = SimpleNamespace(language="en", platform="avibe", reply_enhancements=False)
        controller.session_handler = _StubSessionHandler()
        controller.agent_service.release_runtime_turn = Mock()
        controller.message_handler = SimpleNamespace(handle_user_message=self._handle_user_message)
        self.controller = controller
        self.dispatcher = ConsolidatedMessageDispatcher(controller)
        controller.message_dispatcher = self.dispatcher
        self.persisted: list[str] = []

        def persist(_context, _type, text, **_kwargs):
            self.persisted.append(text)
            return {"id": f"row-{len(self.persisted)}", "native_message_id": None}

        monkeypatch.setattr("core.message_dispatcher.persist_agent_message", persist)
        monkeypatch.setattr("core.message_dispatcher.agent_message_exists", lambda *_args: None)

    async def _handle_user_message(self, context, _text, **_kwargs):
        set_dispatch_phase(context, DISPATCH_PHASE_ATTEMPTING)
        self.receivers.append(asyncio.ensure_future(self.controller.emit_agent_message(context, "result", **self.emit)))
        return None

    async def start(self, turn_id: str, context) -> asyncio.Task:
        # The fixture's admission records starts without running them; this Turn
        # runs on the real runner from here.
        self.manager._run = SessionTurnManager._run.__get__(self.manager, SessionTurnManager)
        with self.engine.connect() as conn:
            initial = delivery_store.initial_deliveries_for_turn(conn, turn_id)[0]
        await self.manager._run(
            "ses_fsm",
            context,
            "prompt",
            logical_turn_id=turn_id,
            delivery_id=str(initial["id"]),
            durable_preallocated=True,
        )
        return self.manager.in_flight["ses_fsm"].task

    async def receiver(self) -> asyncio.Task:
        while not self.receivers:
            await asyncio.sleep(0)
        return self.receivers[0]

    def turn(self, turn_id: str) -> dict:
        with self.engine.connect() as conn:
            turn = delivery_store.get_turn(conn, turn_id)
        assert turn is not None
        return turn


def _evidence(turn: dict) -> dict:
    return json.loads(str(turn.get("terminal_evidence_json") or "{}"))


async def _blocked_at(dispatcher, monkeypatch, name: str) -> asyncio.Event:
    """Make one of the emit's IM surfaces wait until the test cancels it."""

    entered = asyncio.Event()

    async def held(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(dispatcher, name, held)
    return entered


@pytest.mark.parametrize("owner", ["runner", "agent_initiated"])
@pytest.mark.parametrize("interruption", ["none", "cancel", "refresh", "shutdown"])
@pytest.mark.parametrize("backlog", [False, True])
def test_contended_terminal_keeps_cleanup_fenced_until_evidence_commits(
    managers, monkeypatch, owner, interruption, backlog,
):
    """#2706 B2: a signalled runner cannot outrun the asynchronous writer wait.

    Prior tests only delayed IM surfaces / Hub, not the evidence write itself.
    Check the real dispatcher and each cleanup owner, including repeated cancel.
    """
    manager, _other, engine, engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)

    async def exercise():
        context = _context()
        if owner == "runner":
            turn_id, context = await _activate(manager)
        else:
            assert manager.register_agent_initiated_turn(context)
            turn_id = context.platform_specific["turn_token"]
        if backlog:
            queued = await manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p3", content="next"), context=_context(),
            )
        with engine.begin() as conn:
            conn.execute(update(agent_sessions).where(agent_sessions.c.id == "ses_fsm").values(agent_status="running"))
            conn.execute(messages.insert().values(
                id="prior-result", session_id="ses_fsm", platform="avibe", author="agent", type="result",
                source="agent", content_text="prior result", content_json="{}", metadata_json="{}",
                created_at="2026-07-31T23:59:00Z", updated_at="2026-07-31T23:59:00Z",
            ))
        contended = asyncio.Event()
        cleanup_waiting = asyncio.Event()
        events = []
        launched = []
        hub_states = []
        original_terminalize = manager._terminalize_durable_turn
        original_join = manager._join_terminal_cleanup

        def hub(_context, _settled_by):
            hub_states.append(harness.turn(turn_id)["state"])

        async def join(candidate):
            if candidate == turn_id:
                cleanup_waiting.set()
            return await original_join(candidate)

        async def launch(candidate, **_kwargs):
            launched.append(candidate)
            return True

        monkeypatch.setattr(manager, "_settle_model_hub_turn", hub)
        monkeypatch.setattr(manager, "_join_terminal_cleanup", join)
        monkeypatch.setattr(manager, "_start_persisted_turn", launch)
        monkeypatch.setattr("core.inbox_events.bus.publish", lambda name, _payload: events.append(name))
        with engine_b.connect() as blocker:
            def terminalize(candidate, outcome, **kwargs):
                first = candidate == turn_id and kwargs.get("evidence_kind") == "terminal_result" and not contended.is_set()
                if first:
                    blocker.exec_driver_sql("BEGIN IMMEDIATE")
                result = original_terminalize(candidate, outcome, **kwargs)
                if first:
                    contended.set()
                return result

            monkeypatch.setattr(manager, "_terminalize_durable_turn", terminalize)
            if owner == "runner":
                task = await harness.start(turn_id, context)
            else:
                task = manager.in_flight["ses_fsm"].task
                harness.receivers.append(asyncio.create_task(harness.controller.emit_agent_message(context, "result", RESULT)))
            try:
                await asyncio.wait_for(contended.wait(), WAIT_S)
                await asyncio.wait_for(cleanup_waiting.wait(), WAIT_S)
                assert not task.done()
                assert harness.turn(turn_id)["state"] == "active"
                assert not hub_states and not launched and "turn.end" not in events
                transition = None
                if interruption == "cancel":
                    task.cancel()
                    # A second cancellation must not cancel the pending write.
                    asyncio.get_running_loop().call_soon(task.cancel)
                elif interruption in {"refresh", "shutdown"}:
                    transition_waiting = asyncio.Event()
                    ensure = manager._ensure_terminal_completion

                    def joined(pending):
                        result = ensure(pending)
                        if asyncio.current_task() is transition:
                            transition_waiting.set()
                        return result

                    monkeypatch.setattr(manager, "_ensure_terminal_completion", joined)
                    if interruption == "refresh":
                        manager.begin_backend_drain("codex")
                        transition = asyncio.create_task(manager.release_for_backend_refresh(
                            backend="codex", base_session_ids={"ses_fsm"},
                        ))
                    else:
                        transition = asyncio.create_task(manager.release_for_service_shutdown())
                    await asyncio.wait_for(transition_waiting.wait(), WAIT_S)
                blocker.rollback()
                await asyncio.wait_for(asyncio.gather(task, *harness.receivers, return_exceptions=True), WAIT_S)
                if transition is not None:
                    await asyncio.wait_for(transition, WAIT_S)
            finally:
                blocker.rollback()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, *harness.receivers, return_exceptions=True)
        turn = harness.turn(turn_id)
        assert turn["terminal_evidence_kind"] == "terminal_result"
        assert _evidence(turn)["result_text"] == RESULT
        assert "ses_fsm" not in manager.in_flight
        assert not manager._pending_terminals
        assert events.index("inbox.session.updated") < events.index("turn.end")
        assert events.count("turn.end") == 1
        if not backlog or interruption in {"refresh", "shutdown"}:
            assert events.index("session.status") < events.index("turn.end")
            assert launched == []
        if backlog:
            with engine.connect() as conn:
                selected = delivery_store.get_delivery(conn, str(queued.delivery_id))
            if interruption in {"refresh", "shutdown"}:
                assert selected["state"] == "queued"
                if interruption == "refresh":
                    await manager.end_backend_drain("codex")
                    with engine.connect() as conn:
                        selected = delivery_store.get_delivery(conn, str(queued.delivery_id))
                    assert selected["state"] == "claimed" and launched == [selected["turn_id"]]
            else:
                assert selected["state"] == "claimed" and launched == [selected["turn_id"]]
        assert hub_states == (["terminal"] if owner == "runner" else [])

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "branch",
    ["silent", "duplicate", "suppressed", "delivered"],
)
def test_a_terminal_result_reaches_the_runner_with_its_evidence(managers, monkeypatch, branch) -> None:
    """#2714: the IM processing indicator's cleanup awaits after the Turn's signal.
    On master the runner woke there and terminalized the Turn as ``runner_release``,
    before the evidence-carrying write, so a late-accepted steer lost the result it
    settles its Run from. The commit now runs signal, release and that write with no
    await between them."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    if branch == "silent":
        harness.emit = {"text": ""}
    elif branch == "duplicate":
        monkeypatch.setattr(
            "core.message_dispatcher.agent_message_exists",
            lambda *_args: {"id": "row-0", "text": RESULT, "metadata": {}},
        )
        harness.emit = {"text": RESULT, "output": MessageOutput(completes_turn=True, idempotency_key="native-1")}

    async def run() -> str:
        turn_id, context = await _activate(manager, text="prompt")
        if branch == "suppressed":
            context.platform_specific["suppress_delivery"] = True
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(runner, WAIT_S)
        await asyncio.wait_for(asyncio.gather(*harness.receivers), WAIT_S)
        return turn_id

    turn_id = asyncio.run(run())

    turn = harness.turn(turn_id)
    assert turn["state"] == "terminal"
    assert turn["terminal_evidence_kind"] == "terminal_result"
    assert _evidence(turn)["result_text"] == ("" if branch == "silent" else RESULT)
    assert harness.controller.processing_indicator.finished == 1
    harness.controller.agent_service.release_runtime_turn.assert_called_once()


@pytest.mark.parametrize(
    ("branch", "surface"),
    [
        ("silent", "_collapse_status_bubble"),
        ("silent", "_clear_consolidated_state"),
        ("delivered", "_upload_file_links"),
        ("delivered", "_retire_status_bubble"),
        ("delivered", "_drop_status_keys"),
        ("delivered", "_finish_processing_indicator_turn"),
    ],
)
def test_a_cancellation_in_an_im_surface_still_commits_the_turn(managers, monkeypatch, branch, surface) -> None:
    """A Stop that lands in an IM surface after the durable steps interrupts that
    surface, still runs the Turn commit, and then propagates. On master the collapse
    and clear sat before the signal, and the delivered branch's uploads, bubble retire
    and state drop sat before its durable writes, so such a Stop left the Turn to the
    runner without evidence."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    if branch == "silent":
        harness.emit = {"text": ""}
    else:
        harness.emit = {"text": f"{RESULT} [report](file:///etc/hosts)"}
        monkeypatch.setattr(harness.dispatcher, "_concise_status_bubble_id", lambda _context: "bubble-1")
        monkeypatch.setattr(harness.dispatcher, "_get_consolidated_message_key", lambda _context: "status-key")

    async def run() -> tuple[str, BaseException | None]:
        entered = await _blocked_at(harness.dispatcher, monkeypatch, surface)
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(entered.wait(), WAIT_S)
        harness.receivers[0].cancel()
        outcome = (await asyncio.wait_for(asyncio.gather(*harness.receivers, return_exceptions=True), WAIT_S))[0]
        await asyncio.wait_for(runner, WAIT_S)
        return turn_id, outcome

    turn_id, outcome = asyncio.run(run())

    assert isinstance(outcome, asyncio.CancelledError)
    turn = harness.turn(turn_id)
    assert turn["terminal_evidence_kind"] == "terminal_result"
    assert _evidence(turn)["result_text"].startswith("" if branch == "silent" else RESULT)
    harness.controller.agent_service.release_runtime_turn.assert_called_once()
    if branch == "delivered":
        assert harness.persisted, "the delivered result was not persisted before its IM surfaces"


def test_a_held_activity_settlement_failure_still_commits_the_turn(managers, monkeypatch) -> None:
    """The emit's post-signal step raises after the signal: its error is held until
    the commit has run, then reaches the caller as on master. On master it skipped
    the release and the Turn's write, and the runner won without evidence."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    harness.emit = {
        "text": "",
        "output": MessageOutput(completes_turn=True, requires_delivery_for_run_settlement=True),
    }
    monkeypatch.setattr(harness.dispatcher, "_settle_activity_output_claims", lambda *_args, **_kwargs: False)

    async def run() -> tuple[str, BaseException | None]:
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(runner, WAIT_S)
        outcome = (await asyncio.wait_for(asyncio.gather(*harness.receivers, return_exceptions=True), WAIT_S))[0]
        return turn_id, outcome

    turn_id, outcome = asyncio.run(run())

    assert isinstance(outcome, ActivityOutputDeliveryError)
    turn = harness.turn(turn_id)
    assert turn["terminal_evidence_kind"] == "terminal_result"
    harness.controller.agent_service.release_runtime_turn.assert_called_once()


def test_a_durable_step_failure_keeps_the_turn_owner_live(managers, monkeypatch) -> None:
    """A Workbench result whose row was not persisted fails before the signal, as on
    master: the emit raises, the runtime is released, and neither the signal nor the
    Turn's write runs, so the Turn stays active for exact reconciliation."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    monkeypatch.setattr("core.message_dispatcher.persist_agent_message", lambda *_args, **_kwargs: None)

    async def run() -> tuple[str, BaseException | None, dict]:
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        outcome = (await asyncio.wait_for(asyncio.gather(await harness.receiver(), return_exceptions=True), WAIT_S))[0]
        turn = harness.turn(turn_id)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        return turn_id, outcome, turn

    _turn_id, outcome, turn = asyncio.run(run())

    assert isinstance(outcome, RuntimeError)
    assert turn["state"] == "active"
    harness.controller.agent_service.release_runtime_turn.assert_called_once()


def _fail_first_turn_write(manager, monkeypatch, *, failures: int = 1) -> list[str]:
    """Make the first evidence-carrying Turn writes raise, as a busy database would."""

    original = manager._terminalize_durable_turn
    calls: list[str] = []

    def terminalize(turn_id, outcome, **kwargs):
        calls.append(str(kwargs.get("evidence_kind")))
        if len(calls) <= failures and calls[-1] == "terminal_result":
            raise RuntimeError("database is locked")
        return original(turn_id, outcome, **kwargs)

    monkeypatch.setattr(manager, "_terminalize_durable_turn", terminalize)
    return calls


def test_the_runner_commits_the_evidence_when_the_turn_write_fails(managers, monkeypatch) -> None:
    """``_finish_durable_terminal_result`` logs and returns when its write raises. The
    runner's release then commits the evidence the commit handed to that write,
    instead of a ``runner_release`` without it."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    calls = _fail_first_turn_write(manager, monkeypatch)

    async def run() -> str:
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(runner, WAIT_S)
        await asyncio.wait_for(asyncio.gather(*harness.receivers), WAIT_S)
        return turn_id

    turn_id = asyncio.run(run())

    assert calls == ["terminal_result", "terminal_result"]
    turn = harness.turn(turn_id)
    assert turn["terminal_evidence_kind"] == "terminal_result"
    assert _evidence(turn)["result_text"] == RESULT


def test_the_runner_write_is_a_no_op_when_the_turn_write_lands(managers, monkeypatch) -> None:
    """With the evidence write landed, the runner's fallback finds the Turn terminal
    and changes nothing."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    original = manager._reconcile_durable_runner_release
    releases: list[dict] = []

    def reconcile(*args, **kwargs):
        result = original(*args, **kwargs)
        releases.append({"terminal_commit": kwargs.get("terminal_commit"), "result": result})
        return result

    monkeypatch.setattr(manager, "_reconcile_durable_runner_release", reconcile)

    async def run() -> str:
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(runner, WAIT_S)
        await asyncio.wait_for(asyncio.gather(*harness.receivers), WAIT_S)
        return turn_id

    turn_id = asyncio.run(run())

    (release,) = releases
    assert release["terminal_commit"]["evidence"]["result_text"] == RESULT
    assert release["result"]["changed"] is False
    assert harness.turn(turn_id)["terminal_evidence_kind"] == "terminal_result"


def test_a_release_without_a_commit_hands_the_runner_no_evidence(managers, monkeypatch) -> None:
    """A Turn released without a terminal emit (a backend's no-result release) gives the
    runner nothing to commit: it terminalizes as ``runner_release``, as before."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)

    async def release_without_result(context, _text, **_kwargs):
        set_dispatch_phase(context, DISPATCH_PHASE_ATTEMPTING)
        harness.controller.mark_turn_complete(context)
        return None

    harness.controller.message_handler = SimpleNamespace(handle_user_message=release_without_result)

    async def run() -> str:
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(runner, WAIT_S)
        return turn_id

    turn_id = asyncio.run(run())

    turn = harness.turn(turn_id)
    assert turn["terminal_evidence_kind"] == "runner_release"
    assert _evidence(turn) == {}


def test_a_stop_while_the_turn_write_fails_still_commits_the_evidence(managers, monkeypatch) -> None:
    """A cancellation reaches both the emit, in an IM surface, and the runner. The
    commit runs and its write fails; the runner is then cancelled before it resumes,
    so ``dispatch_turn`` returns no outcome. Its ``finally`` still hands the commit's
    evidence over, and the runner's cancelled path commits it. Whether the queue moves
    on stays the cancellation's call: this one names no Stop, so the backlog waits."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    harness.emit = {"text": ""}
    harness.controller.processing_indicator.finish_terminal_turn = lambda _context: asyncio.sleep(0, None)
    monkeypatch.setattr(harness.dispatcher, "_finish_processing_indicator_turn", _no_wait)
    calls = _fail_first_turn_write(manager, monkeypatch)

    async def run() -> tuple[str, str]:
        entered = await _blocked_at(harness.dispatcher, monkeypatch, "_collapse_status_bubble")
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(entered.wait(), WAIT_S)
        backlog = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="backlog"),
            context=_context(),
        )
        harness.receivers[0].cancel()
        runner.cancel()
        await asyncio.wait_for(asyncio.gather(runner, *harness.receivers, return_exceptions=True), WAIT_S)
        return turn_id, str(backlog.delivery_id)

    turn_id, backlog_id = asyncio.run(run())

    assert calls == ["terminal_result", "terminal_result"]
    turn = harness.turn(turn_id)
    assert turn["terminal_evidence_kind"] == "terminal_result"
    assert "result_text" in _evidence(turn)
    with engine.connect() as conn:
        assert delivery_store.get_delivery(conn, backlog_id)["state"] == "queued"


async def _no_wait(_context) -> None:
    return None


def _record_turn_boundary_events(engine, session_id: str) -> tuple[list[str], int]:
    """Subscribe to the Session's terminal events, with the Session projected running."""

    from core.inbox_events import bus

    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(agent_status="running"))

    events: list[str] = []

    def record(event_type, data):
        if isinstance(data, dict) and data.get("session_id") == session_id and event_type in {
            "session.status",
            "turn.end",
        }:
            events.append(event_type)

    return events, bus.subscribe_callback(record)


def test_the_runner_fallback_commits_before_hub_finalization_and_turn_end(managers, monkeypatch) -> None:
    """#2735: when the emit's own Turn write fails, the runner's write stands in for
    it, so it lands where that one would have. The Turn is terminal before Model Hub
    finalization, which the Hub's successor fence relies on, and its session.status
    goes out before turn.end."""

    from core.inbox_events import bus

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    _fail_first_turn_write(manager, monkeypatch)
    states_at_hub_finalization: list[str] = []

    def settle_model_hub_turn(context, _settled_by):
        states_at_hub_finalization.append(harness.turn(context.platform_specific["turn_token"])["state"])

    monkeypatch.setattr(manager, "_settle_model_hub_turn", settle_model_hub_turn)
    events, subscription = _record_turn_boundary_events(engine, "ses_fsm")

    async def run() -> str:
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(runner, WAIT_S)
        await asyncio.wait_for(asyncio.gather(*harness.receivers), WAIT_S)
        return turn_id

    try:
        turn_id = asyncio.run(run())
    finally:
        bus.unsubscribe(subscription)

    assert harness.turn(turn_id)["terminal_evidence_kind"] == "terminal_result"
    assert states_at_hub_finalization == ["terminal"]
    assert events == ["session.status", "turn.end"]


def test_the_agent_initiated_holder_publishes_turn_end_after_its_terminal_write(managers) -> None:
    """#2735: the agent-initiated holder's own terminal write publishes the Turn's
    session.status; turn.end follows it, as it does after the emit's write."""

    from core.inbox_events import bus
    from core.run_settlement import SETTLED_BY_TERMINAL_RESULT

    manager, _other, engine, _engine_b, _starts = managers
    events, subscription = _record_turn_boundary_events(engine, "ses_fsm")

    async def run() -> str:
        context = _context()
        assert manager.register_agent_initiated_turn(context) is True
        await asyncio.sleep(0)
        turn_id = str(context.platform_specific["turn_token"])
        holder = manager.in_flight["ses_fsm"].task
        sink = manager.get_turn_sink(resolve_turn_sink_key(manager.controller, context))
        sink["settled_by"] = SETTLED_BY_TERMINAL_RESULT
        sink["done_event"].set()
        await asyncio.wait_for(asyncio.gather(holder, return_exceptions=True), WAIT_S)
        return turn_id

    try:
        turn_id = asyncio.run(run())
    finally:
        bus.unsubscribe(subscription)

    assert harness_state(engine, turn_id) == "terminal"
    assert events[-2:] == ["session.status", "turn.end"]


def harness_state(engine, turn_id: str) -> str:
    with engine.connect() as conn:
        return str(delivery_store.get_turn(conn, turn_id)["state"])


def test_a_fallback_write_that_fails_again_is_retried_after_hub_finalization(managers, monkeypatch) -> None:
    """When the runner's early fallback write meets the same busy database, it still
    gets the attempt after Model Hub finalization that it had on master, and the
    Turn ends with its evidence instead of staying active with its queue held."""

    manager, _other, engine, _engine_b, _starts = managers
    harness = _Harness(manager, engine, monkeypatch)
    calls = _fail_first_turn_write(manager, monkeypatch, failures=2)

    async def run() -> str:
        turn_id, context = await _activate(manager, text="prompt")
        runner = await harness.start(turn_id, context)
        await asyncio.wait_for(runner, WAIT_S)
        await asyncio.wait_for(asyncio.gather(*harness.receivers), WAIT_S)
        return turn_id

    turn_id = asyncio.run(run())

    assert calls == ["terminal_result", "terminal_result", "terminal_result"]
    turn = harness.turn(turn_id)
    assert turn["state"] == "terminal"
    assert turn["terminal_evidence_kind"] == "terminal_result"
