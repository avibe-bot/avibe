"""Current native picker scope. Synchronous, local-only: use a worker thread.

Neither readers nor producers may use a most-recent identity when this returns None.
The binary revision triggers discovery; it deliberately is *not* the inventory key.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _file_revision(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _native_revision(backend: str) -> str | None:
    from vibe.native_oauth_store import read_native_oauth
    current = read_native_oauth(backend, allow_secret=False)
    if current and (current.exportable or (current.payload or {}).get("status") == "metadata_only"):
        return current.revision
    return None


def inventory_identity(backend: str, config, *, cwd: str | None = None) -> str | None:
    """Compute the current key without launching a CLI or returning credential material."""
    settings = getattr(config.agents, backend)
    if not settings.enabled:
        return None
    if backend == "antigravity":
        from modules.agents.antigravity.models import account_cache_identity
        return account_cache_identity(settings)
    if backend == "codex":
        from vibe.codex_config import get_codex_home, _load_toml
        home = get_codex_home()
        native = _load_toml(home / "config.toml")
        provider_id = native.get("model_provider", "openai")
        provider = native.get("model_providers", {}).get(provider_id, {})
        # A command-supplied credential has no cheap current identity. A table
        # override is not an independent inventory (notably Avibe's Hub table).
        if native.get("model_catalog_json") or provider.get("auth"):
            return None
        env_key = provider.get("env_key") or "OPENAI_API_KEY"
        key = os.environ.get(env_key)
        revision = _native_revision(backend)
        if not (revision or key):
            return None
        return _key(str(home), provider_id, provider, os.environ.get("OPENAI_BASE_URL"), key, revision)
    if backend == "claude":
        from vibe.claude_config import build_claude_subprocess_env
        env = build_claude_subprocess_env(settings)
        credential = [env.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")]
        revision = None if any(credential) else _native_revision(backend)
        if not (revision or any(credential)):
            return None
        return _key(credential, env.get("ANTHROPIC_BASE_URL"), revision)
    if backend == "opencode":
        from modules.agents.opencode.client_manager import global_config_files
        from vibe.opencode_config import get_opencode_auth_path
        directory = Path(cwd or config.runtime.default_cwd).expanduser().resolve()
        # Use the same global provider config files the server uses. Include
        # its cwd's native config, never another workspace's default menu.
        profile = [_file_revision(p) for p in (*global_config_files(), directory / "opencode.json", directory / "opencode.jsonc")]
        credential = _file_revision(get_opencode_auth_path())
        if not credential and not any(profile):
            return None
        return _key(str(directory), credential, profile)
    raise ValueError("unsupported native backend")
