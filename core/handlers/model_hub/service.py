"""Model Hub aggregate service used by REST routes and backend injection."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, cast

from sqlalchemy import func, select

from config import paths
from config.v2_config import (
    CONFIG_LOCK,
    MODEL_HUB_BACKENDS,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    model_hub_fixed_menu_ids,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
    canonical_opencode_menu_identity,
    normalize_model_hub_base_url,
    normalize_model_hub_vendor_id,
)
from core.services.settings import default_config
from storage.db import get_cached_sqlite_engine
from storage.models import agent_sessions, messages

from .adapter import (
    EngineAdapter,
    EngineHealth,
    EngineStatus,
    InvokeHandle,
    OAuthFlowState,
    OriginNotAllowedError,
    RawCallOutcome,
    RawOutcomeKind,
    RetainedMaterialDisposition,
    ObservationDiscovery,
    ObservationOutcome,
    SOURCE_PROTOCOLS,
    SourceObservation,
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
from .errors import ModelDiscoveryError
from .identifiers import STANDARD_OPENCODE_VENDOR_IDS
from .migration import (
    MigrationConflictError,
    apply_native_migration,
    scan_native_configs,
)
from .oauth import (
    NativeOAuthAdapter,
    NativeOAuthUnavailableError,
    OAuthAdapter,
    OAuthChannel,
    OAuthFlowBinding,
    OAuthFlowRegistry,
    UnavailableNativeOAuthAdapter,
)
from .provenance import BoundedProvenanceStore
from .request import ModelHubRequest
from .resolver import (
    BackendName,
    ModelHubTurnResolution,
    allowed_origins,
    inspect_exact_hop,
    matching_v1_model_id as _matching_v1_model_id,
    resolve_model_hub_turn,
    source_after_cooldown_recovery,
    source_eligible_for_backend,
    source_runnable,
)
from .revocations import CredentialRevocationJournal

CONTRACT_VERSION = 5
AGENT_CHAIN_CONTRACT_VERSION = 5
PROBE_RESULT_CONTRACT_VERSION = 5
logger = logging.getLogger(__name__)

_NATIVE_VENDOR_BACKENDS = {"anthropic": "claude", "openai": "codex"}
class ModelHubError(Exception):
    def __init__(
        self,
        code: str,
        *,
        status: int = 400,
        detail: Optional[str] = None,
        supply_state: Optional[Literal["waiting", "interrupted"]] = None,
        data: Optional[Mapping[str, Any]] = None,
    ):
        detail_key = detail or f"modelHub.errors.{code}"
        super().__init__(detail_key)
        self.code = code
        self.status = status
        self.detail = detail_key
        self.supply_state = supply_state
        self.data = dict(data or {})


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
        model_hub = ModelHubConfig.from_payload(model_hub.to_payload())
        with CONFIG_LOCK:
            try:
                config = V2Config.load()
            except FileNotFoundError:
                config = default_config()
            config.model_hub = model_hub
            config.save()

class UnavailableEngineAdapter:
    """Explicit fail-closed adapter for isolated callers and tests."""

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

    async def provision_transient_credential(self, vendor: str, secret: str, base_url: str | None) -> str:
        raise EngineUnavailableError

    async def revoke_credential(self, credential_ref: str) -> None:
        raise EngineUnavailableError

    async def sync_sources(self, bindings) -> None:
        raise EngineUnavailableError

    async def discover_models(self, vendor: str, protocol: str, base_url: str | None, credential_ref: str):
        raise EngineUnavailableError

    async def observe_source(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order,
    ) -> SourceObservation:
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


AttemptObserver = Callable[
    [
        str,
        str,
        Literal["native_cli", "hub"],
        bool,
        Optional[RawCallOutcome],
        Optional[ResolutionDecision],
    ],
    None,
]


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
    try:
        return normalize_model_hub_base_url(value)
    except (TypeError, ValueError):
        raise ModelHubError("discovery_failed") from None


def _builtin_model_ids(backend: str) -> tuple[str, ...]:
    """Real built-in model ids for a fixed-menu backend, from the bundled catalog.

    Single source of truth for both the native-subscription supply list
    (_native_model_ids) and the agents endpoint's read-only builtin_models
    projection (agent-supply.schema.json v1.2), so the two never diverge.
    """
    return model_hub_fixed_menu_ids(backend)


def _native_model_ids(vendor: str) -> tuple[str, ...]:
    backend = _NATIVE_VENDOR_BACKENDS.get(vendor)
    if backend is None:
        return ()
    return _builtin_model_ids(backend)


def _default_protocol(vendor: str) -> str:
    if vendor == "anthropic":
        return "anthropic"
    if vendor == "openai":
        return "openai_responses"
    return "openai_chat"


async def _provision_transient_credential_with_cancellation_ownership(
    service: "ModelHubService",
    vendor: str,
    key: str,
    base_url: str | None,
) -> str:
    """Keep ownership of an engine ref when the caller is cancelled mid-provision."""

    provision_task = asyncio.create_task(
        service._engine_call(
            service.adapter.provision_transient_credential(vendor, key, base_url)
        )
    )
    try:
        return await asyncio.shield(provision_task)
    except asyncio.CancelledError as cancelled:
        # The shield leaves provisioning alive; wait for its ref before settling
        # cancellation so the transient material can be journaled and revoked.
        while True:
            try:
                transient_ref = await asyncio.shield(provision_task)
                break
            except asyncio.CancelledError:
                continue
        await _rollback_credential_before_settling(service, "observation", transient_ref)
        raise cancelled


async def _rollback_credential_before_settling(
    service: "ModelHubService",
    source_id: str,
    credential_ref: str,
) -> bool:
    """Finish transient cleanup before propagating a cancellation."""

    rollback_task = asyncio.create_task(
        service._rollback_credential(source_id, credential_ref)
    )
    try:
        return await asyncio.shield(rollback_task)
    except asyncio.CancelledError as cancelled:
        while True:
            try:
                await asyncio.shield(rollback_task)
                break
            except asyncio.CancelledError:
                continue
        raise cancelled


async def _rollback_replacement_before_settling(
    service: "ModelHubService",
    source_id: str,
    replacement_ref: str,
    old_credential_ref: str | None,
    *,
    old_revocation_recorded: bool,
) -> None:
    replacement_task = asyncio.create_task(
        service._rollback_replacement(
            source_id,
            replacement_ref,
            old_credential_ref,
            old_revocation_recorded=old_revocation_recorded,
        )
    )
    try:
        await asyncio.shield(replacement_task)
    except asyncio.CancelledError as cancelled:
        while True:
            try:
                await asyncio.shield(replacement_task)
                break
            except asyncio.CancelledError:
                continue
        raise cancelled


def _binding(source: ModelHubSourceConfig) -> SourceBinding:
    if not source.credential_ref:
        raise ModelHubError("engine_down", status=503)
    source_origins = allowed_origins(source)
    if source.kind == "subscription" and not source_origins:
        raise ModelHubError("mode_switch_blocked", status=409)
    return SourceBinding(
        source_id=source.id,
        vendor=source.vendor,
        protocol=source.protocol,
        base_url=source.base_url,
        credential_ref=source.credential_ref,
        allowed_origins=source_origins,
        model_ids=tuple(model.id for model in source.models),
    )


def _oauth_payload(
    flow: OAuthFlowState,
    *,
    channel: str,
    intent: Literal["create", "reauth"] = "create",
) -> dict:
    return {
        "flow_id": flow.flow_id,
        "intent": intent,
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
    # Import lazily to avoid the runtime adapter's dependency back on this service module.
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    return {
        "contract_version": 5,
        "manifest": EngineRuntimeManager().contract_manifest(),
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
        provenance: Optional[BoundedProvenanceStore] = None,
        native_oauth_adapter: Optional[NativeOAuthAdapter] = None,
        oauth_flows: Optional[OAuthFlowRegistry] = None,
        revocations: Optional[CredentialRevocationJournal] = None,
        migration_claude_oauth_probe: Optional[Callable[[], bool]] = None,
        requested_model_override: Optional[Callable[[BackendName], Optional[str]]] = None,
        selected_agent_override: Optional[Callable[[BackendName], Optional[str]]] = None,
        named_agents_override: Optional[
            Callable[[BackendName], list[tuple[str, Optional[str]]]]
        ] = None,
        now: Callable[[], datetime] = _utc_now,
    ):
        self.store = store
        self.adapter = adapter
        self.events = events
        self.provenance = provenance or BoundedProvenanceStore(
            paths.get_state_dir() / "model_hub_turn_provenance.json"
        )
        self.native_oauth_adapter = native_oauth_adapter or UnavailableNativeOAuthAdapter()
        self.oauth_flows = oauth_flows or OAuthFlowRegistry(paths.get_state_dir() / "model_hub_oauth_flows.json")
        self.revocations = revocations or CredentialRevocationJournal(
            paths.get_state_dir() / "model_hub_pending_revocations.json"
        )
        self.migration_claude_oauth_probe = migration_claude_oauth_probe
        self.requested_model_override = requested_model_override
        self.selected_agent_override = selected_agent_override
        self.named_agents_override = named_agents_override
        self.now = now
        self.native_source_ready: Callable[[BackendName, ModelHubSourceConfig], bool] = (
            lambda _backend, _source: True
        )
        self._mutation_lock = asyncio.Lock()
        self._engine_synced = False
        self._engine_preparation_failed = False

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

    def _requested_model(self, agent: ModelHubAgentSupplyConfig) -> str:
        if self.requested_model_override is not None:
            requested_model = str(
                self.requested_model_override(cast(BackendName, agent.backend)) or ""
            ).strip()
            if requested_model:
                return requested_model
        return ""

    def _unavailable_native_sources(
        self,
        config: ModelHubConfig,
        backend: BackendName,
    ) -> frozenset[str]:
        return frozenset(
            source.id
            for source in config.sources
            if source.supply_channel == "native_cli"
            and source_eligible_for_backend(source, backend)
            and not self.native_source_ready(
                backend,
                source_after_cooldown_recovery(source, self.now()),
            )
        )

    async def _engine_call(self, awaitable):
        try:
            return await awaitable
        except OriginNotAllowedError:
            raise ModelHubError("mode_switch_blocked", status=409) from None
        except ModelDiscoveryError:
            raise ModelHubError("discovery_failed", status=502) from None
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
        ineligible_source_ids = {
            inspection.source_id
            for backend_name in MODEL_HUB_BACKENDS
            for menu_model, route in config.agents[backend_name].routes.items()
            for hop in route.hops
            for inspection in (
                inspect_exact_hop(
                    config,
                    cast(BackendName, backend_name),
                    menu_model,
                    hop,
                ),
            )
            if inspection.source_id is not None
            and not inspection.configuration_eligible
        }
        return [
            _binding(source)
            for source in config.sources
            if source.supply_channel == "hub"
            and bool(source.models)
            and source.id not in ineligible_source_ids
        ]

    @staticmethod
    def _clone_config(config: ModelHubConfig) -> ModelHubConfig:
        return ModelHubConfig.from_payload(config.to_payload())

    @staticmethod
    def _persisted_credential(
        config: ModelHubConfig,
        source_id: str,
        credential_ref: str | None,
    ) -> bool:
        if credential_ref is None:
            return False
        return any(
            source.id == source_id and source.credential_ref == credential_ref
            for source in config.sources
        )

    def _save_config(self, config: ModelHubConfig) -> ModelHubConfig:
        canonical = ModelHubConfig.from_payload(config.to_payload())
        self.store.save(canonical)
        return canonical

    async def _sync_sources(self, config: ModelHubConfig, *, force_empty: bool = False) -> None:
        bindings = self._bindings(config)
        has_hub_sources = any(
            source.supply_channel == "hub" for source in config.sources
        )
        if not bindings and not force_empty and not has_hub_sources:
            self._engine_preparation_failed = False
            return
        await self._engine_call(self.adapter.sync_sources(bindings))
        self._engine_preparation_failed = False

    async def _commit_synced(self, previous: ModelHubConfig, updated: ModelHubConfig) -> None:
        """Persist the authoritative config before updating its engine projection."""

        self._engine_synced = False
        updated = ModelHubConfig.from_payload(updated.to_payload())
        previous_bindings = self._bindings(previous)
        updated_bindings = self._bindings(updated)
        self._save_config(updated)
        try:
            await self._sync_sources(updated, force_empty=bool(previous_bindings))
        except asyncio.CancelledError:
            # The config write is the persistence boundary. A cancelled sync is
            # reconciled by the next demand and must not roll back a saved ref.
            self._engine_synced = False
            raise
        except Exception:
            self._save_config(previous)
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
            self._engine_synced = True
            active_credentials = {
                source.id: source.credential_ref for source in config.sources
            }
            for pending in pending_revocations:
                if active_credentials.get(pending.source_id) == pending.credential_ref:
                    try:
                        self.revocations.remove(
                            pending.source_id,
                            pending.credential_ref,
                        )
                    except OSError:
                        pass
                    continue
                if pending.operation == "cleanup_orphaned_oauth_material":
                    try:
                        cleaned = (
                            await self.adapter.cleanup_orphaned_oauth_material(
                                pending.credential_ref
                            )
                        )
                    except Exception:
                        continue
                    if not cleaned:
                        continue
                else:
                    try:
                        await self.adapter.revoke_credential(
                            pending.credential_ref
                        )
                    except Exception as error:
                        if not self._credential_was_already_revoked(error):
                            continue
                try:
                    self.revocations.remove(
                        pending.source_id,
                        pending.credential_ref,
                    )
                except OSError:
                    pass

    async def _prepare_engine_for_demand(self, *, already_synced: bool = False) -> None:
        try:
            if already_synced:
                self._engine_preparation_failed = False
                return
            await self._ensure_engine_synced()
        except Exception:
            self._engine_preparation_failed = True
            raise
        self._engine_preparation_failed = False

    def _runtime_status_after_demand(self, status: EngineStatus) -> EngineStatus:
        if self._engine_preparation_failed and status.health in {
            EngineHealth.NOT_INSTALLED,
            EngineHealth.NOT_STARTED,
        }:
            return replace(status, health=EngineHealth.DOWN)
        return status

    @staticmethod
    def _credential_was_already_revoked(error: Exception) -> bool:
        # The frozen adapter surface has no typed not-found result. Match the
        # concrete runtime's closed error here so replay remains idempotent.
        from vibe.model_hub_runtime.state import EngineStateError

        return (
            isinstance(error, EngineStateError)
            and str(error) == "credential is unavailable"
        )

    def _oauth_adapter(self, channel: OAuthChannel) -> OAuthAdapter:
        if channel == "hub":
            return self.adapter
        return self.native_oauth_adapter

    def _oauth_channel(self, flow_id: str) -> OAuthChannel:
        return self._oauth_binding(flow_id).channel

    @staticmethod
    def _is_hub_unsuccessful_terminal(
        binding: OAuthFlowBinding,
        flow: OAuthFlowState,
    ) -> bool:
        return (
            binding.channel == "hub"
            and flow.state in {"failed", "cancelled"}
        )

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

    @staticmethod
    def _observation_protocol_order(payload: Mapping[str, Any]) -> tuple[str, ...]:
        requested = payload.get("protocol_order")
        if requested is None:
            return SOURCE_PROTOCOLS
        if (
            not isinstance(requested, list)
            or not all(isinstance(item, str) for item in requested)
        ):
            raise ModelHubError("discovery_failed")
        requested_order = tuple(requested)
        if len(set(requested_order)) != len(SOURCE_PROTOCOLS) or set(requested_order) != set(SOURCE_PROTOCOLS):
            raise ModelHubError("discovery_failed")
        return requested_order

    @staticmethod
    def _observation_payload(observation: SourceObservation) -> dict:
        return {
            "contract_version": CONTRACT_VERSION,
            "outcome": observation.outcome.value,
            "reachable": observation.reachable,
            "authenticated": (
                "authenticated"
                if observation.authenticated is True
                else "rejected"
                if observation.authenticated is False
                else "unknown"
            ),
            "protocol": observation.protocol,
            "discovery": observation.discovery.value,
            "models": list(observation.model_ids),
        }

    @staticmethod
    def _validate_observation(observation: SourceObservation) -> SourceObservation:
        if not isinstance(observation, SourceObservation):
            raise ModelHubError("discovery_failed", status=502)
        if not isinstance(observation.outcome, ObservationOutcome):
            raise ModelHubError("discovery_failed", status=502)
        if not isinstance(observation.discovery, ObservationDiscovery):
            raise ModelHubError("discovery_failed", status=502)
        if observation.reachable not in {True, False, None}:
            raise ModelHubError("discovery_failed", status=502)
        if observation.authenticated not in {True, False, None}:
            raise ModelHubError("discovery_failed", status=502)
        if observation.protocol is not None and observation.protocol not in SOURCE_PROTOCOLS:
            raise ModelHubError("discovery_failed", status=502)
        if any(
            not isinstance(model_id, str)
            or not model_id
            or contains_credential_material(model_id)
            for model_id in observation.model_ids
        ) or len(set(observation.model_ids)) != len(observation.model_ids):
            raise ModelHubError("discovery_failed", status=502)
        expected = {
            ObservationOutcome.OBSERVED: (True, True),
            ObservationOutcome.AMBIGUOUS: (True, True),
            ObservationOutcome.UNREACHABLE: (False, None),
            ObservationOutcome.AUTHENTICATION_FAILED: (True, False),
            ObservationOutcome.ADAPTER_ERROR: (None, None),
            ObservationOutcome.TIMEOUT: (None, None),
        }[observation.outcome]
        if observation.reachable is not expected[0] or observation.authenticated is not expected[1]:
            raise ModelHubError("discovery_failed", status=502)
        if observation.outcome is ObservationOutcome.OBSERVED and observation.protocol is None:
            raise ModelHubError("discovery_failed", status=502)
        if observation.outcome is not ObservationOutcome.OBSERVED and observation.outcome is not ObservationOutcome.AUTHENTICATION_FAILED and observation.protocol is not None:
            raise ModelHubError("discovery_failed", status=502)
        if observation.outcome in {
            ObservationOutcome.UNREACHABLE,
            ObservationOutcome.AUTHENTICATION_FAILED,
            ObservationOutcome.ADAPTER_ERROR,
            ObservationOutcome.TIMEOUT,
        } and observation.discovery is not ObservationDiscovery.NOT_ATTEMPTED:
            raise ModelHubError("discovery_failed", status=502)
        if observation.discovery is ObservationDiscovery.FAILED and observation.model_ids:
            raise ModelHubError("discovery_failed", status=502)
        return observation

    async def _observe_source_payload(self, payload: Mapping[str, Any]) -> SourceObservation:
        if set(payload) - {"vendor", "base_url", "key", "protocol_order"}:
            raise ModelHubError("discovery_failed")
        vendor = payload.get("vendor")
        try:
            vendor = normalize_model_hub_vendor_id(vendor)
        except ValueError:
            raise ModelHubError("discovery_failed")
        base_url = _validated_base_url(payload.get("base_url"))
        key = payload.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ModelHubError("discovery_failed")
        protocol_order = self._observation_protocol_order(payload)
        transient_ref = await _provision_transient_credential_with_cancellation_ownership(
            self,
            vendor,
            key.strip(),
            base_url,
        )
        try:
            try:
                observation = await self.adapter.observe_source(
                    vendor,
                    base_url,
                    transient_ref,
                    protocol_order,
                )
            except asyncio.TimeoutError:
                observation = SourceObservation(
                    outcome=ObservationOutcome.TIMEOUT,
                    reachable=None,
                    authenticated=None,
                    protocol=None,
                    discovery=ObservationDiscovery.NOT_ATTEMPTED,
                    model_ids=(),
                )
            except EngineUnavailableError:
                raise ModelHubError("engine_down", status=503) from None
            except asyncio.CancelledError:
                raise
            except Exception:
                observation = SourceObservation(
                    outcome=ObservationOutcome.ADAPTER_ERROR,
                    reachable=None,
                    authenticated=None,
                    protocol=None,
                    discovery=ObservationDiscovery.NOT_ATTEMPTED,
                    model_ids=(),
                )
            return self._validate_observation(observation)
        finally:
            await _rollback_credential_before_settling(
                self,
                "observation",
                transient_ref,
            )

    async def observe_source(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ModelHubError("discovery_failed")
        observation = await self._observe_source_payload(payload)
        return {"observation": self._observation_payload(observation)}

    def _record_event(self, **event_fields: Any) -> None:
        try:
            source_ids = {
                source.id
                for source in self.store.load().sources
            }
            for field in ("from_source", "to_source"):
                source_id = event_fields.get(field)
                if source_id is not None and source_id not in source_ids:
                    raise ValueError(
                        f"Resolution event names unknown {field}"
                    )
            self.events.append(build_resolution_event(**event_fields))
        except Exception:
            # Resolution telemetry is best effort and must never affect routing.
            logger.warning("Failed to persist Model Hub resolution event")

    async def _rollback_credential(
        self,
        source_id: str,
        credential_ref: str,
    ) -> bool:
        journaled = False
        try:
            self.revocations.add(source_id, credential_ref)
        except OSError:
            pass
        else:
            journaled = True
        try:
            await self.adapter.revoke_credential(credential_ref)
        except Exception:
            return journaled
        if not journaled:
            return True
        try:
            self.revocations.remove(source_id, credential_ref)
        except OSError:
            # A replayed revoke is safer than losing the only durable ref.
            pass
        return True

    async def _rollback_replacement(
        self,
        source_id: str,
        replacement_ref: str,
        old_credential_ref: str | None,
        *,
        old_revocation_recorded: bool,
    ) -> None:
        if old_revocation_recorded and old_credential_ref:
            try:
                self.revocations.remove(source_id, old_credential_ref)
            except OSError:
                pass
        if replacement_ref != old_credential_ref:
            await self._rollback_credential(source_id, replacement_ref)

    async def _cleanup_orphaned_hub_material(
        self,
        source_id: str,
        credential_ref: str,
    ) -> None:
        journaled = False
        try:
            self.revocations.add(
                source_id,
                credential_ref,
                operation="cleanup_orphaned_oauth_material",
            )
        except OSError:
            pass
        else:
            journaled = True
        try:
            cleaned = await self.adapter.cleanup_orphaned_oauth_material(
                credential_ref
            )
        except Exception:
            cleaned = False
        if not cleaned:
            if not journaled:
                raise ModelHubError("engine_down", status=503)
            return
        if journaled:
            try:
                self.revocations.remove(source_id, credential_ref)
            except OSError:
                pass

    def _mark_same_handle_reauth_needs_action(self, source_id: str) -> None:
        # The engine may replace OAuth material behind the same opaque ref.
        # Without an old snapshot, fail closed instead of restoring stale supply.
        config = self._clone_config(self.store.load())
        source = self._source(config, source_id)
        source.models = [
            model for model in source.models if model.provenance == "manual"
        ]
        source.state = ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.oauth_expired",
        )
        self._save_config(config)

    async def _discard_unbound_hub_flow(self, flow: OAuthFlowState) -> None:
        if flow.credential_ref:
            await self._rollback_credential(flow.source_id, flow.credential_ref)
            return
        try:
            await self.adapter.cancel_oauth(flow.flow_id)
        except Exception:
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
        *,
        allow_empty: bool = False,
    ) -> None:
        if (not allow_empty and not discovered and not manual_models) or any(
            not isinstance(model_id, str)
            or not model_id
            or contains_credential_material(model_id)
            for model_id in discovered
        ) or len(set(discovered)) != len(discovered):
            raise ModelHubError("discovery_failed", status=502)
        discovered_at = self.now().isoformat()
        manual_model_ids = {model.id for model in manual_models}
        existing_by_id = {model.id: model for model in source.models}
        discovered_models = []
        for model_id in discovered:
            if model_id in manual_model_ids:
                continue
            existing = existing_by_id.get(model_id)
            discovered_models.append(
                ModelHubModelConfig(
                    id=model_id,
                    provenance="discovered",
                    reasoning_efforts=list(existing.reasoning_efforts) if existing else [],
                    display_name=existing.display_name if existing else None,
                    discovered_at=discovered_at,
                )
            )
        source.models = discovered_models + manual_models
        source.last_discovered_at = discovered_at

    async def _commit_new_source_locked(
        self,
        source: ModelHubSourceConfig,
        *,
        previous: Optional[ModelHubConfig] = None,
    ) -> None:
        previous = previous or self.store.load()
        config = self._clone_config(previous)
        if any(item.id == source.id for item in config.sources):
            raise ModelHubError("migration_item_conflict", status=409)
        config.sources.append(source)
        self._apply_source_placement(config, source)
        await self._commit_synced(previous, config)

    def _apply_source_placement(
        self,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
    ) -> None:
        """Apply matching-v1 and placement-v1 to one newly added Source."""

        for backend in MODEL_HUB_BACKENDS:
            agent = config.agents[backend]
            if self._eligible_for_agent(source, backend) and source.id not in agent.sources.order:
                agent.sources.order.append(source.id)
            menu_ids = self._agent_menu_model_ids(agent)
            for menu_model in menu_ids:
                route = agent.routes.setdefault(menu_model, ModelHubRouteConfig())
                if not self._eligible_for_agent(source, backend):
                    continue
                matched_model = _matching_v1_model_id(
                    backend=cast(BackendName, backend),
                    requested_model=menu_model,
                    source=source,
                    checked_models=menu_ids,
                )
                if matched_model is None or any(hop.source_id == source.id for hop in route.hops):
                    continue
                route.hops = (*route.hops, ModelHubRouteHopConfig(source.id, matched_model))

    @staticmethod
    def _agent_menu_model_ids(agent: ModelHubAgentSupplyConfig) -> tuple[str, ...]:
        if agent.menu_kind == "fixed":
            return tuple(_builtin_model_ids(agent.backend))
        return tuple(agent.menu.checked if agent.menu else ())

    def _added_to(self, source_id: str) -> list[dict]:
        config = self.store.load()
        result: list[dict] = []
        for backend in MODEL_HUB_BACKENDS:
            routes = config.agents[backend].routes
            for menu_model, route in routes.items():
                for position, hop in enumerate(route.hops, start=1):
                    if hop.source_id == source_id:
                        result.append(
                            {
                                "backend": backend,
                                "menu_model": menu_model,
                                "source_id": source_id,
                                "model_id": hop.model_id,
                                "position": position,
                            }
                        )
        return result

    def _adopted_by(self, source_id: str) -> list[dict]:
        config = self.store.load()
        return [
            {"backend": backend, "menu_model": menu_model}
            for backend in MODEL_HUB_BACKENDS
            for menu_model, route in config.agents[backend].routes.items()
            if any(hop.source_id == source_id for hop in route.hops)
        ]

    def _source_creation_result(self, source: dict) -> dict:
        return {
            "source": source,
            "added_to": self._added_to(source["id"]),
            "adopted_by": self._adopted_by(source["id"]),
        }

    async def _create_oauth_source(
        self,
        source: ModelHubSourceConfig,
        manual_models: list[ModelHubModelConfig],
        *,
        oauth_ref: str,
        channel: Literal["native_cli", "hub"],
        vendor: str,
        completed_flow: Optional[OAuthFlowState] = None,
        idempotent: bool = False,
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
                if binding.completed:
                    existing = self._completed_oauth_source(binding)
                    if idempotent and existing is not None:
                        return existing.to_payload()
                    raise ModelHubError("flow_not_found", status=404)
                flow = completed_flow or await self._oauth_status(oauth_ref, binding.channel)
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
                existing = next((item for item in previous.sources if item.id == source.id), None)
                if idempotent and existing is not None and self._source_matches_binding(existing, binding):
                    try:
                        self.oauth_flows.complete(oauth_ref)
                    except (KeyError, OSError):
                        pass
                    return existing.to_payload()
                if existing is not None:
                    raise ModelHubError("migration_item_conflict", status=409)
                if channel == "hub":
                    source.credential_ref = cast(str, flow.credential_ref)
                    rollback_credential_ref = source.credential_ref
                else:
                    try:
                        source_status = self.native_oauth_adapter.completed_source_status(oauth_ref)
                    except KeyError:
                        raise ModelHubError("flow_not_found", status=404) from None
                    except NativeOAuthUnavailableError:
                        raise ModelHubError("engine_down", status=503) from None
                    except Exception:
                        raise ModelHubError("engine_down", status=503) from None
                    source.account_label = source_status.account_label
                    source.state = (
                        ModelHubSourceStateConfig(status="standby")
                        if source_status.signed_in
                        else ModelHubSourceStateConfig(
                            status="needs_action",
                            detail_key="models.source.needs_action.oauth_expired",
                        )
                    )

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
                    previous=previous,
                )
                persisted = True
                try:
                    self.oauth_flows.complete(oauth_ref)
                except (KeyError, OSError):
                    pass
                return source.to_payload()
            except asyncio.CancelledError:
                persisted = self._persisted_credential(
                    self.store.load(),
                    source.id,
                    rollback_credential_ref,
                )
                if rollback_credential_ref is not None and not persisted:
                    await _rollback_credential_before_settling(
                        self,
                        source.id,
                        rollback_credential_ref,
                    )
                    try:
                        self.oauth_flows.forget(oauth_ref)
                    except OSError:
                        pass
                raise
            except Exception:
                if rollback_credential_ref is not None and not persisted:
                    await self._rollback_credential(source.id, rollback_credential_ref)
                    try:
                        self.oauth_flows.forget(oauth_ref)
                    except OSError:
                        pass
                raise

    @staticmethod
    def _source_matches_binding(
        source: ModelHubSourceConfig,
        binding: OAuthFlowBinding,
    ) -> bool:
        return (
            source.kind == "subscription"
            and source.supply_channel == binding.channel
            and source.vendor == binding.vendor
        )

    def _completed_oauth_source(
        self,
        binding: OAuthFlowBinding,
    ) -> ModelHubSourceConfig | None:
        if binding.source_id is None:
            return None
        source = next(
            (item for item in self.store.load().sources if item.id == binding.source_id),
            None,
        )
        if source is None or not self._source_matches_binding(source, binding):
            return None
        return source

    def _completed_oauth_flow(
        self,
        flow_id: str,
        binding: OAuthFlowBinding,
    ) -> OAuthFlowState | None:
        source = self._completed_oauth_source(binding)
        if binding.intent == "reauth" and not binding.completed:
            return None
        if source is None:
            if binding.completed:
                try:
                    self.oauth_flows.forget(flow_id)
                except OSError:
                    pass
                raise ModelHubError("flow_not_found", status=404)
            return None
        if not binding.completed:
            try:
                self.oauth_flows.complete(flow_id)
            except (KeyError, OSError):
                pass
        failed = binding.intent == "create" and source.state.status == "error"
        return OAuthFlowState(
            flow_id=flow_id,
            source_id=source.id,
            vendor=source.vendor,
            state="failed" if failed else "success",
            auth_url=None,
            device_code=None,
            expects="none",
            instructions_key=None,
            error_key=(
                source.state.detail_key or "settings.models.oauth.error.generic"
                if failed
                else None
            ),
            expires_at_iso=None,
            credential_ref=(
                source.credential_ref
                if binding.channel == "hub"
                else None
            ),
            channel=binding.channel,
            retained_material_disposition=(
                RetainedMaterialDisposition.FLOW_SOURCE_REF
                if binding.channel == "hub"
                else RetainedMaterialDisposition.NONE
            ),
            retained_credential_ref=(
                source.credential_ref
                if binding.channel == "hub"
                else None
            ),
        )

    def _completed_reauth_result(
        self,
        binding: OAuthFlowBinding,
    ) -> dict | None:
        if not binding.completed:
            return None
        source = self._completed_oauth_source(binding)
        if source is None:
            raise ModelHubError("flow_not_found", status=404)
        return {
            "source": source.to_payload(),
            "recovered": binding.recovered is True,
            "interrupted_pairs": list(binding.interrupted_pairs),
        }

    async def _mark_native_reauth_unavailable(
        self,
        source_id: str,
        *,
        status: Literal["needs_action", "error"],
    ) -> list[dict]:
        async with self._mutation_lock:
            config = self.store.load()
            source = self._source(config, source_id)
            source.models = [
                model for model in source.models if model.provenance == "manual"
            ]
            source.account_label = None
            source.state = ModelHubSourceStateConfig(
                status=status,
                detail_key=(
                    "models.source.needs_action.oauth_expired"
                    if status == "needs_action"
                    else "models.source.error.unclassified"
                ),
            )
            self._save_config(config)
            return self._would_interrupt(config)

    async def _materialize_reauth(
        self,
        flow_id: str,
        binding: OAuthFlowBinding,
        flow: OAuthFlowState,
    ) -> dict:
        if binding.source_id is None or binding.vendor is None:
            raise ModelHubError("flow_not_found", status=404)

        if binding.channel == "native_cli":
            try:
                source_status = self.native_oauth_adapter.completed_source_status(
                    flow_id
                )
            except KeyError:
                raise ModelHubError("flow_not_found", status=404) from None
            except Exception:
                interrupted_pairs = await self._mark_native_reauth_unavailable(
                    binding.source_id,
                    status="error",
                )
                raise ModelHubError(
                    "discovery_failed",
                    status=502,
                    data={"interrupted_pairs": interrupted_pairs},
                ) from None

            async with self._mutation_lock:
                binding = self._oauth_binding(flow_id)
                completed = self._completed_reauth_result(binding)
                if completed is not None:
                    return completed
                config = self.store.load()
                source = self._source(config, binding.source_id)
                if not self._source_matches_binding(source, binding):
                    raise ModelHubError("flow_not_found", status=404)
                source.account_label = source_status.account_label
                source.state = (
                    ModelHubSourceStateConfig(status="standby")
                    if source_status.signed_in
                    else ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key="models.source.needs_action.oauth_expired",
                    )
                )
                manual = [
                    model for model in source.models if model.provenance == "manual"
                ]
                discovered = list(_native_model_ids(binding.vendor))
                if not discovered:
                    source.models = []
                    source.state = ModelHubSourceStateConfig(
                        status="error",
                        detail_key="models.source.error.unclassified",
                    )
                    self._save_config(config)
                    interrupted_pairs = self._would_interrupt(config)
                    raise ModelHubError(
                        "discovery_failed",
                        status=502,
                        data={"interrupted_pairs": interrupted_pairs},
                    )
                self._apply_discovered_models(source, manual, discovered)
                self._save_config(config)
                interrupted_pairs = self._would_interrupt(config)
                self._complete_reauth_flow(
                    flow_id,
                    binding,
                    interrupted_pairs,
                )
                return {
                    "source": source.to_payload(),
                    "recovered": binding.recovered is True,
                    "interrupted_pairs": interrupted_pairs,
                }

        if not flow.credential_ref:
            raise ModelHubError("flow_not_found", status=404)
        replacement_ref = flow.credential_ref
        async with self._mutation_lock:
            binding = self._oauth_binding(flow_id)
            completed = self._completed_reauth_result(binding)
            if completed is not None:
                return completed
            source: ModelHubSourceConfig | None = None
            old_credential_ref: str | None = None
            committed = False
            old_revocation_recorded = False
            try:
                previous = self.store.load()
                config = self._clone_config(previous)
                source = self._source(config, binding.source_id)
                old_credential_ref = source.credential_ref
                if not self._source_matches_binding(source, binding):
                    raise ModelHubError("flow_not_found", status=404)
                manual = [
                    model
                    for model in source.models
                    if model.provenance == "manual"
                ]
                source.credential_ref = replacement_ref
                discovered = await self._discover(source)
                self._apply_discovered_models(source, manual, discovered)
                source.state = ModelHubSourceStateConfig(status="standby")
                interrupted_pairs = self._would_interrupt(config)
                if (
                    old_credential_ref
                    and old_credential_ref != replacement_ref
                ):
                    self.revocations.add(source.id, old_credential_ref)
                    old_revocation_recorded = True
                await self._commit_synced(previous, config)
                committed = True
                self._complete_reauth_flow(
                    flow_id,
                    binding,
                    interrupted_pairs,
                )
            except asyncio.CancelledError:
                committed = self._persisted_credential(
                    self.store.load(),
                    binding.source_id,
                    replacement_ref,
                )
                if not committed:
                    try:
                        if source is None:
                            await _rollback_credential_before_settling(
                                self,
                                binding.source_id,
                                replacement_ref,
                            )
                        elif replacement_ref == old_credential_ref:
                            self._mark_same_handle_reauth_needs_action(source.id)
                        else:
                            await _rollback_replacement_before_settling(
                                self,
                                source.id,
                                replacement_ref,
                                old_credential_ref,
                                old_revocation_recorded=old_revocation_recorded,
                            )
                    finally:
                        try:
                            self.oauth_flows.forget(flow_id)
                        except OSError:
                            pass
                raise
            except Exception:
                if not committed:
                    try:
                        if source is None:
                            await self._rollback_credential(
                                binding.source_id,
                                replacement_ref,
                            )
                        elif replacement_ref == old_credential_ref:
                            self._mark_same_handle_reauth_needs_action(source.id)
                        else:
                            await self._rollback_replacement(
                                source.id,
                                replacement_ref,
                                old_credential_ref,
                                old_revocation_recorded=old_revocation_recorded,
                            )
                    finally:
                        try:
                            self.oauth_flows.forget(flow_id)
                        except OSError:
                            pass
                raise

            if old_credential_ref and old_credential_ref != replacement_ref:
                try:
                    await self.adapter.revoke_credential(old_credential_ref)
                except Exception:
                    pass
                else:
                    try:
                        self.revocations.remove(
                            binding.source_id,
                            old_credential_ref,
                        )
                    except OSError:
                        pass
            return {
                "source": source.to_payload(),
                "recovered": binding.recovered is True,
                "interrupted_pairs": interrupted_pairs,
            }

    def _complete_reauth_flow(
        self,
        flow_id: str,
        binding: OAuthFlowBinding,
        interrupted_pairs: list[dict],
    ) -> None:
        try:
            self.oauth_flows.complete(
                flow_id,
                recovered=binding.recovered is True,
                interrupted_pairs=interrupted_pairs,
            )
        except KeyError:
            try:
                self.oauth_flows.remember(
                    flow_id,
                    binding.channel,
                    binding.source_id,
                    binding.vendor,
                    experimental_consent=binding.experimental_consent,
                    intent="reauth",
                    recovered=binding.recovered,
                )
                self.oauth_flows.complete(
                    flow_id,
                    recovered=binding.recovered is True,
                    interrupted_pairs=interrupted_pairs,
                )
            except (KeyError, OSError):
                pass
        except OSError:
            pass

    def _fail_closed_hub_reauth(
        self,
        binding: OAuthFlowBinding,
        *,
        config: ModelHubConfig | None = None,
    ) -> ModelHubConfig:
        config = config or self.store.load()
        if binding.source_id is None:
            raise ModelHubError("flow_not_found", status=404)
        source = self._source(config, binding.source_id)
        if not self._source_matches_binding(source, binding):
            raise ModelHubError("flow_not_found", status=404)
        source.models = [
            model
            for model in source.models
            if model.provenance == "manual"
        ]
        source.state = ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.oauth_expired",
        )
        self._save_config(config)
        return config

    async def _materialize_failed_hub_reauth(
        self,
        binding: OAuthFlowBinding,
        flow: OAuthFlowState,
        *,
        config: ModelHubConfig | None = None,
    ) -> ModelHubConfig | None:
        disposition = flow.retained_material_disposition
        if disposition in {
            RetainedMaterialDisposition.NONE,
            RetainedMaterialDisposition.FOREIGN_SOURCE_REF,
        }:
            return config
        if disposition in {
            RetainedMaterialDisposition.FLOW_SOURCE_REF,
            RetainedMaterialDisposition.UNKNOWN,
        }:
            return self._fail_closed_hub_reauth(
                binding,
                config=config,
            )
        if disposition == RetainedMaterialDisposition.ORPHAN_REF:
            if (
                binding.source_id is None
                or flow.retained_credential_ref is None
            ):
                raise ModelHubError("engine_down", status=503)
            await self._cleanup_orphaned_hub_material(
                binding.source_id,
                flow.retained_credential_ref,
            )
            return config
        raise ModelHubError("engine_down", status=503)

    async def _materialize_failed_hub_flow(
        self,
        binding: OAuthFlowBinding,
        flow: OAuthFlowState,
        *,
        config: ModelHubConfig | None = None,
    ) -> ModelHubConfig | None:
        if binding.intent == "reauth":
            return await self._materialize_failed_hub_reauth(
                binding,
                flow,
                config=config,
            )
        if (
            flow.retained_material_disposition
            == RetainedMaterialDisposition.FLOW_SOURCE_REF
        ):
            if (
                binding.source_id is None
                or flow.retained_credential_ref is None
            ):
                raise ModelHubError("engine_down", status=503)
            cleanup_secured = await self._rollback_credential(
                binding.source_id,
                flow.retained_credential_ref,
            )
            if not cleanup_secured:
                raise ModelHubError("engine_down", status=503)
            return config
        if (
            flow.retained_material_disposition
            == RetainedMaterialDisposition.ORPHAN_REF
        ):
            if (
                binding.source_id is None
                or flow.retained_credential_ref is None
            ):
                raise ModelHubError("engine_down", status=503)
            await self._cleanup_orphaned_hub_material(
                binding.source_id,
                flow.retained_credential_ref,
            )
        return config

    async def _materialize_completed_oauth(
        self,
        flow_id: str,
        binding: OAuthFlowBinding,
        flow: OAuthFlowState,
    ) -> tuple[OAuthFlowState, dict | None]:
        if flow.state != "success":
            if (
                self._is_hub_unsuccessful_terminal(binding, flow)
                and binding.source_id is not None
            ):
                async with self._mutation_lock:
                    await self._materialize_failed_hub_flow(
                        binding,
                        flow,
                    )
            return flow, None
        if binding.source_id is None or binding.vendor is None:
            raise ModelHubError("flow_not_found", status=404)
        if binding.intent == "reauth":
            repair_result = await self._materialize_reauth(
                flow_id,
                binding,
                flow,
            )
            return flow, repair_result
        source = ModelHubSourceConfig(
            id=binding.source_id,
            created_at=self.now().isoformat(),
            kind="subscription",
            vendor=binding.vendor,
            display_name=binding.vendor,
            protocol=_default_protocol(binding.vendor),
            base_url=None,
            supply_channel=binding.channel,
            billing="monthly",
            state=ModelHubSourceStateConfig(status="standby"),
            usage=ModelHubSourceUsageConfig(),
            models=[],
        )
        await self._create_oauth_source(
            source,
            [],
            oauth_ref=flow_id,
            channel=binding.channel,
            vendor=binding.vendor,
            completed_flow=flow,
            idempotent=True,
        )
        completed = self._completed_oauth_flow(flow_id, binding)
        if completed is None:
            raise ModelHubError("flow_not_found", status=404)
        return completed, None

    def list_sources(self) -> list[dict]:
        config = self.store.load()
        return [source.to_payload() for source in config.sources]

    async def create_source(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ModelHubError("discovery_failed")
        if set(payload) - {
            "kind",
            "vendor",
            "display_name",
            "base_url",
            "supply_channel",
            "billing",
            "models",
            "key",
            "oauth_flow_ref",
            "protocol_order",
        }:
            raise ModelHubError("discovery_failed")
        forbidden = {
            "id",
            "credential_ref",
            "account_label",
            "masked_credential",
            "experimental_consent_at",
            "state",
            "usage",
            "created_at",
            "last_discovered_at",
        } & set(payload)
        if forbidden or "protocol" in payload:
            raise ModelHubError("discovery_failed")
        kind = payload.get("kind")
        vendor = payload.get("vendor")
        display_name = payload.get("display_name") or vendor
        try:
            vendor = normalize_model_hub_vendor_id(vendor)
        except ValueError:
            raise ModelHubError("discovery_failed") from None
        if kind not in {"subscription", "api_key"}:
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
            source = ModelHubSourceConfig(
                id=_source_id(),
                kind=kind,
                vendor=vendor,
                display_name=display_name,
                protocol=_default_protocol(vendor),
                base_url=base_url,
                supply_channel=channel,
                billing=billing,
                state=ModelHubSourceStateConfig(status="standby"),
                usage=ModelHubSourceUsageConfig(),
                models=manual_models,
                created_at=self.now().isoformat(),
            )
            return self._source_creation_result(
                await self._create_oauth_source(
                    source,
                    manual_models,
                    oauth_ref=oauth_ref,
                    channel=cast(Literal["native_cli", "hub"], channel),
                    vendor=vendor,
                )
            )
        if kind == "subscription":
            raise ModelHubError("flow_not_found", status=404)

        observation = await self._observe_source_payload(
            {
                "vendor": vendor,
                "base_url": base_url,
                "key": credential_value,
                "protocol_order": payload.get("protocol_order"),
            }
        )
        if (
            observation.outcome is not ObservationOutcome.OBSERVED
            or observation.protocol is None
        ):
            raise ModelHubError(
                "discovery_failed",
                status=422,
                data={"observation": self._observation_payload(observation)},
            )
        source = ModelHubSourceConfig(
            id=_source_id(),
            kind=kind,
            vendor=vendor,
            display_name=display_name,
            protocol=cast(Literal["anthropic", "openai_responses", "openai_chat"], observation.protocol),
            base_url=base_url,
            supply_channel=channel,
            billing=billing,
            state=ModelHubSourceStateConfig(status="standby"),
            usage=ModelHubSourceUsageConfig(),
            models=manual_models,
            created_at=self.now().isoformat(),
            credential_ref="cred_preflight",
        )
        if observation.discovery is ObservationDiscovery.SUCCEEDED:
            self._apply_discovered_models(
                source,
                manual_models,
                list(observation.model_ids),
                allow_empty=True,
            )
        elif observation.discovery is ObservationDiscovery.FAILED:
            source.state = ModelHubSourceStateConfig(
                status="error",
                detail_key="models.source.error.unclassified",
            )

        # AC-29 requires the canonical Source validator to run before any
        # permanent credential is provisioned. The placeholder is never saved.
        try:
            source = ModelHubSourceConfig.from_payload(source.to_payload())
        except (TypeError, ValueError):
            raise ModelHubError("discovery_failed") from None

        rollback_credential_ref = await self._engine_call(
            self.adapter.provision_credential(
                vendor,
                source.protocol,
                cast(str, credential_value),
                source.base_url,
            )
        )
        source.credential_ref = rollback_credential_ref
        source.masked_credential = _mask_credential(cast(str, credential_value))
        source = ModelHubSourceConfig.from_payload(source.to_payload())
        persisted = False
        try:
            async with self._mutation_lock:
                await self._commit_new_source_locked(source)
                persisted = True
            return self._source_creation_result(source.to_payload())
        except asyncio.CancelledError:
            persisted = self._persisted_credential(
                self.store.load(),
                source.id,
                rollback_credential_ref,
            )
            if not persisted:
                await _rollback_credential_before_settling(
                    self,
                    source.id,
                    rollback_credential_ref,
                )
            raise
        except Exception:
            if not persisted:
                await self._rollback_credential(source.id, rollback_credential_ref)
            raise

    async def replace_credential(self, source_id: str, payload: object) -> dict:
        if (
            not isinstance(payload, dict)
            or set(payload) - {"key", "force"}
            or set(payload) < {"key"}
            or not isinstance(payload.get("key"), str)
            or not str(payload["key"]).strip()
            or (
                "force" in payload
                and not isinstance(payload.get("force"), bool)
            )
        ):
            raise ModelHubError("discovery_failed")

        key = str(payload["key"]).strip()
        force = payload.get("force") is True
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            if (
                source.kind != "api_key"
                or source.supply_channel != "hub"
                or not source.credential_ref
            ):
                raise ModelHubError("discovery_failed")

            old_credential_ref = source.credential_ref
            replacement_ref = await self._engine_call(
                self.adapter.provision_credential(
                    source.vendor,
                    source.protocol,
                    key,
                    source.base_url,
                )
            )
            committed = False
            old_revocation_recorded = False
            try:
                source.credential_ref = replacement_ref
                source.masked_credential = _mask_credential(key)
                manual = [
                    model for model in source.models if model.provenance == "manual"
                ]
                discovered = await self._discover(source)
                self._apply_discovered_models(source, manual, discovered)
                source.state = ModelHubSourceStateConfig(status="standby")

                removed_hops, interrupted = self._guard_inventory_mutation(
                    previous,
                    config,
                    source.id,
                    force=force,
                )

                if old_credential_ref != replacement_ref:
                    self.revocations.add(source.id, old_credential_ref)
                    old_revocation_recorded = True
                await self._commit_synced(previous, config)
                committed = True
            except asyncio.CancelledError:
                committed = self._persisted_credential(
                    self.store.load(),
                    source.id,
                    replacement_ref,
                )
                if not committed:
                    await _rollback_replacement_before_settling(
                        self,
                        source.id,
                        replacement_ref,
                        old_credential_ref,
                        old_revocation_recorded=old_revocation_recorded,
                    )
                raise
            except Exception:
                if not committed:
                    await self._rollback_replacement(
                        source.id,
                        replacement_ref,
                        old_credential_ref,
                        old_revocation_recorded=old_revocation_recorded,
                    )
                raise

            if old_credential_ref != replacement_ref:
                try:
                    await self.adapter.revoke_credential(old_credential_ref)
                except Exception:
                    pass
                else:
                    try:
                        self.revocations.remove(source.id, old_credential_ref)
                    except OSError:
                        pass
            return {
                "source": source.to_payload(),
                "removed_hops": removed_hops,
                "interrupted": interrupted,
            }

    async def patch_source(self, source_id: str, payload: dict) -> dict:
        if (
            not isinstance(payload, dict)
            or set(payload) - {"display_name", "base_url", "force"}
            or ("force" in payload and not isinstance(payload["force"], bool))
        ):
            raise ModelHubError("discovery_failed")
        base_url = _validated_base_url(payload.get("base_url")) if "base_url" in payload else None
        force = payload.get("force") is True
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
                removed_hops, interrupted = self._guard_inventory_mutation(
                    previous,
                    config,
                    source.id,
                    force=force,
                )
            if "base_url" in payload:
                await self._commit_synced(previous, config)
            else:
                self._save_config(config)
                removed_hops = []
                interrupted = []
            return {
                "source": source.to_payload(),
                "removed_hops": removed_hops,
                "interrupted": interrupted,
            }

    def _protected_menu_models(
        self,
        config: ModelHubConfig,
        backend: BackendName,
    ) -> dict[str, list[str]]:
        agent = config.agents[backend]
        if agent.mode != "hub":
            return {}

        protected: dict[str, list[str]] = {}

        def add(model_id: object, named_agent: str | None = None) -> None:
            normalized = str(model_id or "").strip()
            if not normalized:
                return
            names = protected.setdefault(normalized, [])
            if named_agent is not None and named_agent not in names:
                names.append(named_agent)

        if self.named_agents_override is not None:
            for name, pinned_model in self.named_agents_override(backend):
                add(pinned_model, name)
        if agent.menu_kind == "open" and agent.menu is not None:
            for identifier in agent.menu.checked:
                add(identifier)
        else:
            for model_id in _builtin_model_ids(backend):
                add(model_id)
        for model_id in agent.routes:
            add(model_id)
        return protected

    def _would_interrupt(
        self,
        config: ModelHubConfig,
        *,
        newly_empty_routes: frozenset[tuple[str, str]] = frozenset(),
    ) -> list[dict]:
        gaps: list[dict] = []
        for backend_name in MODEL_HUB_BACKENDS:
            backend = cast(BackendName, backend_name)
            unavailable_source_ids = self._unavailable_native_sources(config, backend)
            for model_id, agents in self._protected_menu_models(config, backend).items():
                route = config.agents[backend].routes.get(model_id)
                if (
                    (route is None or not route.hops)
                    and (backend, model_id) not in newly_empty_routes
                ):
                    continue
                resolution = resolve_model_hub_turn(
                    config,
                    backend,
                    model_id,
                    now=self.now(),
                    unavailable_source_ids=unavailable_source_ids,
                )
                if resolution.candidates:
                    continue
                gaps.append(
                    {
                        "backend": backend,
                        "model_id": model_id,
                        "agents": agents,
                    }
                )
        return gaps

    def _introduced_interruptions(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
        *,
        newly_empty_routes: frozenset[tuple[str, str]] = frozenset(),
    ) -> list[dict]:
        baseline = {
            (item["backend"], item["model_id"])
            for item in self._would_interrupt(previous)
        }
        return [
            item
            for item in self._would_interrupt(
                updated,
                newly_empty_routes=newly_empty_routes,
            )
            if (item["backend"], item["model_id"]) not in baseline
        ]

    def _invalidated_route_hops(
        self,
        config: ModelHubConfig,
        source_id: str,
    ) -> list[dict]:
        return [
            {
                "backend": backend,
                "menu_model": menu_model,
                "source_id": source_id,
                "model_id": hop.model_id,
            }
            for backend in MODEL_HUB_BACKENDS
            for menu_model, route in config.agents[backend].routes.items()
            for hop in route.hops
            if hop.source_id == source_id
            and inspect_exact_hop(
                config,
                cast(BackendName, backend),
                menu_model,
                hop,
                now=self.now(),
                unavailable_source_ids=self._unavailable_native_sources(
                    config,
                    cast(BackendName, backend),
                ),
            ).reason
            == "model_unsupported"
        ]

    @staticmethod
    def _prune_invalidated_route_hops(
        config: ModelHubConfig,
        invalidated_hops: list[dict],
    ) -> None:
        identities = {
            (
                item["backend"],
                item["menu_model"],
                item["source_id"],
                item["model_id"],
            )
            for item in invalidated_hops
        }
        for backend in MODEL_HUB_BACKENDS:
            for menu_model, route in config.agents[backend].routes.items():
                route.hops = tuple(
                    hop
                    for hop in route.hops
                    if (backend, menu_model, hop.source_id, hop.model_id)
                    not in identities
                )

    def _guard_inventory_mutation(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
        source_id: str,
        *,
        force: bool,
    ) -> tuple[list[dict], list[dict]]:
        would_remove_hops = self._invalidated_route_hops(updated, source_id)
        would_interrupt = self._introduced_interruptions(previous, updated)
        if (would_remove_hops or would_interrupt) and not force:
            raise ModelHubError(
                (
                    "source_model_in_route_chain"
                    if would_remove_hops
                    else "source_last_supplier"
                ),
                status=409,
                data={
                    "would_remove_hops": would_remove_hops,
                    "would_interrupt": would_interrupt,
                },
            )
        if would_remove_hops:
            self._prune_invalidated_route_hops(updated, would_remove_hops)
        return would_remove_hops, would_interrupt

    async def delete_source(self, source_id: str, *, force: bool = False) -> dict:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            removed_hops = [
                {"backend": backend, "menu_model": model_id, "source_id": source_id, "model_id": hop.model_id}
                for backend in MODEL_HUB_BACKENDS
                for model_id, route in config.agents[backend].routes.items()
                for hop in route.hops
                if hop.source_id == source_id
            ]
            config.sources = [item for item in config.sources if item.id != source_id]
            for agent in config.agents.values():
                agent.sources.order = [item for item in agent.sources.order if item != source_id]
                for model_id, route in list(agent.routes.items()):
                    route.hops = tuple(hop for hop in route.hops if hop.source_id != source_id)
                    agent.routes[model_id] = route
            newly_empty_routes = frozenset(
                (item["backend"], item["menu_model"])
                for item in removed_hops
                if not config.agents[item["backend"]].routes[item["menu_model"]].hops
            )
            would_interrupt = self._introduced_interruptions(
                previous,
                config,
                newly_empty_routes=newly_empty_routes,
            )
            if removed_hops and not force:
                raise ModelHubError(
                    "source_in_route_chain",
                    status=409,
                    data={
                        "would_remove_hops": removed_hops,
                        "would_interrupt": would_interrupt,
                    },
                )
            if would_interrupt and not force:
                raise ModelHubError(
                    "source_last_supplier",
                    status=409,
                    data={"would_remove_hops": [], "would_interrupt": would_interrupt},
                )
            self._prune_unavailable_agent_references(config)
            if source.credential_ref:
                self.revocations.add(source.id, source.credential_ref)
            try:
                await self._commit_synced(previous, config)
            except Exception:
                if source.credential_ref:
                    self.revocations.remove(source.id, source.credential_ref)
                raise
            try:
                if source.credential_ref:
                    await self._engine_call(self.adapter.revoke_credential(source.credential_ref))
            except ModelHubError:
                restored = False
                try:
                    self._save_config(previous)
                    restored = True
                    self._engine_synced = False
                    await self._sync_sources(previous)
                    self._engine_synced = True
                finally:
                    if restored:
                        self.revocations.remove(
                            source.id,
                            source.credential_ref,
                        )
                raise
            if source.credential_ref:
                self.revocations.remove(source.id, source.credential_ref)
            return {"removed_hops": removed_hops, "interrupted": would_interrupt}

    async def refresh_source(self, source_id: str, *, force: bool = False) -> dict:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            if source.supply_channel == "native_cli":
                raise ModelHubError("discovery_failed")
            try:
                model_ids = await self._discover(source)
                manual = [
                    model
                    for model in source.models
                    if model.provenance == "manual"
                ]
                self._apply_discovered_models(source, manual, model_ids)
            except ModelHubError as exc:
                if exc.code != "discovery_failed":
                    raise
                source.state = ModelHubSourceStateConfig(
                    status="error",
                    detail_key="models.source.error.unclassified",
                )
                self._save_config(config)
                self._record_event(
                    agent="system",
                    kind="needs_action",
                    model_id=None,
                    reason="unclassified_error",
                    from_source=source.id,
                    from_label=source.display_name,
                    now=self.now(),
                )
                raise
            source.state = ModelHubSourceStateConfig(status="standby")
            removed_hops, would_interrupt = self._guard_inventory_mutation(
                previous,
                config,
                source.id,
                force=force,
            )
            await self._commit_synced(previous, config)
            return {
                "source": source.to_payload(),
                "removed_hops": removed_hops,
                "interrupted": would_interrupt,
            }

    @staticmethod
    def _eligible_for_agent(source: ModelHubSourceConfig, backend: str) -> bool:
        return source_eligible_for_backend(source, backend)

    @classmethod
    def _source_eligibility(cls, source: ModelHubSourceConfig, backend: str) -> dict:
        if cls._eligible_for_agent(source, backend):
            reason_key = None
        elif source.kind == "subscription":
            reason_key = "models.eligibility.subscription_wrong_client"
        else:
            reason_key = "models.eligibility.subscription_wrong_client"
        return {
            "source_id": source.id,
            "eligible": reason_key is None,
            "reason_key": reason_key,
        }

    @staticmethod
    def _invalid_source_order(
        *,
        rejected_keys: list[str] | None = None,
    ) -> ModelHubError:
        safe_rejected = []
        for key in rejected_keys or []:
            safe_rejected.append(
                key
                if (
                    0 < len(key) <= 64
                    and key.isascii()
                    and key[0].isalpha()
                    and all(character.isalnum() or character == "_" for character in key)
                    and not contains_credential_material(key)
                )
                else "[redacted]"
            )
        return ModelHubError(
            "invalid_source_order",
            data=(
                {"rejected_keys": safe_rejected}
                if safe_rejected
                else None
            ),
        )

    async def set_agent_sources(self, backend: str, payload: object) -> dict:
        if backend not in MODEL_HUB_BACKENDS or not isinstance(payload, dict):
            raise self._invalid_source_order()
        if set(payload) != {"order"}:
            rejected = sorted(set(payload) - {"order"})
            raise self._invalid_source_order(rejected_keys=rejected)
        order = payload.get("order")
        if not isinstance(order, list):
            raise self._invalid_source_order()

        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            agent = self._agent(config, backend)
            seen: set[str] = set()
            by_id = {source.id: source for source in config.sources}
            for source_id in order:
                if (
                    not isinstance(source_id, str)
                    or source_id in seen
                    or source_id not in by_id
                    or not self._eligible_for_agent(by_id[source_id], backend)
                ):
                    raise self._invalid_source_order()
                seen.add(source_id)
            agent.sources.order = list(order)
            self._save_config(config)
            return self._agent_payload(config, agent)

    def get_agent_sources(self, backend: str) -> dict:
        config = self.store.load()
        return self._agent_payload(config, self._agent(config, backend))

    async def set_agent_chain(self, backend: str, model_id: object, payload: object) -> dict:
        if (
            backend not in MODEL_HUB_BACKENDS
            or not isinstance(model_id, str)
            or not model_id.strip()
            or not isinstance(payload, dict)
            or set(payload) - {"hops", "force"}
            or "hops" not in payload
            or ("force" in payload and not isinstance(payload["force"], bool))
        ):
            raise ModelHubError("mapping_target_unavailable", status=409)
        try:
            route = ModelHubRouteConfig.from_payload({"hops": payload["hops"]})
        except (TypeError, ValueError):
            raise ModelHubError("mapping_target_unavailable", status=409) from None

        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            agent = self._agent(config, backend)
            if agent.mode == "direct":
                raise self._direct_mode_error()
            if model_id not in self._agent_menu_model_ids(agent):
                raise ModelHubError("mapping_target_unavailable", status=409)
            by_id = {source.id: source for source in config.sources}
            old_route = agent.routes.get(model_id, ModelHubRouteConfig())
            old_pairs = {(hop.source_id, hop.model_id) for hop in old_route.hops}
            for hop in route.hops:
                source = by_id.get(hop.source_id)
                if source is None or not self._eligible_for_agent(source, backend):
                    raise ModelHubError("mapping_target_unavailable", status=409)
                if (hop.source_id, hop.model_id) not in old_pairs and not any(
                    model.id == hop.model_id for model in source.models
                ):
                    raise ModelHubError("mapping_target_unavailable", status=409)
            removed_hops = [
                {
                    "backend": backend,
                    "menu_model": model_id,
                    "source_id": hop.source_id,
                    "model_id": hop.model_id,
                }
                for hop in old_route.hops
                if (hop.source_id, hop.model_id)
                not in {(item.source_id, item.model_id) for item in route.hops}
            ]
            agent.routes[model_id] = route
            interrupted = self._introduced_interruptions(
                previous,
                config,
                newly_empty_routes=(
                    frozenset({(backend, model_id)})
                    if old_route.hops and not route.hops
                    else frozenset()
                ),
            )
            if interrupted and payload.get("force") is not True:
                raise ModelHubError(
                    "source_last_supplier",
                    status=409,
                    data={
                        "would_remove_hops": removed_hops,
                        "would_interrupt": interrupted,
                    },
                )
            self._save_config(config)
            return {
                "chain": self._agent_chain(config, backend, model_id),
                "removed_hops": removed_hops,
                "interrupted": interrupted,
            }

    def _prune_unavailable_agent_references(self, config: ModelHubConfig) -> None:
        # Route membership is user configuration. Inventory refresh and source
        # deletion may annotate or remove exact hops, but never re-match menus.
        return

    def _agent_payload(self, config: ModelHubConfig, agent: ModelHubAgentSupplyConfig) -> dict:
        backend = cast(BackendName, agent.backend)
        builtin_models = list(_builtin_model_ids(agent.backend)) if agent.menu_kind == "fixed" else None
        standard_vendors = sorted(STANDARD_OPENCODE_VENDOR_IDS) if agent.backend == "opencode" else None
        requested_model = self._requested_model(agent)
        unavailable_source_ids = self._unavailable_native_sources(config, backend)
        resolution = resolve_model_hub_turn(
            config,
            backend,
            requested_model,
            now=self.now(),
            unavailable_source_ids=unavailable_source_ids,
        )
        menu_model_ids = (
            builtin_models if builtin_models is not None else list(agent.menu.checked if agent.menu else ())
        )
        model_supply = [
            {
                "model_id": model_id,
                "chain_length": len(agent.routes.get(model_id, ModelHubRouteConfig()).hops),
            }
            for model_id in menu_model_ids
        ]
        selected_model_id = (
            (resolution.requested_model or None)
            if agent.mode == "hub"
            else None
        )
        selected_model_explicit = agent.mode == "hub" and bool(requested_model)
        current_chain_source_ids = {
            source.id
            for source in resolution.matching_sources
        }
        sources = (
            {
                "order": config.effective_source_order(agent.backend),
                "eligibility": [
                    {
                        **self._source_eligibility(
                            source,
                            agent.backend,
                        ),
                        "in_current_model_chain": (
                            source.id in current_chain_source_ids
                            if selected_model_id is not None
                            else None
                        ),
                        "process_availability_reason": (
                            "native_cli_unavailable"
                            if source.id in unavailable_source_ids
                            else None
                        ),
                    }
                    for source in sorted(config.sources, key=lambda item: item.id)
                ],
            }
            if agent.mode == "hub"
            else None
        )
        selected_by_agent = (
            self.selected_agent_override(backend)
            if agent.mode == "hub" and self.selected_agent_override is not None
            else None
        )
        named_agents = []
        if self.named_agents_override is not None:
            for name, pinned_model in self.named_agents_override(backend):
                requested = str(pinned_model or "").strip()
                named_resolution = resolve_model_hub_turn(
                    config,
                    backend,
                    requested,
                    now=self.now(),
                    unavailable_source_ids=unavailable_source_ids,
                )
                named_agents.append(
                    {
                        "name": name,
                        "effective_model_id": named_resolution.requested_model or None,
                        "supply_status": (
                            named_resolution.supply_status
                            if agent.mode == "hub" and named_resolution.requested_model
                            else None
                        ),
                    }
                )
        agent_payload = agent.to_payload()
        agent_payload["routes"] = (
            agent_payload["routes"] if agent.mode == "hub" else None
        )
        return {
            **agent_payload,
            "selected_by_agent": selected_by_agent,
            "selected_model_id": selected_model_id,
            "selected_model_explicit": selected_model_explicit,
            "sources": sources,
            "supply_status": (
                resolution.supply_status
                if agent.mode == "hub" and resolution.requested_model
                else None
            ),
            "model_supply": model_supply if agent.mode == "hub" else None,
            "named_agents": named_agents,
            "builtin_models": builtin_models,
            "standard_vendors": standard_vendors,
        }

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
            self._save_config(config)
            return self._agent_payload(config, agent)

    async def set_opencode_menu(self, menu: object) -> dict:
        async with self._mutation_lock:
            config = self.store.load()
            agent = config.agents["opencode"]
            try:
                parsed_menu = ModelHubMenuConfig.from_payload(cast(dict, menu))
                candidate = agent.to_payload()
                candidate["menu"] = parsed_menu.to_payload()
                candidate_routes = cast(dict, candidate["routes"])
                for identifier in parsed_menu.checked:
                    candidate_routes.setdefault(identifier, ModelHubRouteConfig().to_payload())
                parsed = ModelHubAgentSupplyConfig.from_payload(
                    candidate,
                    expected_backend="opencode",
                )
            except (TypeError, ValueError) as exc:
                raise ModelHubError("mapping_target_unavailable") from exc
            agent.menu = parsed.menu
            agent.routes = parsed.routes
            self._save_config(config)
            return self._agent_payload(config, agent)

    @staticmethod
    def _validated_reasoning_efforts(
        model: ModelHubModelConfig,
        value: object,
    ) -> list[str]:
        try:
            validated = ModelHubModelConfig.from_payload(
                {
                    **model.to_payload(),
                    "reasoning_efforts": value,
                }
            )
        except ValueError:
            raise ModelHubError("mapping_target_unavailable") from None
        return validated.reasoning_efforts

    async def add_custom_model(self, source_id: object, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ModelHubError("source_not_found", status=404)
        model_id = payload.get("model_id")
        display_name = payload.get("display_name")
        reasoning_efforts = payload.get("reasoning_efforts")
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
            source = self._source(config, str(source_id or ""))
            existing = next((model for model in source.models if model.id == model_id), None)
            if existing is None:
                model = ModelHubModelConfig(
                    id=model_id,
                    display_name=display_name,
                    provenance="manual",
                    discovered_at=None,
                )
                model.reasoning_efforts = self._validated_reasoning_efforts(
                    model,
                    reasoning_efforts,
                )
                source.models.append(model)
            elif existing.provenance == "discovered":
                raise ModelHubError("source_model_managed_upstream", status=409)
            else:
                existing.display_name = display_name
                existing.reasoning_efforts = self._validated_reasoning_efforts(
                    existing,
                    reasoning_efforts,
                )
            await self._commit_synced(previous, config)
            return source.to_payload()

    async def update_model_reasoning_efforts(
        self,
        source_id: object,
        model_id: object,
        payload: object,
    ) -> dict:
        if not isinstance(model_id, str) or not model_id or not isinstance(payload, dict):
            raise ModelHubError("mapping_target_unavailable")
        if set(payload) != {"reasoning_efforts"}:
            raise ModelHubError("mapping_target_unavailable")
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, str(source_id or ""))
            model = next((item for item in source.models if item.id == model_id), None)
            if model is None:
                raise ModelHubError("mapping_target_unavailable", status=404)
            model.reasoning_efforts = self._validated_reasoning_efforts(
                model,
                payload["reasoning_efforts"],
            )
            await self._commit_synced(previous, config)
            return source.to_payload()

    async def delete_custom_model(
        self,
        source_id: object,
        model_id: object,
        *,
        force: bool = False,
    ) -> dict:
        if not isinstance(model_id, str) or not model_id:
            raise ModelHubError("mapping_target_unavailable")
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, str(source_id or ""))
            model = next((item for item in source.models if item.id == model_id), None)
            if model is None:
                raise ModelHubError("mapping_target_unavailable", status=404)
            if model.provenance == "discovered":
                raise ModelHubError("source_model_managed_upstream", status=409)
            removed_hops = [
                {
                    "backend": backend,
                    "menu_model": menu_model,
                    "source_id": source.id,
                    "model_id": model_id,
                }
                for backend in MODEL_HUB_BACKENDS
                for menu_model, route in config.agents[backend].routes.items()
                for hop in route.hops
                if hop.source_id == source.id and hop.model_id == model_id
            ]
            source.models = [
                item
                for item in source.models
                if item.id != model_id
            ]
            would_interrupt = self._introduced_interruptions(previous, config)
            if (removed_hops or would_interrupt) and not force:
                raise ModelHubError(
                    "source_model_in_route_chain" if removed_hops else "source_last_supplier",
                    status=409,
                    data={
                        "would_remove_hops": removed_hops,
                        "would_interrupt": would_interrupt,
                    },
                )
            if force and removed_hops:
                for agent in config.agents.values():
                    for route in agent.routes.values():
                        route.hops = tuple(
                            hop
                            for hop in route.hops
                            if not (hop.source_id == source.id and hop.model_id == model_id)
                        )
            await self._commit_synced(previous, config)
            return {
                "source": source.to_payload(),
                "removed_hops": removed_hops,
                "interrupted": would_interrupt,
            }

    def list_events(self, *, limit: int = 20, before: Optional[str] = None) -> list[dict]:
        events = self.events.list(limit=limit, before=before)
        for event in events:
            if event.get("severity") is None:
                event["severity"] = (
                    "action_required"
                    if event.get("kind") in {"needs_action", "supply_interrupted"}
                    else "info"
                )
        return events

    @staticmethod
    def _direct_mode_error() -> ModelHubError:
        return ModelHubError(
            "direct_mode",
            status=409,
            detail="models.hub.direct_mode",
        )

    @staticmethod
    def _chain_supply_state(chain: list[dict]) -> Literal["ok", "waiting", "interrupted"]:
        if any(item["runnable"] for item in chain):
            return "ok"
        if chain and all(
            item["health"] == "cooldown" and item["reason"] is None
            for item in chain
        ):
            return "waiting"
        return "interrupted"

    def _agent_chain(
        self,
        config: ModelHubConfig,
        backend: str,
        model_id: str,
    ) -> dict:
        agent = self._agent(config, backend)
        if agent.mode == "direct":
            raise self._direct_mode_error()
        now = self.now()
        unavailable = self._unavailable_native_sources(
            config,
            cast(BackendName, backend),
        )
        resolution = resolve_model_hub_turn(
            config,
            cast(BackendName, backend),
            model_id,
            now=now,
            unavailable_source_ids=unavailable,
        )
        chain: list[dict] = []
        for inspection in resolution.inspected_hops:
            source = inspection.source
            if source is None:
                chain.append(
                    {
                        "source_id": inspection.source_id,
                        "model_id": inspection.model_id,
                        "channel": "hub",
                        "health": "error",
                        "runnable": False,
                        "reason": inspection.reason,
                        "retry_at": None,
                    }
                )
                continue
            status = source.state.status
            health = (
                "healthy"
                if status in {"active", "standby"}
                else status
            )
            chain.append(
                {
                    "source_id": source.id,
                    "model_id": inspection.model_id,
                    "channel": source.supply_channel,
                    "health": health,
                    "runnable": inspection.runnable,
                    "reason": inspection.reason,
                    "retry_at": inspection.retry_at,
                }
            )
        current = next(
            (
                {"source_id": item["source_id"], "model_id": item["model_id"]}
                for item in chain
                if item["runnable"]
            ),
            None,
        )
        return {
            "contract_version": AGENT_CHAIN_CONTRACT_VERSION,
            "backend": backend,
            "model_id": resolution.requested_model or model_id,
            "chain": chain,
            "current": current,
            "supply_state": self._chain_supply_state(chain),
        }

    def agent_chain(self, backend: str, model_id: object) -> dict:
        if backend not in MODEL_HUB_BACKENDS or not isinstance(model_id, str) or not model_id:
            raise ModelHubError("mapping_target_unavailable", status=409)
        return self._agent_chain(self.store.load(), backend, model_id)

    @staticmethod
    def _probe_request(
        source: ModelHubSourceConfig,
        model_id: str,
        backend: str,
    ) -> ModelHubRequest:
        # A probe enters the same translation seam as a live backend turn, so
        # its payload must be shaped in the backend's client protocol.
        request_protocol = {
            "claude": "anthropic",
            "codex": "openai_responses",
        }.get(backend, source.protocol)
        if request_protocol == "anthropic":
            payload = {
                "model": model_id,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        elif request_protocol == "openai_responses":
            payload = {
                "model": model_id,
                "max_output_tokens": 1,
                "input": "ping",
            }
        else:
            payload = {
                "model": model_id,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        return ModelHubRequest(payload, protocol=request_protocol)

    @staticmethod
    def _probe_failure(
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
    ) -> tuple[str, Optional[EventReason]]:
        if decision.action == "surface":
            return "models.source.error.unclassified", None
        if decision.reason == "permission_denied":
            return "models.source.error.unclassified", None
        if outcome.kind == RawOutcomeKind.NETWORK_ERROR:
            return "models.source.cooldown.network", "network"
        if outcome.kind == RawOutcomeKind.TIMEOUT:
            return "models.source.cooldown.timeout", "network"
        text = " ".join(
            value
            for value in (outcome.error_code, outcome.redacted_message)
            if isinstance(value, str)
        ).lower()
        if outcome.http_status == 401:
            return "models.source.needs_action.oauth_expired", "credential_expired"
        if outcome.http_status == 402 or "balance" in text:
            return "models.source.needs_action.balance_exhausted", "balance_exhausted"
        if outcome.http_status == 403 and any(
            marker in text for marker in ("ban", "suspend", "disabled account")
        ):
            return "models.source.needs_action.account_banned", "account_banned"
        if outcome.http_status == 403:
            return "models.source.needs_action.credential_revoked", "credential_revoked"
        if decision.reason == "unclassified_error":
            return "models.source.error.unclassified", "unclassified_error"
        if decision.action == "fallback" and decision.reason is not None:
            return f"models.source.cooldown.{decision.reason}", cast(
                EventReason,
                decision.reason,
            )
        return "models.source.error.unclassified", "unclassified_error"

    async def _set_source_blocker(
        self,
        source_id: str,
        *,
        backend: BackendName,
        model_id: str,
        detail_key: str,
        reason: EventReason,
    ) -> None:
        async with self._mutation_lock:
            config = self.store.load()
            source = self._source(config, source_id)
            status: Literal["error", "needs_action"] = (
                "error"
                if reason == "unclassified_error"
                else "needs_action"
            )
            unchanged = (
                source.state.status == status
                and source.state.detail_key == detail_key
            )
            source.state = ModelHubSourceStateConfig(
                status=status,
                detail_key=detail_key,
            )
            self._save_config(config)
            if not unchanged:
                self._record_event(
                    agent=cast(EventAgent, backend),
                    kind="needs_action",
                    model_id=model_id,
                    reason=reason,
                    from_source=source.id,
                    from_label=source.display_name,
                    now=self.now(),
                )

    async def probe_agent(self, backend: str, model_id: object = None) -> dict:
        if backend not in MODEL_HUB_BACKENDS:
            raise ModelHubError("mapping_target_unavailable", status=409)
        config = self.store.load()
        agent = self._agent(config, backend)
        if agent.mode == "direct":
            raise self._direct_mode_error()
        requested_model = (
            str(model_id).strip()
            if isinstance(model_id, str) and model_id.strip()
            else self._requested_model(agent)
        )
        chain_payload = self._agent_chain(config, backend, requested_model)
        candidate_payload = next(
            (item for item in chain_payload["chain"] if item["runnable"]),
            None,
        )
        if candidate_payload is None:
            supply_state = chain_payload["supply_state"]
            retry_at = min(
                (
                    item["retry_at"]
                    for item in chain_payload["chain"]
                    if item["retry_at"]
                ),
                default=None,
            )
            raise ModelHubError(
                "probe_no_candidate",
                status=409,
                detail=f"models.probe.no_candidate.{supply_state}",
                data={
                    "supply": {
                        "supply_state": supply_state,
                        "retry_at": retry_at,
                    }
                },
            )
        source = self._source(config, candidate_payload["source_id"])
        resolved_model = candidate_payload["model_id"]
        if source.supply_channel == "native_cli":
            ready = self.native_source_ready(
                cast(BackendName, backend),
                source_after_cooldown_recovery(source, self.now()),
            )
            return {
                "contract_version": PROBE_RESULT_CONTRACT_VERSION,
                "backend": backend,
                "channel": "native_cli",
                "reachable": ready,
                "source_id": source.id,
                "model_id": resolved_model,
                "latency_ms": None,
                "error": (
                    None
                    if ready
                    else "models.probe.native_cli_unavailable"
                ),
            }

        await self._prepare_engine_for_demand()
        started_at = time.monotonic()
        handle = await self._engine_call(
            self.adapter.invoke(
                source.id,
                resolved_model,
                self._probe_request(source, resolved_model, backend),
                False,
                backend,
            )
        )
        if handle.stream is not None:
            async for _chunk in handle.stream:
                pass
        outcome = await self._engine_call(handle.outcome())
        decision = classify_outcome(outcome)
        if decision.action == "refresh":
            handle = await self._engine_call(
                self.adapter.invoke(
                    source.id,
                    resolved_model,
                    self._probe_request(source, resolved_model, backend),
                    False,
                    backend,
                )
            )
            if handle.stream is not None:
                async for _chunk in handle.stream:
                    pass
            outcome = await self._engine_call(handle.outcome())
            decision = classify_outcome(outcome, refresh_attempted=True)
        elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
        reachable = decision.action == "return"
        error_key: Optional[str] = None
        latency_ms: Optional[int] = elapsed_ms
        if not reachable:
            error_key, event_reason = self._probe_failure(outcome, decision)
            if error_key in {
                "models.source.cooldown.network",
                "models.source.cooldown.timeout",
            }:
                latency_ms = None
            if event_reason in {
                "quota_exhausted",
                "rate_limited",
                "server_error",
                "network",
            }:
                await self._cooldown(
                    source,
                    decision,
                    agent=cast(EventAgent, backend),
                    model_id=chain_payload["model_id"],
                    detail_key=error_key,
                )
            elif event_reason is not None:
                await self._set_source_blocker(
                    source.id,
                    backend=cast(BackendName, backend),
                    model_id=chain_payload["model_id"],
                    detail_key=error_key,
                    reason=event_reason,
                )
        return {
            "contract_version": PROBE_RESULT_CONTRACT_VERSION,
            "backend": backend,
            "channel": "hub",
            "reachable": reachable,
            "source_id": source.id,
            "model_id": resolved_model,
            "latency_ms": latency_ms,
            "error": error_key,
        }

    @staticmethod
    def note_turn_mode(
        turn_id: str,
        mode: Literal["direct", "hub"],
    ) -> None:
        """Store the mode on the message row whose lifetime defines the turn."""

        normalized = str(turn_id or "").strip()
        if not normalized:
            return
        with get_cached_sqlite_engine().begin() as conn:
            conn.execute(
                messages.update()
                .where(
                    func.json_extract(
                        messages.c.metadata_json,
                        "$.turn_id",
                    )
                    == normalized
                )
                .values(
                    metadata_json=func.json_set(
                        messages.c.metadata_json,
                        "$.model_hub_mode",
                        mode,
                    )
                )
            )

    @staticmethod
    def _known_turn(
        turn_id: str,
    ) -> tuple[Optional[str], Optional[Literal["direct", "hub"]]]:
        try:
            with get_cached_sqlite_engine().connect() as conn:
                row = conn.execute(
                    select(
                        agent_sessions.c.agent_backend.label("backend"),
                        func.json_extract(
                            messages.c.metadata_json,
                            "$.model_hub_mode",
                        ).label("mode"),
                    )
                    .select_from(
                        messages.join(
                            agent_sessions,
                            messages.c.session_id == agent_sessions.c.id,
                        )
                    )
                    .where(
                        func.json_extract(
                            messages.c.metadata_json,
                            "$.turn_id",
                        )
                        == turn_id
                    )
                    .limit(1)
                ).mappings().first()
        except Exception:
            return None, None
        if row is None:
            return None, None
        mode = row["mode"]
        return (
            str(row["backend"]),
            mode if mode in {"direct", "hub"} else None,
        )

    def get_turn_provenance(self, turn_id: object) -> dict:
        normalized = str(turn_id or "").strip()
        if not normalized:
            raise ModelHubError("turn_not_found", status=404)
        record = self.provenance.get(normalized)
        if record is not None:
            return record
        backend, mode = self._known_turn(normalized)
        if backend is None:
            raise ModelHubError("turn_not_found", status=404)
        detail = (
            "models.provenance.direct_mode"
            if mode == "direct"
            else "models.provenance.attribution_ambiguous"
        )
        raise ModelHubError(
            "provenance_unavailable",
            status=409,
            detail=detail,
        )

    async def reauth_source(self, source_id: str, payload: object) -> dict:
        if (
            not isinstance(payload, dict)
            or set(payload) - {"acknowledge_irreversible"}
            or (
                "acknowledge_irreversible" in payload
                and payload.get("acknowledge_irreversible") is not True
            )
        ):
            raise ModelHubError("reauth_confirmation_required", status=409)

        async with self._mutation_lock:
            config = self.store.load()
            source = self._source(config, source_id)
            if source.kind != "subscription":
                raise ModelHubError("discovery_failed")
            if (
                source.supply_channel == "native_cli"
                and payload.get("acknowledge_irreversible") is not True
            ):
                raise ModelHubError("reauth_confirmation_required", status=409)

            replace_pending_flow_id: str | None = None
            pending = self.oauth_flows.pending_reauth(source.id)
            if pending is not None:
                pending_flow_id, pending_binding = pending
                try:
                    pending_flow = await self._oauth_status(
                        pending_flow_id,
                        pending_binding.channel,
                    )
                    self._raise_if_flow_expired(pending_flow_id, pending_flow)
                except ModelHubError as error:
                    # Hub OAuth flow state is process-local. After a controller
                    # restart, its unknown-flow error reaches L2 as engine_down.
                    if (
                        error.code == "engine_down"
                        and pending_binding.channel == "hub"
                    ):
                        replace_pending_flow_id = pending_flow_id
                    elif error.code not in {"flow_expired", "flow_not_found"}:
                        raise
                else:
                    if pending_flow.state not in {"failed", "cancelled"}:
                        return {
                            "flow": _oauth_payload(
                                pending_flow,
                                channel=pending_binding.channel,
                                intent="reauth",
                            )
                        }
                    if self._is_hub_unsuccessful_terminal(
                        pending_binding,
                        pending_flow,
                    ):
                        config = (
                            await self._materialize_failed_hub_reauth(
                                pending_binding,
                                pending_flow,
                                config=config,
                            )
                            or config
                        )
                        source = self._source(config, source_id)
                    try:
                        self.oauth_flows.forget(pending_flow_id)
                    except OSError:
                        raise ModelHubError("engine_down", status=503) from None

            channel = cast(OAuthChannel, source.supply_channel)
            oauth_adapter = self._oauth_adapter(channel)
            recovered = source.state.status in {"needs_action", "error"}

            def mark_native_irreversible_start() -> Callable[[], None]:
                previous = self._clone_config(config)
                affected_backend = _NATIVE_VENDOR_BACKENDS[source.vendor]
                for candidate in config.sources:
                    if (
                        candidate.supply_channel != "native_cli"
                        or _NATIVE_VENDOR_BACKENDS.get(candidate.vendor)
                        != affected_backend
                    ):
                        continue
                    candidate.models = [
                        model
                        for model in candidate.models
                        if model.provenance == "manual"
                    ]
                    candidate.account_label = None
                    candidate.state = ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key="models.source.needs_action.oauth_expired",
                    )
                self._save_config(config)

                def restore_after_spawn_failure() -> None:
                    self._save_config(previous)

                return restore_after_spawn_failure

            flow = await self._oauth_call(
                (
                    self.native_oauth_adapter.start_reauth(
                        source.id,
                        source.vendor,
                        on_irreversible_start=mark_native_irreversible_start,
                    )
                    if channel == "native_cli"
                    else oauth_adapter.start_oauth(source.id, source.vendor)
                )
            )
            if flow.source_id != source.id or flow.vendor != source.vendor:
                if channel == "hub":
                    await self._discard_unbound_hub_flow(flow)
                raise ModelHubError("flow_not_found", status=502)
            try:
                self.oauth_flows.remember(
                    flow.flow_id,
                    channel,
                    source.id,
                    source.vendor,
                    experimental_consent=False,
                    intent="reauth",
                    recovered=recovered,
                    replace_flow_id=replace_pending_flow_id,
                )
            except OSError:
                if channel == "hub":
                    await self._discard_unbound_hub_flow(flow)
                else:
                    try:
                        await self.native_oauth_adapter.cancel_oauth(flow.flow_id)
                    except Exception:
                        pass
                raise ModelHubError("engine_down", status=503) from None
            return {
                "flow": _oauth_payload(
                    flow,
                    channel=channel,
                    intent="reauth",
                )
            }

    async def oauth_start(self, payload: dict) -> dict:
        vendor = payload.get("vendor") if isinstance(payload, dict) else None
        channel = payload.get("channel") if isinstance(payload, dict) else None
        if not isinstance(vendor, str) or channel not in {"native_cli", "hub"}:
            raise ModelHubError("flow_not_found", status=400)
        try:
            vendor = normalize_model_hub_vendor_id(vendor)
        except ValueError:
            raise ModelHubError("flow_not_found", status=400) from None
        oauth_channel = cast(OAuthChannel, channel)
        if oauth_channel == "native_cli":
            backend = _NATIVE_VENDOR_BACKENDS.get(vendor)
            if backend is not None:
                existing = next(
                    (
                        source
                        for source in self.store.load().sources
                        if source.supply_channel == "native_cli"
                        and source_eligible_for_backend(source, cast(BackendName, backend))
                    ),
                    None,
                )
                if existing is not None:
                    raise ModelHubError(
                        "native_source_already_exists",
                        status=409,
                        data={"existing_source_id": existing.id},
                    )
        pending_source_id = _source_id()
        flow = await self._oauth_call(
            self._oauth_adapter(oauth_channel).start_oauth(pending_source_id, vendor)
        )
        if flow.source_id != pending_source_id or flow.vendor != vendor:
            raise ModelHubError("flow_not_found", status=502)
        self.oauth_flows.remember(
            flow.flow_id,
            oauth_channel,
            pending_source_id,
            vendor,
            experimental_consent=False,
        )
        return {"flow": _oauth_payload(flow, channel=channel)}

    def _oauth_result(
        self,
        flow_id: str,
        flow: OAuthFlowState,
        *,
        channel: OAuthChannel,
    ) -> dict:
        binding = self._oauth_binding(flow_id)
        result = {
            "flow": _oauth_payload(
                flow,
                channel=channel,
                intent=binding.intent,
            )
        }
        if flow.state != "success":
            return result
        source = self._completed_oauth_source(binding)
        if source is None:
            raise ModelHubError("flow_not_found", status=404)
        if binding.intent == "reauth":
            result.update(
                {
                    "source": source.to_payload(),
                    "recovered": binding.recovered is True,
                    "interrupted_pairs": list(binding.interrupted_pairs),
                }
            )
        else:
            result.update(self._source_creation_result(source.to_payload()))
        return result

    async def oauth_status(self, flow_id: str) -> dict:
        binding = self._oauth_binding(flow_id)
        completed = self._completed_oauth_flow(flow_id, binding)
        if completed is not None:
            return self._oauth_result(
                flow_id,
                completed,
                channel=binding.channel,
            )
        flow = await self._oauth_status(flow_id, binding.channel)
        self._raise_if_flow_expired(flow_id, flow)
        flow, repair_result = await self._materialize_completed_oauth(
            flow_id,
            binding,
            flow,
        )
        if repair_result is not None:
            return {
                "flow": _oauth_payload(
                    flow,
                    channel=binding.channel,
                    intent="reauth",
                ),
                **repair_result,
            }
        return self._oauth_result(flow_id, flow, channel=binding.channel)

    async def oauth_submit(self, payload: dict) -> dict:
        flow_id = payload.get("flow_id") if isinstance(payload, dict) else None
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(flow_id, str) or not isinstance(value, str):
            raise ModelHubError("flow_not_found", status=404)
        binding = self._oauth_binding(flow_id)
        completed = self._completed_oauth_flow(flow_id, binding)
        if completed is not None:
            return self._oauth_result(
                flow_id,
                completed,
                channel=binding.channel,
            )
        current = await self._oauth_status(flow_id, binding.channel)
        self._raise_if_flow_expired(flow_id, current)
        flow = await self._oauth_call(
            self._oauth_adapter(binding.channel).submit_oauth(flow_id, value),
            flow_id=flow_id,
        )
        flow, repair_result = await self._materialize_completed_oauth(
            flow_id,
            binding,
            flow,
        )
        if repair_result is not None:
            return {
                "flow": _oauth_payload(
                    flow,
                    channel=binding.channel,
                    intent="reauth",
                ),
                **repair_result,
            }
        return self._oauth_result(flow_id, flow, channel=binding.channel)

    async def oauth_cancel(self, flow_id: object) -> None:
        if not isinstance(flow_id, str):
            raise ModelHubError("flow_not_found", status=404)
        terminal: tuple[OAuthFlowBinding, OAuthFlowState] | None = None
        async with self._mutation_lock:
            binding = self._oauth_binding(flow_id)
            completed = self._completed_oauth_flow(flow_id, binding)
            if completed is not None:
                return
            flow = await self._oauth_status(flow_id, binding.channel)
            self._raise_if_flow_expired(flow_id, flow)
            if flow.state == "success" or self._is_hub_unsuccessful_terminal(
                binding,
                flow,
            ):
                terminal = (binding, flow)
            else:
                await self._oauth_call(
                    self._oauth_adapter(binding.channel).cancel_oauth(flow_id),
                    flow_id=flow_id,
                )
                if binding.channel == "hub":
                    cancelled = await self._oauth_status(
                        flow_id,
                        binding.channel,
                    )
                    if (
                        cancelled.state == "success"
                        or self._is_hub_unsuccessful_terminal(
                            binding,
                            cancelled,
                        )
                    ):
                        terminal = (binding, cancelled)
                    else:
                        self.oauth_flows.forget(flow_id)
                else:
                    self.oauth_flows.forget(flow_id)
        if terminal is not None:
            binding, flow = terminal
            await self._materialize_completed_oauth(
                flow_id,
                binding,
                flow,
            )
            if flow.state != "success":
                async with self._mutation_lock:
                    try:
                        self.oauth_flows.forget(flow_id)
                    except OSError:
                        raise ModelHubError("engine_down", status=503) from None

    async def runtime_status(self) -> dict:
        status = await self._engine_call(self.adapter.status())
        return _runtime_payload(self._runtime_status_after_demand(status))

    async def runtime_start(self) -> dict:
        await self._prepare_engine_for_demand()
        status = await self._engine_call(self.adapter.start())
        return _runtime_payload(status)

    def migration_scan(self) -> dict:
        config = self.store.load()
        return {
            "items": [
                item.to_payload()
                for item in scan_native_configs(
                    config,
                    mask_credential=_mask_credential,
                    claude_oauth_probe=self.migration_claude_oauth_probe,
                    validate_base_url=_validated_base_url,
                )
            ]
        }

    async def migration_apply(self, item_ids: object) -> dict:
        try:
            applied, added_to = await apply_native_migration(
                self,
                item_ids,
                mask_credential=_mask_credential,
                validate_base_url=_validated_base_url,
            )
        except MigrationConflictError:
            raise ModelHubError("migration_item_conflict", status=409)
        return {
            "applied": applied,
            "sources": self.list_sources(),
            "added_to": added_to,
        }

    async def _recover_resolution_sources(
        self,
        resolution: ModelHubTurnResolution,
    ) -> None:
        if not resolution.recoverable_source_ids:
            return
        async with self._mutation_lock:
            config = self.store.load()
            config_changed = False
            for source_id in resolution.recoverable_source_ids:
                source = next(
                    (item for item in config.sources if item.id == source_id),
                    None,
                )
                if source is None or source.state.status != "cooldown":
                    continue
                recovered_source = source_after_cooldown_recovery(source, self.now())
                if recovered_source is source:
                    continue
                source.state = recovered_source.state
                config_changed = True
                self._record_event(
                    agent=cast(EventAgent, resolution.backend),
                    kind="recover",
                    model_id=resolution.requested_model,
                    reason="recovery",
                    to_source=source.id,
                    to_label=source.display_name,
                    now=self.now(),
                )
            if config_changed:
                self._save_config(config)

    async def _cooldown(
        self,
        source: ModelHubSourceConfig,
        decision: ResolutionDecision,
        *,
        agent: EventAgent,
        model_id: str,
        detail_key: Optional[str] = None,
    ) -> None:
        async with self._mutation_lock:
            config = self.store.load()
            try:
                current = self._source(config, source.id)
            except ModelHubError:
                return
            already_cooling = current.state.status == "cooldown"
            current.state = ModelHubSourceStateConfig(
                status="cooldown",
                retry_at=(self.now() + timedelta(seconds=decision.cooldown_seconds)).isoformat(),
                detail_key=detail_key or f"models.source.cooldown.{decision.reason}",
            )
            self._save_config(config)
            if not already_cooling:
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

    @staticmethod
    def _request_reasoning_effort(request: Mapping[str, Any]) -> str | None:
        direct = request.get("reasoning_effort")
        if isinstance(direct, str) and direct:
            return direct
        reasoning = request.get("reasoning")
        if isinstance(reasoning, Mapping):
            nested = reasoning.get("effort")
            if isinstance(nested, str) and nested:
                return nested
        return None

    @staticmethod
    def _request_for_exact_reasoning_effort(
        request: Mapping[str, Any],
        source: ModelHubSourceConfig,
        model_id: str,
    ) -> Mapping[str, Any]:
        requested = ModelHubService._request_reasoning_effort(request)
        model = next((item for item in source.models if item.id == model_id), None)
        exact = (
            requested
            if requested is not None
            and model is not None
            and requested in model.reasoning_efforts
            else None
        )
        payload = dict(request)
        changed = False
        if "reasoning_effort" in payload:
            payload["reasoning_effort"] = exact
            changed = True
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, Mapping) and "effort" in reasoning:
            payload["reasoning"] = {**reasoning, "effort": exact}
            changed = True
        if not changed:
            return request
        if isinstance(request, ModelHubRequest):
            return ModelHubRequest(
                payload,
                protocol=request.protocol,
                headers=request.headers,
            )
        return payload

    async def resolve(
        self,
        *,
        backend: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool = False,
        supply_channel: Literal["hub"] | None = None,
        attempt_observer: Optional[AttemptObserver] = None,
    ) -> ResolvedInvocation:
        if backend not in {"claude", "codex", "opencode"}:
            raise ModelHubError("mapping_target_unavailable")
        engine_prepared = False
        if self.revocations.list():
            try:
                await self._ensure_engine_synced()
            except ModelHubError:
                # Cleanup remains durable and must not invent a supply failure.
                pass
            else:
                engine_prepared = True
        config = self.store.load()
        resolution = resolve_model_hub_turn(
            config,
            cast(BackendName, backend),
            model_id,
            now=self.now(),
            unavailable_source_ids=self._unavailable_native_sources(
                config,
                cast(BackendName, backend),
            ),
            supply_channel=supply_channel,
        )
        if config.agents[backend].mode != "hub":
            raise ModelHubError("mode_switch_blocked", status=409)
        if resolution.channel == "direct":
            raise ModelHubError("mapping_target_unavailable", status=409)
        if resolution.recoverable_source_ids:
            await self._recover_resolution_sources(resolution)
            config = self.store.load()
            resolution = resolve_model_hub_turn(
                config,
                cast(BackendName, backend),
                model_id,
                now=self.now(),
                unavailable_source_ids=self._unavailable_native_sources(
                    config,
                    cast(BackendName, backend),
                ),
                supply_channel=supply_channel,
            )
        event_agent = cast(EventAgent, backend)
        candidate_hops = list(resolution.candidate_hops)
        if not candidate_hops:
            supply_state = (
                "waiting"
                if resolution.supply_status == "waiting"
                else "interrupted"
            )
            raise ModelHubError(
                "mapping_target_unavailable",
                status=409,
                supply_state=supply_state,
            )

        failed_source: Optional[ModelHubSourceConfig] = None
        failed_reason: Optional[EventReason] = None
        globally_blocked_source_ids: set[str] = set()
        for inspection in candidate_hops:
            source = inspection.source
            target_model = inspection.model_id
            if source is None or target_model is None:
                raise AssertionError("runnable hop must have an exact identity")
            if source.id in globally_blocked_source_ids:
                continue
            exact_request = self._request_for_exact_reasoning_effort(
                request,
                source,
                target_model,
            )
            if source.supply_channel == "native_cli":
                self._emit_switch(
                    agent=event_agent,
                    model_id=model_id,
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
            await self._prepare_engine_for_demand(already_synced=engine_prepared)
            engine_prepared = True
            if attempt_observer is not None:
                attempt_observer(
                    source.id,
                    target_model,
                    "hub",
                    False,
                    None,
                    None,
                )
            handle, outcome = await self._invoke(
                source=source,
                model_id=target_model,
                request=exact_request,
                stream=stream,
                backend=backend,
            )
            if outcome is None:
                self._emit_switch(
                    agent=event_agent,
                    model_id=model_id,
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
                    request=exact_request,
                    stream=stream,
                    backend=backend,
                )
                if outcome is None:
                    self._emit_switch(
                        agent=event_agent,
                        model_id=model_id,
                        failed_source=failed_source,
                        failed_reason=failed_reason,
                        source=source,
                    )
                    return ResolvedInvocation(source.id, target_model, handle, None)
                decision = classify_outcome(outcome, refresh_attempted=True)
            if attempt_observer is not None:
                attempt_observer(
                    source.id,
                    target_model,
                    "hub",
                    False,
                    outcome,
                    decision,
                )
            if decision.action == "return":
                self._emit_switch(
                    agent=event_agent,
                    model_id=model_id,
                    failed_source=failed_source,
                    failed_reason=failed_reason,
                    source=source,
                )
                return ResolvedInvocation(source.id, target_model, handle, outcome)
            if decision.action == "surface":
                raise ModelHubError(
                    decision.error_code or outcome.error_code or "engine_down",
                    status=(
                        outcome.http_status
                        if outcome.http_status is not None and 400 <= outcome.http_status <= 599
                        else 502
                    ),
                )
            if decision.action == "fallback":
                event_reason = cast(EventReason, decision.reason)
                if event_reason in {
                    "quota_exhausted",
                    "rate_limited",
                    "server_error",
                    "network",
                }:
                    await self._cooldown(
                        source,
                        decision,
                        agent=event_agent,
                        model_id=model_id,
                    )
                elif event_reason != "permission_denied":
                    detail_key = {
                        "credential_expired": "models.source.needs_action.oauth_expired",
                        "credential_revoked": "models.source.needs_action.credential_revoked",
                        "balance_exhausted": "models.source.needs_action.balance_exhausted",
                        "account_banned": "models.source.needs_action.account_banned",
                        "unclassified_error": "models.source.error.unclassified",
                    }[event_reason]
                    await self._set_source_blocker(
                        source.id,
                        backend=cast(BackendName, backend),
                        model_id=model_id,
                        detail_key=detail_key,
                        reason=event_reason,
                    )
                if event_reason != "permission_denied":
                    globally_blocked_source_ids.add(source.id)
                failed_source = source
                failed_reason = event_reason
                continue
            raise ModelHubError(decision.error_code or "engine_down", status=502)
        raise ModelHubError("engine_down", status=503)


def create_default_service(
    *,
    adapter: Optional[EngineAdapter] = None,
    native_oauth_adapter: Optional[NativeOAuthAdapter] = None,
    requested_model_override: Optional[Callable[[BackendName], Optional[str]]] = None,
    selected_agent_override: Optional[Callable[[BackendName], Optional[str]]] = None,
    named_agents_override: Optional[
        Callable[[BackendName], list[tuple[str, Optional[str]]]]
    ] = None,
) -> ModelHubService:
    if adapter is None:
        from vibe.model_hub_runtime import get_model_hub_engine_adapter

        adapter = get_model_hub_engine_adapter()

    if native_oauth_adapter is None:
        from .native_oauth import create_native_oauth_adapter

        native_oauth_adapter = create_native_oauth_adapter()

    def claude_oauth_probe() -> bool:
        from vibe.api import (
            _build_claude_status_probe_env,
            _read_claude_cli_oauth_signed_in,
            _resolve_claude_status_probe_cwd,
        )
        from vibe.claude_config import build_claude_subprocess_env

        try:
            config = V2Config.load()
        except FileNotFoundError:
            config = default_config()
        claude = config.agents.claude
        env = _build_claude_status_probe_env(
            build_claude_subprocess_env(claude, force_oauth=True)
        )
        return _read_claude_cli_oauth_signed_in(
            claude.cli_path,
            env=env,
            cwd=_resolve_claude_status_probe_cwd(config),
        ) is True

    return ModelHubService(
        store=V2ModelHubConfigStore(),
        adapter=adapter,
        events=BoundedEventLog(paths.get_state_dir() / "model_hub_resolution_events.json"),
        native_oauth_adapter=native_oauth_adapter,
        oauth_flows=OAuthFlowRegistry(paths.get_state_dir() / "model_hub_oauth_flows.json"),
        revocations=CredentialRevocationJournal(paths.get_state_dir() / "model_hub_pending_revocations.json"),
        migration_claude_oauth_probe=claude_oauth_probe,
        requested_model_override=requested_model_override,
        selected_agent_override=selected_agent_override,
        named_agents_override=named_agents_override,
    )
