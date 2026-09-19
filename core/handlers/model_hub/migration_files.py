"""Plan native configuration cleanup without changing any native file."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from vibe.claude_config import get_claude_settings_path
from vibe.codex_config import (
    CREDENTIALS_STORE_KEY,
    LEGACY_MANAGED_PROVIDER_IDS,
    MANAGED_PROVIDER_ID,
    _dump_toml,
    get_codex_config_paths,
)
from vibe.opencode_config import (
    get_opencode_auth_path,
    get_opencode_config_paths,
    parse_jsonc_object,
)

from .migration_journal import NativeFileEdit, TakeoverStateError, _read_regular

if TYPE_CHECKING:
    from .migration import NativeMigrationItem

def _object(content: bytes, *, jsonc: bool = False) -> dict:
    try:
        result = parse_jsonc_object(content.decode()) if jsonc else json.loads(content)
    except (ValueError, UnicodeError):
        raise TakeoverStateError("native configuration cannot be parsed") from None
    if not isinstance(result, dict):
        raise TakeoverStateError("native configuration cannot be parsed")
    return result


def _json_bytes(payload: dict) -> bytes:
    # Preserve even unrelated escaped surrogate values without accepting them
    # as Hub model identities or failing halfway through native cleanup.
    return (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode()


def plan_native_cleanup(
    items: list[NativeMigrationItem],
    *,
    home: Path | None,
    project_roots: tuple[Path, ...] = (),
) -> list[NativeFileEdit]:
    """One before/after image per path, even when providers share a file.

    Only selected backend authentication is removed. Planning refuses malformed
    files instead of using the forgiving UI probes that can return an empty
    document after a parse failure.
    """
    edits: dict[Path, NativeFileEdit] = {}
    # Native-store resolution owns credential paths (including isolated secure
    # roots), while this planner owns one final file image for the whole batch.
    for item in items:
        for operation in (item.native_store_edit or {}).get("operations", []):
            if operation.get("kind") != "file":
                continue
            path = Path(operation["path"]).absolute()
            before = operation["before"]
            after = operation["after"]
            edit = NativeFileEdit(
                path,
                before["raw"].encode() if before["exists"] else None,
                after["raw"].encode() if after["exists"] else None,
            )
            if path in edits and edits[path] != edit:
                raise TakeoverStateError("conflicting native credential snapshots")
            edits[path] = edit

    def edit_json(path: Path, transform, *, jsonc: bool = False) -> None:
        path = path.absolute()
        staged = edits.get(path)
        original = staged.before if staged else _read_regular(path)
        if original is None:
            return
        working = staged.after if staged else original
        if working is None:
            return
        payload = _object(working, jsonc=jsonc)
        before = json.dumps(payload, sort_keys=True)
        transform(payload)
        if json.dumps(payload, sort_keys=True) != before:
            edits[path] = NativeFileEdit(path.absolute(), original, _json_bytes(payload))

    backends = {item.backend for item in items}
    selected_secrets = {item.secret for item in items if item.secret}
    if "claude" in backends:
        def clear_settings(payload: dict) -> None:
            env = payload.get("env")
            if env is not None and not isinstance(env, dict):
                raise TakeoverStateError("native configuration cannot be parsed")
            if isinstance(env, dict):
                for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
                    if key != "ANTHROPIC_BASE_URL" and env.get(key) and env[key] not in selected_secrets:
                        raise TakeoverStateError("another native credential requires migration")
                    env.pop(key, None)
            if payload.get("apiKeyHelper"):
                raise TakeoverStateError("native credential helper requires configuration")
            payload.pop("apiKeyHelper", None)

        edit_json(get_claude_settings_path(home), clear_settings)
        for root in project_roots:
            for filename in ("settings.json", "settings.local.json"):
                edit_json(root / ".claude" / filename, clear_settings)

    if "codex" in backends:
        config_path, auth_path = get_codex_config_paths(home)

        def clear_auth(payload: dict) -> None:
            if payload.get("OPENAI_API_KEY") and payload["OPENAI_API_KEY"] not in selected_secrets:
                raise TakeoverStateError("another native credential requires migration")
            if payload.get("tokens") and not any(
                item.backend == "codex" and item.kind == "oauth_native"
                for item in items
            ):
                raise TakeoverStateError("another native credential requires migration")
            for key in ("OPENAI_API_KEY", "tokens", "auth_mode", "last_refresh"):
                payload.pop(key, None)

        edit_json(auth_path, clear_auth)
        content = _read_regular(config_path)
        if content is not None:
            try:
                config = tomllib.loads(content.decode())
            except (ValueError, UnicodeError):
                raise TakeoverStateError("native configuration cannot be parsed") from None
            before = json.dumps(config, sort_keys=True, default=str)
            removable = {MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS}
            removable.update(item.native_provider_id for item in items if item.backend == "codex" and item.native_provider_id)
            providers = config.get("model_providers")
            if isinstance(providers, dict):
                for provider_id in removable:
                    provider = providers.get(provider_id)
                    if not isinstance(provider, dict):
                        continue
                    # Retain user labels, capabilities, and timeout preferences.
                    for key in ("base_url", "env_key", "experimental_bearer_token", "http_headers", "env_http_headers", "requires_openai_auth"):
                        provider.pop(key, None)
                if not providers:
                    config.pop("model_providers", None)
            if config.get("model_provider") in removable:
                config.pop("model_provider", None)
            config.pop(CREDENTIALS_STORE_KEY, None)
            if json.dumps(config, sort_keys=True, default=str) != before:
                edits[config_path] = NativeFileEdit(config_path.absolute(), content, _dump_toml(config).encode())

    if "opencode" in backends:
        vendors = {item.vendor for item in items if item.backend == "opencode"}

        def clear_providers(payload: dict) -> None:
            providers = payload.get("provider")
            if providers is None:
                return
            if not isinstance(providers, dict):
                raise TakeoverStateError("native configuration cannot be parsed")
            for vendor in vendors:
                provider = providers.get(vendor)
                if not isinstance(provider, dict):
                    continue
                options = provider.get("options")
                if isinstance(options, dict):
                    if options.get("apiKey") and options["apiKey"] not in selected_secrets:
                        raise TakeoverStateError("another native credential requires migration")
                    options.pop("apiKey", None)
                    options.pop("baseURL", None)
                    if not options:
                        provider.pop("options", None)

        config_paths = get_opencode_config_paths(home)
        config_paths.extend(
            root / filename for root in project_roots
            for filename in ("opencode.json", "opencode.jsonc", ".opencode/opencode.json", ".opencode/opencode.jsonc")
        )
        for path in dict.fromkeys(config_paths):
            edit_json(path, clear_providers, jsonc=True)

        def clear_provider_auth(payload: dict) -> None:
            for vendor in vendors:
                payload.pop(vendor, None)

        edit_json(get_opencode_auth_path(home), clear_provider_auth)
    return list(edits.values())
