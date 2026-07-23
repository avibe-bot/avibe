"""Read-only native-config discovery and copy-only Model Hub migration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, Protocol

from config.v2_config import (
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
)
from core.handlers.model_hub.events import contains_credential_material
from vibe.backend_model_catalog import backend_model_entries, load_bundled_catalog
from vibe.claude_config import read_claude_oauth_signed_in, read_claude_settings_env
from vibe.codex_config import _load_auth, get_codex_config_paths, read_codex_auth_state
from vibe.opencode_config import (
    get_opencode_custom_provider_adapter,
    load_first_opencode_user_config,
    read_opencode_provider_auth_entries,
)

MigrationAction = Literal["import", "controlled_import", "keep_native", "reauth"]
MigrationKind = Literal["api_key", "oauth_native", "opencode_provider"]
_CUSTOM_ENDPOINT_NOTE = "settings.models.source.customEndpoint"
_NATIVE_SUPPLY_NOTE = "settings.models.source.nativeSupply"
_OPENCODE_BUILTIN_PROTOCOLS: dict[
    str,
    Literal["anthropic", "openai_compatible"],
] = {
    "alibaba-cn": "openai_compatible",
    "deepseek": "openai_compatible",
    "minimax": "anthropic",
    "openrouter": "openai_compatible",
    "poe": "openai_compatible",
}


class MigrationConflictError(ValueError):
    pass


class MigrationHost(Protocol):
    store: Any
    adapter: Any
    _mutation_lock: Any
    now: Callable[[], datetime]
    migration_claude_oauth_probe: Optional[Callable[[], bool]]

    @staticmethod
    def _clone_config(config: ModelHubConfig) -> ModelHubConfig: ...

    async def _engine_call(self, awaitable: Awaitable[Any]) -> Any: ...

    async def _commit_synced(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
    ) -> None: ...

    async def _rollback_credential(
        self,
        source_id: str,
        credential_ref: str,
    ) -> None: ...

    def _apply_discovered_models(
        self,
        source: ModelHubSourceConfig,
        manual_models: list[ModelHubModelConfig],
        discovered: list[str],
    ) -> None: ...


@dataclass(frozen=True)
class NativeMigrationItem:
    id: str
    source_id: str
    backend: Literal["claude", "codex", "opencode"]
    kind: MigrationKind
    masked_detail: str
    proposed_action: MigrationAction
    selected: bool
    notes_key: Optional[str]
    vendor: str
    protocol: Literal[
        "anthropic",
        "openai_responses",
        "openai_chat",
        "openai_compatible",
    ]
    display_name: str
    base_url: Optional[str] = None
    secret: Optional[str] = field(default=None, repr=False)
    account_label: Optional[str] = None
    manual_models: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "backend": self.backend,
            "kind": self.kind,
            "masked_detail": self.masked_detail,
            "proposed_action": self.proposed_action,
            "selected": self.selected,
            "notes_key": self.notes_key,
        }


def _stable_suffix(*parts: str) -> str:
    identity = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:16]


def _ids(
    backend: str,
    kind: str,
    identity: str,
    action: MigrationAction,
    version: str = "",
) -> tuple[str, str]:
    source_id = f"src_{_stable_suffix('source', backend, kind, identity)}"
    item_id = f"mig_{_stable_suffix('item', backend, kind, identity, action, version)}"
    return item_id, source_id


def _native_model_ids(backend: str) -> tuple[str, ...]:
    catalog = load_bundled_catalog()
    return tuple(entry["id"] for entry in backend_model_entries(backend, catalog))


def _safe_account_label(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 64
        or re.fullmatch(r"[^@\s]+@[^@\s]+", candidate) is None
        or contains_credential_material(candidate)
    ):
        return None
    return candidate


def _claude_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
    oauth_probe: Optional[Callable[[], bool]],
) -> list[NativeMigrationItem]:
    items: list[NativeMigrationItem] = []
    env = read_claude_settings_env(home)
    api_key = env.get("ANTHROPIC_API_KEY")
    auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    base_url = env.get("ANTHROPIC_BASE_URL")
    if api_key:
        action: MigrationAction = "import"
        item_id, source_id = _ids(
            "claude",
            "api_key",
            "settings-env",
            action,
            _stable_suffix(api_key, base_url or ""),
        )
        detail = mask_credential(api_key)
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="claude",
                kind="api_key",
                masked_detail=detail,
                proposed_action=action,
                selected=True,
                notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
                vendor="anthropic",
                protocol="anthropic",
                display_name="Anthropic",
                base_url=base_url,
                secret=api_key,
            )
        )

    if auth_token:
        action = "reauth"
        item_id, source_id = _ids(
            "claude",
            "api_key",
            "settings-auth-token",
            action,
            _stable_suffix(auth_token, base_url or ""),
        )
        detail = mask_credential(auth_token)
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="claude",
                kind="api_key",
                masked_detail=detail,
                proposed_action=action,
                selected=False,
                notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
                vendor="anthropic",
                protocol="anthropic",
                display_name="Anthropic",
                base_url=base_url,
            )
        )

    # Claude settings env takes precedence over the native OAuth store. A
    # leftover OAuth credential must not outrank the auth the CLI will use.
    if api_key or auth_token:
        return items

    oauth_signed_in = read_claude_oauth_signed_in(home)
    if not oauth_signed_in and oauth_probe is not None:
        try:
            oauth_signed_in = bool(oauth_probe())
        except Exception:
            oauth_signed_in = False
    if oauth_signed_in:
        action = "keep_native"
        item_id, source_id = _ids("claude", "oauth_native", "oauth", action)
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="claude",
                kind="oauth_native",
                masked_detail="",
                proposed_action=action,
                selected=True,
                notes_key=_NATIVE_SUPPLY_NOTE,
                vendor="anthropic",
                protocol="anthropic",
                display_name="Claude",
            )
        )
    return items


def _codex_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
) -> list[NativeMigrationItem]:
    _, auth_path = get_codex_config_paths(home)
    auth_data = _load_auth(auth_path)
    if not isinstance(auth_data, dict):
        return []
    state = read_codex_auth_state(home)
    # auth.json is authoritative only when Codex is explicitly using its
    # file credential store. In auto/keyring mode it may contain stale values
    # while the live credential remains inaccessible in the OS keyring.
    if state.get("file_store_active") is not True:
        return []
    items: list[NativeMigrationItem] = []
    raw_auth_mode = auth_data.get("auth_mode")
    auth_mode = raw_auth_mode.strip().lower() if isinstance(raw_auth_mode, str) else None

    api_key = auth_data.get("OPENAI_API_KEY")
    has_api_key = isinstance(api_key, str) and bool(api_key.strip())
    api_key_is_active = auth_mode != "chatgpt" and has_api_key
    oauth_is_active = auth_mode == "chatgpt" or (
        auth_mode != "apikey" and not has_api_key
    )
    if api_key_is_active:
        assert isinstance(api_key, str)
        api_key = api_key.strip()
        base_url = state.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            base_url = None
        wire_api = state.get("wire_api")
        protocol = "openai_chat" if wire_api == "chat" else "openai_responses"
        item_id, source_id = _ids(
            "codex",
            "api_key",
            "auth-json-api-key",
            "import",
            _stable_suffix(api_key, base_url or "", protocol),
        )
        detail = mask_credential(api_key)
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="codex",
                kind="api_key",
                masked_detail=detail,
                proposed_action="import",
                selected=True,
                notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
                vendor="openai",
                protocol=protocol,
                display_name="OpenAI",
                base_url=base_url,
                secret=api_key,
            )
        )

    tokens = auth_data.get("tokens")
    if not oauth_is_active or not isinstance(tokens, dict) or not any(
        isinstance(tokens.get(key), str) and bool(tokens[key].strip())
        for key in ("access_token", "refresh_token", "id_token")
    ):
        return items

    action: MigrationAction = "keep_native"
    item_id, source_id = _ids(
        "codex",
        "oauth_native",
        "auth-json",
        action,
    )
    account = state.get("chatgpt_account")
    account_label = _safe_account_label(account.get("email") if isinstance(account, dict) else None)
    items.append(
        NativeMigrationItem(
            id=item_id,
            source_id=source_id,
            backend="codex",
            kind="oauth_native",
            masked_detail=account_label or "",
            proposed_action=action,
            selected=True,
            notes_key=_NATIVE_SUPPLY_NOTE,
            vendor="openai",
            protocol="openai_responses",
            display_name="ChatGPT",
            account_label=account_label,
        )
    )
    return items


def _load_opencode_provider_catalog(home: Optional[Path]) -> dict[str, dict[str, Any]]:
    if home is not None:
        path = home / ".cache" / "opencode" / "models.json"
    else:
        cache_home = os.environ.get("XDG_CACHE_HOME")
        path = (
            Path(cache_home).expanduser() / "opencode" / "models.json"
            if cache_home
            else Path.home() / ".cache" / "opencode" / "models.json"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        provider_id.strip().lower(): provider
        for provider_id, provider in payload.items()
        if isinstance(provider_id, str) and provider_id.strip() and isinstance(provider, dict)
    }


def _opencode_protocol(
    provider_id: str,
    provider_config: dict[str, Any],
    catalog_provider: dict[str, Any],
) -> Optional[Literal["anthropic", "openai_responses", "openai_compatible"]]:
    if provider_id == "anthropic":
        return "anthropic"
    if provider_id == "openai":
        return "openai_responses"
    custom_adapter = get_opencode_custom_provider_adapter(provider_id, provider_config)
    if custom_adapter == "anthropic-compatible":
        return "anthropic"
    if custom_adapter == "openai-compatible":
        return "openai_compatible"
    builtin_protocol = _OPENCODE_BUILTIN_PROTOCOLS.get(provider_id)
    if builtin_protocol is not None:
        return builtin_protocol
    npm = provider_config.get("npm") or catalog_provider.get("npm")
    if npm == "@ai-sdk/anthropic":
        return "anthropic"
    if npm == "@ai-sdk/openai":
        return "openai_responses"
    if npm in {"@ai-sdk/openai-compatible", "@openrouter/ai-sdk-provider"}:
        return "openai_compatible"
    return None


def _opencode_manual_models(provider_config: dict[str, Any]) -> tuple[str, ...]:
    raw_models = provider_config.get("models")
    if not isinstance(raw_models, dict):
        return ()
    return tuple(
        model_id.strip()
        for model_id in raw_models
        if isinstance(model_id, str) and model_id.strip() and not contains_credential_material(model_id.strip())
    )


def _opencode_plaintext_key(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if re.fullmatch(r"\{env:[^{}]+\}", candidate):
        return None
    return candidate


def _opencode_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
) -> list[NativeMigrationItem]:
    probe = load_first_opencode_user_config(home=home)
    provider_configs: dict[str, dict[str, Any]] = {}
    if isinstance(probe.config, dict):
        raw_providers = probe.config.get("provider")
        if isinstance(raw_providers, dict):
            provider_configs = {
                provider_id.strip().lower(): provider_config
                for provider_id, provider_config in raw_providers.items()
                if isinstance(provider_id, str) and provider_id.strip() and isinstance(provider_config, dict)
            }
    auth_entries = {
        provider_id.strip().lower(): entry
        for provider_id, entry in read_opencode_provider_auth_entries(home=home).items()
        if provider_id.strip()
    }
    provider_catalog = _load_opencode_provider_catalog(home)
    provider_ids = set(provider_configs) | set(auth_entries)
    items: list[NativeMigrationItem] = []
    for provider_id in sorted(provider_ids):
        if (
            not provider_id
            or len(provider_id) > 64
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for char in provider_id)
            or not provider_id[0].isalnum()
            or contains_credential_material(provider_id)
        ):
            continue
        provider_config = provider_configs.get(provider_id, {})
        options = provider_config.get("options")
        if not isinstance(options, dict):
            options = {}
        config_key = _opencode_plaintext_key(options.get("apiKey"))
        auth_entry = auth_entries.get(provider_id, {})
        auth_key = (
            _opencode_plaintext_key(auth_entry.get("key"))
            if auth_entry.get("type") == "api"
            else None
        )
        secret = config_key or auth_key
        if secret is None:
            continue
        raw_base_url = options.get("baseURL")
        base_url = raw_base_url.strip() if isinstance(raw_base_url, str) and raw_base_url.strip() else None
        catalog_provider = provider_catalog.get(provider_id, {})
        if base_url is None:
            catalog_api = catalog_provider.get("api")
            if isinstance(catalog_api, str) and catalog_api.strip():
                base_url = catalog_api.strip()
        protocol = _opencode_protocol(provider_id, provider_config, catalog_provider)
        if protocol is None:
            continue
        if base_url is None and provider_id not in {"anthropic", "openai"}:
            continue
        manual_models = _opencode_manual_models(provider_config)
        action: MigrationAction = "import"
        item_id, source_id = _ids(
            "opencode",
            "opencode_provider",
            provider_id,
            action,
            _stable_suffix(secret, base_url or "", protocol, *manual_models),
        )
        detail = f"{provider_id} · {mask_credential(secret)}"
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="opencode",
                kind="opencode_provider",
                masked_detail=detail,
                proposed_action=action,
                selected=True,
                notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
                vendor=provider_id,
                protocol=protocol,
                display_name=provider_id,
                base_url=base_url,
                secret=secret,
                manual_models=manual_models,
            )
        )
    return items


def scan_native_configs(
    config: ModelHubConfig,
    *,
    mask_credential: Callable[[str], str],
    home: Optional[Path] = None,
    claude_oauth_probe: Optional[Callable[[], bool]] = None,
) -> list[NativeMigrationItem]:
    """Read native stores without modifying or deleting any path."""

    items = [
        *_claude_items(
            home=home,
            mask_credential=mask_credential,
            oauth_probe=claude_oauth_probe,
        ),
        *_codex_items(home=home, mask_credential=mask_credential),
        *_opencode_items(home=home, mask_credential=mask_credential),
    ]
    existing_source_ids = {source.id for source in config.sources}
    return [item for item in items if item.source_id not in existing_source_ids]


def _new_source(
    item: NativeMigrationItem,
    *,
    now: datetime,
    validate_base_url: Callable[[object], Optional[str]],
) -> ModelHubSourceConfig:
    keep_native = item.proposed_action == "keep_native"
    controlled = item.proposed_action == "controlled_import"
    discovered_at = now.isoformat()
    models = (
        [
            ModelHubModelConfig(
                id=model_id,
                provenance="discovered",
                discovered_at=discovered_at,
            )
            for model_id in _native_model_ids(item.backend)
        ]
        if keep_native
        else []
    )
    return ModelHubSourceConfig(
        id=item.source_id,
        kind="subscription" if keep_native or controlled else "api_key",
        vendor=item.vendor,
        display_name=item.display_name,
        protocol=item.protocol,
        base_url=validate_base_url(item.base_url),
        supply_channel="native_cli" if keep_native else "hub",
        experimental_consent_at=discovered_at if controlled else None,
        billing="monthly" if keep_native or controlled else "metered",
        state=ModelHubSourceStateConfig(status="standby"),
        usage=ModelHubSourceUsageConfig(),
        models=models,
        account_label=item.account_label,
    )


def _migration_rollback_id(source_id: str, credential_ref: str) -> str:
    return f"{source_id}:migration:{_stable_suffix(credential_ref)}"


async def apply_native_migration(
    host: MigrationHost,
    item_ids: object,
    *,
    mask_credential: Callable[[str], str],
    validate_base_url: Callable[[object], Optional[str]],
) -> int:
    """Provision, probe, and atomically persist a selected migration batch."""

    if (
        not isinstance(item_ids, list)
        or not all(isinstance(item_id, str) and item_id for item_id in item_ids)
        or len(set(item_ids)) != len(item_ids)
    ):
        raise MigrationConflictError
    if not item_ids:
        return 0

    async with host._mutation_lock:
        previous = host.store.load()
        available = await asyncio.to_thread(
            scan_native_configs,
            previous,
            mask_credential=mask_credential,
            claude_oauth_probe=host.migration_claude_oauth_probe,
        )
        by_id = {item.id: item for item in available}
        missing = [item_id for item_id in item_ids if item_id not in by_id]
        if missing:
            raise MigrationConflictError

        selected = [by_id[item_id] for item_id in item_ids]
        if any(item.proposed_action in {"controlled_import", "reauth"} for item in selected):
            raise MigrationConflictError
        selected.sort(key=lambda item: item.proposed_action != "keep_native")
        updated = host._clone_config(previous)
        existing_ids = {source.id for source in updated.sources}
        if any(item.source_id in existing_ids for item in selected):
            raise MigrationConflictError

        provisioned: list[tuple[str, str]] = []
        native_source_ids: list[str] = []
        hub_source_ids: list[str] = []
        persisted = False
        try:
            for item in selected:
                source = _new_source(
                    item,
                    now=host.now(),
                    validate_base_url=validate_base_url,
                )
                if item.proposed_action == "import":
                    if not item.secret:
                        raise MigrationConflictError
                    credential_ref = await host._engine_call(
                        host.adapter.provision_credential(
                            item.vendor,
                            item.protocol,
                            item.secret,
                            source.base_url,
                        )
                    )
                    provisioned.append((source.id, credential_ref))
                    source.credential_ref = credential_ref
                    source.masked_credential = mask_credential(item.secret)
                    discovered = list(
                        await host._engine_call(
                            host.adapter.discover_models(
                                item.vendor,
                                item.protocol,
                                source.base_url,
                                credential_ref,
                            )
                        )
                    )
                    manual_models = [
                        ModelHubModelConfig(id=model_id, provenance="manual") for model_id in item.manual_models
                    ]
                    host._apply_discovered_models(source, manual_models, discovered)
                updated.sources.append(source)
                if item.proposed_action == "keep_native":
                    native_source_ids.append(source.id)
                else:
                    hub_source_ids.append(source.id)

            updated.priority_order = [
                *updated.priority_order,
                *native_source_ids,
                *hub_source_ids,
            ]

            await host._commit_synced(previous, updated)
            persisted = True
            return len(selected)
        finally:
            if not persisted:
                for source_id, credential_ref in reversed(provisioned):
                    await host._rollback_credential(
                        _migration_rollback_id(source_id, credential_ref),
                        credential_ref,
                    )
