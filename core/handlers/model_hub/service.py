"""Model Hub aggregate service used by REST routes and backend injection."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from config import paths
from config.v2_config import (
    CONFIG_LOCK,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMappingConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
)
from core.services.settings import default_config
from vibe.backend_model_catalog import backend_model_entries, load_bundled_catalog

from .adapter import (
    EngineAdapter,
    EngineHealth,
    EngineStatus,
    InvokeHandle,
    OAuthFlowState,
    OriginNotAllowedError,
    RawCallOutcome,
    SourceBinding,
)
from .classification import ResolutionDecision, classify_outcome
from .events import (
    BoundedEventLog,
    EventAgent,
    EventReason,
    build_resolution_event,
    contains_credential_material,
)
from .identifiers import opencode_model_id, opencode_provider_id, parse_opencode_model_id
from .oauth import (
    NativeOAuthUnavailableError,
    OAuthAdapter,
    OAuthChannel,
    OAuthFlowBinding,
    OAuthFlowRegistry,
    UnavailableNativeOAuthAdapter,
)
from .revocations import CredentialRevocationJournal

CONTRACT_VERSION = 1
logger = logging.getLogger(__name__)

_NATIVE_VENDOR_BACKENDS = {"anthropic": "claude", "openai": "codex"}
_CREDENTIAL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "credential",
    "key",
    "password",
    "passwd",
    "secret",
    "sig",
    "signature",
    "token",
}

_RUNTIME_MANIFEST = {
    "name": "cliproxyapi",
    "version": "v7.2.95",
    "source_sha": "f71ec0eb6776854457892452cf28c47f0d658251",
    "assets": [
        {
            "platform": "darwin-arm64",
            "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.95/CLIProxyAPI_7.2.95_darwin_aarch64.tar.gz",
            "size_bytes": 14384655,
            "sha256": "c7ccc28b7db5d1799999a9e22725ccc6bd0e36d9aa023da6b52b7c1a71aad978",
        },
        {
            "platform": "linux-amd64",
            "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.95/CLIProxyAPI_7.2.95_linux_amd64.tar.gz",
            "size_bytes": 15401775,
            "sha256": "826604e2dbf11913b0f373047f7bca1829eb2bab8a45d3a1916cc2534c7a9fd5",
        },
    ],
}


class ModelHubError(Exception):
    def __init__(self, code: str, *, status: int = 400):
        detail_key = f"modelHub.errors.{code}"
        super().__init__(detail_key)
        self.code = code
        self.status = status
        self.detail = detail_key


class EngineUnavailableError(RuntimeError):
    pass


class ModelHubConfigStore(Protocol):
    def load(self) -> ModelHubConfig: ...

    def save(self, config: ModelHubConfig) -> None: ...


class V2ModelHubConfigStore:
    def load(self) -> ModelHubConfig:
        try:
            return V2Config.load().model_hub
        except FileNotFoundError:
            return default_config().model_hub

    def save(self, model_hub: ModelHubConfig) -> None:
        with CONFIG_LOCK:
            try:
                config = V2Config.load()
            except FileNotFoundError:
                config = default_config()
            config.model_hub = model_hub
            config.save()


class UnavailableEngineAdapter:
    """Fail-closed placeholder until the L1 runtime implementation is present."""

    async def ensure_installed(self) -> EngineStatus:
        return await self.status()

    async def start(self) -> EngineStatus:
        raise EngineUnavailableError

    async def stop(self) -> None:
        return None

    async def status(self) -> EngineStatus:
        return EngineStatus(
            health=EngineHealth.NOT_INSTALLED,
            installed_version=None,
            verified=False,
            listen_host="127.0.0.1",
            listen_port=None,
            last_check_iso=None,
        )

    async def gateway_token(self) -> str:
        raise EngineUnavailableError

    async def provision_credential(self, vendor: str, protocol: str, secret: str, base_url: str | None) -> str:
        raise EngineUnavailableError

    async def revoke_credential(self, credential_ref: str) -> None:
        raise EngineUnavailableError

    async def sync_sources(self, bindings) -> None:
        raise EngineUnavailableError

    async def discover_models(self, vendor: str, protocol: str, base_url: str | None, credential_ref: str):
        raise EngineUnavailableError

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        raise EngineUnavailableError

    async def oauth_status(self, flow_id: str) -> OAuthFlowState:
        raise EngineUnavailableError

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        raise EngineUnavailableError

    async def cancel_oauth(self, flow_id: str) -> None:
        raise EngineUnavailableError

    async def invoke(
        self,
        source_id: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        origin: str,
    ) -> InvokeHandle:
        raise EngineUnavailableError


@dataclass(frozen=True)
class ResolvedInvocation:
    source_id: str
    model_id: str
    handle: Optional[InvokeHandle]
    outcome: Optional[RawCallOutcome]
    supply_channel: Literal["native_cli", "hub"] = "hub"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _source_id() -> str:
    return f"src_{uuid.uuid4().hex[:12]}"


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mask_credential(value: str) -> str:
    """Create the one-way display mask frozen by the source contract."""
    normalized = value.strip()
    if len(normalized) <= 4:
        return "…" + ("•" * len(normalized))
    prefix_length = min(7, len(normalized) - 5)
    return f"{normalized[:prefix_length]}…{normalized[-4:]}"


def _validated_base_url(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ModelHubError("discovery_failed")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        raise ModelHubError("discovery_failed") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or contains_credential_material(value)
    ):
        raise ModelHubError("discovery_failed")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = key.strip().lower().replace("-", "_").replace(".", "_")
        if normalized in _CREDENTIAL_QUERY_KEYS or any(
            marker in normalized
            for marker in (
                "api_key",
                "access_token",
                "auth_token",
                "token",
                "authorization",
                "signature",
                "secret",
                "password",
                "credential",
            )
        ):
            raise ModelHubError("discovery_failed")
    return value


def _native_model_ids(vendor: str) -> tuple[str, ...]:
    backend = _NATIVE_VENDOR_BACKENDS.get(vendor)
    if backend is None:
        return ()
    catalog = load_bundled_catalog()
    return tuple(entry["id"] for entry in backend_model_entries(backend, catalog))


def _default_protocol(vendor: str) -> str:
    if vendor == "anthropic":
        return "anthropic"
    if vendor == "openai":
        return "openai_responses"
    return "openai_compatible"


def _allowed_origins(source: ModelHubSourceConfig) -> tuple[str, ...]:
    if source.kind == "api_key":
        return ()
    if source.vendor == "anthropic":
        return ("claude",)
    if source.vendor == "openai":
        return ("codex",)
    return ()


def _binding(source: ModelHubSourceConfig) -> SourceBinding:
    if not source.credential_ref:
        raise ModelHubError("engine_down", status=503)
    allowed_origins = _allowed_origins(source)
    if source.kind == "subscription" and not allowed_origins:
        raise ModelHubError("mode_switch_blocked", status=409)
    return SourceBinding(
        source_id=source.id,
        vendor=source.vendor,
        protocol=source.protocol,
        base_url=source.base_url,
        credential_ref=source.credential_ref,
        allowed_origins=allowed_origins,
        model_ids=tuple(model.id for model in source.models),
    )


def _oauth_payload(flow: OAuthFlowState, *, channel: str) -> dict:
    return {
        "flow_id": flow.flow_id,
        "source_id": flow.source_id,
        "vendor": flow.vendor,
        "channel": channel,
        "state": flow.state,
        "presentation": {
            "auth_url": flow.auth_url,
            "device_code": flow.device_code,
            "expects": flow.expects,
            "instructions_key": flow.instructions_key,
        },
        "error_key": flow.error_key,
        "expires_at": flow.expires_at_iso,
    }


def _runtime_payload(status: EngineStatus) -> dict:
    return {
        "manifest": _RUNTIME_MANIFEST,
        "status": {
            "installed_version": status.installed_version,
            "verified": status.verified,
            "listening": (
                {"host": status.listen_host, "port": status.listen_port}
                if status.listen_port is not None
                else None
            ),
            "health": status.health.value,
            "last_check": status.last_check_iso,
        },
    }


class ModelHubService:
    def __init__(
        self,
        *,
        store: ModelHubConfigStore,
        adapter: EngineAdapter,
        events: BoundedEventLog,
        native_oauth_adapter: Optional[OAuthAdapter] = None,
        oauth_flows: Optional[OAuthFlowRegistry] = None,
        revocations: Optional[CredentialRevocationJournal] = None,
        now: Callable[[], datetime] = _utc_now,
    ):
        self.store = store
        self.adapter = adapter
        self.events = events
        self.native_oauth_adapter = native_oauth_adapter or UnavailableNativeOAuthAdapter()
        self.oauth_flows = oauth_flows or OAuthFlowRegistry(paths.get_state_dir() / "model_hub_oauth_flows.json")
        self.revocations = revocations or CredentialRevocationJournal(
            paths.get_state_dir() / "model_hub_pending_revocations.json"
        )
        self.now = now
        self._mutation_lock = asyncio.Lock()
        self._engine_synced = False

    @staticmethod
    def _source(config: ModelHubConfig, source_id: str) -> ModelHubSourceConfig:
        source = next((item for item in config.sources if item.id == source_id), None)
        if source is None:
            raise ModelHubError("source_not_found", status=404)
        return source

    @staticmethod
    def _agent(config: ModelHubConfig, backend: str) -> ModelHubAgentSupplyConfig:
        agent = config.agents.get(backend)
        if agent is None:
            raise ModelHubError("mode_switch_blocked")
        return agent

    async def _engine_call(self, awaitable):
        try:
            return await awaitable
        except OriginNotAllowedError:
            raise ModelHubError("mode_switch_blocked", status=409) from None
        except EngineUnavailableError:
            raise ModelHubError("engine_down", status=503) from None
        except NativeOAuthUnavailableError:
            raise ModelHubError("engine_down", status=503) from None
        except ModelHubError:
            raise
        except Exception:
            # Engine failures may carry upstream context. Never expose or log it.
            raise ModelHubError("engine_down", status=503) from None

    async def _oauth_call(self, awaitable, *, flow_id: Optional[str] = None):
        try:
            return await awaitable
        except KeyError:
            if flow_id is not None:
                self.oauth_flows.forget(flow_id)
            raise ModelHubError("flow_not_found", status=404) from None
        except (EngineUnavailableError, NativeOAuthUnavailableError):
            raise ModelHubError("engine_down", status=503) from None
        except ModelHubError:
            raise
        except Exception:
            raise ModelHubError("engine_down", status=503) from None

    def _bindings(self, config: ModelHubConfig) -> list[SourceBinding]:
        return [_binding(source) for source in config.sources if source.supply_channel == "hub"]

    @staticmethod
    def _clone_config(config: ModelHubConfig) -> ModelHubConfig:
        return ModelHubConfig.from_payload(config.to_payload())

    async def _sync_sources(self, config: ModelHubConfig, *, force_empty: bool = False) -> None:
        bindings = self._bindings(config)
        if not bindings and not force_empty:
            return
        await self._engine_call(self.adapter.sync_sources(bindings))

    async def _commit_synced(self, previous: ModelHubConfig, updated: ModelHubConfig) -> None:
        """Persist the authoritative config before updating its engine projection."""

        self._engine_synced = False
        previous_bindings = self._bindings(previous)
        updated_bindings = self._bindings(updated)
        self.store.save(updated)
        try:
            await self._sync_sources(updated, force_empty=bool(previous_bindings))
        except Exception:
            self.store.save(previous)
            try:
                await self._sync_sources(previous, force_empty=bool(updated_bindings))
            except ModelHubError:
                self._engine_synced = False
            else:
                self._engine_synced = True
            raise
        self._engine_synced = True

    async def _ensure_engine_synced(self) -> None:
        pending_revocations = self.revocations.list()
        if self._engine_synced and not pending_revocations:
            return
        async with self._mutation_lock:
            pending_revocations = self.revocations.list()
            if self._engine_synced and not pending_revocations:
                return
            config = self.store.load()
            await self._sync_sources(config, force_empty=bool(pending_revocations))
            active_source_ids = {source.id for source in config.sources}
            for pending in pending_revocations:
                if pending.source_id in active_source_ids:
                    self.revocations.remove(pending.source_id)
                    continue
                await self._engine_call(self.adapter.revoke_credential(pending.credential_ref))
                self.revocations.remove(pending.source_id)
            self._engine_synced = True

    def _oauth_adapter(self, channel: OAuthChannel) -> OAuthAdapter:
        if channel == "hub":
            return self.adapter
        return self.native_oauth_adapter

    def _oauth_channel(self, flow_id: str) -> OAuthChannel:
        return self._oauth_binding(flow_id).channel

    def _oauth_binding(self, flow_id: str) -> OAuthFlowBinding:
        binding = self.oauth_flows.binding(flow_id)
        if binding is None:
            raise ModelHubError("flow_not_found", status=404)
        return binding

    async def _oauth_status(self, flow_id: str, channel: OAuthChannel) -> OAuthFlowState:
        return await self._oauth_call(
            self._oauth_adapter(channel).oauth_status(flow_id),
            flow_id=flow_id,
        )

    async def _discover(self, source: ModelHubSourceConfig) -> list[str]:
        if not source.credential_ref:
            return [model.id for model in source.models]
        return list(
            await self._engine_call(
                self.adapter.discover_models(
                    source.vendor,
                    source.protocol,
                    source.base_url,
                    source.credential_ref,
                )
            )
        )

    def _record_event(self, **event_fields: Any) -> None:
        try:
            self.events.append(build_resolution_event(**event_fields))
        except Exception:
            # Resolution telemetry is best effort and must never affect routing.
            logger.warning("Failed to persist Model Hub resolution event")

    async def _rollback_credential(self, source_id: str, credential_ref: str) -> None:
        self.revocations.add(source_id, credential_ref)
        try:
            await self.adapter.revoke_credential(credential_ref)
        except Exception:
            return
        try:
            self.revocations.remove(source_id)
        except OSError:
            # A replayed revoke is safer than losing the only durable ref.
            pass

    def _raise_if_flow_expired(self, flow_id: str, flow: OAuthFlowState) -> None:
        if not flow.expires_at_iso or flow.state in {"success", "failed", "cancelled"}:
            return
        try:
            expired = _parse_datetime(flow.expires_at_iso) <= self.now()
        except ValueError:
            return
        if expired:
            self.oauth_flows.forget(flow_id)
            raise ModelHubError("flow_expired", status=410)

    def _apply_discovered_models(
        self,
        source: ModelHubSourceConfig,
        manual_models: list[ModelHubModelConfig],
        discovered: list[str],
    ) -> None:
        if any(
            not isinstance(model_id, str)
            or not model_id
            or contains_credential_material(model_id)
            for model_id in discovered
        ):
            raise ModelHubError("discovery_failed")
        discovered_at = self.now().isoformat()
        manual_model_ids = {model.id for model in manual_models}
        source.models = [
            ModelHubModelConfig(id=model_id, provenance="discovered", discovered_at=discovered_at)
            for model_id in discovered
            if model_id not in manual_model_ids
        ] + manual_models

    async def _commit_new_source_locked(
        self,
        source: ModelHubSourceConfig,
        *,
        consented: bool,
        previous: Optional[ModelHubConfig] = None,
    ) -> None:
        previous = previous or self.store.load()
        config = self._clone_config(previous)
        if any(item.id == source.id for item in config.sources):
            raise ModelHubError("migration_item_conflict", status=409)
        config.sources.append(source)
        config.priority_order.append(source.id)
        if consented:
            config.subscription_hub_experimental = True
        await self._commit_synced(previous, config)

    async def _create_oauth_source(
        self,
        source: ModelHubSourceConfig,
        manual_models: list[ModelHubModelConfig],
        *,
        oauth_ref: str,
        channel: Literal["native_cli", "hub"],
        vendor: str,
        consented: bool,
    ) -> dict:
        # Claim and consume a completed flow under the aggregate lock. This
        # prevents a duplicate browser retry from revoking the winning source's
        # credential while still retaining rollback ownership before discovery.
        async with self._mutation_lock:
            rollback_credential_ref: Optional[str] = None
            persisted = False
            try:
                binding = self._oauth_binding(oauth_ref)
                if binding.channel != channel:
                    raise ModelHubError("flow_not_found", status=404)
                flow = await self._oauth_status(oauth_ref, binding.channel)
                if flow.state != "success" or (channel == "hub" and not flow.credential_ref):
                    raise ModelHubError("flow_not_found", status=404)
                if (
                    flow.vendor != vendor
                    or flow.vendor != binding.vendor
                    or flow.source_id != binding.source_id
                ):
                    raise ModelHubError("flow_not_found", status=404)

                source.id = flow.source_id
                previous = self.store.load()
                if any(item.id == source.id for item in previous.sources):
                    raise ModelHubError("migration_item_conflict", status=409)
                if channel == "hub":
                    source.credential_ref = cast(str, flow.credential_ref)
                    rollback_credential_ref = source.credential_ref

                discovered = (
                    await self._discover(source)
                    if channel == "hub"
                    else list(_native_model_ids(vendor))
                )
                if channel == "native_cli" and not discovered:
                    raise ModelHubError("discovery_failed")
                self._apply_discovered_models(source, manual_models, discovered)
                await self._commit_new_source_locked(
                    source,
                    consented=consented,
                    previous=previous,
                )
                persisted = True
                try:
                    self.oauth_flows.forget(oauth_ref)
                except OSError:
                    pass
                return source.to_payload()
            except Exception:
                if rollback_credential_ref is not None and not persisted:
                    await self._rollback_credential(source.id, rollback_credential_ref)
                    try:
                        self.oauth_flows.forget(oauth_ref)
                    except OSError:
                        pass
                raise

    def list_sources(self) -> list[dict]:
        config = self.store.load()
        by_id = {source.id: source for source in config.sources}
        return [by_id[source_id].to_payload() for source_id in config.priority_order]

    async def create_source(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ModelHubError("discovery_failed")
        forbidden = {
            "id",
            "credential_ref",
            "account_label",
            "masked_credential",
            "experimental_consent_at",
            "state",
            "usage",
        } & set(payload)
        if forbidden:
            raise ModelHubError("discovery_failed")
        kind = payload.get("kind")
        vendor = payload.get("vendor")
        display_name = payload.get("display_name") or vendor
        if (
            kind not in {"subscription", "api_key"}
            or not isinstance(vendor, str)
            or not vendor
            or contains_credential_material(vendor)
        ):
            raise ModelHubError("discovery_failed")
        if (
            not isinstance(display_name, str)
            or not display_name
            or len(display_name) > 64
            or contains_credential_material(display_name)
        ):
            raise ModelHubError("discovery_failed")
        channel = payload.get("supply_channel") or ("native_cli" if kind == "subscription" else "hub")
        if channel not in {"native_cli", "hub"} or (kind == "api_key" and channel != "hub"):
            raise ModelHubError("discovery_failed")
        if channel == "native_cli" and vendor not in _NATIVE_VENDOR_BACKENDS:
            raise ModelHubError("discovery_failed")
        consented = payload.get("experimental_consent") is True
        if "experimental_consent" in payload and not isinstance(payload.get("experimental_consent"), bool):
            raise ModelHubError("consent_required")
        if kind == "subscription" and channel == "hub" and not consented:
            raise ModelHubError("consent_required", status=409)
        if consented and not (kind == "subscription" and channel == "hub"):
            raise ModelHubError("consent_required")

        protocol = payload.get("protocol") or _default_protocol(vendor)
        billing = payload.get("billing") or ("monthly" if kind == "subscription" else "metered")
        models_payload = payload.get("models", [])
        if not isinstance(models_payload, list):
            raise ModelHubError("discovery_failed")
        base_url = _validated_base_url(payload.get("base_url"))
        if kind == "subscription" and base_url is not None:
            raise ModelHubError("discovery_failed")
        try:
            manual_models = [ModelHubModelConfig.from_payload(model) for model in models_payload]
            if any(
                model.provenance != "manual"
                or contains_credential_material(model.id)
                or contains_credential_material(model.display_name or "")
                for model in manual_models
            ):
                raise ValueError("Client-declared source models must use manual provenance")
            source = ModelHubSourceConfig(
                id=_source_id(),
                kind=kind,
                vendor=vendor,
                display_name=display_name,
                protocol=protocol,
                base_url=base_url,
                supply_channel=channel,
                experimental_consent_at=self.now().isoformat() if consented else None,
                billing=billing,
                state=ModelHubSourceStateConfig(status="standby"),
                usage=ModelHubSourceUsageConfig(),
                models=manual_models,
            )
            source = ModelHubSourceConfig.from_payload(source.to_payload())
        except (TypeError, ValueError):
            raise ModelHubError("discovery_failed") from None

        credential_value = payload.get("key")
        oauth_ref = payload.get("oauth_flow_ref")
        if credential_value is not None:
            if not isinstance(credential_value, str):
                raise ModelHubError("discovery_failed")
            credential_value = credential_value.strip()
        if oauth_ref is not None and not isinstance(oauth_ref, str):
            raise ModelHubError("flow_not_found", status=404)
        if kind == "subscription" and credential_value is not None:
            raise ModelHubError("discovery_failed")
        if kind == "api_key" and oauth_ref is not None:
            raise ModelHubError("discovery_failed")
        if kind == "api_key" and not credential_value:
            raise ModelHubError("discovery_failed")

        if oauth_ref:
            return await self._create_oauth_source(
                source,
                manual_models,
                oauth_ref=oauth_ref,
                channel=cast(Literal["native_cli", "hub"], channel),
                vendor=vendor,
                consented=consented,
            )
        if kind == "subscription":
            raise ModelHubError("flow_not_found", status=404)

        rollback_credential_ref = await self._engine_call(
            self.adapter.provision_credential(vendor, protocol, cast(str, credential_value), source.base_url)
        )
        source.credential_ref = rollback_credential_ref
        source.masked_credential = _mask_credential(cast(str, credential_value))
        persisted = False
        try:
            discovered = await self._discover(source)
            self._apply_discovered_models(source, manual_models, discovered)
            async with self._mutation_lock:
                await self._commit_new_source_locked(source, consented=consented)
                persisted = True
            return source.to_payload()
        except Exception:
            if not persisted:
                await self._rollback_credential(source.id, rollback_credential_ref)
            raise

    async def patch_source(self, source_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"display_name", "base_url"}:
            raise ModelHubError("discovery_failed")
        base_url = _validated_base_url(payload.get("base_url")) if "base_url" in payload else None
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            if "display_name" in payload:
                display_name = payload["display_name"]
                if (
                    not isinstance(display_name, str)
                    or not display_name
                    or len(display_name) > 64
                    or contains_credential_material(display_name)
                ):
                    raise ModelHubError("discovery_failed")
                source.display_name = display_name
            if "base_url" in payload:
                if source.kind != "api_key":
                    raise ModelHubError("discovery_failed")
                source.base_url = base_url
                discovered = await self._discover(source)
                manual = [model for model in source.models if model.provenance == "manual"]
                self._apply_discovered_models(source, manual, discovered)
            if "base_url" in payload:
                await self._commit_synced(previous, config)
            else:
                self.store.save(config)
            return source.to_payload()

    @staticmethod
    def _selected_targets(agent: ModelHubAgentSupplyConfig) -> list[tuple[Optional[str], str]]:
        if agent.backend == "opencode" and agent.menu:
            targets = []
            for identifier in agent.menu.checked:
                try:
                    provider, model_id = parse_opencode_model_id(identifier)
                except ValueError:
                    continue
                else:
                    targets.append((provider, model_id))
            return targets
        return [(None, mapping.target_model_id) for mapping in agent.mappings if mapping.enabled]

    def _only_selected_model_supplier(
        self,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
        model_id: str,
    ) -> bool:
        for agent in config.agents.values():
            if agent.mode != "hub":
                continue
            for provider, target_model_id in self._selected_targets(agent):
                if target_model_id != model_id or (
                    provider is not None and provider != opencode_provider_id(source.vendor)
                ):
                    continue
                suppliers = [
                    candidate
                    for candidate in config.sources
                    if self._eligible_for_agent(candidate, agent.backend)
                    and (provider is None or opencode_provider_id(candidate.vendor) == provider)
                    and any(item.id == model_id for item in candidate.models)
                ]
                if len(suppliers) == 1 and suppliers[0].id == source.id:
                    return True
        return False

    def _only_selected_supplier(self, config: ModelHubConfig, source: ModelHubSourceConfig) -> bool:
        return any(self._only_selected_model_supplier(config, source, model.id) for model in source.models)

    async def delete_source(self, source_id: str, *, force: bool = False) -> None:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            if not force and self._only_selected_supplier(config, source):
                raise ModelHubError("mode_switch_blocked", status=409)
            config.sources = [item for item in config.sources if item.id != source_id]
            config.priority_order = [item for item in config.priority_order if item != source_id]
            if source.credential_ref:
                self.revocations.add(source.id, source.credential_ref)
            try:
                await self._commit_synced(previous, config)
            except Exception:
                self.revocations.remove(source.id)
                raise
            try:
                if source.credential_ref:
                    await self._engine_call(self.adapter.revoke_credential(source.credential_ref))
            except ModelHubError:
                restored = False
                try:
                    self.store.save(previous)
                    restored = True
                    self._engine_synced = False
                    await self._sync_sources(previous)
                    self._engine_synced = True
                finally:
                    if restored:
                        self.revocations.remove(source.id)
                raise
            self.revocations.remove(source.id)

    async def test_source(self, source_id: str) -> tuple[dict, int]:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            if source.supply_channel == "native_cli":
                raise ModelHubError("discovery_failed")
            model_ids = await self._discover(source)
            manual = [model for model in source.models if model.provenance == "manual"]
            self._apply_discovered_models(source, manual, model_ids)
            source.state = ModelHubSourceStateConfig(status="standby")
            await self._commit_synced(previous, config)
            return source.to_payload(), len(model_ids)

    async def set_priority(self, order: object) -> dict:
        async with self._mutation_lock:
            config = self.store.load()
            source_ids = [source.id for source in config.sources]
            if (
                not isinstance(order, list)
                or not all(isinstance(item, str) for item in order)
                or len(set(order)) != len(order)
                or set(order) != set(source_ids)
            ):
                raise ModelHubError("invalid_priority_order")
            config.priority_order = list(order)
            self.store.save(config)
            return {"contract_version": CONTRACT_VERSION, "order": list(order)}

    def priority(self) -> dict:
        return {"contract_version": CONTRACT_VERSION, "order": list(self.store.load().priority_order)}

    @staticmethod
    def _eligible_for_agent(source: ModelHubSourceConfig, backend: str) -> bool:
        if source.kind == "api_key":
            return source.supply_channel == "hub"
        return backend in _allowed_origins(source)

    def _source_available(self, source: ModelHubSourceConfig) -> bool:
        if source.state.status == "error":
            return False
        if source.state.status != "cooldown":
            return True
        try:
            return _parse_datetime(source.state.retry_at or "") <= self.now()
        except ValueError:
            return False

    def _agent_payload(self, config: ModelHubConfig, agent: ModelHubAgentSupplyConfig) -> dict:
        current = None
        if agent.mode == "hub":
            if agent.backend == "opencode" and (agent.menu is None or not agent.menu.checked):
                return {**agent.to_payload(), "current": None}
            provider = None
            target = next((mapping.target_model_id for mapping in agent.mappings if mapping.enabled), None)
            if target is None and agent.menu and agent.menu.checked:
                try:
                    provider, target = parse_opencode_model_id(agent.menu.checked[0])
                except ValueError:
                    provider = None
            by_id = {source.id: source for source in config.sources}
            for source_id in config.priority_order:
                source = by_id[source_id]
                if not self._eligible_for_agent(source, agent.backend):
                    continue
                if not self._source_available(source):
                    continue
                if provider is not None and opencode_provider_id(source.vendor) != provider:
                    continue
                model = next((model for model in source.models if target is None or model.id == target), None)
                if model is not None:
                    current = {"model_id": model.id, "source_id": source.id, "channel": source.supply_channel}
                    break
        return {**agent.to_payload(), "current": current}

    def list_agents(self) -> list[dict]:
        config = self.store.load()
        return [self._agent_payload(config, config.agents[backend]) for backend in ("claude", "codex", "opencode")]

    async def set_agent_mode(self, backend: str, mode: object) -> dict:
        if mode not in {"hub", "direct"}:
            raise ModelHubError("mode_switch_blocked")
        async with self._mutation_lock:
            config = self.store.load()
            agent = self._agent(config, backend)
            agent.mode = mode
            self.store.save(config)
            return self._agent_payload(config, agent)

    async def set_mappings(self, backend: str, mappings: object) -> dict:
        async with self._mutation_lock:
            config = self.store.load()
            agent = self._agent(config, backend)
            if agent.menu_kind != "fixed" or not isinstance(mappings, list):
                raise ModelHubError("mapping_target_unavailable")
            try:
                parsed = [ModelHubMappingConfig.from_payload(mapping) for mapping in mappings]
            except ValueError as exc:
                raise ModelHubError("mapping_target_unavailable") from exc
            available = {
                model.id
                for source in config.sources
                if self._eligible_for_agent(source, backend)
                for model in source.models
            }
            if any(mapping.enabled and mapping.target_model_id not in available for mapping in parsed):
                raise ModelHubError("mapping_target_unavailable")
            agent.mappings = parsed
            self.store.save(config)
            return self._agent_payload(config, agent)

    async def set_opencode_menu(self, menu: object) -> dict:
        async with self._mutation_lock:
            config = self.store.load()
            agent = config.agents["opencode"]
            try:
                parsed = ModelHubMenuConfig.from_payload(cast(dict, menu))
            except (TypeError, ValueError) as exc:
                raise ModelHubError("mapping_target_unavailable") from exc
            available = {
                opencode_model_id(source.vendor, model.id)
                for source in config.sources
                if self._eligible_for_agent(source, "opencode")
                for model in source.models
            }
            if any(identifier not in available for identifier in parsed.checked):
                raise ModelHubError("mapping_target_unavailable")
            agent.menu = parsed
            self.store.save(config)
            return self._agent_payload(config, agent)

    async def add_custom_model(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ModelHubError("source_not_found", status=404)
        model_id = payload.get("model_id")
        display_name = payload.get("display_name")
        if (
            not isinstance(model_id, str)
            or not model_id
            or contains_credential_material(model_id)
        ):
            raise ModelHubError("mapping_target_unavailable")
        if display_name is not None and (
            not isinstance(display_name, str) or contains_credential_material(display_name)
        ):
            raise ModelHubError("mapping_target_unavailable")
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, str(payload.get("source_id") or ""))
            existing = next((model for model in source.models if model.id == model_id), None)
            if existing is None:
                source.models.append(
                    ModelHubModelConfig(
                        id=model_id,
                        display_name=display_name,
                        provenance="manual",
                        discovered_at=None,
                    )
                )
            elif existing.provenance == "manual":
                existing.display_name = display_name
            await self._commit_synced(previous, config)
            return source.to_payload()

    async def delete_custom_model(self, source_id: object, model_id: object) -> dict:
        if not isinstance(model_id, str) or not model_id:
            raise ModelHubError("mapping_target_unavailable")
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, str(source_id or ""))
            manual = next(
                (model for model in source.models if model.id == model_id and model.provenance == "manual"),
                None,
            )
            if manual is not None and self._only_selected_model_supplier(config, source, model_id):
                raise ModelHubError("mode_switch_blocked", status=409)
            source.models = [
                model
                for model in source.models
                if not (model.id == model_id and model.provenance == "manual")
            ]
            await self._commit_synced(previous, config)
            return source.to_payload()

    def list_events(self, *, limit: int = 20, before: Optional[str] = None) -> list[dict]:
        return self.events.list(limit=limit, before=before)

    async def oauth_start(self, payload: dict) -> dict:
        vendor = payload.get("vendor") if isinstance(payload, dict) else None
        channel = payload.get("channel") if isinstance(payload, dict) else None
        if not isinstance(vendor, str) or channel not in {"native_cli", "hub"}:
            raise ModelHubError("flow_not_found", status=400)
        consented = payload.get("experimental_consent") is True
        if "experimental_consent" in payload and not isinstance(payload.get("experimental_consent"), bool):
            raise ModelHubError("consent_required")
        if channel == "hub" and not consented:
            raise ModelHubError("consent_required", status=409)
        if consented and channel != "hub":
            raise ModelHubError("consent_required")
        oauth_channel = cast(OAuthChannel, channel)
        pending_source_id = _source_id()
        flow = await self._oauth_call(
            self._oauth_adapter(oauth_channel).start_oauth(pending_source_id, vendor)
        )
        if flow.source_id != pending_source_id or flow.vendor != vendor:
            raise ModelHubError("flow_not_found", status=502)
        self.oauth_flows.remember(flow.flow_id, oauth_channel, pending_source_id, vendor)
        if channel == "hub" and consented:
            async with self._mutation_lock:
                config = self.store.load()
                config.subscription_hub_experimental = True
                self.store.save(config)
        return _oauth_payload(flow, channel=channel)

    async def oauth_status(self, flow_id: str) -> dict:
        channel = self._oauth_channel(flow_id)
        flow = await self._oauth_status(flow_id, channel)
        self._raise_if_flow_expired(flow_id, flow)
        return _oauth_payload(flow, channel=channel)

    async def oauth_submit(self, payload: dict) -> dict:
        flow_id = payload.get("flow_id") if isinstance(payload, dict) else None
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(flow_id, str) or not isinstance(value, str):
            raise ModelHubError("flow_not_found", status=404)
        channel = self._oauth_channel(flow_id)
        current = await self._oauth_status(flow_id, channel)
        self._raise_if_flow_expired(flow_id, current)
        flow = await self._oauth_call(
            self._oauth_adapter(channel).submit_oauth(flow_id, value),
            flow_id=flow_id,
        )
        return _oauth_payload(flow, channel=channel)

    async def oauth_cancel(self, flow_id: object) -> None:
        if not isinstance(flow_id, str):
            raise ModelHubError("flow_not_found", status=404)
        channel = self._oauth_channel(flow_id)
        await self._oauth_call(
            self._oauth_adapter(channel).cancel_oauth(flow_id),
            flow_id=flow_id,
        )
        self.oauth_flows.forget(flow_id)

    async def runtime_status(self) -> dict:
        return _runtime_payload(await self._engine_call(self.adapter.status()))

    def migration_scan(self) -> dict:
        # L6 supplies the native-config scanner. Empty is valid and read-only.
        return {"items": []}

    def migration_apply(self, item_ids: object) -> dict:
        if not isinstance(item_ids, list) or item_ids:
            raise ModelHubError("migration_item_conflict", status=409)
        return {"applied": 0, "sources": self.list_sources()}

    async def _resolution_candidates(
        self,
        backend: str,
        model_id: str,
        *,
        provider: Optional[str] = None,
    ) -> list[ModelHubSourceConfig]:
        async with self._mutation_lock:
            config = self.store.load()
            by_id = {source.id: source for source in config.sources}
            candidates: list[ModelHubSourceConfig] = []
            config_changed = False
            for source_id in config.priority_order:
                source = by_id[source_id]
                matches_request = (
                    self._eligible_for_agent(source, backend)
                    and (provider is None or opencode_provider_id(source.vendor) == provider)
                    and any(model.id == model_id for model in source.models)
                )
                if not matches_request:
                    continue
                if source.state.status == "cooldown":
                    try:
                        retry_at = _parse_datetime(source.state.retry_at or "")
                    except ValueError:
                        retry_at = self.now() + timedelta(days=1)
                    if retry_at > self.now():
                        continue
                    source.state = ModelHubSourceStateConfig(status="standby")
                    config_changed = True
                    self._record_event(
                        agent=cast(EventAgent, backend),
                        kind="recover",
                        model_id=model_id,
                        reason="recovery",
                        to_source=source.id,
                        to_label=source.display_name,
                        now=self.now(),
                    )
                if source.state.status != "error":
                    candidates.append(source)
            if config_changed:
                self.store.save(config)
            return candidates

    async def _cooldown(
        self,
        source: ModelHubSourceConfig,
        decision: ResolutionDecision,
        *,
        agent: EventAgent,
        model_id: str,
    ) -> None:
        async with self._mutation_lock:
            config = self.store.load()
            try:
                current = self._source(config, source.id)
            except ModelHubError:
                return
            current.state = ModelHubSourceStateConfig(
                status="cooldown",
                retry_at=(self.now() + timedelta(seconds=decision.cooldown_seconds)).isoformat(),
                detail_key=f"models.source.cooldown.{decision.reason}",
            )
            self.store.save(config)
            self._record_event(
                agent=agent,
                kind="cooldown",
                model_id=model_id,
                reason=cast(EventReason, decision.reason),
                from_source=current.id,
                from_label=current.display_name,
                now=self.now(),
            )

    def _emit_switch(
        self,
        *,
        agent: EventAgent,
        model_id: str,
        failed_source: Optional[ModelHubSourceConfig],
        failed_reason: Optional[EventReason],
        source: ModelHubSourceConfig,
    ) -> None:
        if failed_source is None or failed_reason is None:
            return
        billing_note = (
            "entered_metered" if failed_source.billing == "monthly" and source.billing == "metered" else None
        )
        self._record_event(
            agent=agent,
            kind="switch",
            model_id=model_id,
            reason=failed_reason,
            from_source=failed_source.id,
            to_source=source.id,
            from_label=failed_source.display_name,
            to_label=source.display_name,
            billing_note=billing_note,
            now=self.now(),
        )

    async def _invoke(
        self,
        *,
        source: ModelHubSourceConfig,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        backend: str,
    ) -> tuple[InvokeHandle, Optional[RawCallOutcome]]:
        handle = await self._engine_call(
            self.adapter.invoke(source.id, model_id, request, stream, backend)
        )
        if handle.stream is not None:
            return handle, None
        return handle, await self._engine_call(handle.outcome())

    async def resolve(
        self,
        *,
        backend: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool = False,
    ) -> ResolvedInvocation:
        if backend not in {"claude", "codex", "opencode"}:
            raise ModelHubError("mapping_target_unavailable")
        config = self.store.load()
        agent = self._agent(config, backend)
        if agent.mode != "hub":
            raise ModelHubError("mode_switch_blocked", status=409)
        target_model = next(
            (
                mapping.target_model_id
                for mapping in agent.mappings
                if mapping.enabled and mapping.builtin_id == model_id
            ),
            model_id,
        )
        mapping_applied = target_model != model_id
        provider = None
        if backend == "opencode":
            try:
                provider, target_model = parse_opencode_model_id(target_model)
            except ValueError:
                raise ModelHubError("mapping_target_unavailable", status=409)
            selected_identifier = f"{provider}/{target_model}"
            if agent.menu is None or selected_identifier not in agent.menu.checked:
                raise ModelHubError("mapping_target_unavailable", status=409)
        event_agent = cast(EventAgent, backend)
        if mapping_applied:
            self._record_event(
                agent=event_agent,
                kind="mapping_applied",
                model_id=target_model,
                reason="mapping",
                from_label=model_id,
                now=self.now(),
            )
        candidates = await self._resolution_candidates(backend, target_model, provider=provider)
        if not candidates:
            raise ModelHubError("mapping_target_unavailable", status=409)

        failed_source: Optional[ModelHubSourceConfig] = None
        failed_reason: Optional[EventReason] = None
        for source in candidates:
            if source.supply_channel == "native_cli":
                if self.revocations.list():
                    try:
                        await self._ensure_engine_synced()
                    except ModelHubError:
                        # Credential cleanup remains durable; native routing is independent.
                        pass
                self._emit_switch(
                    agent=event_agent,
                    model_id=target_model,
                    failed_source=failed_source,
                    failed_reason=failed_reason,
                    source=source,
                )
                return ResolvedInvocation(
                    source.id,
                    target_model,
                    None,
                    None,
                    supply_channel="native_cli",
                )
            await self._ensure_engine_synced()
            handle, outcome = await self._invoke(
                source=source,
                model_id=target_model,
                request=request,
                stream=stream,
                backend=backend,
            )
            if outcome is None:
                self._emit_switch(
                    agent=event_agent,
                    model_id=target_model,
                    failed_source=failed_source,
                    failed_reason=failed_reason,
                    source=source,
                )
                return ResolvedInvocation(source.id, target_model, handle, None)
            decision = classify_outcome(outcome)
            if decision.action == "refresh":
                # The engine refreshes its credential internally; L2 retries the
                # exact same source once and never falls through on a second 401.
                handle, outcome = await self._invoke(
                    source=source,
                    model_id=target_model,
                    request=request,
                    stream=stream,
                    backend=backend,
                )
                if outcome is None:
                    self._emit_switch(
                        agent=event_agent,
                        model_id=target_model,
                        failed_source=failed_source,
                        failed_reason=failed_reason,
                        source=source,
                    )
                    return ResolvedInvocation(source.id, target_model, handle, None)
                decision = classify_outcome(outcome, refresh_attempted=True)
            if decision.action == "return":
                self._emit_switch(
                    agent=event_agent,
                    model_id=target_model,
                    failed_source=failed_source,
                    failed_reason=failed_reason,
                    source=source,
                )
                return ResolvedInvocation(source.id, target_model, handle, outcome)
            if decision.action == "fallback":
                await self._cooldown(source, decision, agent=event_agent, model_id=target_model)
                failed_source = source
                failed_reason = cast(EventReason, decision.reason)
                continue
            raise ModelHubError(decision.error_code or "engine_down", status=502)
        raise ModelHubError("engine_down", status=503)


def create_default_service(
    *,
    adapter: Optional[EngineAdapter] = None,
    native_oauth_adapter: Optional[OAuthAdapter] = None,
) -> ModelHubService:
    return ModelHubService(
        store=V2ModelHubConfigStore(),
        adapter=adapter or UnavailableEngineAdapter(),
        events=BoundedEventLog(paths.get_state_dir() / "model_hub_resolution_events.json"),
        native_oauth_adapter=native_oauth_adapter,
        oauth_flows=OAuthFlowRegistry(paths.get_state_dir() / "model_hub_oauth_flows.json"),
        revocations=CredentialRevocationJournal(paths.get_state_dir() / "model_hub_pending_revocations.json"),
    )
