from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


def _load_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "prepare_regression.py"
    spec = importlib.util.spec_from_file_location("prepare_regression", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "ANTHROPIC_BASE_URL": "https://anthropic.example/v1",
        "OPENAI_API_KEY": "sk-openai-test",
        "OPENAI_BASE_URL": "https://openai.example",
        "OPENAI_API_BASE": "https://openai.example/v1",
        "REGRESSION_UI_HOST": "192.168.2.3",
        "REGRESSION_DEFAULT_CWD": "/home/avibe/.avibe/workdir",
        "REGRESSION_DEFAULT_BACKEND": "opencode",
        "REGRESSION_LOG_LEVEL": "DEBUG",
        "REGRESSION_LANGUAGE": "en",
        "REGRESSION_CLAUDE_BASE_URL": "https://ai-relay.example",
        "REGRESSION_CLAUDE_AUTH_TOKEN": "sk-claude-auth-token",
        "REGRESSION_CLAUDE_ATTRIBUTION_HEADER": "0",
        "REGRESSION_CODEX_MODEL": "gpt-5.4",
        "REGRESSION_CODEX_REVIEW_MODEL": "gpt-5.4",
        "REGRESSION_CODEX_REASONING_EFFORT": "xhigh",
        "REGRESSION_CODEX_BASE_URL": "https://ai-relay.example",
        "REGRESSION_CODEX_OPENAI_API_KEY": "sk-codex-openai",
        "REGRESSION_OPENCODE_OPENAI_BASE_URL": "https://ai-relay.example/v1",
        "REGRESSION_OPENCODE_OPENAI_API_KEY": "sk-opencode-openai",
        "REGRESSION_OPENCODE_ANTHROPIC_BASE_URL": "https://ai-relay.example/v1",
        "REGRESSION_OPENCODE_ANTHROPIC_API_KEY": "sk-opencode-anthropic",
        "REGRESSION_SLACK_BOT_TOKEN": "xoxb-test-token",
        "REGRESSION_SLACK_APP_TOKEN": "xapp-test-token",
        "REGRESSION_SLACK_CHANNEL": "C123SLACK",
        "REGRESSION_SLACK_BACKEND": "opencode",
        "REGRESSION_DISCORD_BOT_TOKEN": "discord-token-1234567890",
        "REGRESSION_DISCORD_CHANNEL": "123456789012345678",
        "REGRESSION_DISCORD_GUILD_ALLOWLIST": "754776951587340359",
        "REGRESSION_DISCORD_BACKEND": "codex",
        "REGRESSION_FEISHU_APP_ID": "cli_test_app_id",
        "REGRESSION_FEISHU_APP_SECRET": "test-app-secret",
        "REGRESSION_FEISHU_CHAT_ID": "oc_test_chat_id",
        "REGRESSION_FEISHU_BACKEND": "claude",
        "REGRESSION_WECHAT_BACKEND": "opencode",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_prepare_generates_unified_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    module.prepare(tmp_path)

    config = json.loads((tmp_path / "home" / ".avibe" / "config" / "config.json").read_text(encoding="utf-8"))
    settings = json.loads((tmp_path / "home" / ".avibe" / "state" / "settings.json").read_text(encoding="utf-8"))

    # Unified config has all four platforms enabled
    assert config["platforms"]["enabled"] == ["slack", "discord", "lark", "wechat"]
    assert config["platforms"]["primary"] == "slack"

    # All platform credentials populated
    assert config["slack"]["bot_token"] == "xoxb-test-token"
    assert config["discord"]["bot_token"] == "discord-token-1234567890"
    assert "guild_allowlist" not in config["discord"]
    assert config["lark"]["app_id"] == "cli_test_app_id"
    assert config["wechat"]["base_url"] == "https://ilinkai.weixin.qq.com"

    # All three backends enabled
    assert config["agents"]["opencode"]["enabled"] is True
    assert config["agents"]["claude"]["enabled"] is True
    assert config["agents"]["codex"]["enabled"] is True
    assert "default_backend" not in config["agents"]
    assert config["agents"]["opencode"]["cli_path"] == "opencode"
    assert config["agents"]["opencode"]["default_model"] == "gpt-5.4"
    assert config["agents"]["opencode"]["default_provider"] == "openai"

    # UI host propagated
    assert config["ui"]["setup_host"] == "192.168.2.3"
    assert config["runtime"]["default_cwd"] == "/home/avibe/.avibe/workdir"

    # Per-channel routing in settings for each platform
    assert settings["scopes"]["channel"]["slack"]["C123SLACK"]["routing"]["agent_name"] == "opencode"
    assert settings["scopes"]["channel"]["slack"]["C123SLACK"]["custom_cwd"] == "/home/avibe/.avibe/workdir"
    assert settings["scopes"]["channel"]["discord"]["123456789012345678"]["routing"]["agent_name"] == "codex"
    assert settings["schema_version"] == 5
    assert settings["scopes"]["guild"]["discord"]["754776951587340359"]["enabled"] is True
    assert settings["scopes"]["guild_policy"]["discord"]["default_enabled"] is False
    assert settings["scopes"]["channel"]["lark"]["oc_test_chat_id"]["routing"]["agent_name"] == "claude"

    # WeChat has no channel set, so scope is empty
    assert settings["scopes"]["channel"]["wechat"] == {}

    # Directory structure
    assert (tmp_path / "home" / ".avibe" / "workdir").is_dir()
    assert (tmp_path / "home" / ".avibe" / "state" / "sessions.json").exists()

    # Shared agent home configs
    assert (
        json.loads((tmp_path / "home" / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"][
            "ANTHROPIC_AUTH_TOKEN"
        ]
        == "sk-claude-auth-token"
    )
    codex_config = (tmp_path / "home" / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'model = "gpt-5.4"' in codex_config
    assert 'base_url = "https://ai-relay.example"' in codex_config
    assert "supports_websockets = false" in codex_config
    assert "responses_websockets_v2 = false" in codex_config
    assert "suppress_unstable_features_warning = true" in codex_config
    opencode_config = json.loads(
        (tmp_path / "home" / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert opencode_config["permission"] == "allow"
    assert opencode_config["provider"]["openai"]["options"]["baseURL"] == "https://ai-relay.example/v1"


def test_prepare_derives_openai_relay_base_from_anthropic_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _set_required_env(monkeypatch)
    for key in (
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "REGRESSION_CODEX_BASE_URL",
        "REGRESSION_OPENCODE_OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.example")

    module.prepare(tmp_path)

    codex_config = (tmp_path / "home" / ".codex" / "config.toml").read_text(encoding="utf-8")
    opencode_config = json.loads(
        (tmp_path / "home" / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert 'base_url = "https://relay.example/v1"' in codex_config
    assert "supports_websockets = false" in codex_config
    assert opencode_config["provider"]["openai"]["options"]["baseURL"] == "https://relay.example/v1"


def test_prepare_ignores_legacy_regression_env_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)
    monkeypatch.setenv("THREE_REGRESSION_SLACK_CHANNEL", "C123LEGACY")

    module.prepare(tmp_path)

    settings = json.loads((tmp_path / "home" / ".avibe" / "state" / "settings.json").read_text(encoding="utf-8"))
    assert settings["scopes"]["channel"]["slack"] == {}


def test_prepare_preserves_existing_state_without_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    vibe_dir = tmp_path / "home" / ".avibe"
    (vibe_dir / "config").mkdir(parents=True)
    (vibe_dir / "state").mkdir(parents=True)
    (vibe_dir / "config" / "config.json").write_text('{"keep": true}', encoding="utf-8")
    (vibe_dir / "state" / "settings.json").write_text('{"custom": true}', encoding="utf-8")
    (vibe_dir / "state" / "sessions.json").write_text('{"session": true}', encoding="utf-8")
    shared_home = tmp_path / "home"
    (shared_home / ".claude").mkdir(parents=True)
    (shared_home / ".codex").mkdir(parents=True)
    (shared_home / ".config" / "opencode").mkdir(parents=True)
    (shared_home / ".claude" / "settings.json").write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "keep"}}', encoding="utf-8")
    (shared_home / ".claude.json").write_text('{"keep": true}', encoding="utf-8")
    (shared_home / ".codex" / "config.toml").write_text('model = "keep"\n', encoding="utf-8")
    (shared_home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "keep"}', encoding="utf-8")
    (shared_home / ".config" / "opencode" / "opencode.json").write_text('{"keep": true}', encoding="utf-8")

    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "REGRESSION_SLACK_BOT_TOKEN",
        "REGRESSION_SLACK_APP_TOKEN",
        "REGRESSION_DISCORD_BOT_TOKEN",
        "REGRESSION_FEISHU_APP_ID",
        "REGRESSION_FEISHU_APP_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)

    module.prepare(tmp_path)

    assert json.loads((vibe_dir / "config" / "config.json").read_text(encoding="utf-8")) == {"keep": True}
    assert json.loads((vibe_dir / "state" / "settings.json").read_text(encoding="utf-8")) == {"custom": True}
    assert json.loads((vibe_dir / "state" / "sessions.json").read_text(encoding="utf-8")) == {"session": True}
    assert json.loads((shared_home / ".claude" / "settings.json").read_text(encoding="utf-8")) == {
        "env": {"ANTHROPIC_AUTH_TOKEN": "keep"}
    }
    assert json.loads((shared_home / ".claude.json").read_text(encoding="utf-8")) == {"keep": True}
    assert (shared_home / ".codex" / "config.toml").read_text(encoding="utf-8") == 'model = "keep"\n'
    assert json.loads((shared_home / ".codex" / "auth.json").read_text(encoding="utf-8")) == {"OPENAI_API_KEY": "keep"}
    assert json.loads((shared_home / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8")) == {
        "keep": True
    }


def test_prepare_ignores_legacy_regression_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    old_vibe = tmp_path / "vibe"
    old_shared = tmp_path / "shared-home"
    (old_vibe / "config").mkdir(parents=True)
    (old_vibe / "state").mkdir(parents=True)
    (old_vibe / "workdir").mkdir(parents=True)
    (old_vibe / "config" / "config.json").write_text(
        json.dumps(
            {
                "runtime": {"default_cwd": "/data/vibe_remote/workdir"},
                # Legacy root-global install baked by pre-#545 base images; must be
                # migrated to the user-owned path on a preserved-state update.
                "agents": {"opencode": {"cli_path": "/usr/local/bin/opencode"}},
            }
        ),
        encoding="utf-8",
    )
    (old_vibe / "state" / "settings.json").write_text(
        json.dumps(
            {
                "scopes": {
                    "channel": {
                        "slack": {
                            "C123": {
                                "custom_cwd": "/data/vibe_remote/workdir",
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (old_vibe / "state" / "sessions.json").write_text("{}", encoding="utf-8")
    (old_vibe / "workdir" / "keep.txt").write_text("keep-me", encoding="utf-8")
    (old_shared / ".claude").mkdir(parents=True)
    (old_shared / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

    module.prepare(tmp_path)

    avibe_home = tmp_path / "home" / ".avibe"
    assert old_vibe.exists()
    assert old_shared.exists()
    assert not (tmp_path / "home" / ".vibe_remote").exists()
    assert avibe_home.is_dir()
    assert (old_vibe / "workdir" / "keep.txt").read_text(encoding="utf-8") == "keep-me"
    config = json.loads((avibe_home / "config" / "config.json").read_text(encoding="utf-8"))
    settings = json.loads((avibe_home / "state" / "settings.json").read_text(encoding="utf-8"))
    assert config["runtime"]["default_cwd"] == "/home/avibe/.avibe/workdir"
    assert config["agents"]["opencode"]["cli_path"] == "opencode"
    assert settings["scopes"]["channel"]["slack"]["C123SLACK"]["custom_cwd"] == "/home/avibe/.avibe/workdir"
    assert (tmp_path / "home" / ".claude.json").is_file()


def test_prepare_keeps_avibe_home_authoritative_when_legacy_layout_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _set_required_env(monkeypatch)

    old_vibe = tmp_path / "vibe"
    (old_vibe / "config").mkdir(parents=True)
    (old_vibe / "state").mkdir(parents=True)
    (old_vibe / "workdir").mkdir(parents=True)
    (old_vibe / "config" / "config.json").write_text('{"old": true}', encoding="utf-8")
    (old_vibe / "state" / "settings.json").write_text("{}", encoding="utf-8")
    (old_vibe / "state" / "sessions.json").write_text("{}", encoding="utf-8")
    (old_vibe / "workdir" / "old.txt").write_text("old", encoding="utf-8")
    avibe_home = tmp_path / "home" / ".avibe"
    (avibe_home / "config").mkdir(parents=True)
    (avibe_home / "state").mkdir(parents=True)
    (avibe_home / "config" / "config.json").write_text('{"new": true}', encoding="utf-8")
    (avibe_home / "state" / "settings.json").write_text("{}", encoding="utf-8")
    (avibe_home / "state" / "sessions.json").write_text("{}", encoding="utf-8")

    module.prepare(tmp_path)

    assert old_vibe.exists()
    assert not (tmp_path / "home" / ".vibe_remote").exists()
    assert json.loads((avibe_home / "config" / "config.json").read_text(encoding="utf-8")) == {"new": True}


def test_prepare_repairs_stale_container_paths_inside_avibe_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    vibe_dir = tmp_path / "home" / ".avibe"
    state_dir = vibe_dir / "state"
    (vibe_dir / "config").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (vibe_dir / "config" / "config.json").write_text(
        json.dumps(
            {
                "runtime": {"default_cwd": "/data/vibe_remote/workdir"},
                # Legacy root-global install baked by pre-#545 base images; must be
                # migrated to the user-owned path on a preserved-state update.
                "agents": {"opencode": {"cli_path": "/usr/local/bin/opencode"}},
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "settings.json").write_text(
        json.dumps({"scopes": {"channel": {"slack": {"C123": {"custom_cwd": "/data/vibe_remote/workdir"}}}}}),
        encoding="utf-8",
    )
    (state_dir / "sessions.json").write_text(
        json.dumps(
            {
                "thread_bindings": {
                    "slack_1774535203.606599:/data/vibe_remote/workdir": "ses-old",
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "scheduled_tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {"prompt": "Send ![image](file:///data/vibe_remote/workdir/concert.jpg)"},
                ]
            }
        ),
        encoding="utf-8",
    )
    db_path = state_dir / "vibe.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table agent_sessions (id text primary key, workdir text)")
        conn.execute("create table scope_settings (scope_id text primary key, workdir text, settings_json text)")
        conn.execute(
            "insert into agent_sessions (id, workdir) values (?, ?)",
            ("ses-old", "/data/vibe_remote/workdir"),
        )
        conn.execute(
            "insert into scope_settings (scope_id, workdir, settings_json) values (?, ?, ?)",
            (
                "scope-old",
                "/data/vibe_remote/workdir",
                json.dumps({"custom_cwd": "/data/vibe_remote/workdir"}),
            ),
        )

    module.prepare(tmp_path)

    config = json.loads((vibe_dir / "config" / "config.json").read_text(encoding="utf-8"))
    settings = json.loads((state_dir / "settings.json").read_text(encoding="utf-8"))
    sessions = json.loads((state_dir / "sessions.json").read_text(encoding="utf-8"))
    scheduled_tasks = json.loads((state_dir / "scheduled_tasks.json").read_text(encoding="utf-8"))
    assert config["runtime"]["default_cwd"] == "/home/avibe/.avibe/workdir"
    assert config["agents"]["opencode"]["cli_path"] == "opencode"
    assert settings["scopes"]["channel"]["slack"]["C123"]["custom_cwd"] == "/home/avibe/.avibe/workdir"
    assert "slack_1774535203.606599:/home/avibe/.avibe/workdir" in sessions["thread_bindings"]
    assert "slack_1774535203.606599:/data/vibe_remote/workdir" not in sessions["thread_bindings"]
    assert (
        scheduled_tasks["tasks"][0]["prompt"]
        == "Send ![image](file:///home/avibe/.avibe/workdir/concert.jpg)"
    )

    with sqlite3.connect(db_path) as conn:
        session_workdir = conn.execute("select workdir from agent_sessions where id = 'ses-old'").fetchone()[0]
        scope_workdir, settings_json = conn.execute(
            "select workdir, settings_json from scope_settings where scope_id = 'scope-old'"
        ).fetchone()
    assert session_workdir == "/home/avibe/.avibe/workdir"
    assert scope_workdir == "/home/avibe/.avibe/workdir"
    assert json.loads(settings_json)["custom_cwd"] == "/home/avibe/.avibe/workdir"


def test_prepare_without_reset_still_requires_llm_keys_when_shared_configs_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module()
    _set_required_env(monkeypatch)

    vibe_dir = tmp_path / "home" / ".avibe"
    (vibe_dir / "config").mkdir(parents=True)
    (vibe_dir / "state").mkdir(parents=True)
    (vibe_dir / "config" / "config.json").write_text('{"keep": true}', encoding="utf-8")
    (vibe_dir / "state" / "settings.json").write_text('{"custom": true}', encoding="utf-8")
    (vibe_dir / "state" / "sessions.json").write_text('{"session": true}', encoding="utf-8")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY, OPENAI_API_KEY"):
        module.prepare(tmp_path)


def test_prepare_without_reset_still_requires_platform_envs_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module()
    _set_required_env(monkeypatch)

    vibe_dir = tmp_path / "home" / ".avibe"
    (vibe_dir / "state").mkdir(parents=True)
    (vibe_dir / "state" / "sessions.json").write_text('{"session": true}', encoding="utf-8")
    shared_home = tmp_path / "home"
    (shared_home / ".claude").mkdir(parents=True)
    (shared_home / ".codex").mkdir(parents=True)
    (shared_home / ".config" / "opencode").mkdir(parents=True)
    (shared_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (shared_home / ".claude.json").write_text("{}", encoding="utf-8")
    (shared_home / ".codex" / "config.toml").write_text("", encoding="utf-8")
    (shared_home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (shared_home / ".config" / "opencode" / "opencode.json").write_text("{}", encoding="utf-8")

    monkeypatch.delenv("REGRESSION_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("REGRESSION_SLACK_APP_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="REGRESSION_SLACK_BOT_TOKEN, REGRESSION_SLACK_APP_TOKEN"):
        module.prepare(tmp_path)


def test_prepare_allows_missing_channel_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("REGRESSION_DISCORD_CHANNEL", raising=False)
    monkeypatch.delenv("REGRESSION_FEISHU_CHAT_ID", raising=False)
    monkeypatch.delenv("REGRESSION_WECHAT_CHANNEL", raising=False)

    module.prepare(tmp_path, reset_mode="config")

    settings = json.loads((tmp_path / "home" / ".avibe" / "state" / "settings.json").read_text(encoding="utf-8"))
    assert settings["scopes"]["channel"]["slack"] == {}
    assert settings["scopes"]["channel"]["discord"] == {}
    assert settings["scopes"]["channel"]["lark"] == {}
    assert settings["scopes"]["channel"]["wechat"] == {}


def test_prepare_preserves_discord_denylist_only_guild_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)
    monkeypatch.delenv("REGRESSION_DISCORD_GUILD_ALLOWLIST", raising=False)
    monkeypatch.setenv("REGRESSION_DISCORD_GUILD_DENYLIST", "blocked-guild")

    module.prepare(tmp_path, reset_mode="config")

    settings = json.loads((tmp_path / "home" / ".avibe" / "state" / "settings.json").read_text(encoding="utf-8"))
    assert settings["scopes"]["guild"]["discord"]["blocked-guild"]["enabled"] is False
    assert settings["scopes"]["guild_policy"]["discord"]["default_enabled"] is True


def test_prepare_requires_supported_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _load_module()
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REGRESSION_SLACK_BACKEND", "unknown")

    with pytest.raises(SystemExit, match="REGRESSION_SLACK_BACKEND"):
        module.prepare(tmp_path)


def test_prepare_reset_config_preserves_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    workdir = tmp_path / "home" / ".avibe" / "workdir"
    workdir.mkdir(parents=True)
    (workdir / "keep.txt").write_text("keep-me", encoding="utf-8")
    config_dir = tmp_path / "home" / ".avibe" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text('{"stale": true}', encoding="utf-8")

    module.prepare(tmp_path, reset_mode="config")

    assert (workdir / "keep.txt").read_text(encoding="utf-8") == "keep-me"
    refreshed = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    assert "default_backend" not in refreshed["agents"]


def test_prepare_reset_config_rewrites_shared_agent_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    shared_home = tmp_path / "home"
    (shared_home / ".claude").mkdir(parents=True)
    (shared_home / ".codex").mkdir(parents=True)
    (shared_home / ".config" / "opencode").mkdir(parents=True)
    (shared_home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "stale-token"}}),
        encoding="utf-8",
    )
    (shared_home / ".claude.json").write_text('{"keep": true}', encoding="utf-8")
    (shared_home / ".codex" / "config.toml").write_text('model = "stale-model"\n', encoding="utf-8")
    (shared_home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "stale-key"}', encoding="utf-8")
    (shared_home / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps(
            {
                "provider": {
                    "openai": {"options": {"apiKey": "stale-openai", "baseURL": "https://stale.example/v1"}},
                    "anthropic": {"options": {"apiKey": "stale-anthropic", "baseURL": "https://stale.example/v1"}},
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("REGRESSION_CODEX_MODEL", "gpt-5.4")
    monkeypatch.setenv("REGRESSION_CODEX_OPENAI_API_KEY", "sk-codex-new")
    monkeypatch.setenv("REGRESSION_OPENCODE_OPENAI_API_KEY", "sk-opencode-new")
    monkeypatch.setenv("REGRESSION_OPENCODE_ANTHROPIC_API_KEY", "sk-opencode-anthropic-new")
    monkeypatch.setenv("REGRESSION_OPENCODE_OPENAI_BASE_URL", "https://fresh.example/v1")
    monkeypatch.setenv("REGRESSION_OPENCODE_ANTHROPIC_BASE_URL", "https://fresh.example/v1")
    monkeypatch.setenv("REGRESSION_CLAUDE_AUTH_TOKEN", "sk-claude-fresh")

    module.prepare(tmp_path, reset_mode="config")

    claude_settings = json.loads((shared_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    codex_config = (shared_home / ".codex" / "config.toml").read_text(encoding="utf-8")
    codex_auth = json.loads((shared_home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    opencode_config = json.loads((shared_home / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8"))

    assert claude_settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-claude-fresh"
    assert json.loads((shared_home / ".claude.json").read_text(encoding="utf-8")) == {"keep": True}
    assert 'model = "gpt-5.4"' in codex_config
    assert codex_auth["OPENAI_API_KEY"] == "sk-codex-new"
    assert opencode_config["provider"]["openai"]["options"]["apiKey"] == "sk-opencode-new"
    assert opencode_config["provider"]["openai"]["options"]["baseURL"] == "https://fresh.example/v1"
    assert opencode_config["provider"]["anthropic"]["options"]["apiKey"] == "sk-opencode-anthropic-new"
    assert opencode_config["provider"]["anthropic"]["options"]["baseURL"] == "https://fresh.example/v1"


def test_prepare_replaces_docker_created_claude_json_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    shared_home = tmp_path / "home"
    (shared_home / ".claude").mkdir(parents=True)
    (shared_home / ".codex").mkdir(parents=True)
    (shared_home / ".config" / "opencode").mkdir(parents=True)
    (shared_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (shared_home / ".claude.json").mkdir(parents=True)
    (shared_home / ".codex" / "config.toml").write_text("", encoding="utf-8")
    (shared_home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (shared_home / ".config" / "opencode" / "opencode.json").write_text("{}", encoding="utf-8")

    module.prepare(tmp_path)

    claude_state_path = shared_home / ".claude.json"
    assert claude_state_path.is_file()
    assert json.loads(claude_state_path.read_text(encoding="utf-8")) == {}


def test_prepare_reset_all_clears_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)

    workdir = tmp_path / "home" / ".avibe" / "workdir"
    workdir.mkdir(parents=True)
    (workdir / "drop.txt").write_text("remove-me", encoding="utf-8")

    module.prepare(tmp_path, reset_mode="all")

    assert not (workdir / "drop.txt").exists()


def test_prepare_ignores_default_backend_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REGRESSION_DEFAULT_BACKEND", "claude")

    module.prepare(tmp_path, reset_mode="config")

    config = json.loads((tmp_path / "home" / ".avibe" / "config" / "config.json").read_text(encoding="utf-8"))
    assert "default_backend" not in config["agents"]


def test_prepare_all_platform_channel_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REGRESSION_WECHAT_CHANNEL", "wx_test_room")
    monkeypatch.setenv("REGRESSION_WECHAT_BACKEND", "codex")

    module.prepare(tmp_path, reset_mode="config")

    settings = json.loads((tmp_path / "home" / ".avibe" / "state" / "settings.json").read_text(encoding="utf-8"))
    assert settings["scopes"]["channel"]["wechat"]["wx_test_room"]["routing"]["agent_name"] == "codex"
    assert settings["scopes"]["channel"]["slack"]["C123SLACK"]["routing"]["agent_name"] == "opencode"
