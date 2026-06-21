"""Unit tests for ``build_claude_subprocess_env``.

This helper centralises the Anthropic / Claude env composition for both
the one-shot CLI launch path (``core/handlers/session_handler.py``) and
the control-channel SDK client path (``core/agent_auth_service.py``).

Without this single source of truth, a future env-injection site can
silently drift back into the bug Codex flagged in PR #282 round 3:
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` leaking through even
though the user picked ``auth_mode = oauth`` in Settings.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vibe.claude_config import (
    build_claude_subprocess_env,
    clear_claude_oauth_settings_backup,
    get_claude_oauth_settings_backup_path,
    read_claude_oauth_settings_backup,
    read_claude_settings_env,
    restore_claude_settings_env,
    write_claude_oauth_settings_backup,
)


def _cfg(**kwargs) -> SimpleNamespace:
    """Build the minimal duck-typed V2Config.claude block the helper reads."""
    return SimpleNamespace(**kwargs)


def _empty_claude_home(tmp_path, monkeypatch) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))


def test_oauth_strips_inherited_api_key_and_auth_token() -> None:
    env = {
        "ANTHROPIC_API_KEY": "sk-leaked",
        "ANTHROPIC_AUTH_TOKEN": "bearer-leaked",
        "ANTHROPIC_BASE_URL": "https://shell.example",
        "CLAUDE_CONFIG_DIR": "/keep",
        "PATH": "/dropped",
    }
    out = build_claude_subprocess_env(_cfg(auth_mode="oauth", auth_mode_set=True), base_env=env)
    assert "ANTHROPIC_API_KEY" not in out
    assert "ANTHROPIC_AUTH_TOKEN" not in out
    assert "ANTHROPIC_BASE_URL" not in out
    # Non-credential CLAUDE_ vars and unrelated keys behave as before:
    # inherit only the namespaced ones, never PATH.
    assert out["CLAUDE_CONFIG_DIR"] == "/keep"
    assert "PATH" not in out


def test_force_oauth_bypasses_api_key_mode_settings_and_config(
    tmp_path, monkeypatch
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        '{"env":{"ANTHROPIC_API_KEY":"sk-settings","ANTHROPIC_AUTH_TOKEN":"bearer-settings","ANTHROPIC_BASE_URL":"https://settings.example"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    env = {
        "ANTHROPIC_API_KEY": "sk-shell",
        "ANTHROPIC_AUTH_TOKEN": "bearer-shell",
        "ANTHROPIC_BASE_URL": "https://shell.example",
        "CLAUDE_CONFIG_DIR": str(claude_home),
    }

    out = build_claude_subprocess_env(
        _cfg(
            auth_mode="api_key",
            auth_mode_set=True,
            api_key="sk-configured",
            base_url="https://configured.example",
        ),
        base_env=env,
        force_oauth=True,
    )

    assert "ANTHROPIC_API_KEY" not in out
    assert "ANTHROPIC_AUTH_TOKEN" not in out
    assert "ANTHROPIC_BASE_URL" not in out
    assert out["CLAUDE_CONFIG_DIR"] == str(claude_home)


def test_restore_claude_settings_env_preserves_base_url_only(tmp_path, monkeypatch) -> None:
    _empty_claude_home(tmp_path, monkeypatch)

    restore_claude_settings_env({"ANTHROPIC_BASE_URL": "https://relay.example"})

    assert read_claude_settings_env() == {
        "ANTHROPIC_BASE_URL": "https://relay.example",
    }


def test_claude_oauth_settings_backup_round_trips_relevant_env_only(
    tmp_path, monkeypatch
) -> None:
    _empty_claude_home(tmp_path, monkeypatch)

    write_claude_oauth_settings_backup(
        {
            "ANTHROPIC_API_KEY": " sk-old ",
            "ANTHROPIC_BASE_URL": "https://relay.example",
            "UNRELATED": "ignored",
        }
    )

    assert read_claude_oauth_settings_backup() == {
        "ANTHROPIC_API_KEY": "sk-old",
        "ANTHROPIC_BASE_URL": "https://relay.example",
    }
    assert get_claude_oauth_settings_backup_path().exists()

    clear_claude_oauth_settings_backup()

    assert read_claude_oauth_settings_backup() is None
    assert not get_claude_oauth_settings_backup_path().exists()


def test_api_key_mode_injects_configured_key_and_drops_bearer(tmp_path, monkeypatch) -> None:
    _empty_claude_home(tmp_path, monkeypatch)
    env = {
        "ANTHROPIC_API_KEY": "sk-stale",
        "ANTHROPIC_AUTH_TOKEN": "bearer-stale",
    }
    out = build_claude_subprocess_env(
        _cfg(auth_mode="api_key", api_key="sk-new"),
        base_env=env,
    )
    assert out["ANTHROPIC_API_KEY"] == "sk-new"
    # An explicit api_key + an inherited AUTH_TOKEN would let the SDK
    # pick the wrong Authorization header; the helper must drop the
    # token so the api_key wins unambiguously.
    assert "ANTHROPIC_AUTH_TOKEN" not in out


def test_api_key_mode_preserves_settings_json_auth_token_semantics(
    tmp_path, monkeypatch
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        '{"env":{"ANTHROPIC_AUTH_TOKEN":"bearer-settings","ANTHROPIC_BASE_URL":"https://relay.example"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    out = build_claude_subprocess_env(
        _cfg(auth_mode="api_key", api_key="", auth_mode_set=True),
        base_env={"ANTHROPIC_API_KEY": "sk-shell-stale"},
    )

    assert "ANTHROPIC_API_KEY" not in out
    assert out["ANTHROPIC_AUTH_TOKEN"] == "bearer-settings"
    assert out["ANTHROPIC_BASE_URL"] == "https://relay.example"


def test_api_key_mode_without_configured_key_keeps_inherited(tmp_path, monkeypatch) -> None:
    _empty_claude_home(tmp_path, monkeypatch)
    # If the user picked api_key mode but left the field blank, fall
    # back to whatever the shell exported; this matches the existing
    # session_handler semantics before the auth_mode toggle existed.
    env = {"ANTHROPIC_API_KEY": "sk-from-shell"}
    out = build_claude_subprocess_env(
        _cfg(auth_mode="api_key", api_key=""),
        base_env=env,
    )
    assert out["ANTHROPIC_API_KEY"] == "sk-from-shell"


def test_base_url_override_overrides_inherited(tmp_path, monkeypatch) -> None:
    _empty_claude_home(tmp_path, monkeypatch)
    env = {"ANTHROPIC_BASE_URL": "https://shell.example"}
    out = build_claude_subprocess_env(
        _cfg(auth_mode="api_key", api_key="sk-test", base_url="https://relay.example/v1"),
        base_env=env,
    )
    assert out["ANTHROPIC_BASE_URL"] == "https://relay.example/v1"


def test_base_url_blank_does_not_override_inherited() -> None:
    env = {"ANTHROPIC_BASE_URL": "https://shell.example"}
    out = build_claude_subprocess_env(
        _cfg(auth_mode="oauth", base_url="   "),
        base_env=env,
    )
    assert out["ANTHROPIC_BASE_URL"] == "https://shell.example"


def test_no_config_returns_inherited_subset() -> None:
    env = {
        "ANTHROPIC_API_KEY": "sk-shell",
        "CLAUDE_CONFIG_DIR": "/dir",
        "PATH": "/skip",
    }
    out = build_claude_subprocess_env(None, base_env=env)
    assert out == {
        "ANTHROPIC_API_KEY": "sk-shell",
        "CLAUDE_CONFIG_DIR": "/dir",
    }


@pytest.mark.parametrize(
    "auth_mode",
    ["", "unknown", None],
)
def test_unknown_auth_mode_leaves_inherited_unchanged(auth_mode) -> None:
    # The helper only acts on the two known modes. Anything else (legacy
    # configs, future modes, missing field) falls through with the
    # inherited env preserved — preferable to silently stripping vars on
    # configs we don't understand.
    env = {"ANTHROPIC_API_KEY": "sk-shell", "ANTHROPIC_AUTH_TOKEN": "bearer"}
    out = build_claude_subprocess_env(_cfg(auth_mode=auth_mode), base_env=env)
    assert out["ANTHROPIC_API_KEY"] == "sk-shell"
    assert out["ANTHROPIC_AUTH_TOKEN"] == "bearer"


def test_oauth_with_no_inherited_keys_returns_clean_env() -> None:
    env = {"PATH": "/x"}
    out = build_claude_subprocess_env(_cfg(auth_mode="oauth"), base_env=env)
    assert out == {}
