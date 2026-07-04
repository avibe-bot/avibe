from types import SimpleNamespace

from config.v2_config import DiscordConfig, TelegramConfig, V2Config
from core.controller import Controller


def _config_payload(discord_payload: dict | None = None, telegram_payload: dict | None = None) -> dict:
    platform = "telegram" if telegram_payload is not None else "discord"
    return {
        "platform": platform,
        "platforms": {"enabled": [platform], "primary": platform},
        "mode": "self_host",
        "version": "v2",
        "discord": discord_payload or {},
        "telegram": telegram_payload or {},
        "runtime": {"default_cwd": "_tmp", "log_level": "INFO"},
        "agents": {
            "default_backend": "opencode",
            "opencode": {"enabled": True, "cli_path": "opencode"},
            "claude": {"enabled": True, "cli_path": "claude"},
            "codex": {"enabled": True, "cli_path": "codex"},
        },
    }


def test_refresh_config_updates_platform_message_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    stale_discord_config = DiscordConfig(bot_token="discord-token", require_mention=True)
    controller = Controller.__new__(Controller)
    controller.config = V2Config.from_payload(
        _config_payload(
            discord_payload={
                "bot_token": "discord-token",
                "require_mention": stale_discord_config.require_mention,
            }
        )
    )
    controller.im_clients = {"discord": SimpleNamespace(config=stale_discord_config)}
    controller._config_mtime = None

    latest_config = V2Config.from_payload(
        _config_payload(
            discord_payload={
                "bot_token": "discord-token",
                "require_mention": False,
            }
        )
    )
    latest_config.save()

    controller._refresh_config_from_disk()

    assert stale_discord_config.require_mention is False


def test_refresh_config_updates_telegram_option_only_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    stale_telegram_config = TelegramConfig(
        bot_token="123456:test-token",
        require_mention=True,
        forum_auto_topic=True,
    )
    controller = Controller.__new__(Controller)
    controller.config = V2Config.from_payload(
        _config_payload(
            telegram_payload={
                "bot_token": "123456:test-token",
                "require_mention": stale_telegram_config.require_mention,
                "forum_auto_topic": stale_telegram_config.forum_auto_topic,
            }
        )
    )
    controller.im_clients = {"telegram": SimpleNamespace(config=stale_telegram_config)}
    controller._config_mtime = None

    latest_config = V2Config.from_payload(
        _config_payload(
            telegram_payload={
                "bot_token": "123456:test-token",
                "require_mention": False,
                "forum_auto_topic": False,
            }
        )
    )
    latest_config.save()

    controller._refresh_config_from_disk()

    assert stale_telegram_config.require_mention is False
    assert stale_telegram_config.forum_auto_topic is False


def test_refresh_config_updates_remote_access_for_audio_asr(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    controller = Controller.__new__(Controller)
    controller.config = V2Config.from_payload(_config_payload({"bot_token": "discord-token"}))
    controller.im_clients = {}
    controller._config_mtime = None
    controller.audio_asr_service = SimpleNamespace(config=controller.config)

    latest_config = V2Config.from_payload(_config_payload({"bot_token": "discord-token"}))
    latest_config.remote_access.vibe_cloud.enabled = True
    latest_config.remote_access.vibe_cloud.backend_url = "https://avibe.bot"
    latest_config.remote_access.vibe_cloud.instance_id = "inst_123"
    latest_config.remote_access.vibe_cloud.instance_secret = "secret"
    latest_config.save()

    controller._refresh_config_from_disk()

    assert controller.config.remote_access.vibe_cloud.instance_secret == "secret"
    assert controller.audio_asr_service.config is controller.config


def test_refresh_config_updates_agent_progress_style(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    controller = Controller.__new__(Controller)
    controller.config = V2Config.from_payload(
        {**_config_payload({"bot_token": "discord-token"}), "agent_progress_style": "off"}
    )
    controller.im_clients = {}
    controller._config_mtime = None
    assert controller.config.agent_progress_style == "off"

    latest_config = V2Config.from_payload(
        {
            **_config_payload({"bot_token": "discord-token"}),
            "agent_progress_style": "concise",
            "agent_status_heartbeat_ms": 9000,
            "agent_status_no_output_ms": 45000,
        }
    )
    latest_config.save()

    controller._refresh_config_from_disk()

    assert controller.config.agent_progress_style == "concise"
    assert controller.config.agent_status_heartbeat_ms == 9000
    assert controller.config.agent_status_no_output_ms == 45000


def test_progress_style_getter_self_refreshes_from_disk(tmp_path, monkeypatch) -> None:
    # Background / scheduled runs never pass through an IM inbound handler, so
    # nothing calls _refresh_config_from_disk before the style gate. The getter
    # must reload on its own so a Web UI off->concise change takes effect without
    # an unrelated refresh or restart.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    controller = Controller.__new__(Controller)
    controller.config = V2Config.from_payload(
        {**_config_payload({"bot_token": "discord-token"}), "agent_progress_style": "off"}
    )
    controller.im_clients = {}
    controller._config_mtime = None

    latest_config = V2Config.from_payload(
        {**_config_payload({"bot_token": "discord-token"}), "agent_progress_style": "concise"}
    )
    latest_config.save()

    # No prior _refresh_config_from_disk call; the getter itself must pick it up.
    assert controller.get_progress_style_for_context(None) == "concise"
    assert controller.get_heartbeat_interval_ms_for_context(None) == 15000
