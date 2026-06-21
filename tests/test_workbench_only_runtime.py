"""Workbench-only runtime: zero external IM platforms.

A workbench-only install enables no Slack/Discord/Telegram/Lark/WeChat
platform. The Avibe Workbench (in-process Web UI) is the sole inbound surface.
These tests cover the two delicate seams that make that work end-to-end:

1. Controller ``_init_modules`` must always use ``MultiIMClient`` for the IM
   runtime, even when no external IM platforms are enabled. In workbench-only
   mode that wrapper has zero runtime clients and stays alive via its idle loop.
2. ``AvibeBot`` remains registered separately as the in-process delivery client
   for Web UI replies, with a settings manager for "avibe".

The has-IM path is exercised by the broader multi-platform suite; here we only
assert the workbench-only behavior that previously raised.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller
from modules.im.avibe import AvibeBot, AvibeConfig
from modules.im.base import BaseIMClient, BaseIMConfig, MessageContext
from modules.im.multi import MultiIMClient


@dataclass
class _BootConfig(BaseIMConfig):
    def validate(self) -> None:
        return None


class _BootClient(BaseIMClient):
    def __init__(self, platform: str):
        super().__init__(_BootConfig())
        self.platform = platform
        self.settings_manager = None
        self.controller = None

    def set_settings_manager(self, settings_manager):
        self.settings_manager = settings_manager

    def set_controller(self, controller):
        self.controller = controller

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        return self.platform

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        return self.platform

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None):
        return True

    async def answer_callback(self, callback_id, text=None, show_alert=False):
        return True

    def register_handlers(self):
        return None

    def run(self):
        return None

    async def get_user_info(self, user_id: str):
        return {"id": user_id}

    async def get_channel_info(self, channel_id: str):
        return {"id": channel_id}

    def format_markdown(self, text: str) -> str:
        return text


def _workbench_only_app_config():
    payload = {
        "platform": "slack",
        "platforms": {"enabled": [], "primary": "slack"},
        "mode": "self_host",
        "version": "v2",
        "runtime": {"default_cwd": "_tmp", "log_level": "INFO"},
        "agents": {
            "default_backend": "opencode",
            "opencode": {"enabled": True, "cli_path": "opencode"},
        },
        "setup_completed": True,
    }
    return to_app_config(V2Config.from_payload(payload))


def _boot_app_config(enabled: list[str]):
    primary = enabled[0] if enabled else "avibe"
    payload = {
        "platform": primary,
        "platforms": {"enabled": enabled, "primary": primary},
        "mode": "self_host",
        "version": "v2",
        "runtime": {"default_cwd": "_tmp", "log_level": "INFO"},
        "agents": {
            "default_backend": "opencode",
            "opencode": {"enabled": True, "cli_path": "opencode"},
        },
        "setup_completed": True,
        "slack": {"bot_token": "xoxb-test", "app_token": "xapp-test"},
        "discord": {"bot_token": "discord-token"},
        "telegram": {"bot_token": "123456:test-token"},
        "lark": {"app_id": "cli_a", "app_secret": "secret", "domain": "feishu"},
        "wechat": {"bot_token": "wechat-token"},
    }
    return to_app_config(V2Config.from_payload(payload))


def _init_controller_modules(app_config):
    controller = Controller.__new__(Controller)
    controller.config = app_config
    controller.enabled_platforms = list(app_config.enabled_platforms())
    controller.primary_platform = getattr(getattr(app_config, "platforms", None), "primary", app_config.platform)
    controller._init_modules()
    return controller


def test_init_modules_uses_empty_multi_runtime_for_workbench_only():
    app_config = _workbench_only_app_config()
    # The compat view of a workbench-only config exposes no enabled platforms.
    assert app_config.enabled_platforms() == []

    controller = _init_controller_modules(app_config)

    assert list(controller.im_clients.keys()) == ["avibe"]
    assert isinstance(controller.im_clients["avibe"], AvibeBot)
    assert isinstance(controller.im_client, MultiIMClient)
    assert controller.im_client.clients == {}
    assert controller.primary_platform == "avibe"
    # The primary-anchored settings manager resolves (no KeyError on an empty
    # enabled-platform set) for both the no-context and avibe-context cases.
    assert controller.get_settings_manager_for_context(None).platform == "avibe"
    avibe_ctx = MessageContext(user_id="u", channel_id="c", platform="avibe")
    assert controller.get_settings_manager_for_context(avibe_ctx).platform == "avibe"
    # Agent routing has a route for the avibe primary.
    assert "avibe" in controller.agent_router.platform_routes


def test_workbench_only_multi_runtime_routes_avibe_cleanup_to_avibe_client():
    controller = _init_controller_modules(_workbench_only_app_config())
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
    context = MessageContext(user_id="u", channel_id="c", platform="avibe")

    assert controller.im_client.get_client_for_context(context) is avibe
    assert asyncio.run(controller.im_client.delete_message(context, "ack-1")) is True
    assert asyncio.run(controller.im_client.remove_reaction(context, "ack-1", "eyes")) is True
    assert deleted == [("avibe", "ack-1")]
    assert reactions == [("avibe", "ack-1", "eyes")]


def test_init_modules_boots_single_platform_with_multi_runtime(monkeypatch):
    app_config = _boot_app_config(["slack"])
    monkeypatch.setattr("core.controller.IMFactory.create_clients", lambda config: {"slack": _BootClient("slack")})

    controller = _init_controller_modules(app_config)

    assert isinstance(controller.im_client, MultiIMClient)
    assert list(controller.im_client.clients.keys()) == ["slack"]
    assert controller.primary_platform == "slack"
    assert "avibe" in controller.im_clients
    assert "slack" in controller.agent_router.platform_routes


def test_init_modules_boots_four_platforms_with_multi_runtime(monkeypatch):
    enabled = ["slack", "discord", "telegram", "lark"]
    app_config = _boot_app_config(enabled)
    monkeypatch.setattr(
        "core.controller.IMFactory.create_clients",
        lambda config: {platform: _BootClient(platform) for platform in enabled},
    )

    controller = _init_controller_modules(app_config)

    assert isinstance(controller.im_client, MultiIMClient)
    assert list(controller.im_client.clients.keys()) == enabled
    assert controller.primary_platform == "slack"
    assert "avibe" in controller.im_clients
    assert set(enabled) <= set(controller.agent_router.platform_routes)


def test_avibe_run_fires_on_ready_once_then_blocks_until_stop():
    # Stand-in for the controller event loop + its _dispatch_to_controller_loop
    # bridge, so we exercise the real cross-loop hand-off AvibeBot.run relies on.
    controller_loop = asyncio.new_event_loop()
    ready_calls: list[bool] = []

    def _start_loop() -> None:
        asyncio.set_event_loop(controller_loop)
        controller_loop.run_forever()

    loop_thread = threading.Thread(target=_start_loop, daemon=True)
    loop_thread.start()
    try:
        # Wait for the loop to actually be running before scheduling onto it.
        for _ in range(100):
            if controller_loop.is_running():
                break
            time.sleep(0.01)

        async def _on_ready() -> None:
            assert asyncio.get_running_loop() is controller_loop
            ready_calls.append(True)

        def _dispatch_to_controller_loop(callback):
            async def _wrapped(*args, **kwargs):
                try:
                    current = asyncio.get_running_loop()
                except RuntimeError:
                    current = None
                if current is controller_loop:
                    return await callback(*args, **kwargs)
                future = asyncio.run_coroutine_threadsafe(callback(*args, **kwargs), controller_loop)
                return await asyncio.wrap_future(future)

            return _wrapped

        bot = AvibeBot(AvibeConfig())
        bot.register_callbacks(on_message=None, on_ready=_dispatch_to_controller_loop(_on_ready))

        returned: list[bool] = []

        def _run_im() -> None:
            bot.run()
            returned.append(True)

        im_thread = threading.Thread(target=_run_im, name="im-runtime", daemon=True)
        im_thread.start()

        # on_ready fires once and run() blocks (the thread stays alive).
        for _ in range(100):
            if ready_calls:
                break
            time.sleep(0.01)
        assert ready_calls == [True]
        time.sleep(0.05)
        assert im_thread.is_alive() is True
        assert returned == []

        # stop() (called from another thread, like cleanup_sync) releases run().
        bot.stop()
        im_thread.join(timeout=3)
        assert im_thread.is_alive() is False
        assert returned == [True]
        # on_ready did not fire a second time.
        assert ready_calls == [True]
    finally:
        controller_loop.call_soon_threadsafe(controller_loop.stop)
        loop_thread.join(timeout=3)
        controller_loop.close()


def test_avibe_stop_is_safe_before_run():
    # cleanup_sync may call stop() even if run() never started (e.g. early
    # failure). It must not raise.
    bot = AvibeBot(AvibeConfig())
    bot.stop()
