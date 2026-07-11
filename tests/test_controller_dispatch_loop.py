from __future__ import annotations

import asyncio
import threading
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_cleanup_sync_stops_watch_service_on_stopped_loop() -> None:
    controller = Controller.__new__(Controller)
    loop = asyncio.new_event_loop()
    controller._loop = loop
    stopped: dict[str, bool] = {"watch": False, "tasks": False, "runtime": False}

    class _Stopper:
        def __init__(self, key: str) -> None:
            self.key = key

        async def stop(self) -> None:
            stopped[self.key] = True

    controller.scheduled_task_service = _Stopper("tasks")
    controller.watch_service = _Stopper("watch")
    controller.runtime_command_watcher = _Stopper("runtime")
    controller.update_checker = type("UpdateChecker", (), {"stop": lambda self: None})()
    controller.receiver_tasks = {}
    controller.im_client = None
    controller._im_thread = None

    try:
        controller.cleanup_sync()
    finally:
        loop.close()

    assert stopped["tasks"] is True
    assert stopped["watch"] is True
    assert stopped["runtime"] is True


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
