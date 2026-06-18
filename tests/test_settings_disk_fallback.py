"""Regression tests for the Settings → Backends disk-fallback reads.

Page-feedback fix for PR #282: the user configured their Claude /
Codex relay by hand-editing ``~/.claude/settings.json`` /
``~/.codex/config.toml`` (or a prior tool did), but the Settings UI
rendered the API Key input as empty and the Base URL as the default.
The fixes:

1. ``vibe.codex_config.read_codex_auth_state`` now honours the
   top-level ``model_provider`` key when looking up ``base_url`` — a
   user-defined ``[model_providers.OpenAI]`` section is no longer
   ignored just because the key happens to be TitleCase rather than
   our managed lowercase ``openai``.

2. ``vibe.api.get_claude_auth`` falls back to
   ``~/.claude/settings.json`` env values (``ANTHROPIC_API_KEY`` /
   ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_BASE_URL``) when V2Config is
   empty, surfacing the live state via ``api_key_masked`` /
   ``base_url`` and tagging the source as ``settings_json``.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from vibe.codex_config import read_codex_auth_state


def test_codex_reads_base_url_from_user_titlecase_provider(tmp_path: Path) -> None:
    """When the user points Codex at a relay via ``[model_providers.OpenAI]``
    (matching their on-disk config.toml literally), the Settings UI must
    pick up that ``base_url`` rather than reporting the default.
    """
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("{}", encoding="utf-8")
    (codex_dir / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "OpenAI"',
                "",
                "[model_providers.OpenAI]",
                'name = "OpenAI"',
                'base_url = "https://ai-relay.chainbot.io"',
                'wire_api = "responses"',
                "requires_openai_auth = true",
            ]
        ),
        encoding="utf-8",
    )
    state = read_codex_auth_state(home=tmp_path)
    assert state["base_url"] == "https://ai-relay.chainbot.io"


def test_codex_falls_back_to_managed_section_when_no_active_provider(tmp_path: Path) -> None:
    """If ``model_provider`` is unset but our managed ``openai`` section
    has a ``base_url`` (the shape we write via ``apply_codex_auth``),
    surface that. Preserves the pre-fix behaviour for vibe-initiated
    saves that never touched the top-level ``model_provider`` key.
    """
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("{}", encoding="utf-8")
    (codex_dir / "config.toml").write_text(
        "\n".join(
            [
                "[model_providers.openai]",
                'base_url = "https://vibe-managed.example.io"',
            ]
        ),
        encoding="utf-8",
    )
    state = read_codex_auth_state(home=tmp_path)
    assert state["base_url"] == "https://vibe-managed.example.io"


def test_codex_no_base_url_when_neither_section_has_one(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("{}", encoding="utf-8")
    (codex_dir / "config.toml").write_text("model = \"gpt-5.4\"\n", encoding="utf-8")
    state = read_codex_auth_state(home=tmp_path)
    assert state["base_url"] is None


def _write_claude_settings(home: Path, env: dict) -> None:
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({"env": env}), encoding="utf-8"
    )


def _assert_claude_managed_env(env: dict) -> None:
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"


def test_claude_oauth_state_reads_current_dot_credentials_file(tmp_path: Path) -> None:
    """Claude Code 2.x writes OAuth tokens to ``.credentials.json`` on Linux.

    The Settings page must treat that as a real OAuth login; otherwise the
    user sees "Not signed in" immediately after a successful OAuth flow.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-token",
                    "refreshToken": "refresh-token",
                }
            }
        ),
        encoding="utf-8",
    )

    from vibe.claude_config import read_claude_oauth_signed_in

    assert read_claude_oauth_signed_in(home=tmp_path) is True


def test_get_claude_auth_prefers_cli_oauth_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Settings page should trust Claude Code's own auth status first.

    This covers keychain-backed installs and avoids depending solely on the
    exact credentials filename used by a specific Claude Code release.
    """
    _write_claude_settings(tmp_path, {})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)

    class _FakeAgent:
        auth_mode = "oauth"
        api_key = None
        base_url = None
        cli_path = "claude"

    class _FakeAgents:
        claude = _FakeAgent()

    class _FakeRuntime:
        default_cwd = str(tmp_path / "runtime")

    class _FakeConfig:
        agents = _FakeAgents()
        runtime = _FakeRuntime()

    monkeypatch.setenv("PATH", "/fake/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stale-shell")
    monkeypatch.setattr("vibe.api.resolve_cli_path", lambda _binary: None)

    def fake_run(cmd, **kwargs):
        assert cmd == ["claude", "auth", "status", "--json"]
        env = kwargs.get("env") or {}
        assert env.get("PATH") == "/fake/bin"
        assert "ANTHROPIC_API_KEY" not in env
        assert kwargs.get("cwd") == str(tmp_path / "runtime")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"loggedIn": true, "authMethod": "claude.ai"}',
            stderr="",
        )

    monkeypatch.setattr("vibe.api.load_config", lambda: _FakeConfig())
    monkeypatch.setattr("vibe.api.subprocess.run", fake_run)

    from vibe.api import get_claude_auth

    state = get_claude_auth()

    assert state["has_oauth_credentials"] is True
    assert state["active_auth_mode"] == "oauth"
    assert (tmp_path / "runtime").is_dir()


def test_get_claude_auth_resolves_default_claude_cli_before_status_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_settings(tmp_path, {})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)

    resolved_cli = tmp_path / ".claude" / "local" / "claude"
    resolved_cli.parent.mkdir(parents=True)
    resolved_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved_cli.chmod(0o755)

    class _FakeAgent:
        auth_mode = "oauth"
        api_key = None
        base_url = None
        cli_path = "claude"

    class _FakeAgents:
        claude = _FakeAgent()

    class _FakeConfig:
        agents = _FakeAgents()

    def fake_run(cmd, **_kwargs):
        assert cmd == [str(resolved_cli), "auth", "status", "--json"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"loggedIn": true, "authMethod": "claude.ai"}',
            stderr="",
        )

    monkeypatch.setattr("vibe.api.Path.home", lambda: tmp_path)
    monkeypatch.setattr("vibe.api.shutil.which", lambda _binary: None)
    monkeypatch.setattr("vibe.api.load_config", lambda: _FakeConfig())
    monkeypatch.setattr("vibe.api.subprocess.run", fake_run)

    from vibe.api import get_claude_auth

    state = get_claude_auth()

    assert state["has_oauth_credentials"] is True
    assert state["active_auth_mode"] == "oauth"


def test_get_claude_auth_ignores_cli_api_key_status_for_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_settings(tmp_path, {})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)

    class _FakeAgent:
        auth_mode = "oauth"
        api_key = None
        base_url = None
        cli_path = "claude"

    class _FakeAgents:
        claude = _FakeAgent()

    class _FakeConfig:
        agents = _FakeAgents()

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"loggedIn": true, "authMethod": "api-key"}',
            stderr="",
        )

    monkeypatch.setattr("vibe.api.load_config", lambda: _FakeConfig())
    monkeypatch.setattr("vibe.api.subprocess.run", fake_run)

    from vibe.api import get_claude_auth

    state = get_claude_auth()

    assert state["has_oauth_credentials"] is False
    assert state["active_auth_mode"] == "none"


def test_get_claude_auth_suppresses_stale_disk_oauth_for_concrete_non_oauth_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_settings(tmp_path, {})
    claude_dir = tmp_path / ".claude"
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "stale-oauth",
                    "refreshToken": "stale-refresh",
                    "expiresAt": 4_102_444_800_000,
                    "scopes": ["user:inference"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)

    class _FakeAgent:
        auth_mode = "oauth"
        api_key = None
        base_url = None
        cli_path = "claude"

    class _FakeAgents:
        claude = _FakeAgent()

    class _FakeConfig:
        agents = _FakeAgents()

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"loggedIn": true, "authMethod": "third_party"}',
            stderr="",
        )

    monkeypatch.setattr("vibe.api.load_config", lambda: _FakeConfig())
    monkeypatch.setattr("vibe.api.subprocess.run", fake_run)

    from vibe.api import get_claude_auth

    state = get_claude_auth()

    assert state["has_oauth_credentials"] is False
    assert state["active_auth_mode"] == "none"


def test_get_claude_auth_treats_setup_token_status_as_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_claude_settings(tmp_path, {})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)

    class _FakeAgent:
        auth_mode = "oauth"
        api_key = None
        base_url = None
        cli_path = "claude"

    class _FakeAgents:
        claude = _FakeAgent()

    class _FakeConfig:
        agents = _FakeAgents()

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"loggedIn": true, "authMethod": "oauth_token"}',
            stderr="",
        )

    monkeypatch.setattr("vibe.api.load_config", lambda: _FakeConfig())
    monkeypatch.setattr("vibe.api.subprocess.run", fake_run)

    from vibe.api import get_claude_auth

    state = get_claude_auth()

    assert state["has_oauth_credentials"] is True
    assert state["active_auth_mode"] == "oauth"


def test_claude_settings_json_auth_token_surfaces_in_get_claude_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude's get_claude_auth must report a masked key + base URL when
    V2Config is empty but ``settings.json`` carries them. Mirrors the
    regression env where the user pre-configured a relay via the env
    block before our Settings UI existed.
    """
    _write_claude_settings(
        tmp_path,
        {
            "ANTHROPIC_AUTH_TOKEN": "sk-9c552_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_738a",
            "ANTHROPIC_BASE_URL": "https://ai-relay.chainbot.io",
        },
    )
    # Pin Claude's home + V2Config home to the temp dir so the readers
    # find our fake settings.json and the V2Config load returns defaults
    # (i.e. ``api_key=None``).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    monkeypatch.setattr("vibe.api._read_claude_cli_oauth_signed_in", lambda _path, **_kwargs: None)

    from vibe.api import get_claude_auth

    state = get_claude_auth()
    assert state["has_api_key"] is True
    assert state["api_key_source"] == "settings_json"
    masked = state["api_key_masked"]
    assert masked is not None
    assert masked.endswith("738a")
    assert "xxxxxxxx" not in masked  # plaintext middle must not leak
    assert state["base_url"] == "https://ai-relay.chainbot.io"
    assert state["active_auth_mode"] == "api_key"
    # settings.json alone is not a *conflict*; it's only a conflict when
    # BOTH V2Config and settings.json carry a key.
    assert state["settings_conflict"] is False


def test_claude_api_key_mode_wins_over_stale_oauth_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-key mode is mutually exclusive in Avibe's active auth state.

    Claude Code may keep account tokens in its own credentials file after
    the user switches Avibe to API-key mode. That raw token signal must not
    make the Settings UI or runtime treat OAuth as the active source.
    """
    _write_claude_settings(
        tmp_path,
        {
            "ANTHROPIC_API_KEY": "sk-ant-active-settings-key",
            "ANTHROPIC_BASE_URL": "https://relay.example.invalid",
        },
    )
    claude_dir = tmp_path / ".claude"
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "stale-oauth",
                    "refreshToken": "stale-refresh",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    monkeypatch.setattr("vibe.api._read_claude_cli_oauth_signed_in", lambda _path, **_kwargs: None)

    class _FakeAgent:
        auth_mode = "api_key"
        api_key = None
        base_url = None
        cli_path = "claude"

    class _FakeAgents:
        claude = _FakeAgent()

    class _FakeConfig:
        agents = _FakeAgents()

    monkeypatch.setattr("vibe.api.load_config", lambda: _FakeConfig())

    from vibe.api import get_claude_auth

    state = get_claude_auth()

    assert state["auth_mode"] == "api_key"
    assert state["active_auth_mode"] == "api_key"
    assert state["has_api_key"] is True
    assert state["has_oauth_credentials"] is True


def test_claude_settings_json_takes_precedence_over_legacy_v2config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings.json wins over legacy V2Config because Claude Code layers
    it on top of inherited env at launch."""
    _write_claude_settings(
        tmp_path,
        {"ANTHROPIC_API_KEY": "sk-stale-key-from-settings"},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    monkeypatch.setattr("vibe.api._read_claude_cli_oauth_signed_in", lambda _path, **_kwargs: None)

    # Inject a V2Config with a fresh key by patching load_config.
    class _FakeAgent:
        auth_mode = "api_key"
        api_key = "sk-fresh-key-from-v2config"
        base_url = "https://v2config.example.io"

    class _FakeAgents:
        claude = _FakeAgent()

    class _FakeConfig:
        agents = _FakeAgents()

    monkeypatch.setattr("vibe.api.load_config", lambda: _FakeConfig())

    from vibe.api import get_claude_auth

    state = get_claude_auth()
    assert state["api_key_source"] == "settings_json"
    masked = state["api_key_masked"]
    assert masked is not None
    assert masked.endswith("ings")
    assert state["base_url"] == "https://v2config.example.io"
    assert state["settings_conflict"] is False


def test_save_claude_auth_writes_settings_json_and_clears_v2_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    monkeypatch.setattr("vibe.api._read_claude_cli_oauth_signed_in", lambda _path, **_kwargs: None)
    cleanup_calls: list[bool] = []
    monkeypatch.setattr(
        "vibe.api._clear_claude_oauth_credentials_after_api_key_save",
        lambda _service=None: cleanup_calls.append(True) or {"ok": True},
    )

    from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config
    from vibe.api import get_claude_auth, save_claude_auth

    cfg = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )
    cfg.agents.claude.auth_mode = "api_key"
    cfg.agents.claude.api_key = "sk-old-v2-key"
    cfg.agents.claude.base_url = "https://old.example.invalid"
    cfg.save()

    result = save_claude_auth(
        {
            "auth_mode": "api_key",
            "api_key": "sk-ant-new-settings-key",
            "base_url": "https://relay.example.invalid",
        }
    )

    assert result["ok"] is True
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"] == "sk-ant-new-settings-key"
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://relay.example.invalid"
    _assert_claude_managed_env(settings["env"])

    saved = V2Config.load()
    assert saved.agents.claude.api_key is None
    assert saved.agents.claude.base_url is None
    assert saved.agents.claude.auth_mode == "api_key"
    assert saved.agents.claude.auth_mode_set is True

    state = get_claude_auth()
    assert state["api_key_source"] == "settings_json"
    assert state["base_url"] == "https://relay.example.invalid"
    assert cleanup_calls == [True]


def test_save_claude_auth_reports_partial_when_oauth_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    monkeypatch.setattr("vibe.api._read_claude_cli_oauth_signed_in", lambda _path, **_kwargs: None)
    monkeypatch.setattr(
        "vibe.api._clear_claude_oauth_credentials_after_api_key_save",
        lambda _service=None: {
            "ok": True,
            "partial": True,
            "warning": "oauth_cleanup_failed",
            "detail": "claude auth logout exited non-zero",
        },
    )

    from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config
    from vibe.api import save_claude_auth

    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()

    result = save_claude_auth(
        {
            "auth_mode": "api_key",
            "api_key": "sk-ant-new-settings-key",
            "base_url": "https://relay.example.invalid",
        }
    )

    assert result["ok"] is True
    assert result["active_auth_mode"] == "api_key"
    assert result["partial"] is True
    assert result["warning"] == "oauth_cleanup_failed"
    assert "logout" in result["detail"]
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"] == "sk-ant-new-settings-key"
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://relay.example.invalid"


def test_save_claude_auth_restores_pending_oauth_backup_before_writing_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    monkeypatch.setattr("vibe.api._read_claude_cli_oauth_signed_in", lambda _path, **_kwargs: None)

    from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config
    from vibe.api import save_claude_auth
    from vibe.claude_config import (
        read_claude_settings_env,
        write_claude_oauth_settings_backup,
    )

    write_claude_oauth_settings_backup(
        {
            "ANTHROPIC_API_KEY": "sk-old-backup",
            "ANTHROPIC_BASE_URL": "https://old-backup.example.invalid",
        }
    )

    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()

    cleanup_calls: list[object] = []
    monkeypatch.setattr(
        "vibe.api._clear_claude_oauth_credentials_after_api_key_save",
        lambda service=None: cleanup_calls.append(service) or {"ok": True},
    )

    result = save_claude_auth(
        {
            "auth_mode": "api_key",
            "api_key": "sk-new-key",
            "base_url": "https://new-relay.example.invalid",
        }
    )

    assert result["ok"] is True
    assert cleanup_calls and cleanup_calls[0] is not None
    assert read_claude_settings_env() == {
        "ANTHROPIC_API_KEY": "sk-new-key",
        "ANTHROPIC_BASE_URL": "https://new-relay.example.invalid",
    }


def test_save_claude_auth_keeps_settings_token_over_legacy_v2_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    _write_claude_settings(
        tmp_path,
        {
            "ANTHROPIC_AUTH_TOKEN": "token-from-settings",
            "ANTHROPIC_BASE_URL": "https://old-relay.example.invalid",
        },
    )

    from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config
    from vibe.api import save_claude_auth

    cfg = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )
    cfg.agents.claude.auth_mode = "api_key"
    cfg.agents.claude.api_key = "sk-legacy-v2-key"
    cfg.agents.claude.base_url = "https://legacy-v2.example.invalid"
    cfg.save()

    result = save_claude_auth(
        {
            "auth_mode": "api_key",
            "api_key": "",
            "base_url": "https://new-relay.example.invalid",
        }
    )

    assert result["ok"] is True
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "token-from-settings"
    assert "ANTHROPIC_API_KEY" not in settings["env"]
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://new-relay.example.invalid"
    _assert_claude_managed_env(settings["env"])


def test_apply_claude_auth_oauth_removes_auth_env_but_keeps_managed_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    _write_claude_settings(
        tmp_path,
        {
            "ANTHROPIC_API_KEY": "sk-stale",
            "ANTHROPIC_BASE_URL": "https://stale-relay.example.invalid",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0",
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "1",
            "CUSTOM_FLAG": "keep-me",
        },
    )

    from vibe.claude_config import apply_claude_auth

    apply_claude_auth(auth_mode="oauth", api_key=None, base_url=None)

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "ANTHROPIC_API_KEY" not in settings["env"]
    assert "ANTHROPIC_BASE_URL" not in settings["env"]
    assert settings["env"]["CUSTOM_FLAG"] == "keep-me"
    _assert_claude_managed_env(settings["env"])


def test_save_claude_auth_fails_without_overwriting_malformed_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text('{"model": "claude-sonnet", ', encoding="utf-8")

    from vibe.api import save_claude_auth

    result = save_claude_auth(
        {
            "auth_mode": "api_key",
            "api_key": "sk-new-key",
            "base_url": "https://relay.example.invalid",
        }
    )

    assert result["ok"] is False
    assert "Expecting property name" in result["message"]
    assert settings_path.read_text(encoding="utf-8") == '{"model": "claude-sonnet", '


def test_apply_claude_auth_uses_unique_temp_files_for_concurrent_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr("config.paths._home", lambda: tmp_path, raising=False)

    from vibe.claude_config import apply_claude_auth

    errors: list[BaseException] = []

    def write_key(index: int) -> None:
        try:
            apply_claude_auth(
                auth_mode="api_key",
                api_key=f"sk-key-{index}",
                base_url=f"https://relay-{index}.example.invalid",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write_key, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"].startswith("sk-key-")
    _assert_claude_managed_env(settings["env"])
