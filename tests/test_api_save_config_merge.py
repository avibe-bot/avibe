from __future__ import annotations

import ast
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import UiConfig, V2Config
from vibe import api


def _full_config_payload() -> dict:
    return {
        "platform": "discord",
        "mode": "self_host",
        "version": "v2",
        "slack": {
            "bot_token": "",
            "app_token": None,
            "signing_secret": None,
            "team_id": None,
            "team_name": None,
            "app_id": None,
            "require_mention": False,
            "disable_link_unfurl": False,
        },
        "discord": {
            "bot_token": "discord-token-1234567890",
            "application_id": None,
            "guild_allowlist": ["754776951587340359"],
            "guild_denylist": [],
            "require_mention": False,
        },
        "lark": {
            "app_id": "",
            "app_secret": "",
            "require_mention": False,
            "domain": "feishu",
        },
        "runtime": {
            "default_cwd": "/tmp/workdir",
            "log_level": "INFO",
        },
        "agents": {
            "default_backend": "codex",
            "opencode": {
                "enabled": True,
                "cli_path": "opencode",
                "default_agent": None,
                "default_model": None,
                "default_reasoning_effort": None,
                "error_retry_limit": 1,
            },
            "claude": {
                "enabled": True,
                "cli_path": "claude",
                "default_model": None,
            },
            "codex": {
                "enabled": True,
                "cli_path": "codex",
                "default_model": "gpt-5.4",
            },
        },
        "gateway": None,
        "ui": {
            "setup_host": "127.0.0.1",
            "setup_port": 5123,
            "open_browser": False,
        },
        "update": {
            "auto_update": False,
            "check_interval_minutes": 0,
            "idle_minutes": 30,
            "notify_admins": False,
        },
        "ack_mode": "reaction",
        "show_duration": True,
        "include_time_info": True,
        "include_user_info": True,
        "reply_enhancements": True,
        "show_pages_prompt": True,
        "language": "en",
    }


def test_save_config_merges_partial_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    original = api.save_config(_full_config_payload())
    assert original.show_duration is True
    assert original.include_time_info is True
    assert original.update.auto_update is False

    updated = api.save_config({"show_duration": False, "include_time_info": False, "update": {"auto_update": True}})

    assert updated.show_duration is False
    assert updated.include_time_info is False
    assert updated.update.auto_update is True
    assert updated.platform == "discord"
    assert updated.discord is not None
    assert updated.discord.bot_token == "discord-token-1234567890"
    assert updated.runtime.default_cwd == "/tmp/workdir"


def test_save_config_seeds_default_for_partial_payload_on_fresh_install(monkeypatch, tmp_path):
    """Regression: a fresh install (no config file yet) must accept the wizard's
    reused provider-config modal POSTing only ``{"agents": ...}``.

    Before the default-seed fix the partial payload went straight into
    ``V2Config.from_payload`` and raised (missing ``mode``/``runtime``), so the
    advertised first-run "Configure provider" flow failed until a config existed.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    import pytest

    with pytest.raises(FileNotFoundError):
        api.load_config()  # precondition: truly fresh, no config file

    created = api.save_config(
        {"agents": {"claude": {"enabled": True, "cli_path": "claude", "default_model": "sonnet"}}}
    )

    # The partial save merges onto the workbench-only default and persists.
    assert created.mode == "self_host"
    assert created.agents.claude.enabled is True
    assert created.agents.claude.default_model == "sonnet"
    # Configuring a provider mid-wizard must not complete setup...
    assert created.setup_completed is False
    assert created.setup_state()["needs_setup"] is True
    # ...nor leave a phantom Slack transport: the seeded base is workbench-only.
    assert created.platforms.enabled == []
    assert created.platforms.primary == "avibe"


def test_save_config_defaults_show_duration_to_false_for_new_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload.pop("show_duration")

    created = api.save_config(payload)

    assert created.show_duration is False


def test_save_config_defaults_include_time_info_to_true_for_new_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload.pop("include_time_info")

    created = api.save_config(payload)

    assert created.include_time_info is True


def test_save_config_accepts_typing_ack_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    updated = api.save_config({**_full_config_payload(), "ack_mode": "typing"})

    assert updated.ack_mode == "typing"


def test_save_config_merges_audio_asr_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    created = api.save_config(_full_config_payload())
    assert created.audio_asr.enabled is True
    assert created.audio_asr.enabled_configured is False
    assert created.audio_asr.echo_transcript is True

    updated = api.save_config({"audio_asr": {"enabled": False, "enabled_configured": True, "echo_transcript": False}})
    payload = api.config_to_payload(updated)

    assert updated.audio_asr.enabled is False
    assert updated.audio_asr.enabled_configured is True
    assert updated.audio_asr.echo_transcript is False
    assert updated.audio_asr.endpoint_path == "/v1/audio/transcriptions"
    assert payload["audio_asr"]["enabled"] is False
    assert payload["audio_asr"]["enabled_configured"] is True
    assert payload["audio_asr"]["echo_transcript"] is False


def test_save_config_marks_explicit_audio_asr_disable_patch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    api.save_config(_full_config_payload())

    updated = api.save_config({"audio_asr": {"enabled": False}})

    assert updated.audio_asr.enabled is False
    assert updated.audio_asr.enabled_configured is True


def test_config_load_defaults_missing_audio_asr_to_enabled():
    payload = _full_config_payload()
    payload.pop("audio_asr", None)

    created = V2Config.from_payload(payload)

    assert created.audio_asr.enabled is True
    assert created.audio_asr.enabled_configured is False


def test_config_payload_defaults_instance_name_to_remote_access_slug(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "https://alex-app.avibe.bot"

    payload = api.config_to_payload(config)

    assert payload["ui"]["instance_name"] == ""
    assert payload["ui"]["default_instance_name"] == "alex"
    assert payload["ui"]["system_hostname"] == "macbook"


def test_config_payload_default_instance_name_falls_back_to_hostname(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = False
    config.remote_access.vibe_cloud.public_url = "https://alex-app.avibe.bot"

    payload = api.config_to_payload(config)

    assert payload["ui"]["default_instance_name"] == "macbook"


def test_config_payload_default_instance_name_ignores_invalid_remote_url(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "http://alex-app.avibe.bot"

    payload = api.config_to_payload(config)

    assert payload["ui"]["default_instance_name"] == "macbook"


def test_config_payload_default_instance_name_ignores_malformed_remote_url(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "https://["

    payload = api.config_to_payload(config)

    assert payload["ui"]["default_instance_name"] == "macbook"


def test_save_config_preserves_show_pages_prompt_toggle(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    created = api.save_config(_full_config_payload())
    assert created.show_pages_prompt is True

    updated = api.save_config({"show_pages_prompt": False})
    payload = api.config_to_payload(updated)

    assert updated.show_pages_prompt is False
    assert payload["show_pages_prompt"] is False


def test_save_config_preserves_status_bubble_settings_on_partial_save(monkeypatch, tmp_path):
    """An unrelated partial save must NOT reset agent_progress_style / intervals.

    Regression for the config_to_payload omission that wiped these on any UI save.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    full = _full_config_payload()
    full["agent_progress_style"] = "verbose"
    full["agent_status_heartbeat_ms"] = 12000
    created = api.save_config(full)
    assert created.agent_progress_style == "verbose"
    assert created.agent_status_heartbeat_ms == 12000

    # Toggle an unrelated field — the status-bubble settings must survive.
    updated = api.save_config({"show_duration": False})
    payload = api.config_to_payload(updated)

    assert updated.agent_progress_style == "verbose"
    assert updated.agent_status_heartbeat_ms == 12000
    assert payload["agent_progress_style"] == "verbose"
    assert payload["agent_status_heartbeat_ms"] == 12000


def test_config_load_defaults_missing_show_pages_prompt_to_enabled():
    payload = _full_config_payload()
    payload.pop("show_pages_prompt")

    created = V2Config.from_payload(payload)

    assert created.show_pages_prompt is True


def test_config_load_preserves_pre_upgrade_audio_asr_false_as_opt_out():
    payload = _full_config_payload()
    payload["audio_asr"] = {"enabled": False, "echo_transcript": True}

    created = V2Config.from_payload(payload)

    assert created.audio_asr.enabled is False
    assert created.audio_asr.enabled_configured is True


def test_save_config_preserves_explicit_audio_asr_opt_out(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["audio_asr"] = {
        "enabled": False,
        "enabled_configured": True,
        "echo_transcript": True,
    }

    created = api.save_config(payload)

    assert created.audio_asr.enabled is False
    assert created.audio_asr.enabled_configured is True


def test_config_to_payload_redacts_remote_access_secrets_and_save_preserves_them(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = _full_config_payload()
    payload["remote_access"] = {
        "provider": "vibe_cloud",
        "vibe_cloud": {
            "enabled": True,
            "backend_url": "https://avibe.bot",
            "public_url": "https://alex.avibe.bot",
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://avibe.bot",
            "authorization_endpoint": "https://avibe.bot/oauth/authorize",
            "token_endpoint": "https://avibe.bot/oauth/token",
            "jwks_uri": "https://avibe.bot/oauth/jwks.json",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
            "session_secret": "session-secret",
        },
    }
    created = api.save_config(payload)

    redacted = api.config_to_payload(created)
    cloud_payload = redacted["remote_access"]["vibe_cloud"]
    updated = api.save_config({**redacted, "show_duration": False})

    assert "tunnel_token" not in cloud_payload
    assert "instance_secret" not in cloud_payload
    assert "session_secret" not in cloud_payload
    assert updated.remote_access.vibe_cloud.tunnel_token == "tunnel-token"
    assert updated.remote_access.vibe_cloud.instance_secret == "instance-secret"
    assert updated.remote_access.vibe_cloud.session_secret == "session-secret"


def test_config_to_payload_redacts_platform_and_gateway_secrets_and_save_preserves_them(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = _full_config_payload()
    payload["slack"] = {
        **payload["slack"],
        "bot_token": "xoxb-secret-token",
        "app_token": "xapp-secret-token",
        "signing_secret": "slack-signing-secret",
    }
    payload["telegram"] = {
        "bot_token": "123456:telegram-secret",
        "webhook_secret_token": "telegram-webhook-secret",
        "require_mention": True,
        "forum_auto_topic": True,
        "use_webhook": True,
    }
    payload["lark"] = {
        "app_id": "cli_lark_id",
        "app_secret": "lark-secret",
        "require_mention": False,
        "domain": "feishu",
    }
    payload["wechat"] = {
        "bot_token": "wechat-secret",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "require_mention": False,
    }
    payload["gateway"] = {
        "relay_url": "https://relay.example",
        "workspace_token": "workspace-secret",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }

    created = api.save_config(payload)
    redacted = api.config_to_payload(created)

    assert redacted["slack"]["bot_token_length"] == len("xoxb-secret-token")
    assert redacted["slack"]["has_bot_token"] is True
    assert "bot_token" not in redacted["slack"]
    assert redacted["slack"]["has_app_token"] is True
    assert "app_token" not in redacted["slack"]
    assert redacted["slack"]["has_signing_secret"] is True
    assert "signing_secret" not in redacted["slack"]
    assert redacted["discord"]["has_bot_token"] is True
    assert "bot_token" not in redacted["discord"]
    assert redacted["telegram"]["has_bot_token"] is True
    assert "bot_token" not in redacted["telegram"]
    assert redacted["telegram"]["has_webhook_secret_token"] is True
    assert "webhook_secret_token" not in redacted["telegram"]
    assert redacted["lark"]["app_id"] == "cli_lark_id"
    assert redacted["lark"]["has_app_secret"] is True
    assert "app_secret" not in redacted["lark"]
    assert redacted["wechat"]["has_bot_token"] is True
    assert "bot_token" not in redacted["wechat"]
    assert redacted["gateway"]["has_workspace_token"] is True
    assert "workspace_token" not in redacted["gateway"]
    assert redacted["gateway"]["has_client_secret"] is True
    assert "client_secret" not in redacted["gateway"]

    included = api.config_to_payload(created, include_secrets=True)
    assert included["slack"]["bot_token"] == "xoxb-secret-token"
    assert included["gateway"]["client_secret"] == "client-secret"

    redacted["show_duration"] = False
    updated = api.save_config(redacted)

    assert updated.slack.bot_token == "xoxb-secret-token"
    assert updated.slack.app_token == "xapp-secret-token"
    assert updated.slack.signing_secret == "slack-signing-secret"
    assert updated.discord.bot_token == "discord-token-1234567890"
    assert updated.telegram.bot_token == "123456:telegram-secret"
    assert updated.telegram.webhook_secret_token == "telegram-webhook-secret"
    assert updated.lark.app_secret == "lark-secret"
    assert updated.wechat.bot_token == "wechat-secret"
    assert updated.gateway is not None
    assert updated.gateway.workspace_token == "workspace-secret"
    assert updated.gateway.client_secret == "client-secret"


def test_save_config_accepts_slack_disable_link_unfurl(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["slack"]["disable_link_unfurl"] = True

    updated = api.save_config(payload)

    assert updated.slack.disable_link_unfurl is True


def test_save_config_preserves_platforms_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["slack"]["bot_token"] = "xoxb-valid-token"
    payload["slack"]["app_token"] = "xapp-valid-token"
    updated = api.save_config(
        {
            **payload,
            "wechat": {
                "corp_id": "wk123",
                "agent_id": "agent1",
                "secret": "sec",
                "token": "tok",
                "aes_key": "aes",
            },
            "platforms": {"enabled": ["slack", "discord", "wechat"], "primary": "discord"},
        }
    )

    assert updated.platform == "discord"
    assert updated.platforms.primary == "discord"
    assert updated.platforms.enabled == ["slack", "discord", "wechat"]


def test_save_config_migrates_legacy_single_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    updated = api.save_config(_full_config_payload())
    payload = api.config_to_payload(updated)

    assert updated.platforms.primary == "discord"
    assert updated.platforms.enabled == ["discord"]
    assert payload["platforms"] == {"enabled": ["discord"], "primary": "discord"}


def test_save_config_rejects_enabled_platform_without_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    import pytest

    payload = _full_config_payload()
    payload["platform"] = "avibe"
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    payload["lark"] = None
    created = api.save_config(payload)
    assert created.platforms.enabled == []
    assert created.lark is None

    with pytest.raises(ValueError, match="Config 'lark' must be provided when lark is enabled"):
        api.save_config({"platform": "lark", "platforms": {"enabled": ["lark"], "primary": "lark"}})


def test_save_config_rejects_enabled_platform_without_runtime_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    import pytest

    with pytest.raises(ValueError, match="Config 'lark.app_id', 'lark.app_secret' must be provided"):
        api.save_config(
            {
                **_full_config_payload(),
                "platform": "lark",
                "platforms": {"enabled": ["lark"], "primary": "lark"},
                "lark": {},
            }
        )

    with pytest.raises(ValueError, match="Config 'slack.bot_token' must be provided"):
        api.save_config(
            {
                **_full_config_payload(),
                "platform": "slack",
                "platforms": {"enabled": ["slack"], "primary": "slack"},
                "slack": {"bot_token": "", "app_token": "xapp-valid"},
            }
        )


def test_save_config_allows_slack_bot_token_only_runtime_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = {
        **_full_config_payload(),
        "platform": "slack",
        "platforms": {"enabled": ["slack"], "primary": "slack"},
        "slack": {"bot_token": "xoxb-valid", "app_token": ""},
    }

    config = api.save_config(payload)

    assert config.platforms.enabled == ["slack"]
    assert config.slack.bot_token == "xoxb-valid"
    assert config.slack.app_token == ""


def test_save_config_rejects_setup_completion_with_enabled_platform_without_runtime_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    import pytest

    payload = _full_config_payload()
    payload["platform"] = "avibe"
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    created = api.save_config(payload)
    assert created.platforms.enabled == []

    with pytest.raises(ValueError, match="Config 'lark.app_id', 'lark.app_secret' must be provided"):
        api.save_config(
            {
                "platform": "lark",
                "platforms": {"enabled": ["lark"], "primary": "lark"},
                "lark": {"domain": "feishu"},
                "setup_completed": True,
            }
        )


def test_save_config_allows_unrelated_save_for_legacy_enabled_platform_without_runtime_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["platform"] = "slack"
    payload["platforms"] = {"enabled": ["slack"], "primary": "slack"}
    payload["slack"] = {"bot_token": "", "app_token": ""}
    V2Config.from_payload(payload).save()

    updated = api.save_config({"remote_access": {"vibe_cloud": {"enabled": False}}})

    assert updated.platforms.enabled == ["slack"]
    assert updated.slack.bot_token == ""
    assert updated.remote_access.vibe_cloud.enabled is False


def test_save_config_allows_redacted_lark_round_trip_for_legacy_missing_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["platform"] = "lark"
    payload["platforms"] = {"enabled": ["lark"], "primary": "lark"}
    payload["lark"] = {"app_id": "cli_lark_id", "app_secret": "", "domain": "feishu"}
    V2Config.from_payload(payload).save()

    updated = api.save_config(
        {
            "lark": {
                "app_id": "cli_lark_id",
                "has_app_secret": True,
                "app_secret_length": 0,
                "domain": "lark",
            }
        }
    )

    assert updated.platforms.enabled == ["lark"]
    assert updated.lark.app_id == "cli_lark_id"
    assert updated.lark.app_secret == ""
    assert updated.lark.domain == "lark"

    import pytest

    with pytest.raises(ValueError, match="Config 'lark.app_secret' must be provided"):
        api.save_config(
            {
                "lark": {
                    "app_id": "cli_lark_changed",
                    "has_app_secret": True,
                    "app_secret_length": 0,
                    "domain": "lark",
                }
            }
        )


def test_save_config_preserves_disabled_platform_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["platform"] = "avibe"
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    payload["lark"] = None
    created = api.save_config(payload)
    assert created.platforms.enabled == []
    assert created.lark is None

    updated = api.save_config(
        {
            "platform": "lark",
            "lark": {
                "app_id": "cli_test",
                "app_secret": "secret",
                "domain": "feishu",
            },
        }
    )

    assert updated.platforms.enabled == []
    assert updated.platforms.primary == "avibe"
    assert updated.lark is not None
    assert updated.lark.app_id == "cli_test"
    assert updated.lark.app_secret == "secret"

    enabled = api.save_config({"platform": "lark", "platforms": {"enabled": ["lark"], "primary": "lark"}})

    assert enabled.platforms.enabled == ["lark"]
    assert enabled.platforms.primary == "lark"
    assert enabled.lark is not None
    assert enabled.lark.app_id == "cli_test"
    assert enabled.lark.app_secret == "secret"


def test_init_sessions_is_noop_when_sessions_file_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    store = api.SessionsStore()
    store.state.session_mappings = {
        "discord::749794605024936027": {
            "codex": {"discord_1482432040375943208": "019d1f70-692b-7c32-b152-b4aef9e24002"}
        }
    }
    store.save()

    api.init_sessions()

    reloaded = api.SessionsStore()
    reloaded.load()
    assert reloaded.state.session_mappings == store.state.session_mappings


def test_config_post_does_not_call_init_sessions():
    source = Path("vibe/ui_server.py").read_text(encoding="utf-8")
    module = ast.parse(source)

    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    functions = {node.name: node for node in module.body if isinstance(node, function_types)}
    pending = ["config_post"]
    save_path_nodes = {}
    while pending:
        name = pending.pop()
        if name in save_path_nodes:
            continue
        node = functions[name]
        save_path_nodes[name] = node
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in functions and child.id != name:
                pending.append(child.id)

    assert "_save_config_and_runtime_decisions" in save_path_nodes

    calls_init_sessions = []
    for name, function_node in save_path_nodes.items():
        for node in ast.walk(function_node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "api" and node.func.attr == "init_sessions":
                    calls_init_sessions.append(name)

    assert calls_init_sessions == []


def test_settings_platforms_apply_uses_parent_platform_identity():
    source = Path("ui/src/components/settings/SettingsPlatformsPage.tsx").read_text(encoding="utf-8")

    assert "const handleApplyPlatform = async (platform: string, nextData: any)" in source
    assert "onApply={(data) => handleApplyPlatform(id, data)}" in source
    assert "const platform = String(nextData?.platform || '')" not in source


def test_settings_platforms_persists_discord_guild_scope_before_auto_enable():
    source = Path("ui/src/components/settings/SettingsPlatformsPage.tsx").read_text(encoding="utf-8")

    assert "const savePlatformSettings = async (platform: string, nextData: any)" in source
    assert "platform === 'discord'" in source
    assert "await api.saveSettings({" in source
    assert "await savePlatformSettings(platform, nextData);" in source


def test_platform_runnable_config_keeps_wechat_token_optional():
    source = Path("ui/src/lib/platforms.ts").read_text(encoding="utf-8")

    assert "if (platform === 'wechat')" in source
    assert "return Boolean(data?.wechat);" in source


def test_wizard_platform_selection_preserves_credential_drafts_on_continue():
    source = Path("ui/src/components/steps/PlatformSelection.tsx").read_text(encoding="utf-8")

    assert "const nextData = {" in source
    assert "...credentialDraft," in source
    assert "await onSave(nextData);" in source
    assert "onNext(nextData);" in source
    assert "onNext(selectionData);" not in source


# ---------------------------------------------------------------------------
# Config field-completeness (guards the "partial save silently drops a field"
# class of bug). ``save_config`` deep-merges the incoming payload onto
# ``config_to_payload(load_config())`` as its base; any field the base payload
# omits is lost whenever a save does not itself re-send that field. So every
# persisted config field MUST appear in both serializers.
# ---------------------------------------------------------------------------


def test_config_to_payload_includes_avault_agent():
    """Regression: ``config_to_payload`` dropped ``agents.avault`` entirely, so
    every UI save reset ``agents.avault.cli_path`` to the dataclass default."""
    config = V2Config.from_payload(_full_config_payload())
    config.agents.avault.cli_path = "/opt/managed/avault"

    payload = api.config_to_payload(config)

    assert "avault" in payload["agents"]
    assert payload["agents"]["avault"]["cli_path"] == "/opt/managed/avault"


def test_save_config_preserves_avault_cli_path_on_unrelated_partial_save(monkeypatch, tmp_path):
    """A partial UI save (e.g. toggling ``show_duration``) must NOT reset a
    previously-persisted ``agents.avault.cli_path`` (set by ``vibe runtime
    prepare`` -> ``_persist_avault_cli_path``)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    full = _full_config_payload()
    full["agents"]["avault"] = {"cli_path": "/opt/managed/avault"}
    created = api.save_config(full)
    assert created.agents.avault.cli_path == "/opt/managed/avault"

    updated = api.save_config({"show_duration": False})

    assert updated.agents.avault.cli_path == "/opt/managed/avault"
    assert api.config_to_payload(updated)["agents"]["avault"]["cli_path"] == "/opt/managed/avault"


def test_save_config_preserves_ui_fields_on_unrelated_partial_save(monkeypatch, tmp_path):
    """The owner-facing scenario: after enabling ``show_agent_activity`` (and a
    custom font size / instance name), an unrelated partial save must keep them.
    Guards the ``ui`` sub-block of the deep-merge base."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    full = _full_config_payload()
    full["ui"] = {
        **full["ui"],
        "show_agent_activity": True,
        "chat_message_font_size": 20,
        "instance_name": "OwnerBox",
    }
    created = api.save_config(full)
    assert created.ui.show_agent_activity is True

    updated = api.save_config({"show_duration": False})

    assert updated.ui.show_agent_activity is True
    assert updated.ui.chat_message_font_size == 20
    assert updated.ui.instance_name == "OwnerBox"


def test_full_config_serializers_cover_every_config_field(monkeypatch, tmp_path):
    """Mechanism guard for the whole class: both full-config serializers
    (``V2Config.save`` on disk and ``config_to_payload``, the save merge base)
    must emit every persisted field — top-level, every ``UiConfig`` sub-field,
    and every agent backend. A newly-added field hand-listed into only one
    serializer (or neither) fails here, so it cannot silently drop on save."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    config = api.save_config(_full_config_payload())

    ui_field_names = {f.name for f in fields(UiConfig)}
    # ``platform_configs`` is the internal per-platform aggregate; it is emitted
    # under each platform's own key, not as a top-level ``platform_configs`` key.
    top_level = {f.name for f in fields(V2Config)} - {"platform_configs"}
    agents = {"opencode", "claude", "codex", "avault"}

    def _assert_complete(label: str, payload: dict) -> None:
        assert top_level <= set(payload), f"{label} top-level missing: {top_level - set(payload)}"
        assert ui_field_names <= set(payload["ui"]), f"{label} ui missing: {ui_field_names - set(payload['ui'])}"
        assert agents <= set(payload["agents"]), f"{label} agents missing: {agents - set(payload['agents'])}"

    _assert_complete("config_to_payload", api.config_to_payload(config))

    import json

    from config import paths

    _assert_complete("V2Config.save", json.loads(paths.get_config_path().read_text(encoding="utf-8")))
