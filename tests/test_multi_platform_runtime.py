from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im.base import BaseIMClient, BaseIMConfig, MessageContext
from modules.im.multi import IMClientRemovalError, MultiIMClient
from modules.settings_manager import MultiSettingsManager
from config.v2_sessions import ActivePollInfo
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.processing_indicator import ProcessingIndicatorService
from modules.agents.base import AgentRequest
from modules.agents.service import AgentService
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.poll_loop import OpenCodePollLoop
from modules.agents.opencode.utils import resolve_opencode_reasoning_effort


@dataclass
class _StubConfig(BaseIMConfig):
    def validate(self) -> None:
        return None


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


def test_multi_im_client_remove_pending_platform_completes_ready():
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

    removed = client.remove_client("discord")

    assert removed is discord
    assert ready_calls == [True]


def test_multi_im_client_remove_last_pending_platform_completes_empty_ready():
    slack = _StubClient("slack")
    client = MultiIMClient({"slack": slack}, primary_platform="slack")
    ready_calls: list[bool] = []

    async def on_ready():
        ready_calls.append(True)

    client.register_callbacks(on_ready=on_ready)

    removed = client.remove_client("slack")

    assert removed is slack
    assert ready_calls == [True]


def test_multi_im_client_run_returns_when_all_enabled_threads_exit():
    client = MultiIMClient({"slack": _StubClient("slack")}, primary_platform="slack")
    returned: list[bool] = []

    def _run() -> None:
        client.run()
        returned.append(True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert returned == [True]


def test_multi_im_client_run_raises_single_platform_runtime_crash():
    boom = RuntimeError("slack failed")
    client = MultiIMClient({"slack": _CrashingClient("slack", boom)}, primary_platform="slack")

    try:
        client.run()
    except RuntimeError as exc:
        assert exc is boom
    else:
        raise AssertionError("MultiIMClient.run should raise the platform runtime crash")


def test_multi_im_client_run_raises_multi_platform_runtime_crash_when_all_exit():
    boom = RuntimeError("discord failed")
    client = MultiIMClient(
        {"slack": _StubClient("slack"), "discord": _CrashingClient("discord", boom)},
        primary_platform="slack",
    )

    try:
        client.run()
    except RuntimeError as exc:
        assert exc is boom
    else:
        raise AssertionError("MultiIMClient.run should raise the captured platform runtime crash")


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


def test_opencode_prompt_disables_question_tool_for_all_platforms():
    calls = []

    class _Server:
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
            return "oc-session"

        def set_request_session(self, *args):
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
                    "platform": "slack",
                    "reply_enhancements": True,
                    "show_pages_prompt": True,
                    "remote_access": None,
                    "language": "en",
                    "opencode": type(
                            "OpenCodeConfig",
                            (),
                            {
                                "default_model": "GPT-5.4",
                                "default_provider": "openai",
                                "default_reasoning_effort": "high",
                            },
                    )(),
                },
            )()
            self.im_client = _StubClient("slack")
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
                platform_specific={"agent_session_id": "ses_test"},
            ),
            message="hello",
            working_path="/tmp/work",
            base_session_id="base",
            composite_session_id="base:/tmp/work",
            session_key="slack::c",
        )
        await agent._process_message(request)

    asyncio.run(_run())

    assert calls
    assert calls[0]["tools"] == {"question": False}
    assert calls[0]["model"] == {"providerID": "openai", "modelID": "gpt-5.4"}
    assert calls[0]["reasoning_effort"] == "high"


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
    assert "use `ses-target` for Show Pages" in system


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

    class _Server:
        async def ensure_running(self):
            return None

        async def list_messages(self, session_id, directory):
            return []

        async def prompt_async(self, **kwargs):
            return None

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
            return "oc-session"

        def set_request_session(self, *args):
            return None

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        def add_active_poll(self, **kwargs):
            return None

        def remove_active_poll(self, session_id):
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
    agent._get_server = _get_server
    agent._delete_ack = lambda request: _async_noop()
    agent._remove_ack_reaction = _remove_ack

    request = AgentRequest(
        context=MessageContext(
            user_id="u",
            channel_id="c",
            platform="slack",
            platform_specific={"agent_session_id": "ses_test"},
        ),
        message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    asyncio.run(agent._process_message(request))

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

        async def emit_agent_message(self, context, message_type, text, parse_mode=None, *, is_error=False, level="normal"):
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


def test_opencode_poll_emits_error_result_on_retry_exhaustion():
    # A completed assistant message carrying an error, with retries exhausted
    # (error_retry_limit=0) and the auth-recovery path declining (non-auth error),
    # is a terminal FAILURE. It must (a) emit an ERROR result so the dot turns red
    # and (b) return should_emit=False so the caller does NOT then emit the idle
    # "(No response from OpenCode)" warning that would reset the dot to idle (Codex P2).
    emitted = []

    class _AuthSvc:
        async def maybe_emit_auth_recovery_message(self, context, backend, message):
            return False  # non-auth error → caller emits the terminal result itself

    class _Formatter:
        def format_toolcall(self, *args, **kwargs):
            return "tool"

    class _Controller:
        agent_auth_service = _AuthSvc()

        def _t(self, key):
            return f"translated:{key}"

        async def emit_agent_message(self, context, message_type, text, parse_mode=None, *, is_error=False, level="normal"):
            emitted.append((message_type, is_error))

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
    assert ("result", True) in emitted
    assert not any(mtype == "notify" for mtype, _ in emitted)


def test_opencode_poll_emits_notify_and_silent_error_result_on_empty_terminal_message():
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

        async def emit_agent_message(self, context, message_type, text, parse_mode=None, *, is_error=False, level="normal"):
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
    assert should_emit is False
    assert emitted == [
        (
            "notify",
            "provider:glm/glm-5.2/(Default):AI_APICallError (ECONNRESET) while calling https://relay.example/messages",
            False,
            "normal",
        ),
        (
            "result",
            "provider:glm/glm-5.2/(Default):AI_APICallError (ECONNRESET) while calling https://relay.example/messages",
            True,
            "silent",
        ),
    ]


def test_opencode_restored_poll_preserves_model_details_for_empty_terminal_probe():
    emitted = []
    removed = []
    diagnostics = []

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

        async def emit_agent_message(self, context, message_type, text, parse_mode=None, *, is_error=False, level="normal"):
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

        async def emit_result_message(self, *args, **kwargs):
            raise AssertionError("empty terminal path should emit failure directly")

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
        prompt_started_at=42.0,
    )

    loop = OpenCodePollLoop(_Agent())
    asyncio.run(loop.run_restored_poll_loop(poll))

    assert diagnostics == [("glm", "glm-5.2")]
    assert removed == ["oc-session"]
    assert emitted == [
        (
            "notify",
            "Resuming interrupted OpenCode session after restart...",
            False,
            "normal",
        ),
        (
            "notify",
            "provider:glm/glm-5.2/high:Provider API returned HTTP 503: No available accounts",
            False,
            "normal",
        ),
        (
            "result",
            "provider:glm/glm-5.2/high:Provider API returned HTTP 503: No available accounts",
            True,
            "silent",
        ),
    ]


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


def test_multi_im_client_on_ready_fires_only_after_all_platforms():
    """on_ready callback must wait for all platform clients to be ready."""
    slack = _StubClient("slack")
    wechat = _StubClient("wechat")
    client = MultiIMClient({"slack": slack, "wechat": wechat}, primary_platform="slack")

    ready_calls: list[bool] = []

    async def _on_ready():
        ready_calls.append(True)

    client.register_callbacks(on_message=None, on_ready=_on_ready)

    # Simulate only Slack becoming ready — on_ready should NOT fire yet
    slack_on_ready = slack.on_ready_callback
    assert slack_on_ready is not None
    asyncio.run(slack_on_ready())
    assert ready_calls == [], "on_ready fired before all platforms were ready"

    # Now simulate WeChat becoming ready — on_ready should fire exactly once
    wechat_on_ready = wechat.on_ready_callback
    assert wechat_on_ready is not None
    asyncio.run(wechat_on_ready())
    assert ready_calls == [True], "on_ready should fire exactly once after all platforms are ready"

    # Calling again should not fire a second time
    asyncio.run(wechat_on_ready())
    assert len(ready_calls) == 1, "on_ready should not fire more than once"
