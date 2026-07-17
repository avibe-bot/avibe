from __future__ import annotations

from config.v2_config import (
    AgentsConfig,
    DiscordConfig,
    LarkConfig,
    PlatformsConfig,
    RuntimeConfig,
    SlackConfig,
    TelegramConfig,
    UiConfig,
    UpdateConfig,
    V2Config,
    WeChatConfig,
)
from vibe import api


def _base_config(**overrides) -> V2Config:
    payload = {
        "mode": "self_host",
        "version": "v2",
        "platform": "slack",
        "platforms": PlatformsConfig(enabled=["slack"], primary="slack"),
        "slack": SlackConfig(bot_token=""),
        "runtime": RuntimeConfig(default_cwd="."),
        "agents": AgentsConfig(),
        "ui": UiConfig(),
        "update": UpdateConfig(),
    }
    payload.update(overrides)
    return V2Config(**payload)


def test_setup_state_counts_telegram_credentials() -> None:
    config = _base_config(
        platform="telegram",
        platforms=PlatformsConfig(enabled=["telegram"], primary="telegram"),
        telegram=TelegramConfig(bot_token="123456:test-token"),
        setup_completed=True,
    )

    assert config.platform_has_credentials("telegram") is True
    assert config.configured_platforms() == ["telegram"]
    assert config.setup_state() == {
        "needs_setup": False,
        "configured_platforms": ["telegram"],
        "missing_credentials": [],
    }


def test_setup_state_only_counts_enabled_platforms() -> None:
    config = _base_config(
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        discord=DiscordConfig(bot_token="configured-but-disabled"),
    )

    assert config.platform_has_credentials("discord") is True
    assert config.configured_platforms() == []
    assert config.setup_state()["needs_setup"] is True


def test_setup_state_uses_setup_completed_flag() -> None:
    # ``needs_setup`` is gated solely on the explicit ``setup_completed`` flag,
    # independent of whether any platform credentials are configured. A
    # workbench-only install (no IM credentials) that finished the wizard is
    # therefore not bounced back to /setup.
    config = _base_config(setup_completed=True)

    assert config.configured_platforms() == []
    assert config.setup_state()["needs_setup"] is False


def test_runtime_resource_governance_round_trips_through_payload() -> None:
    config = _base_config(
        runtime=RuntimeConfig(
            default_cwd=".",
            resource_governance={
                "mode": "enabled",
                "agent_memory_max_bytes": 1536 * 1024 * 1024,
            },
        )
    )

    payload = api.config_to_payload(config, include_secrets=True)
    restored = V2Config.from_payload(payload)

    assert payload["runtime"]["resource_governance"]["mode"] == "enabled"
    assert restored.runtime.resource_governance == config.runtime.resource_governance


def test_from_payload_migrates_legacy_setup_completed_true() -> None:
    # A payload that predates the flag (no ``setup_completed`` key) but has a
    # mode plus a credentialed enabled platform migrates to completed=True.
    payload = api.config_to_payload(
        _base_config(
            platform="telegram",
            platforms=PlatformsConfig(enabled=["telegram"], primary="telegram"),
            telegram=TelegramConfig(bot_token="123456:test-token"),
        ),
        include_secrets=True,
    )
    payload.pop("setup_completed", None)

    config = V2Config.from_payload(payload)

    assert config.setup_completed is True
    assert config.setup_state()["needs_setup"] is False


def test_from_payload_migrates_legacy_setup_completed_false() -> None:
    # A legacy payload (no ``setup_completed`` key) whose only enabled platform
    # lacks credentials migrates to completed=False, so the wizard still runs.
    payload = api.config_to_payload(
        _base_config(
            platform="telegram",
            platforms=PlatformsConfig(enabled=["telegram"], primary="telegram"),
            telegram=TelegramConfig(bot_token=""),
        ),
        include_secrets=True,
    )
    payload.pop("setup_completed", None)

    config = V2Config.from_payload(payload)

    assert config.setup_completed is False
    assert config.setup_state()["needs_setup"] is True


def test_validate_strips_workbench_and_retargets_primary() -> None:
    # A legacy/hand-edited config with avibe alongside a real IM (and avibe as
    # primary) must normalize: avibe is stripped from `enabled` and the primary
    # is retargeted to the real platform, so the IM factory/controller never see
    # a stranded 'avibe' primary (which would crash startup).
    platforms = PlatformsConfig(enabled=["avibe", "slack"], primary="avibe")
    platforms.validate()
    assert platforms.enabled == ["slack"]
    assert platforms.primary == "slack"

    # avibe trailing with a real primary: stripped, primary untouched.
    trailing = PlatformsConfig(enabled=["slack", "avibe"], primary="slack")
    trailing.validate()
    assert trailing.enabled == ["slack"]
    assert trailing.primary == "slack"

    # avibe-only enabled normalizes to workbench-only.
    workbench = PlatformsConfig(enabled=["avibe"], primary="avibe")
    workbench.validate()
    assert workbench.enabled == []
    assert workbench.primary == "avibe"


def test_validate_derives_primary_from_enabled_without_resurrecting() -> None:
    # ``enabled`` is the source of truth; ``primary`` is an internal default with
    # no user-facing control. A stale primary that is NOT in the enabled set
    # (e.g. surviving a deep config merge after the platform was disabled) must
    # FOLLOW enabled — retarget to the first enabled platform — instead of
    # forcing the removed platform back into ``enabled``. This is what lets the
    # UI persist an enabled-set change by sending only ``platforms.enabled``.
    stale = PlatformsConfig(enabled=["discord"], primary="slack")
    stale.validate()
    assert stale.enabled == ["discord"]
    assert stale.primary == "discord"

    # Order preserved; primary tracks the first enabled platform.
    multi = PlatformsConfig(enabled=["telegram", "discord"], primary="slack")
    multi.validate()
    assert multi.enabled == ["telegram", "discord"]
    assert multi.primary == "telegram"

    # A primary still present in enabled is left untouched.
    kept = PlatformsConfig(enabled=["slack", "discord"], primary="discord")
    kept.validate()
    assert kept.enabled == ["slack", "discord"]
    assert kept.primary == "discord"


def test_config_payload_includes_platform_catalog_and_setup_state() -> None:
    config = _base_config(
        platforms=PlatformsConfig(enabled=["slack", "discord", "telegram", "lark", "wechat"], primary="slack"),
        slack=SlackConfig(bot_token="xoxb-test"),
        discord=DiscordConfig(bot_token="discord-token"),
        telegram=TelegramConfig(bot_token="123456:test-token"),
        lark=LarkConfig(app_id="app-id", app_secret="app-secret"),
        wechat=WeChatConfig(bot_token="wechat-token"),
        setup_completed=True,
    )

    payload = api.config_to_payload(config)

    assert [platform["id"] for platform in payload["platform_catalog"]] == [
        "slack",
        "discord",
        "telegram",
        "lark",
        "wechat",
        "avibe",
    ]
    assert [backend["id"] for backend in payload["agent_backend_catalog"]] == [
        "opencode",
        "claude",
        "codex",
    ]
    assert payload["setup_state"]["configured_platforms"] == ["slack", "discord", "telegram", "lark", "wechat"]
    assert payload["setup_state"]["needs_setup"] is False
    assert payload["ui"]["chat_message_font_size"] == 14


def test_platforms_validate_allows_empty_enabled_and_anchors_avibe() -> None:
    # Workbench-only install: the wizard saves no external IM platform. Empty
    # ``enabled`` must validate (no longer raise) and anchor ``primary`` to the
    # in-process Avibe surface without force-inserting a real IM into ``enabled``.
    platforms = PlatformsConfig(enabled=[], primary="slack")

    platforms.validate()

    assert platforms.primary == "avibe"
    assert platforms.enabled == []


def test_platforms_validate_keeps_non_empty_enabled_unchanged() -> None:
    # ``enabled`` is the source of truth: a stale primary not in the enabled set
    # retargets to the first enabled platform; the enabled list is NOT grown to
    # resurrect the removed platform (see
    # test_validate_derives_primary_from_enabled_without_resurrecting).
    platforms = PlatformsConfig(enabled=["discord"], primary="slack")

    platforms.validate()

    assert platforms.primary == "discord"
    assert platforms.enabled == ["discord"]


def test_workbench_only_config_round_trips_with_avibe_primary() -> None:
    config = _base_config(
        platforms=PlatformsConfig(enabled=[], primary="slack"),
        setup_completed=True,
    )
    # ``save()`` validates before persisting; mirror that so the serialized
    # payload reflects what actually lands on disk for a workbench-only install.
    config.platforms.validate()
    config.platform = config.platforms.primary

    payload = api.config_to_payload(config, include_secrets=True)
    assert payload["platforms"] == {"enabled": [], "primary": "avibe"}

    restored = V2Config.from_payload(payload)

    assert restored.platforms.primary == "avibe"
    assert restored.platforms.enabled == []
    assert restored.platform == "avibe"
    assert restored.enabled_platforms() == []


def test_chat_message_font_size_is_clamped() -> None:
    payload = api.config_to_payload(_base_config())
    payload["ui"]["chat_message_font_size"] = 99

    config = V2Config.from_payload(payload)

    assert config.ui.chat_message_font_size == 20

    payload["ui"]["chat_message_font_size"] = "bad"
    config = V2Config.from_payload(payload)

    assert config.ui.chat_message_font_size == 14


def test_show_agent_activity_defaults_off_and_round_trips() -> None:
    # Default off: absent from the ui payload → False, and serializes into the
    # payload so the Web UI + ChatPage bootstrap can read it (like the font size).
    payload = api.config_to_payload(_base_config())
    assert payload["ui"]["show_agent_activity"] is False

    payload["ui"]["show_agent_activity"] = True
    config = V2Config.from_payload(payload)
    assert config.ui.show_agent_activity is True
    assert api.config_to_payload(config)["ui"]["show_agent_activity"] is True

    # Non-bool values coerce to a real bool (config file hand-edit robustness).
    payload["ui"]["show_agent_activity"] = 1
    assert V2Config.from_payload(payload).ui.show_agent_activity is True

    # String forms are parsed explicitly — ``bool("false")`` would be True, which
    # must NOT enable streaming. Known truthy/falsey strings resolve correctly.
    for truthy in ("true", "True", "1", "yes", "on"):
        payload["ui"]["show_agent_activity"] = truthy
        assert V2Config.from_payload(payload).ui.show_agent_activity is True, truthy
    for falsey in ("false", "False", "0", "no", "off", ""):
        payload["ui"]["show_agent_activity"] = falsey
        assert V2Config.from_payload(payload).ui.show_agent_activity is False, falsey


def test_config_payload_includes_vibe_cloud_remote_access() -> None:
    config = _base_config()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "https://alex.avibe.bot"
    config.remote_access.vibe_cloud.instance_id = "inst_123"

    payload = api.config_to_payload(config)

    assert payload["remote_access"]["provider"] == "vibe_cloud"
    assert payload["remote_access"]["vibe_cloud"]["enabled"] is True
    assert payload["remote_access"]["vibe_cloud"]["public_url"] == "https://alex.avibe.bot"
