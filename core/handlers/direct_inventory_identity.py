"""Current native picker authority. Local reads only; call off the event loop."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class NativeScope:
    identity: str  # Stable across a token refresh of a named account.
    revision: str  # Reject in-flight replies after a credential change.
    mode: str
    account: str = ""  # Digest only, checked against the native control reply.


def _read_object(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            raw = f.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > 1024 * 1024:
        raise ValueError("credential metadata too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid profile")
    return value


def _codex_scope():
    from vibe.codex_config import get_codex_home
    from vibe.native_oauth_store import read_native_oauth

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    home = get_codex_home()
    try:
        with (home / "config.toml").open("rb") as file:
            native = tomllib.load(file)
    except FileNotFoundError:
        native = {}
    if native.get("profile") or os.environ.get("CODEX_PROFILE") or native.get("model_catalog_json"):
        return None
    provider_id = native.get("model_provider", "openai")
    provider = native.get("model_providers", {}).get(provider_id, {})
    if any(provider.get(k) for k in ("auth", "env_http_headers", "experimental_bearer_token")):
        return None
    route = _key(str(home), provider_id, provider, os.environ.get("OPENAI_BASE_URL"), native.get("model_catalog_url"))
    # Pin both env inputs in the scope. Do not guess which wins a conflicting
    # environment; debug models and the ordinary CLI differ in key precedence.
    keys = {
        k
        for name in ("CODEX_API_KEY", "OPENAI_API_KEY", provider.get("env_key") or "OPENAI_API_KEY")
        if (k := os.environ.get(name))
    }
    if len(keys) > 1:
        return None
    bag = read_native_oauth("codex", allow_secret=False)
    if bag is not None and not bag.exportable:
        return None
    data = bag.payload if bag else {}
    if keys:
        key = keys.pop()
        if data and (data.get("tokens") or (data.get("OPENAI_API_KEY") and data["OPENAI_API_KEY"] != key)):
            # CODEX_API_KEY is explicit for `debug models`; other conflicting
            # file-vs-env configurations are not reliably attributable.
            if os.environ.get("CODEX_API_KEY") != key:
                return None
    else:
        key = data.get("OPENAI_API_KEY")
    if isinstance(key, str) and key.strip():
        identity = _key(route, "api", key)
        return NativeScope(identity, identity, "api")
    tokens = data.get("tokens") or {}
    account = tokens.get("account_id")
    if account and (tokens.get("access_token") or tokens.get("refresh_token")):
        return NativeScope(_key(route, "chatgpt", account), bag.revision, "chatgpt")
    return None


def _claude_scope(settings):
    from vibe.claude_config import build_claude_subprocess_env, get_claude_settings_path
    from vibe.native_oauth_store import read_native_oauth

    file = _read_object(get_claude_settings_path())
    env = {**file.get("env", {}), **build_claude_subprocess_env(settings)}
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
    native = read_native_oauth("claude", allow_secret=False)
    if not native or not native.exportable:
        return None  # A Keychain item existing does not prove an active OAuth account.
    payload = native.payload or {}
    inner = payload.get("claudeAiOauth", payload)
    emails = {
        str(source[field]).strip().casefold()
        for source in (inner, payload)
        for field in ("email", "emailAddress", "accountEmail")
        if source.get(field)
    }
    email = emails.pop() if len(emails) == 1 else None
    if not isinstance(email, str) or not email.strip():
        return None
    account = _key(email.casefold())
    return NativeScope(_key("oauth", account, env.get("ANTHROPIC_BASE_URL")), native.revision, "oauth", account)


def claude_account_matches(scope: NativeScope, info: dict) -> bool:
    account = info.get("account") or {}
    if account.get("apiProvider") != "firstParty":
        return False
    if scope.mode == "api":
        return (
            account.get("apiKeySource") == "ANTHROPIC_API_KEY"
            and not account.get("email")
            and not account.get("subscriptionType")
            and account.get("tokenSource") in (None, "none")
        )
    email = account.get("email")
    return (
        isinstance(email, str)
        and _key(email.casefold()) == scope.account
        and (account.get("tokenSource") == "claude.ai" or bool(account.get("subscriptionType")))
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
