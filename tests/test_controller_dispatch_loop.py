from __future__ import annotations

import asyncio
import threading
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.controller import Controller


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
