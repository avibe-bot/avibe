from __future__ import annotations

import asyncio
import json
import threading
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.controller import Controller
from core.git_binary import ResolvedGit
from core.inbox_events import InboxEventBus
from core.message_output import MessageOutput
from core.session_turns import SessionTurnManager
from core.show_git import ShowGitCheckpointService
from modules.im import MessageContext


def test_dispatch_to_controller_loop_runs_callback_on_controller_loop():
    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    result: dict[str, object] = {}

    async def callback(value: str) -> str:
        result["thread"] = threading.current_thread().name
        result["loop"] = asyncio.get_running_loop()
        result["value"] = value
        return value.upper()

    wrapped = Controller._dispatch_to_controller_loop(controller, callback)

    def _loop_runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=_loop_runner, name="controller-loop", daemon=True)
    loop_thread.start()

    async def _invoke() -> str:
        return await wrapped("hello")

    try:
        output = asyncio.run(_invoke())
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    assert output == "HELLO"
    assert result["thread"] == "controller-loop"
    assert result["value"] == "hello"


def test_dispatch_im_message_to_controller_loop_backgrounds_untracked_platforms():
    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    callback_started = threading.Event()
    callback_can_finish = threading.Event()
    result: dict[str, object] = {}

    async def callback(context, value: str) -> None:
        result["thread"] = threading.current_thread().name
        result["loop"] = asyncio.get_running_loop()
        result["platform"] = context.platform
        result["value"] = value
        callback_started.set()
        await asyncio.to_thread(callback_can_finish.wait)
        result["finished"] = True

    wrapped = Controller._dispatch_im_message_to_controller_loop(controller, callback)
    context = SimpleNamespace(platform="slack", platform_specific={"platform": "slack"})

    def _loop_runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=_loop_runner, name="controller-loop", daemon=True)
    loop_thread.start()

    async def _invoke() -> None:
        await asyncio.wait_for(wrapped(context, "hello"), timeout=0.2)

    try:
        asyncio.run(_invoke())
        assert callback_started.wait(timeout=1)
        assert "finished" not in result
        callback_can_finish.set()
        deadline = loop.time() + 2
        while "finished" not in result and loop.time() < deadline:
            threading.Event().wait(0.01)
    finally:
        callback_can_finish.set()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    assert result["finished"] is True
    assert result["thread"] == "controller-loop"
    assert result["platform"] == "slack"
    assert result["value"] == "hello"


def test_dispatch_im_message_to_controller_loop_waits_for_tracked_platforms():
    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    callback_started = threading.Event()
    callback_can_finish = threading.Event()
    result: dict[str, object] = {}

    async def callback(context, value: str) -> str:
        result["thread"] = threading.current_thread().name
        result["loop"] = asyncio.get_running_loop()
        result["platform"] = context.platform
        result["value"] = value
        callback_started.set()
        await asyncio.to_thread(callback_can_finish.wait)
        result["finished"] = True
        return "done"

    wrapped = Controller._dispatch_im_message_to_controller_loop(controller, callback)
    context = SimpleNamespace(platform="telegram", platform_specific={"platform": "telegram"})

    def _loop_runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=_loop_runner, name="controller-loop", daemon=True)
    loop_thread.start()

    async def _invoke() -> str:
        task = asyncio.create_task(wrapped(context, "hello"))
        await asyncio.to_thread(callback_started.wait)
        await asyncio.sleep(0)
        assert not task.done()
        callback_can_finish.set()
        return await asyncio.wait_for(task, timeout=1)

    try:
        output = asyncio.run(_invoke())
    finally:
        callback_can_finish.set()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    assert output == "done"
    assert result["finished"] is True
    assert result["thread"] == "controller-loop"
    assert result["platform"] == "telegram"
    assert result["value"] == "hello"


def test_dispatch_im_message_to_controller_loop_waits_for_standalone_wechat_without_context_platform():
    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    controller.im_client = type("WeChatBot", (), {"__module__": "modules.im.wechat"})()
    callback_started = threading.Event()
    callback_can_finish = threading.Event()
    result: dict[str, object] = {}

    async def callback(context, value: str) -> str:
        result["thread"] = threading.current_thread().name
        result["loop"] = asyncio.get_running_loop()
        result["value"] = value
        callback_started.set()
        await asyncio.to_thread(callback_can_finish.wait)
        result["finished"] = True
        return "done"

    wrapped = Controller._dispatch_im_message_to_controller_loop(controller, callback)
    context = SimpleNamespace(platform="", platform_specific={})

    def _loop_runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=_loop_runner, name="controller-loop", daemon=True)
    loop_thread.start()

    async def _invoke() -> str:
        task = asyncio.create_task(wrapped(context, "hello"))
        await asyncio.to_thread(callback_started.wait)
        await asyncio.sleep(0)
        assert not task.done()
        callback_can_finish.set()
        return await asyncio.wait_for(task, timeout=1)

    try:
        output = asyncio.run(_invoke())
    finally:
        callback_can_finish.set()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    assert output == "done"
    assert result["finished"] is True
    assert result["thread"] == "controller-loop"
    assert result["value"] == "hello"


def test_dispatch_to_controller_loop_can_gate_runtime_admission_on_recovery():
    controller = Controller.__new__(Controller)
    controller._loop = None
    called: list[str] = []

    async def scenario() -> None:
        controller._delivery_recovery_complete = asyncio.Event()

        async def callback(value: str) -> str:
            called.append(value)
            return value.upper()

        wrapped = Controller._dispatch_to_controller_loop(
            controller,
            callback,
            wait_for_owner_recovery=True,
        )
        task = asyncio.create_task(wrapped("hello"))
        await asyncio.sleep(0)
        assert not task.done()
        assert called == []
        controller._delivery_recovery_complete.set()
        assert await task == "HELLO"

    asyncio.run(scenario())
    assert called == ["hello"]


def test_dispatch_im_message_to_controller_loop_gates_admission_on_recovery():
    controller = Controller.__new__(Controller)
    controller._loop = None
    called: list[str] = []

    async def scenario() -> None:
        controller._delivery_recovery_complete = asyncio.Event()

        async def callback(_context, value: str) -> str:
            called.append(value)
            return value.upper()

        wrapped = Controller._dispatch_im_message_to_controller_loop(
            controller,
            callback,
            wait_for_owner_recovery=True,
        )
        context = SimpleNamespace(
            platform="telegram",
            platform_specific={"platform": "telegram"},
        )
        task = asyncio.create_task(wrapped(context, "hello"))
        await asyncio.sleep(0)
        assert not task.done()
        assert called == []
        controller._delivery_recovery_complete.set()
        assert await task == "HELLO"

    asyncio.run(scenario())
    assert called == ["hello"]


def test_setup_callbacks_gates_work_admission_but_not_runtime_evidence():
    controller = Controller.__new__(Controller)
    callback = AsyncMock()
    controller.command_handler = SimpleNamespace(
        handle_start=callback,
        handle_new=callback,
        handle_cwd=callback,
        handle_set_cwd=callback,
        handle_resume=callback,
        handle_setup=callback,
        handle_stop=callback,
        handle_bind=callback,
        handle_change_cwd_submission=callback,
    )
    controller.settings_handler = SimpleNamespace(
        handle_settings=callback,
        handle_settings_update=callback,
        handle_routing_update=callback,
        handle_routing_modal_update=callback,
    )
    controller.message_handler = SimpleNamespace(handle_callback_query=callback)
    controller.session_handler = SimpleNamespace(
        handle_resume_session_submission=callback
    )
    controller._on_runtime_ready = callback
    controller._on_im_ready = callback
    registered: dict[str, object] = {}
    controller.im_client = SimpleNamespace(
        register_callbacks=lambda **kwargs: registered.update(kwargs)
    )

    controller._dispatch_to_controller_loop = Mock(
        side_effect=lambda target, *, wait_for_owner_recovery=False: (
            "controller",
            target,
            wait_for_owner_recovery,
        )
    )
    controller._dispatch_im_message_to_controller_loop = Mock(
        side_effect=lambda target, *, wait_for_owner_recovery=False: (
            "message",
            target,
            wait_for_owner_recovery,
        )
    )

    Controller._setup_callbacks(controller)

    assert all(value[2] is True for value in registered["on_command"].values())
    assert registered["on_message"][0::2] == ("message", True)
    for name in (
        "on_callback_query",
        "on_settings_update",
        "on_change_cwd",
        "on_routing_update",
        "on_routing_modal_update",
        "on_resume_session",
    ):
        assert registered[name][0::2] == ("controller", True)
    assert registered["on_ready"][0::2] == ("controller", False)
    assert registered["on_transport_ready"][0::2] == ("controller", False)


def test_cleanup_sync_stops_watch_service_on_stopped_loop() -> None:
    """Scenario: MEMORY-INDEP-008."""

    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    stopped: dict[str, bool] = {
        "watch": False,
        "tasks": False,
        "supervisor": False,
        "runtime": False,
        "capture": False,
        "capture-registration": False,
    }
    stop_order: list[str] = []

    class _Stopper:
        def __init__(self, key: str) -> None:
            self.key = key

        async def stop(self) -> None:
            stopped[self.key] = True
            stop_order.append(self.key)

    class _Supervisor(_Stopper):
        def quiesce(self) -> None:
            stop_order.append("quiesce")

        async def run_sync(self, operation):  # noqa: ANN001, ANN202
            assert not stopped["supervisor"]
            return operation()

    class _WatchStopper(_Stopper):
        async def stop(self) -> None:
            await controller.runtime_work_supervisor.run_sync(lambda: None)
            await super().stop()

    class _MemoryRuntime:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            assert stopped["capture"] is True
            assert archive_flush_task.cancelled()
            self.closed = True
            stopped["runtime"] = True
            stop_order.append("memory-runtime")

    class _MessageHandler:
        def quiesce_memory_capture_tasks(self) -> None:
            stopped["capture-registration"] = True
            stop_order.append("capture-registration")

        async def cancel_memory_capture_tasks(self) -> None:
            assert stopped["capture-registration"] is True
            stopped["capture"] = True
            stop_order.append("capture")

    controller.scheduled_task_service = _Stopper("tasks")
    controller.runtime_work_supervisor = _Supervisor("supervisor")
    controller.watch_service = _WatchStopper("watch")
    controller.runtime_command_watcher = _Stopper("runtime")
    controller.message_handler = _MessageHandler()
    old_memory_runtime = _MemoryRuntime()
    fresh_memory_runtime = _MemoryRuntime()
    controller.memory_runtime = old_memory_runtime

    async def retained_factory_reset() -> None:
        await asyncio.sleep(0)
        stop_order.append("factory-reset")
        controller.memory_runtime = fresh_memory_runtime

    controller._memory_factory_reset_task = loop.create_task(retained_factory_reset())

    async def pending_archive_flush() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stop_order.append("archive-flush")

    archive_flush_task = loop.create_task(pending_archive_flush())
    controller._archive_memory_flush_tasks = {archive_flush_task}
    loop.run_until_complete(asyncio.sleep(0))
    controller.update_checker = type("UpdateChecker", (), {"stop": lambda self: None})()
    controller.receiver_tasks = {}
    controller.im_client = None
    controller._im_thread = None

    try:
        controller.cleanup_sync()
    finally:
        if not archive_flush_task.done():
            archive_flush_task.cancel()
            loop.run_until_complete(
                asyncio.gather(archive_flush_task, return_exceptions=True)
            )
        loop.close()

    assert archive_flush_task.cancelled()
    assert stopped["tasks"] is True
    assert stopped["watch"] is True
    assert stopped["supervisor"] is True
    assert stopped["runtime"] is True
    assert stopped["capture"] is True
    assert stopped["capture-registration"] is True
    assert old_memory_runtime.closed is False
    assert fresh_memory_runtime.closed is True
    runtime_work_order = [
        event for event in stop_order if event != "factory-reset"
    ]
    assert runtime_work_order[0] == "quiesce"
    assert set(runtime_work_order[1:3]) == {"tasks", "watch"}
    assert runtime_work_order[3] == "supervisor"
    assert stop_order.index("factory-reset") < stop_order.index("capture")
    assert stop_order[-4:] == [
        "capture-registration",
        "capture",
        "archive-flush",
        "memory-runtime",
    ]


@pytest.mark.asyncio
async def test_controller_joins_retained_factory_reset_task() -> None:
    controller = Controller.__new__(Controller)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def retained_reset() -> None:
        entered.set()
        await release.wait()

    task = asyncio.create_task(retained_reset())
    controller._memory_factory_reset_task = task
    await entered.wait()

    joining = asyncio.create_task(controller._join_memory_factory_reset_task())
    await asyncio.sleep(0)
    assert joining.done() is False

    joining.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await joining

    assert task.done() is True
    assert controller._memory_factory_reset_task is None


@pytest.mark.anyio
async def test_runtime_work_stack_stops_supervisor_after_service_failure() -> None:
    controller = Controller.__new__(Controller)
    controller._shutdown_tainted = False
    stopped: list[str] = []

    class _Service:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def stop(self) -> None:
            stopped.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} failed")

    class _Supervisor:
        def quiesce(self) -> None:
            stopped.append("quiesce")

        async def stop(self) -> None:
            stopped.append("supervisor")

    controller.scheduled_task_service = _Service("tasks", fail=True)
    controller.watch_service = _Service("watch")
    controller.runtime_work_supervisor = _Supervisor()

    with pytest.raises(RuntimeError, match="runtime work stack shutdown failed"):
        await controller._stop_runtime_work_stack()

    assert stopped[0] == "quiesce"
    assert set(stopped[1:3]) == {"tasks", "watch"}
    assert stopped[3] == "supervisor"
    assert controller._shutdown_tainted is True


@pytest.mark.anyio
async def test_hfr_284_runtime_work_stack_joins_controller_lanes_before_service_teardown() -> None:
    controller = Controller.__new__(Controller)
    controller._shutdown_tainted = False
    controller._runtime_work_tokens = [object()]
    join_entered = asyncio.Event()
    release_join = asyncio.Event()
    stopped: list[str] = []

    class _Service:
        def __init__(self, name: str) -> None:
            self.name = name

        async def stop(self) -> None:
            stopped.append(self.name)

    class _Supervisor:
        def quiesce(self) -> None:
            stopped.append("quiesce")

        def begin_unregister(self, _token):  # noqa: ANN001, ANN202
            async def _join() -> None:
                join_entered.set()
                await release_join.wait()
                stopped.append("controller-lanes")

            return asyncio.create_task(_join())

        async def stop(self) -> None:
            stopped.append("supervisor")

    controller.scheduled_task_service = _Service("tasks")
    controller.watch_service = _Service("watch")
    controller.runtime_work_supervisor = _Supervisor()

    shutdown = asyncio.create_task(controller._stop_runtime_work_stack())
    await asyncio.wait_for(join_entered.wait(), 1)
    assert stopped == ["quiesce"]

    release_join.set()
    await shutdown

    assert stopped[1] == "controller-lanes"
    assert set(stopped[2:4]) == {"tasks", "watch"}
    assert stopped[4] == "supervisor"
    assert controller._runtime_work_tokens == []


@pytest.mark.anyio
async def test_runtime_work_stack_drains_run_activity_before_executor_stop() -> None:
    controller = Controller.__new__(Controller)
    controller._shutdown_tainted = False
    controller._runtime_work_tokens = []
    stopped: list[str] = []

    class _Dispatcher:
        async def drain_agent_run_activity(self) -> None:
            stopped.append("activity")

    class _Service:
        def __init__(self, name: str) -> None:
            self.name = name

        async def stop(self) -> None:
            stopped.append(self.name)

    class _Supervisor:
        def quiesce(self) -> None:
            stopped.append("quiesce")

        async def stop(self) -> None:
            stopped.append("supervisor")

    controller.message_dispatcher = _Dispatcher()
    controller.model_hub_service = _Service("model-hub")
    controller.scheduled_task_service = _Service("tasks")
    controller.watch_service = _Service("watch")
    controller.runtime_work_supervisor = _Supervisor()

    await controller._stop_runtime_work_stack()

    assert stopped[0:2] == ["quiesce", "activity"]
    assert set(stopped[2:5]) == {"model-hub", "tasks", "watch"}
    assert stopped[5] == "supervisor"


def test_request_shutdown_keeps_loop_owned_supervisor_join_alive_after_grace() -> None:
    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    controller._shutdown_requested = False
    controller._shutdown_task = None
    controller._shutdown_tainted = False
    controller._runtime_work_shutdown_grace_seconds = 0.0
    started = threading.Event()
    release = threading.Event()

    class _Supervisor:
        async def stop(self) -> None:
            started.set()
            await asyncio.to_thread(release.wait)

    controller.runtime_work_supervisor = _Supervisor()

    thread = threading.Thread(target=loop.run_forever, name="controller-loop")
    thread.start()
    try:
        controller.request_shutdown("test")
        assert started.wait(timeout=1)
        assert thread.is_alive()
        assert controller.service_lock_safe_to_release is False
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert controller._shutdown_tainted is True
        assert controller.service_lock_safe_to_release is False
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_im_show_checkpoint_lifecycle_spans_real_start_to_terminal_result(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    controller = Controller.__new__(Controller)
    updated_threads = []
    emitted_messages = []
    linked_messages = []

    class _Dispatcher:
        def update_thread_message_id(self, context) -> None:
            updated_threads.append(context)

        async def emit_agent_message(self, **kwargs):
            emitted_messages.append(kwargs)
            return "result-1"

    controller.message_dispatcher = _Dispatcher()
    controller.sessions = SimpleNamespace(
        find_session_for_anchor=lambda session_key, anchor: {"id": "ses_im"}
        if (session_key, anchor) == ("slack:C", "anchor")
        else None
    )
    controller.agent_service = SimpleNamespace(emit_matches_runtime_turn=lambda _context: True)
    controller._get_session_key = lambda context: f"{context.platform}:{context.channel_id}"
    checkpoint_service = ShowGitCheckpointService(ResolvedGit(path=Path("/usr/bin/git"), source="system"))
    checkpoint_bus = InboxEventBus()
    checkpoint_service.start(checkpoint_bus)
    controller.show_git_checkpoint_service = checkpoint_service
    controller.session_turns = SessionTurnManager(controller)
    monkeypatch.setattr(
        "core.message_mirror.link_inbound_message_session",
        lambda **kwargs: linked_messages.append(kwargs),
    )
    context = MessageContext(
        user_id="U",
        channel_id="C",
        platform="slack",
        message_id="msg-1",
        platform_specific={},
    )
    context.platform_specific["turn_base_session_id"] = "anchor"
    lifecycle = []
    subscription_id = checkpoint_bus.subscribe_callback(
        lambda event_type, data: lifecycle.append((event_type, data))
        if event_type in {"turn.start", "turn.end"}
        else None
    )
    try:
        controller.session_turns.on_running(context)
        controller.update_thread_message_id(context)
        detached_result = asyncio.run(
            controller.emit_agent_message(
                context,
                "result",
                "activity finished",
                output=MessageOutput(completes_turn=False, detached=True),
            )
        )
        assert lifecycle == [("turn.start", {"session_id": "ses_im"})]
        first_result = asyncio.run(controller.emit_agent_message(context, "result", "done"))
        controller.session_turns.on_terminal_result(context, is_error=False)
        controller.session_turns.on_terminal_delivery_complete(context)
        second_result = asyncio.run(controller.emit_agent_message(context, "result", "duplicate"))
        controller.session_turns.on_terminal_result(context, is_error=False)
        controller.session_turns.on_terminal_delivery_complete(context)
    finally:
        checkpoint_bus.unsubscribe(subscription_id)
        checkpoint_service.stop()

    assert detached_result == "result-1"
    assert first_result == "result-1"
    assert second_result == "result-1"
    assert updated_threads == [context]
    assert len(emitted_messages) == 3
    assert emitted_messages[0]["output"] == MessageOutput(completes_turn=False, detached=True)
    assert context.platform_specific["agent_session_id"] == "ses_im"
    assert linked_messages == [
        {
            "platform": "slack",
            "native_message_id": "msg-1",
            "session_id": "ses_im",
        }
    ]
    assert lifecycle == [
        ("turn.start", {"session_id": "ses_im"}),
        ("turn.end", {"session_id": "ses_im"}),
    ]


def test_first_im_show_turn_adopts_on_terminal_after_backend_binds_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    controller = Controller.__new__(Controller)
    linked_messages = []

    class _Dispatcher:
        def update_thread_message_id(self, _context) -> None:
            return None

        async def emit_agent_message(self, **_kwargs):
            return "result-1"

    controller.message_dispatcher = _Dispatcher()
    controller.sessions = SimpleNamespace(find_session_for_anchor=lambda _session_key, _anchor: None)
    controller.agent_service = SimpleNamespace(emit_matches_runtime_turn=lambda _context: True)
    controller._get_session_key = lambda context: f"{context.platform}:{context.channel_id}"
    checkpoint_service = ShowGitCheckpointService(ResolvedGit(path=Path("/usr/bin/git"), source="system"))
    checkpoint_bus = InboxEventBus()
    checkpoint_service.start(checkpoint_bus)
    controller.show_git_checkpoint_service = checkpoint_service
    controller.session_turns = SessionTurnManager(controller)
    monkeypatch.setattr(
        "core.message_mirror.link_inbound_message_session",
        lambda **kwargs: linked_messages.append(kwargs),
    )
    context = MessageContext(
        user_id="U",
        channel_id="C",
        platform="slack",
        message_id="msg-new",
        platform_specific={},
    )
    context.platform_specific["turn_base_session_id"] = "new-anchor"
    lifecycle = []
    subscription_id = checkpoint_bus.subscribe_callback(
        lambda event_type, data: lifecycle.append((event_type, data))
        if event_type in {"turn.start", "turn.end"}
        else None
    )
    try:
        controller.session_turns.on_running(context)
        controller.update_thread_message_id(context)
        assert lifecycle == []
        context.platform_specific["agent_session_id"] = "ses_new_im"
        asyncio.run(controller.emit_agent_message(context, "result", "done"))
        controller.session_turns.on_terminal_result(context, is_error=False)
        controller.session_turns.on_terminal_delivery_complete(context)
    finally:
        checkpoint_bus.unsubscribe(subscription_id)
        checkpoint_service.stop()

    assert lifecycle == [("turn.end", {"session_id": "ses_new_im"})]
    assert linked_messages == [
        {
            "platform": "slack",
            "native_message_id": "msg-new",
            "session_id": "ses_new_im",
        }
    ]


def test_terminal_checkpoint_runs_after_dispatcher_delivery() -> None:
    controller = Controller.__new__(Controller)
    order = []

    class _CheckpointService:
        @staticmethod
        def begin_turn(_controller, _context) -> None:
            order.append("checkpoint-start")

        @staticmethod
        def end_turn(_context) -> None:
            order.append("checkpoint-end")

    class _Dispatcher:
        async def emit_agent_message(self, **kwargs):
            controller.session_turns.on_terminal_result(kwargs["context"], is_error=False)
            order.append("delivered")
            return "result-1"

    controller.show_git_checkpoint_service = _CheckpointService()
    controller.message_dispatcher = _Dispatcher()
    controller.set_agent_status = lambda _session_id, _status: None
    controller.session_turns = SessionTurnManager(controller)
    context = MessageContext(
        user_id="U",
        channel_id="C",
        platform="slack",
        platform_specific={"agent_session_id": "ses_delivery_order"},
    )

    controller.session_turns.on_running(context)
    result = asyncio.run(controller.emit_agent_message(context, "result", "done"))

    assert result == "result-1"
    assert order == ["checkpoint-start", "delivered", "checkpoint-end"]


def test_terminal_delivery_failure_keeps_turn_owner_live() -> None:
    controller = Controller.__new__(Controller)
    completed: list[MessageContext] = []

    class _Dispatcher:
        @staticmethod
        async def emit_agent_message(**_kwargs):
            raise RuntimeError("Workbench run output was not durably persisted")

    controller.message_dispatcher = _Dispatcher()
    controller.session_turns = SimpleNamespace(
        on_terminal_delivery_complete=lambda context: completed.append(context)
    )
    context = MessageContext(
        user_id="U",
        channel_id="C",
        platform="avibe",
        platform_specific={"turn_token": "turn-live"},
    )

    with pytest.raises(RuntimeError, match="not durably persisted"):
        asyncio.run(controller.emit_agent_message(context, "result", "done"))

    assert completed == []
def test_cleanup_sync_settles_the_internal_server_task(tmp_path, monkeypatch) -> None:
    """Shutdown must cancel the task, not just abandon it.

    Leaving it pending meant the done callback that records "stopped" never
    ran, so ``internal-server.json`` kept "ready" and ``vibe status`` reported a
    ready internal server against a service that no longer existed.
    """

    from config import paths
    from core import internal_server

    status_path = tmp_path / "runtime" / "internal-server.json"
    monkeypatch.setattr(paths, "get_internal_server_status_path", lambda: status_path)

    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop

    class _Stopper:
        async def stop(self) -> None:
            return None

    controller.scheduled_task_service = _Stopper()
    controller.watch_service = _Stopper()
    controller.runtime_command_watcher = _Stopper()
    controller.update_checker = type("UpdateChecker", (), {"stop": lambda self: None})()
    controller.receiver_tasks = {}
    controller.im_client = None
    controller._im_thread = None

    async def never_returns() -> None:
        await asyncio.Event().wait()

    task = loop.create_task(never_returns())
    controller._internal_server_task = task

    try:
        controller.cleanup_sync()
    finally:
        loop.close()

    assert task.cancelled()
    assert controller._internal_server_task is None
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "stopped"


def test_cleanup_sync_cancels_memory_reconcile_before_closing_runtime() -> None:
    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    controller.cleanup_task = None

    class _Stopper:
        async def stop(self) -> None:
            return None

    controller.scheduled_task_service = _Stopper()
    controller.watch_service = _Stopper()
    controller.runtime_command_watcher = _Stopper()
    controller.update_checker = type("UpdateChecker", (), {"stop": lambda self: None})()
    controller.receiver_tasks = {}
    controller.im_client = None
    controller._im_thread = None

    async def never_returns() -> None:
        await asyncio.Event().wait()

    reconcile_task = loop.create_task(never_returns())
    controller._memory_reconcile_task = reconcile_task

    class _MemoryRuntime:
        async def close(self) -> None:
            assert reconcile_task.cancelled()

    controller.memory_runtime = _MemoryRuntime()

    try:
        controller.cleanup_sync()
    finally:
        loop.close()

    assert reconcile_task.cancelled()
    assert controller._memory_reconcile_task is None
