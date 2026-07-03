from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_settings import RoutingSettings, SettingsStore, UserSettings
from core.controller import Controller
from modules.im import MessageContext
from modules.settings_manager import SettingsManager
from vibe import api


def test_get_users_respects_platform_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform(
        "slack",
        {"U1": UserSettings(display_name="Slack Admin", is_admin=True)},
    )
    store.set_users_for_platform(
        "wechat",
        {"wx1": UserSettings(display_name="WeChat Admin", is_admin=True)},
    )
    store.save()

    slack_users = api.get_users("slack")
    wechat_users = api.get_users("wechat")

    assert set(slack_users["users"].keys()) == {"U1"}
    assert set(wechat_users["users"].keys()) == {"wx1"}


def test_wechat_settings_filter_toolcall_visibility(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    settings = api.save_settings(
        {
            "platform": "wechat",
            "channels": {
                "wx-chat": {
                    "enabled": True,
                    "show_message_types": ["assistant", "toolcall"],
                }
            },
        }
    )
    users = api.save_users(
        {
            "platform": "wechat",
            "users": {
                "wx-user": {
                    "display_name": "WeChat User",
                    "enabled": True,
                    "show_message_types": ["assistant", "toolcall"],
                }
            },
        }
    )

    assert settings["channels"]["wx-chat"]["show_message_types"] == ["assistant"]
    assert users["users"]["wx-user"]["show_message_types"] == ["assistant"]


def test_toggle_admin_is_scoped_per_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform(
        "slack",
        {"U1": UserSettings(display_name="Slack User", is_admin=False)},
    )
    store.set_users_for_platform(
        "wechat",
        {"U1": UserSettings(display_name="WeChat User", is_admin=False)},
    )
    store.save()

    result = api.toggle_admin("U1", True, "wechat")

    assert result["ok"] is True
    assert store.get_user("U1", platform="slack").is_admin is False
    assert store.get_user("U1", platform="wechat").is_admin is True


def test_controller_codex_overrides_resolve_dm_user_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform(
        "slack",
        {
            "U1": UserSettings(
                display_name="Alex",
                routing=RoutingSettings(
                    agent_name="codex",
                    model="gpt-5.5",
                    reasoning_effort="xhigh",
                ),
            )
        },
    )
    store.save()

    controller = Controller.__new__(Controller)
    controller.primary_platform = "slack"
    controller.platform_settings_managers = {
        "slack": SettingsManager(platform="slack"),
    }
    context = MessageContext(
        platform="slack",
        user_id="U1",
        channel_id="D1",
        platform_specific={"is_dm": True},
    )

    assert Controller._get_settings_key(controller, context) == "U1"
    assert Controller.get_codex_overrides(controller, context) == (
        None,
        "gpt-5.5",
        "xhigh",
    )
