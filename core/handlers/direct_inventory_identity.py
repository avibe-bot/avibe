"""Current native picker authority. Local reads only; call off the event loop."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from vibe.native_inventory_files import read_native_file


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class NativeScope:
    identity: str  # Stable across a token refresh of a named account.
    revision: str  # Reject in-flight replies after a credential change.
    mode: str


def _read_object(path: Path) -> dict:
    raw = read_native_file(path, 1024 * 1024)
    if raw is None:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid profile")
    return value


def _codex_scope():
    from vibe.codex_config import get_codex_home

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    home = get_codex_home()
    native = tomllib.loads((read_native_file(home / "config.toml", 1024 * 1024) or b"").decode())
    # Positive scope: the default first-party provider and a file credential.
    # Don't reproduce the CLI's gateway/profile/auth precedence in a picker.
    if native.keys() - {"cli_auth_credentials_store", "model", "model_reasoning_effort", "model_provider", "projects"}:
        return None
    if native.get("model_provider", "openai") != "openai" or native.get("cli_auth_credentials_store", "file") != "file":
        return None
    if any(
        os.environ.get(k)
        for k in (
            "CODEX_ACCESS_TOKEN",
            "CODEX_PROFILE",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "CHATGPT_BASE_URL",
            "OPENAI_ORGANIZATION",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT_ID",
        )
    ):
        return None
    data = _read_object(home / "auth.json")
    mode = data.get("auth_mode")
    tokens = data.get("tokens")
    keys = {key for key in (data.get("OPENAI_API_KEY"), os.getenv("CODEX_API_KEY"), os.getenv("OPENAI_API_KEY")) if key}
    if keys:
        if len(keys) != 1 or tokens or mode not in (None, "apikey"):
            return None
        key = keys.pop()
        if not isinstance(key, str) or not key.strip():
            return None
        identity = _key("codex-first-party-file-v2", str(home), "api", key)
        return NativeScope(identity, identity, "api")
    if (
        mode in (None, "chatgpt")
        and isinstance(tokens, dict)
        and isinstance(account := tokens.get("account_id"), str)
        and account.strip()
        and tokens.get("access_token")
    ):
        return NativeScope(_key("codex-first-party-file-v2", str(home), "chatgpt", account), _key(data), "chatgpt")
    return None


def claude_picker_environment(settings):
    """Read settings once with the inventory file bound, including at launch."""
    from vibe.claude_config import build_claude_subprocess_env, get_claude_settings_path

    file = _read_object(get_claude_settings_path())
    file_env = file.get("env", {})
    if not isinstance(file_env, dict) or any(not isinstance(v, str) for v in file_env.values()):
        raise ValueError("invalid Claude settings environment")
    env = {**file_env, **build_claude_subprocess_env(settings, settings_env=file_env)}
    return file, env


def _claude_scope(settings):
    file, env = claude_picker_environment(settings)
    file_env = file.get("env", {})
    if any(
        env.get(name) or file_env.get(name)
        for name in (
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        )
    ):
        return None
    if file.get("apiKeyHelper") or any(
        v for k, v in env.items() if k.startswith("CLAUDE_CODE_USE_") and v not in ("0", "false")
    ):
        return None
    if env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return None  # Cannot verify the subject of an injected bearer.
    key = env.get("ANTHROPIC_API_KEY")
    if key:
        identity = _key("api", env.get("ANTHROPIC_BASE_URL"), key)
        return NativeScope(identity, identity, "api")
    return None  # An email or org name cannot prove a stable organization ID.


def claude_account_matches(scope: NativeScope, info: dict) -> bool:
    account = info.get("account") or {}
    if account.get("apiProvider") != "firstParty" or scope.mode != "api":
        # The SDK does not attest an OAuth organization's stable identity.
        return False
    return (
        account.get("apiKeySource") == "ANTHROPIC_API_KEY"
        and not account.get("email")
        and not account.get("organization")
        and not account.get("subscriptionType")
        and account.get("tokenSource") in (None, "none")
    )


def inventory_scope(backend: str, config, *, cwd: str | None = None) -> NativeScope | None:
    settings = getattr(config.agents, backend)
    if not settings.enabled:
        return None
    try:
        if backend == "codex":
            return _codex_scope()
        if backend == "claude":
            return _claude_scope(settings)
        if backend == "antigravity":
            from modules.agents.antigravity.models import account_cache_identity

            value = account_cache_identity(settings)
        elif backend == "opencode":
            from modules.agents.opencode.picker_inventory import binding

            bound = binding(config, cwd)
            return NativeScope(*bound, backend) if bound else None
        else:
            return None
        return NativeScope(value, value, backend) if value else None
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None


def inventory_identity(backend: str, config, *, cwd: str | None = None) -> str | None:
    """Return the current trusted scope, not a cached identity or a 'default'."""
    scope = inventory_scope(backend, config, cwd=cwd)
    return scope.identity if scope else None
