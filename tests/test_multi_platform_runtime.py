from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im.base import BaseIMClient, BaseIMConfig, MessageContext
from modules.im.multi import IMClientRemovalError, MultiIMClient
from modules.settings_manager import MultiSettingsManager
from config.v2_sessions import ActivePollInfo
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    backend_dispatch_attempted,
    set_dispatch_phase,
)
from core.processing_indicator import ProcessingIndicatorService
from modules.agents.base import AgentRequest
from modules.agents.model_hub import OpenCodeOverlay, launch_for_context
from modules.agents.service import AgentService
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.server import (
    OpenCodeManagedPolicyRefreshPendingError,
    OpenCodeRuntimeConfigInvalidError,
)
from modules.agents.opencode.poll_loop import (
    OpenCodePollLoop,
    _settlement_assistant_message,
)
from modules.agents.opencode.utils import resolve_opencode_reasoning_effort


ATTEMPT_ID = "atm_1234567890abcdef1234567890abcdef"


@dataclass
class _StubConfig(BaseIMConfig):
    def validate(self) -> None:
        return None


def test_opencode_policy_refresh_failure_is_localized() -> None:
    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = type(
        "Controller",
        (),
        {"config": type("Config", (), {"language": "zh"})()},
    )()

    display = agent._server_start_error_display_text(
        OpenCodeManagedPolicyRefreshPendingError("internal diagnostic")
    )

    assert "仍在完成已有回合" in display
    assert "internal diagnostic" not in display


def test_opencode_runtime_config_failure_is_localized() -> None:
    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = type(
        "Controller",
        (),
        {"config": type("Config", (), {"language": "zh"})()},
    )()

    display = agent._server_start_error_display_text(
        OpenCodeRuntimeConfigInvalidError("internal diagnostic")
    )

    assert "运行时配置" in display
    assert "internal diagnostic" not in display


def test_opencode_hub_turn_with_empty_menu_uses_overlay_and_keeps_server_running(
    monkeypatch,
) -> None:
    calls: list[str] = []
    reservation = object()
    empty_overlay = OpenCodeOverlay(
        path=Path("/tmp/opencode-empty-overlay.json"),
        content_hash="empty-overlay-hash",
        content=(
            b'{"enabled_providers":["avibe-openai"],"provider":'
            b'{"avibe-openai":{"models":{}}}}\n'
        ),
        provider_ids=("avibe-openai",),
        model_provider_ids=(),
        checked_identifiers=(),
        available_identifiers=(),
        launches=(),
    )

    class _Runtime:
        @staticmethod
        def turn_mode(_backend):
            return "hub"

        @staticmethod
        async def prepare_opencode_overlay():
            return empty_overlay

    class _Server:
        async def configure_model_hub_overlay(self, overlay):
            assert overlay is empty_overlay
            calls.append("configure")
            return reservation

        async def release_model_hub_overlay_reservation(self, value):
            assert value is reservation
            calls.append("release")

        async def ensure_running(self):
            calls.append("ensure")

    server = _Server()

    async def _get_server():
        return server

    async def _emit_failure(*_args, **_kwargs):
        calls.append("failure")

    async def _remove_ack(_request):
        calls.append("ack")

    monkeypatch.setattr(
        "modules.agents.opencode.agent.emit_backend_failure",
        _emit_failure,
    )
    controller = type(
        "Controller",
        (),
        {
            "config": type("Config", (), {"language": "en"})(),
            "model_hub_runtime": _Runtime(),
        },
    )()
    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = controller
    agent.config = controller.config
    agent._get_server = _get_server
    agent._remove_ack_reaction = _remove_ack

    def _finish_after_start(_server):
        calls.append("attach")
        raise RuntimeError("test boundary after server start")

    agent._attach_server_activation = _finish_after_start
    request = AgentRequest(
        context=MessageContext(
            user_id="user",
            channel_id="channel",
            platform="slack",
            platform_specific={},
        ),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::channel",
    )

    asyncio.run(agent._process_message(request))

    assert calls == ["configure", "ensure", "attach", "release", "failure", "ack"]


class _StubClient(BaseIMClient):
    def __init__(self, name: str, *, supports_editing: bool = True, run_until_stopped: bool = False):
        super().__init__(_StubConfig())
        self.name = name
        self._supports_editing = supports_editing
        self._run_until_stopped = run_until_stopped
        self._stop_event = threading.Event()
        self.started = threading.Event()
        self.sent = []
        self.removed = []
        self.dismissed = []
        self.question_modals = []
        self.stopped = False

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent.append((context.platform, context.channel_id, text))
        return self.name

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        return self.name

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None):
        return True

    def supports_message_editing(self, context=None):
        return self._supports_editing

    async def remove_inline_keyboard(self, context, message_id, text=None, parse_mode=None):
        self.removed.append((context.platform, message_id, text))
        return True

    async def dismiss_form_message(self, context):
        self.dismissed.append((context.platform, context.message_id))

    async def open_question_modal(self, trigger_id, context, pending, callback_prefix="claude_question"):
        self.question_modals.append((trigger_id, context.platform, pending, callback_prefix))
        return self.name

    async def answer_callback(self, callback_id, text=None, show_alert=False):
        return True

    def register_handlers(self):
        return None

    def run(self):
        self.started.set()
        if self._run_until_stopped:
            self._stop_event.wait()
        return None

    def stop(self):
        self.stopped = True
        self._stop_event.set()

    async def get_user_info(self, user_id: str):
        return {"id": user_id, "name": self.name}

    async def get_channel_info(self, channel_id: str):
        return {"id": channel_id, "name": self.name}

    async def send_dm(self, user_id: str, text: str, **kwargs):
        self.sent.append(("dm", user_id, text))
        return self.name

    async def download_file(self, file_info, max_bytes=None, timeout_seconds=30):
        self.sent.append(("download", file_info.get("platform"), file_info.get("name")))
        return b"data"

    async def download_file_to_path(self, file_info, target_path, max_bytes=None, timeout_seconds=30):
        self.sent.append(("download_to_path", file_info.get("platform"), target_path))
        from modules.im.base import FileDownloadResult

        return FileDownloadResult(True, target_path)

    async def clear_typing_indicator(self, context):
        self.sent.append(("clear_typing", context.platform, context.user_id, (context.platform_specific or {}).get("context_token")))
        return True

    async def send_typing_indicator(self, context):
        self.sent.append(("typing", context.platform, context.user_id))
        return True

    async def delete_message(self, context, message_id):
        self.sent.append(("delete", context.platform, context.channel_id, message_id))
        return True

    def format_markdown(self, text: str) -> str:
        return text


class _ModalLessClient(_StubClient):
    open_question_modal = None


class _SlowStopClient(_StubClient):
    def __init__(self, name: str):
        super().__init__(name, run_until_stopped=True)
        self.stop_entered = threading.Event()
        self.finish_stop = threading.Event()

    def stop(self):
        self.stopped = True
        self.stop_entered.set()
        self._stop_event.set()
        self.finish_stop.wait(timeout=5)


class _CrashingClient(_StubClient):
    def __init__(self, name: str, exc: BaseException):
        super().__init__(name)
        self.exc = exc

    def run(self):
        self.started.set()
        raise self.exc


class _RestartOnceClient(_StubClient):
    def __init__(self, name: str, *, crash_first: bool):
        super().__init__(name, run_until_stopped=True)
        self.crash_first = crash_first
        self.run_calls = 0
        self.restarted = threading.Event()

    def run(self):
        self.run_calls += 1
        self.started.set()
        if self.run_calls == 1:
            if self.crash_first:
                raise RuntimeError(f"{self.name} failed")
            return
        self.restarted.set()
        self._stop_event.wait()


def test_multi_settings_manager_routes_scoped_keys(tmp_path):
    manager = MultiSettingsManager(
        ["slack", "wechat"], settings_file=str(tmp_path / "settings.json"), primary_platform="slack"
    )

    manager.set_custom_cwd("wechat::user-1", "/tmp/wx")
    manager.set_custom_cwd("slack::C123", "/tmp/slack")

    assert manager.get_custom_cwd("wechat::user-1") == "/tmp/wx"
    assert manager.get_custom_cwd("slack::C123") == "/tmp/slack"
    assert manager.managers["slack"].sessions is manager.sessions
    assert manager.managers["wechat"].sessions is manager.sessions


def test_multi_im_client_routes_send_by_context_platform():
    slack = _StubClient("slack")
    wechat = _StubClient("wechat")
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")

    asyncio.run(client.send_message(MessageContext(user_id="u", channel_id="c", platform="wechat"), "hello"))

    assert slack.sent == []
    assert wechat.sent == [("wechat", "c", "hello")]


def test_multi_im_client_delegates_question_modal_by_context_platform():
    slack = _StubClient("slack")
    discord = _StubClient("discord")
    client = MultiIMClient({"slack": slack, "discord": discord}, primary_platform="slack")
    context = MessageContext(user_id="u", channel_id="c", platform="discord")
    pending = {"questions": [{"header": "H", "question": "Q", "options": ["A"]}]}

    assert hasattr(client, "open_question_modal")
    result = asyncio.run(
        client.open_question_modal(
            trigger_id="trigger-1",
            context=context,
            pending=pending,
            callback_prefix="test_question",
        )
    )

    assert result == "discord"
    assert slack.question_modals == []
    assert discord.question_modals == [("trigger-1", "discord", pending, "test_question")]


def test_multi_im_client_question_modal_falls_back_for_modal_less_platform():
    wechat = _ModalLessClient("wechat")
    client = MultiIMClient({"wechat": wechat}, primary_platform="wechat")
    context = MessageContext(user_id="u", channel_id="c", platform="wechat")

    result = asyncio.run(client.open_question_modal("trigger-1", context, {"questions": []}, "test_question"))

    assert wechat.question_modals == []
    assert result == "wechat"
    assert wechat.sent == [("wechat", "c", "Modal UI is not available. Please reply with a custom message.")]


def test_multi_im_client_add_client_registers_callbacks_before_start():
    client = MultiIMClient({}, primary_platform="avibe")
    added = _StubClient("slack")
    captured: list[str | None] = []

    async def on_message(context: MessageContext, text: str):
        captured.append(context.platform)

    client.register_callbacks(on_message=on_message)
    client.add_client("slack", added)

    assert client.clients["slack"] is added
    assert added.on_message_callback is not None
    asyncio.run(added.on_message_callback(MessageContext(user_id="u", channel_id="c"), "hello"))
    assert captured == ["slack"]


def test_multi_im_client_remove_client_stops_and_drops_platform():
    slack = _StubClient("slack")
    wechat = _StubClient("wechat")
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")

    removed = client.remove_client("slack")

    assert removed is slack
    assert slack.stopped is True
    assert "slack" not in client.clients
    assert client.primary_platform == "wechat"


def test_multi_im_client_remove_last_client_restores_workbench_formatter():
    slack = _StubClient("slack")
    client = MultiIMClient({"slack": slack}, primary_platform="slack")

    removed = client.remove_client("slack")

    assert removed is slack
    assert client.clients == {}
    assert client.primary_platform == "avibe"
    assert client.formatter is not None
    assert "Warning" in client.formatter.format_warning("heads up")


def test_multi_im_client_connected_transport_does_not_emit_runtime_ready():
    slack = _StubClient("slack")
    discord = _StubClient("discord")
    client = MultiIMClient({"slack": slack, "discord": discord}, primary_platform="slack")
    ready_calls: list[bool] = []

    async def on_ready():
        ready_calls.append(True)

    client.register_callbacks(on_ready=on_ready)

    assert slack.on_ready_callback is not None
    asyncio.run(slack.on_ready_callback())
    assert ready_calls == []
    assert client._ready_platforms == {"slack"}
    assert client.is_transport_ready("slack") is True
    assert client.is_transport_ready("discord") is False

    removed = client.remove_client("discord")

    assert removed is discord
    assert ready_calls == []


def test_multi_im_client_clears_transport_readiness_on_disconnect():
    discord = _StubClient("discord")
    client = MultiIMClient({"discord": discord}, primary_platform="discord")
    unavailable_calls: list[str] = []

    async def on_transport_unready(*, platform: str):
        unavailable_calls.append(platform)

    client.register_callbacks(on_transport_unready=on_transport_unready)

    asyncio.run(discord.on_ready_callback())
    assert client.is_transport_ready("discord") is True

    asyncio.run(discord.on_transport_unready_callback())
    assert client.is_transport_ready("discord") is False
    assert unavailable_calls == ["discord"]

    asyncio.run(discord.on_transport_unready_callback())
    assert unavailable_calls == ["discord"]


def test_multi_im_client_remove_before_runtime_start_does_not_emit_ready():
    slack = _StubClient("slack")
    client = MultiIMClient({"slack": slack}, primary_platform="slack")
    ready_calls: list[bool] = []

    async def on_ready():
        ready_calls.append(True)

    client.register_callbacks(on_ready=on_ready)

    removed = client.remove_client("slack")

    assert removed is slack
    assert ready_calls == []


def test_multi_im_client_run_emits_ready_while_transports_are_unavailable():
    slack = _StubClient("slack", run_until_stopped=True)
    discord = _StubClient("discord", run_until_stopped=True)
    client = MultiIMClient({"slack": slack, "discord": discord}, primary_platform="slack")
    ready = threading.Event()
    ready_calls: list[bool] = []

    async def on_ready():
        ready_calls.append(True)
        ready.set()

    client.register_callbacks(on_ready=on_ready)
    thread = threading.Thread(target=client.run, daemon=True)
    thread.start()

    assert ready.wait(timeout=2)
    assert client._ready_platforms == set()
    assert ready_calls == [True]

    client.stop()
    thread.join(timeout=2)
    assert thread.is_alive() is False


def test_multi_im_client_run_stays_alive_when_all_enabled_threads_exit():
    client = MultiIMClient({"slack": _StubClient("slack")}, primary_platform="slack")
    returned: list[bool] = []

    def _run() -> None:
        client.run()
        returned.append(True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    assert client._run_started.wait(timeout=2)
    time.sleep(0.6)
    assert thread.is_alive() is True
    assert returned == []

    client.stop()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert returned == [True]


def test_multi_im_client_isolates_single_platform_runtime_crash():
    boom = RuntimeError("slack failed")
    crashing = _CrashingClient("slack", boom)
    client = MultiIMClient({"slack": crashing}, primary_platform="slack")
    returned: list[bool] = []

    def _run() -> None:
        client.run()
        returned.append(True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    assert crashing.started.wait(timeout=2)
    time.sleep(0.6)
    assert thread.is_alive() is True
    assert returned == []

    client.stop()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert returned == [True]


def test_multi_im_client_restarts_exited_and_crashed_platform_runtimes(monkeypatch):
    monkeypatch.setattr("modules.im.multi._RUNTIME_RETRY_INITIAL_SECONDS", 0.01)
    for crash_first in (False, True):
        restarting = _RestartOnceClient("slack", crash_first=crash_first)
        client = MultiIMClient({"slack": restarting}, primary_platform="slack")
        thread = threading.Thread(target=client.run, daemon=True)
        thread.start()

        assert restarting.started.wait(timeout=2)
        assert restarting.restarted.wait(timeout=2)
        assert restarting.run_calls == 2
        assert client._threads["slack"].is_alive() is True

        client.stop()
        thread.join(timeout=2)

        assert thread.is_alive() is False


def test_multi_im_client_isolates_crash_when_all_platform_threads_exit():
    boom = RuntimeError("discord failed")
    slack = _StubClient("slack")
    discord = _CrashingClient("discord", boom)
    client = MultiIMClient(
        {"slack": slack, "discord": discord},
        primary_platform="slack",
    )
    returned: list[bool] = []

    def _run() -> None:
        client.run()
        returned.append(True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    assert slack.started.wait(timeout=2)
    assert discord.started.wait(timeout=2)
    time.sleep(0.6)
    assert thread.is_alive() is True
    assert returned == []

    client.stop()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert returned == [True]


def test_multi_im_client_remove_client_keeps_maps_when_thread_will_not_stop():
    stuck = _StubClient("slack", run_until_stopped=True)
    client = MultiIMClient({"slack": stuck}, primary_platform="slack")
    never_stop = threading.Event()
    thread = threading.Thread(target=never_stop.wait, daemon=True)
    thread.start()
    client._threads["slack"] = thread

    try:
        try:
            client.remove_client("slack")
        except IMClientRemovalError:
            pass
        else:
            raise AssertionError("remove_client should fail when the old runtime thread stays alive")

        assert client.clients["slack"] is stuck
        assert client._threads["slack"] is thread
    finally:
        never_stop.set()
        thread.join(timeout=2)


def test_multi_im_client_hot_remove_last_client_does_not_return_runtime():
    slow = _SlowStopClient("slack")
    client = MultiIMClient({"slack": slow}, primary_platform="slack")
    returned: list[bool] = []
    removal_errors: list[BaseException] = []

    def _run() -> None:
        client.run()
        returned.append(True)

    runtime_thread = threading.Thread(target=_run, daemon=True)
    runtime_thread.start()
    assert client._run_started.wait(timeout=2)
    assert slow.started.wait(timeout=2)

    def _remove() -> None:
        try:
            client.remove_client("slack")
        except BaseException as exc:
            removal_errors.append(exc)

    remover = threading.Thread(target=_remove, daemon=True)
    remover.start()
    assert slow.stop_entered.wait(timeout=2)

    deadline = time.monotonic() + 2
    dead_platform_thread = False
    while time.monotonic() < deadline:
        with client._clients_lock:
            platform_thread = client._threads.get("slack")
        if platform_thread is not None and not platform_thread.is_alive():
            dead_platform_thread = True
            break
        time.sleep(0.01)
    assert dead_platform_thread is True

    time.sleep(0.7)
    assert runtime_thread.is_alive() is True
    assert returned == []

    slow.finish_stop.set()
    remover.join(timeout=2)
    assert remover.is_alive() is False
    assert removal_errors == []
    assert client.clients == {}
    assert client.primary_platform == "avibe"
    assert runtime_thread.is_alive() is True
    assert returned == []

    client.stop()
    runtime_thread.join(timeout=2)
    assert runtime_thread.is_alive() is False
    assert returned == [True]


def test_multi_im_client_empty_runtime_stays_alive_until_stop():
    client = MultiIMClient({}, primary_platform="avibe")
    returned: list[bool] = []

    assert client.formatter is not None
    assert "Error" in client.formatter.format_error("boom")

    def _run() -> None:
        client.run()
        returned.append(True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    assert client._run_started.wait(timeout=2)
    time.sleep(0.05)
    assert thread.is_alive() is True
    assert returned == []

    client.stop()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert returned == [True]


def test_multi_im_client_routes_message_edit_capability_by_context_platform():
    slack = _StubClient("slack")
    wechat = _StubClient("wechat", supports_editing=False)
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")

    assert client.supports_message_editing(MessageContext(user_id="u", channel_id="c", platform="slack"))
    assert not client.supports_message_editing(MessageContext(user_id="u", channel_id="c", platform="wechat"))


def test_multi_im_client_annotates_inbound_context_platform():
    slack = _StubClient("slack")
    wechat = _StubClient("wechat")
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")
    captured: list[str | None] = []

    async def on_message(context: MessageContext, text: str):
        captured.append(context.platform)

    client.register_callbacks(on_message=on_message)

    callback = wechat.on_message_callback
    assert callback is not None
    asyncio.run(callback(MessageContext(user_id="u", channel_id="c"), "hello"))

    assert captured == ["wechat"]


def test_multi_im_client_routes_scoped_identity_lookups():
    slack = _StubClient("slack")
    wechat = _StubClient("wechat")
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")

    user_info = asyncio.run(client.get_user_info("wechat::wx-user"))
    channel_info = asyncio.run(client.get_channel_info("wechat::wx-chat"))
    asyncio.run(client.send_dm("wechat::wx-user", "hello"))

    assert user_info == {"id": "wx-user", "name": "wechat"}
    assert channel_info == {"id": "wx-chat", "name": "wechat"}
    assert wechat.sent[-1] == ("dm", "wx-user", "hello")


def test_active_poll_info_round_trips_restored_typing_context():
    poll = ActivePollInfo(
        opencode_session_id="ses-1",
        base_session_id="base-1",
        channel_id="chan-1",
        thread_id="thread-1",
        settings_key="chan-1",
        working_path="/tmp/work",
        user_id="user-1",
        platform="wechat",
        typing_indicator_active=True,
        context_token="ctx-1",
        processing_indicator={
            "platform": "wechat",
            "user_id": "user-1",
            "channel_id": "chan-1",
            "thread_id": "thread-1",
            "context_token": "ctx-1",
            "typing_indicator_active": True,
        },
    )

    restored = ActivePollInfo.from_dict(poll.to_dict())

    assert restored.platform == "wechat"
    assert restored.typing_indicator_active is True
    assert restored.context_token == "ctx-1"
    assert restored.processing_indicator["context_token"] == "ctx-1"


def test_opencode_restored_ack_preserves_wechat_typing_context():
    captured = []
    wechat = _StubClient("wechat")

    class _StubAgent:
        def __init__(self):
            self.controller = type(
                "Controller",
                (),
                {
                    "config": type("Config", (), {"platform": "wechat", "ack_mode": "typing", "language": "en"})(),
                    "im_client": wechat,
                    "get_im_client_for_context": lambda self, context: wechat,
                },
            )()
            self.controller.processing_indicator = ProcessingIndicatorService(self.controller)

        async def _remove_ack_reaction(self, request):
            captured.append(request)
            await self.controller.processing_indicator.finish(request)

    poll = ActivePollInfo(
        opencode_session_id="ses-1",
        base_session_id="base-1",
        channel_id="chan-1",
        thread_id="thread-1",
        settings_key="chan-1",
        working_path="/tmp/work",
        user_id="user-1",
        platform="wechat",
        typing_indicator_active=True,
        context_token="ctx-1",
        processing_indicator={
            "platform": "wechat",
            "user_id": "user-1",
            "channel_id": "chan-1",
            "thread_id": "thread-1",
            "context_token": "ctx-1",
            "typing_indicator_active": True,
        },
    )
    loop = OpenCodePollLoop(_StubAgent())

    asyncio.run(loop.remove_restored_ack(poll))

    request = captured[0]
    assert request.typing_indicator_active is False
    assert request.context.platform == "wechat"
    assert request.context.platform_specific == {"platform": "wechat", "context_token": "ctx-1"}
    assert wechat.sent == [("clear_typing", "wechat", "user-1", "ctx-1")]


def test_opencode_prompt_disables_question_tool_for_all_platforms(monkeypatch):
    snapshot_id = "f" * 64
    snapshot_root = f"/old-avibe-home/builtin-skills/{snapshot_id}"
    monkeypatch.setenv("AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID", snapshot_id)
    monkeypatch.setenv("AVIBE_BUILTIN_SKILLS_ROOT", snapshot_root)
    calls = []
    active_polls = []
    active_poll_updates = []
    recovery_order = []
    overlay_reservation = object()
    configured_overlays = []
    active_registrations = []
    released_reservations = []
    prompt_skill_cwds = []
    prompt_memory_modes = []

    def build_prompt(**kwargs):
        prompt_skill_cwds.append(kwargs.get("skills_cwd"))
        prompt_memory_modes.append(kwargs.get("memory_enabled"))
        return "system prompt"

    monkeypatch.setattr(
        "modules.agents.opencode.agent.build_system_prompt_injection",
        build_prompt,
    )

    class _Server:
        async def configure_model_hub_overlay(self, overlay):
            configured_overlays.append(overlay)
            return overlay_reservation

        async def ensure_running(self):
            return None

        async def list_messages(self, session_id, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {
                        "id": "openai",
                        "models": {
                            "gpt-5.4": {
                                "variants": {"high": {}},
                            }
                        },
                    }
                ]
            }

        async def prompt_async(self, **kwargs):
            recovery_order.append("prompt")
            calls.append(kwargs)

        async def mark_run_active(self, session_id, *, overlay_reservation=None):
            active_registrations.append((session_id, overlay_reservation))

        async def release_model_hub_overlay_reservation(self, reservation):
            released_reservations.append(reservation)

        async def mark_run_inactive(self, session_id):
            return None

        def get_default_agent_from_config(self):
            return None

        def get_agent_model_from_config(self, _agent):
            return None

        def get_agent_reasoning_effort_from_config(self, _agent):
            return None

    class _SessionManager:
        async def ensure_working_dir(self, path):
            return None

        async def get_or_create_session_id(self, request, server):
            return "oc-session"

        def set_request_session(self, *args):
            return None

        def set_agent_session_id(self, *_args):
            return None

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        def add_active_poll(self, **kwargs):
            recovery_order.append("poll")
            active_polls.append(kwargs)
            return None

        def remove_active_poll(self, session_id):
            return None

        def update_active_poll_state(self, session_id, **kwargs):
            recovery_order.append("accepted")
            active_poll_updates.append((session_id, kwargs))

    class _PollLoop:
        async def run_prompt_poll(self, *args, **kwargs):
            return "done", True

    async def _get_server():
        return _Server()

    async def _async_noop():
        return None

    class _Controller:
        def __init__(self):
            self.config = type(
                "Config",
                (),
                {
                    "platform": "avibe",
                    "reply_enhancements": True,
                    "show_pages_prompt": True,
                    "remote_access": None,
                    "language": "en",
                    "memory": type("MemoryConfig", (), {"enabled": True})(),
                    "opencode": type(
                            "OpenCodeConfig",
                            (),
                            {
                                "default_provider": "openai",
                                "default_reasoning_effort": "high",
                            },
                    )(),
                },
            )()
            self.im_client = _StubClient("avibe")
            self.settings_manager = type("Settings", (), {"sessions": _Sessions()})()
            self.sessions = self.settings_manager.sessions
            self.processing_indicator = type("Processing", (), {"snapshot_request": lambda self, request: {}})()

        def get_opencode_overrides(self, context):
            return None, "openai/gpt-5.4", None

    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = _Controller()
    agent.config = agent.controller.config
    agent.im_client = agent.controller.im_client
    agent.settings_manager = agent.controller.settings_manager
    agent.sessions = agent.controller.sessions
    agent.opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
    agent._session_manager = _SessionManager()
    agent._poll_loop = _PollLoop()
    agent._steering_states = {}
    agent._get_server = _get_server
    agent._delete_ack = lambda request: _async_noop()
    agent._remove_ack_reaction = lambda request: _async_noop()
    agent.emit_result_message = lambda *args, **kwargs: _async_noop()

    async def _run():
        request = AgentRequest(
            context=MessageContext(
                user_id="u",
                channel_id="c",
                platform="slack",
                platform_specific={
                    "agent_session_id": "ses_test",
                    "turn_token": "logical-turn",
                    "delivery_start_attempt_id": ATTEMPT_ID,
                },
            ),
            message="hello",
            user_message="hello",
            working_path="/tmp/work",
            base_session_id="base",
            composite_session_id="base:/tmp/work",
            session_key="avibe::c",
        )
        await agent._process_message(request)

    asyncio.run(_run())

    assert calls
    assert calls[0]["tools"] == {"question": False, "skill": False}
    assert calls[0]["model"] == {"providerID": "openai", "modelID": "gpt-5.4"}
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["attempt_id"] == ATTEMPT_ID
    assert "message_id" not in calls[0]
    assert recovery_order[:3] == ["poll", "prompt", "accepted"]
    assert configured_overlays == [None]
    assert active_registrations == [("oc-session", overlay_reservation)]
    assert released_reservations == []
    assert active_poll_updates[0][0] == "oc-session"
    assert isinstance(active_poll_updates[0][1]["prompt_started_at"], float)
    steering_snapshot = active_polls[0]["processing_indicator"]["opencode_native_steering"]
    assert steering_snapshot["system"] == calls[0]["system"]
    assert active_polls[0]["processing_indicator"][
        "opencode_managed_skill_builtin_snapshot"
    ] == {"id": snapshot_id, "root": snapshot_root}
    assert prompt_skill_cwds == ["/tmp/work"]
    assert prompt_memory_modes == [True]

    binding_failures = []

    def fail_binding(*args, **kwargs):
        raise OSError("binding unavailable")

    async def record_failure(context, error_text):
        binding_failures.append(error_text)

    async def emit_failure(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "modules.agents.opencode.agent.bind_caller_context_session",
        fail_binding,
    )
    monkeypatch.setattr(
        "modules.agents.opencode.agent.emit_backend_failure",
        emit_failure,
    )
    agent.record_model_hub_native_failure = record_failure
    calls.clear()

    asyncio.run(_run())

    assert calls
    assert calls[0]["tools"] == {"question": False, "skill": False}
    assert prompt_skill_cwds == ["/tmp/work", None]
    assert binding_failures == []


def test_opencode_clears_default_variant_for_non_reasoning_model():
    catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "glm-5.2": {
                        "capabilities": {"reasoning": False},
                        "variants": {},
                    }
                },
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "glm", "modelID": "glm-5.2"},
            None,
            catalog,
        )
        is None
    )


def test_opencode_clears_default_variant_for_model_without_variant_metadata():
    catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "glm-5.2": {
                        "id": "glm-5.2",
                        "name": "GLM 5.2",
                    }
                },
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "glm", "modelID": "glm-5.2"},
            None,
            catalog,
        )
        is None
    )


def test_opencode_keeps_unspecified_variant_when_catalog_says_reasoning_supported():
    catalog = {
        "providers": [
            {
                "id": "openai",
                "models": {
                    "gpt-5.4": {
                        "id": "gpt-5.4",
                        "capabilities": {"reasoning": True},
                    }
                },
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "openai", "modelID": "gpt-5.4"},
            None,
            catalog,
        )
        is None
    )


def test_opencode_clears_default_variant_for_list_model_catalog():
    catalog = {
        "providers": [
            {
                "provider_id": "glm",
                "models": [
                    {
                        "id": "glm-5.2",
                        "name": "GLM 5.2",
                    }
                ],
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "glm", "modelID": "glm-5.2"},
            None,
            catalog,
        )
        is None
    )


def test_opencode_keeps_supported_reasoning_variant():
    catalog = {
        "providers": [
            {
                "id": "openai",
                "models": {
                    "gpt-5.4": {
                        "capabilities": {"reasoning": True},
                        "variants": {"high": {"reasoningEffort": "high"}},
                    }
                },
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "openai", "modelID": "gpt-5.4"},
            "high",
            catalog,
        )
        == "high"
    )


def test_opencode_clears_unsupported_requested_variant():
    catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "glm-5.2": {
                        "variants": {"high": {"thinking": {"effort": "high"}}},
                    }
                },
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "glm", "modelID": "glm-5.2"},
            "default",
            catalog,
        )
        is None
    )


def test_opencode_keeps_supported_reasoning_variant_for_list_model_catalog():
    catalog = {
        "providers": [
            {
                "name": "openai",
                "models": [
                    {
                        "id": "gpt-5.4",
                        "capabilities": {"reasoning": True},
                        "variants": {"high": {"reasoningEffort": "high"}},
                    }
                ],
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "openai", "modelID": "gpt-5.4"},
            "high",
            catalog,
        )
        == "high"
    )


def test_opencode_keeps_requested_variant_when_catalog_says_reasoning_supported():
    catalog = {
        "providers": [
            {
                "id": "openai",
                "models": {
                    "gpt-5.4": {
                        "id": "gpt-5.4",
                        "capabilities": {"reasoning": True},
                    }
                },
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "openai", "modelID": "gpt-5.4"},
            "high",
            catalog,
        )
        == "high"
    )


def test_opencode_clears_unsupported_reasoning_variant():
    catalog = {
        "providers": [
            {
                "id": "anthropic",
                "models": {
                    "claude-opus-4-5": {
                        "capabilities": {"reasoning": True},
                        "variants": {"low": {"effort": "low"}},
                    }
                },
            }
        ]
    }

    assert (
        resolve_opencode_reasoning_effort(
            {"providerID": "anthropic", "modelID": "claude-opus-4-5"},
            "max",
            catalog,
        )
        is None
    )


def test_opencode_fork_prompt_marks_target_session_id_authoritative():
    calls = []

    class _Server:
        async def ensure_running(self):
            return None

        async def list_messages(self, session_id, directory):
            return []

        async def prompt_async(self, **kwargs):
            calls.append(kwargs)

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            return None

        def get_default_agent_from_config(self):
            return None

        def get_agent_model_from_config(self, _agent):
            return None

        def get_agent_reasoning_effort_from_config(self, _agent):
            return None

    class _SessionManager:
        async def ensure_working_dir(self, path):
            return None

        async def get_or_create_session_id(self, request, server):
            return "oc-fork"

        def set_request_session(self, *args):
            return None

        def set_agent_session_id(self, *_args):
            return None

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        def add_active_poll(self, **kwargs):
            return None

        def remove_active_poll(self, session_id):
            return None

    class _PollLoop:
        async def run_prompt_poll(self, *args, **kwargs):
            return "done", True

    async def _get_server():
        return _Server()

    async def _async_noop():
        return None

    class _Controller:
        def __init__(self):
            self.config = type(
                "Config",
                (),
                {
                    "platform": "avibe",
                    "reply_enhancements": True,
                    "show_pages_prompt": True,
                    "remote_access": None,
                    "language": "en",
                    "opencode": type(
                        "OpenCodeConfig",
                        (),
                        {
                            "default_model": None,
                            "default_provider": None,
                            "default_reasoning_effort": None,
                        },
                    )(),
                },
            )()
            self.im_client = _StubClient("avibe")
            self.settings_manager = type("Settings", (), {"sessions": _Sessions()})()
            self.sessions = self.settings_manager.sessions
            self.processing_indicator = type("Processing", (), {"snapshot_request": lambda self, request: {}})()

        def get_opencode_overrides(self, context):
            return None, None, None

    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = _Controller()
    agent.config = agent.controller.config
    agent.im_client = agent.controller.im_client
    agent.settings_manager = agent.controller.settings_manager
    agent.sessions = agent.controller.sessions
    agent.opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
    agent._session_manager = _SessionManager()
    agent._poll_loop = _PollLoop()
    agent._steering_states = {}
    agent._get_server = _get_server
    agent._delete_ack = lambda request: _async_noop()
    agent._remove_ack_reaction = lambda request: _async_noop()
    agent.emit_result_message = lambda *args, **kwargs: _async_noop()

    async def _run():
        request = AgentRequest(
            context=MessageContext(
                user_id="u",
                channel_id="ses-target",
                platform="avibe",
                platform_specific={
                    "agent_session_id": "ses-target",
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "opencode",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "oc-source",
                            "source_backend": "opencode",
                        },
                    },
                },
            ),
            message="hello",
            user_message="hello",
            working_path="/tmp/work",
            base_session_id="ses-target",
            composite_session_id="ses-target:/tmp/work",
            session_key="avibe::ses-target",
        )
        await agent._process_message(request)

    asyncio.run(_run())

    system = calls[0]["system"]
    assert "Current session id: `ses-target`" in system
    assert "This Agent Session was forked from `ses-source`." in system
    assert "The authoritative Avibe session id for this fork is `ses-target`." in system
    assert "treat it as historical source-context only" in system
    assert "use `ses-target` for Show Pages" not in system


def test_opencode_normal_text_matching_legacy_question_prefix_is_processed():
    processed = []

    class _Controller:
        def __init__(self):
            self.config = type("Config", (), {})()
            self.im_client = _StubClient("slack")
            self.settings_manager = type("Settings", (), {"sessions": object()})()

    class _SessionManager:
        def get_session_lock(self, base_session_id):
            return asyncio.Lock()

        def pop_request_session(self, base_session_id):
            return None

    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = _Controller()
    agent.config = agent.controller.config
    agent.im_client = agent.controller.im_client
    agent.settings_manager = agent.controller.settings_manager
    agent._session_manager = _SessionManager()
    agent._active_requests = {}

    async def _process_message(request):
        processed.append(request.message)

    agent._process_message = _process_message

    async def _run():
        request = AgentRequest(
            context=MessageContext(user_id="u", channel_id="c", platform="slack"),
            message="opencode_question:choose:1",
            user_message="",
            working_path="/tmp/work",
            base_session_id="base",
            composite_session_id="base:/tmp/work",
            session_key="slack::c",
        )
        await agent.handle_message(request)

    asyncio.run(_run())

    assert processed == ["opencode_question:choose:1"]


def test_opencode_process_message_removes_active_poll_when_question_tool_aborts():
    removed = []
    ack_removed = []
    retirement_order = []

    request_context = MessageContext(
        user_id="u",
        channel_id="c",
        platform="slack",
        platform_specific={"agent_session_id": "ses_test"},
    )
    set_dispatch_phase(request_context, DISPATCH_PHASE_PREWRITE)

    class _Server:
        async def ensure_running(self):
            return None

        async def list_messages(self, session_id, directory):
            return []

        async def prompt_async(self, **kwargs):
            assert backend_dispatch_attempted(request_context) is True
            return None

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            retirement_order.append(("marker", session_id))
            return None

        def get_default_agent_from_config(self):
            return None

        def get_agent_model_from_config(self, _agent):
            return None

        def get_agent_reasoning_effort_from_config(self, _agent):
            return None

    class _SessionManager:
        async def ensure_working_dir(self, path):
            return None

        async def get_or_create_session_id(self, request, server):
            return "oc-session"

        def set_request_session(self, *args):
            return None

        def set_agent_session_id(self, *_args):
            return None

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        def add_active_poll(self, **kwargs):
            return None

        def remove_active_poll(self, session_id):
            retirement_order.append(("poll", session_id))
            removed.append(session_id)

    class _PollLoop:
        async def run_prompt_poll(self, *args, **kwargs):
            return None, False

    class _Controller:
        def __init__(self):
            self.config = type(
                "Config",
                (),
                {
                    "platform": "slack",
                    "reply_enhancements": True,
                    "show_pages_prompt": True,
                    "remote_access": None,
                    "language": "en",
                },
            )()
            self.im_client = _StubClient("slack")
            self.settings_manager = type("Settings", (), {"sessions": _Sessions()})()
            self.sessions = self.settings_manager.sessions
            self.processing_indicator = type("Processing", (), {"snapshot_request": lambda self, request: {}})()

        def get_opencode_overrides(self, context):
            return None, None, None

    async def _get_server():
        return _Server()

    async def _async_noop():
        return None

    async def _remove_ack(request):
        ack_removed.append(request.base_session_id)

    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = _Controller()
    agent.config = agent.controller.config
    agent.im_client = agent.controller.im_client
    agent.settings_manager = agent.controller.settings_manager
    agent.sessions = agent.controller.sessions
    agent.opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
    agent._session_manager = _SessionManager()
    agent._poll_loop = _PollLoop()
    agent._steering_states = {}
    agent._get_server = _get_server
    agent._delete_ack = lambda request: _async_noop()
    agent._remove_ack_reaction = _remove_ack

    request = AgentRequest(
        context=request_context,
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    asyncio.run(agent._process_message(request))

    assert retirement_order == [
        ("marker", "oc-session"),
        ("poll", "oc-session"),
    ]
    assert removed == ["oc-session"]
    assert ack_removed == ["base"]


def test_opencode_poll_aborts_disabled_question_toolcall():
    emitted = []
    aborted = []

    class _Formatter:
        def format_toolcall(self, *args, **kwargs):
            return "tool"

    class _Controller:
        def _t(self, key):
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
        ):
            emitted.append((message_type, text))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()
        im_client = type("IM", (), {"formatter": _Formatter()})()

        def _get_formatter(self, context):
            return _Formatter()

        def _to_relative_path(self, path, working_path):
            return path

        def _extract_response_text(self, message):
            return ""

    class _Server:
        async def list_messages(self, session_id, directory):
            return [
                {
                    "info": {"id": "msg-1", "role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "id": "part-1",
                            "tool": "question",
                            "state": {"status": "pending", "input": {"questions": []}},
                        }
                    ],
                }
            ]

        async def abort_session(self, session_id, directory):
            aborted.append((session_id, directory))
            return True

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    loop = OpenCodePollLoop(_Agent())
    final_text, should_emit = asyncio.run(
        loop.run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert final_text is None
    assert should_emit is False
    assert aborted == [("oc-session", "/tmp/work")]
    # A disabled-question abort is a terminal FAILURE → emitted as an error RESULT
    # (the outbound chokepoint turns the dot red), not a bare notify that never
    # settles the dot.
    assert emitted[0][0] == "result"
    assert emitted[0][1] == "translated:error.opencodeQuestionToolDisabled"


def test_settlement_assistant_message_walks_back_to_the_owning_turn():
    """Every live snapshot shape settles the same owning assistant.

    Seed the shapes already seen in production: last-is-assistant, a trailing
    user inject, an empty leftover generation after a completed error, and a
    follow-up that already has parts. The owner is a property of the snapshot,
    not of which id happens to sit at messages[-1].
    """

    error_assistant = {
        "info": {
            "id": "msg-err",
            "role": "assistant",
            "time": {"completed": 1},
            "error": {"name": "UnknownError"},
        },
        "parts": [],
    }
    trailing_user = {
        "info": {"id": "msg-user", "role": "user", "time": {}},
        "parts": [{"type": "text", "text": "继续"}],
    }
    empty_inflight = {
        "info": {"id": "msg-empty", "role": "assistant", "time": {}},
        "parts": [],
    }
    live_followup = {
        "info": {"id": "msg-live", "role": "assistant", "time": {}},
        "parts": [{"type": "text", "text": "working"}],
    }
    completed_ok = {
        "info": {
            "id": "msg-ok",
            "role": "assistant",
            "time": {"completed": 1},
            "finish": "stop",
        },
        "parts": [{"type": "text", "text": "done"}],
    }

    cases = (
        ([error_assistant], False, "msg-err"),
        ([error_assistant], True, "msg-err"),
        ([error_assistant, trailing_user], False, "msg-err"),
        ([error_assistant, trailing_user], True, None),
        ([error_assistant, trailing_user, empty_inflight], False, None),
        ([error_assistant, trailing_user, empty_inflight], True, None),
        ([error_assistant, live_followup], True, None),
        ([error_assistant, live_followup], False, None),
        ([completed_ok, trailing_user], False, "msg-ok"),
        ([completed_ok, trailing_user], True, None),
        ([trailing_user], False, None),
        ([trailing_user], True, None),
        ([error_assistant, trailing_user, empty_inflight, completed_ok], False, "msg-ok"),
    )
    for messages, native_live, expected_id in cases:
        owner = _settlement_assistant_message(
            messages, set(), native_live=native_live
        )
        actual_id = None if owner is None else owner["info"]["id"]
        assert actual_id == expected_id, (native_live, expected_id, actual_id)

    owner = _settlement_assistant_message(
        [error_assistant],
        set(),
        native_live=False,
        awaiting_after_ids={"msg-err"},
    )
    assert owner is None
    owner = _settlement_assistant_message(
        [error_assistant, trailing_user],
        set(),
        native_live=False,
        awaiting_after_ids={"msg-err"},
    )
    assert owner is None
    owner = _settlement_assistant_message(
        [error_assistant, trailing_user, completed_ok],
        set(),
        native_live=False,
        awaiting_after_ids={"msg-err"},
    )
    assert owner is not None and owner["info"]["id"] == "msg-ok"


def test_opencode_poll_notifies_and_settles_on_retry_exhaustion():
    # A completed assistant message carrying an error, with retries exhausted
    # (error_retry_limit=0) and the auth-recovery path declining (non-auth error),
    # is a terminal FAILURE. It emits one visible notification plus one silent
    # failed settlement, then suppresses the idle "(No response from OpenCode)"
    # result that would overwrite the terminal state.
    emitted = []
    model_hub_failures = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Formatter:
        def format_toolcall(self, *args, **kwargs):
            return "tool"

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            if key == "error.opencodeBackendError":
                return f"OpenCode error: {kwargs['error']}"
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()
        im_client = type("IM", (), {"formatter": _Formatter()})()

        def _get_formatter(self, context):
            return _Formatter()

        def _to_relative_path(self, path, working_path):
            return path

        def _extract_response_text(self, message):
            return ""

        async def record_model_hub_native_failure(self, context, diagnostic):
            model_hub_failures.append((context, diagnostic))
            return True

    class _Server:
        async def list_messages(self, session_id, directory):
            return [
                {
                    "info": {
                        "id": "msg-err",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "error": {"name": "ProviderError", "data": {"message": "rate limited"}},
                    },
                    "parts": [],
                }
            ]

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    loop = OpenCodePollLoop(_Agent())
    final_text, should_emit = asyncio.run(
        loop.run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert final_text is None
    # should_emit False → caller skips the idle "(No response)" warning that would
    # otherwise reset the dot we just turned red.
    assert should_emit is False
    assert [item[0] for item in emitted] == ["notify", "result"]
    assert emitted[0][1] == "OpenCode error: ProviderError - rate limited"
    assert emitted[1][1:] == ("", True, "silent", "ProviderError - rate limited")
    assert model_hub_failures == [(request.context, "ProviderError - rate limited")]


def test_opencode_poll_settles_error_when_trailing_user_is_last():
    """A watch/steer inject after a completed error is not the turn.

    The live hang was: assistant completed with error, then a user message
    ("继续" / a watch callback) became messages[-1], so the last-only
    settlement never ran.
    """

    emitted = []
    model_hub_failures = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            if key == "error.opencodeBackendError":
                return f"OpenCode error: {kwargs['error']}"
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()

        def _extract_response_text(self, message):
            return ""

        async def record_model_hub_native_failure(self, context, diagnostic):
            model_hub_failures.append(diagnostic)
            return True

    class _Server:
        async def list_messages(self, session_id, directory):
            return [
                {
                    "info": {
                        "id": "msg-err",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "error": {
                            "name": "UnknownError",
                            "data": {"message": "unknown certificate verification error"},
                        },
                    },
                    "parts": [],
                },
                {
                    "info": {"id": "msg-user", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "继续"}],
                },
            ]

        async def get_session_status(self, session_id, directory):
            return None

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    final_text, should_emit = asyncio.run(
        OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert (final_text, should_emit) == (None, False)
    assert [item[0] for item in emitted] == ["notify", "result"]
    assert "certificate verification" in emitted[0][1]
    assert model_hub_failures == [
        "UnknownError - unknown certificate verification error"
    ]


def test_opencode_poll_keeps_accepted_steer_pending_while_native_busy(monkeypatch):
    """A trailing user while OpenCode is busy is a live steer, not a hang."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    emitted = []
    polls = {"n": 0}

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            return f"translated:{key}"

        async def emit_agent_message(self, *args, **kwargs):
            emitted.append((args, kwargs))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()

        def _extract_response_text(self, message):
            return "steered"

        async def record_model_hub_native_failure(self, context, diagnostic):
            raise AssertionError("must not settle the prior turn while steer is live")

    class _Server:
        async def list_messages(self, session_id, directory):
            polls["n"] += 1
            followup_ready = polls["n"] >= 3
            rows = [
                {
                    "info": {
                        "id": "msg-ok",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "finish": "stop",
                    },
                    "parts": [{"type": "text", "text": "old"}],
                },
                {
                    "info": {"id": "msg-steer", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "steer"}],
                },
            ]
            if followup_ready:
                rows.append(
                    {
                        "info": {
                            "id": "msg-new",
                            "role": "assistant",
                            "time": {"completed": 1},
                            "finish": "stop",
                        },
                        "parts": [{"type": "text", "text": "steered"}],
                    }
                )
            return rows

        async def get_session_status(self, session_id, directory):
            if polls["n"] >= 3:
                return {"type": "idle"}
            return {"type": "busy"}

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    final_text, should_emit = asyncio.run(
        OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert (final_text, should_emit) == ("steered", True)
    assert polls["n"] == 3
    assert not any(item[0][1] == "result" for item in emitted)


def test_opencode_poll_uses_snapshot_live_flag_instead_of_rereading_status(monkeypatch):
    """The wrapper's busy decision wins over a later idle status read."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    emitted = []
    polls = {"n": 0}
    status_reads = {"n": 0}

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            return f"translated:{key}"

        async def emit_agent_message(self, *args, **kwargs):
            emitted.append((args, kwargs))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()

        def _extract_response_text(self, message):
            return "steered"

        async def record_model_hub_native_failure(self, context, diagnostic):
            raise AssertionError("stale idle status must not settle the pre-steer turn")

    class _Server:
        last_list_native_live = True

        async def list_messages(self, session_id, directory):
            polls["n"] += 1
            followup_ready = polls["n"] >= 3
            if followup_ready:
                self.last_list_native_live = False
            rows = [
                {
                    "info": {
                        "id": "msg-ok",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "finish": "stop",
                    },
                    "parts": [{"type": "text", "text": "old"}],
                },
                {
                    "info": {"id": "msg-steer", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "steer"}],
                },
            ]
            if followup_ready:
                rows.append(
                    {
                        "info": {
                            "id": "msg-new",
                            "role": "assistant",
                            "time": {"completed": 1},
                            "finish": "stop",
                        },
                        "parts": [{"type": "text", "text": "steered"}],
                    }
                )
            return rows

        async def get_session_status(self, session_id, directory):
            status_reads["n"] += 1
            return None

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    final_text, should_emit = asyncio.run(
        OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert (final_text, should_emit) == ("steered", True)
    assert polls["n"] == 3
    assert status_reads["n"] == 0
    assert not any(item[0][1] == "result" for item in emitted)


def test_opencode_poll_keeps_retry_pending_on_empty_inflight_assistant(monkeypatch):
    """Auto-retry ``continue`` must stay live until the new assistant completes."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    emitted = []
    polls = {"n": 0}

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            if key == "error.opencodeBackendError":
                return f"OpenCode error: {kwargs['error']}"
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()

        def _extract_response_text(self, message):
            return "recovered"

        async def record_model_hub_native_failure(self, context, diagnostic):
            raise AssertionError("must not settle while retry is in flight")

    class _Server:
        async def list_messages(self, session_id, directory):
            polls["n"] += 1
            followup_completed = polls["n"] >= 3
            return [
                {
                    "info": {
                        "id": "msg-err",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "error": {"name": "UnknownError", "data": {"message": "tls failed"}},
                    },
                    "parts": [],
                },
                {
                    "info": {"id": "msg-user", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "continue"}],
                },
                {
                    "info": {
                        "id": "msg-empty",
                        "role": "assistant",
                        "time": {"completed": 1} if followup_completed else {},
                        "finish": "stop" if followup_completed else None,
                    },
                    "parts": (
                        [{"type": "text", "text": "recovered"}] if followup_completed else []
                    ),
                },
            ]

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    final_text, should_emit = asyncio.run(
        OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert (final_text, should_emit) == ("recovered", True)
    assert polls["n"] == 3
    assert not any(item[0] == "result" for item in emitted)


def test_opencode_poll_keeps_continue_pending_through_idle_visibility_gap(monkeypatch):
    """After posting continue, an idle trailing-user snapshot is still live."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POST_INJECT_CONFIRMATION_SECONDS", 1.0
    )
    emitted = []
    polls = {"n": 0}

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            if key == "error.opencodeBackendError":
                return f"OpenCode error: {kwargs['error']}"
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 1})()
        controller = _Controller()

        def _extract_response_text(self, message):
            return "recovered"

        async def record_model_hub_native_failure(self, context, diagnostic):
            raise AssertionError("must not settle during the continue visibility gap")

    class _Server:
        async def list_messages(self, session_id, directory):
            polls["n"] += 1
            if polls["n"] == 1:
                return [
                    {
                        "info": {
                            "id": "msg-err",
                            "role": "assistant",
                            "time": {"completed": 1},
                            "error": {"name": "UnknownError", "data": {"message": "tls"}},
                        },
                        "parts": [],
                    }
                ]
            if polls["n"] == 2:
                return [
                    {
                        "info": {
                            "id": "msg-err",
                            "role": "assistant",
                            "time": {"completed": 1},
                            "error": {"name": "UnknownError", "data": {"message": "tls"}},
                        },
                        "parts": [],
                    },
                    {
                        "info": {"id": "msg-continue", "role": "user", "time": {}},
                        "parts": [{"type": "text", "text": "continue"}],
                    },
                ]
            return [
                {
                    "info": {
                        "id": "msg-err",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "error": {"name": "UnknownError", "data": {"message": "tls"}},
                    },
                    "parts": [],
                },
                {
                    "info": {"id": "msg-continue", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "continue"}],
                },
                {
                    "info": {
                        "id": "msg-new",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "finish": "stop",
                    },
                    "parts": [{"type": "text", "text": "recovered"}],
                },
            ]

        async def prompt_async(self, **kwargs):
            return None

        async def get_session_status(self, session_id, directory):
            return None

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    final_text, should_emit = asyncio.run(
        OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert (final_text, should_emit) == ("recovered", True)
    assert polls["n"] >= 3
    assert not any(item[0] == "result" for item in emitted)


def test_opencode_poll_keeps_retry_pending_before_continue_user_appears(monkeypatch):
    """Awaiting boundary holds the old error until post-inject evidence exists."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    emitted = []
    polls = {"n": 0}

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            if key == "error.opencodeBackendError":
                return f"OpenCode error: {kwargs['error']}"
            return f"translated:{key}"

        async def emit_agent_message(self, *args, **kwargs):
            emitted.append((args, kwargs))

    class _State:
        awaiting_after_message_ids = {"msg-err"}
        awaiting_user_text = "continue"

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 1})()
        controller = _Controller()

        def _extract_response_text(self, message):
            return "recovered"

        async def record_model_hub_native_failure(self, context, diagnostic):
            raise AssertionError("must not settle the prior error before continue appears")

    class _Server:
        def __init__(self):
            self._state = _State()

        async def list_messages(self, session_id, directory):
            polls["n"] += 1
            error = {
                "info": {
                    "id": "msg-err",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "error": {"name": "UnknownError", "data": {"message": "tls"}},
                },
                "parts": [],
            }
            if polls["n"] == 1:
                return [error]
            if polls["n"] == 2:
                return [
                    error,
                    {
                        "info": {"id": "msg-continue", "role": "user", "time": {}},
                        "parts": [{"type": "text", "text": "continue"}],
                    },
                ]
            return [
                error,
                {
                    "info": {"id": "msg-continue", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "continue"}],
                },
                {
                    "info": {
                        "id": "msg-new",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "finish": "stop",
                    },
                    "parts": [{"type": "text", "text": "recovered"}],
                },
            ]

        async def get_session_status(self, session_id, directory):
            return None

        async def prompt_async(self, **kwargs):
            return None

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    final_text, should_emit = asyncio.run(
        OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert (final_text, should_emit) == ("recovered", True)
    assert polls["n"] >= 3
    assert not any(item[0][1] == "result" for item in emitted)


def test_opencode_poll_does_not_settle_error_while_followup_has_parts():
    """A follow-up assistant that already has parts is still the live turn."""

    emitted = []
    polls = {"n": 0}

    class _Controller:
        def _t(self, key, **kwargs):
            return f"translated:{key}"

        async def emit_agent_message(self, *args, **kwargs):
            emitted.append((args, kwargs))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()

        def _extract_response_text(self, message):
            return "recovered"

        async def record_model_hub_native_failure(self, context, diagnostic):
            raise AssertionError("must not settle the earlier error")

    class _Server:
        async def list_messages(self, session_id, directory):
            polls["n"] += 1
            followup_completed = polls["n"] >= 2
            return [
                {
                    "info": {
                        "id": "msg-err",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "error": {"name": "UnknownError", "data": {"message": "old"}},
                    },
                    "parts": [],
                },
                {
                    "info": {
                        "id": "msg-live",
                        "role": "assistant",
                        "time": {"completed": 1} if followup_completed else {},
                        "finish": "stop" if followup_completed else None,
                    },
                    "parts": [{"type": "text", "text": "recovered"}],
                },
            ]

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    final_text, should_emit = asyncio.run(
        OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert (final_text, should_emit) == ("recovered", True)
    assert polls["n"] == 2
    assert not any(item[0][1] == "result" for item in emitted)


def test_opencode_poll_keeps_explicit_empty_completion_on_success_path():
    emitted = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(self, context, backend, message):
            return False

    class _Formatter:
        def format_toolcall(self, *args, **kwargs):
            return "tool"

    class _Controller:
        agent_auth_service = _AuthSvc()

        def __init__(self):
            self.config = type("Config", (), {"platform": "slack", "ack_mode": "reaction", "language": "en"})()
            self.im_client = type("IM", (), {"formatter": _Formatter()})()
            self.processing_indicator = ProcessingIndicatorService(self)

        def _t(self, key, **kwargs):
            if key == "common.default":
                return "(Default)"
            if key == "error.opencodeEmptyResponse":
                return "empty:{provider}/{model}/{variant}".format(**kwargs)
            if key == "error.opencodeProviderRuntimeError":
                return "provider:{provider}/{model}/{variant}:{detail}".format(**kwargs)
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
        ):
            emitted.append((message_type, text, is_error, level))

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()
        im_client = type("IM", (), {"formatter": _Formatter()})()

        def _get_formatter(self, context):
            return _Formatter()

        def _to_relative_path(self, path, working_path):
            return path

        def _extract_response_text(self, message):
            return ""

    class _Server:
        async def get_recent_session_error(self, session_id, since=None):
            return "AI_APICallError (ECONNRESET) while calling https://relay.example/messages"

        async def get_provider_api_diagnostic(self, provider_id, model_id):
            return None

        def get_last_prompt_started_at(self, session_id):
            return 42.0

        async def list_messages(self, session_id, directory):
            return [
                {
                    "info": {
                        "id": "msg-empty",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "finish": "unknown",
                        "tokens": {
                            "input": 8,
                            "output": 4,
                            "reasoning": 2,
                            "cache": {"read": 1, "write": 0},
                        },
                    },
                    "parts": [
                        {"type": "step-start", "id": "step-start"},
                        {"type": "step-finish", "id": "step-finish"},
                    ],
                }
            ]

    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack"),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    loop = OpenCodePollLoop(_Agent())
    final_text, should_emit = asyncio.run(
        loop.run_prompt_poll(
            request,
            _Server(),
            "oc-session",
            agent_to_use=None,
            model_dict={"providerID": "glm", "modelID": "glm-5.2"},
            reasoning_effort=None,
            baseline_message_ids=set(),
        )
    )

    assert final_text is None
    assert should_emit is True
    assert emitted == []


def test_opencode_restored_poll_settles_error_after_retry_budget(monkeypatch):
    """A restored poll must emit the backend error, not (No response)."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    emitted = []
    removed = []
    results = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def __init__(self):
            self.config = type("Config", (), {"language": "en"})()
            self.processing_indicator = ProcessingIndicatorService(self)

        def _t(self, key, **kwargs):
            if key == "error.opencodeBackendError":
                return f"OpenCode error: {kwargs['error']}"
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Sessions:
        def remove_active_poll(self, session_id):
            removed.append(session_id)

        def update_active_poll_state(self, session_id, **kwargs):
            return None

    class _Server:
        async def list_messages(self, session_id, directory):
            return [
                {
                    "info": {
                        "id": "msg-err",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "error": {
                            "name": "UnknownError",
                            "data": {"message": "certificate failed"},
                        },
                    },
                    "parts": [],
                },
                {
                    "info": {"id": "msg-user", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "继续"}],
                },
            ]

        async def get_session_status(self, session_id, directory):
            return None

    server = _Server()

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 1})()
        controller = _Controller()
        sessions = _Sessions()

        async def _get_server(self):
            return server

        def _extract_response_text(self, message):
            return ""

        async def emit_result_message(self, context, text, **kwargs):
            results.append(text)

        async def _remove_ack_reaction(self, request):
            return None

        async def record_model_hub_native_failure(self, context, diagnostic):
            return True

    poll = ActivePollInfo(
        opencode_session_id="oc-session",
        base_session_id="base",
        channel_id="c",
        thread_id="t",
        settings_key="c",
        working_path="/tmp/work",
        baseline_message_ids=[],
        platform="slack",
        prompt_started_at=time.time(),
    )

    terminal = asyncio.run(OpenCodePollLoop(_Agent()).run_restored_poll_loop(poll))

    assert terminal is True
    assert removed == []
    assert any(item[0] == "result" and item[2] is True for item in emitted)
    assert all("No response from OpenCode" not in str(item) for item in results)
    assert any("certificate failed" in (item[1] or "") for item in emitted)


def test_opencode_restored_poll_keeps_empty_completion_successful():
    emitted = []
    removed = []
    diagnostics = []
    results = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(self, context, backend, message):
            return False

    class _Formatter:
        def format_toolcall(self, *args, **kwargs):
            return "tool"

    class _Controller:
        agent_auth_service = _AuthSvc()

        def __init__(self):
            self.config = type("Config", (), {"platform": "slack", "ack_mode": "reaction", "language": "en"})()
            self.im_client = type("IM", (), {"formatter": _Formatter()})()
            self.processing_indicator = ProcessingIndicatorService(self)

        def _t(self, key, **kwargs):
            if key == "common.default":
                return "(Default)"
            if key == "error.opencodeEmptyResponse":
                return "empty:{provider}/{model}/{variant}".format(**kwargs)
            if key == "error.opencodeProviderRuntimeError":
                return "provider:{provider}/{model}/{variant}:{detail}".format(**kwargs)
            return f"translated:{key}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
        ):
            emitted.append((message_type, text, is_error, level))

    class _Sessions:
        def update_active_poll_state(self, *args, **kwargs):
            return None

        def remove_active_poll(self, session_id):
            removed.append(session_id)

    class _Server:
        async def get_recent_session_error(self, session_id, since=None):
            return None

        async def get_provider_api_diagnostic(self, provider_id, model_id):
            diagnostics.append((provider_id, model_id))
            return "Provider API returned HTTP 503: No available accounts"

        async def abort_session(self, session_id, directory):
            return None

        async def list_messages(self, session_id, directory):
            return [
                {
                    "info": {
                        "id": "msg-empty",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "finish": "unknown",
                        "tokens": {
                            "input": 8,
                            "output": 4,
                            "reasoning": 2,
                            "cache": {"read": 1, "write": 0},
                        },
                    },
                    "parts": [{"type": "step-finish", "id": "step-finish"}],
                }
            ]

    server = _Server()

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()
        sessions = _Sessions()
        im_client = type("IM", (), {"formatter": _Formatter()})()

        async def _get_server(self):
            return server

        def _get_formatter(self, context):
            return _Formatter()

        def _to_relative_path(self, path, working_path):
            return path

        def _extract_response_text(self, message):
            return ""

        async def emit_result_message(self, context, text, **kwargs):
            results.append((text, kwargs))

        async def _remove_ack_reaction(self, request):
            return None

    poll = ActivePollInfo(
        opencode_session_id="oc-session",
        base_session_id="base",
        channel_id="c",
        thread_id="t",
        settings_key="c",
        working_path="/tmp/work",
        baseline_message_ids=[],
        platform="slack",
        model_dict={"providerID": "glm", "modelID": "glm-5.2"},
        reasoning_effort="high",
        prompt_started_at=time.time(),
    )

    loop = OpenCodePollLoop(_Agent())
    terminal = asyncio.run(loop.run_restored_poll_loop(poll))

    assert diagnostics == []
    assert terminal is True
    assert removed == []
    assert emitted == [
        (
            "notify",
            "Resuming interrupted OpenCode session after restart...",
            False,
            "normal",
        ),
    ]
    assert len(results) == 1
    assert results[0][0] == "(No response from OpenCode)"
    assert results[0][1]["subtype"] == "warning"
    assert isinstance(results[0][1]["started_at"], float)


def test_opencode_restored_poll_settles_after_consecutive_transport_failures(
    monkeypatch,
):
    """A restored poll with a dead runtime settles by failure count too."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_FAILURE_SETTLE_LIMIT", 3
    )

    emitted = []
    removed = []
    aborted = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def __init__(self):
            self.config = type(
                "Config", (), {"platform": "slack", "ack_mode": "reaction", "language": "en"}
            )()
            self.processing_indicator = ProcessingIndicatorService(self)

        def _t(self, key, **kwargs):
            return f"{key}:{kwargs.get('count')}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Sessions:
        def remove_active_poll(self, session_id):
            removed.append(session_id)

    class _Server:
        async def list_messages(self, session_id, directory):
            raise ConnectionError("daemon down")

        async def abort_session(self, session_id, directory):
            aborted.append((session_id, directory))
            return True

    server = _Server()

    class _Agent:
        opencode_config = type(
            "OpenCodeConfig",
            (),
            {"error_retry_limit": 0, "active_turn_timeout_seconds": 0},
        )()
        controller = _Controller()
        sessions = _Sessions()

        async def _get_server(self):
            return server

        async def _remove_ack_reaction(self, request):
            return None

        async def record_model_hub_native_failure(self, context, diagnostic):
            return False

    poll = ActivePollInfo(
        opencode_session_id="oc-restored-transport-dead",
        base_session_id="base",
        channel_id="c",
        thread_id="t",
        settings_key="c",
        working_path="/tmp/work",
        baseline_message_ids=[],
        platform="slack",
        prompt_started_at=time.time(),
    )

    terminal = asyncio.run(OpenCodePollLoop(_Agent()).run_restored_poll_loop(poll))

    assert aborted == [("oc-restored-transport-dead", "/tmp/work")]
    assert terminal is True
    assert removed == []
    assert any(
        item[0] == "notify" and item[1] == "error.opencodePollTransportFailure:3"
        for item in emitted
    )
    assert any(item[0] == "result" and item[2] is True for item in emitted)
    assert all("No response from OpenCode" not in (item[1] or "") for item in emitted)


def test_opencode_restored_poll_consumes_original_timeout_budget():
    emitted = []
    removed = []
    aborted = []
    list_calls = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def __init__(self):
            self.config = type(
                "Config", (), {"platform": "slack", "ack_mode": "reaction", "language": "en"}
            )()
            self.processing_indicator = ProcessingIndicatorService(self)

        def _t(self, key, **kwargs):
            return f"{key}:{kwargs.get('seconds')}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Sessions:
        def remove_active_poll(self, session_id):
            removed.append(session_id)

    class _Server:
        async def list_messages(self, session_id, directory):
            list_calls.append((session_id, directory))
            return []

        async def abort_session(self, session_id, directory):
            aborted.append((session_id, directory))
            return True

    server = _Server()

    class _Agent:
        opencode_config = type(
            "OpenCodeConfig",
            (),
            {"error_retry_limit": 0, "active_turn_timeout_seconds": 0.05},
        )()
        controller = _Controller()
        sessions = _Sessions()

        async def _get_server(self):
            return server

        async def _remove_ack_reaction(self, request):
            return None

        async def record_model_hub_native_failure(self, context, diagnostic):
            return False

    poll = ActivePollInfo(
        opencode_session_id="oc-restored-timeout",
        base_session_id="base",
        channel_id="c",
        thread_id="t",
        settings_key="c",
        working_path="/tmp/work",
        baseline_message_ids=[],
        platform="slack",
        prompt_started_at=time.time() - 1,
    )

    terminal = asyncio.run(OpenCodePollLoop(_Agent()).run_restored_poll_loop(poll))

    assert list_calls == []
    assert aborted == [("oc-restored-timeout", "/tmp/work")]
    assert terminal is True
    assert removed == []
    assert [item[0] for item in emitted] == ["notify", "notify", "result"]
    assert emitted[1][1] == "error.opencodeActiveTurnTimeout:0.05"
    assert emitted[2][1:] == (
        "",
        True,
        "silent",
        "OpenCode active turn exceeded the configured 0.05-second wall-clock limit",
    )
    assert all("No response from OpenCode" not in item[1] for item in emitted)


def test_opencode_active_turn_poll_propagates_cancellation_without_settlement():
    emitted = []
    aborted = []
    poll_started = asyncio.Event()

    class _Controller:
        async def emit_agent_message(self, *args, **kwargs):
            emitted.append((args, kwargs))

    class _Server:
        async def list_messages(self, session_id, directory):
            poll_started.set()
            await asyncio.Event().wait()

        async def abort_session(self, session_id, directory):
            aborted.append((session_id, directory))
            return True

    class _Agent:
        opencode_config = type(
            "OpenCodeConfig",
            (),
            {"error_retry_limit": 0, "active_turn_timeout_seconds": 60},
        )()
        controller = _Controller()

        @staticmethod
        def _extract_response_text(message):
            return ""

        async def record_model_hub_native_failure(self, context, diagnostic):
            return False

    async def _run():
        request = AgentRequest(
            context=MessageContext(
                user_id="user",
                channel_id="channel",
                platform="slack",
            ),
            message="stop me",
            user_message="stop me",
            working_path="/tmp/work",
            base_session_id="base",
            composite_session_id="composite",
            session_key="slack::channel",
        )
        task = asyncio.create_task(
            OpenCodePollLoop(_Agent()).run_prompt_poll(
                request,
                _Server(),
                "oc-cancelled",
                agent_to_use=None,
                model_dict=None,
                reasoning_effort=None,
                baseline_message_ids=set(),
            )
        )
        await asyncio.wait_for(poll_started.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    assert aborted == []
    assert emitted == []


def test_opencode_active_turn_timeout_reads_disabled_semantics():
    """Every unset, non-positive, or non-finite shape reads as disabled.

    The seed set is every value shape the config can carry once the shipped
    default is disabled: the dataclass default (missing attribute), an
    explicit opt-out, a negative number, None, NaN, infinity, and an
    unparseable string. None of them may silently re-enable the historical
    wall-clock cap; only a positive opt-in survives as itself.
    """

    def _config_with(value) -> type:
        attrs = {"error_retry_limit": 0}
        if value is not Ellipsis:
            attrs["active_turn_timeout_seconds"] = value
        return type("OpenCodeConfig", (), attrs)()

    for raw_value in (Ellipsis, 0, -5, None, float("nan"), float("inf"), "garbage"):
        loop = OpenCodePollLoop(type("A", (), {"opencode_config": _config_with(raw_value)})())
        assert loop._active_turn_timeout_seconds() == 0.0, raw_value

    loop = OpenCodePollLoop(type("A", (), {"opencode_config": _config_with(90 * 60)})())
    assert loop._active_turn_timeout_seconds() == 5400.0

    assert (
        OpenCodePollLoop._deadline_from_persisted_start(0.0, time.time())
        == float("inf")
    )
    assert OpenCodePollLoop._wait_timeout(float("inf")) is None
    assert OpenCodePollLoop._wait_timeout(1.5) == 1.5


def test_opencode_prompt_poll_has_no_wall_clock_deadline_when_disabled(monkeypatch):
    """With the cap disabled the poll loop only stops on a terminal message."""

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )

    aborted = []
    poll_count = {"n": 0}

    class _Controller:
        def _t(self, key, **kwargs):
            return f"{key}:{kwargs}"

        async def emit_agent_message(self, *args, **kwargs):
            raise AssertionError("no emission expected on a clean terminal path")

    class _Server:
        async def list_messages(self, session_id, directory):
            poll_count["n"] += 1
            if poll_count["n"] < 3:
                return []
            return [
                {
                    "info": {
                        "id": "msg-final",
                        "role": "assistant",
                        "time": {"completed": True},
                        "finish": "stop",
                    },
                    "parts": [{"type": "text", "text": "done"}],
                }
            ]

        async def abort_session(self, session_id, directory):
            aborted.append((session_id, directory))
            return True

    class _Agent:
        opencode_config = type(
            "OpenCodeConfig",
            (),
            {"error_retry_limit": 0, "active_turn_timeout_seconds": 0},
        )()
        controller = _Controller()

        @staticmethod
        def _extract_response_text(message):
            return "done"

        async def record_model_hub_native_failure(self, context, diagnostic):
            return False

    async def _run():
        request = AgentRequest(
            context=MessageContext(
                user_id="user",
                channel_id="channel",
                platform="slack",
            ),
            message="work",
            user_message="work",
            working_path="/tmp/work",
            base_session_id="base",
            composite_session_id="composite",
            session_key="slack::channel",
        )
        return await OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-no-deadline",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )

    final_text, should_emit = asyncio.run(_run())

    assert final_text == "done"
    assert should_emit is True
    assert poll_count["n"] == 3
    assert aborted == []


def test_opencode_prompt_poll_settles_after_consecutive_transport_failures(
    monkeypatch,
):
    """A dead runtime settles the turn by failure count, never by duration.

    With the wall-clock cap disabled, persistent polling errors are the only
    remaining bound: after the configured number of consecutive failures the
    poll owner aborts the native session and emits one failed terminal result.
    The count is consecutive — the reset-on-recovery case is covered by its
    own test — so an intermittent blip never trips this bound.
    """

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_FAILURE_SETTLE_LIMIT", 3
    )

    emitted = []
    aborted = []
    attempts = {"n": 0}

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key, **kwargs):
            return f"{key}:{kwargs.get('count')}"

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Server:
        async def list_messages(self, session_id, directory):
            attempts["n"] += 1
            raise ConnectionError("daemon down")

        async def abort_session(self, session_id, directory):
            aborted.append((session_id, directory))
            return True

    class _Agent:
        opencode_config = type(
            "OpenCodeConfig",
            (),
            {"error_retry_limit": 0, "active_turn_timeout_seconds": 0},
        )()
        controller = _Controller()

        @staticmethod
        def _extract_response_text(message):
            return ""

        async def record_model_hub_native_failure(self, context, diagnostic):
            return False

    async def _run():
        request = AgentRequest(
            context=MessageContext(
                user_id="user",
                channel_id="channel",
                platform="slack",
            ),
            message="work",
            user_message="work",
            working_path="/tmp/work",
            base_session_id="base",
            composite_session_id="composite",
            session_key="slack::channel",
        )
        return await OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-transport-dead",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )

    final_text, should_emit = asyncio.run(_run())

    assert (final_text, should_emit) == (None, False)
    assert attempts["n"] == 3
    assert aborted == [("oc-transport-dead", "/tmp/work")]
    assert any(
        item[0] == "notify" and item[1] == "error.opencodePollTransportFailure:3"
        for item in emitted
    )
    assert any(item[0] == "result" and item[2] is True for item in emitted)
    assert all("No response from OpenCode" not in (item[1] or "") for item in emitted)


def test_opencode_prompt_poll_transport_failure_counter_resets_on_recovery(
    monkeypatch,
):
    """Failures only count while they are consecutive.

    Two poll errors followed by a successful poll and a terminal message must
    complete normally: the counter reset on the successful poll proves the
    bound is on the outage, not on the turn's total history.
    """

    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "modules.agents.opencode.poll_loop._POLL_FAILURE_SETTLE_LIMIT", 3
    )

    aborted = []
    emitted = []
    attempts = {"n": 0}

    class _Controller:
        def _t(self, key, **kwargs):
            return f"{key}:{kwargs}"

        async def emit_agent_message(self, *args, **kwargs):
            emitted.append((args, kwargs))

    class _Server:
        async def list_messages(self, session_id, directory):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise ConnectionError("brief outage")
            if attempts["n"] == 3:
                return []
            return [
                {
                    "info": {
                        "id": "msg-final",
                        "role": "assistant",
                        "time": {"completed": True},
                        "finish": "stop",
                    },
                    "parts": [{"type": "text", "text": "done"}],
                }
            ]

        async def abort_session(self, session_id, directory):
            aborted.append((session_id, directory))
            return True

    class _Agent:
        opencode_config = type(
            "OpenCodeConfig",
            (),
            {"error_retry_limit": 0, "active_turn_timeout_seconds": 0},
        )()
        controller = _Controller()

        @staticmethod
        def _extract_response_text(message):
            return "done"

        async def record_model_hub_native_failure(self, context, diagnostic):
            return False

    async def _run():
        request = AgentRequest(
            context=MessageContext(
                user_id="user",
                channel_id="channel",
                platform="slack",
            ),
            message="work",
            user_message="work",
            working_path="/tmp/work",
            base_session_id="base",
            composite_session_id="composite",
            session_key="slack::channel",
        )
        return await OpenCodePollLoop(_Agent()).run_prompt_poll(
            request,
            _Server(),
            "oc-transport-recovered",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        )

    final_text, should_emit = asyncio.run(_run())

    assert final_text == "done"
    assert should_emit is True
    assert aborted == []
    assert not any(item[0][1] == "result" and item[0][2] is True for item in emitted)


def test_mh_chan_001_opencode_restored_poll_records_source_failure():
    emitted = []
    removed = []
    model_hub_failures = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(
            self, context, backend, message, *, output=None, terminal_error=None
        ):
            return False

    class _Controller:
        agent_auth_service = _AuthSvc()

        def __init__(self):
            self.config = type(
                "Config", (), {"platform": "slack", "ack_mode": "reaction", "language": "zh"}
            )()
            self.processing_indicator = ProcessingIndicatorService(self)

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            parse_mode=None,
            *,
            is_error=False,
            level="normal",
            output=None,
            terminal_error=None,
        ):
            emitted.append((message_type, text, is_error, level, terminal_error))

    class _Sessions:
        def remove_active_poll(self, session_id):
            removed.append(session_id)

        def update_active_poll_state(self, *args, **kwargs):
            return None

    class _Server:
        async def list_messages(self, session_id, directory):
            return [
                {
                    "info": {
                        "id": "msg-restored-error",
                        "role": "assistant",
                        "time": {"completed": 1},
                        "error": {
                            "name": "NativeSessionEndedBeforeResult",
                            "data": {"message": "OpenCode 已结束，但没有产出模型回复。"},
                        },
                    },
                    "parts": [],
                }
            ]

    server = _Server()

    class _Agent:
        opencode_config = type("OpenCodeConfig", (), {"error_retry_limit": 0})()
        controller = _Controller()
        sessions = _Sessions()

        async def _get_server(self):
            return server

        def _extract_response_text(self, message):
            return ""

        def _to_relative_path(self, path, working_path):
            return path

        async def _remove_ack_reaction(self, request):
            return None

        async def record_model_hub_native_failure(self, context, diagnostic):
            launch = launch_for_context(context)
            model_hub_failures.append(
                (launch.channel if launch else None, launch.source_id if launch else None, diagnostic)
            )
            return True

    poll = ActivePollInfo(
        opencode_session_id="oc-restored-error",
        base_session_id="base",
        channel_id="c",
        thread_id="t",
        settings_key="c",
        working_path="/tmp/work",
        baseline_message_ids=[],
        platform="slack",
        processing_indicator={
            "platform": "slack",
            "user_id": "u",
            "channel_id": "c",
            "model_hub_launch": {
                "backend": "opencode",
                "channel": "hub",
                "source_id": "src_hub_restore",
                "target_model": "gpt-5",
            },
        },
    )

    terminal = asyncio.run(OpenCodePollLoop(_Agent()).run_restored_poll_loop(poll))

    assert model_hub_failures == [
        (
            "hub",
            "src_hub_restore",
            "NativeSessionEndedBeforeResult - OpenCode 已结束，但没有产出模型回复。",
        )
    ]
    assert terminal is True
    assert removed == []
    assert [item[0] for item in emitted] == ["notify", "notify", "result"]
    assert emitted[1][1] == (
        "OpenCode 错误：NativeSessionEndedBeforeResult - "
        "OpenCode 已结束，但没有产出模型回复。"
    )


def test_processing_indicator_handle_is_source_of_truth_for_backend_cleanup():
    wechat = _StubClient("wechat")

    class _Controller:
        def __init__(self):
            self.config = type("Config", (), {"platform": "wechat", "ack_mode": "typing", "language": "en"})()
            self.im_client = wechat
            self.settings_manager = type("Settings", (), {})()
            self.processing_indicator = ProcessingIndicatorService(self)

        def get_im_client_for_context(self, context):
            return wechat

    controller = _Controller()
    handle = controller.processing_indicator.handle_from_snapshot(
        {
            "platform": "wechat",
            "user_id": "user-1",
            "channel_id": "chan-1",
            "context_token": "ctx-1",
            "typing_indicator_active": True,
        }
    )
    request = type(
        "Request",
        (),
        {
            "context": handle.context,
            "ack_message_id": None,
            "ack_reaction_message_id": None,
            "ack_reaction_emoji": None,
            "typing_indicator_active": False,
            "typing_indicator_task": None,
            "processing_indicator": handle,
        },
    )()

    asyncio.run(controller.processing_indicator.finish(request))

    assert request.typing_indicator_active is False
    assert handle.typing_indicator_active is False
    assert wechat.sent == [("clear_typing", "wechat", "user-1", "ctx-1")]


def test_processing_indicator_clear_policy_comes_from_platform_registry():
    slack = _StubClient("slack")

    class _Controller:
        def __init__(self):
            self.config = type("Config", (), {"platform": "slack", "ack_mode": "typing", "language": "en"})()
            self.im_client = slack

        def get_im_client_for_context(self, context):
            return slack

    controller = _Controller()
    service = ProcessingIndicatorService(controller)
    handle = service.handle_from_snapshot(
        {
            "platform": "slack",
            "user_id": "user-1",
            "channel_id": "chan-1",
            "typing_indicator_active": True,
        }
    )

    asyncio.run(service.finish(handle))

    assert handle.typing_indicator_active is False
    assert slack.sent == []


def test_processing_indicator_message_delete_policy_comes_from_platform_registry():
    telegram = _StubClient("telegram")

    class _Controller:
        def __init__(self):
            self.config = type("Config", (), {"platform": "telegram", "ack_mode": "message", "language": "en"})()
            self.im_client = telegram

        def get_im_client_for_context(self, context):
            return telegram

    service = ProcessingIndicatorService(_Controller())
    handle = service.handle_from_snapshot(
        {
            "platform": "telegram",
            "user_id": "user-1",
            "channel_id": "chat-1",
            "ack_message_id": "ack-1",
            "ack_message_channel_id": "chat-1",
        }
    )
    request = type("Request", (), {"context": handle.context, "ack_message_id": "ack-1", "processing_indicator": handle})()

    asyncio.run(service.delete_ack_message(request))

    assert request.ack_message_id is None
    assert handle.ack_message_id is None
    assert telegram.sent == [("delete", "telegram", "chat-1", "ack-1")]


class _TerminalCleanupSettings:
    def _canonicalize_message_type(self, message_type):
        return message_type

    def is_message_type_hidden(self, settings_key, canonical_type):
        return False


class _TerminalCleanupController:
    def __init__(self, platform: str, client: _StubClient):
        self.config = type(
            "Config",
            (),
            {"platform": platform, "ack_mode": "typing", "language": "en", "reply_enhancements": False},
        )()
        self.im_client = client
        self.session_handler = type("SessionHandler", (), {"finalize_scheduled_delivery": lambda *args: None})()
        self.processing_indicator = ProcessingIndicatorService(self)
        self.agent_service = AgentService(self)

    def get_im_client_for_context(self, context):
        return self.im_client

    def get_settings_manager_for_context(self, context):
        return _TerminalCleanupSettings()

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"{context.platform}::{context.channel_id}"


async def _run_terminal_result_cleanup(platform: str, *, platform_specific=None):
    client = _StubClient(platform)
    controller = _TerminalCleanupController(platform, client)
    dispatcher = ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="user-1",
        channel_id="chan-1",
        platform=platform,
        platform_specific=platform_specific,
    )
    handle = await controller.processing_indicator.start(context, "claude")
    request = AgentRequest(
        context=context,
        message="hello",
        user_message="hello",
        working_path="/tmp",
        base_session_id="base",
        composite_session_id="base:/tmp",
        session_key=f"{platform}::chan-1",
        processing_indicator=handle,
    )
    controller.processing_indicator.apply_to_request(request, handle)
    controller.agent_service._stamp_runtime_turn(request, "base:/tmp", "turn-1")
    gate = controller.agent_service._get_turn_gate("base:/tmp")
    gate.token = "turn-1"
    gate.backend = "claude"
    controller.processing_indicator.track_turn(context, request)

    await dispatcher.emit_agent_message(context, "result", "done")

    assert request.typing_indicator_active is False
    assert request.typing_indicator_task is None
    assert handle.typing_indicator_active is False
    return client


def test_terminal_result_finishes_registered_telegram_typing_turn():
    client = asyncio.run(_run_terminal_result_cleanup("telegram"))

    assert client.sent == [("typing", "telegram", "user-1"), ("telegram", "chan-1", "done")]


def test_terminal_result_finishes_registered_wechat_typing_turn():
    client = asyncio.run(_run_terminal_result_cleanup("wechat", platform_specific={"context_token": "ctx-1"}))

    assert ("clear_typing", "wechat", "user-1", "ctx-1") in client.sent


def test_multi_im_client_routes_download_by_file_info_platform():
    slack = _StubClient("slack")
    wechat = _StubClient("wechat")
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")

    asyncio.run(client.download_file_to_path({"platform": "wechat", "name": "a.jpg"}, "/tmp/a.jpg"))

    assert slack.sent == []
    assert wechat.sent == [("download_to_path", "wechat", "/tmp/a.jpg")]


def test_multi_im_client_routes_remove_inline_keyboard_by_context_platform():
    slack = _StubClient("slack")
    lark = _StubClient("lark")
    client = MultiIMClient({"slack": slack, "lark": lark}, primary_platform="slack")

    asyncio.run(
        client.remove_inline_keyboard(
            MessageContext(user_id="u", channel_id="c", platform="lark"),
            "om_123",
        )
    )

    assert slack.removed == []
    assert lark.removed == [("lark", "om_123", None)]


def test_multi_im_client_routes_dismiss_form_message_by_context_platform():
    slack = _StubClient("slack")
    lark = _StubClient("lark")
    client = MultiIMClient({"slack": slack, "lark": lark}, primary_platform="slack")

    asyncio.run(
        client.dismiss_form_message(
            MessageContext(user_id="u", channel_id="c", platform="lark", message_id="om_456")
        )
    )

    assert slack.dismissed == []
    assert lark.dismissed == [("lark", "om_456")]


def test_multi_im_client_transport_ready_callbacks_do_not_repeat_runtime_ready():
    slack = _StubClient("slack")
    wechat = _StubClient("wechat")
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")

    ready_calls: list[bool] = []
    transport_ready_calls: list[str] = []

    async def _on_ready():
        ready_calls.append(True)

    async def _on_transport_ready(*, platform: str):
        transport_ready_calls.append(platform)

    client.register_callbacks(
        on_message=None,
        on_ready=_on_ready,
        on_transport_ready=_on_transport_ready,
    )

    slack_on_ready = slack.on_ready_callback
    assert slack_on_ready is not None
    asyncio.run(slack_on_ready())
    assert ready_calls == []
    assert transport_ready_calls == ["slack"]

    wechat_on_ready = wechat.on_ready_callback
    assert wechat_on_ready is not None
    asyncio.run(wechat_on_ready())
    assert ready_calls == []
    assert transport_ready_calls == ["slack", "wechat"]
    assert client._ready_platforms == {"slack", "wechat"}

    asyncio.run(wechat_on_ready())
    assert transport_ready_calls == ["slack", "wechat"]

    client._emit_runtime_ready_once()
    assert ready_calls == [True]
