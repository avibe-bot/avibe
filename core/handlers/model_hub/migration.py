"""Native-config discovery and transactional Model Hub credential takeover."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Optional, Protocol, cast

from config.v2_config import (
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    ObservationDiscovery,
    ObservationOutcome,
    OAuthCredentialRejectedError,
    SourceObservation,
)
from core.handlers.model_hub.events import contains_credential_material
from core.handlers.model_hub.identifiers import canonical_model_id
from core.handlers.model_hub.reasoning_tiers import resolve_reasoning_tiers
from core.handlers.model_hub.migration_files import (
    claude_settings_paths,
    codex_config_paths,
    opencode_auth_path,
    opencode_config_paths,
    plan_native_cleanup,
    read_native_config,
    read_native_toml,
)
from core.handlers.model_hub.migration_journal import (
    NativeFileEdit,
    NativeTakeoverJournal,
    TakeoverStateError,
)
from vibe.backend_model_catalog import (
    backend_model_entries,
    bundled_catalog_reasoning_efforts_by_model,
    load_bundled_catalog,
)
from vibe.codex_config import (
    read_codex_auth_state,
)
from vibe.native_oauth_store import (
    NativeOAuthSnapshot,
    apply_keychain_edit,
    read_native_oauth,
)
from vibe.opencode_config import (
    get_opencode_custom_provider_adapter,
)

MigrationAction = Literal["import", "controlled_import", "keep_native", "reauth"]
MigrationKind = Literal["api_key", "oauth_native", "opencode_provider"]
_CUSTOM_ENDPOINT_NOTE = "settings.models.source.customEndpoint"
_NATIVE_SUPPLY_NOTE = "settings.models.source.nativeSupply"
_OPENCODE_BUILTIN_PROTOCOLS: dict[
    str,
    Literal["anthropic", "openai_chat"],
] = {
    "deepseek": "openai_chat",
    "minimax": "anthropic",
    "openrouter": "openai_chat",
}
_OPENCODE_UNSUPPORTED_NATIVE_IDS = {"alibaba-cn", "poe"}
# Pinned CPA ordinary-grant compatibility. Extra native scopes are permitted,
# but the gateway does not promise to retain connector/plugin capabilities.
_OAUTH_CLIENT_IDS = {
    "codex": "app_EMoamEEZ73f0CkXaXp7hrann",
    "claude": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
}
_OAUTH_REFRESH_SCOPES = {
    "codex": frozenset({"openid", "profile", "email"}),
    "claude": frozenset({
        "user:profile", "user:inference", "user:sessions:claude_code",
        "user:mcp_servers", "user:file_upload",
    }),
}


class MigrationConflictError(ValueError):
    pass


class MigrationCredentialsInvalidError(RuntimeError):
    """Custody transferred, but one or more grants require Hub reauthentication."""


class MigrationHost(Protocol):
    store: Any
    adapter: Any
    revocations: Any
    _mutation_lock: Any
    now: Callable[[], datetime]
    migration_home: Optional[Path]
    migration_project_roots: Callable[[], tuple[Path, ...]]
    migration_journal: NativeTakeoverJournal
    migration_blocked_backends: set[str]
    migration_guard: Any
    _migration_lock: Any
    _engine_synced: bool

    @staticmethod
    def _clone_config(config: ModelHubConfig) -> ModelHubConfig: ...

    @staticmethod
    def _mark_source_unverified(source: ModelHubSourceConfig) -> None: ...

    async def _engine_call(self, awaitable: Awaitable[Any]) -> Any: ...

    async def _commit_synced(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
        *,
        rollback_on_sync_failure: bool = True,
    ) -> None: ...

    async def _ensure_runtime_dependency(self) -> Any: ...

    def _save_config(self, config: ModelHubConfig) -> ModelHubConfig: ...

    async def _sync_sources(self, config: ModelHubConfig, *, force_empty: bool = False) -> None: ...

    async def _rollback_credential(
        self,
        source_id: str,
        credential_ref: str,
    ) -> None: ...

    async def _provision_oauth_credential(
        self,
        source_id: str,
        vendor: str,
        material: Mapping[str, object],
        *, on_reserved: Callable[[str], None] | None = None,
    ) -> str: ...

    async def _require_proven_source_payload(
        self,
        payload: dict[str, object],
        *, on_reserved: Callable[[str], None] | None = None,
    ) -> SourceObservation: ...

    async def _require_proven_observation(
        self, vendor: str, base_url: str | None, credential_ref: str,
        protocol_order: tuple[str, ...],
    ) -> SourceObservation: ...

    def _apply_discovered_models(
        self,
        source: ModelHubSourceConfig,
        manual_models: list[ModelHubModelConfig],
        discovered: list[DiscoveredModel],
        *,
        allow_empty: bool = False,
        catalog_efforts_by_model: Mapping[str, tuple[str, ...]] | None = None,
    ) -> list[tuple[str, Literal["upstream", "catalog"]]]: ...

    def _apply_source_placement(
        self,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
    ) -> None: ...

    def _added_to(self, source_id: str) -> list[dict]: ...


@dataclass(frozen=True)
class NativeManualModel:
    id: str
    display_name: Optional[str] = None


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
    ]
    display_name: str
    base_url: Optional[str] = None
    secret: Optional[str] = field(default=None, repr=False)
    account_label: Optional[str] = None
    native_provider_id: Optional[str] = field(default=None, repr=False)
    manual_models: tuple[NativeManualModel, ...] = ()
    # Already-masked credential text, produced by the same `mask_credential` the
    # producer used for `masked_detail`. Carried as its own field so a client can
    # render provider and key as separate elements without re-parsing the
    # composed detail string; never holds plaintext.
    masked_credential: Optional[str] = None
    # Native OAuth material is process-local only. It is converted into the
    # engine's auth-file shape before the source is persisted and is never
    # serialized in the scan response.
    oauth_material: Optional[dict[str, object]] = field(default=None, repr=False)
    native_store_edit: Optional[dict[str, object]] = field(default=None, repr=False)
    native_store_revision: Optional[str] = field(default=None, repr=False)
    # A metadata-only OS-store row represents one selected credential
    # container. Its API-key/OAuth components are resolved after consent.
    native_store_placeholder: bool = False

    def to_payload(self) -> dict[str, object]:
        # Presentation metadata is additive: `vendor` and `display_name` let a
        # client name and draw the provider instead of inferring identity from
        # `backend` (which cannot tell an OpenCode config key from an auth.json
        # one). Every pre-existing key keeps its exact value so the broader
        # settings migration surface is untouched.
        return {
            "id": self.id,
            "backend": self.backend,
            "kind": self.kind,
            "masked_detail": self.masked_detail,
            "proposed_action": self.proposed_action,
            "selected": self.selected,
            "notes_key": self.notes_key,
            "vendor": self.vendor,
            "display_name": self.display_name,
            "masked_credential": self.masked_credential,
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


def _oauth_field(payload: Mapping[str, object], *names: str) -> object:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    return None


def _oauth_text(payload: Mapping[str, object], *names: str) -> Optional[str]:
    value = _oauth_field(payload, *names)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _jwt_claims(token: str | None) -> dict[str, object]:
    """Read local grant metadata, not an authentication or signature verdict."""
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _normalize_oauth_material(
    *,
    provider: Literal["claude", "codex"],
    payload: Mapping[str, object],
    account_id: Optional[str] = None,
) -> Optional[dict[str, object]]:
    """Convert native CLI OAuth JSON into the engine auth-file shape."""

    nested = payload
    if provider == "claude" and isinstance(payload.get("claudeAiOauth"), dict):
        nested = cast(Mapping[str, object], payload["claudeAiOauth"])
    if provider == "codex" and isinstance(payload.get("tokens"), dict):
        nested = cast(Mapping[str, object], payload["tokens"])

    # Official native stores often omit client/scope metadata. Their locator
    # establishes the ordinary CLI grant; explicit contrary metadata must
    # never be discarded and silently replaced with CPA's fixed defaults.
    for metadata in (payload, nested):
        for name in ("client_id", "clientId"):
            if name in metadata and metadata[name] != _OAUTH_CLIENT_IDS[provider]:
                return None
        for name in ("scope", "scopes"):
            if name not in metadata:
                continue
            scopes = metadata[name]
            if isinstance(scopes, str):
                granted = set(scopes.split())
            elif isinstance(scopes, list) and all(isinstance(value, str) for value in scopes):
                granted = set(scopes)
            else:
                return None
            if not _OAUTH_REFRESH_SCOPES[provider].issubset(granted):
                return None

    access_token = _oauth_text(nested, "access_token", "accessToken")
    refresh_token = _oauth_text(nested, "refresh_token", "refreshToken")
    id_token = _oauth_text(nested, "id_token", "idToken")
    # The gateway must own a refresh-capable grant after takeover. An access
    # token without its refresh token would work only until its current expiry,
    # then leave the user with a source that cannot be maintained.
    if not refresh_token:
        return None

    material: dict[str, object] = {
        "type": provider,
    }
    for target, value in (
        ("access_token", access_token),
        ("refresh_token", refresh_token),
        ("id_token", id_token),
        (
            "last_refresh",
            _oauth_field(nested, "last_refresh", "lastRefresh")
            or _oauth_field(payload, "last_refresh", "lastRefresh"),
        ),
        ("email", _oauth_text(nested, "email") or _oauth_text(payload, "email")),
        ("expired", _oauth_field(nested, "expired")),
    ):
        if value is not None:
            material[target] = value
    claims = _jwt_claims(id_token)
    if provider == "codex":
        auth_claims = claims.get("https://api.openai.com/auth")
        if not account_id and isinstance(auth_claims, dict):
            account_id = _oauth_text(auth_claims, "chatgpt_account_id")
        # CPA's importer does not derive the account header from the id token.
        if not account_id:
            return None
        material["account_id"] = account_id
        email = _oauth_text(claims, "email")
        if email:
            material.setdefault("email", email)
    if "expired" not in material:
        expires_at = _oauth_field(nested, "expiresAt", "expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            # Claude's native expiresAt is epoch milliseconds.
            seconds = expires_at / 1000 if "expiresAt" in nested else expires_at
        else:
            seconds = _jwt_claims(access_token).get("exp")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            try:
                material["expired"] = datetime.fromtimestamp(
                    seconds, tz=timezone.utc
                ).isoformat()
            except (OverflowError, OSError, ValueError):
                return None
    if provider == "claude":
        for target, names in (
            ("account_uuid", ("account_uuid", "accountUuid")),
            ("organization_uuid", ("organization_uuid", "organizationUuid")),
            ("organization_name", ("organization_name", "organizationName")),
            ("claude_device_ids", ("claude_device_ids", "claudeDeviceIds")),
        ):
            value = _oauth_field(nested, *names)
            if value is not None:
                material[target] = value
    return material


def _read_codex_oauth_material(
    auth_data: Mapping[str, object],
) -> Optional[dict[str, object]]:
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    account_id: Optional[str] = None
    raw_account_id = tokens.get("account_id") or auth_data.get("account_id")
    if isinstance(raw_account_id, str) and raw_account_id.strip():
        account_id = raw_account_id.strip()
    return _normalize_oauth_material(
        provider="codex",
        payload=auth_data,
        account_id=account_id,
    )


def _native_store_items(
    backend: Literal["claude", "codex"],
    snapshot: NativeOAuthSnapshot | None,
    *,
    mask_credential: Callable[[str], str],
    state: Mapping[str, object] | None = None,
) -> list[NativeMigrationItem]:
    if snapshot is None:
        return []
    payload = snapshot.payload or {}
    state = state or {}
    vendor = "anthropic" if backend == "claude" else "openai"
    oauth_protocol = "anthropic" if backend == "claude" else "openai_responses"
    material = (
        _normalize_oauth_material(provider="claude", payload=payload)
        if backend == "claude" else _read_codex_oauth_material(payload)
    ) if snapshot.exportable else None
    secret = _oauth_text(payload, "OPENAI_API_KEY") if backend == "codex" and snapshot.exportable else None
    placeholder = payload.get("store") == "keychain" and payload.get("status") == "metadata_only"
    blocked = not snapshot.exportable and not placeholder
    has_oauth = backend == "claude" or bool(payload.get("tokens"))
    if has_oauth and material is None and not placeholder:
        blocked = True
    primary_id = f"mig_{_stable_suffix(backend, 'native-store', snapshot.revision)}"
    items: list[NativeMigrationItem] = []
    if has_oauth or placeholder or blocked:
        action: MigrationAction = "keep_native" if blocked else "import"
        _, source_id = _ids(backend, "oauth_native", "native-store", action)
        account = _safe_account_label(material.get("email")) if material else None
        items.append(NativeMigrationItem(
            id=primary_id, source_id=source_id, backend=backend,
            kind="oauth_native", masked_detail=account or "",
            proposed_action=action, selected=not blocked,
            notes_key=_NATIVE_SUPPLY_NOTE, vendor=vendor,
            protocol=cast(Any, oauth_protocol),
            display_name="Claude" if backend == "claude" else "ChatGPT",
            account_label=account, oauth_material=material,
            native_provider_id=_oauth_text(state, "active_provider_id"),
            native_store_edit=snapshot.keychain_edit,
            native_store_revision=snapshot.revision,
            native_store_placeholder=placeholder,
        ))
    if secret:
        base_url = _oauth_text(state, "base_url")
        protocol = "openai_chat" if state.get("wire_api") == "chat" else "openai_responses"
        _, source_id = _ids(backend, "api_key", "native-store", "import")
        # Metadata-only consent binds this same primary ID for an API-only bag.
        item_id = primary_id if not items else f"mig_{_stable_suffix(primary_id, 'api_key')}"
        detail = mask_credential(secret)
        items.append(NativeMigrationItem(
            id=item_id, source_id=source_id, backend=backend, kind="api_key",
            masked_detail=detail, proposed_action="import", selected=True,
            notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
            vendor=vendor, protocol=cast(Any, protocol), display_name="OpenAI",
            base_url=base_url, secret=secret, masked_credential=detail,
            native_provider_id=_oauth_text(state, "active_provider_id"),
            native_store_edit=snapshot.keychain_edit,
            native_store_revision=snapshot.revision,
        ))
    if backend == "codex":
        routing_revision = _stable_suffix(
            _oauth_text(state, "base_url") or "",
            _oauth_text(state, "wire_api") or "",
            _oauth_text(state, "active_provider_id") or "",
        )
        items = [replace(item, id=f"mig_{_stable_suffix(item.id, routing_revision)}") for item in items]
    return items


def _blocked_item(backend: str, identity: str, reason: str = "config") -> NativeMigrationItem:
    item_id, source_id = _ids(backend, "api_key", identity, "reauth")
    return NativeMigrationItem(
        id=item_id, source_id=source_id, backend=cast(Any, backend),
        kind="api_key", masked_detail="", proposed_action="reauth",
        selected=False, notes_key=f"settings.models.migration.blocked.{reason}",
        vendor={"claude": "anthropic", "codex": "openai", "opencode": "opencode"}[backend],
        protocol="anthropic" if backend == "claude" else "openai_responses",
        display_name={"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}[backend],
    )


def _claude_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
    allow_secret: bool = False,
    project_roots: tuple[Path, ...] = (),
) -> list[NativeMigrationItem]:
    items: list[NativeMigrationItem] = []
    for path in claude_settings_paths(home, project_roots):
        try:
            config = read_native_config(path)
        except (TakeoverStateError, OSError):
            items.append(_blocked_item("claude", str(path)))
            continue
        if config is None:
            continue
        env = config.get("env", {})
        if not isinstance(env, dict):
            items.append(_blocked_item("claude", str(path)))
            continue
        base_url = _oauth_text(env, "ANTHROPIC_BASE_URL")
        api_key = _oauth_text(env, "ANTHROPIC_API_KEY")
        if api_key:
            item_id, source_id = _ids(
                "claude", "api_key", str(path), "import",
                _stable_suffix(api_key, base_url or ""),
            )
            detail = mask_credential(api_key)
            items.append(NativeMigrationItem(
                id=item_id, source_id=source_id, backend="claude", kind="api_key",
                masked_detail=detail, proposed_action="import", selected=True,
                notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
                vendor="anthropic", protocol="anthropic", display_name="Anthropic",
                base_url=base_url, secret=api_key, masked_credential=detail,
            ))
        if config.get("apiKeyHelper") or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            items.append(_blocked_item("claude", f"{path}:helper-or-token", "credential"))

    items.extend(_native_store_items(
        "claude", read_native_oauth("claude", home=home, allow_secret=allow_secret),
        mask_credential=mask_credential,
    ))
    return items


def _codex_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
    allow_secret: bool = False,
    project_roots: tuple[Path, ...] = (),
) -> list[NativeMigrationItem]:
    items = _native_store_items(
        "codex", read_native_oauth("codex", home=home, allow_secret=allow_secret),
        mask_credential=mask_credential, state=read_codex_auth_state(home),
    )
    for path in codex_config_paths(home, project_roots):
        try:
            config = read_native_toml(path)
        except (TakeoverStateError, OSError):
            items.append(_blocked_item("codex", str(path)))
            continue
        if config is None:
            continue
        providers = config.get("model_providers", {})
        if not isinstance(providers, dict):
            items.append(_blocked_item("codex", str(path)))
            continue
        for provider_id, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            key = _oauth_text(provider, "experimental_bearer_token")
            env_key = _oauth_text(provider, "env_key")
            headers = provider.get("http_headers", {})
            env_headers = provider.get("env_http_headers", {})
            if (
                (env_key and os.environ.get(env_key))
                or env_headers
                or (headers and (key or config.get("model_provider") == provider_id))
            ):
                # Header templates and shell-owned keys cannot be silently
                # reinterpreted as an ordinary Authorization bearer grant.
                items.append(_blocked_item("codex", f"{path}:{provider_id}", "environment"))
                continue
            if not key:
                continue
            base_url = _oauth_text(provider, "base_url")
            protocol = "openai_chat" if provider.get("wire_api") == "chat" else "openai_responses"
            item_id, source_id = _ids(
                "codex", "api_key", f"{path}:{provider_id}", "import",
                _stable_suffix(key, base_url or "", protocol),
            )
            masked = mask_credential(key)
            items.append(NativeMigrationItem(
                id=item_id, source_id=source_id, backend="codex", kind="api_key",
                masked_detail=masked, proposed_action="import", selected=True,
                notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
                vendor="openai", protocol=protocol, display_name="OpenAI",
                secret=key, base_url=base_url, masked_credential=masked,
                native_provider_id=provider_id,
            ))
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
) -> Optional[Literal["anthropic", "openai_responses", "openai_chat"]]:
    if provider_id == "anthropic":
        return "anthropic"
    if provider_id == "openai":
        return "openai_responses"
    custom_adapter = get_opencode_custom_provider_adapter(provider_id, provider_config)
    if custom_adapter == "anthropic-compatible":
        return "anthropic"
    if custom_adapter == "openai-compatible":
        return "openai_chat"
    builtin_protocol = _OPENCODE_BUILTIN_PROTOCOLS.get(provider_id)
    if builtin_protocol is not None:
        return builtin_protocol
    npm = provider_config.get("npm") or catalog_provider.get("npm")
    if npm == "@ai-sdk/anthropic":
        return "anthropic"
    if npm == "@ai-sdk/openai":
        return "openai_responses"
    if npm in {"@ai-sdk/openai-compatible", "@openrouter/ai-sdk-provider"}:
        return "openai_chat"
    return None


def _opencode_manual_models(
    provider_config: dict[str, Any],
) -> tuple[NativeManualModel, ...]:
    raw_models = provider_config.get("models")
    if not isinstance(raw_models, dict):
        return ()
    models: list[NativeManualModel] = []
    for model_id, model_config in raw_models.items():
        model_id = canonical_model_id(model_id)
        if model_id is None or contains_credential_material(model_id):
            continue
        raw_name = model_config.get("name") if isinstance(model_config, dict) else None
        display_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        if display_name and contains_credential_material(display_name):
            display_name = None
        models.append(
            NativeManualModel(
                id=model_id,
                display_name=display_name,
            )
        )
    return tuple(models)


def _opencode_plaintext_key(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if "{env:" in candidate or "{file:" in candidate:
        return None
    return candidate


def _opencode_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
    project_roots: tuple[Path, ...] = (),
) -> list[NativeMigrationItem]:
    items: list[NativeMigrationItem] = []
    try:
        auth_entries = read_native_config(opencode_auth_path(home)) or {}
    except (TakeoverStateError, OSError):
        return [_blocked_item("opencode", "auth-file")]
    provider_catalog = _load_opencode_provider_catalog(home)
    seen_providers: set[str] = set()
    for path in opencode_config_paths(home, project_roots):
        try:
            config = read_native_config(path, jsonc=True)
        except (TakeoverStateError, OSError):
            items.append(_blocked_item("opencode", str(path)))
            continue
        if config is None:
            continue
        provider_configs = config.get("provider", {})
        if not isinstance(provider_configs, dict):
            items.append(_blocked_item("opencode", str(path)))
            continue
        seen_providers.update(provider_configs)
        relevant_auth = {key: value for key, value in auth_entries.items() if key in provider_configs}
        items.extend(_opencode_candidates(
            provider_configs, relevant_auth, provider_catalog, str(path), mask_credential,
        ))
    unseen_auth = {key: value for key, value in auth_entries.items() if key not in seen_providers}
    items.extend(_opencode_candidates({}, unseen_auth, provider_catalog, "auth-file", mask_credential))
    return items


def _opencode_candidates(
    provider_configs: dict, auth_entries: dict, provider_catalog: dict,
    locator: str, mask_credential: Callable[[str], str],
) -> list[NativeMigrationItem]:
    provider_ids = set(provider_configs) | set(auth_entries)
    items: list[NativeMigrationItem] = []
    for provider_id in sorted(provider_ids):
        if (
            not isinstance(provider_id, str) or not provider_id
            or len(provider_id) > 64
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for char in provider_id)
            or not provider_id[0].isalnum()
            or contains_credential_material(provider_id)
        ):
            items.append(_blocked_item("opencode", f"{locator}:invalid-provider"))
            continue
        provider_config = provider_configs.get(provider_id, {})
        if not isinstance(provider_config, dict):
            items.append(_blocked_item("opencode", f"{locator}:{provider_id}"))
            continue
        options = provider_config.get("options")
        if not isinstance(options, dict):
            options = {}
        key_setting = options.get("apiKey")
        if isinstance(key_setting, str) and (
            "{file:" in key_setting
            or (
                "{env:" in key_setting
                and (
                    re.fullmatch(r"\{env:[^{}]+\}", key_setting) is None
                    or os.environ.get(key_setting[5:-1])
                )
            )
        ):
            items.append(_blocked_item("opencode", f"{locator}:{provider_id}:key-reference", "environment"))
            continue
        config_key = _opencode_plaintext_key(options.get("apiKey"))
        auth_entry = auth_entries.get(provider_id, {})
        if not isinstance(auth_entry, dict):
            items.append(_blocked_item("opencode", f"{locator}:{provider_id}"))
            continue
        auth_key = (
            _opencode_plaintext_key(auth_entry.get("key"))
            if auth_entry.get("type") == "api"
            else None
        )
        secret = config_key or auth_key
        if secret is None:
            if options.get("apiKey") or auth_entry:
                items.append(_blocked_item("opencode", f"{locator}:{provider_id}", "credential"))
            continue
        raw_base_url = options.get("baseURL")
        base_url = raw_base_url.strip() if isinstance(raw_base_url, str) and raw_base_url.strip() else None
        catalog_provider = provider_catalog.get(provider_id, {})
        if base_url is None:
            catalog_api = catalog_provider.get("api")
            if isinstance(catalog_api, str) and catalog_api.strip():
                base_url = catalog_api.strip()
        protocol = _opencode_protocol(provider_id, provider_config, catalog_provider)
        if (
            protocol is None or provider_id in _OPENCODE_UNSUPPORTED_NATIVE_IDS
            or (base_url is None and provider_id not in {"anthropic", "openai"})
            or (auth_entry and auth_entry.get("type") != "api")
        ):
            items.append(_blocked_item("opencode", f"{locator}:{provider_id}", "credential"))
            continue
        manual_models = _opencode_manual_models(provider_config)
        action: MigrationAction = "import"
        item_id, source_id = _ids(
            "opencode",
            "opencode_provider",
            f"{provider_id}:{locator}",
            action,
            _stable_suffix(
                secret,
                base_url or "",
                protocol,
                *(f"{model.id}\0{model.display_name or ''}" for model in manual_models),
            ),
        )
        masked_secret = mask_credential(secret)
        detail = f"{provider_id} · {masked_secret}"
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
                masked_credential=masked_secret,
            )
        )
        if auth_key and auth_key != secret:
            # Preserve both settings and auth-store keys, even when one is
            # currently shadowed. Cleanup cannot discard an unimported account.
            alternate = items[-1]
            masked = mask_credential(auth_key)
            items.append(replace(
                alternate, id=f"mig_{_stable_suffix(alternate.id, auth_key)}",
                source_id=f"src_{_stable_suffix(alternate.source_id, auth_key)}",
                secret=auth_key, masked_detail=f"{provider_id} · {masked}",
                masked_credential=masked,
            ))
    return items


def scan_native_configs(
    config: ModelHubConfig,
    *,
    mask_credential: Callable[[str], str],
    home: Optional[Path] = None,
    validate_base_url: Optional[Callable[[object], Optional[str]]] = None,
    legacy_auth: Mapping[str, Mapping[str, object]] | None = None,
    secret_backends: tuple[str, ...] = (),
    project_roots: tuple[Path, ...] = (),
    clean_native_stores: Mapping[str, str] | None = None,
) -> list[NativeMigrationItem]:
    """Read native stores without modifying or deleting any path."""

    items = [
        *_claude_items(
            home=home,
            mask_credential=mask_credential,
            allow_secret="claude" in secret_backends,
            project_roots=project_roots,
        ),
        *_codex_items(
            home=home, mask_credential=mask_credential,
            allow_secret="codex" in secret_backends,
            project_roots=project_roots,
        ),
        *_opencode_items(home=home, mask_credential=mask_credential, project_roots=project_roots),
    ]
    if home is None:
        # The process cannot remove a key from the parent shell or its startup
        # files. Do not claim complete native takeover while that precedence
        # layer remains active, and never execute a helper to obtain its key.
        inherited = {
            "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"),
            "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY"),
            "opencode": ("OPENCODE_CONFIG_CONTENT", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"),
        }
        for backend, names in inherited.items():
            if any(os.environ.get(name) for name in names):
                items.append(_blocked_item(backend, "inherited-environment", "environment"))
    for backend, auth in (legacy_auth or {}).items():
        if backend not in {"claude", "codex"}:
            continue
        secret = _oauth_text(auth, "api_key")
        base_url = _oauth_text(auth, "base_url")
        if not secret or any(
            item.backend == backend and item.secret == secret and item.base_url == base_url
            for item in items
        ):
            continue
        item_id, source_id = _ids(
            backend, "api_key", "avibe-config", "import",
            _stable_suffix(secret, base_url or ""),
        )
        masked = mask_credential(secret)
        items.append(NativeMigrationItem(
            id=item_id, source_id=source_id, backend=cast(Any, backend),
            kind="api_key", masked_detail=masked, proposed_action="import",
            selected=True, notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
            vendor="anthropic" if backend == "claude" else "openai",
            protocol="anthropic" if backend == "claude" else "openai_responses",
            display_name="Anthropic" if backend == "claude" else "OpenAI",
            secret=secret, base_url=base_url, masked_credential=masked,
        ))
    if validate_base_url is not None:
        valid_items: list[NativeMigrationItem] = []
        for item in items:
            try:
                validate_base_url(item.base_url)
            except Exception:
                valid_items.append(replace(
                    item, proposed_action="reauth", selected=False, secret=None,
                    base_url=None, notes_key="settings.models.migration.blocked.config",
                ))
                continue
            valid_items.append(item)
        items = valid_items
    existing_native_sources = {
        source.vendor: source
        for source in config.sources
        if source.kind == "subscription" and source.supply_channel == "native_cli"
    }
    candidates: list[NativeMigrationItem] = []
    for item in items:
        if (
            item.native_store_placeholder
            and (clean_native_stores or {}).get(item.backend) == item.native_store_revision
        ):
            # A completed cleanup verified this exact metadata revision holds
            # only unrelated native data (e.g. MCP OAuth). Do not read it again
            # merely to rediscover the absence of subscription credentials.
            continue
        native_source = existing_native_sources.get(item.vendor)
        if item.kind == "oauth_native" and native_source is not None:
            candidates.append(replace(
                item,
                source_id=native_source.id,
                id=f"mig_{_stable_suffix(item.id, native_source.id)}",
            ))
            continue
        candidates.append(item)
    return candidates


def _validated_source(
    item: NativeMigrationItem,
    *,
    now: datetime,
    protocol: Literal["anthropic", "openai_responses", "openai_chat"],
    validate_base_url: Callable[[object], Optional[str]],
    credential_ref: str | None = None,
    masked_credential: str | None = None,
    discovered: tuple[DiscoveredModel, ...] = (),
    catalog_efforts_by_model: Mapping[str, tuple[str, ...]],
) -> ModelHubSourceConfig:
    keep_native = item.proposed_action == "keep_native"
    controlled = item.proposed_action == "controlled_import"
    imported_oauth = item.kind == "oauth_native" and item.proposed_action == "import"
    discovered_at = now.isoformat()
    models = []
    if keep_native or imported_oauth:
        model_ids = (
            tuple(model.id for model in discovered)
            if imported_oauth and discovered
            else _native_model_ids(item.backend)
        )
        for model_id in model_ids:
            resolution = resolve_reasoning_tiers(
                protocol=protocol,
                model_id=model_id,
                catalog_efforts_by_model=catalog_efforts_by_model,
            )
            models.append(
                {
                    "id": model_id,
                    "display_name": None,
                    "origin": "discovered",
                    "reasoning_efforts": list(resolution.efforts),
                    "reasoning_efforts_source": resolution.source,
                    "discovered_at": discovered_at,
                }
            )
    payload: dict[str, object] = {
        "id": item.source_id,
        "created_at": discovered_at,
        "last_discovered_at": discovered_at if models else None,
        "kind": "subscription" if keep_native or controlled or imported_oauth else "api_key",
        "vendor": item.vendor,
        "display_name": item.display_name,
        "protocol": protocol,
        "base_url": validate_base_url(item.base_url),
        "supply_channel": "native_cli" if keep_native else "hub",
        "billing": "monthly" if keep_native or controlled or imported_oauth else "metered",
        "state": {"status": "standby", "retry_at": None, "detail_key": None},
        "usage": {
            "cycle_used_pct": None,
            "month_spend_cents": None,
            "currency": None,
            "projected_exhaust_at": None,
        },
        "models": models,
        "credential_ref": credential_ref,
        "account_label": item.account_label,
        "masked_credential": masked_credential,
    }
    try:
        return ModelHubSourceConfig.from_payload(payload)
    except ValueError:
        # Canonical Source validation is the last input boundary before custody.
        # Invalid native metadata must not escape as an unredacted server error.
        raise MigrationConflictError from None


def _native_auth_snapshot(host: MigrationHost, backends: tuple[str, ...]) -> dict:
    reader = getattr(host.store, "native_auth_snapshot", None)
    return reader(backends) if callable(reader) else {}


async def _prepare_takeover(
    host: MigrationHost,
    previous: ModelHubConfig,
    selected: list[NativeMigrationItem],
    *,
    mask_credential: Callable[[str], str],
    validate_base_url: Callable[[object], Optional[str]],
    consented: list[NativeMigrationItem] | None = None,
    project_roots: tuple[Path, ...] = (),
    retained_source_ids: tuple[str, ...] | None = None,
    clean_native_stores: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Stage all grants and durable before/after images, still native-owned."""
    clean_native_stores = dict(clean_native_stores or {})
    cleanup_items = [
        *selected,
        *(item for item in (consented or []) if (
            item.native_store_placeholder and item.backend in clean_native_stores
        )),
    ]
    edits = plan_native_cleanup(cleanup_items, home=host.migration_home, project_roots=project_roots)
    updated = host._clone_config(previous)
    provisioned: list[dict[str, str]] = []
    source_ids: list[str] = list(retained_source_ids or ())
    catalog = bundled_catalog_reasoning_efforts_by_model()
    handoff_attempted = False
    try:
        # A completed receipt can authorize cleanup of the exact old material
        # reintroduced by an external writer. Its current Hub refs are already
        # authoritative; never provision that old OAuth snapshot again.
        for item in selected if retained_source_ids is None else ():
            protocol = item.protocol
            observation: SourceObservation | None = None
            existing = next((source for source in updated.sources if source.id == item.source_id), None)
            if item.kind != "oauth_native":
                if not item.secret:
                    raise MigrationConflictError
                # Old copy-only imports, including manually created sources,
                # can retain their identity and routing without another key.
                for candidate in updated.sources:
                    if (
                        candidate.kind == "api_key"
                        and candidate.vendor == item.vendor
                        and candidate.protocol == protocol
                        and candidate.base_url == validate_base_url(item.base_url)
                        and candidate.credential_ref
                        and await host._engine_call(host.adapter.matches_api_key_credential(
                            candidate.credential_ref, item.vendor, protocol,
                            item.secret, validate_base_url(item.base_url),
                        ))
                    ):
                        # Identity reuse is not current authentication proof.
                        # Observe the exact existing target/ref without changing
                        # its inventory, state, order, or user-owned routes.
                        await host._require_proven_observation(
                            candidate.vendor, candidate.base_url,
                            candidate.credential_ref, (candidate.protocol,),
                        )
                        source_ids.append(candidate.id)
                        break
                else:
                    observation = await host._require_proven_source_payload({
                        "vendor": item.vendor,
                        "base_url": validate_base_url(item.base_url),
                        "key": item.secret,
                    }, on_reserved=lambda ref: host.revocations.add("observation", ref))
                    protocol = cast(Any, observation.protocol)
                if observation is None:
                    continue
            if existing is not None and not (
                existing.kind == "subscription"
                and existing.supply_channel == "native_cli"
                and item.kind == "oauth_native"
            ):
                # A different key at the same native locator is a new Source,
                # not permission to replace an already edited Hub source.
                item = replace(item, source_id=f"src_{_stable_suffix(item.id, 'takeover')}")
                existing = None

            def reserved(ref: str) -> None:
                # Use the final Source identity so recovery retains current
                # Hub grants instead of misclassifying them as orphaned keys.
                host.revocations.add(item.source_id, ref)
                provisioned.append({
                    "source_id": item.source_id, "credential_ref": ref,
                    "kind": "oauth" if item.kind == "oauth_native" else "api_key",
                })

            if item.kind == "oauth_native":
                if not item.oauth_material:
                    raise MigrationConflictError
                credential_ref = await host._engine_call(host._provision_oauth_credential(
                    item.source_id, item.vendor, item.oauth_material,
                    on_reserved=reserved,
                ))
            else:
                credential_ref = await host._engine_call(host.adapter.provision_credential(
                    item.vendor, protocol, item.secret, validate_base_url(item.base_url),
                    on_reserved=reserved,
                ))
            if not provisioned or provisioned[-1]["credential_ref"] != credential_ref:
                raise MigrationConflictError
            source = _validated_source(
                item, now=host.now(), protocol=protocol,
                validate_base_url=validate_base_url, credential_ref=credential_ref,
                masked_credential=mask_credential(item.secret) if item.secret else None,
                catalog_efforts_by_model=catalog,
            )
            if observation is not None:
                manual = [ModelHubModelConfig.from_payload({
                    "id": model.id, "display_name": model.display_name,
                    "origin": "manual", "reasoning_efforts": [], "discovered_at": None,
                }) for model in item.manual_models]
                host._apply_discovered_models(
                    source, manual, list(observation.models),
                    allow_empty=True, catalog_efforts_by_model=catalog,
                )
                if observation.discovery is not ObservationDiscovery.SUCCEEDED:
                    host._mark_source_unverified(source)
            if existing is not None:
                # Custody changes, not the identity/menu/orders the user owns.
                replacement = existing.to_payload()
                replacement.update({
                    "supply_channel": "hub", "credential_ref": credential_ref,
                    "account_label": item.account_label or existing.account_label,
                    "state": source.state.to_payload(),
                })
                if not existing.models:
                    replacement["models"] = [model.to_payload() for model in source.models]
                source = ModelHubSourceConfig.from_payload(replacement)
                updated.sources = [source if value.id == source.id else value for value in updated.sources]
            else:
                updated.sources.append(source)
                host._apply_source_placement(updated, source)
            source_ids.append(source.id)
        backends = sorted({item.backend for item in (consented or selected)})
        native_before = _native_auth_snapshot(host, tuple(backends))
        credential_backends = {item.backend for item in selected}
        native_after = {
            backend: ({
                name: ("oauth" if name == "auth_mode" else True if name == "auth_mode_set" else None)
                for name in values
            } if backend in credential_backends else dict(values))
            for backend, values in native_before.items()
        }
        updated.enabled = True
        for backend in backends:
            updated.agents[backend].mode = "hub"
        updated = ModelHubConfig.from_payload(updated.to_payload())
        for edit in edits:
            edit.check()
        record = {
            "version": 1, "phase": "prepared",
            "items": [item.to_payload() for item in (consented or selected)],
            "backends": backends, "source_ids": source_ids,
            "credentials": provisioned,
            "previous": previous.to_payload(), "updated": updated.to_payload(),
            "files": [edit.to_payload() for edit in edits],
            "native_before": native_before, "native_after": native_after,
            "keychain": [],
            "clean_native_stores": clean_native_stores,
        }
        seen_stores: set[str] = set()
        for item in selected:
            edit = item.native_store_edit
            if not edit or item.native_store_revision in seen_stores:
                continue
            operations = [operation for operation in edit["operations"] if operation["kind"] == "keychain"]
            if operations:
                record["keychain"].append({**edit, "operations": operations})
            seen_stores.add(item.native_store_revision)
        # Replacement can succeed before a durability/readback failure is
        # reported. From this attempt onward recovery owns the refs: the
        # prepared record if present, otherwise their write-ahead revocations.
        # Never revoke a grant that a possibly durable prepared record needs.
        handoff_attempted = True
        host.migration_journal.save(record)
        return record
    finally:
        if not handoff_attempted:
            for credential in reversed(provisioned):
                await host._rollback_credential(
                    credential["source_id"],
                    credential["credential_ref"],
                )


async def _revert_takeover(host: MigrationHost, record: dict[str, Any]) -> None:
    record["phase"] = "reverting"
    host.migration_journal.save(record)
    previous = ModelHubConfig.from_payload(record["previous"])
    updated = ModelHubConfig.from_payload(record["updated"])
    current = host.store.load()
    if current.to_payload() not in (previous.to_payload(), updated.to_payload()):
        raise MigrationConflictError
    if current.to_payload() == updated.to_payload():
        host._save_config(previous)
        host._engine_synced = False
    for raw in reversed(record["files"]):
        NativeFileEdit.from_payload(raw).apply(reverse=True)
    for edit in reversed(record.get("keychain", [])):
        await asyncio.to_thread(apply_keychain_edit, edit, reverse=True)
    for credential in reversed(record["credentials"]):
        await host._rollback_credential(
            credential["source_id"],
            credential["credential_ref"],
        )
    host.migration_journal.forget()
    host.migration_blocked_backends.difference_update(record["backends"])


async def _verify_clean_native_stores(host: MigrationHost, record: Mapping[str, Any]) -> None:
    for backend, revision in record.get("clean_native_stores", {}).items():
        snapshot = await asyncio.to_thread(
            read_native_oauth, backend, home=host.migration_home,
        )
        if (
            snapshot is None or snapshot.revision != revision
            or (snapshot.payload or {}).get("status") != "metadata_only"
        ):
            raise MigrationConflictError


async def _resume_takeover(
    host: MigrationHost,
    record: dict[str, Any],
    verify_idle: Callable[[], Awaitable[None]] | None = None,
) -> tuple[int, list[dict]]:
    if record["phase"] == "complete":
        current = host.store.load()
        if (
            not set(record["source_ids"]).issubset(source.id for source in current.sources)
            or any(current.agents[backend].mode != "hub" for backend in record["backends"])
            or record.get("source_credentials") != {
                source.id: source.credential_ref
                for source in current.sources if source.id in record["source_ids"]
            }
        ):
            raise MigrationConflictError
        if record.get("outcome") == "needs_auth":
            raise MigrationCredentialsInvalidError
        if record.get("outcome") == "reauth_requested":
            raise MigrationConflictError
        return len(record["items"]), [
            position for source_id in record["source_ids"] for position in host._added_to(source_id)
        ]
    host.migration_blocked_backends.update(record["backends"])
    if verify_idle is None:
        raise MigrationConflictError
    if record["phase"] == "reverting":
        await _revert_takeover(host, record)
        raise MigrationConflictError
    try:
        await _verify_clean_native_stores(host, record)
        if record["phase"] == "prepared":
            # Installation and host compatibility are knowable before custody.
            # Staged grants are outside CPA's watched directory, so ensuring
            # the dependency here cannot publish them. An unavailable runtime
            # must leave a working native login intact.
            await host._ensure_runtime_dependency()
            await verify_idle()
            await _verify_clean_native_stores(host, record)
            for edit in record.get("keychain", []):
                await asyncio.to_thread(apply_keychain_edit, edit)
            for raw in record["files"]:
                NativeFileEdit.from_payload(raw).apply()
            record["phase"] = "withdrawn"
            host.migration_journal.save(record)
        previous = ModelHubConfig.from_payload(record["previous"])
        updated = ModelHubConfig.from_payload(record["updated"])
        current = host.store.load()
        terminal = record.get("terminal")
        if current.to_payload() not in (
            previous.to_payload(), updated.to_payload(),
            terminal.get("config") if terminal else None,
        ):
            raise MigrationConflictError
        if record["phase"] == "withdrawn":
            await _verify_clean_native_stores(host, record)
            # Save the decision without projecting staged grants into CPA.
            # sync_sources is a runtime write, not a config-only operation.
            host._save_config(updated)
            host._engine_synced = False
            # This durable marker precedes any credential exposure to CPA.
            # An empty-container-only confirmation transferred no grant and
            # changed no native bytes: keep it reversible through runtime start
            # so a newly observed login can be presented for fresh consent.
            if (
                record["source_ids"] or record["credentials"]
                or record["files"] or record.get("keychain")
                or record["native_before"] != record["native_after"]
            ):
                record["phase"] = "exposed"
            host.migration_journal.save(record)
        for raw in record["files"]:
            NativeFileEdit.from_payload(raw).check(applied=True)
        # Store mutations are replayable and must still match their cleaned
        # state before a grant can be published to CPA.
        if record.get("keychain"):
            from vibe.native_oauth_store import check_keychain_edit

            record.setdefault("clean_native_stores", {})
            for edit in record["keychain"]:
                snapshot = await asyncio.to_thread(
                    read_native_oauth, edit["backend"], home=host.migration_home,
                )
                # Bind the public metadata revision to the verified private
                # post-state. A detected intervening login prevents completion.
                await asyncio.to_thread(check_keychain_edit, edit, applied=True)
                if snapshot and (snapshot.payload or {}).get("status") == "metadata_only":
                    record["clean_native_stores"][edit["backend"]] = snapshot.revision
        await verify_idle()
        if terminal:
            return _finish_rejected_takeover(host, record)
        for credential in record["credentials"]:
            if credential["kind"] == "oauth":
                await host._engine_call(host.adapter.activate_oauth_credential(credential["credential_ref"]))
        await host._ensure_runtime_dependency()
        # Always reconcile, including recovery with equal before/after config.
        # _commit_synced intentionally skips equal bindings and cannot do this.
        await host._sync_sources(updated)
        host._engine_synced = True
        await host._engine_call(host.adapter.start())
        invalid_source_ids = []
        for credential in record["credentials"]:
            if credential["kind"] == "oauth":
                if credential["source_id"] in record.get("validated_source_ids", []):
                    continue
                async def validate(ref: str) -> bool:
                    try:
                        await host.adapter.validate_oauth_credential(ref)
                    except OAuthCredentialRejectedError:
                        return False
                    return True

                if not await host._engine_call(validate(credential["credential_ref"])):
                    invalid_source_ids.append(credential["source_id"])
                else:
                    record.setdefault("validated_source_ids", []).append(credential["source_id"])
                    host.migration_journal.save(record)
        await _verify_clean_native_stores(host, record)
        if invalid_source_ids:
            terminal_config = host._clone_config(updated)
            for source in terminal_config.sources:
                if source.id in invalid_source_ids:
                    source.state = ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key="models.source.needs_action.oauth_expired",
                    )
            # Persist the terminal decision before its config write. A crash
            # between either write and the receipt must finish custody, never
            # restore the original grant or repeat a rejected refresh.
            record["terminal"] = {
                "invalid_source_ids": invalid_source_ids,
                "config": terminal_config.to_payload(),
            }
            host.migration_journal.save(record)
            return _finish_rejected_takeover(host, record)
        host.migration_journal.complete(record)
        host.migration_blocked_backends.difference_update(record["backends"])
        return len(record["items"]), [
            position for source_id in record["source_ids"] for position in host._added_to(source_id)
        ]
    except BaseException:
        if record["phase"] in {"prepared", "withdrawn"}:
            await _revert_takeover(host, record)
        raise


def _finish_rejected_takeover(
    host: MigrationHost, record: dict[str, Any],
) -> tuple[int, list[dict]]:
    host._save_config(ModelHubConfig.from_payload(record["terminal"]["config"]))
    host.migration_journal.complete(record)
    host.migration_blocked_backends.difference_update(record["backends"])
    raise MigrationCredentialsInvalidError


async def apply_native_migration(
    host: MigrationHost,
    item_ids: object,
    *,
    mask_credential: Callable[[str], str],
    validate_base_url: Callable[[object], Optional[str]],
) -> tuple[int, list[dict]]:
    """Own a takeover from consent through cleanup; callers shield cancellation."""
    if (
        not isinstance(item_ids, list)
        or not all(isinstance(value, str) and value for value in item_ids)
        or len(set(item_ids)) != len(item_ids)
    ):
        raise MigrationConflictError
    if not item_ids:
        return 0, []
    async with host._migration_lock:
        completed_record = None
        record = host.migration_journal.load() or host.migration_journal.completed()
        if record is not None:
            same_selection = {item["id"] for item in record["items"]} == set(item_ids)
            if record["phase"] != "complete" and not same_selection:
                raise MigrationConflictError
            if same_selection:
                if record["phase"] == "complete":
                    async with host.migration_guard(tuple(record["backends"])) as verify_idle:
                        async with host._mutation_lock:
                            result = await _resume_takeover(host, record)
                            await verify_idle()
                            residual = await asyncio.to_thread(
                                scan_native_configs, host.store.load(),
                                mask_credential=mask_credential, home=host.migration_home,
                                validate_base_url=validate_base_url,
                                legacy_auth=_native_auth_snapshot(host, tuple(record["backends"])),
                                project_roots=host.migration_project_roots(),
                                clean_native_stores=record.get("clean_native_stores"),
                            )
                            if not any(item.backend in record["backends"] for item in residual):
                                return result
                            completed_record = record
                else:
                    async with host.migration_guard(tuple(record["backends"])) as verify_idle:
                        async with host._mutation_lock:
                            return await _resume_takeover(host, record, verify_idle)
        available = await asyncio.to_thread(
            scan_native_configs, host.store.load(), mask_credential=mask_credential,
            home=host.migration_home,
            validate_base_url=validate_base_url,
            legacy_auth=_native_auth_snapshot(host, ("claude", "codex", "opencode")),
            project_roots=host.migration_project_roots(),
            clean_native_stores=(host.migration_journal.completed() or {}).get("clean_native_stores"),
        )
        selected = [item for item in available if item.id in item_ids]
        if len(selected) != len(item_ids) or any(item.proposed_action != "import" for item in selected):
            raise MigrationConflictError
        backends = tuple(sorted({item.backend for item in selected}))
        # A CLI takeover cannot leave an unselected credential maintaining its
        # original authentication. Selection is therefore grouped by backend.
        if any(item.backend in backends and item.id not in item_ids for item in available):
            raise MigrationConflictError
        async with host.migration_guard(backends) as verify_idle:
            async with host._mutation_lock:
                if completed_record is not None:
                    # The guards above and below are separate acquisitions:
                    # recheck persisted ownership before a cleanup-only replay.
                    await _resume_takeover(host, completed_record)
                previous = host.store.load()
                project_roots = host.migration_project_roots()
                rescanned = await asyncio.to_thread(
                    scan_native_configs, previous, mask_credential=mask_credential, home=host.migration_home,
                    validate_base_url=validate_base_url,
                    legacy_auth=_native_auth_snapshot(host, backends),
                    secret_backends=backends,
                    project_roots=project_roots,
                    clean_native_stores=(host.migration_journal.completed() or {}).get("clean_native_stores"),
                )
                consented = selected
                selected = []
                clean_native_stores: dict[str, str] = {}
                for original in consented:
                    resolved = [
                        item for item in rescanned
                        if (
                            item.backend == original.backend
                            and item.native_store_revision == original.native_store_revision
                            and item.native_store_revision is not None
                        )
                    ] if original.native_store_placeholder else [
                        item for item in rescanned if item.id == original.id
                    ]
                    if original.native_store_placeholder and not resolved:
                        # A consent-time secret read can establish that an
                        # opaque container holds only unrelated data. Bind the
                        # absence to the same metadata, routing and Source
                        # identity before recording it as verified clean.
                        metadata_rows = await asyncio.to_thread(
                            scan_native_configs, previous,
                            mask_credential=mask_credential, home=host.migration_home,
                            validate_base_url=validate_base_url,
                            legacy_auth=_native_auth_snapshot(host, backends),
                            project_roots=project_roots,
                        )
                        if any(
                            item.id == original.id
                            and item.native_store_placeholder
                            and item.native_store_revision == original.native_store_revision
                            for item in metadata_rows
                        ) and not any(
                            item.backend == original.backend and item.native_store_revision is not None
                            for item in rescanned
                        ):
                            clean_native_stores[original.backend] = original.native_store_revision
                            continue
                    if original.native_store_placeholder and not any(
                        item.id == original.id for item in resolved
                    ):
                        # Container revision alone does not bind the routing
                        # target or an existing native Source selected by UI.
                        raise MigrationConflictError
                    if not resolved or any(
                        item.proposed_action != "import" or item.native_store_placeholder
                        for item in resolved
                    ):
                        raise MigrationConflictError
                    selected.extend(item for item in resolved if item not in selected)
                if any(item.backend in backends and item not in selected for item in rescanned):
                    raise MigrationConflictError
                record = await _prepare_takeover(
                    host, previous, selected, mask_credential=mask_credential,
                    validate_base_url=validate_base_url,
                    consented=consented,
                    project_roots=project_roots,
                    retained_source_ids=(
                        tuple(completed_record["source_ids"])
                        if completed_record is not None else None
                    ),
                    clean_native_stores=clean_native_stores,
                )
                return await _resume_takeover(host, record, verify_idle)


async def recover_native_migration(host: MigrationHost) -> None:
    """Recover confirmed work before CPA runtime recovery or native admission."""
    record = host.migration_journal.load()
    if record is None or record["phase"] == "complete":
        return
    host.migration_blocked_backends.update(record["backends"])
    async with host._migration_lock:
        async with host.migration_guard(tuple(record["backends"])) as verify_idle:
            async with host._mutation_lock:
                try:
                    await _resume_takeover(host, record, verify_idle)
                except MigrationCredentialsInvalidError:
                    # The durable Source state is the repair entry point.
                    # No native credential or admission gate remains owned.
                    pass


async def prepare_takeover_reauthentication(host: MigrationHost, source_id: str) -> None:
    """Honor explicit Hub reauthentication without guessing refresh validity.

    This is not a migration-success path. The user has acknowledged replacing
    authentication in Hub; release only the completed native withdrawal, keep
    every current engine grant, and let the existing Hub OAuth flow repair it.
    """
    async with host._migration_lock:
        record = host.migration_journal.load()
        if record is None:
            return
        if record["phase"] != "exposed":
            raise MigrationConflictError
        oauth_ids = [
            credential["source_id"] for credential in record["credentials"]
            if credential["kind"] == "oauth"
        ]
        if source_id not in oauth_ids:
            raise MigrationConflictError
        async with host.migration_guard(tuple(record["backends"])) as verify_idle:
            async with host._mutation_lock:
                if not record.get("terminal"):
                    updated = ModelHubConfig.from_payload(record["updated"])
                    if host.store.load().to_payload() != updated.to_payload():
                        raise MigrationConflictError
                    unresolved = [
                        value for value in oauth_ids
                        if value == source_id or value not in record.get("validated_source_ids", [])
                    ]
                    terminal = host._clone_config(updated)
                    for source in terminal.sources:
                        if source.id in unresolved:
                            source.state = ModelHubSourceStateConfig(
                                status="needs_action",
                                detail_key="models.source.needs_action.oauth_expired",
                            )
                    record["terminal"] = {
                        "invalid_source_ids": unresolved,
                        "config": terminal.to_payload(),
                        "reason": "reauth_requested",
                    }
                    host.migration_journal.save(record)
                try:
                    # Reuses strict file/store cleanup checks, persisted config
                    # identity, and idle verification before removing any gate.
                    await _resume_takeover(host, record, verify_idle)
                except MigrationCredentialsInvalidError:
                    pass
