"""Native-config discovery and transactional Model Hub credential takeover."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection, Literal, Mapping, Optional, Protocol, cast

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
    env_reference,
    env_references,
    native_store_items,
    opencode_auth_path,
    opencode_config_paths,
    plan_native_cleanup,
    planned_native_references,
    read_native_config,
)
from core.handlers.model_hub.migration_journal import (
    NativeFileEdit,
    NativeTakeoverJournal,
    TakeoverStateError,
)
from core.handlers.model_hub.migration_persisted import PersistedInventory
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
from vibe.model_hub_runtime.api_key_vendors import (
    validate_api_key_auth_scheme,
    validate_migration_api_key_transport,
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


class MigrationReauthorizationRequiredError(RuntimeError):
    """A changed native snapshot cannot prove a new post-takeover OAuth grant."""


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

    def _reconcile_native_auth(self, backends: tuple[str, ...]) -> None: ...

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
        auth_scheme: str | None = None,
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
    # Codex routing (provider, URL, wire API) the store's credential feeds.
    native_store_routing: Optional[str] = field(default=None, repr=False)
    source_paths: tuple[str, ...] = ()
    required_backends: tuple[str, ...] = ()
    shell_variables: tuple[str, ...] = field(default=(), repr=False)
    shell_auth_variables: tuple[str, ...] = field(default=(), repr=False)
    shell_values: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    file_snapshots: tuple[NativeFileEdit, ...] = field(default=(), repr=False)
    auth_scheme: str | None = field(default=None, repr=False)
    receipt_identity: str | None = field(default=None, repr=False)
    # The native config file itself cannot be parsed, so the CLI fails before
    # any Hub override applies; this row blocks Hub mode, not just import.
    config_blocker: bool = field(default=False, repr=False)
    # The settings ``env`` field a Claude credential came from; cleanup
    # consent is bound to it, not to equal bytes held in another field.
    native_field: str | None = field(default=None, repr=False)

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
            "source_paths": list(self.source_paths),
            "required_backends": list(self.required_backends),
            # The backend's native config cannot be parsed, so Hub mode would
            # fail every launch: the whole group is blocked, not just this row.
            "config_blocker": self.config_blocker,
            # An opaque store resolves only after consent and may hold a key.
            "may_hold_api_key": self.kind != "oauth_native" or (
                self.native_store_placeholder and self.backend == "codex"
            ),
        }


def _stable_suffix(*parts: str) -> str:
    identity = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:16]


def _retained_key_identity(item: NativeMigrationItem) -> str:
    """Name a copied static key independently of the store revision holding it."""
    return "key_" + _stable_suffix(
        item.backend, item.vendor, item.protocol, item.auth_scheme or "",
        item.native_provider_id or "", item.secret or "", item.base_url or "",
    )


def _retained_key_fingerprint(item: NativeMigrationItem) -> str:
    """Name a copied static key by backend and material alone, across routes."""
    return "keymat_" + _stable_suffix(item.backend, (item.secret or "").strip())


def _retained_copy_live(copy: Mapping[str, str] | None, live_credentials: Mapping[str, object]) -> bool:
    """A kept native key is shadowed only while Hub still holds its exact copy."""
    return copy is not None and live_credentials.get(copy["source_id"], None) == copy["credential_ref"]


def _receipt_sources_intact(host: MigrationHost, record: Mapping[str, Any]) -> bool:
    """Whether every Source a completed batch created still holds its credential."""
    current = {source.id: source.credential_ref for source in host.store.load().sources}
    return all(
        source_id in current and current[source_id] == record.get("source_credentials", {}).get(source_id)
        for source_id in record["source_ids"]
    )


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
    source_paths: tuple[str, ...] = (),
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
        note = _NATIVE_SUPPLY_NOTE
        if blocked and payload.get("store") == "file":
            reason = "unreadable" if payload.get("status") == "permission_needed" else "config"
            note = f"settings.models.migration.blocked.{reason}"
        _, source_id = _ids(backend, "oauth_native", "native-store", action)
        account = _safe_account_label(material.get("email")) if material else None
        items.append(NativeMigrationItem(
            id=primary_id, source_id=source_id, backend=backend,
            kind="oauth_native", masked_detail=account or "",
            proposed_action=action, selected=not blocked,
            notes_key=note, vendor=vendor,
            protocol=cast(Any, oauth_protocol),
            display_name="Claude" if backend == "claude" else "ChatGPT",
            account_label=account, oauth_material=material,
            native_provider_id=_oauth_text(state, "active_provider_id"),
            native_store_edit=snapshot.keychain_edit,
            native_store_revision=snapshot.revision,
            native_store_placeholder=placeholder,
            source_paths=source_paths,
            # Codex reads its own auth.json before any Hub routing applies,
            # so one it cannot read or parse fails every launch.
            config_blocker=(
                backend == "codex" and payload.get("store") == "file"
                and payload.get("status") in {"invalid", "permission_needed"}
            ),
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
            source_paths=source_paths,
        ))
    if backend == "codex":
        routing_revision = _stable_suffix(
            _oauth_text(state, "base_url") or "",
            _oauth_text(state, "wire_api") or "",
            _oauth_text(state, "active_provider_id") or "",
        )
        items = [
            replace(
                item, id=f"mig_{_stable_suffix(item.id, routing_revision)}",
                native_store_routing=routing_revision,
            )
            for item in items
        ]
    return items


def _blocked_item(
    backend: str, identity: str, reason: str = "config", *,
    source_paths: tuple[str, ...] = (),
    shell_variables: tuple[str, ...] = (),
    shell_auth_variables: tuple[str, ...] = (),
    config_blocker: bool = False,
) -> NativeMigrationItem:
    item_id, source_id = _ids(backend, "api_key", identity, "reauth")
    return NativeMigrationItem(
        id=item_id, source_id=source_id, backend=cast(Any, backend),
        kind="api_key", masked_detail="", proposed_action="reauth",
        selected=False, notes_key=f"settings.models.migration.blocked.{reason}",
        vendor={"claude": "anthropic", "codex": "openai", "opencode": "opencode"}[backend],
        protocol="anthropic" if backend == "claude" else "openai_responses",
        display_name={"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}[backend],
        source_paths=source_paths, shell_variables=shell_variables,
        shell_auth_variables=shell_auth_variables, config_blocker=config_blocker,
    )


def _native_source_paths(persisted: PersistedInventory, backend: str) -> tuple[str, ...]:
    paths = persisted.credential_paths[backend]
    return tuple(dict.fromkeys([
        *(str(guard.path) for guard in persisted.guards if guard.path in paths and guard.before is not None),
        *(str(path) for path in paths if path in persisted.problems),
    ]))


def _claude_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
    allow_secret: bool = False,
    project_roots: tuple[Path, ...] = (),
    persisted: PersistedInventory,
) -> list[NativeMigrationItem]:
    items: list[NativeMigrationItem] = []
    for path in claude_settings_paths(home, project_roots):
        if path in persisted.problems:
            items.append(_blocked_item(
                "claude", str(path), persisted.problems[path], source_paths=(str(path),),
                config_blocker=True,
            ))
            continue
        config = persisted.documents[path]
        if config is None:
            continue
        env = config.get("env", {})
        if not isinstance(env, dict):
            items.append(_blocked_item(
                "claude", str(path), source_paths=(str(path),), config_blocker=True,
            ))
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
                source_paths=(str(path),), native_field="ANTHROPIC_API_KEY",
            ))
        if config.get("apiKeyHelper"):
            items.append(_blocked_item(
                "claude", f"{path}:helper", "helper", source_paths=(str(path),),
            ))
        token = _oauth_text(env, "ANTHROPIC_AUTH_TOKEN")
        if token:
            try:
                scheme = validate_api_key_auth_scheme("anthropic", "anthropic", base_url, token, "bearer")
            except ValueError:
                items.append(_blocked_item(
                    "claude", f"{path}:ANTHROPIC_AUTH_TOKEN", "token", source_paths=(str(path),),
                ))
            else:
                item_id, source_id = _ids(
                    "claude", "api_key", f"{path}:ANTHROPIC_AUTH_TOKEN", "import",
                    _stable_suffix(token, base_url or "", scheme or ""),
                )
                masked = mask_credential(token)
                items.append(NativeMigrationItem(
                    id=item_id, source_id=source_id, backend="claude", kind="api_key",
                    masked_detail=masked, proposed_action="import", selected=True,
                    notes_key=_CUSTOM_ENDPOINT_NOTE, vendor="anthropic", protocol="anthropic",
                    display_name="Anthropic", base_url=base_url, secret=token,
                    masked_credential=masked, source_paths=(str(path),), auth_scheme=scheme,
                    native_field="ANTHROPIC_AUTH_TOKEN",
                ))
        if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            items.append(_blocked_item(
                "claude", f"{path}:CLAUDE_CODE_OAUTH_TOKEN", "token", source_paths=(str(path),),
            ))

    items.extend(_native_store_items(
        "claude", read_native_oauth("claude", home=home, allow_secret=allow_secret),
        mask_credential=mask_credential,
        source_paths=_native_source_paths(persisted, "claude"),
    ))
    return items


def _codex_items(
    *,
    home: Optional[Path],
    mask_credential: Callable[[str], str],
    allow_secret: bool = False,
    project_roots: tuple[Path, ...] = (),
    persisted: PersistedInventory,
) -> list[NativeMigrationItem]:
    items = _native_store_items(
        "codex", read_native_oauth("codex", home=home, allow_secret=allow_secret),
        mask_credential=mask_credential, state=read_codex_auth_state(home),
        source_paths=_native_source_paths(persisted, "codex"),
    )
    for path in codex_config_paths(home, project_roots):
        if path in persisted.problems:
            items.append(_blocked_item(
                "codex", str(path), persisted.problems[path], source_paths=(str(path),),
                config_blocker=True,
            ))
            continue
        config = persisted.documents[path]
        if config is None:
            continue
        providers = config.get("model_providers", {})
        # Only the provider map migration reads and rewrites is checked here.
        # The rest of the file belongs to the installed CLI, whose accepted
        # shapes differ by version, and fails in direct mode alike.
        if not isinstance(providers, dict):
            items.append(_blocked_item(
                "codex", str(path), source_paths=(str(path),), config_blocker=True,
            ))
            continue
        for provider_id, provider in providers.items():
            if not _codex_provider_well_typed(provider):
                # Codex deserializes the whole provider map before any Hub
                # override applies, so one malformed entry fails every launch.
                items.append(_blocked_item(
                    "codex", f"{path}:{provider_id}", source_paths=(str(path),),
                    config_blocker=True,
                ))
                continue
            key = _oauth_text(provider, "experimental_bearer_token")
            env_key = _oauth_text(provider, "env_key")
            headers = provider.get("http_headers", {})
            env_headers = provider.get("env_http_headers", {})
            names = tuple(dict.fromkeys([
                *([env_key] if env_key else []),
                *((value for value in env_headers.values() if isinstance(value, str))
                  if isinstance(env_headers, dict) else ()),
            ]))
            paths = tuple(dict.fromkeys([
                str(path), *(source for name in names for source in persisted.resolve(name).paths),
            ]))
            if env_headers or headers:
                # Header templates and shell-owned keys cannot be silently
                # reinterpreted as an ordinary Authorization bearer grant.
                items.append(_blocked_item(
                    "codex", f"{path}:{provider_id}", "headers",
                    source_paths=paths, shell_variables=names, shell_auth_variables=names,
                ))
                continue
            shell_values: tuple[tuple[str, str], ...] = ()
            if env_key:
                resolved = persisted.resolve(env_key)
                saved_key = resolved.value.strip() if resolved.value is not None else None
                if resolved.reason or not saved_key:
                    items.append(_blocked_item(
                        "codex", f"{path}:{provider_id}", resolved.reason or "reference",
                        source_paths=paths, shell_variables=names, shell_auth_variables=names,
                    ))
                    continue
                if key and key != saved_key:
                    # Two credential mechanisms must not silently discard one.
                    items.append(_blocked_item(
                        "codex", f"{path}:{provider_id}", "credential",
                        source_paths=paths, shell_variables=names, shell_auth_variables=names,
                    ))
                    continue
                key = saved_key
                shell_values = ((env_key, cast(str, resolved.value)),)
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
                source_paths=paths, shell_variables=names, shell_values=shell_values,
                shell_auth_variables=names,
            ))
    return items


def _load_opencode_provider_catalog(persisted: PersistedInventory) -> dict[str, dict[str, Any]]:
    payload = persisted.documents.get(persisted.catalog_path) or {}
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


# Every field of Codex's `ModelProviderInfo` (codex-rs/core/config.schema.json),
# by declared type. Codex ignores unknown keys but rejects a known one of the
# wrong type, so the whole table is checked rather than fields one at a time.
_CODEX_PROVIDER_FIELDS: dict[str, str] = {
    **dict.fromkeys((
        "name", "base_url", "env_key", "env_key_instructions",
        "experimental_bearer_token", "model_catalog_url",
    ), "text"),
    **dict.fromkeys(("http_headers", "env_http_headers", "query_params"), "text_map"),
    **{field: field for field in ("auth", "aws", "gateway_oauth")},
    **dict.fromkeys((
        "request_max_retries", "stream_max_retries", "stream_idle_timeout_ms",
        "websocket_connect_timeout_ms",
    ), "count"),
    **dict.fromkeys((
        "requires_openai_auth", "supports_websockets", "supports_standalone_web_search",
    ), "flag"),
    "wire_api": "wire_api",
}


# The nested tables Codex deserializes (required keys present, known ones of
# their declared type), from the same schema. Unknown keys are ignored: only
# `--strict-config`, which launches never pass, rejects them. A value is a kind name, a
# nested spec, or a tuple of alternative specs (a tagged enum).
_CodexSpec = dict[str, object]
_COMMAND_SPEC: _CodexSpec = {"command": "text", "args": "texts", "timeout_ms": "positive_count"}
_CODEX_NESTED_SPECS: dict[str, tuple[_CodexSpec, frozenset[str]]] = {
    "auth": ({
        "command": "text", "args": "texts", "cwd": "text",
        "refresh_interval_ms": "count", "timeout_ms": "positive_count",
    }, frozenset({"command"})),
    "aws": ({
        "profile": "text", "region": "text",
        "auth_refresh": (_COMMAND_SPEC, frozenset({"command"})),
        "credential_export": (_COMMAND_SPEC, frozenset({"command"})),
    }, frozenset()),
    "gateway_oauth": ({
        "authorization_url": "text", "client_id": "text", "token_url": "text",
        "resource": "text", "scopes": "texts", "redirect_port": "port",
        "delivery": (
            ({"kind": ("header",), "name": "text", "scheme": "text"}, frozenset({"kind", "name"})),
            ({"kind": ("cookie",), "name": "text"}, frozenset({"kind", "name"})),
        ),
    }, frozenset({"authorization_url", "client_id", "delivery", "token_url"})),
}


def _text_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _codex_field_well_typed(kind: str, value: object) -> bool:
    if kind == "text":
        return isinstance(value, str)
    if kind == "text_map":
        return _text_map(value)
    if kind == "count":
        # TOML integers are signed 64-bit; Codex rejects anything wider.
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63
    if kind == "flag":
        return isinstance(value, bool)
    if kind in _CODEX_NESTED_SPECS:
        return _codex_table_well_typed(_CODEX_NESTED_SPECS[kind], value)
    # An unknown variant fails deserialization. `chat` stays accepted: older
    # CLIs still run it, and migration carries it as openai_chat.
    return value in ("chat", "responses")


def _codex_table_well_typed(spec: object, value: object) -> bool:
    if isinstance(spec, str):
        if spec == "texts":
            return isinstance(value, list) and all(isinstance(item, str) for item in value)
        if spec in ("positive_count", "port"):
            limit = 2**16 if spec == "port" else 2**63
            minimum = 1 if spec == "positive_count" else 0
            return isinstance(value, int) and not isinstance(value, bool) and minimum <= value < limit
        return _codex_field_well_typed(spec, value)
    if isinstance(spec, tuple) and spec and isinstance(spec[0], tuple):
        # A tagged enum: exactly one variant has to accept the table.
        return any(_codex_table_well_typed(variant, value) for variant in spec)
    if isinstance(spec, tuple) and len(spec) == 2 and isinstance(spec[0], dict):
        fields, required = spec
        return (
            isinstance(value, dict)
            and required <= value.keys()
            and all(_codex_table_well_typed(fields[key], item) for key, item in value.items() if key in fields)
        )
    # A literal-choice tuple, such as a variant tag.
    return value in spec


def _codex_provider_well_typed(provider: object) -> bool:
    """Whether Codex can deserialize this ``model_providers`` entry.

    A field of the wrong type fails the whole config before any Hub override
    applies, so only an absent field or one of its declared type is safe.
    """
    return isinstance(provider, dict) and all(
        _codex_field_well_typed(kind, provider[field])
        for field, kind in _CODEX_PROVIDER_FIELDS.items() if field in provider
    )


_OPENCODE_COST = ({
    "input": "finite", "output": "finite", "cache_read": "finite", "cache_write": "finite",
}, frozenset({"input", "output"}))
_OPENCODE_MODALITIES = ("modality", "text", "audio", "image", "video", "pdf")
_OPENCODE_MODEL = ({
    "id": "text", "name": "text", "family": "text", "release_date": "text",
    "attachment": "flag", "reasoning": "flag", "temperature": "flag", "tool_call": "flag",
    "experimental": "flag",
    "interleaved": ("any", "flag", "text", ({"field": "text"}, frozenset({"field"}))),
    "cost": ({**_OPENCODE_COST[0], "context_over_200k": _OPENCODE_COST}, _OPENCODE_COST[1]),
    "limit": ({"context": "finite", "input": "finite", "output": "finite"}, frozenset({"context", "output"})),
    "modalities": ({"input": _OPENCODE_MODALITIES, "output": _OPENCODE_MODALITIES}, frozenset()),
    "status": ("choice", "alpha", "beta", "deprecated", "active"),
    "provider": ({"npm": "text", "api": "text"}, frozenset()),
    "options": "record",
    "headers": "text_map",
    "variants": ("values", ({"disabled": "flag"}, frozenset())),
}, frozenset())
_OPENCODE_TIMEOUT = ("any", "positive_count", ("choice", False))
_OPENCODE_PROVIDER = ({
    "api": "text", "name": "text", "id": "text", "npm": "text",
    "env": "texts", "whitelist": "texts", "blacklist": "texts",
    "options": ({
        "apiKey": "text", "baseURL": "text", "enterpriseUrl": "text", "setCacheKey": "flag",
        "timeout": _OPENCODE_TIMEOUT, "headerTimeout": _OPENCODE_TIMEOUT,
        "chunkTimeout": _OPENCODE_TIMEOUT,
    }, frozenset()),
    "models": ("values", _OPENCODE_MODEL),
}, frozenset())


def _opencode_value_well_typed(spec: object, value: object) -> bool:
    """Check ``value`` against OpenCode's ``ProviderConfig`` schema.

    Effect structs drop unknown keys, so only declared fields are checked.
    """
    if spec == "text":
        return isinstance(value, str)
    if spec == "flag":
        return isinstance(value, bool)
    if spec == "texts":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if spec == "text_map":
        return _text_map(value)
    if spec == "record":
        return isinstance(value, dict)
    if spec == "finite":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if spec == "positive_count":
        # JSON has one number type; `1.0` is the integer 1 to OpenCode.
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value == int(value) and value > 0
        )
    kind = spec[0] if isinstance(spec, tuple) else None
    if kind == "any":
        return any(_opencode_value_well_typed(variant, value) for variant in spec[1:])
    if kind == "choice":
        return any(value is choice or (type(value) is type(choice) and value == choice) for choice in spec[1:])
    if kind == "modality":
        return isinstance(value, list) and all(item in spec[1:] for item in value)
    if kind == "values":
        return isinstance(value, dict) and all(_opencode_value_well_typed(spec[1], item) for item in value.values())
    fields, required = cast(tuple[dict[str, object], frozenset[str]], spec)
    return (
        isinstance(value, dict)
        and required <= value.keys()
        and all(_opencode_value_well_typed(fields[key], item) for key, item in value.items() if key in fields)
    )


def _opencode_provider_well_typed(provider: object) -> bool:
    """Whether OpenCode's config schema accepts this ``provider`` entry.

    OpenCode validates the whole file on start, so a malformed typed field
    fails every launch, Hub-owned or not.
    """
    return _opencode_value_well_typed(_OPENCODE_PROVIDER, provider)


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
    persisted: PersistedInventory,
) -> list[NativeMigrationItem]:
    items: list[NativeMigrationItem] = []
    try:
        auth_entries = read_native_config(opencode_auth_path(home)) or {}
    except (TakeoverStateError, OSError):
        # Hub mode cannot prove what an unreadable auth map would shadow, so
        # it blocks; config layers still surface their own rows beside it.
        auth_entries = {}
        items.append(_blocked_item(
            "opencode", "auth-file", "unreadable",
            source_paths=(str(opencode_auth_path(home)),), config_blocker=True,
        ))
    provider_catalog = _load_opencode_provider_catalog(persisted)
    seen_providers: set[str] = set()
    for path in opencode_config_paths(home, project_roots):
        if path in persisted.problems:
            items.append(_blocked_item(
                "opencode", str(path), persisted.problems[path], source_paths=(str(path),),
                config_blocker=True,
            ))
            continue
        config = persisted.documents[path]
        if config is None:
            continue
        provider_configs = config.get("provider", {})
        if not isinstance(provider_configs, dict):
            items.append(_blocked_item(
                "opencode", str(path), source_paths=(str(path),), config_blocker=True,
            ))
            continue
        seen_providers.update(provider_configs)
        relevant_auth = {key: value for key, value in auth_entries.items() if key in provider_configs}
        items.extend(_opencode_candidates(
            provider_configs, relevant_auth, provider_catalog, str(path), mask_credential,
            persisted=persisted, auth_path=opencode_auth_path(home),
        ))
    unseen_auth = {key: value for key, value in auth_entries.items() if key not in seen_providers}
    items.extend(_opencode_candidates(
        {}, unseen_auth, provider_catalog, str(opencode_auth_path(home)), mask_credential,
        persisted=persisted, auth_path=opencode_auth_path(home),
    ))
    return items


def _opencode_candidates(
    provider_configs: dict, auth_entries: dict, provider_catalog: dict,
    locator: str, mask_credential: Callable[[str], str],
    *, persisted: PersistedInventory, auth_path: Path,
) -> list[NativeMigrationItem]:
    provider_ids = set(provider_configs) | set(auth_entries)
    items: list[NativeMigrationItem] = []
    for provider_id in sorted(provider_ids):
        paths = (locator,)
        names: tuple[str, ...] = ()
        auth_names: tuple[str, ...] = ()

        def blocked(
            identity: str, reason: str = "config", *, config_blocker: bool = False,
        ) -> NativeMigrationItem:
            return _blocked_item(
                "opencode", identity, reason, source_paths=paths, shell_variables=names,
                shell_auth_variables=auth_names, config_blocker=config_blocker,
            )

        if (
            not isinstance(provider_id, str) or not provider_id
            or len(provider_id) > 64
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for char in provider_id)
            or not provider_id[0].isalnum()
            or contains_credential_material(provider_id)
        ):
            items.append(blocked(f"{locator}:invalid-provider"))
            continue
        provider_config = provider_configs.get(provider_id, {})
        if not _opencode_provider_well_typed(provider_config):
            # OpenCode validates the whole provider map before any Hub
            # override applies, so one malformed entry fails every launch.
            items.append(blocked(f"{locator}:{provider_id}", config_blocker=True))
            continue
        options = provider_config.get("options")
        if not isinstance(options, dict):
            options = {}
        key_setting = options.get("apiKey")
        names = env_references(options)
        auth_names = tuple(dict.fromkeys([
            *env_references(key_setting), *env_references(options.get("headers")),
        ]))
        paths = tuple(dict.fromkeys([
            locator, *(source for name in names for source in persisted.resolve(name).paths),
            *([str(auth_path)] if provider_id in auth_entries else []),
        ]))
        if options.get("headers"):
            items.append(blocked(f"{locator}:{provider_id}:headers", "headers"))
            continue
        shell_values: list[tuple[str, str]] = []
        config_key = _opencode_plaintext_key(key_setting)
        auth_entry = auth_entries.get(provider_id, {})
        if not isinstance(auth_entry, dict):
            items.append(blocked(f"{locator}:{provider_id}"))
            continue
        auth_key = (
            _opencode_plaintext_key(auth_entry.get("key"))
            if auth_entry.get("type") == "api"
            else None
        )
        key_name = env_reference(key_setting)
        if key_name:
            resolved = persisted.resolve(key_name)
            config_key = (resolved.value.strip() or None) if resolved.value is not None else None
            if resolved.reason or (not config_key and not auth_key):
                items.append(blocked(
                    f"{locator}:{provider_id}:key-reference", resolved.reason or "reference",
                ))
                continue
            if config_key:
                shell_values.append((key_name, cast(str, resolved.value)))
        elif isinstance(key_setting, str) and ("{env:" in key_setting or "{file:" in key_setting):
            items.append(blocked(f"{locator}:{provider_id}:key-reference", "reference"))
            continue
        secret = config_key or auth_key
        if secret is None:
            if options.get("apiKey") or auth_entry:
                items.append(blocked(f"{locator}:{provider_id}", "credential"))
            continue
        raw_base_url = options.get("baseURL")
        base_url = raw_base_url.strip() if isinstance(raw_base_url, str) and raw_base_url.strip() else None
        base_name = env_reference(raw_base_url)
        if base_name:
            resolved = persisted.resolve(base_name)
            if resolved.reason or not resolved.value:
                items.append(blocked(
                    f"{locator}:{provider_id}:base-reference", resolved.reason or "reference",
                ))
                continue
            base_url = resolved.value
            shell_values.append((base_name, base_url))
        elif base_url and ("{env:" in base_url or "{file:" in base_url):
            items.append(blocked(f"{locator}:{provider_id}:base-reference", "reference"))
            continue
        catalog_provider = provider_catalog.get(provider_id, {})
        if base_url is None:
            catalog_api = catalog_provider.get("api")
            if isinstance(catalog_api, str) and catalog_api.strip():
                base_url = catalog_api.strip()
                paths = tuple(dict.fromkeys([*paths, str(persisted.catalog_path)]))
        protocol = _opencode_protocol(provider_id, provider_config, catalog_provider)
        if protocol != _opencode_protocol(provider_id, provider_config, {}):
            paths = tuple(dict.fromkeys([*paths, str(persisted.catalog_path)]))
        if (
            protocol is None or provider_id in _OPENCODE_UNSUPPORTED_NATIVE_IDS
            or (base_url is None and provider_id not in {"anthropic", "openai"})
            or (auth_entry and auth_entry.get("type") != "api")
        ):
            items.append(blocked(f"{locator}:{provider_id}", "credential"))
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
                source_paths=paths, shell_variables=names, shell_values=tuple(shell_values),
                shell_auth_variables=auth_names,
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


def _shell_items(
    persisted: PersistedInventory, mask_credential: Callable[[str], str],
    native_items: list[NativeMigrationItem],
) -> list[NativeMigrationItem]:
    """Import literal credentials only with a single, persisted target."""
    items: list[NativeMigrationItem] = []
    specs = (
        ("claude", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "anthropic", "anthropic"),
        ("claude", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "anthropic", "anthropic"),
        ("claude", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "anthropic", "anthropic"),
        ("codex", "OPENAI_API_KEY", "OPENAI_BASE_URL", "openai", "openai_responses"),
        ("codex", "CODEX_API_KEY", "OPENAI_BASE_URL", "openai", "openai_responses"),
        ("opencode", "OPENROUTER_API_KEY", None, "openrouter", "openai_chat"),
    )
    for backend, name, base_name, vendor, protocol in specs:
        if any(item.backend == backend and name in item.shell_auth_variables for item in native_items):
            # A native provider reference owns the target and transport.
            continue
        value = persisted.resolve(name)
        # Use the same canonical API-key bytes as proof, reuse and custody.
        # The original assignment remains separate for exact native cleanup.
        secret = value.value.strip() if value.value is not None else None
        if not value.paths or (not secret and not value.reason):
            continue
        base = persisted.resolve(base_name) if base_name else None
        names = (name, *((base_name,) if base and base.paths else ()))
        paths = tuple(dict.fromkeys([*value.paths, *(base.paths if base else ())]))
        reason = value.reason or (base.reason if base else None)
        base_url = base.value if base else None
        if base_url and any(
            profile.values.get(name) and profile.values.get(base_name) != base_url
            for profile in persisted.profiles
        ):
            # A login shell and an interactive shell need not read the same
            # profiles. A key cannot inherit a guessed target from another one.
            reason = reason or "ambiguous_shell"
        scheme = None
        if name == "CLAUDE_CODE_OAUTH_TOKEN":
            reason = reason or "token"
        elif name == "ANTHROPIC_AUTH_TOKEN" and secret and not reason:
            try:
                scheme = validate_api_key_auth_scheme(
                    vendor, protocol, base_url, secret, "bearer",
                )
            except ValueError:
                reason = "token"
        if reason or not secret:
            items.append(_blocked_item(
                backend, f"shell:{name}", reason or "reference",
                source_paths=paths, shell_variables=names, shell_auth_variables=(name,),
            ))
            continue
        if vendor == "openrouter":
            base_url = "https://openrouter.ai/api/v1"
        kind = "opencode_provider" if backend == "opencode" else "api_key"
        item_id, source_id = _ids(
            backend, kind, f"shell:{name}", "import",
            _stable_suffix(secret, base_url or "", scheme or ""),
        )
        masked = mask_credential(secret)
        items.append(NativeMigrationItem(
            id=item_id, source_id=source_id, backend=cast(Any, backend),
            kind=cast(Any, kind), masked_detail=masked, proposed_action="import",
            selected=True, notes_key=_CUSTOM_ENDPOINT_NOTE if base_url else None,
            vendor=vendor, protocol=cast(Any, protocol), display_name=vendor,
            base_url=base_url, secret=secret, masked_credential=masked,
            source_paths=paths, shell_variables=names,
            shell_auth_variables=(name,),
            shell_values=(
                (name, cast(str, value.value)),
                *(((base_name, base_url),) if base and base.value else ()),
            ),
            auth_scheme=scheme,
        ))
    return items


def _bind_persisted_inventory(
    items: list[NativeMigrationItem], persisted: PersistedInventory,
) -> list[NativeMigrationItem]:
    """Consent includes all consumers of any shell assignment being removed."""
    dependencies: dict[str, set[tuple[str, str]]] = {}
    for item in items:
        dependencies.setdefault(item.backend, set()).update(
            (path, name) for name in item.shell_variables
            for path in persisted.resolve(name).paths
        )
    for backend in dependencies:
        # Unsupported providers still consume their saved references. A
        # selectable peer cannot erase that source by omitting a blocked row.
        dependencies[backend].update(
            (path, name) for name in persisted.references[backend]
            for path in persisted.resolve(name).paths
        )
    linked = {backend: {backend} for backend in dependencies}
    for backend, values in dependencies.items():
        for other, other_values in dependencies.items():
            if values & other_values:
                linked[backend].add(other)
    for _ in linked:
        for backend in linked:
            linked[backend].update(
                member for other in tuple(linked[backend]) for member in linked[other]
            )
    config_paths = set().union(*persisted.backend_paths.values())
    unknown_consumers = [
        row for row in items
        if row.notes_key in {
            "settings.models.migration.blocked.config",
            "settings.models.migration.blocked.unreadable",
        }
        and any(Path(path) in config_paths for path in row.source_paths)
    ]
    result = []
    for item in items:
        closure = tuple(sorted(linked[item.backend]))
        shared = any(dependencies[backend] for backend in closure)
        if shared and unknown_consumers and item.proposed_action == "import":
            item = replace(
                item, proposed_action="reauth", selected=False,
                notes_key=unknown_consumers[0].notes_key,
                source_paths=tuple(dict.fromkeys([
                    *item.source_paths,
                    *(path for row in unknown_consumers for path in row.source_paths),
                ])),
            )
        guards = persisted.snapshots(item.backend, shared=shared)
        revision = persisted.revision(guards)
        dependency_revision = _stable_suffix(
            *closure,
            *(f"{backend}\0{path}\0{name}" for backend in closure
              for path, name in sorted(dependencies[backend])),
        )
        result.append(replace(
            item,
            id=f"mig_{_stable_suffix(item.id, revision, dependency_revision, item.auth_scheme or '')}",
            required_backends=closure if len(closure) > 1 else (),
            file_snapshots=guards,
        ))
    # A read concurrent with discovery must never bind a different before image.
    for guard in dict.fromkeys(guard for item in result for guard in item.file_snapshots):
        guard.check()
    return result


def _deduplicate_opencode_items(
    items: list[NativeMigrationItem],
    validate_base_url: Callable[[object], Optional[str]] | None,
) -> list[NativeMigrationItem]:
    """One logical credential/target, with consent covering every native copy."""
    groups: dict[tuple[str, str, str | None, str, str | None], list[NativeMigrationItem]] = {}
    for item in items:
        if item.kind == "opencode_provider" and item.proposed_action == "import" and item.secret:
            target = validate_base_url(item.base_url) if validate_base_url else item.base_url
            groups.setdefault((item.vendor, item.protocol, target, item.secret, item.auth_scheme), []).append(item)
    replacements: dict[str, NativeMigrationItem] = {}
    duplicates: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        first = group[0]
        models: dict[str, NativeManualModel] = {}
        for item in group:
            for model in item.manual_models:
                # Keep the complete model union; the later configuration layer
                # supplies a label when the same model is named in both.
                models[model.id] = model
        # Layer order also owns repeated model labels, so it is consent-bound.
        revision = _stable_suffix(*(item.id for item in group))
        replacements[first.id] = replace(
            first, id=f"mig_{revision}", source_id=f"src_{revision}",
            manual_models=tuple(models.values()),
            source_paths=tuple(dict.fromkeys(path for item in group for path in item.source_paths)),
            shell_variables=tuple(dict.fromkeys(name for item in group for name in item.shell_variables)),
            shell_auth_variables=tuple(dict.fromkeys(
                name for item in group for name in item.shell_auth_variables
            )),
            shell_values=tuple(dict.fromkeys(pair for item in group for pair in item.shell_values)),
        )
        duplicates.update(item.id for item in group[1:])
    return [replacements.get(item.id, item) for item in items if item.id not in duplicates]


def _require_native_api_key_transport(
    item: NativeMigrationItem, *, observed_protocol: str | None = None,
) -> None:
    if item.kind == "oauth_native":
        return
    protocol = observed_protocol if observed_protocol is not None else item.protocol
    try:
        validate_migration_api_key_transport(
            item.vendor, protocol, item.base_url, item.secret, item.auth_scheme,
        )
        if (protocol == "anthropic") != (item.protocol == "anthropic"):
            # None is legacy protocol-auth, not permission to replace a native
            # x-api-key with Bearer (or vice versa) after observation.
            raise ValueError
    except ValueError:
        raise MigrationConflictError from None


def _bearer_transport(item: NativeMigrationItem) -> NativeMigrationItem | None:
    """Carry a custom-endpoint Anthropic key over as the Bearer the engine sends.

    The pinned engine sends ``x-api-key`` only to the official origin. Custom
    endpoints get Bearer, so migration proves and provisions exactly that header;
    an endpoint that refuses it fails proof and the native files stay untouched.
    """
    if item.kind == "oauth_native" or item.protocol != "anthropic" or item.auth_scheme is not None:
        return None
    candidate = replace(item, auth_scheme="bearer")
    try:
        _require_native_api_key_transport(candidate)
    except MigrationConflictError:
        return None
    return candidate


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
    retained_native_ids: Mapping[str, Mapping[str, str]] | None = None,
) -> list[NativeMigrationItem]:
    """Read native stores without modifying or deleting any path."""

    persisted = PersistedInventory.read(home, project_roots)
    items = [
        *_claude_items(
            home=home,
            mask_credential=mask_credential,
            allow_secret="claude" in secret_backends,
            project_roots=project_roots,
            persisted=persisted,
        ),
        *_codex_items(
            home=home, mask_credential=mask_credential,
            allow_secret="codex" in secret_backends,
            project_roots=project_roots,
            persisted=persisted,
        ),
        *_opencode_items(
            home=home, mask_credential=mask_credential, project_roots=project_roots,
            persisted=persisted,
        ),
    ]
    items.extend(_shell_items(persisted, mask_credential, items))
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
    admitted: list[NativeMigrationItem] = []
    for item in items:
        if item.proposed_action == "import":
            try:
                _require_native_api_key_transport(item)
            except MigrationConflictError:
                item = _bearer_transport(item) or replace(
                    item, proposed_action="reauth", selected=False,
                    notes_key="settings.models.migration.blocked.transport",
                )
        admitted.append(item)
    items = admitted
    items = _deduplicate_opencode_items(items, validate_base_url)
    existing_native_sources = {
        source.vendor: source
        for source in config.sources
        if source.kind == "subscription" and source.supply_channel == "native_cli"
    }
    live_credentials = {source.id: source.credential_ref for source in config.sources}
    candidates: list[NativeMigrationItem] = []
    for item in items:
        # Keep the credential/target identity independent of both consent's
        # full file snapshots and an existing Source's custody placement.
        # Completed receipts can then recognize the exact old grant without
        # accepting old consent after a file changed.
        item = replace(item, receipt_identity=item.id)
        if (
            item.native_store_placeholder
            and (clean_native_stores or {}).get(item.backend) == item.native_store_revision
        ):
            # A completed cleanup verified this exact metadata revision holds
            # only unrelated native data (e.g. MCP OAuth). Do not read it again
            # merely to rediscover the absence of subscription credentials.
            continue
        if item.native_store_placeholder and any(
            copy.get("store_backend") == item.backend
            and copy.get("store_routing", "") == (item.native_store_routing or "")
            and copy.get("store_revision") == item.native_store_revision
            and _retained_copy_live(copy, live_credentials)
            for copy in (retained_native_ids or {}).values()
        ):
            # An opaque store whose only credential is a kept, copied key. Its
            # metadata revision was recorded at completion; any change re-offers.
            continue
        if (
            item.kind != "oauth_native" and item.secret
            and _retained_copy_live(
                (retained_native_ids or {}).get(_retained_key_identity(item)), live_credentials,
            )
        ):
            # A completed takeover copied this exact static key and left it
            # native by choice. It is shadowed by the Hub launch, not pending.
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
    return _bind_persisted_inventory(candidates, persisted)


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


def _ensure_takeover_placement(
    config: ModelHubConfig, source: ModelHubSourceConfig, backend: str,
) -> None:
    """Identity reuse still owes the consenting backend a configured placement."""
    if not config.source_eligible_for_backend(source, backend):
        raise MigrationConflictError
    agent = config.agents[backend]
    if source.id not in agent.sources.order and not any(
        hop.source_id == source.id for route in agent.routes.values() for hop in route.hops
    ):
        agent.sources.order.append(source.id)


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
    retained_item_ids: frozenset[str] = frozenset(),
    clean_native_stores: Mapping[str, str] | None = None,
    clean_api_keys: bool = False,
    retained_keys: tuple[NativeMigrationItem, ...] = (),
) -> dict[str, Any]:
    """Stage all grants and durable before/after images, still native-owned.

    ``retained_keys`` are keys an earlier copy-only batch already copied; a
    cleanup batch withdraws them natively without provisioning them again.
    """
    for item in selected:
        # Recheck before cleanup planning, reuse, proof or credential custody.
        _require_native_api_key_transport(item)
    clean_native_stores = dict(clean_native_stores or {})
    cleanup_items = [
        *selected,
        *retained_keys,
        *(item for item in (consented or []) if (
            item.native_store_placeholder and item.backend in clean_native_stores
        )),
    ]
    edits = plan_native_cleanup(
        cleanup_items, home=host.migration_home, project_roots=project_roots,
        clean_api_keys=clean_api_keys,
    )
    updated = host._clone_config(previous)
    provisioned: list[dict[str, str]] = []
    source_ids: list[str] = list(retained_source_ids or ())
    item_sources: dict[str, dict[str, str]] = {}
    catalog = bundled_catalog_reasoning_efforts_by_model()
    handoff_attempted = False
    try:
        # A completed receipt can authorize cleanup of the exact old material
        # reintroduced by an external writer. Its current Hub refs are already
        # authoritative; never provision that old OAuth snapshot again.
        for item in selected:
            if item.id in retained_item_ids:
                continue
            protocol = item.protocol
            auth_options = {"auth_scheme": item.auth_scheme} if item.auth_scheme is not None else {}
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
                            **auth_options,
                        ))
                    ):
                        # Identity reuse is not current authentication proof.
                        # Observe the exact existing target/ref without changing
                        # its inventory, state, or user-owned routes.
                        reuse_observation = await host._require_proven_observation(
                            candidate.vendor, candidate.base_url,
                            candidate.credential_ref, (candidate.protocol,),
                        )
                        _require_native_api_key_transport(item, observed_protocol=reuse_observation.protocol)
                        _ensure_takeover_placement(updated, candidate, item.backend)
                        source_ids.append(candidate.id)
                        item_sources[_retained_key_identity(item)] = {
                            "source_id": candidate.id, "credential_ref": candidate.credential_ref,
                            "key_fingerprint": _retained_key_fingerprint(item),
                            **({
                        "store_backend": item.backend, "store_routing": item.native_store_routing or "",
                    } if item.native_store_revision else {}),
                        }
                        break
                else:
                    observation = await host._require_proven_source_payload({
                        "vendor": item.vendor,
                        "base_url": validate_base_url(item.base_url),
                        "key": item.secret,
                        # Bearer exists only on the Anthropic interface.
                        **({"protocol": "anthropic"} if item.auth_scheme == "bearer" else {}),
                    }, on_reserved=lambda ref: host.revocations.add("observation", ref), **auth_options)
                    protocol = cast(Any, observation.protocol)
                    _require_native_api_key_transport(item, observed_protocol=protocol)
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
                    on_reserved=reserved, **auth_options,
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
                _ensure_takeover_placement(updated, source, item.backend)
            else:
                updated.sources.append(source)
                host._apply_source_placement(updated, source)
            source_ids.append(source.id)
            if item.kind != "oauth_native":
                item_sources[_retained_key_identity(item)] = {
                    "source_id": source.id, "credential_ref": source.credential_ref,
                    "key_fingerprint": _retained_key_fingerprint(item),
                    **({
                        "store_backend": item.backend, "store_routing": item.native_store_routing or "",
                    } if item.native_store_revision else {}),
                }
        backends = sorted({item.backend for item in [*(consented or selected), *retained_keys]})
        native_before = _native_auth_snapshot(host, tuple(backends))
        # Copy-only keeps the Avibe-saved native key; Hub launches shadow it.
        # Cleanup clears the Avibe-saved key only when this batch carries it; an
        # excluded key (e.g. an unimportable endpoint) keeps its Direct config.
        carried = {
            (item.backend, item.secret, item.base_url)
            for item in [*selected, *retained_keys] if item.secret
        }
        credential_backends = {
            backend for backend in {item.backend for item in [*selected, *retained_keys]}
            if not native_before.get(backend, {}).get("api_key")
            or (
                backend, native_before[backend]["api_key"], native_before[backend].get("base_url"),
            ) in carried
        } if clean_api_keys else set()
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
            "inventory_ids": [item.receipt_identity or item.id for item in (consented or selected)],
            "backends": backends, "source_ids": source_ids,
            "credentials": provisioned,
            "previous": previous.to_payload(), "updated": updated.to_payload(),
            "files": [edit.to_payload() for edit in edits],
            "native_before": native_before, "native_after": native_after,
            "keychain": [],
            "clean_native_stores": clean_native_stores,
            # Each copied key stays hidden only while its Hub Source exists.
            "clean_api_keys": clean_api_keys,
            "retained_native_ids": {} if clean_api_keys else {
                identity: copy for identity, copy in item_sources.items()
            },
            # A cleanup batch withdraws these kept keys, retiring their receipts.
            # Identities and material fingerprints both retire: a receipt
            # written under an earlier route still names this withdrawn key.
            "withdrawn_native_ids": sorted({
                name for item in [*selected, *retained_keys]
                if clean_api_keys and item.kind != "oauth_native"
                for name in (_retained_key_identity(item), _retained_key_fingerprint(item))
            }),
        }
        seen_stores: set[str] = set()
        for item in native_store_items([*selected, *retained_keys], clean_api_keys=clean_api_keys):
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
        edit = NativeFileEdit.from_payload(raw)
        # A compare-only guard never owned any bytes. An external edit must
        # stop forward publication, but cannot prevent rollback of our writes.
        if edit.before != edit.after:
            edit.apply(reverse=True)
    for edit in reversed(record.get("keychain", [])):
        await asyncio.to_thread(apply_keychain_edit, edit, reverse=True)
    for credential in reversed(record["credentials"]):
        await host._rollback_credential(
            credential["source_id"],
            credential["credential_ref"],
        )
    host._reconcile_native_auth(tuple(record["backends"]))
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


async def _record_retained_store_revisions(host: MigrationHost, record: dict[str, Any]) -> None:
    """Bind each kept key held in an opaque store to that store's final revision."""
    for copy in record.get("retained_native_ids", {}).values():
        backend = copy.get("store_backend")
        if backend is None:
            continue
        # The store still holds this key, so it is not verified clean: it stays
        # hidden only while the copied Hub source is live.
        record.get("clean_native_stores", {}).pop(backend, None)
        snapshot = await asyncio.to_thread(read_native_oauth, backend, home=host.migration_home)
        verified = record.get("verified_store_revisions", {}).get(backend)
        # Bind only the revision verified against our post-edit image; a later
        # external write stays unbound, so its store is offered again.
        if (
            snapshot is not None and (snapshot.payload or {}).get("status") == "metadata_only"
            and (verified is None or snapshot.revision == verified)
        ):
            copy["store_revision"] = snapshot.revision


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
            host._reconcile_native_auth(tuple(record["backends"]))
            # This durable marker precedes any credential exposure to CPA.
            # An empty-container-only confirmation transferred no grant and
            # changed no native bytes: keep it reversible through runtime start
            # so a newly observed login can be presented for fresh consent.
            if (
                record["source_ids"] or record["credentials"]
                or any(raw["before"] != raw["after"] for raw in record["files"])
                or record.get("keychain")
                or record["native_before"] != record["native_after"]
            ):
                record["phase"] = "exposed"
            host.migration_journal.save(record)
        else:
            # Recovery may start with an equal persisted Hub config but stale
            # launch consumers. Reconcile before any possible publication.
            host._reconcile_native_auth(tuple(record["backends"]))
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
                if snapshot is not None:
                    record.setdefault("verified_store_revisions", {})[edit["backend"]] = snapshot.revision
                if snapshot and (snapshot.payload or {}).get("status") == "metadata_only":
                    record["clean_native_stores"][edit["backend"]] = snapshot.revision
        await verify_idle()
        if terminal:
            # Every terminal path binds kept keys before its receipt.
            await _record_retained_store_revisions(host, record)
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
        # Runtime start is another await boundary. A new saved login or
        # consumer introduced there must not disappear behind a receipt.
        for raw in record["files"]:
            NativeFileEdit.from_payload(raw).check(applied=True)
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
            await _record_retained_store_revisions(host, record)
            record["terminal"] = {
                "invalid_source_ids": invalid_source_ids,
                "config": terminal_config.to_payload(),
            }
            host.migration_journal.save(record)
            return _finish_rejected_takeover(host, record)
        host._reconcile_native_auth(tuple(record["backends"]))
        await _record_retained_store_revisions(host, record)
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
    host._reconcile_native_auth(tuple(record["backends"]))
    host.migration_journal.complete(record)
    host.migration_blocked_backends.difference_update(record["backends"])
    raise MigrationCredentialsInvalidError


async def apply_native_migration(
    host: MigrationHost,
    item_ids: object,
    *,
    mask_credential: Callable[[str], str],
    validate_base_url: Callable[[object], Optional[str]],
    clean_api_keys: bool = False,
) -> tuple[int, list[dict]]:
    """Own a takeover from consent through cleanup; callers shield cancellation.

    Static API keys are copied and left native unless ``clean_api_keys``.
    """
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
        # A copy-only batch whose Sources were deleted or re-keyed no longer
        # holds its still-native keys; the same rows then migrate afresh. A
        # cleanup receipt keeps guarding the material it withdrew.
        replayable = record is not None and (
            record["phase"] != "complete" or record.get("clean_api_keys", True)
            or _receipt_sources_intact(host, record)
        )
        if record is not None:
            same_selection = {item["id"] for item in record["items"]} == set(item_ids)
            if record["phase"] != "complete" and not same_selection:
                raise MigrationConflictError
            # Receipts predating the option always cleaned native keys. A
            # pending journal from before it already staged that decision and
            # resumes as staged.
            if replayable and same_selection and (
                "clean_api_keys" in record or record["phase"] == "complete"
            ) and record.get("clean_api_keys", True) != clean_api_keys:
                raise MigrationConflictError
            if same_selection and replayable:
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
                                retained_native_ids=record.get("retained_native_ids"),
                            )
                            if not any(
                                item.backend in record["backends"]
                                and (item.proposed_action == "import" or item.config_blocker)
                                for item in residual
                            ):
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
            retained_native_ids=(host.migration_journal.completed() or {}).get("retained_native_ids"),
        )
        selected = [item for item in available if item.id in item_ids]
        if len(selected) != len(item_ids) or any(item.proposed_action != "import" for item in selected):
            raise MigrationConflictError
        backends = tuple(sorted({item.backend for item in selected}))
        if any(set(item.required_backends) - set(backends) for item in selected):
            raise MigrationConflictError
        # A takeover moves every credential the Hub can carry, so selection is
        # grouped by backend. Rows it cannot carry stay native: a Hub launch
        # pins its own connection above them, so they are shadowed, not used.
        if any(
            item.backend in backends and item.proposed_action == "import" and item.id not in item_ids
            for item in available
        ):
            raise MigrationConflictError
        # Hub mode over a native config the CLI cannot parse fails every launch.
        if any(item.backend in backends and item.config_blocker for item in available):
            raise MigrationConflictError
        if clean_api_keys:
            # Cleanup also withdraws keys a copy-only batch kept. Such a key
            # may be shared with another backend (e.g. one shell variable);
            # joint cleanup then spans that backend's kept keys as well.
            hidden = [
                item for item in await asyncio.to_thread(
                    scan_native_configs, host.store.load(), mask_credential=mask_credential,
                    home=host.migration_home, validate_base_url=validate_base_url,
                    legacy_auth=_native_auth_snapshot(host, ("claude", "codex", "opencode")),
                    project_roots=host.migration_project_roots(),
                    clean_native_stores=(host.migration_journal.completed() or {}).get("clean_native_stores"),
                )
                if item.kind != "oauth_native" and item.proposed_action == "import"
                and item.id not in {row.id for row in available}
            ]
            scope = set(backends)
            while True:
                grown = scope | {
                    backend for item in hidden if item.backend in scope for backend in item.required_backends
                }
                if grown == scope:
                    break
                scope = grown
            if any(
                item.backend in scope - set(backends) and item.proposed_action == "import"
                for item in available
            ):
                # A linked backend still has credentials awaiting consent.
                raise MigrationConflictError
            backends = tuple(sorted(scope))
        retained_inventory_ids = (
            set(record.get("inventory_ids", [item["id"] for item in record["items"]]))
            & {item.receipt_identity or item.id for item in selected}
            if replayable and record["phase"] == "complete" else set()
        )
        previous_oauth_backends = (
            host.migration_journal.oauth_custody_backends(record)
            if record is not None and record["phase"] == "complete" else frozenset()
        )
        if retained_inventory_ids:
            # Fresh file-bound consent may describe the same old grant that
            # an external writer restored after cleanup. Keep the current Hub
            # credential rather than provisioning/refreshing that old grant.
            # Before file-bound IDs existed, receipt item IDs already were
            # these opaque inventory IDs; preserve that exact legacy proof.
            # Consent can add or omit other backends. Grant ownership is not
            # conditional on the entire selection being repeated.
            completed_record = record
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
                    retained_native_ids=(host.migration_journal.completed() or {}).get("retained_native_ids"),
                )
                consented = selected
                selected = []
                retained_item_ids: set[str] = set()
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
                    if (
                        original.backend in previous_oauth_backends
                        and (original.receipt_identity or original.id) not in retained_inventory_ids
                        and any(item.kind == "oauth_native" for item in resolved)
                    ):
                        # Snapshot/revision changes cannot distinguish new
                        # authorization from an old refresh token copied into
                        # an edited container. Only Hub reauthentication can
                        # establish a fresh grant; never reprovision by guess.
                        raise MigrationReauthorizationRequiredError
                    selected.extend(item for item in resolved if item not in selected)
                    if (original.receipt_identity or original.id) in retained_inventory_ids:
                        retained_item_ids.update(item.id for item in resolved)
                if any(
                    item.backend in backends and (
                        (item.proposed_action == "import" and item not in selected)
                        or item.config_blocker
                    )
                    for item in rescanned
                ):
                    raise MigrationConflictError
                retained_keys: tuple[NativeMigrationItem, ...] = ()
                if clean_api_keys:
                    # Keys a copy-only batch kept are hidden from consent while
                    # their Hub copy lives, yet cleanup must withdraw them too
                    # or they read as unselected native credentials.
                    shown = {item.id for item in rescanned}
                    unfiltered = await asyncio.to_thread(
                        scan_native_configs, previous, mask_credential=mask_credential,
                        home=host.migration_home, validate_base_url=validate_base_url,
                        legacy_auth=_native_auth_snapshot(host, backends),
                        secret_backends=backends, project_roots=project_roots,
                        clean_native_stores=(host.migration_journal.completed() or {}).get("clean_native_stores"),
                    )
                    retained_keys = tuple(
                        item for item in unfiltered
                        if item.backend in backends and item.id not in shown
                        and item.kind != "oauth_native" and item.proposed_action == "import" and item.secret
                    )
                record = await _prepare_takeover(
                    host, previous, selected, mask_credential=mask_credential,
                    validate_base_url=validate_base_url,
                    consented=consented,
                    project_roots=project_roots,
                    retained_source_ids=(
                        tuple(completed_record["source_ids"])
                        if completed_record is not None else None
                    ),
                    retained_item_ids=frozenset(retained_item_ids),
                    clean_native_stores=clean_native_stores,
                    clean_api_keys=clean_api_keys,
                    retained_keys=retained_keys,
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
