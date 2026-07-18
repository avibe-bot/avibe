#!/usr/bin/env python3
"""Prepare generated config/state for the unified Incus regression environment.

Generates a single config.json with all four IM platforms enabled and all
three agent backends configured, plus per-channel routing in settings.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path


PLATFORM_DEFS = {
    "slack": {
        "platform": "slack",
        "channel_env": "REGRESSION_SLACK_CHANNEL",
        "backend_env": "REGRESSION_SLACK_BACKEND",
        "required_envs": (
            "REGRESSION_SLACK_BOT_TOKEN",
            "REGRESSION_SLACK_APP_TOKEN",
        ),
    },
    "discord": {
        "platform": "discord",
        "channel_env": "REGRESSION_DISCORD_CHANNEL",
        "backend_env": "REGRESSION_DISCORD_BACKEND",
        "required_envs": ("REGRESSION_DISCORD_BOT_TOKEN",),
    },
    "feishu": {
        "platform": "lark",
        "channel_env": "REGRESSION_FEISHU_CHAT_ID",
        "backend_env": "REGRESSION_FEISHU_BACKEND",
        "required_envs": (
            "REGRESSION_FEISHU_APP_ID",
            "REGRESSION_FEISHU_APP_SECRET",
        ),
    },
    "wechat": {
        "platform": "wechat",
        "channel_env": "REGRESSION_WECHAT_CHANNEL",
        "backend_env": "REGRESSION_WECHAT_BACKEND",
        "required_envs": (),  # bot_token obtained via QR login, not env
    },
}

SUPPORTED_BACKENDS = {"opencode", "claude", "codex"}
RESET_MODES = {"none", "config", "all"}
CONTAINER_HOME = Path("/home/avibe")
CONTAINER_AVIBE_HOME = CONTAINER_HOME / ".avibe"
DEFAULT_CWD = str(CONTAINER_AVIBE_HOME / "workdir")
# OpenCode is a bare PATH-resolved name (the system default; claude/codex too).
# The service PATH prefers ~/.local/bin, where build-base installs the user-owned,
# self-updatable binary; on a not-yet-rebuilt base it still resolves the old
# /usr/local/bin copy. Bare avoids pinning an absolute path that may be absent on
# a given instance (the cause of breakage across base-image generations).
CONTAINER_OPENCODE_CLI = "opencode"
ENV_PREFIX = "REGRESSION_"


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    if value is None:
        value = default
    return value.strip()


def _optional(key: str) -> str | None:
    value = _env(key)
    return value or None


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def _require_envs(keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if not _env(key)]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Missing required environment variables: {joined}")


def _platform_prefix(name: str) -> str:
    return f"REGRESSION_{name.upper()}"


def _build_routing(name: str) -> dict:
    prefix = _platform_prefix(name)
    backend = _env(f"{prefix}_BACKEND")
    if backend and backend not in SUPPORTED_BACKENDS:
        allowed = ", ".join(sorted(SUPPORTED_BACKENDS))
        raise SystemExit(f"{prefix}_BACKEND must be one of: {allowed}")

    return {
        "agent_name": backend or None,
        "opencode_agent": _optional(f"{prefix}_OPENCODE_AGENT"),
        "opencode_model": _optional(f"{prefix}_OPENCODE_MODEL"),
        "opencode_reasoning_effort": _optional(f"{prefix}_OPENCODE_REASONING_EFFORT"),
        "claude_agent": _optional(f"{prefix}_CLAUDE_AGENT"),
        "claude_model": _optional(f"{prefix}_CLAUDE_MODEL"),
        "codex_agent": _optional(f"{prefix}_CODEX_AGENT"),
        "codex_model": _optional(f"{prefix}_CODEX_MODEL"),
        "codex_reasoning_effort": _optional(f"{prefix}_CODEX_REASONING_EFFORT"),
    }


def _default_cwd() -> str:
    return _normalize_stale_container_path(_env("REGRESSION_DEFAULT_CWD", DEFAULT_CWD))


def _ui_host() -> str:
    return _env("REGRESSION_UI_HOST", "127.0.0.1")


def _build_slack_payload() -> dict:
    prefix = _platform_prefix("slack")
    require_mention = _parse_bool(_env(f"{prefix}_REQUIRE_MENTION"), default=False)
    return {
        "bot_token": _env("REGRESSION_SLACK_BOT_TOKEN"),
        "app_token": _env("REGRESSION_SLACK_APP_TOKEN") or None,
        "signing_secret": None,
        "team_id": None,
        "team_name": None,
        "app_id": None,
        "require_mention": require_mention,
    }


def _build_discord_payload() -> dict:
    prefix = _platform_prefix("discord")
    require_mention = _parse_bool(_env(f"{prefix}_REQUIRE_MENTION"), default=False)
    return {
        "bot_token": _env("REGRESSION_DISCORD_BOT_TOKEN"),
        "application_id": None,
        "require_mention": require_mention,
    }


def _build_lark_payload() -> dict:
    prefix = _platform_prefix("feishu")
    require_mention = _parse_bool(_env(f"{prefix}_REQUIRE_MENTION"), default=False)
    return {
        "app_id": _env("REGRESSION_FEISHU_APP_ID"),
        "app_secret": _env("REGRESSION_FEISHU_APP_SECRET"),
        "require_mention": require_mention,
        "domain": _env("REGRESSION_FEISHU_DOMAIN", "feishu"),
    }


def _build_wechat_payload() -> dict:
    prefix = _platform_prefix("wechat")
    require_mention = _parse_bool(_env(f"{prefix}_REQUIRE_MENTION"), default=False)
    return {
        "bot_token": _env("REGRESSION_WECHAT_BOT_TOKEN"),
        "base_url": _env("REGRESSION_WECHAT_BASE_URL", "https://ilinkai.weixin.qq.com"),
        "cdn_base_url": _env("REGRESSION_WECHAT_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c"),
        "require_mention": require_mention,
    }


def _build_config_payload() -> dict:
    """Build a unified config.json with all four platforms and all three backends."""
    return {
        "platforms": {
            "enabled": ["slack", "discord", "lark", "wechat"],
            "primary": "slack",
        },
        "platform": "slack",
        "mode": "self_host",
        "version": "v2",
        "slack": _build_slack_payload(),
        "discord": _build_discord_payload(),
        "lark": _build_lark_payload(),
        "wechat": _build_wechat_payload(),
        "runtime": {
            "default_cwd": _default_cwd(),
            "log_level": _env("REGRESSION_LOG_LEVEL", "INFO"),
        },
        "agents": {
            "opencode": {
                "enabled": True,
                "cli_path": CONTAINER_OPENCODE_CLI,
                "default_agent": _optional("REGRESSION_OPENCODE_AGENT"),
                "default_model": _optional("REGRESSION_OPENCODE_MODEL") or "gpt-5.4",
                "default_reasoning_effort": _optional("REGRESSION_OPENCODE_REASONING_EFFORT"),
                "error_retry_limit": 1,
                "default_provider": _optional("REGRESSION_OPENCODE_DEFAULT_PROVIDER") or "openai",
            },
            "claude": {
                "enabled": True,
                "cli_path": "claude",
                "default_model": _optional("REGRESSION_CLAUDE_MODEL"),
            },
            "codex": {
                "enabled": True,
                "cli_path": "codex",
                "default_model": _optional("REGRESSION_CODEX_MODEL"),
            },
        },
        "gateway": None,
        "ui": {
            "setup_host": _ui_host(),
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
        "language": _env("REGRESSION_LANGUAGE", "en"),
    }


def _build_settings_payload() -> dict:
    """Build a unified settings.json with per-channel routing for every platform."""
    channel_scopes: dict[str, dict] = {}
    guild_scopes: dict[str, dict] = {}
    guild_policy_scopes: dict[str, dict] = {}

    for name, pdef in PLATFORM_DEFS.items():
        platform_key = pdef["platform"]
        channel_id = _env(pdef["channel_env"])
        routing = _build_routing(name)
        scope: dict = {}

        if channel_id and routing["agent_name"]:
            prefix = _platform_prefix(name)
            scope[channel_id] = {
                "enabled": True,
                "show_message_types": ["assistant"],
                "custom_cwd": _default_cwd(),
                "routing": routing,
                "require_mention": _parse_bool(
                    _env(f"{prefix}_REQUIRE_MENTION"),
                    default=False,
                ),
            }

        channel_scopes[platform_key] = scope

    discord_allowlist = _parse_csv(_optional("REGRESSION_DISCORD_GUILD_ALLOWLIST")) or []
    discord_denylist = _parse_csv(_optional("REGRESSION_DISCORD_GUILD_DENYLIST")) or []
    if discord_allowlist or discord_denylist:
        guild_scopes["discord"] = {
            guild_id: {"enabled": True}
            for guild_id in discord_allowlist
        }
        for guild_id in discord_denylist:
            guild_scopes["discord"][guild_id] = {"enabled": False}
        guild_policy_scopes["discord"] = {
            "default_enabled": not bool(discord_allowlist),
        }

    return {
        "schema_version": 5,
        "scopes": {
            "channel": channel_scopes,
            "guild": guild_scopes,
            "guild_policy": guild_policy_scopes,
            "user": {},
        },
        "bind_codes": [],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ensure_file_path(path: Path, default_content: str) -> None:
    """Ensure a bind-mounted file path did not get created as a directory."""
    if path.exists() and path.is_dir():
        shutil.rmtree(path)
    if not path.exists():
        _write_text(path, default_content)


def _home_root(output_root: Path) -> Path:
    return output_root / "home"


def _avibe_home(output_root: Path) -> Path:
    return _home_root(output_root) / ".avibe"


def _active_vibe_dir(output_root: Path) -> Path:
    return _avibe_home(output_root)


def _agent_home(output_root: Path) -> Path:
    return _home_root(output_root)


def _normalize_stale_container_path(value: str | None) -> str:
    if not value:
        return value or ""
    prefix_replacements = (
        ("/data/vibe_remote", str(CONTAINER_AVIBE_HOME)),
        ("/root/.avibe", str(CONTAINER_AVIBE_HOME)),
        ("/root/.vibe_remote", str(CONTAINER_AVIBE_HOME)),
        ("/root", str(CONTAINER_HOME)),
    )
    normalized = value
    for old, new in prefix_replacements:
        if normalized == old:
            return new
        if normalized.startswith(old + "/"):
            return new + normalized[len(old):]
    for old, new in prefix_replacements[:-1]:
        normalized = normalized.replace(old + "/", new + "/")
    return normalized


def _rewrite_json_stale_container_paths(value):
    if isinstance(value, dict):
        return {
            _normalize_stale_container_path(key) if isinstance(key, str) else key: _rewrite_json_stale_container_paths(
                item
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_json_stale_container_paths(item) for item in value]
    if isinstance(value, str):
        return _normalize_stale_container_path(value)
    return value


def _rewrite_json_file_stale_container_paths(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    _write_json(path, _rewrite_json_stale_container_paths(payload))


def _rewrite_sqlite_stale_container_paths(db_path: Path) -> None:
    if not db_path.exists() or not db_path.is_file():
        return
    with sqlite3.connect(str(db_path)) as conn:
        table_columns = {
            "agent_sessions": ("workdir",),
            "scope_settings": ("workdir", "settings_json"),
            "runtime_records": ("workdir", "payload_json"),
            "run_definitions": ("cwd", "command_json", "metadata_json"),
            "agent_runs": ("message_payload_json", "result_payload_json", "metadata_json"),
            "media_objects": ("local_path",),
        }
        for table, columns in table_columns.items():
            existing = {
                row[1]
                for row in conn.execute(f'pragma table_info("{table}")').fetchall()
            }
            for column in columns:
                if column not in existing:
                    continue
                rows = conn.execute(
                    f'select rowid, "{column}" from "{table}" where "{column}" is not null'
                ).fetchall()
                for rowid, value in rows:
                    if not isinstance(value, str):
                        continue
                    next_value = _normalize_stale_container_path(value)
                    if column.endswith("_json"):
                        try:
                            next_value = json.dumps(
                                _rewrite_json_stale_container_paths(json.loads(value)),
                                separators=(",", ":"),
                            )
                        except json.JSONDecodeError:
                            next_value = _normalize_stale_container_path(value)
                    if next_value != value:
                        conn.execute(
                            f'update "{table}" set "{column}" = ? where rowid = ?',
                            (next_value, rowid),
                        )
        conn.commit()


def _ensure_shared_home(output_root: Path, reset_mode: str = "none") -> Path:
    if reset_mode == "all":
        home_root = _home_root(output_root)
        for relative in (
            ".claude",
            ".claude.json",
            ".codex",
            ".config/opencode",
            ".local/share/opencode",
        ):
            target = home_root / relative
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()

    shared_root = _agent_home(output_root)
    for subdir in (
        ".claude",
        ".codex",
        ".config/opencode",
        ".local/share/opencode",
    ):
        (shared_root / subdir).mkdir(parents=True, exist_ok=True)
    return shared_root


def _validate_reset_mode(reset_mode: str) -> None:
    if reset_mode not in RESET_MODES:
        allowed = ", ".join(sorted(RESET_MODES))
        raise SystemExit(f"reset_mode must be one of: {allowed}")


def _ensure_vibe_dir(vibe_dir: Path, reset_mode: str = "none") -> None:
    _validate_reset_mode(reset_mode)

    if reset_mode == "all" and vibe_dir.exists():
        shutil.rmtree(vibe_dir)
    elif reset_mode == "config" and vibe_dir.exists():
        for subdir in ("config", "state", "runtime"):
            target = vibe_dir / subdir
            if target.exists():
                shutil.rmtree(target)

    for subdir in ("config", "state", "logs", "runtime", "attachments", "workdir"):
        (vibe_dir / subdir).mkdir(parents=True, exist_ok=True)


def _build_claude_settings_payload() -> dict:
    auth_token = _optional("REGRESSION_CLAUDE_AUTH_TOKEN") or _env("ANTHROPIC_API_KEY")
    payload = {
        "env": {
            "ANTHROPIC_BASE_URL": _optional("REGRESSION_CLAUDE_BASE_URL")
            or _optional("ANTHROPIC_BASE_URL")
            or "",
            "ANTHROPIC_AUTH_TOKEN": auth_token,
            "CLAUDE_CODE_ATTRIBUTION_HEADER": _env("REGRESSION_CLAUDE_ATTRIBUTION_HEADER", "0"),
        }
    }
    return payload


def _relay_openai_base_from_anthropic_base() -> str | None:
    anthropic_base = _optional("ANTHROPIC_BASE_URL")
    if not anthropic_base:
        return None
    return anthropic_base.rstrip("/") + "/v1"


def _openai_base_url(*extra_keys: str) -> str | None:
    for key in (*extra_keys, "OPENAI_API_BASE", "OPENAI_BASE_URL"):
        value = _optional(key)
        if value:
            return value
    return _relay_openai_base_from_anthropic_base()


def _build_codex_config_toml() -> str:
    model_provider = _env("REGRESSION_CODEX_MODEL_PROVIDER", "OpenAI")
    model = _env("REGRESSION_CODEX_MODEL", "gpt-5.4")
    review_model = _env("REGRESSION_CODEX_REVIEW_MODEL", model)
    reasoning_effort = _env("REGRESSION_CODEX_REASONING_EFFORT", "xhigh")
    base_url = _openai_base_url("REGRESSION_CODEX_BASE_URL")
    disable_storage = str(
        _parse_bool(_optional("REGRESSION_CODEX_DISABLE_RESPONSE_STORAGE"), default=True)
    ).lower()
    responses_websockets_v2 = str(
        _parse_bool(_optional("REGRESSION_CODEX_RESPONSES_WEBSOCKETS_V2"), default=False)
    ).lower()
    suppress_unstable_warning = str(
        _parse_bool(_optional("REGRESSION_CODEX_SUPPRESS_UNSTABLE_WARNING"), default=True)
    ).lower()
    provider_lines = [
        "[model_providers.OpenAI]",
        'name = "OpenAI"',
    ]
    if base_url:
        provider_lines.append(f'base_url = "{base_url}"')
    provider_lines.extend(
        [
            'wire_api = "responses"',
            f"supports_websockets = {str(not bool(base_url)).lower()}",
            "requires_openai_auth = true",
        ]
    )

    return (
        f'model_provider = "{model_provider}"\n'
        f'model = "{model}"\n'
        f'review_model = "{review_model}"\n'
        f'model_reasoning_effort = "{reasoning_effort}"\n'
        f"disable_response_storage = {disable_storage}\n"
        f"suppress_unstable_features_warning = {suppress_unstable_warning}\n"
        'network_access = "enabled"\n'
        "windows_wsl_setup_acknowledged = true\n"
        f"model_context_window = {_env('REGRESSION_CODEX_CONTEXT_WINDOW', '1000000')}\n"
        f"model_auto_compact_token_limit = {_env('REGRESSION_CODEX_AUTO_COMPACT_TOKEN_LIMIT', '900000')}\n\n"
        + "\n".join(provider_lines)
        + "\n\n"
        "[features]\n"
        f"responses_websockets_v2 = {responses_websockets_v2}\n"
    )


def _build_codex_auth_payload() -> dict:
    return {
        "OPENAI_API_KEY": _optional("REGRESSION_CODEX_OPENAI_API_KEY") or _env("OPENAI_API_KEY"),
    }


def _build_opencode_payload() -> dict:
    openai_base = _openai_base_url("REGRESSION_OPENCODE_OPENAI_BASE_URL")
    anthropic_base = _optional("REGRESSION_OPENCODE_ANTHROPIC_BASE_URL")
    if not anthropic_base:
        anthropic_base = _optional("ANTHROPIC_BASE_URL")
    openai_key = _optional("REGRESSION_OPENCODE_OPENAI_API_KEY") or _env("OPENAI_API_KEY")
    anthropic_key = _optional("REGRESSION_OPENCODE_ANTHROPIC_API_KEY") or _env("ANTHROPIC_API_KEY")
    openai_options = {"apiKey": openai_key}
    if openai_base:
        openai_options["baseURL"] = openai_base
    anthropic_options = {"apiKey": anthropic_key}
    if anthropic_base:
        anthropic_options["baseURL"] = anthropic_base

    return {
        "permission": "allow",
        "provider": {
            "openai": {
                "options": openai_options,
                "models": {
                    "gpt-5.4": {
                        "name": "GPT-5.4",
                        "options": {"store": False},
                        "variants": {"low": {}, "medium": {}, "high": {}, "xhigh": {}},
                    },
                    "gpt-5.3-codex-spark": {
                        "name": "GPT-5.3 Codex Spark",
                        "options": {"store": False},
                        "variants": {"low": {}, "medium": {}, "high": {}, "xhigh": {}},
                    },
                    "gpt-5.3-codex": {
                        "name": "GPT-5.3 Codex",
                        "options": {"store": False},
                        "variants": {"low": {}, "medium": {}, "high": {}, "xhigh": {}},
                    },
                },
            },
            "anthropic": {
                "options": anthropic_options,
                "npm": "@ai-sdk/anthropic",
            },
        },
        "agent": {
            "build": {"options": {"store": False}},
            "plan": {"options": {"store": False}},
        },
        "$schema": "https://opencode.ai/config.json",
    }


def _shared_agent_config_paths(output_root: Path) -> tuple[Path, ...]:
    shared_root = _agent_home(output_root)
    return (
        shared_root / ".claude" / "settings.json",
        shared_root / ".claude.json",
        shared_root / ".codex" / "config.toml",
        shared_root / ".codex" / "auth.json",
        shared_root / ".config" / "opencode" / "opencode.json",
    )


def _should_write_shared_agent_configs(output_root: Path, *, reset_mode: str) -> bool:
    return reset_mode in {"config", "all"} or any(
        not path.is_file() for path in _shared_agent_config_paths(output_root)
    )


def _repair_shared_agent_state_files(output_root: Path, *, reset_mode: str) -> None:
    shared_root = _ensure_shared_home(output_root, reset_mode=reset_mode)
    _ensure_file_path(shared_root / ".claude.json", "{}\n")


def _write_shared_agent_configs(output_root: Path, *, reset_mode: str) -> None:
    shared_root = _ensure_shared_home(output_root, reset_mode=reset_mode)
    _write_json(shared_root / ".claude" / "settings.json", _build_claude_settings_payload())
    claude_state_path = shared_root / ".claude.json"
    _ensure_file_path(claude_state_path, "{}\n")
    _write_text(shared_root / ".codex" / "config.toml", _build_codex_config_toml())
    _write_json(shared_root / ".codex" / "auth.json", _build_codex_auth_payload())
    _write_json(shared_root / ".config" / "opencode" / "opencode.json", _build_opencode_payload())


def _normalize_config_payload(path: Path) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return

    agents = payload.get("agents")
    if not isinstance(agents, dict):
        return
    opencode = agents.get("opencode")
    if not isinstance(opencode, dict):
        return
    cli_path = str(opencode.get("cli_path") or "")
    # Migrate any recognized opencode install location to the bare PATH-resolved
    # name. Pinning an absolute path breaks across base-image generations: the
    # pre-#545 root-global /usr/local/bin/opencode and the user-owned
    # ~/.local/bin/opencode (or its ~/.opencode source) do not both exist on every
    # instance, so a preserved-state config could point at a path that is absent.
    # A custom user-set path is left untouched.
    known_opencode_paths = {
        "",
        "opencode",
        "/usr/local/bin/opencode",
        str(CONTAINER_HOME / ".opencode" / "bin" / "opencode"),
        str(CONTAINER_HOME / ".local" / "bin" / "opencode"),
    }
    if cli_path not in known_opencode_paths:
        return

    opencode["cli_path"] = CONTAINER_OPENCODE_CLI
    _write_json(path, payload)


def _normalize_existing_state(vibe_dir: Path) -> None:
    config_path = vibe_dir / "config" / "config.json"
    _rewrite_json_file_stale_container_paths(config_path)
    _normalize_config_payload(config_path)
    _rewrite_json_file_stale_container_paths(vibe_dir / "state" / "settings.json")
    _rewrite_json_file_stale_container_paths(vibe_dir / "state" / "sessions.json")
    _rewrite_json_file_stale_container_paths(vibe_dir / "state" / "scheduled_tasks.json")
    _rewrite_sqlite_stale_container_paths(vibe_dir / "state" / "vibe.sqlite")


def prepare(output_root: Path, reset_mode: str = "none") -> None:
    _validate_reset_mode(reset_mode)
    home_root = _home_root(output_root)
    if reset_mode == "all" and (home_root.exists() or home_root.is_symlink()):
        if home_root.is_dir() and not home_root.is_symlink():
            shutil.rmtree(home_root)
        else:
            home_root.unlink()
    home_root.mkdir(parents=True, exist_ok=True)

    vibe_dir = _active_vibe_dir(output_root)
    config_path = vibe_dir / "config" / "config.json"
    settings_path = vibe_dir / "state" / "settings.json"
    sessions_path = vibe_dir / "state" / "sessions.json"
    needs_config = reset_mode in {"config", "all"} or not config_path.exists()
    needs_settings = reset_mode in {"config", "all"} or not settings_path.exists()
    needs_sessions = reset_mode in {"config", "all"} or not sessions_path.exists()
    _repair_shared_agent_state_files(output_root, reset_mode=reset_mode)
    needs_shared_agent_configs = _should_write_shared_agent_configs(output_root, reset_mode=reset_mode)

    if needs_shared_agent_configs:
        _require_envs(("ANTHROPIC_API_KEY", "OPENAI_API_KEY"))

    if needs_config or needs_settings:
        for pdef in PLATFORM_DEFS.values():
            _require_envs(pdef["required_envs"])

    if needs_shared_agent_configs:
        _write_shared_agent_configs(output_root, reset_mode=reset_mode)

    _ensure_vibe_dir(vibe_dir, reset_mode=reset_mode)

    if needs_config:
        _write_json(config_path, _build_config_payload())
    if needs_settings:
        _write_json(settings_path, _build_settings_payload())
    if needs_sessions:
        _write_json(sessions_path, {})
    if not needs_config or not needs_settings:
        _normalize_existing_state(vibe_dir)

    summary_lines: list[str] = []
    for name, pdef in PLATFORM_DEFS.items():
        channel = _env(pdef["channel_env"]) or "(configure later in UI)"
        agent = _env(pdef["backend_env"]) or "(global default Agent)"
        summary_lines.append(f"  {name}: platform={pdef['platform']} channel={channel} agent={agent}")

    print(f"Prepared unified regression state under {vibe_dir}")
    print("Platform routing:")
    print("\n".join(summary_lines))
    print(f"State: {reset_mode if reset_mode != 'none' else 'preserved'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        default=str(Path(".runtime") / "incus-regression" / "seed"),
        help="Directory that will hold generated state",
    )
    parser.add_argument(
        "--reset-mode",
        choices=sorted(RESET_MODES),
        default="none",
        help="Reset scope before generating files: none, config, or all",
    )
    args = parser.parse_args()

    try:
        prepare(Path(args.output_root).resolve(), reset_mode=args.reset_mode)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
