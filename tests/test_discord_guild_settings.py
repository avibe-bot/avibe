from __future__ import annotations

import json

import pytest

from config import paths
from config.v2_config import V2Config
from config.v2_settings import GuildSettings, SettingsStore
from modules.settings_manager import SettingsManager
from vibe import api


def _config_payload() -> dict:
    return {
        "platform": "discord",
        "platforms": {"enabled": ["discord"], "primary": "discord"},
        "mode": "self_host",
        "version": "v2",
        "discord": {
            "bot_token": "discord-token-1234567890",
            "require_mention": False,
        },
        "runtime": {"default_cwd": "_tmp", "log_level": "INFO"},
        "agents": {
            "default_backend": "opencode",
            "opencode": {"enabled": True, "cli_path": "opencode"},
            "claude": {"enabled": True, "cli_path": "claude"},
            "codex": {"enabled": True, "cli_path": "codex"},
        },
    }


def test_settings_store_persists_discord_guild_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    store = SettingsStore.get_instance()
    store.set_guilds_for_platform(
        "discord",
        {
            "guild-1": GuildSettings(enabled=True),
            "guild-2": GuildSettings(enabled=False),
        },
    )
    store.save()
    SettingsStore.reset_instance()

    reloaded = SettingsStore.get_instance()

    assert reloaded.has_guild_scope_for_platform("discord") is True
    assert reloaded.is_guild_enabled("discord", "guild-1") is True
    assert reloaded.is_guild_enabled("discord", "guild-2") is False
    assert reloaded.is_guild_enabled("discord", "guild-3") is False


def test_discord_settings_manager_prefers_explicit_guild_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    manager = SettingsManager(platform="discord")
    manager.set_enabled_guild_ids(["guild-1", "guild-2"])

    assert manager.has_guild_scope() is True
    assert manager.is_guild_enabled("guild-1") is True
    assert manager.is_guild_enabled("guild-3") is False


def test_save_config_moves_legacy_discord_allowlist_to_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    saved = api.save_config(
        {
            **_config_payload(),
            "discord": {
                "bot_token": "discord-token-1234567890",
                "guild_allowlist": ["guild-1", "guild-2"],
                "guild_denylist": [],
                "require_mention": False,
            },
        }
    )
    payload = api.config_to_payload(saved)
    settings = api.get_settings("discord")
    saved_config = json.loads(paths.get_config_path().read_text(encoding="utf-8"))

    assert "guild_allowlist" not in payload["discord"]
    assert "guild_allowlist" not in saved_config["discord"]
    assert settings["guild_allowlist"] == ["guild-1", "guild-2"]
    assert settings["guilds"]["guild-1"]["enabled"] is True


def test_save_config_moves_legacy_discord_denylist_to_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    saved = api.save_config(
        {
            **_config_payload(),
            "discord": {
                "bot_token": "discord-token-1234567890",
                "guild_allowlist": [],
                "guild_denylist": ["guild-blocked"],
                "require_mention": False,
            },
        }
    )
    payload = api.config_to_payload(saved)
    settings = api.get_settings("discord")
    store = SettingsStore.get_instance()

    assert "guild_denylist" not in payload["discord"]
    assert settings["guild_scope_configured"] is True
    assert settings["guild_default_enabled"] is True
    assert settings["guilds"]["guild-blocked"]["enabled"] is False
    assert store.is_guild_enabled("discord", "guild-blocked") is False
    assert store.is_guild_enabled("discord", "guild-other") is True


def test_partial_save_config_migrates_existing_legacy_discord_denylist(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    paths.ensure_data_dirs()
    paths.get_config_path().write_text(
        json.dumps(
            {
                **_config_payload(),
                "discord": {
                    "bot_token": "discord-token-1234567890",
                    "guild_allowlist": [],
                    "guild_denylist": ["guild-blocked"],
                    "require_mention": False,
                },
            }
        ),
        encoding="utf-8",
    )

    api.save_config({"show_duration": False})
    store = SettingsStore.get_instance()
    saved_config = json.loads(paths.get_config_path().read_text(encoding="utf-8"))

    assert "guild_denylist" not in saved_config["discord"]
    assert store.has_guild_scope_for_platform("discord") is True
    assert store.get_guild_default_enabled_for_platform("discord") is True
    assert store.is_guild_enabled("discord", "guild-blocked") is False
    assert store.is_guild_enabled("discord", "guild-other") is True


def test_partial_legacy_guild_config_update_preserves_omitted_denylist(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    paths.ensure_data_dirs()
    paths.get_config_path().write_text(
        json.dumps(
            {
                **_config_payload(),
                "discord": {
                    "bot_token": "discord-token-1234567890",
                    "guild_allowlist": ["guild-old"],
                    "guild_denylist": ["guild-blocked"],
                    "require_mention": False,
                },
            }
        ),
        encoding="utf-8",
    )

    api.save_config(
        {
            "discord": {
                "guild_allowlist": ["guild-new"],
            },
        }
    )
    store = SettingsStore.get_instance()

    assert store.get_guild_default_enabled_for_platform("discord") is False
    assert store.is_guild_enabled("discord", "guild-new") is True
    assert store.is_guild_enabled("discord", "guild-blocked") is False
    assert store.is_guild_enabled("discord", "guild-old") is False


def test_direct_config_save_preserves_unmigrated_discord_guild_rules(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    config = V2Config.from_payload(
        {
            **_config_payload(),
            "platform": "slack",
            "platforms": {"enabled": ["slack"], "primary": "slack"},
            "slack": {
                "bot_token": "xoxb-test-token",
                "app_token": "xapp-test-token",
                "require_mention": False,
            },
            "discord": {
                "bot_token": "discord-token-1234567890",
                "guild_allowlist": [],
                "guild_denylist": ["guild-blocked"],
                "require_mention": False,
            },
        }
    )

    config.save()
    saved_config = json.loads(paths.get_config_path().read_text(encoding="utf-8"))

    assert saved_config["discord"]["guild_allowlist"] == []
    assert saved_config["discord"]["guild_denylist"] == ["guild-blocked"]


def test_save_settings_preserves_discord_denylist_policy_when_default_omitted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    store = SettingsStore.get_instance()
    store.set_guilds_for_platform(
        "discord",
        {"guild-blocked": GuildSettings(enabled=False)},
        default_enabled=True,
    )
    store.save()

    settings = api.save_settings(
        {
            "platform": "discord",
            "guilds": {
                "guild-enabled": {"enabled": True},
            },
        }
    )
    reloaded = SettingsStore.get_instance()

    assert settings["guild_default_enabled"] is True
    assert settings["guilds"]["guild-blocked"]["enabled"] is False
    assert settings["guilds"]["guild-enabled"]["enabled"] is True
    assert reloaded.is_guild_enabled("discord", "guild-blocked") is False
    assert reloaded.is_guild_enabled("discord", "guild-other") is True


def test_wizard_style_guild_save_preserves_migrated_discord_denylist(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    store = SettingsStore.get_instance()
    store.set_guilds_for_platform(
        "discord",
        {"guild-blocked": GuildSettings(enabled=False)},
        default_enabled=True,
    )
    store.save()

    api.save_settings({"platform": "discord", "guilds": {}})
    reloaded = SettingsStore.get_instance()

    assert reloaded.has_guild_scope_for_platform("discord") is True
    assert reloaded.get_guild_default_enabled_for_platform("discord") is True
    assert reloaded.is_guild_enabled("discord", "guild-blocked") is False
    assert reloaded.is_guild_enabled("discord", "guild-other") is True


def test_save_config_validates_before_migrating_discord_guild_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    payload = {
        **_config_payload(),
        "platforms": {"enabled": ["discord", "wechat"], "primary": "discord"},
        "discord": {
            "bot_token": "discord-token-1234567890",
            "guild_allowlist": ["guild-1"],
            "require_mention": False,
        },
    }

    with pytest.raises(ValueError, match="wechat.*must be provided"):
        api.save_config(payload)

    assert SettingsStore.get_instance().has_guild_scope_for_platform("discord") is False
