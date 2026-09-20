from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_compat import to_app_config
from config.v2_config import (
    AgentsConfig,
    ClaudeConfig,
    CodexConfig,
    DEFAULT_AGENT_BACKEND,
    DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS,
    DEFAULT_OPENCODE_ERROR_RETRY_LIMIT,
    DiscordConfig,
    OpenCodeConfig,
    PlatformsConfig,
    RuntimeConfig,
    SlackConfig,
    TelegramConfig,
    UiConfig,
    UpdateConfig,
    V2Config,
)


def test_to_app_config_preserves_enabled_platforms():
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="discord",
        platforms=PlatformsConfig(enabled=["slack", "discord"], primary="discord"),
        slack=SlackConfig(bot_token="", app_token=None, signing_secret=None, team_id=None, team_name=None, app_id=None),
        runtime=RuntimeConfig(default_cwd=".", log_level="INFO"),
        agents=AgentsConfig(
            default_backend="opencode",
            opencode=OpenCodeConfig(enabled=True, cli_path="opencode"),
            claude=ClaudeConfig(enabled=True, cli_path="claude"),
            codex=CodexConfig(enabled=False, cli_path="codex"),
        ),
        discord=DiscordConfig(bot_token="discord-token"),
        ui=UiConfig(),
        update=UpdateConfig(),
    )

    compat = to_app_config(config)

    assert compat.platform == "discord"
    assert compat.platforms == {"enabled": ["slack", "discord"], "primary": "discord"}
    assert compat.enabled_platforms() == ["slack", "discord"]
    assert compat.default_cwd == config.runtime.default_cwd


def test_to_app_config_preserves_telegram_config():
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="telegram",
        platforms=PlatformsConfig(enabled=["telegram"], primary="telegram"),
        slack=SlackConfig(bot_token="", app_token=None, signing_secret=None, team_id=None, team_name=None, app_id=None),
        runtime=RuntimeConfig(default_cwd=".", log_level="INFO"),
        agents=AgentsConfig(
            default_backend="opencode",
            opencode=OpenCodeConfig(enabled=True, cli_path="opencode"),
            claude=ClaudeConfig(enabled=True, cli_path="claude"),
            codex=CodexConfig(enabled=False, cli_path="codex"),
        ),
        telegram=TelegramConfig(bot_token="123456:test-token", require_mention=True, forum_auto_topic=True),
        ui=UiConfig(),
        update=UpdateConfig(),
    )

    compat = to_app_config(config)

    assert compat.platform == "telegram"
    assert compat.telegram is not None
    assert compat.telegram.bot_token == "123456:test-token"


def test_to_app_config_workbench_only_yields_no_enabled_platforms() -> None:
    # A workbench-only V2Config (empty enabled, primary anchored to "avibe")
    # must surface an empty enabled list in the compat view too, so the IM
    # factory creates no clients and the controller wires the in-process Avibe
    # surface itself. Falling back to ``[self.platform]`` here would make the
    # factory try (and fail) to build an "avibe" client from a missing config.
    config = V2Config(
        mode="self_host",
        version="v2",
        platforms=PlatformsConfig(enabled=[], primary="slack"),
        slack=SlackConfig(),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        update=UpdateConfig(),
    )
    config.platforms.validate()
    config.platform = config.platforms.primary

    compat = to_app_config(config)

    assert compat.platform == "avibe"
    assert compat.platforms == {"enabled": [], "primary": "avibe"}
    assert compat.enabled_platforms() == []


def test_to_app_config_uses_shared_agent_defaults() -> None:
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        update=UpdateConfig(),
    )

    compat = to_app_config(config)

    assert compat.default_backend == DEFAULT_AGENT_BACKEND
    assert compat.claude.idle_timeout_seconds == DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    assert compat.opencode is not None
    assert compat.opencode.error_retry_limit == DEFAULT_OPENCODE_ERROR_RETRY_LIMIT
    assert (
        compat.opencode.active_turn_timeout_seconds
        == DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS
    )
    assert DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS == 0


def test_config_load_neutralizes_legacy_opencode_turn_timeout_default(
    monkeypatch, tmp_path
) -> None:
    """The shipped 5400-second cap is the old default's echo, not a choice.

    Seeding every persisted shape proves the property in one pass: a legacy
    payload (marker absent) with the default echo loads as disabled, while the
    same value carrying the provenance marker — the shape every post-change
    save writes — is an explicit choice that survives. An explicit custom value
    and an absent key keep their meaning.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.services.settings import default_config
    from vibe import api

    base = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    # A genuine pre-change payload carries the echo value and no provenance
    # marker; a post-change save always carries the marker.
    cases = [
        (
            {
                "active_turn_timeout_seconds": 90 * 60,
                "legacy_turn_timeout_neutralized": True,
            },
            None,
            90 * 60,
        ),
        ({"active_turn_timeout_seconds": 7200}, None, 7200),
        ({"active_turn_timeout_seconds": 0}, None, 0),
        ({"active_turn_timeout_seconds": 90 * 60}, "legacy", 0),
        ({"active_turn_timeout_seconds": 7200}, "legacy", 7200),
    ]
    for index, (override, legacy_shape, expected) in enumerate(cases):
        payload = copy.deepcopy(base)
        payload["agents"]["opencode"].update(override)
        if legacy_shape:
            payload["agents"]["opencode"].pop("legacy_turn_timeout_neutralized", None)
        config_path = tmp_path / f"config-{index}.json"
        config_path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = V2Config.load(config_path=config_path)

        assert loaded.agents.opencode.active_turn_timeout_seconds == expected
        assert loaded.agents.opencode.legacy_turn_timeout_neutralized is True

    payload = copy.deepcopy(base)
    payload["agents"]["opencode"].pop("active_turn_timeout_seconds", None)
    config_path = tmp_path / "config-missing.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.agents.opencode.active_turn_timeout_seconds == 0


def test_opencode_timeout_only_migration_does_not_rewrite_model_hub_config(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.services.settings import default_config
    from vibe import api

    payload = api.config_to_payload(
        default_config(),
        include_secrets=True,
        include_internal=True,
    )
    payload["agents"]["opencode"]["active_turn_timeout_seconds"] = 90 * 60
    payload["agents"]["opencode"].pop("legacy_turn_timeout_neutralized", None)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    original = config_path.read_bytes()

    for _ in range(2):
        loaded = V2Config.load(config_path=config_path)

        assert loaded.agents.opencode.active_turn_timeout_seconds == 0
        assert loaded.agents.opencode.legacy_turn_timeout_neutralized is True
        assert config_path.read_bytes() == original
        assert not list(
            config_path.parent.glob("config.json.bak-model-hub-migration-*")
        )


def test_to_app_config_exposes_opencode_provider_and_reasoning_fields() -> None:
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        update=UpdateConfig(),
    )
    config.agents.opencode.default_reasoning_effort = "high"
    config.agents.opencode.default_provider = "openai"

    compat = to_app_config(config)

    assert compat.opencode is not None
    assert compat.opencode.default_reasoning_effort == "high"
    assert compat.opencode.default_provider == "openai"


def test_to_app_config_exposes_codex_auth_mode() -> None:
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )
    config.agents.codex.auth_mode = "api_key"

    compat = to_app_config(config)

    assert compat.codex is not None
    assert compat.codex.auth_mode == "api_key"
