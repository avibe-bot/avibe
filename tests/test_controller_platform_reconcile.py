from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from config.v2_config import DiscordConfig, LarkConfig, SlackConfig
from core.controller import Controller
from modules.agent_router import AgentRouter
from modules.im.base import BaseIMClient, BaseIMConfig, MessageContext
from modules.im.multi import MultiIMClient
from modules.settings_manager import MultiSettingsManager


@dataclass
class _StubConfig(BaseIMConfig):
    name: str = ""

    def validate(self) -> None:
        return None


class _Formatter:
    def format_error(self, text: str) -> str:
        return text


class _Client(BaseIMClient):
    def __init__(self, name: str, config: BaseIMConfig | None = None):
        super().__init__(config or _StubConfig(name=name))
        self.name = name
        self.stopped = False
        self.settings_manager = None
        self.controller = None

    def set_settings_manager(self, settings_manager):
        self.settings_manager = settings_manager

    def set_controller(self, controller):
        self.controller = controller

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        return self.name

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        return self.name

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None):
        return True

    async def answer_callback(self, callback_id, text=None, show_alert=False):
        return True

    def register_handlers(self):
        return None

    def run(self):
        return None

    def stop(self):
        self.stopped = True

    async def get_user_info(self, user_id: str):
        return {"id": user_id}

    async def get_channel_info(self, channel_id: str):
        return {"id": channel_id}

    def format_markdown(self, text: str) -> str:
        return text


async def _noop(*args, **kwargs):
    return None


def _handler(**overrides):
    methods = {
        "handle_start",
        "handle_new",
        "handle_cwd",
        "handle_set_cwd",
        "handle_resume",
        "handle_setup",
        "handle_stop",
        "handle_bind",
        "handle_change_cwd_submission",
        "handle_settings",
        "handle_settings_update",
        "handle_routing_update",
        "handle_routing_modal_update",
        "handle_callback_query",
        "handle_resume_session_submission",
    }
    values = {name: _noop for name in methods}
    values.update(overrides)
    return SimpleNamespace(**values)


class _Descriptor:
    def __init__(self, platform: str):
        self.id = platform
        self.config_key = platform
        self.credential_fields = ("bot_token",)

    def runtime_reconcile_field_names(self):
        if self.id == "lark":
            return ("app_id", "app_secret", "domain")
        return ("bot_token",)

    def get_config(self, config):
        return getattr(config, self.id)

    def create_client(self, config):
        return _Client(self.id, self.get_config(config))

    def create_formatter(self):
        return _Formatter()


def _config(
    enabled: list[str],
    *,
    primary: str | None = None,
    slack_token: str = "xoxb-old",
    discord_token: str = "discord-old-token",
    lark_secret: str = "lark-old-secret",
):
    primary = primary or (enabled[0] if enabled else "avibe")
    return SimpleNamespace(
        platform=primary,
        platforms=SimpleNamespace(enabled=enabled, primary=primary),
        enabled_platforms=lambda: list(enabled),
        slack=SlackConfig(bot_token=slack_token),
        discord=DiscordConfig(bot_token=discord_token),
        lark=LarkConfig(app_id="cli_a", app_secret=lark_secret, domain="feishu"),
        claude=SimpleNamespace(),
        language="en",
    )


def _controller(config):
    controller = Controller.__new__(Controller)
    controller.config = config
    controller.enabled_platforms = list(config.enabled_platforms())
    controller.primary_platform = Controller._derive_primary_platform(config)
    clients = {platform: _Client(platform, getattr(config, platform)) for platform in controller.enabled_platforms}
    for platform, client in clients.items():
        client.formatter = _Formatter()
    controller.im_clients = dict(clients)
    controller.im_clients["avibe"] = _Client("avibe")
    controller.im_client = MultiIMClient(
        dict(clients),
        controller.primary_platform,
        auxiliary_clients={"avibe": controller.im_clients["avibe"]},
    )
    controller._removed_im_clients = {}
    controller.settings_manager = MultiSettingsManager(
        Controller._settings_platforms_for(controller.enabled_platforms, controller.primary_platform),
        primary_platform=controller.primary_platform,
    )
    controller.platform_settings_managers = controller.settings_manager.managers
    controller.sessions = controller.settings_manager.sessions
    controller.agent_router = AgentRouter.from_file(None, platform=controller.primary_platform, fallback_backend="opencode")
    controller.processing_indicator = SimpleNamespace(config=config)
    controller.audio_asr_service = SimpleNamespace(config=config)
    controller.claude_client = SimpleNamespace(config=config.claude, formatter=None)
    controller.command_handler = _handler()
    controller.settings_handler = _handler()
    controller.message_handler = _handler()
    controller.session_handler = _handler()
    controller.agent_service = SimpleNamespace(agents={})
    controller._reconcile_lock = None
    controller._loop = None
    return controller


def test_reconcile_adds_platform_with_callbacks_before_start(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack"]))
    controller._setup_callbacks()

    result = asyncio.run(controller.reconcile_platforms(_config(["slack", "discord"])))

    assert result["added"] == ["discord"]
    assert "discord" in controller.im_client.clients
    assert "discord" in controller.platform_settings_managers
    assert controller.im_client.clients["discord"].on_message_callback is not None
    assert controller.im_client.clients["discord"].settings_manager.platform == "discord"


def test_reconcile_removes_platform_and_keeps_workbench_delivery(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack", "discord"]))
    removed_client = controller.im_clients["discord"]

    result = asyncio.run(controller.reconcile_platforms(_config(["slack"])))

    assert result["removed"] == ["discord"]
    assert removed_client.stopped is True
    assert "discord" not in controller.im_client.clients
    assert "discord" not in controller.im_clients
    assert "avibe" in controller.im_clients


def test_reconcile_routes_removed_platform_context_to_noop_sink(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack", "discord"]))
    slack = controller.im_clients["slack"]

    asyncio.run(controller.reconcile_platforms(_config(["slack"])))

    stale_context = MessageContext(user_id="u", channel_id="discord-channel", platform="discord")
    removed_client = controller.get_im_client_for_context(stale_context)
    platform_client = controller._get_im_client_for_platform("discord")

    assert removed_client is platform_client
    assert removed_client is not slack
    assert asyncio.run(removed_client.send_message(stale_context, "late result")) is None
    assert asyncio.run(removed_client.send_message_with_buttons(stale_context, "late result", None)) is None
    assert asyncio.run(removed_client.send_typing_indicator(stale_context)) is False
    assert asyncio.run(removed_client.clear_typing_indicator(stale_context)) is False
    assert asyncio.run(removed_client.delete_message(stale_context, "ack-1")) is False


def test_reconcile_routes_to_sink_while_platform_stop_is_in_progress(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack", "discord"]))
    slack = controller.im_clients["slack"]
    discord = controller.im_clients["discord"]
    seen_during_stop: list[BaseIMClient] = []

    def _remove_client(platform):
        assert platform == "discord"
        seen_during_stop.append(
            controller.get_im_client_for_context(MessageContext(user_id="u", channel_id="c", platform=platform))
        )
        return discord

    controller.im_client.remove_client = _remove_client

    result = asyncio.run(controller.reconcile_platforms(_config(["slack"])))

    assert result["removed"] == ["discord"]
    assert seen_during_stop
    assert seen_during_stop[0] is not discord
    assert seen_during_stop[0] is not slack
    assert asyncio.run(seen_during_stop[0].send_message(MessageContext(user_id="u", channel_id="c", platform="discord"), "late")) is None


def test_reconcile_rebuilds_enabled_platform_on_credential_change(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack"]))
    old_client = controller.im_clients["slack"]

    result = asyncio.run(controller.reconcile_platforms(_config(["slack"], slack_token="xoxb-new")))

    assert result["rebuilt"] == ["slack"]
    assert old_client.stopped is True
    assert controller.im_clients["slack"] is not old_client
    assert controller.im_clients["slack"].config.bot_token == "xoxb-new"


def test_reconcile_rebuild_installs_sink_before_new_client(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack"]))
    old_client = controller.im_clients["slack"]
    seen_during_rebuild: list[BaseIMClient] = []

    def _build_platform_client(platform, config):
        seen_during_rebuild.append(
            controller.get_im_client_for_context(MessageContext(user_id="u", channel_id="c", platform=platform))
        )
        return _Client(platform, getattr(config, platform))

    controller._build_platform_client = _build_platform_client

    result = asyncio.run(controller.reconcile_platforms(_config(["slack"], slack_token="xoxb-new")))

    assert result["rebuilt"] == ["slack"]
    assert old_client.stopped is True
    assert len(seen_during_rebuild) == 1
    assert seen_during_rebuild[0] is not old_client
    assert seen_during_rebuild[0] is not controller.im_clients["slack"]
    assert asyncio.run(seen_during_rebuild[0].send_message(MessageContext(user_id="u", channel_id="c", platform="slack"), "late")) is None


def test_reconcile_noop_when_runtime_fields_do_not_change(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack"]))
    original_client = controller.im_clients["slack"]

    result = asyncio.run(controller.reconcile_platforms(_config(["slack"])))

    assert result["added"] == []
    assert result["removed"] == []
    assert result["rebuilt"] == []
    assert controller.im_clients["slack"] is original_client


def test_reconcile_disable_all_keeps_empty_multi_runtime(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack"]))

    result = asyncio.run(controller.reconcile_platforms(_config([])))

    assert result["removed"] == ["slack"]
    assert result["enabled"] == []
    assert result["primary"] == "avibe"
    assert controller.primary_platform == "avibe"
    assert controller.im_client.clients == {}
    assert "avibe" in controller.im_clients
    assert controller.get_im_client_for_context(MessageContext(user_id="u", channel_id="c", platform="avibe")).name == "avibe"


def test_reconcile_disable_all_keeps_multi_runtime_avibe_delivery(monkeypatch):
    monkeypatch.setattr("core.controller.get_platform_descriptor", lambda platform: _Descriptor(platform))
    controller = _controller(_config(["slack"]))
    avibe = controller.im_clients["avibe"]
    deleted: list[tuple[str, str]] = []
    reactions: list[tuple[str, str, str]] = []

    async def _delete_message(context, message_id):
        deleted.append((context.platform, message_id))
        return True

    async def _remove_reaction(context, message_id, emoji):
        reactions.append((context.platform, message_id, emoji))
        return True

    avibe.delete_message = _delete_message
    avibe.remove_reaction = _remove_reaction

    asyncio.run(controller.reconcile_platforms(_config([])))
    context = MessageContext(user_id="u", channel_id="c", platform="avibe")

    assert controller.im_client.get_client_for_context(context) is avibe
    assert asyncio.run(controller.im_client.delete_message(context, "ack-1")) is True
    assert asyncio.run(controller.im_client.remove_reaction(context, "ack-1", "eyes")) is True
    assert deleted == [("avibe", "ack-1")]
    assert reactions == [("avibe", "ack-1", "eyes")]
