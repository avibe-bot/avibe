"""Read-only native-config discovery and copy-only Model Hub migration."""

from __future__ import annotations

import hashlib
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
        if base_url:
            detail += " + ANTHROPIC_BASE_URL"
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="claude",
                kind="api_key",
                masked_detail=detail,
                proposed_action=action,
                selected=True,
                notes_key=None,
                vendor="anthropic",
                protocol="anthropic",
                display_name="Anthropic",
                base_url=base_url,
                secret=api_key,
            )
        )

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
                masked_detail="Claude OAuth",
                proposed_action=action,
                selected=True,
                notes_key="models.migration.keep_native.sanctioned",
                vendor="anthropic",
                protocol="anthropic",
                display_name="Claude",
            )
        )
    return items


def _codex_items(
    config: ModelHubConfig,
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
) -> list[NativeMigrationItem]:
    _, auth_path = get_codex_config_paths(home)
    auth_data = _load_auth(auth_path)
    if not isinstance(auth_data, dict):
        return []
    state = read_codex_auth_state(home)
    items: list[NativeMigrationItem] = []

    api_key = auth_data.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        api_key = api_key.strip()
        base_url = state.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            base_url = None
        item_id, source_id = _ids(
            "codex",
            "api_key",
            "auth-json-api-key",
            "import",
            _stable_suffix(api_key, base_url or ""),
        )
        detail = mask_credential(api_key)
        if base_url:
            detail += " + base_url"
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="codex",
                kind="api_key",
                masked_detail=detail,
                proposed_action="import",
                selected=True,
                notes_key=None,
                vendor="openai",
                protocol="openai_responses",
                display_name="OpenAI",
                base_url=base_url,
                secret=api_key,
            )
        )

    tokens = auth_data.get("tokens") if isinstance(auth_data, dict) else None
    if not isinstance(tokens, dict):
        return items

    action: MigrationAction = "keep_native"
    item_id, source_id = _ids(
        "codex",
        "oauth_native",
        "auth-json",
        action,
    )
    account = state.get("chatgpt_account")
    account_label = _safe_account_label(
        account.get("email") if isinstance(account, dict) else None
    )
    detail = account_label or "Codex auth.json"
    items.append(
        NativeMigrationItem(
            id=item_id,
            source_id=source_id,
            backend="codex",
            kind="oauth_native",
            masked_detail=detail,
            proposed_action=action,
            selected=True,
            notes_key=(
                "models.migration.keep_native.reauthorize_in_hub"
                if config.subscription_hub_experimental
                else "models.migration.keep_native.sanctioned"
            ),
            vendor="openai",
            protocol="openai_responses",
            display_name="ChatGPT",
            account_label=account_label,
        )
    )
    return items


def _opencode_protocol(
    provider_id: str,
    provider_config: dict[str, Any],
) -> Literal["anthropic", "openai_responses", "openai_compatible"]:
    if provider_id == "anthropic":
        return "anthropic"
    if provider_id == "openai":
        return "openai_responses"
    if get_opencode_custom_provider_adapter(provider_id, provider_config) == "anthropic-compatible":
        return "anthropic"
    return "openai_compatible"


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
                if isinstance(provider_id, str)
                and provider_id.strip()
                and isinstance(provider_config, dict)
            }
    auth_entries = {
        provider_id.strip().lower(): entry
        for provider_id, entry in read_opencode_provider_auth_entries(home=home).items()
        if provider_id.strip()
    }
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
        config_key = options.get("apiKey")
        auth_entry = auth_entries.get(provider_id, {})
        auth_key = auth_entry.get("key") if auth_entry.get("type") == "api" else None
        secret = config_key if isinstance(config_key, str) and config_key.strip() else auth_key
        if not isinstance(secret, str) or not secret.strip():
            continue
        secret = secret.strip()
        raw_base_url = options.get("baseURL")
        base_url = raw_base_url.strip() if isinstance(raw_base_url, str) and raw_base_url.strip() else None
        if base_url is None and provider_id not in {"anthropic", "openai"}:
            continue
        action: MigrationAction = "import"
        item_id, source_id = _ids(
            "opencode",
            "opencode_provider",
            provider_id,
            action,
            _stable_suffix(secret, base_url or ""),
        )
        detail = f"{provider_id} · {mask_credential(secret)}"
        if base_url:
            detail += " + baseURL"
        items.append(
            NativeMigrationItem(
                id=item_id,
                source_id=source_id,
                backend="opencode",
                kind="opencode_provider",
                masked_detail=detail,
                proposed_action=action,
                selected=True,
                notes_key=None,
                vendor=provider_id,
                protocol=_opencode_protocol(provider_id, provider_config),
                display_name=provider_id,
                base_url=base_url,
                secret=secret,
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
        *_codex_items(config, home=home, mask_credential=mask_credential),
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
        available = scan_native_configs(
            previous,
            mask_credential=mask_credential,
            claude_oauth_probe=host.migration_claude_oauth_probe,
        )
        by_id = {item.id: item for item in available}
        missing = [item_id for item_id in item_ids if item_id not in by_id]
        if missing:
            raise MigrationConflictError

        selected = [by_id[item_id] for item_id in item_ids]
        updated = host._clone_config(previous)
        existing_ids = {source.id for source in updated.sources}
        if any(item.source_id in existing_ids for item in selected):
            raise MigrationConflictError

        provisioned: list[tuple[str, str]] = []
        persisted = False
        try:
            for item in selected:
                source = _new_source(
                    item,
                    now=host.now(),
                    validate_base_url=validate_base_url,
                )
                if item.proposed_action == "controlled_import":
                    raise MigrationConflictError
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
                    host._apply_discovered_models(source, [], discovered)
                updated.sources.append(source)
                updated.priority_order.append(source.id)

            await host._commit_synced(previous, updated)
            persisted = True
            return len(selected)
        finally:
            if not persisted:
                for source_id, credential_ref in reversed(provisioned):
                    await host._rollback_credential(source_id, credential_ref)
