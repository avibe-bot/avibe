"""Model Hub aggregate service used by REST routes and backend injection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, Optional, Protocol, cast

from sqlalchemy import func, select

from config import paths
from config.v2_config import (
    CONFIG_LOCK,
    MODEL_HUB_BACKENDS,
    ModelHubAgentSupplyConfig,
    ModelHubBackendModelConfig,
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
    normalize_storable_backend_model_text,
    normalize_model_hub_base_url,
    normalize_model_hub_vendor_id,
    validate_model_hub_source_client_nonce,
)
from core.agent_auth_service import BackendLoginInProgressError
from core.services.settings import default_config
from storage.db import get_cached_sqlite_engine
from storage.models import agent_sessions, messages
from vibe.backend_model_catalog import bundled_catalog_reasoning_efforts_by_model
from vibe.model_hub_runtime.api_key_vendors import (
    catalog_api_key_vendor_label,
    pinned_api_key_protocol,
)

from .adapter import (
    DiscoveredModel,
    EngineAdapter,
    EngineHealth,
    EngineStatus,
    InvokeHandle,
    OAuthFlowState,
    OriginNotAllowedError,
    RawCallOutcome,
    RawOutcomeKind,
    RetainedMaterialDisposition,
    RuntimePlatformUnsupportedError,
    ObservationDiscovery,
    ObservationOutcome,
    SOURCE_PROTOCOLS,
    SourceObservation,
    SourceBinding,
    make_source_observation,
    validate_source_observation,
)
from .async_owner import await_owned_task
from .catalog_admission import (
    admissible_backend_model,
    backend_model_admission_error,
)
from .classification import (
    ResolutionDecision,
    classify_outcome,
    source_settlement_allowed,
    source_settlement_rule,
    terminal_outcome_category,
)
from .events import (
    BoundedEventLog,
    EventAgent,
    EventReason,
    build_resolution_event,
    contains_credential_material,
    redact_credential_material,
)
from .errors import ModelDiscoveryError
from .identifiers import STANDARD_OPENCODE_VENDOR_IDS, canonical_model_id
from .migration import (
    MigrationConflictError,
    apply_native_migration,
    build_native_migration_source,
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
from .provenance import (
    BoundedProvenanceStore,
    ENGINE_DOWN_TURN_OUTCOME,
    ExactHopBlocker,
    TurnOutcomeProjectionInput,
    exact_hop_blockers,
    produce_turn_outcome,
)
from .request import ModelHubRequest
from .reasoning_tiers import resolve_reasoning_tiers
from .stream_wire import ProtocolSSEState
from .resolver import (
    BackendName,
    ModelHubTurnResolution,
    allowed_origins,
    inspect_exact_hop,
    matching_v1_model_id as _matching_v1_model_id,
    opencode_source_model_identity,
    resolve_model_hub_turn,
    source_after_cooldown_recovery,
    source_eligible_for_backend,
    source_runnable,
)
from .revocations import CredentialRevocationJournal
from .usage import USAGE_DEFAULT_WINDOW_DAYS, BoundedUsageLedger, SourceIdentity, UsageWriter

CONTRACT_VERSION = 7


def _storable_backend_model_metadata(
    display_name: object,
    reasoning_efforts: object,
) -> tuple[Optional[str], list[str]]:
    proposed_display_name = normalize_storable_backend_model_text(
        display_name,
        field_name="display_name",
    )
    proposed_efforts: list[str] = []
    if isinstance(reasoning_efforts, (list, tuple)):
        for effort in reasoning_efforts:
            proposed = normalize_storable_backend_model_text(
                effort,
                field_name="reasoning_efforts",
            )
            if proposed is not None and proposed not in proposed_efforts:
                proposed_efforts.append(proposed)
    return proposed_display_name, proposed_efforts


AGENT_CHAIN_CONTRACT_VERSION = 7
PROBE_RESULT_CONTRACT_VERSION = 7
_REORDER_ORDER_UNSET = object()
_REASONING_EFFORT_TELEMETRY_MAX_BYTES = 256
# Settlement generations are minted per attempt start and live only in this
# runtime's ledger, which restarts with the process. Every generation this
# runtime mints is therefore strictly greater than this pre-attempt value, and
# an attempt that started before the ledger existed settles as this value: older
# than any attempt this runtime can start, yet still able to settle a Source that
# this runtime has not attempted again.
PRE_ATTEMPT_SETTLEMENT_GENERATION = 0


def project_opencode_public_model(
    model: ModelHubBackendModelConfig,
) -> dict[str, Any]:
    """Project backend-owned metadata without upstream Route details."""

    identifier = model.id
    projected: dict[str, Any] = {
        "id": identifier,
        "name": model.display_name or identifier,
    }
    if model.context_window is not None or model.max_output_tokens is not None:
        projected["limit"] = {
            key: value
            for key, value in (
                ("context", model.context_window),
                ("output", model.max_output_tokens),
            )
            if value is not None
        }
    modalities: dict[str, list[str]] = {}
    if model.input_modalities:
        modalities["input"] = list(model.input_modalities)
    if model.output_modalities:
        modalities["output"] = list(model.output_modalities)
    if modalities:
        projected["modalities"] = modalities
    if model.supports_tools is not None:
        projected["tool_call"] = model.supports_tools
    if model.supports_reasoning is not None:
        projected["reasoning"] = model.supports_reasoning
    variants = (
        {
            effort: {"reasoningEffort": effort}
            for effort in model.reasoning_efforts
        }
        if model.supports_reasoning is not False
        else {}
    )
    if variants:
        projected["variants"] = variants
    return projected
logger = logging.getLogger(__name__)

_NATIVE_VENDOR_BACKENDS = {"anthropic": "claude", "openai": "codex"}
_FIXED_BACKEND_PROTOCOLS: dict[
    str,
    Literal["anthropic", "openai_responses", "openai_chat"],
] = {
    "claude": "anthropic",
    "codex": "openai_responses",
}


class ModelHubError(Exception):
    def __init__(
        self,
        code: str,
        *,
        status: int = 400,
        detail: Optional[str] = None,
        supply_state: Optional[Literal["waiting", "interrupted"]] = None,
        data: Optional[Mapping[str, Any]] = None,
        blockers: Iterable[ExactHopBlocker] = (),
        turn_outcome: TurnOutcomeProjectionInput | None = None,
    ):
        detail_key = detail or f"modelHub.errors.{code}"
        super().__init__(detail_key)
        self.code = code
        self.status = status
        self.detail = detail_key
        self.supply_state = supply_state
        self.data = dict(data or {})
        self.blockers = tuple(blockers)
        self.turn_outcome = turn_outcome


class CredentialCleanupUnsettledError(ModelHubError):
    def __init__(self):
        super().__init__("engine_down", status=503)


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

    def ensure_writable(self) -> None:
        try:
            config = V2Config.load()
        except FileNotFoundError:
            return
        if config.load_warnings:
            raise ModelHubError("config_recovery", status=409)

    def save(self, model_hub: ModelHubConfig) -> None:
        model_hub = ModelHubConfig.from_payload(model_hub.to_payload())
        self.ensure_writable()
        from config.v2_config import update_config_fields

        update_config_fields(lambda cfg: setattr(cfg, "model_hub", model_hub))

class UnavailableEngineAdapter:
    """Explicit fail-closed adapter for isolated callers and tests."""

    async def install(self) -> EngineStatus:
        return await self.status()

    async def recover_installation(self) -> EngineStatus:
        return await self.status()

    async def ensure_installed(self) -> EngineStatus:
        return await self.status()

    async def start(self) -> EngineStatus:
        raise EngineUnavailableError

    async def stop_runtime(self) -> EngineStatus:
        return await self.status()

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
    backend: BackendName
    requested_model_id: str
    source_id: str
    source_label: str
    model_id: str
    handle: Optional[InvokeHandle]
    outcome: Optional[RawCallOutcome]
    supply_channel: Literal["native_cli", "hub"] = "hub"
    credential_ref: Optional[str] = None
    settlement_generation: Optional[int] = None


@dataclass(frozen=True)
class HandleSettlement:
    outcome: RawCallOutcome | None
    decision: ResolutionDecision | None
    turn_outcome: TurnOutcomeProjectionInput | None


HandleTerminationOrigin = Literal["downstream_cancel", "upstream_terminal"]


@dataclass(frozen=True)
class ExactReasoningEffortRequest:
    request: Mapping[str, Any]
    stripped_efforts: tuple[str, ...] = ()
    declared_efforts: tuple[str, ...] = ()


def _bounded_reasoning_effort_telemetry(value: object) -> str:
    """Redact and fold an untrusted effort value into bounded telemetry."""

    if not isinstance(value, str):
        if value is None:
            return "<null>"
        if isinstance(value, bool):
            return "<bool>"
        if isinstance(value, int):
            return "<int>"
        if isinstance(value, float):
            return "<float>"
        if isinstance(value, list):
            return "<list>"
        if isinstance(value, Mapping):
            return "<dict>"
        return "<non-string>"

    redacted = redact_credential_material(value)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= _REASONING_EFFORT_TELEMETRY_MAX_BYTES:
        return redacted
    digest = hashlib.sha256(encoded).hexdigest()
    suffix = f"... [sha256:{digest}]"
    preview_bytes = _REASONING_EFFORT_TELEMETRY_MAX_BYTES - len(
        suffix.encode("utf-8")
    )
    preview = encoded[:preview_bytes].decode("utf-8", errors="ignore")
    return f"{preview}{suffix}"


def _bounded_declared_effort_telemetry(values: Iterable[str]) -> tuple[str, ...]:
    redacted = tuple(redact_credential_material(value) for value in values)
    payload = json.dumps(
        redacted,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) <= _REASONING_EFFORT_TELEMETRY_MAX_BYTES:
        return redacted
    return (_bounded_reasoning_effort_telemetry(payload),)


AttemptObserver = Callable[
    [
        str,
        str,
        Literal["native_cli", "hub"],
        bool,
        Optional[RawCallOutcome],
        Optional[ResolutionDecision],
        tuple[str, ...],
        tuple[str, ...],
    ],
    None,
]


def _same_json_value(left: object, right: object) -> bool:
    try:
        options = {
            "allow_nan": False,
            "ensure_ascii": False,
            "separators": (",", ":"),
            "sort_keys": True,
        }
        return json.dumps(left, **options) == json.dumps(right, **options)
    except (TypeError, ValueError):
        return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _project_opencode_public_models(
    config: ModelHubConfig,
    *,
    now: datetime,
    unavailable_source_ids: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    agent = config.agents["opencode"]
    if agent.mode == "direct":
        return {}
    projected: dict[str, dict[str, Any]] = {}
    for model in agent.models:
        projected[model.id] = project_opencode_public_model(model)
    return projected


def load_opencode_public_models() -> dict[str, dict[str, Any]]:
    """Load the persisted, credential-free OpenCode projection for local callers."""

    return _project_opencode_public_models(
        V2ModelHubConfigStore().load(),
        now=_utc_now(),
    )


def _source_id() -> str:
    return f"src_{uuid.uuid4().hex[:12]}"


def _parse_datetime(value: str) -> datetime:
    """Parse a timestamp into something comparable with this service's clock.

    Every parsed value is compared against ``self.now()``, which is UTC-aware.
    A provider or an older persisted record may still carry a naive ISO string,
    and comparing the two raises ``TypeError`` rather than answering the
    question — so read a naive timestamp as the UTC it was written as.
    """

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


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


async def _acquire_credential_ref_with_cancellation_ownership(
    service: "ModelHubService",
    operation: Awaitable[str],
    rollback_source_id: str,
) -> str:
    """Keep ownership of an engine ref when the caller is cancelled mid-provision."""

    provision_task = asyncio.create_task(
        service._engine_call(operation)
    )
    try:
        return await asyncio.shield(provision_task)
    except asyncio.CancelledError as cancelled:
        # The shield leaves provisioning alive; wait for its ref before settling
        # cancellation so the transient material can be journaled and revoked.
        transient_ref = await await_owned_task(provision_task)
        await _rollback_credential_before_settling(
            service,
            rollback_source_id,
            transient_ref,
        )
        raise cancelled


async def _provision_transient_credential_with_cancellation_ownership(
    service: "ModelHubService",
    vendor: str,
    key: str,
    base_url: str | None,
) -> str:
    return await _acquire_credential_ref_with_cancellation_ownership(
        service,
        service.adapter.provision_transient_credential(vendor, key, base_url),
        "observation",
    )


async def _rollback_credential_before_settling(
    service: "ModelHubService",
    source_id: str,
    credential_ref: str,
) -> None:
    """Finish transient cleanup before propagating a cancellation."""

    rollback_task = asyncio.create_task(
        _require_credential_cleanup(service, source_id, credential_ref)
    )
    try:
        await asyncio.shield(rollback_task)
    except asyncio.CancelledError as cancelled:
        await await_owned_task(rollback_task)
        raise cancelled


async def _require_credential_cleanup(
    service: "ModelHubService",
    source_id: str,
    credential_ref: str,
) -> None:
    """Refuse to settle when cleanup is neither complete nor durable."""

    if not await service._rollback_credential(source_id, credential_ref):
        raise CredentialCleanupUnsettledError


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
        await await_owned_task(replacement_task)
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
        model_ids=tuple(model.id for model in source.models if not model.retired),
    )


def _oauth_payload(
    flow: OAuthFlowState,
    *,
    channel: str,
    intent: Literal["create", "reauth"] = "create",
    client_nonce: str | None = None,
    expires_at_iso: str | None = None,
) -> dict:
    payload = {
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
        "expires_at": (
            expires_at_iso
            if client_nonce is not None
            else flow.expires_at_iso
        ),
    }
    if client_nonce is not None:
        payload["client_nonce"] = client_nonce
    return payload


def _runtime_payload(status: EngineStatus, *, enabled: bool) -> dict:
    # Import lazily to avoid the runtime adapter's dependency back on this service module.
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    manager = EngineRuntimeManager()
    return {
        "contract_version": 7,
        "enabled": enabled,
        "host_platform": status.host_platform or manager.host_platform(),
        "manifest": manager.contract_manifest(),
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
            "error_key": (
                status.error_key
                if status.health is EngineHealth.NOT_INSTALLED
                else None
            ),
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
        usage: Optional[BoundedUsageLedger] = None,
        native_oauth_adapter: Optional[NativeOAuthAdapter] = None,
        oauth_flows: Optional[OAuthFlowRegistry] = None,
        revocations: Optional[CredentialRevocationJournal] = None,
        migration_claude_oauth_probe: Optional[Callable[[], bool]] = None,
        migration_home: Optional[Path] = None,
        requested_model_override: Optional[Callable[[BackendName], Optional[str]]] = None,
        selected_agent_override: Optional[Callable[[BackendName], Optional[str]]] = None,
        named_agents_override: Optional[
            Callable[[BackendName], list[tuple[str, Optional[str]]]]
        ] = None,
        cli_present_override: Optional[Callable[[BackendName], bool]] = None,
        cli_presence_refresh: Optional[
            Callable[[bool, tuple[BackendName, ...] | None], None]
        ] = None,
        backend_catalog_changed: Optional[
            Callable[[BackendName], Awaitable[None]]
        ] = None,
        now: Callable[[], datetime] = _utc_now,
    ):
        self.store = store
        self.adapter = adapter
        self.events = events
        self.provenance = provenance or BoundedProvenanceStore(
            paths.get_state_dir() / "model_hub_turn_provenance.json"
        )
        self.usage = usage or BoundedUsageLedger(
            paths.get_state_dir() / "model_hub_usage.json",
            now=now,
        )
        # One writer per ledger, shared with the gateway: both metering populations
        # then have the same owner for the lifetime of what they write.
        self.usage_writer = UsageWriter(self.usage)
        self.native_oauth_adapter = native_oauth_adapter or UnavailableNativeOAuthAdapter()
        self.oauth_flows = oauth_flows or OAuthFlowRegistry(
            paths.get_state_dir() / "model_hub_oauth_flows.json",
            now=now,
        )
        self.revocations = revocations or CredentialRevocationJournal(
            paths.get_state_dir() / "model_hub_pending_revocations.json"
        )
        self.migration_claude_oauth_probe = migration_claude_oauth_probe
        self.migration_home = migration_home
        self.requested_model_override = requested_model_override
        self.selected_agent_override = selected_agent_override
        self.named_agents_override = named_agents_override
        self.cli_present_override = cli_present_override
        self.cli_presence_refresh = cli_presence_refresh
        self.backend_catalog_changed = backend_catalog_changed
        self.now = now
        self.native_source_ready: Callable[[BackendName, ModelHubSourceConfig], bool] = (
            lambda _backend, _source: True
        )
        self._mutation_lock = asyncio.Lock()
        self._oauth_start_tasks: dict[
            tuple[str, str, OAuthChannel], asyncio.Task[dict]
        ] = {}
        self._source_create_nonces: set[str] = set()
        self._next_settlement_generation = PRE_ATTEMPT_SETTLEMENT_GENERATION
        self._latest_source_attempt_generation: dict[str, int] = {}
        self._engine_synced = False
        self._engine_preparation_failed = False
        self._runtime_install_reconcile_lock = asyncio.Lock()
        self._runtime_install_reconciled = False
        self._runtime_lifecycle_lock = asyncio.Lock()
        self._builtin_snapshot_generations: dict[BackendName, str] = {}
        self._builtin_snapshot_cache: dict[BackendName, list[dict[str, Any]]] = {}
        self._pending_builtin_catalog_refresh: set[BackendName] = set()

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

    def _agent_model_ids(
        self,
        agent: ModelHubAgentSupplyConfig,
        requested_model: str,
    ) -> list[str]:
        primary = (
            [model.id for model in agent.models]
        )
        seen: set[str] = set()
        model_ids: list[str] = []
        for model_id in [*primary, requested_model, *agent.routes]:
            if model_id and model_id not in seen:
                seen.add(model_id)
                model_ids.append(model_id)
        return model_ids

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

    @staticmethod
    def _existing_native_source(
        config: ModelHubConfig,
        vendor: str,
    ) -> ModelHubSourceConfig | None:
        backend = _NATIVE_VENDOR_BACKENDS.get(vendor)
        if backend is None:
            return None
        return next(
            (
                source
                for source in config.sources
                if source.supply_channel == "native_cli"
                and source_eligible_for_backend(source, cast(BackendName, backend))
            ),
            None,
        )

    async def _engine_call(self, awaitable):
        try:
            return await awaitable
        except OriginNotAllowedError:
            raise ModelHubError("mode_switch_blocked", status=409) from None
        except ModelDiscoveryError:
            raise ModelHubError("discovery_failed", status=502) from None
        except RuntimePlatformUnsupportedError:
            raise ModelHubError("runtime_platform_unsupported", status=422) from None
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
        except BackendLoginInProgressError as error:
            raise ModelHubError(
                error.code,
                status=409,
                detail="modelHub.errors.native_login_in_progress",
            ) from None
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
            and any(not model.retired for model in source.models)
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

    def _save_projection_neutral(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
    ) -> ModelHubConfig:
        """Persist a synchronous mutation proven not to affect engine bindings."""

        if self._bindings(previous) != self._bindings(updated):
            raise AssertionError("projection-neutral mutation changed engine bindings")
        return self._save_config(updated)

    def _save_runtime_config(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
    ) -> bool:
        """Best-effort persistence for runtime state during config recovery."""

        try:
            self._save_projection_neutral(previous, updated)
        except ValueError as exc:
            if "recovery warnings" not in str(exc):
                raise
            logger.warning("Skipped Model Hub runtime-state persistence during config recovery")
            return False
        return True

    def _reserve_settlement_generation(self, source_id: str) -> int:
        self._next_settlement_generation += 1
        self._latest_source_attempt_generation[source_id] = (
            self._next_settlement_generation
        )
        return self._next_settlement_generation

    def _ensure_config_writable(self) -> None:
        ensure_writable = getattr(self.store, "ensure_writable", None)
        if callable(ensure_writable):
            ensure_writable()

    def _claim_source_create_nonce_locked(self, client_nonce: str) -> None:
        if any(
            source.client_nonce == client_nonce
            for source in self.store.load().sources
        ):
            raise ModelHubError("source_nonce_conflict", status=409)
        if client_nonce in self._source_create_nonces:
            raise ModelHubError("source_create_in_progress", status=409)
        self._source_create_nonces.add(client_nonce)

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

    async def _commit_synced(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
        *,
        rollback_on_sync_failure: bool = True,
    ) -> None:
        """Persist the authoritative config before updating its engine projection."""

        updated = ModelHubConfig.from_payload(updated.to_payload())
        previous_bindings = self._bindings(previous)
        updated_bindings = self._bindings(updated)
        # SourceBinding order is the engine-config serialization order. Agent
        # Route hop order is not part of this projection, so a pure chain reorder
        # compares equal and does not restart a healthy engine.
        if previous_bindings == updated_bindings:
            self._save_config(updated)
            return
        self._engine_synced = False
        self._save_config(updated)
        try:
            await self._sync_sources(updated, force_empty=bool(previous_bindings))
        except asyncio.CancelledError:
            # The config write is the persistence boundary. A cancelled sync is
            # reconciled by the next demand and must not roll back a saved ref.
            self._engine_synced = False
            raise
        except Exception:
            if not rollback_on_sync_failure:
                raise
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

    async def reconcile_runtime_installation(self) -> EngineStatus | None:
        if self._runtime_install_reconciled:
            return None
        async with self._runtime_install_reconcile_lock:
            if self._runtime_install_reconciled:
                return None
            recover = getattr(self.adapter, "recover_installation", None)
            recovered = None
            if callable(recover):
                recovered = await self._engine_call(recover())
            self._runtime_install_reconciled = True
            return recovered if isinstance(recovered, EngineStatus) else None

    async def recover_runtime_intent(self) -> None:
        """Restore the runtime only when the user left it enabled."""

        async with self._runtime_lifecycle_lock:
            await self.reconcile_runtime_installation()
            if not self.store.load().enabled:
                return
            await self._prepare_engine_for_demand()
            await self._engine_call(self.adapter.start())

    async def stop(self) -> None:
        async with self._runtime_lifecycle_lock:
            await self.adapter.stop()

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

    @staticmethod
    def _cancelled_oauth_flow(
        flow_id: str,
        binding: OAuthFlowBinding,
    ) -> OAuthFlowState:
        if binding.source_id is None or binding.vendor is None:
            raise ModelHubError("flow_not_found", status=404)
        return OAuthFlowState(
            flow_id=flow_id,
            source_id=binding.source_id,
            vendor=binding.vendor,
            state="cancelled",
            auth_url=None,
            device_code=None,
            expects="none",
            instructions_key=None,
            error_key=None,
            expires_at_iso=binding.expires_at_iso,
            credential_ref=None,
            channel=binding.channel,
        )

    @staticmethod
    def _flow_payload(
        flow: OAuthFlowState,
        binding: OAuthFlowBinding,
    ) -> dict:
        return _oauth_payload(
            flow,
            channel=binding.channel,
            intent=binding.intent,
            client_nonce=binding.client_nonce,
            expires_at_iso=binding.expires_at_iso,
        )

    async def _replay_nonce_flow(self, flow_id: str) -> dict:
        binding = self._oauth_binding(flow_id)
        if binding.terminal_state == "cancelled":
            flow = self._cancelled_oauth_flow(flow_id, binding)
        else:
            flow = await self._oauth_status(flow_id, binding.channel)
            self._raise_if_flow_expired(flow_id, flow)
            flow, repair_result = await self._materialize_completed_oauth(
                flow_id,
                binding,
                flow,
            )
            if repair_result is not None:
                return {
                    "flow": self._flow_payload(flow, binding),
                    **repair_result,
                }
        return self._oauth_result(flow_id, flow)

    async def _discard_started_oauth_flow(
        self,
        flow: OAuthFlowState,
        channel: OAuthChannel,
    ) -> bool:
        if channel == "hub":
            await self._discard_unbound_hub_flow(flow)
            return True
        try:
            await self.native_oauth_adapter.cancel_oauth(flow.flow_id)
        except Exception:
            return False
        return True

    async def _discover(self, source: ModelHubSourceConfig) -> list[DiscoveredModel]:
        if not source.credential_ref:
            return [
                DiscoveredModel(id=model.id)
                for model in source.models
                if not model.retired
            ]
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
    def _observation_protocols(
        vendor: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, ...]:
        pinned_protocol = pinned_api_key_protocol(vendor)
        if "protocol" not in payload:
            if pinned_protocol is not None:
                return (pinned_protocol,)
            if vendor == "custom":
                return SOURCE_PROTOCOLS
            raise ModelHubError("discovery_failed")
        requested = payload.get("protocol")
        if not isinstance(requested, str) or requested not in SOURCE_PROTOCOLS:
            raise ModelHubError("discovery_failed")
        if pinned_protocol is not None and requested != pinned_protocol:
            raise ModelHubError("discovery_failed")
        return (requested,)

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
            "model_metadata": [
                {
                    "id": model.id,
                    "supported_parameters": (
                        list(model.supported_parameters)
                        if model.supported_parameters is not None
                        else None
                    ),
                }
                for model in observation.models
            ],
        }

    @staticmethod
    def _validate_observation(observation: SourceObservation) -> SourceObservation:
        try:
            validated = validate_source_observation(observation)
        except (TypeError, ValueError):
            raise ModelHubError("discovery_failed", status=502)
        if contains_credential_material(
            [
                {
                    "id": model.id,
                    "supported_parameters": model.supported_parameters,
                }
                for model in validated.models
            ]
        ):
            raise ModelHubError("discovery_failed", status=502)
        return validated

    async def _observe_provisioned_credential(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order: tuple[str, ...],
    ) -> SourceObservation:
        try:
            observation = await self.adapter.observe_source(
                vendor,
                base_url,
                credential_ref,
                protocol_order,
            )
        except asyncio.TimeoutError:
            observation = make_source_observation(
                outcome=ObservationOutcome.TIMEOUT,
                reachable=None,
                authenticated=None,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        except EngineUnavailableError:
            raise ModelHubError("engine_down", status=503) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            observation = make_source_observation(
                outcome=ObservationOutcome.ADAPTER_ERROR,
                reachable=None,
                authenticated=None,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        validated = self._validate_observation(observation)
        if (
            len(protocol_order) == 1
            and validated.protocol is not None
            and validated.protocol != protocol_order[0]
        ):
            raise ModelHubError("discovery_failed", status=502)
        return validated

    async def _require_proven_observation(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order: tuple[str, ...],
    ) -> SourceObservation:
        """Require a persistable protocol owner before Source persistence."""

        observation = await self._observe_provisioned_credential(
            vendor,
            base_url,
            credential_ref,
            protocol_order,
        )
        if (
            observation.outcome is not ObservationOutcome.OBSERVED
            or observation.protocol is None
        ):
            detail_by_outcome = {
                ObservationOutcome.AMBIGUOUS: "modelHub.errors.ambiguous_source",
                ObservationOutcome.UNREACHABLE: "modelHub.errors.source_unreachable",
                ObservationOutcome.AUTHENTICATION_FAILED: "modelHub.errors.authentication_failed",
                ObservationOutcome.ADAPTER_ERROR: "modelHub.errors.observation_failed",
                ObservationOutcome.TIMEOUT: "modelHub.errors.source_timeout",
            }
            # An adapter error may carry authoritative reachability (for example,
            # a reachable endpoint returning an unsupported response), or no
            # reachability evidence at all after an unclassified adapter failure.
            # Only the former justifies copy that says we connected.
            detail = detail_by_outcome.get(observation.outcome)
            if (
                observation.outcome is ObservationOutcome.ADAPTER_ERROR
                and observation.reachable is None
            ):
                detail = "modelHub.errors.adapter_error"
            raise ModelHubError(
                "discovery_failed",
                status=422,
                detail=detail,
                data={"observation": self._observation_payload(observation)},
            )
        return observation

    async def _observe_source_payload(
        self,
        payload: Mapping[str, Any],
        *,
        require_proven: bool = False,
    ) -> SourceObservation:
        if set(payload) - {"vendor", "base_url", "key", "protocol"}:
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
        protocol_order = self._observation_protocols(vendor, payload)
        transient_ref = await _provision_transient_credential_with_cancellation_ownership(
            self,
            vendor,
            key.strip(),
            base_url,
        )
        try:
            if require_proven:
                return await self._require_proven_observation(
                    vendor,
                    base_url,
                    transient_ref,
                    protocol_order,
                )
            return await self._observe_provisioned_credential(
                vendor,
                base_url,
                transient_ref,
                protocol_order,
            )
        finally:
            await _rollback_credential_before_settling(
                self,
                "observation",
                transient_ref,
            )

    async def _require_proven_source_payload(
        self,
        payload: Mapping[str, Any],
    ) -> SourceObservation:
        return await self._observe_source_payload(payload, require_proven=True)

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
            await _require_credential_cleanup(
                self,
                source_id,
                replacement_ref,
            )

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

    async def _mark_same_handle_reauth_needs_action(self, source_id: str) -> None:
        # The engine may replace OAuth material behind the same opaque ref.
        # Without an old snapshot, fail closed instead of restoring stale supply.
        previous = self.store.load()
        config = self._clone_config(previous)
        source = self._source(config, source_id)
        source.models = [
            model
            for model in source.models
            if model.provenance == "manual" or model.retired
        ]
        source.state = ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.oauth_expired",
        )
        await self._commit_synced(
            previous,
            config,
            rollback_on_sync_failure=False,
        )

    async def _discard_unbound_hub_flow(self, flow: OAuthFlowState) -> None:
        if flow.credential_ref:
            await _require_credential_cleanup(
                self,
                flow.source_id,
                flow.credential_ref,
            )
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
        discovered: list[DiscoveredModel],
        *,
        allow_empty: bool = False,
        catalog_efforts_by_model: Mapping[str, tuple[str, ...]] | None = None,
    ) -> list[tuple[str, Literal["upstream", "catalog"]]]:
        # Admit the canonical form, not the text upstream happened to send: what
        # is stored here is what resolution compares and what usage meters, and a
        # listing that names one model twice under two spellings is a failed
        # discovery, not two models.
        canonical = [canonical_model_id(model.id) for model in discovered]
        if (
            (not allow_empty and not discovered and not manual_models)
            or any(
                model_id is None or contains_credential_material(model_id)
                for model_id in canonical
            )
            or len(set(canonical)) != len(canonical)
        ):
            raise ModelHubError("discovery_failed", status=502)
        canonical_models = [
            DiscoveredModel(
                id=model_id,
                supported_parameters=model.supported_parameters,
            )
            for model, model_id in zip(discovered, canonical)
            if model_id is not None
        ]
        discovered_at = self.now().isoformat()
        manual_model_ids = {model.id for model in manual_models}
        existing_by_id = {model.id: model for model in source.models}
        retired_models = [
            model
            for model in source.models
            if model.provenance == "discovered" and model.retired
        ]
        retired_model_ids = {model.id for model in retired_models}
        discovered_by_id = {model.id: model for model in canonical_models}
        overrides: list[tuple[str, Literal["upstream", "catalog"]]] = []
        if catalog_efforts_by_model is None:
            catalog_efforts_by_model = bundled_catalog_reasoning_efforts_by_model()

        def apply_tiers(
            model: ModelHubModelConfig,
            metadata: DiscoveredModel | None,
        ) -> None:
            resolution = resolve_reasoning_tiers(
                protocol=source.protocol,
                model_id=model.id,
                supported_parameters=(
                    metadata.supported_parameters if metadata is not None else None
                ),
                existing_efforts=model.reasoning_efforts,
                existing_source=model.reasoning_efforts_source,
                catalog_efforts_by_model=catalog_efforts_by_model,
            )
            if (
                model.reasoning_efforts
                and model.reasoning_efforts_source == "user"
                and resolution.source in {"upstream", "catalog"}
            ):
                overrides.append((model.id, resolution.source))
            model.reasoning_efforts = list(resolution.efforts)
            model.reasoning_efforts_source = resolution.source

        discovered_models = []
        for metadata in canonical_models:
            model_id = metadata.id
            if model_id in manual_model_ids or model_id in retired_model_ids:
                continue
            existing = existing_by_id.get(model_id)
            model = ModelHubModelConfig(
                id=model_id,
                provenance="discovered",
                reasoning_efforts=list(existing.reasoning_efforts) if existing else [],
                reasoning_efforts_source=(
                    existing.reasoning_efforts_source if existing else None
                ),
                display_name=existing.display_name if existing else None,
                discovered_at=discovered_at,
            )
            apply_tiers(model, metadata)
            discovered_models.append(model)

        for model in (*retired_models, *manual_models):
            apply_tiers(model, discovered_by_id.get(model.id))
        source.models = discovered_models + retired_models + manual_models
        source.last_discovered_at = discovered_at
        return overrides

    def _record_reasoning_tier_overrides(
        self,
        source: ModelHubSourceConfig,
        overrides: Iterable[tuple[str, Literal["upstream", "catalog"]]],
    ) -> None:
        for model_id, managed_source in overrides:
            self._record_event(
                agent="system",
                kind="reasoning_efforts_override",
                model_id=model_id,
                reason=f"{managed_source}_tiers",
                from_source=source.id,
                from_label=source.display_name,
                now=self.now(),
            )

    async def _finalize_successful_discovery(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
        source: ModelHubSourceConfig,
        discovered: list[DiscoveredModel],
        *,
        force: bool,
        confirmed_remove_hops: object,
        confirmed_interruptions: object,
    ) -> tuple[list[dict], list[dict]]:
        """Apply one successful inventory observation and commit it atomically."""

        manual = [model for model in source.models if model.provenance == "manual"]
        overrides = self._apply_discovered_models(
            source,
            manual,
            discovered,
            allow_empty=True,
        )
        source.state = ModelHubSourceStateConfig(status="standby")
        removed_hops, interrupted = self._guard_inventory_mutation(
            previous,
            updated,
            source.id,
            force=force,
            confirmed_remove_hops=confirmed_remove_hops,
            confirmed_interruptions=confirmed_interruptions,
        )
        await self._commit_synced(previous, updated)
        self._record_reasoning_tier_overrides(source, overrides)
        return removed_hops, interrupted

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

    def _matching_menu_model_hops(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        menu_model: str,
    ) -> tuple[ModelHubRouteHopConfig, ...]:
        """Project matching-v1 in placement-v1 order for one menu model."""

        agent = config.agents[backend]
        source_by_id = {source.id: source for source in config.sources}
        hops: list[ModelHubRouteHopConfig] = []
        checked_models = self._agent_menu_model_ids(agent)
        if menu_model not in checked_models:
            checked_models = (*checked_models, menu_model)
        for source_id in agent.sources.order:
            source = source_by_id.get(source_id)
            if source is None or not self._eligible_for_agent(source, backend):
                continue
            matched_model = _matching_v1_model_id(
                backend=backend,
                requested_model=menu_model,
                source=source,
                checked_models=checked_models,
                include_manual=True,
            )
            if matched_model is not None:
                hops.append(ModelHubRouteHopConfig(source.id, matched_model))
        return tuple(hops)

    def _seed_menu_model_route(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        menu_model: str,
    ) -> None:
        """Apply matching-v1 and placement-v1 once for a newly added menu row."""

        config.agents[backend].routes[menu_model] = ModelHubRouteConfig(
            hops=self._matching_menu_model_hops(config, backend, menu_model)
        )

    @staticmethod
    def _agent_menu_model_ids(agent: ModelHubAgentSupplyConfig) -> tuple[str, ...]:
        return tuple(model.id for model in agent.models)

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

    def _adopted_by(
        self,
        source_id: str,
        config: ModelHubConfig | None = None,
    ) -> list[dict]:
        config = config or self.store.load()
        result = [
            {"backend": backend, "menu_model": menu_model}
            for backend in MODEL_HUB_BACKENDS
            if config.agents[backend].mode == "hub"
            for menu_model, route in config.agents[backend].routes.items()
            if any(hop.source_id == source_id for hop in route.hops)
        ]
        result.sort(key=lambda item: (item["backend"], item["menu_model"]))
        return result

    def _source_payload(
        self,
        source: ModelHubSourceConfig,
        config: ModelHubConfig | None = None,
    ) -> dict:
        payload = source.to_payload()
        for model in payload["models"]:
            model.setdefault("retired", False)
        payload["adopted_by"] = self._adopted_by(source.id, config)
        return payload

    def _source_creation_result(self, source: dict) -> dict:
        source = dict(source)
        for model in source["models"]:
            model.setdefault("retired", False)
        source = {
            **source,
            "adopted_by": self._adopted_by(source["id"]),
        }
        return {
            "source": source,
            "added_to": self._added_to(source["id"]),
            "adopted_by": self._adopted_by(source["id"]),
        }

    async def _create_oauth_source(
        self,
        manual_models: list[ModelHubModelConfig],
        *,
        display_name: str,
        billing: Literal["monthly", "metered"],
        created_at: str,
        oauth_ref: str,
        channel: Literal["native_cli", "hub"],
        vendor: str,
        completed_flow: Optional[OAuthFlowState] = None,
        idempotent: bool = False,
    ) -> dict:
        # Claim and consume a completed flow under the aggregate lock. This
        # prevents a duplicate browser retry from revoking the winning source's
        # credential while still retaining rollback ownership before discovery.
        self._ensure_config_writable()
        async with self._mutation_lock:
            rollback_credential_ref: Optional[str] = None
            source_id = ""
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

                source_id = flow.source_id
                previous = self.store.load()
                existing = next((item for item in previous.sources if item.id == source_id), None)
                if idempotent and existing is not None and self._source_matches_binding(existing, binding):
                    try:
                        self.oauth_flows.complete(oauth_ref)
                    except (KeyError, OSError):
                        pass
                    return existing.to_payload()
                if existing is not None:
                    raise ModelHubError("migration_item_conflict", status=409)
                if channel == "native_cli":
                    occupied = self._existing_native_source(previous, vendor)
                    if occupied is not None:
                        # A second provider flow can finish after the first one
                        # committed. Re-check under the mutation lock and clean
                        # the losing native flow before surfacing the singleton
                        # conflict, so it cannot materialize a second Source.
                        cleanup_confirmed = await self._discard_started_oauth_flow(
                            flow,
                            channel,
                        )
                        if not cleanup_confirmed:
                            raise ModelHubError("engine_down", status=503)
                        try:
                            self.oauth_flows.forget(oauth_ref)
                        except OSError:
                            pass
                        raise ModelHubError(
                            "native_source_already_exists",
                            status=409,
                            detail="modelHub.errors.native_subscription_exists",
                            data={"existing_source_id": occupied.id},
                        )
                account_label: str | None = None
                state = ModelHubSourceStateConfig(status="standby")
                discovered: list[DiscoveredModel] | None
                if channel == "hub":
                    rollback_credential_ref = cast(str, flow.credential_ref)
                    observation = await self._require_proven_observation(
                        vendor,
                        None,
                        rollback_credential_ref,
                        SOURCE_PROTOCOLS,
                    )
                    protocol = cast(
                        Literal["anthropic", "openai_responses", "openai_chat"],
                        observation.protocol,
                    )
                    if observation.discovery is ObservationDiscovery.SUCCEEDED:
                        discovered = list(observation.models)
                    elif observation.discovery is ObservationDiscovery.FAILED:
                        discovered = None
                        state = ModelHubSourceStateConfig(
                            status="error",
                            detail_key="models.source.error.unclassified",
                        )
                    else:
                        raise ModelHubError("discovery_failed", status=502)
                else:
                    backend = _NATIVE_VENDOR_BACKENDS.get(vendor)
                    protocol = (
                        _FIXED_BACKEND_PROTOCOLS.get(backend)
                        if backend is not None
                        else None
                    )
                    if protocol is None:
                        raise ModelHubError("discovery_failed")
                    try:
                        source_status = self.native_oauth_adapter.completed_source_status(oauth_ref)
                    except KeyError:
                        raise ModelHubError("flow_not_found", status=404) from None
                    except NativeOAuthUnavailableError:
                        raise ModelHubError("engine_down", status=503) from None
                    except Exception:
                        raise ModelHubError("engine_down", status=503) from None
                    account_label = source_status.account_label
                    state = (
                        ModelHubSourceStateConfig(status="standby")
                        if source_status.signed_in
                        else ModelHubSourceStateConfig(
                            status="needs_action",
                            detail_key="models.source.needs_action.oauth_expired",
                        )
                    )
                    discovered = [
                        DiscoveredModel(id=model_id)
                        for model_id in _native_model_ids(vendor)
                    ]
                if channel == "native_cli" and not discovered:
                    raise ModelHubError("discovery_failed")
                source_payload: dict[str, Any] = {
                    "id": source_id,
                    "created_at": created_at,
                    "kind": "subscription",
                    "vendor": vendor,
                    "display_name": display_name,
                    "protocol": protocol,
                    "base_url": None,
                    "supply_channel": channel,
                    "billing": billing,
                    "state": state.to_payload(),
                    "usage": ModelHubSourceUsageConfig().to_payload(),
                    "models": [model.to_payload() for model in manual_models],
                }
                if rollback_credential_ref is not None:
                    source_payload["credential_ref"] = rollback_credential_ref
                if account_label is not None:
                    source_payload["account_label"] = account_label
                try:
                    source = ModelHubSourceConfig.from_payload(source_payload)
                except (TypeError, ValueError):
                    raise ModelHubError("discovery_failed") from None
                if discovered is not None:
                    self._apply_discovered_models(
                        source,
                        manual_models,
                        discovered,
                        allow_empty=channel == "hub",
                    )
                else:
                    catalog_efforts_by_model = (
                        bundled_catalog_reasoning_efforts_by_model()
                    )
                    for model in source.models:
                        resolution = resolve_reasoning_tiers(
                            protocol=source.protocol,
                            model_id=model.id,
                            existing_efforts=model.reasoning_efforts,
                            existing_source=model.reasoning_efforts_source,
                            catalog_efforts_by_model=catalog_efforts_by_model,
                        )
                        model.reasoning_efforts = list(resolution.efforts)
                        model.reasoning_efforts_source = resolution.source
                try:
                    source = ModelHubSourceConfig.from_payload(source.to_payload())
                except (TypeError, ValueError):
                    raise ModelHubError("discovery_failed") from None
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
                    source_id,
                    rollback_credential_ref,
                )
                if rollback_credential_ref is not None and not persisted:
                    await _rollback_credential_before_settling(
                        self,
                        source_id,
                        rollback_credential_ref,
                    )
                    try:
                        self.oauth_flows.forget(oauth_ref)
                    except OSError:
                        pass
                raise
            except Exception:
                if rollback_credential_ref is not None and not persisted:
                    await _require_credential_cleanup(
                        self,
                        source_id,
                        rollback_credential_ref,
                    )
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
            "source": self._source_payload(source),
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
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            source.models = [
                model
                for model in source.models
                if model.provenance == "manual" or model.retired
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
            await self._commit_synced(previous, config)
            return self._would_interrupt(config)

    async def _materialize_reauth(
        self,
        flow_id: str,
        binding: OAuthFlowBinding,
        flow: OAuthFlowState,
    ) -> dict:
        self._ensure_config_writable()
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
                previous = self.store.load()
                config = self._clone_config(previous)
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
                discovered = [
                    DiscoveredModel(id=model_id)
                    for model_id in _native_model_ids(binding.vendor)
                ]
                if not discovered:
                    source.models = [
                        model
                        for model in source.models
                        if model.provenance == "manual" or model.retired
                    ]
                    source.state = ModelHubSourceStateConfig(
                        status="error",
                        detail_key="models.source.error.unclassified",
                    )
                    await self._commit_synced(previous, config)
                    interrupted_pairs = self._would_interrupt(config)
                    raise ModelHubError(
                        "discovery_failed",
                        status=502,
                        data={"interrupted_pairs": interrupted_pairs},
                    )
                overrides = self._apply_discovered_models(source, manual, discovered)
                await self._commit_synced(previous, config)
                self._record_reasoning_tier_overrides(source, overrides)
                interrupted_pairs = self._would_interrupt(config)
                self._complete_reauth_flow(
                    flow_id,
                    binding,
                    interrupted_pairs,
                )
                return {
                    "source": self._source_payload(source),
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
                overrides = self._apply_discovered_models(source, manual, discovered)
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
                self._record_reasoning_tier_overrides(source, overrides)
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
                            await self._mark_same_handle_reauth_needs_action(source.id)
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
                            await _require_credential_cleanup(
                                self,
                                binding.source_id,
                                replacement_ref,
                            )
                        elif replacement_ref == old_credential_ref:
                            await self._mark_same_handle_reauth_needs_action(source.id)
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
                "source": self._source_payload(source),
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

    async def _fail_closed_hub_reauth(
        self,
        binding: OAuthFlowBinding,
        *,
        config: ModelHubConfig | None = None,
    ) -> ModelHubConfig:
        previous = self._clone_config(config or self.store.load())
        config = self._clone_config(previous)
        if binding.source_id is None:
            raise ModelHubError("flow_not_found", status=404)
        source = self._source(config, binding.source_id)
        if not self._source_matches_binding(source, binding):
            raise ModelHubError("flow_not_found", status=404)
        source.models = [
            model
            for model in source.models
            if model.provenance == "manual" or model.retired
        ]
        source.state = ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.oauth_expired",
        )
        await self._commit_synced(
            previous,
            config,
            rollback_on_sync_failure=False,
        )
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
            return await self._fail_closed_hub_reauth(
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
            await _require_credential_cleanup(
                self,
                binding.source_id,
                flow.retained_credential_ref,
            )
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
        await self._create_oauth_source(
            [],
            display_name=binding.vendor,
            billing="monthly",
            created_at=self.now().isoformat(),
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
        return [self._source_payload(source, config) for source in config.sources]

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
            "protocol",
            "client_nonce",
            "accept_unavailable_inventory",
        }:
            raise ModelHubError("discovery_failed")
        forbidden = {
            "id",
            "credential_ref",
            "account_label",
            "masked_credential",
            "state",
            "usage",
            "created_at",
            "last_discovered_at",
        } & set(payload)
        if forbidden:
            raise ModelHubError("discovery_failed")
        kind = payload.get("kind")
        vendor = payload.get("vendor")
        try:
            vendor = normalize_model_hub_vendor_id(vendor)
        except ValueError:
            raise ModelHubError("discovery_failed") from None
        display_name = payload.get("display_name") or catalog_api_key_vendor_label(vendor) or vendor
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
                or model.reasoning_efforts_source not in {None, "user"}
                # The same admission rule the manual-add surface applies. A source
                # may be created with its models inline, so this is the other way a
                # client-declared identifier enters config — and a client can still
                # be told no, which is the one moment an unbounded identifier is
                # refusable rather than something every later surface must carry.
                or canonical_model_id(model.id) is None
                or contains_credential_material(model.id)
                or contains_credential_material(model.display_name or "")
                for model in manual_models
            ):
                raise ValueError("Client-declared source models must use manual provenance")
        except (TypeError, ValueError):
            raise ModelHubError("discovery_failed") from None

        credential_value = payload.get("key")
        oauth_ref = payload.get("oauth_flow_ref")
        try:
            client_nonce = (
                validate_model_hub_source_client_nonce(payload.get("client_nonce"))
                if "client_nonce" in payload
                else None
            )
        except ValueError:
            raise ModelHubError("discovery_failed") from None
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
        if kind != "api_key" and client_nonce is not None:
            raise ModelHubError("discovery_failed")
        accept_unavailable_inventory = payload.get("accept_unavailable_inventory", False)
        if (
            not isinstance(accept_unavailable_inventory, bool)
            or (
                kind != "api_key"
                and (
                    "protocol" in payload
                    or "accept_unavailable_inventory" in payload
                )
            )
        ):
            raise ModelHubError("discovery_failed")
        protocol_order: tuple[str, ...] | None = None
        if kind == "api_key":
            protocol_order = self._observation_protocols(vendor, payload)

        if oauth_ref:
            return self._source_creation_result(
                await self._create_oauth_source(
                    manual_models,
                    display_name=display_name,
                    billing=cast(Literal["monthly", "metered"], billing),
                    created_at=self.now().isoformat(),
                    oauth_ref=oauth_ref,
                    channel=cast(Literal["native_cli", "hub"], channel),
                    vendor=vendor,
                )
            )
        if kind == "subscription":
            raise ModelHubError("flow_not_found", status=404)

        assert protocol_order is not None
        # Validate the complete client-controlled Source shape before nonce
        # state can mask a malformed request. Probe results replace only the
        # provisional protocol and server-derived model state below.
        try:
            source = ModelHubSourceConfig.from_payload(
                ModelHubSourceConfig(
                    id=_source_id(),
                    kind=kind,
                    vendor=vendor,
                    display_name=display_name,
                    protocol=cast(
                        Literal["anthropic", "openai_responses", "openai_chat"],
                        protocol_order[0],
                    ),
                    base_url=base_url,
                    supply_channel=channel,
                    billing=billing,
                    state=ModelHubSourceStateConfig(status="standby"),
                    usage=ModelHubSourceUsageConfig(),
                    models=manual_models,
                    created_at=self.now().isoformat(),
                    client_nonce=client_nonce,
                    credential_ref="cred_preflight",
                ).to_payload()
            )
        except (TypeError, ValueError):
            raise ModelHubError("discovery_failed") from None

        nonce_claimed = False
        release_nonce = True
        try:
            if client_nonce is not None:
                async with self._mutation_lock:
                    self._claim_source_create_nonce_locked(client_nonce)
                    nonce_claimed = True
            if self.revocations.list():
                await self._ensure_engine_synced()
            observation_payload: dict[str, Any] = {
                "vendor": vendor,
                "base_url": base_url,
                "key": credential_value,
            }
            if "protocol" in payload:
                observation_payload["protocol"] = payload["protocol"]
            observation = await self._require_proven_source_payload(observation_payload)
            if (
                observation.discovery is ObservationDiscovery.FAILED
                and not accept_unavailable_inventory
            ):
                raise ModelHubError(
                    "discovery_failed",
                    status=422,
                    detail="modelHub.errors.inventory_unavailable",
                    data={"observation": self._observation_payload(observation)},
                )
            source.protocol = cast(
                Literal["anthropic", "openai_responses", "openai_chat"],
                observation.protocol,
            )
            if observation.discovery is ObservationDiscovery.SUCCEEDED:
                self._apply_discovered_models(
                    source,
                    manual_models,
                    list(observation.models),
                    allow_empty=True,
                )
            elif observation.discovery is ObservationDiscovery.FAILED:
                catalog_efforts_by_model = bundled_catalog_reasoning_efforts_by_model()
                for model in source.models:
                    resolution = resolve_reasoning_tiers(
                        protocol=source.protocol,
                        model_id=model.id,
                        existing_efforts=model.reasoning_efforts,
                        existing_source=model.reasoning_efforts_source,
                        catalog_efforts_by_model=catalog_efforts_by_model,
                    )
                    model.reasoning_efforts = list(resolution.efforts)
                    model.reasoning_efforts_source = resolution.source
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

            rollback_credential_ref = await (
                _acquire_credential_ref_with_cancellation_ownership(
                    self,
                    self.adapter.provision_credential(
                        vendor,
                        source.protocol,
                        cast(str, credential_value),
                        source.base_url,
                    ),
                    source.id,
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
                    await _require_credential_cleanup(
                        self,
                        source.id,
                        rollback_credential_ref,
                    )
                raise
        except CredentialCleanupUnsettledError:
            release_nonce = False
            raise
        finally:
            if nonce_claimed and release_nonce:
                self._source_create_nonces.discard(cast(str, client_nonce))

    async def replace_credential(self, source_id: str, payload: object) -> dict:
        if (
            not isinstance(payload, dict)
            or set(payload)
            - {"key", "force", "would_remove_hops", "would_interrupt"}
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
                discovered = await self._discover(source)
                if old_credential_ref != replacement_ref:
                    self.revocations.add(source.id, old_credential_ref)
                    old_revocation_recorded = True
                removed_hops, interrupted = await self._finalize_successful_discovery(
                    previous,
                    config,
                    source,
                    discovered,
                    force=force,
                    confirmed_remove_hops=payload.get("would_remove_hops"),
                    confirmed_interruptions=payload.get("would_interrupt"),
                )
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
                "source": self._source_payload(source),
                "removed_hops": removed_hops,
                "interrupted": interrupted,
            }

    async def patch_source(self, source_id: str, payload: dict) -> dict:
        if (
            not isinstance(payload, dict)
            or set(payload)
            - {
                "display_name",
                "base_url",
                "force",
                "would_remove_hops",
                "would_interrupt",
            }
            or ("force" in payload and not isinstance(payload["force"], bool))
        ):
            raise ModelHubError("discovery_failed")
        base_url = _validated_base_url(payload.get("base_url")) if "base_url" in payload else None
        force = payload.get("force") is True
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            removed_hops: list[dict] = []
            interrupted: list[dict] = []
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
            base_url_changed = (
                "base_url" in payload and source.base_url != base_url
            )
            if base_url_changed:
                if (
                    source.kind != "api_key"
                    or source.supply_channel != "hub"
                    or not source.credential_ref
                ):
                    raise ModelHubError("discovery_failed")
                old_credential_ref = source.credential_ref
                replacement_ref = await (
                    _acquire_credential_ref_with_cancellation_ownership(
                        self,
                        self.adapter.retarget_api_key_credential(
                            old_credential_ref,
                            source.vendor,
                            source.protocol,
                            base_url,
                        ),
                        source.id,
                    )
                )
                committed = False
                old_revocation_recorded = False
                try:
                    source.credential_ref = replacement_ref
                    source.base_url = base_url
                    discovered = await self._discover(source)
                    self.revocations.add(source.id, old_credential_ref)
                    old_revocation_recorded = True
                    removed_hops, interrupted = await self._finalize_successful_discovery(
                        previous,
                        config,
                        source,
                        discovered,
                        force=force,
                        confirmed_remove_hops=payload.get("would_remove_hops"),
                        confirmed_interruptions=payload.get("would_interrupt"),
                    )
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

                try:
                    await self.adapter.revoke_credential(old_credential_ref)
                except Exception:
                    pass
                else:
                    try:
                        self.revocations.remove(source.id, old_credential_ref)
                    except OSError:
                        pass
            else:
                await self._commit_synced(previous, config)
            return {
                "source": self._source_payload(source),
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
        for model in agent.models:
            add(model.id)
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
                "position": position,
            }
            for backend in MODEL_HUB_BACKENDS
            for menu_model, route in config.agents[backend].routes.items()
            for position, hop in enumerate(route.hops, start=1)
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
        confirmed_remove_hops: object,
        confirmed_interruptions: object,
    ) -> tuple[list[dict], list[dict]]:
        would_remove_hops = self._invalidated_route_hops(updated, source_id)
        would_interrupt = self._introduced_interruptions(previous, updated)
        self._require_guard_plan(
            force=force,
            confirmed_remove_hops=confirmed_remove_hops,
            confirmed_interruptions=confirmed_interruptions,
            would_remove_hops=would_remove_hops,
            would_interrupt=would_interrupt,
            error=(
                "source_model_in_route_chain"
                if would_remove_hops
                else "source_last_supplier"
            ),
        )
        if would_remove_hops:
            self._prune_invalidated_route_hops(updated, would_remove_hops)
        return would_remove_hops, would_interrupt

    @staticmethod
    def _require_guard_plan(
        *,
        force: bool,
        confirmed_remove_hops: object,
        confirmed_interruptions: object,
        would_remove_hops: list[dict],
        would_interrupt: list[dict],
        error: str,
    ) -> None:
        if not (would_remove_hops or would_interrupt):
            return
        if (
            force
            and _same_json_value(confirmed_remove_hops, would_remove_hops)
            and _same_json_value(confirmed_interruptions, would_interrupt)
        ):
            return
        raise ModelHubError(
            error,
            status=409,
            data={
                "would_remove_hops": would_remove_hops,
                "would_interrupt": would_interrupt,
            },
        )

    async def delete_source(
        self,
        source_id: str,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            removed_hops = [
                {
                    "backend": backend,
                    "menu_model": model_id,
                    "source_id": source_id,
                    "model_id": hop.model_id,
                    "position": position,
                }
                for backend in MODEL_HUB_BACKENDS
                for model_id, route in config.agents[backend].routes.items()
                for position, hop in enumerate(route.hops, start=1)
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
            self._require_guard_plan(
                force=force,
                confirmed_remove_hops=confirmed_remove_hops,
                confirmed_interruptions=confirmed_interruptions,
                would_remove_hops=removed_hops,
                would_interrupt=would_interrupt,
                error=(
                    "source_in_route_chain"
                    if removed_hops
                    else "source_last_supplier"
                ),
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

    async def refresh_source(
        self,
        source_id: str,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            if source.supply_channel == "native_cli":
                raise ModelHubError("discovery_failed")
            try:
                model_ids = await self._discover(source)
                removed_hops, would_interrupt = await self._finalize_successful_discovery(
                    previous,
                    config,
                    source,
                    model_ids,
                    force=force,
                    confirmed_remove_hops=confirmed_remove_hops,
                    confirmed_interruptions=confirmed_interruptions,
                )
            except ModelHubError as exc:
                if exc.code != "discovery_failed":
                    raise
                source.state = ModelHubSourceStateConfig(
                    status="error",
                    detail_key="models.source.error.unclassified",
                )
                await self._commit_synced(previous, config)
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
            return {
                "source": self._source_payload(source),
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

    def _validate_agent_source_order(
        self,
        config: ModelHubConfig,
        backend: str,
        order: object,
    ) -> list[str]:
        if not isinstance(order, list):
            raise self._invalid_source_order()
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
        return list(order)

    async def set_agent_sources(self, backend: str, payload: object) -> dict:
        if backend not in MODEL_HUB_BACKENDS or not isinstance(payload, dict):
            raise self._invalid_source_order()
        if set(payload) != {"order"}:
            rejected = sorted(set(payload) - {"order"})
            raise self._invalid_source_order(rejected_keys=rejected)

        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            agent = self._agent(config, backend)
            agent.sources.order = self._validate_agent_source_order(
                config,
                backend,
                payload.get("order"),
            )
            await self._commit_synced(previous, config)
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
            or set(payload)
            - {"hops", "force", "would_remove_hops", "would_interrupt"}
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
                    model.id == hop.model_id and not model.retired
                    for model in source.models
                ):
                    raise ModelHubError("mapping_target_unavailable", status=409)
            new_pairs = {
                (item.source_id, item.model_id)
                for item in route.hops
            }
            removed_hops = [
                {
                    "backend": backend,
                    "menu_model": model_id,
                    "source_id": hop.source_id,
                    "model_id": hop.model_id,
                    "position": position,
                }
                for position, hop in enumerate(old_route.hops, start=1)
                if (hop.source_id, hop.model_id) not in new_pairs
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
            confirmed = (
                payload.get("force") is True
                and _same_json_value(
                    payload.get("would_remove_hops"),
                    removed_hops,
                )
                and _same_json_value(
                    payload.get("would_interrupt"),
                    interrupted,
                )
            )
            if interrupted and not confirmed:
                raise ModelHubError(
                    "source_last_supplier",
                    status=409,
                    data={
                        "would_remove_hops": removed_hops,
                        "would_interrupt": interrupted,
                    },
                )
            await self._commit_synced(previous, config)
            return {
                "chain": self._agent_chain(config, backend, model_id),
                "removed_hops": removed_hops,
                "interrupted": interrupted,
            }

    def _prune_unavailable_agent_references(self, config: ModelHubConfig) -> None:
        # Route membership is user configuration. Inventory refresh and source
        # deletion may annotate or remove exact hops, but never re-match menus.
        return

    @staticmethod
    def _catalog_model_payload(model: ModelHubBackendModelConfig) -> dict:
        return {
            **model.to_payload(),
            "locked": False,
            "routeable": True,
        }

    @staticmethod
    def _claude_default_catalog_payload() -> dict:
        return {
            "id": "default",
            "display_name": None,
            "origin": "builtin",
            "models_dev_id": None,
            "context_window": None,
            "max_output_tokens": None,
            "input_modalities": [],
            "output_modalities": [],
            "supports_tools": None,
            "supports_reasoning": None,
            "reasoning_efforts": [],
            "locked": True,
            "routeable": False,
        }

    @classmethod
    def _catalog_models_payload(
        cls,
        agent: ModelHubAgentSupplyConfig,
    ) -> list[dict]:
        models = [cls._catalog_model_payload(model) for model in agent.models]
        if agent.backend == "claude":
            models.insert(0, cls._claude_default_catalog_payload())
        return models

    def backend_catalog_models(self, backend: str) -> list[dict]:
        if backend not in MODEL_HUB_BACKENDS:
            raise ModelHubError("mapping_target_unavailable", status=404)
        config = self.store.load()
        return self._catalog_models_payload(config.agents[backend])

    def _agent_payload(self, config: ModelHubConfig, agent: ModelHubAgentSupplyConfig) -> dict:
        backend = cast(BackendName, agent.backend)
        builtin_models = (
            [model.id for model in agent.models]
            if agent.menu_kind == "fixed"
            else None
        )
        standard_vendors = sorted(STANDARD_OPENCODE_VENDOR_IDS) if agent.backend == "opencode" else None
        requested_model = self._requested_model(agent)
        unavailable_source_ids = self._unavailable_native_sources(config, backend)
        now = self.now()
        resolution = resolve_model_hub_turn(
            config,
            backend,
            requested_model,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
        )
        menu_model_ids = [model.id for model in agent.models]
        model_supply = [
            {
                "model_id": model_id,
                "chain_length": len(agent.routes.get(model_id, ModelHubRouteConfig()).hops),
                "has_runnable_hop": any(
                    inspection.runnable
                    for inspection in resolve_model_hub_turn(
                        config,
                        backend,
                        model_id,
                        now=now,
                        unavailable_source_ids=unavailable_source_ids,
                    ).inspected_hops
                ),
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
                    now=now,
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
                        "route_reason": (
                            named_resolution.route_reason
                            if agent.mode == "hub" and named_resolution.requested_model
                            else None
                        ),
                    }
                )
        agent_payload = agent.to_agent_supply_payload()
        agent_payload["routes"] = (
            agent_payload["routes"] if agent.mode == "hub" else None
        )
        return {
            **agent_payload,
            "selected_by_agent": selected_by_agent,
            "selected_model_id": selected_model_id,
            "selected_model_explicit": selected_model_explicit,
            # The UI must distinguish an installed backend CLI from a configured
            # Model Hub route. Keep this host fact at the controller boundary.
            "cli_present": self._cli_present(backend),
            "sources": sources,
            "supply_status": (
                resolution.supply_status
                if agent.mode == "hub" and resolution.requested_model
                else None
            ),
            "model_supply": model_supply if agent.mode == "hub" else None,
            "named_agents": named_agents,
            "catalog_models": self._catalog_models_payload(agent),
            "builtin_models": builtin_models,
            "standard_vendors": standard_vendors,
        }

    def _candidate_suppliers(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        candidate_id: str,
    ) -> tuple[list[dict], Optional[str], list[str]]:
        source_by_id = {source.id: source for source in config.sources}
        suppliers: list[dict] = []
        display_name: Optional[str] = None
        reasoning_efforts: list[str] = []
        for hop in self._matching_menu_model_hops(config, backend, candidate_id):
            source = source_by_id[hop.source_id]
            model = next(
                (
                    item
                    for item in source.models
                    if item.id == hop.model_id and not item.retired
                ),
                None,
            )
            if model is None:
                continue
            suppliers.append(
                {
                    "source_id": source.id,
                    "source_name": source.display_name,
                    "model_id": hop.model_id,
                }
            )
            proposed_name, proposed_efforts = _storable_backend_model_metadata(
                model.display_name,
                model.reasoning_efforts,
            )
            if display_name is None and proposed_name is not None:
                display_name = proposed_name
            for effort in proposed_efforts:
                if effort not in reasoning_efforts:
                    reasoning_efforts.append(effort)
        return suppliers, display_name, reasoning_efforts

    def agent_model_candidates(self, backend: str) -> dict:
        agent_backend = cast(BackendName, backend)
        config = self.store.load()
        agent = self._agent(config, backend)
        menu_models = self._catalog_models_payload(agent)
        menu_ids = {model["id"] for model in menu_models}
        snapshot = self._current_builtin_models(agent_backend)
        builtin_ids: set[str] = set()
        builtin = []
        for item in snapshot:
            model_id = item["id"]
            display_name, reasoning_efforts = _storable_backend_model_metadata(
                item.get("display_name"),
                item.get("reasoning_efforts"),
            )
            admitted = admissible_backend_model(
                agent_backend,
                model_id,
                {
                    "origin": "builtin",
                    "display_name": display_name,
                    "reasoning_efforts": reasoning_efforts,
                },
                claude_builtin_ids=_builtin_model_ids("claude"),
            )
            if admitted is None:
                continue
            builtin_ids.add(admitted.id)
            if admitted.id in menu_ids:
                continue
            suppliers, _source_label, _source_efforts = self._candidate_suppliers(
                config,
                agent_backend,
                admitted.id,
            )
            builtin.append(
                {
                    "id": admitted.id,
                    "display_name": admitted.display_name,
                    "reasoning_efforts": admitted.reasoning_efforts,
                    "suppliers": suppliers,
                    "origin": "builtin",
                }
            )

        provider_ids: list[str] = []
        source_by_id = {source.id: source for source in config.sources}
        for source_id in agent.sources.order:
            source = source_by_id.get(source_id)
            if source is None or not self._eligible_for_agent(source, backend):
                continue
            for model in source.models:
                if model.retired:
                    continue
                candidate_id = (
                    opencode_source_model_identity(source, model.id)
                    if backend == "opencode"
                    else model.id
                )
                if (
                    candidate_id in menu_ids
                    or candidate_id in builtin_ids
                    or candidate_id in provider_ids
                ):
                    continue
                provider_ids.append(candidate_id)

        providers = []
        for model_id in provider_ids:
            suppliers, display_name, reasoning_efforts = self._candidate_suppliers(
                config,
                agent_backend,
                model_id,
            )
            admitted = admissible_backend_model(
                agent_backend,
                model_id,
                {
                    "origin": "provider",
                    "display_name": display_name,
                    "reasoning_efforts": reasoning_efforts,
                },
                claude_builtin_ids=_builtin_model_ids("claude"),
            )
            if admitted is None:
                continue
            providers.append(
                {
                    "id": admitted.id,
                    "display_name": admitted.display_name,
                    "reasoning_efforts": admitted.reasoning_efforts,
                    "suppliers": suppliers,
                    "origin": "provider",
                }
            )
        in_list = []
        for model in menu_models:
            suppliers, source_label, source_efforts = self._candidate_suppliers(
                config,
                agent_backend,
                model["id"],
            )
            provider_candidate = admissible_backend_model(
                agent_backend,
                model["id"],
                {
                    "origin": "provider",
                    "display_name": source_label,
                    "reasoning_efforts": source_efforts,
                },
                claude_builtin_ids=_builtin_model_ids("claude"),
            )
            in_list.append(
                {
                    "id": model["id"],
                    "display_name": model["display_name"],
                    "reasoning_efforts": model["reasoning_efforts"],
                    "suppliers": suppliers,
                    "origin": model["origin"],
                    "group_if_removed": (
                        "builtin"
                        if model["id"] in builtin_ids
                        else "providers"
                        if suppliers and provider_candidate is not None
                        else None
                    ),
                }
            )
        return {"builtin": builtin, "providers": providers, "in_list": in_list}

    def list_agents(self) -> list[dict]:
        config = self.store.load()
        return [self._agent_payload(config, config.agents[backend]) for backend in ("claude", "codex", "opencode")]

    def refresh_cli_presence(
        self,
        *,
        include_npm_global: bool = False,
        backends: tuple[BackendName, ...] | None = None,
    ) -> None:
        if self.cli_presence_refresh is None:
            return
        try:
            self.cli_presence_refresh(include_npm_global, backends)
        except Exception:
            logger.warning("Model Hub CLI presence refresh failed", exc_info=True)

    def _cli_present(self, backend: BackendName) -> bool:
        if self.cli_present_override is None:
            return False
        try:
            return bool(self.cli_present_override(backend))
        except Exception:
            logger.warning("Model Hub CLI presence probe failed for %s", backend, exc_info=True)
            return False

    async def set_agent_mode(self, backend: str, mode: object) -> dict:
        if mode not in {"hub", "direct"}:
            raise ModelHubError("mode_switch_blocked")
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            agent = self._agent(config, backend)
            if agent.mode == "direct" and mode == "hub":
                native_items = await asyncio.to_thread(
                    scan_native_configs,
                    config,
                    mask_credential=_mask_credential,
                    home=self.migration_home,
                    claude_oauth_probe=self.migration_claude_oauth_probe,
                    validate_base_url=_validated_base_url,
                )
                native_item = next(
                    (
                        item
                        for item in native_items
                        if item.backend == backend
                        and item.proposed_action == "keep_native"
                    ),
                    None,
                )
                if native_item is not None:
                    source = build_native_migration_source(
                        native_item,
                        now=self.now(),
                        validate_base_url=_validated_base_url,
                    )
                    config.sources.append(source)
                    self._apply_source_placement(config, source)
            agent.mode = mode
            await self._commit_synced(previous, config)
            committed = self.store.load()
            return self._agent_payload(committed, self._agent(committed, backend))

    async def reorder_agent_chains(
        self,
        backend: str,
        order: object = _REORDER_ORDER_UNSET,
    ) -> dict:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            agent = self._agent(config, backend)
            if order is not _REORDER_ORDER_UNSET:
                # The optional order lets the UI commit the persisted priority and
                # its application to existing routes in one mutation.
                agent.sources.order = self._validate_agent_source_order(
                    config,
                    backend,
                    order,
                )
            source_positions = {
                source_id: position
                for position, source_id in enumerate(agent.sources.order)
            }
            for route in agent.routes.values():
                indexed_hops = list(enumerate(route.hops))
                indexed_hops.sort(
                    key=lambda entry: (
                        (
                            0,
                            source_positions[entry[1].source_id],
                            entry[0],
                        )
                        if entry[1].source_id in source_positions
                        else (1, entry[0], entry[0])
                    )
                )
                route.hops = tuple(hop for _, hop in indexed_hops)
            await self._commit_synced(previous, config)
            return self._agent_payload(config, agent)

    @classmethod
    def _parse_backend_catalog_models(
        cls,
        backend: str,
        payload: object,
    ) -> list[ModelHubBackendModelConfig]:
        if backend not in MODEL_HUB_BACKENDS:
            raise ModelHubError("mapping_target_unavailable")
        if not isinstance(payload, list):
            raise ModelHubError("backend_model_catalog_invalid")
        default_indices = [
            index
            for index, item in enumerate(payload)
            if isinstance(item, dict) and item.get("id") == "default"
        ]
        if backend == "claude":
            if (
                default_indices != [0]
                or payload[0] != cls._claude_default_catalog_payload()
            ):
                raise ModelHubError("backend_model_locked", status=409)
        rows: list[ModelHubBackendModelConfig] = []
        for item in payload:
            if (
                backend == "claude"
                and isinstance(item, dict)
                and item.get("id") == "default"
            ):
                continue
            try:
                model = ModelHubBackendModelConfig.from_payload(item)
            except (TypeError, ValueError) as exc:
                raise ModelHubError("backend_model_catalog_invalid") from exc
            if backend == "opencode":
                try:
                    canonical_opencode_menu_identity(model.id)
                except ValueError as exc:
                    raise ModelHubError("backend_model_id_invalid") from exc
            rows.append(model)
        if len({model.id for model in rows}) != len(rows):
            raise ModelHubError("backend_model_duplicate")
        return rows

    @staticmethod
    def _backend_model_admission_error(
        backend: BackendName,
        model_id: str,
    ) -> str | None:
        return backend_model_admission_error(
            backend,
            model_id,
            claude_builtin_ids=_builtin_model_ids("claude"),
        )

    @staticmethod
    def _parse_expected_suppliers(
        payload: object,
        caller_added_model_ids: Iterable[str],
    ) -> dict[str, list[dict[str, str]]]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ModelHubError("backend_model_catalog_invalid")
        caller_added = set(caller_added_model_ids)
        parsed: dict[str, list[dict[str, str]]] = {}
        for model_id, suppliers in payload.items():
            if (
                not isinstance(model_id, str)
                or model_id not in caller_added
                or not isinstance(suppliers, list)
            ):
                raise ModelHubError("backend_model_catalog_invalid")
            projected: list[dict[str, str]] = []
            for supplier in suppliers:
                if (
                    not isinstance(supplier, dict)
                    or set(supplier) != {"source_id", "model_id"}
                    or not isinstance(supplier.get("source_id"), str)
                    or not supplier["source_id"]
                    or not isinstance(supplier.get("model_id"), str)
                    or not supplier["model_id"]
                ):
                    raise ModelHubError("backend_model_catalog_invalid")
                projected.append(
                    {
                        "source_id": supplier["source_id"],
                        "model_id": supplier["model_id"],
                    }
                )
            if len(
                {(item["source_id"], item["model_id"]) for item in projected}
            ) != len(projected):
                raise ModelHubError("backend_model_catalog_invalid")
            parsed[model_id] = projected
        return parsed

    def _project_expected_suppliers(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        model_ids: Iterable[str],
    ) -> dict[str, list[dict[str, str]]]:
        return {
            model_id: [
                {"source_id": hop.source_id, "model_id": hop.model_id}
                for hop in self._matching_menu_model_hops(
                    config,
                    backend,
                    model_id,
                )
            ]
            for model_id in model_ids
        }

    async def _refresh_backend_catalog(self, backend: BackendName) -> None:
        if self.backend_catalog_changed is None:
            return
        try:
            await self.backend_catalog_changed(backend)
        except ModelHubError:
            raise
        except Exception as exc:
            raise ModelHubError("engine_down", status=503) from exc

    @staticmethod
    def _insert_builtin_model(
        agent: ModelHubAgentSupplyConfig,
        model: ModelHubBackendModelConfig,
        builtin_order: tuple[str, ...],
    ) -> None:
        """Place a new built-in relative to built-ins already in the menu."""

        snapshot_index = builtin_order.index(model.id)
        positions = {item.id: index for index, item in enumerate(agent.models)}
        following = next(
            (
                positions[model_id]
                for model_id in builtin_order[snapshot_index + 1 :]
                if model_id in positions
            ),
            None,
        )
        if following is not None:
            agent.models.insert(following, model)
            return
        preceding = next(
            (
                positions[model_id] + 1
                for model_id in reversed(builtin_order[:snapshot_index])
                if model_id in positions
            ),
            None,
        )
        agent.models.insert(
            preceding if preceding is not None else len(agent.models),
            model,
        )

    def _apply_builtin_reconcile(
        self,
        config: ModelHubConfig,
        snapshots: Mapping[str, Mapping[str, Any]],
    ) -> list[BackendName]:
        changed: list[BackendName] = []
        for backend in ("claude", "codex"):
            agent = config.agents[backend]
            raw_snapshot = snapshots.get(backend, {})
            snapshot: list[ModelHubBackendModelConfig] = []
            for item in raw_snapshot.get("models", []):
                display_name, reasoning_efforts = _storable_backend_model_metadata(
                    item.get("display_name"),
                    item.get("reasoning_efforts"),
                )
                admitted = admissible_backend_model(
                    cast(BackendName, backend),
                    item.get("id"),
                    {
                        "origin": "builtin",
                        "display_name": display_name,
                        "reasoning_efforts": reasoning_efforts,
                    },
                    claude_builtin_ids=_builtin_model_ids("claude"),
                )
                if admitted is not None:
                    snapshot.append(admitted)
            builtin_order = tuple(model.id for model in snapshot)
            present = {model.id for model in agent.models}
            removed = set(agent.removed_model_ids)
            added = False
            for model in snapshot:
                model_id = model.id
                if model_id in present or model_id in removed:
                    continue
                self._insert_builtin_model(
                    agent,
                    model,
                    builtin_order,
                )
                present.add(model_id)
                self._seed_menu_model_route(
                    config,
                    cast(BackendName, backend),
                    model_id,
                )
                added = True
            if added:
                changed.append(cast(BackendName, backend))
        return changed

    def _builtin_snapshots(
        self,
        backends: Iterable[BackendName] = ("claude", "codex"),
    ) -> dict[str, dict[str, Any]]:
        from vibe.backend_model_catalog import backend_builtin_snapshot

        return {
            backend: backend_builtin_snapshot(
                backend,
                cli_installed=self._cli_present(backend),
            )
            for backend in backends
            if backend != "opencode"
        }

    def _reconcile_store_writable(self) -> bool:
        try:
            self._ensure_config_writable()
        except ModelHubError as exc:
            if exc.code != "config_recovery":
                raise
            return False
        return True

    @staticmethod
    def _builtin_snapshot_generation(snapshot: Mapping[str, Any]) -> str:
        generation = snapshot.get("generation")
        if isinstance(generation, str) and generation:
            return generation
        content = {
            "complete": snapshot.get("complete") is True,
            "models": snapshot.get("models", []),
        }
        return hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _current_builtin_models(
        self,
        backend: BackendName,
    ) -> list[dict[str, Any]]:
        cached = self._builtin_snapshot_cache.get(backend)
        if cached is not None:
            return cached
        if backend == "opencode":
            return []
        from vibe.backend_model_catalog import backend_builtin_models

        return backend_builtin_models(backend)

    async def reconcile_builtin_models(
        self,
        backends: Iterable[BackendName] = ("claude", "codex"),
        *,
        notify: bool = True,
    ) -> list[BackendName]:
        async with self._mutation_lock:
            selected_backends = tuple(backends)
            previous = self.store.load()
            snapshots = self._builtin_snapshots(selected_backends)
            store_writable = self._reconcile_store_writable()
            changed_snapshots = {
                backend: snapshot
                for backend, snapshot in snapshots.items()
                if store_writable
                if self._builtin_snapshot_generations.get(
                    cast(BackendName, backend)
                )
                != self._builtin_snapshot_generation(snapshot)
            }
            self._builtin_snapshot_cache.update(
                {
                    cast(BackendName, backend): list(snapshot.get("models", []))
                    for backend, snapshot in snapshots.items()
                }
            )
            changed: list[BackendName] = []
            if changed_snapshots:
                config = self._clone_config(previous)
                changed = self._apply_builtin_reconcile(config, changed_snapshots)
                if changed:
                    try:
                        await self._commit_synced(previous, config)
                    except ModelHubError as exc:
                        if exc.code != "config_recovery":
                            raise
                        return []
                self._builtin_snapshot_generations.update(
                    {
                        cast(BackendName, backend): self._builtin_snapshot_generation(
                            snapshot
                        )
                        for backend, snapshot in changed_snapshots.items()
                    }
                )
                if notify:
                    self._pending_builtin_catalog_refresh.update(changed)
            if notify:
                pending = tuple(
                    backend
                    for backend in selected_backends
                    if backend in self._pending_builtin_catalog_refresh
                    and store_writable
                )
                for backend in pending:
                    await self._refresh_backend_catalog(backend)
                    self._pending_builtin_catalog_refresh.discard(backend)
            return changed

    async def set_agent_models(
        self,
        backend: str,
        baseline: object,
        models: object,
        *,
        expected_suppliers: object = None,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            agent = self._agent(config, backend)
            baseline_models = self._parse_backend_catalog_models(
                backend,
                baseline,
            )
            desired_models = self._parse_backend_catalog_models(
                backend,
                models,
            )

            current_by_id = {model.id: model for model in agent.models}
            baseline_by_id = {model.id: model for model in baseline_models}
            desired_by_id = {model.id: model for model in desired_models}
            caller_added_model_ids = [
                model.id
                for model in desired_models
                if model.id not in baseline_by_id
            ]
            parsed_expected_suppliers = self._parse_expected_suppliers(
                expected_suppliers,
                caller_added_model_ids,
            )
            agent_backend = cast(BackendName, backend)
            builtin_ids = {
                item["id"]
                for item in self._current_builtin_models(agent_backend)
            }
            for model_id in desired_by_id.keys() - current_by_id.keys():
                admission_error = self._backend_model_admission_error(
                    agent_backend,
                    model_id,
                )
                if admission_error == "backend_model_id_invalid":
                    raise ModelHubError(admission_error)
            for model_id, desired in desired_by_id.items():
                trusted = current_by_id.get(model_id) or baseline_by_id.get(model_id)
                if (
                    (
                        trusted is None
                        and desired.origin == "builtin"
                        and model_id not in builtin_ids
                    )
                    or (trusted is not None and desired.origin != trusted.origin)
                ):
                    raise ModelHubError("backend_model_locked", status=409)
            for model_id in desired_by_id.keys() - current_by_id.keys():
                admission_error = self._backend_model_admission_error(
                    agent_backend,
                    model_id,
                )
                if admission_error is not None:
                    raise ModelHubError(admission_error)
            projections = self._project_expected_suppliers(
                config,
                agent_backend,
                parsed_expected_suppliers,
            )
            changed_suppliers = {
                model_id: projections[model_id]
                for model_id, expected in parsed_expected_suppliers.items()
                if projections[model_id] != expected
            }
            if changed_suppliers:
                raise ModelHubError(
                    "candidate_suppliers_changed",
                    status=409,
                    data={"changed": changed_suppliers},
                )
            merged_by_id = dict(current_by_id)

            removed_hops: list[dict] = []
            removed_model_ids = [
                model.id
                for model in baseline_models
                if model.id not in desired_by_id and model.id in current_by_id
            ]
            removed_model_id_set = set(removed_model_ids)
            for model_id in removed_model_ids:
                route = agent.routes.get(model_id)
                current = current_by_id.get(model_id)
                if current is not None and current != baseline_by_id[model_id]:
                    raise ModelHubError("backend_model_conflict", status=409)
                if route is not None:
                    removed_hops.extend(
                        {
                            "backend": backend,
                            "menu_model": model_id,
                            "source_id": hop.source_id,
                            "model_id": hop.model_id,
                            "position": position,
                        }
                        for position, hop in enumerate(route.hops, start=1)
                    )
                    route.hops = ()
                merged_by_id.pop(model_id, None)
                agent.routes.pop(model_id, None)
                if model_id not in agent.removed_model_ids:
                    agent.removed_model_ids.append(model_id)

            agent.models = [
                model
                for model in agent.models
                if model.id not in removed_model_id_set
            ]

            newly_empty_routes = frozenset(
                (backend, item["menu_model"])
                for item in removed_hops
            )
            interrupted = self._introduced_interruptions(
                previous,
                config,
                newly_empty_routes=newly_empty_routes,
            )
            self._require_guard_plan(
                force=force,
                confirmed_remove_hops=confirmed_remove_hops,
                confirmed_interruptions=confirmed_interruptions,
                would_remove_hops=removed_hops,
                would_interrupt=interrupted,
                error="backend_model_in_route",
            )

            for model_id, desired in desired_by_id.items():
                baseline_model = baseline_by_id.get(model_id)
                current = current_by_id.get(model_id)
                if baseline_model is None:
                    if current is not None and current != desired:
                        raise ModelHubError("backend_model_conflict", status=409)
                    merged_by_id[model_id] = desired
                    agent.routes.setdefault(model_id, ModelHubRouteConfig())
                    continue
                if current is None:
                    if desired != baseline_model:
                        raise ModelHubError("backend_model_conflict", status=409)
                    continue
                if (
                    desired != baseline_model
                    and current != baseline_model
                    and current != desired
                ):
                    raise ModelHubError("backend_model_conflict", status=409)
                if desired != baseline_model:
                    merged_by_id[model_id] = desired

            baseline_survivors = [
                model.id for model in baseline_models if model.id in desired_by_id
            ]
            desired_existing = [
                model.id for model in desired_models if model.id in baseline_by_id
            ]
            desired_order = [model.id for model in desired_models]
            appended_order = [
                *baseline_survivors,
                *(
                    model.id
                    for model in desired_models
                    if model.id not in baseline_by_id
                ),
            ]
            order_changed = (
                baseline_survivors != desired_existing
                or desired_order != appended_order
            )
            concurrent_ids = [
                model.id
                for model in agent.models
                if model.id not in baseline_by_id and model.id not in desired_by_id
            ]
            if order_changed:
                ordered_ids = [
                    model.id for model in desired_models if model.id in merged_by_id
                ]
                ordered_ids.extend(
                    model_id
                    for model_id in concurrent_ids
                    if model_id in merged_by_id and model_id not in ordered_ids
                )
            else:
                ordered_ids = [
                    model.id for model in agent.models if model.id in merged_by_id
                ]
                for model in desired_models:
                    if model.id not in ordered_ids and model.id in merged_by_id:
                        ordered_ids.append(model.id)
            agent.models = [merged_by_id[model_id] for model_id in ordered_ids]
            for model_id in caller_added_model_ids:
                if model_id in agent.removed_model_ids:
                    agent.removed_model_ids = [
                        removed_id
                        for removed_id in agent.removed_model_ids
                        if removed_id != model_id
                    ]
                if model_id not in current_by_id and model_id in merged_by_id:
                    self._seed_menu_model_route(
                        config,
                        agent_backend,
                        model_id,
                    )
            if agent.backend == "opencode":
                agent.menu = ModelHubMenuConfig(
                    view=agent.menu.view if agent.menu else "featured",
                    checked=list(ordered_ids),
                )
            await self._commit_synced(previous, config)
            await self._refresh_backend_catalog(cast(BackendName, backend))
            committed = self.store.load()
            result = {
                "agent": self._agent_payload(
                    committed,
                    self._agent(committed, backend),
                )
            }
            if removed_hops:
                result["removed_hops"] = removed_hops
                result["interrupted"] = interrupted
            return result

    @staticmethod
    def models_dev_matches(query: object) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ModelHubError("mapping_target_unavailable")
        from vibe.models_dev_catalog import search_models_dev

        try:
            return search_models_dev(query)
        except (OSError, RuntimeError, ValueError):
            raise ModelHubError("models_dev_unavailable", status=502) from None

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
                    "reasoning_efforts_source": (
                        "user" if isinstance(value, list) and value else None
                    ),
                }
            )
        except ValueError:
            raise ModelHubError("mapping_target_unavailable") from None
        return validated.reasoning_efforts

    @staticmethod
    def _raise_if_reasoning_tiers_managed(model: ModelHubModelConfig) -> None:
        managed_source = model.reasoning_efforts_source
        if managed_source not in {"upstream", "catalog"}:
            return
        raise ModelHubError(
            "source_model_tiers_managed",
            status=409,
            detail=f"settings.models.sourceDetail.tiers.managed.{managed_source}",
            data={"reasoning_efforts_source": managed_source},
        )

    @staticmethod
    def _apply_reasoning_tier_ladder(
        source: ModelHubSourceConfig,
        model: ModelHubModelConfig,
    ) -> None:
        resolution = resolve_reasoning_tiers(
            protocol=source.protocol,
            model_id=model.id,
            existing_efforts=model.reasoning_efforts,
            existing_source=model.reasoning_efforts_source,
        )
        model.reasoning_efforts = list(resolution.efforts)
        model.reasoning_efforts_source = resolution.source

    async def add_custom_model(self, source_id: object, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ModelHubError("source_not_found", status=404)
        model_id = canonical_model_id(payload.get("model_id"))
        display_name = payload.get("display_name")
        reasoning_efforts = payload.get("reasoning_efforts")
        if model_id is None or contains_credential_material(model_id):
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
                model.reasoning_efforts_source = (
                    "user" if model.reasoning_efforts else None
                )
                self._apply_reasoning_tier_ladder(source, model)
                source.models.append(model)
            elif existing.provenance == "discovered":
                raise ModelHubError("source_model_managed_upstream", status=409)
            else:
                self._raise_if_reasoning_tiers_managed(existing)
                existing.display_name = display_name
                existing.reasoning_efforts = self._validated_reasoning_efforts(
                    existing,
                    reasoning_efforts,
                )
                existing.reasoning_efforts_source = (
                    "user" if existing.reasoning_efforts else None
                )
                self._apply_reasoning_tier_ladder(source, existing)
            await self._commit_synced(previous, config)
            return self._source_payload(source)

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
            self._raise_if_reasoning_tiers_managed(model)
            model.reasoning_efforts = self._validated_reasoning_efforts(
                model,
                payload["reasoning_efforts"],
            )
            model.reasoning_efforts_source = (
                "user" if model.reasoning_efforts else None
            )
            await self._commit_synced(previous, config)
            return self._source_payload(source)

    async def delete_custom_model(
        self,
        source_id: object,
        model_id: object,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
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
            removed_hops = [
                {
                    "backend": backend,
                    "menu_model": menu_model,
                    "source_id": source.id,
                    "model_id": model_id,
                    "position": position,
                }
                for backend in MODEL_HUB_BACKENDS
                for menu_model, route in config.agents[backend].routes.items()
                for position, hop in enumerate(route.hops, start=1)
                if hop.source_id == source.id and hop.model_id == model_id
            ]
            if model.provenance == "discovered":
                model.retired = True
            else:
                source.models = [
                    item
                    for item in source.models
                    if item.id != model_id
                ]
            would_interrupt = self._introduced_interruptions(previous, config)
            self._require_guard_plan(
                force=force,
                confirmed_remove_hops=confirmed_remove_hops,
                confirmed_interruptions=confirmed_interruptions,
                would_remove_hops=removed_hops,
                would_interrupt=would_interrupt,
                error=(
                    "source_model_in_route_chain"
                    if removed_hops
                    else "source_last_supplier"
                ),
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
                "source": self._source_payload(source),
                "removed_hops": removed_hops,
                "interrupted": would_interrupt,
            }

    def usage_summary(self, *, days: int = USAGE_DEFAULT_WINDOW_DAYS) -> dict:
        """Report metered token usage, labelled from current Source config.

        Config is what this method owns: which identities exist right now and what
        they are called. The join itself belongs to the ledger, because only the
        ledger knows how a row is keyed — the version of this method that looked a
        label up by `row["source_id"]` is what that rule looks like once it has
        leaked to a caller, and it silently dropped the label of every identity the
        key fold exists for.

        What it hands over is therefore the shape config actually has — Sources, each
        carrying the models listed under it — and not a flat map per key level. A
        metered model's identity is the pair, so a flat model map cannot say which
        Source an ID came from, and answers for one Source with another's models.
        """

        config = self.store.load()
        return self.usage.summary(
            days=days,
            now=self.now(),
            identities=[
                SourceIdentity(
                    source_id=source.id,
                    label=source.display_name,
                    model_ids=[model.id for model in source.models],
                )
                for source in config.sources
            ],
        )

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
        *,
        now: Optional[datetime] = None,
        unavailable_source_ids: Optional[frozenset[str]] = None,
    ) -> dict:
        agent = self._agent(config, backend)
        if agent.mode == "direct":
            raise self._direct_mode_error()
        observed_at = now or self.now()
        unavailable = (
            unavailable_source_ids
            if unavailable_source_ids is not None
            else self._unavailable_native_sources(
                config,
                cast(BackendName, backend),
            )
        )
        resolution = resolve_model_hub_turn(
            config,
            cast(BackendName, backend),
            model_id,
            now=observed_at,
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

    def agent_chains(self, backend: str) -> list[dict]:
        if backend not in MODEL_HUB_BACKENDS:
            raise ModelHubError("mapping_target_unavailable", status=409)
        config = self.store.load()
        agent = self._agent(config, backend)
        if agent.mode == "direct":
            raise self._direct_mode_error()
        requested_model = self._requested_model(agent)
        observed_at = self.now()
        unavailable_source_ids = self._unavailable_native_sources(
            config,
            cast(BackendName, backend),
        )
        return [
            self._agent_chain(
                config,
                backend,
                model_id,
                now=observed_at,
                unavailable_source_ids=unavailable_source_ids,
            )
            for model_id in self._agent_model_ids(agent, requested_model)
        ]

    def opencode_public_models(self) -> dict[str, dict[str, Any]]:
        """Return the safe public projection owned by persisted Hub config."""

        config = self.store.load()
        return _project_opencode_public_models(
            config,
            now=self.now(),
            unavailable_source_ids=self._unavailable_native_sources(
                config,
                "opencode",
            ),
        )

    @staticmethod
    def _probe_request(
        source: ModelHubSourceConfig,
        model_id: str,
        backend: str,
    ) -> ModelHubRequest:
        # A probe enters the same translation seam as a live backend turn, so
        # its payload must be shaped in the backend's client protocol.
        request_protocol = _FIXED_BACKEND_PROTOCOLS.get(backend, source.protocol)
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
        if outcome.kind == RawOutcomeKind.NETWORK_ERROR:
            return "models.source.cooldown.network", "network"
        if outcome.kind == RawOutcomeKind.TIMEOUT:
            return "models.source.cooldown.timeout", "network"
        if decision.reason in {
            "credential_expired",
            "credential_revoked",
        }:
            detail_key = {
                "credential_expired": "models.source.needs_action.oauth_expired",
                "credential_revoked": "models.source.needs_action.credential_revoked",
            }[decision.reason]
            return detail_key, cast(EventReason, decision.reason)
        if decision.reason == "balance_exhausted":
            return "models.source.needs_action.balance_exhausted", "balance_exhausted"
        if decision.reason == "account_banned":
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

    def _write_source_blocker_locked(
        self,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
        *,
        backend: BackendName,
        model_id: str,
        detail_key: str,
        reason: EventReason,
        emit_event: bool = True,
    ) -> bool:
        status: Literal["error", "needs_action"] = (
            "error"
            if reason == "unclassified_error"
            else "needs_action"
        )
        unchanged = (
            source.state.status == status
            and source.state.detail_key == detail_key
        )
        if unchanged:
            return False
        previous = self._clone_config(config)
        source.state = ModelHubSourceStateConfig(
            status=status,
            detail_key=detail_key,
        )
        persisted = self._save_runtime_config(previous, config)
        if persisted and emit_event:
            self._record_event(
                agent=cast(EventAgent, backend),
                kind="needs_action",
                model_id=model_id,
                reason=reason,
                from_source=source.id,
                from_label=source.display_name,
                now=self.now(),
            )
        return persisted

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
        settlement_generation = self._reserve_settlement_generation(source.id)
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
        decision = await self._classify_source_outcome(source, outcome)
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
            if event_reason is not None:
                await self._settle_fallback_source(
                    source,
                    decision,
                    backend=cast(BackendName, backend),
                    model_id=chain_payload["model_id"],
                    detail_key=error_key,
                    settlement_generation=settlement_generation,
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
            or payload.get("acknowledge_irreversible") is not True
        ):
            raise ModelHubError("reauth_confirmation_required", status=409)

        async with self._mutation_lock:
            config = self.store.load()
            source = self._source(config, source_id)
            if source.kind != "subscription":
                raise ModelHubError("discovery_failed")

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
                        if model.provenance == "manual" or model.retired
                    ]
                    candidate.account_label = None
                    candidate.state = ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key="models.source.needs_action.oauth_expired",
                    )
                self._save_projection_neutral(previous, config)

                def restore_after_spawn_failure() -> None:
                    self._save_projection_neutral(config, previous)

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
        client_nonce = (
            payload.get("client_nonce") if isinstance(payload, dict) else None
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(vendor, str)
            or channel not in {"native_cli", "hub"}
            or set(payload) - {"vendor", "channel", "client_nonce"}
            or (
                "client_nonce" in payload
                and not isinstance(client_nonce, str)
            )
        ):
            raise ModelHubError("flow_not_found", status=400)
        try:
            vendor = normalize_model_hub_vendor_id(vendor)
        except ValueError:
            raise ModelHubError("flow_not_found", status=400) from None
        oauth_channel = cast(OAuthChannel, channel)
        self._ensure_config_writable()
        nonce_key: tuple[str, str, OAuthChannel] | None = None
        if client_nonce is not None:
            try:
                nonce_claim = self.oauth_flows.claim_nonce(
                    client_nonce,
                    vendor,
                    oauth_channel,
                )
            except ValueError:
                raise ModelHubError("flow_not_found", status=400) from None
            nonce_key = (client_nonce, vendor, oauth_channel)
            if not nonce_claim.owner:
                if nonce_claim.status == "committed":
                    if nonce_claim.flow_id is None:
                        raise ModelHubError("flow_not_found", status=404)
                    return await self._replay_nonce_flow(nonce_claim.flow_id)
                task = self._oauth_start_tasks.get(nonce_key)
                if task is None:
                    raise ModelHubError("engine_down", status=503)
                return await asyncio.shield(task)

        # Everything the owner can wait on lives inside this coroutine, and that
        # is the invariant rather than an accident of layout: the claim above is
        # what a concurrent same-tuple retry looks for, and the ONLY release is
        # this coroutine's own ``finally``. So an owner await placed out here —
        # the native-slot read used to be one — strands the tuple until restart:
        # the retry finds a pending claim with no task and gets ``engine_down``,
        # and a cancelled owner never reaches the release at all.
        # ``test_oauth_start_keeps_every_owner_await_inside_the_installed_task``
        # holds the shape so the next pre-check cannot re-open the window.
        async def start_and_remember() -> dict:
            pending_source_id = _source_id()
            flow: OAuthFlowState | None = None
            flow_cleanup_done = False
            flow_cleanup_attempted = False
            try:
                if oauth_channel == "native_cli":
                    async with self._mutation_lock:
                        # The sanctioned CLI keeps one credential per vendor, so
                        # a second native Source would describe a credential the
                        # first one already owns. The lock serializes this read
                        # with migration's native Source writer and with the
                        # re-check in ``_create_oauth_source``, so a flow that
                        # started before the first Source was persisted still
                        # cannot materialize a sibling.
                        occupied = self._existing_native_source(
                            self.store.load(),
                            vendor,
                        )
                    if occupied is not None:
                        raise ModelHubError(
                            "native_source_already_exists",
                            status=409,
                            detail="modelHub.errors.native_subscription_exists",
                            data={"existing_source_id": occupied.id},
                        )
                flow = await self._oauth_call(
                    self._oauth_adapter(oauth_channel).start_oauth(
                        pending_source_id,
                        vendor,
                    )
                )
                if flow.source_id != pending_source_id or flow.vendor != vendor:
                    flow_cleanup_attempted = True
                    flow_cleanup_done = await self._discard_started_oauth_flow(
                        flow,
                        oauth_channel,
                    )
                    if not flow_cleanup_done:
                        raise ModelHubError("engine_down", status=503)
                    raise ModelHubError("flow_not_found", status=502)
                if client_nonce is not None and flow.expires_at_iso is None:
                    flow_cleanup_attempted = True
                    flow_cleanup_done = await self._discard_started_oauth_flow(
                        flow,
                        oauth_channel,
                    )
                    if not flow_cleanup_done:
                        raise ModelHubError("engine_down", status=503)
                    raise ModelHubError("flow_not_found", status=502)
                self.oauth_flows.remember(
                    flow.flow_id,
                    oauth_channel,
                    pending_source_id,
                    vendor,
                    client_nonce=client_nonce,
                    expires_at_iso=(
                        flow.expires_at_iso if client_nonce is not None else None
                    ),
                )
            except BaseException as error:
                try:
                    if (
                        flow is not None
                        and not flow_cleanup_done
                        and not flow_cleanup_attempted
                    ):
                        flow_cleanup_attempted = True
                        await self._discard_started_oauth_flow(
                            flow,
                            oauth_channel,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not isinstance(error, asyncio.CancelledError):
                        raise ModelHubError("engine_down", status=503) from None
                finally:
                    if client_nonce is not None:
                        try:
                            self.oauth_flows.release_nonce(
                                client_nonce,
                                vendor,
                                oauth_channel,
                            )
                        except (OSError, ValueError):
                            pass
                raise
            binding = self._oauth_binding(flow.flow_id)
            return {"flow": self._flow_payload(flow, binding)}

        if nonce_key is None:
            return await start_and_remember()
        task = asyncio.create_task(start_and_remember())
        self._oauth_start_tasks[nonce_key] = task

        def forget_settled_start(completed: asyncio.Task[dict]) -> None:
            if self._oauth_start_tasks.get(nonce_key) is completed:
                self._oauth_start_tasks.pop(nonce_key, None)

        task.add_done_callback(forget_settled_start)
        return await task

    def _oauth_result(
        self,
        flow_id: str,
        flow: OAuthFlowState,
    ) -> dict:
        binding = self._oauth_binding(flow_id)
        result = {"flow": self._flow_payload(flow, binding)}
        if flow.state != "success":
            return result
        source = self._completed_oauth_source(binding)
        if source is None:
            raise ModelHubError("flow_not_found", status=404)
        if binding.intent == "reauth":
            result.update(
                {
                    "source": self._source_payload(source),
                    "recovered": binding.recovered is True,
                    "interrupted_pairs": list(binding.interrupted_pairs),
                }
            )
        else:
            result.update(self._source_creation_result(source.to_payload()))
        return result

    async def oauth_status(self, flow_id: str) -> dict:
        binding = self._oauth_binding(flow_id)
        if binding.terminal_state == "cancelled":
            return self._oauth_result(
                flow_id,
                self._cancelled_oauth_flow(flow_id, binding),
            )
        completed = self._completed_oauth_flow(flow_id, binding)
        if completed is not None:
            return self._oauth_result(flow_id, completed)
        flow = await self._oauth_status(flow_id, binding.channel)
        self._raise_if_flow_expired(flow_id, flow)
        flow, repair_result = await self._materialize_completed_oauth(
            flow_id,
            binding,
            flow,
        )
        if repair_result is not None:
            return {
                "flow": self._flow_payload(flow, binding),
                **repair_result,
            }
        return self._oauth_result(flow_id, flow)

    async def oauth_submit(self, payload: dict) -> dict:
        flow_id = payload.get("flow_id") if isinstance(payload, dict) else None
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(flow_id, str) or not isinstance(value, str):
            raise ModelHubError("flow_not_found", status=404)
        self._ensure_config_writable()
        binding = self._oauth_binding(flow_id)
        if binding.terminal_state == "cancelled":
            return self._oauth_result(
                flow_id,
                self._cancelled_oauth_flow(flow_id, binding),
            )
        completed = self._completed_oauth_flow(flow_id, binding)
        if completed is not None:
            return self._oauth_result(flow_id, completed)
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
                "flow": self._flow_payload(flow, binding),
                **repair_result,
            }
        return self._oauth_result(flow_id, flow)

    async def oauth_cancel(self, flow_id: object) -> None:
        if not isinstance(flow_id, str):
            raise ModelHubError("flow_not_found", status=404)
        terminal: tuple[OAuthFlowBinding, OAuthFlowState] | None = None
        async with self._mutation_lock:
            binding = self._oauth_binding(flow_id)
            if binding.terminal_state == "cancelled":
                return
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
                    if binding.client_nonce is not None:
                        self.oauth_flows.retain_cancelled(flow_id)
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
                        if binding.client_nonce is not None:
                            self.oauth_flows.retain_cancelled(flow_id)
                        else:
                            self.oauth_flows.forget(flow_id)
                    except OSError:
                        raise ModelHubError("engine_down", status=503) from None

    async def runtime_status(self) -> dict:
        status = await self.reconcile_runtime_installation()
        if status is None:
            status = await self._engine_call(self.adapter.status())
        return _runtime_payload(
            self._runtime_status_after_demand(status),
            enabled=self.store.load().enabled,
        )

    async def runtime_install(self) -> dict:
        async with self._runtime_lifecycle_lock:
            status = await self.reconcile_runtime_installation()
            if status is None:
                status = await self._engine_call(self.adapter.status())
            status = self._runtime_status_after_demand(status)
            enabled = self.store.load().enabled
            if status.health is not EngineHealth.NOT_INSTALLED:
                return _runtime_payload(status, enabled=enabled)
            install = getattr(self.adapter, "install", None)
            if not callable(install):
                raise ModelHubError("engine_down", status=503)
            return _runtime_payload(
                await self._engine_call(install()),
                enabled=enabled,
            )

    async def runtime_start(self) -> dict:
        async with self._runtime_lifecycle_lock:
            await self.reconcile_runtime_installation()
            async with self._mutation_lock:
                previous = self.store.load()
                updated = self._clone_config(previous)
                updated.enabled = True
                self._save_projection_neutral(previous, updated)
            await self._prepare_engine_for_demand()
            status = await self._engine_call(self.adapter.start())
            return _runtime_payload(status, enabled=True)

    async def runtime_stop(self) -> dict:
        async with self._runtime_lifecycle_lock:
            await self.reconcile_runtime_installation()
            async with self._mutation_lock:
                previous = self.store.load()
                hub_backends = sorted(
                    backend
                    for backend, agent in previous.agents.items()
                    if agent.mode == "hub"
                )
                if hub_backends:
                    raise ModelHubError(
                        "runtime_in_use",
                        status=409,
                        data={"backends": hub_backends},
                    )
                stop_runtime = getattr(self.adapter, "stop_runtime", None)
                if not callable(stop_runtime):
                    raise ModelHubError("engine_down", status=503)
                status = await self._engine_call(stop_runtime())
                if status.health is EngineHealth.INSTALLING:
                    raise ModelHubError("runtime_busy", status=409)
                updated = self._clone_config(previous)
                updated.enabled = False
                self._save_projection_neutral(previous, updated)
                return _runtime_payload(status, enabled=False)

    def migration_scan(self) -> dict:
        config = self.store.load()
        return {
            "items": [
                item.to_payload()
                for item in scan_native_configs(
                    config,
                    mask_credential=_mask_credential,
                    home=self.migration_home,
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
        except ModelHubError as exc:
            if exc.code != "discovery_failed":
                raise
            raise ModelHubError("migration_item_conflict", status=409) from None
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
            previous = self._clone_config(config)
            config_changed = False
            recovered_sources: list[ModelHubSourceConfig] = []
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
                recovered_sources.append(source)
            if config_changed:
                if self._save_runtime_config(previous, config):
                    for source in recovered_sources:
                        self._record_event(
                            agent=cast(EventAgent, resolution.backend),
                            kind="recover",
                            model_id=resolution.requested_model,
                            reason="recovery",
                            to_source=source.id,
                            to_label=source.display_name,
                            now=self.now(),
                        )

    def _write_cooldown_locked(
        self,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
        decision: ResolutionDecision,
        *,
        agent: EventAgent,
        model_id: str,
        detail_key: Optional[str] = None,
        emit_event: bool = True,
    ) -> bool:
        retry_at = self.now() + timedelta(seconds=decision.cooldown_seconds)
        if (
            source.state.status == "cooldown"
            and source.state.retry_at is not None
            and _parse_datetime(source.state.retry_at) >= retry_at
        ):
            return False
        previous = self._clone_config(config)
        already_cooling = source.state.status == "cooldown"
        source.state = ModelHubSourceStateConfig(
            status="cooldown",
            retry_at=retry_at.isoformat(),
            detail_key=detail_key or f"models.source.cooldown.{decision.reason}",
        )
        persisted = self._save_runtime_config(previous, config)
        if persisted and not already_cooling and emit_event:
            self._record_event(
                agent=agent,
                kind="cooldown",
                model_id=model_id,
                reason=cast(EventReason, decision.reason),
                from_source=source.id,
                from_label=source.display_name,
                now=self.now(),
            )
        return persisted

    async def _settle_fallback_source(
        self,
        source: ModelHubSourceConfig,
        decision: ResolutionDecision,
        *,
        backend: BackendName,
        model_id: str,
        emit_event: bool = True,
        detail_key: Optional[str] = None,
        settlement_generation: Optional[int] = None,
    ) -> tuple[EventReason, bool]:
        """Persist one fallback-class Source result before the turn settles."""

        if decision.reason is None:
            raise AssertionError("fallback-class outcome must retain its Source reason")
        event_reason = cast(EventReason, decision.reason)
        settlement_rule = source_settlement_rule(event_reason)
        if not settlement_rule.may_write_health:
            return event_reason, False
        # Generations are minted at attempt start and nowhere else. A settlement
        # that carries none cannot prove it is not superseded, so it does not
        # write Source health; minting one here would let any stale attempt
        # certify itself as the newest. History, the terminal outcome, and the
        # projection are produced by the caller and stay unaffected.
        if settlement_generation is None:
            return event_reason, False
        generation = settlement_generation
        async with self._mutation_lock:
            config = self.store.load()
            try:
                current = self._source(config, source.id)
            except ModelHubError:
                return event_reason, False
            latest_generation = self._latest_source_attempt_generation.get(
                current.id,
                generation,
            )
            if generation < latest_generation:
                return event_reason, False
            if not source_settlement_allowed(current.state.status, event_reason):
                return event_reason, False
            if settlement_rule.status == "cooldown":
                persisted = self._write_cooldown_locked(
                    config,
                    current,
                    decision,
                    agent=cast(EventAgent, backend),
                    model_id=model_id,
                    detail_key=detail_key,
                    emit_event=emit_event,
                )
            else:
                blocker_detail_key = detail_key or {
                    "credential_expired": "models.source.needs_action.oauth_expired",
                    "credential_revoked": "models.source.needs_action.credential_revoked",
                    "balance_exhausted": "models.source.needs_action.balance_exhausted",
                    "account_banned": "models.source.needs_action.account_banned",
                    "unclassified_error": "models.source.error.unclassified",
                }[event_reason]
                persisted = self._write_source_blocker_locked(
                    config,
                    current,
                    backend=backend,
                    model_id=model_id,
                    detail_key=blocker_detail_key,
                    reason=event_reason,
                    emit_event=emit_event,
                )
        return event_reason, persisted

    def _inspect_terminal_chain(
        self,
        *,
        backend: BackendName,
        model_id: str,
    ) -> tuple[ModelHubConfig, ModelHubTurnResolution]:
        """Inspect the complete persisted chain used by the next turn."""

        config = self.store.load()
        resolution = resolve_model_hub_turn(
            config,
            backend,
            model_id,
            now=self.now(),
            unavailable_source_ids=self._unavailable_native_sources(
                config,
                backend,
            ),
        )
        return config, resolution

    def _produce_attempt_terminal_outcome(
        self,
        *,
        backend: BackendName,
        model_id: str,
        source_id: str,
        source_model_id: str,
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
        source_transition_persisted: bool | None = None,
    ) -> TurnOutcomeProjectionInput | None:
        """Produce the sole complete terminal projection after attempt settlement."""

        if (
            outcome.stream_started
            and decision.reason is None
            and decision.error_code == "stream_interrupted"
        ):
            # A refresh-capable credential remains usable for a later turn,
            # while replay after output is still forbidden for this turn.
            return None
        category = terminal_outcome_category(outcome, decision)
        if category == "served":
            return produce_turn_outcome("turn.served")
        if category == "request_nonfallback":
            return produce_turn_outcome("turn.request_nonfallback")
        if category == "upstream_protocol":
            # The Gateway's existing protocol-error copy is the positive row;
            # a request-incompatible projection would misclassify the failure.
            return None
        if category == "engine_down":
            return produce_turn_outcome(
                "turn.engine_down",
                stream_started=outcome.stream_started,
            )
        if (
            category == "fallback_source"
            and outcome.stream_started
            and decision.reason == "network"
            and not source_settlement_rule(decision.reason).may_write_health
        ):
            # G-34 owns the future projection for a truncated stream whose
            # transport failure leaves the same hop current.
            return None
        if category != "fallback_source":
            raise AssertionError("attempt terminal producer received a non-failure")

        config, resolution = self._inspect_terminal_chain(
            backend=backend,
            model_id=model_id,
        )
        return produce_turn_outcome(
            "turn.streamed_fallback",
            config=config,
            resolution=resolution,
            attempted_hop=(source_id, source_model_id),
            source_transition_persisted=source_transition_persisted,
        )

    @staticmethod
    def _produce_no_candidate_terminal_outcome(
        *,
        config: ModelHubConfig,
        resolution: ModelHubTurnResolution,
    ) -> TurnOutcomeProjectionInput:
        return produce_turn_outcome(
            (
                "turn.no_candidate.unconfigured"
                if resolution.route_unconfigured
                else "turn.no_candidate.blocked"
            ),
            config=config,
            resolution=resolution,
        )

    @staticmethod
    def _produce_exhausted_terminal_outcome(
        *,
        config: ModelHubConfig,
        resolution: ModelHubTurnResolution,
    ) -> TurnOutcomeProjectionInput:
        return produce_turn_outcome(
            "turn.exhausted",
            config=config,
            resolution=resolution,
        )

    async def settle_handle_outcome(
        self,
        resolved: ResolvedInvocation | None,
        outcome: RawCallOutcome | None,
        *,
        termination_origin: HandleTerminationOrigin,
        record_attempt: Callable[[RawCallOutcome, ResolutionDecision], None],
    ) -> HandleSettlement:
        """Settle every consumed hub handle before its terminal facts are exposed."""

        if termination_origin == "downstream_cancel" and outcome is None:
            return HandleSettlement(
                outcome=outcome,
                decision=None,
                turn_outcome=produce_turn_outcome("turn.canceled"),
            )
        if termination_origin == "downstream_cancel":
            # The gateway selects this origin only after closing the producer.
            # A committed outcome at that barrier owns history over cancellation.
            termination_origin = "upstream_terminal"
        if termination_origin != "upstream_terminal":
            raise AssertionError("unknown handle termination origin")
        if resolved is None or resolved.supply_channel != "hub":
            raise AssertionError("post-handle settlement requires a hub stream")
        if outcome is None:
            raise AssertionError("upstream handle termination requires an outcome")
        decision = await self._classify_credential_outcome(
            resolved.credential_ref,
            outcome,
        )
        record_attempt(outcome, decision)
        if decision.action == "return":
            return HandleSettlement(
                outcome=outcome,
                decision=decision,
                turn_outcome=self._produce_attempt_terminal_outcome(
                    backend=resolved.backend,
                    model_id=resolved.requested_model_id,
                    source_id=resolved.source_id,
                    source_model_id=resolved.model_id,
                    outcome=outcome,
                    decision=decision,
                ),
            )
        if decision.action != "surface":
            raise AssertionError("consumed streams cannot retry or fall through")
        source_transition_persisted: bool | None = None
        if decision.reason is not None:
            source_transition_persisted = False
            config = self.store.load()
            source = next(
                (item for item in config.sources if item.id == resolved.source_id),
                None,
            )
            if source is not None:
                _reason, source_transition_persisted = await self._settle_fallback_source(
                    source,
                    decision,
                    backend=resolved.backend,
                    model_id=resolved.requested_model_id,
                    settlement_generation=resolved.settlement_generation,
                )
        return HandleSettlement(
            outcome=outcome,
            decision=decision,
            turn_outcome=self._produce_attempt_terminal_outcome(
                backend=resolved.backend,
                model_id=resolved.requested_model_id,
                source_id=resolved.source_id,
                source_model_id=resolved.model_id,
                outcome=outcome,
                decision=decision,
                source_transition_persisted=source_transition_persisted,
            ),
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
    ) -> tuple[InvokeHandle, Optional[RawCallOutcome], asyncio.CancelledError | None]:
        async def meter_handle(
            handle: InvokeHandle,
            outcome: RawCallOutcome | None,
        ) -> None:
            await self._meter_call(
                source_id=source.id,
                model_id=model_id,
                outcome=outcome,
                observed=handle.observed,
            )

        async def meter_available_outcome(
            handle: InvokeHandle,
        ) -> RawCallOutcome | None:
            if handle.stream is not None and not handle.outcome_available:
                return None
            outcome = await self._engine_call(handle.outcome())
            await meter_handle(handle, outcome)
            return outcome

        async def invoke_and_meter_bodyless() -> tuple[InvokeHandle, Optional[RawCallOutcome]]:
            handle = await self._engine_call(
                self.adapter.invoke(source.id, model_id, request, stream, backend)
            )
            if handle.stream is not None:
                # The body is the gateway's to forward, so the tokens in it are the
                # gateway's to meter.
                return handle, None
            outcome = await meter_available_outcome(handle)
            assert outcome is not None
            return handle, outcome

        attempt_task = asyncio.create_task(invoke_and_meter_bodyless())
        cancelled: asyncio.CancelledError | None = None
        try:
            handle, outcome = await asyncio.shield(attempt_task)
        except asyncio.CancelledError as caught:
            cancelled = caught
            try:
                handle, outcome = await await_owned_task(attempt_task)
            except BaseException:
                raise caught

        if cancelled is not None and handle.stream is not None:
            async def close_and_meter_observed_stream() -> RawCallOutcome | None:
                await handle.close_stream()
                outcome = await meter_available_outcome(handle)
                if outcome is None:
                    await meter_handle(handle, None)
                return outcome

            cleanup_task = asyncio.create_task(close_and_meter_observed_stream())
            try:
                outcome = await await_owned_task(cleanup_task)
            except BaseException:
                raise cancelled

        return handle, outcome, cancelled

    async def _settle_cancelled_attempt(
        self,
        *,
        source: ModelHubSourceConfig,
        source_model_id: str,
        requested_model_id: str,
        backend: BackendName,
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
        settlement_generation: int,
        attempt_observer: Optional[AttemptObserver],
    ) -> None:
        """Persist facts from an upstream call that beat downstream cancellation."""

        if attempt_observer is not None:
            attempt_observer(
                source.id,
                source_model_id,
                "hub",
                False,
                outcome,
                decision,
                (),
                (),
            )
        if decision.action == "fallback" or (
            decision.action == "surface"
            and outcome.stream_started
            and decision.reason is not None
        ):
            await self._settle_fallback_source(
                source,
                decision,
                backend=backend,
                model_id=requested_model_id,
                settlement_generation=settlement_generation,
            )

    async def _meter_call(
        self,
        *,
        source_id: str,
        model_id: str,
        outcome: RawCallOutcome | None,
        observed: ProtocolSSEState | None,
    ) -> None:
        """Fold one bodyless upstream call into the usage ledger, best-effort.

        This is the resolver's half of metering. A call that ends here has no body
        to hand onward — a terminal error, or a buffered response the resolver
        itself consumed — so the gateway will never see its token report, and for
        an error the resolver may not even name this Source in what it raises. A
        vendor that reported tokens billed for them whether or not the call ended
        well, and every hop of a failover chain that reported tokens billed the
        Source it was made against, not the one that finally served the turn.

        The population split with the gateway is ``handle.stream is not None``
        one frame above: a call either hands its body onward or it does not, so
        each call is metered exactly once and none is missed.

        The ledger read-modify-write is file I/O, so it runs off the event loop;
        metering is a report, never a control input, and a ledger failure must not
        change the outcome the caller sees.

        Which is also why the write is the writer's and not this call's: a resolve
        cancelled downstream while its row sat queued would otherwise discard usage
        the vendor had already billed. The writer's own bounded wait keeps the
        ordinary path leaving the row on disk before the caller sees its outcome,
        without putting an unresponsive disk in front of the next failover hop.
        """

        usage = outcome.usage if outcome is not None else None
        if usage is None and observed is not None:
            usage = observed.usage
        reached_model = (
            outcome is not None
            and (outcome.kind is RawOutcomeKind.SUCCESS or outcome.stream_started)
        ) or (observed is not None and observed.reached_model)
        if usage is None and not reached_model:
            return
        await self.usage_writer.wait_recorded(
            self.usage_writer.record(
                source_id=source_id,
                model_id=model_id,
                usage=usage,
                at=self.now(),
            )
        )

    async def _classify_source_outcome(
        self,
        source: ModelHubSourceConfig,
        outcome: RawCallOutcome,
    ) -> ResolutionDecision:
        """Apply the credential-capability branch of the section 4.3 matrix once."""

        decision = classify_outcome(outcome)
        if decision.action != "refresh":
            return decision
        return await self._classify_credential_outcome(
            source.credential_ref,
            outcome,
            decision=decision,
        )

    async def _classify_credential_outcome(
        self,
        credential_ref: Optional[str],
        outcome: RawCallOutcome,
        *,
        decision: Optional[ResolutionDecision] = None,
    ) -> ResolutionDecision:
        """Resolve one credential failure using its exact refresh capability."""

        decision = decision or classify_outcome(outcome)
        if decision.action != "refresh":
            return decision
        if not credential_ref:
            raise ModelHubError("engine_down", status=503)
        refreshable = await self._engine_call(
            self.adapter.credential_supports_refresh(credential_ref)
        )
        if refreshable:
            if outcome.stream_started:
                return ResolutionDecision(
                    "surface",
                    error_code="stream_interrupted",
                )
            return decision
        if outcome.stream_started:
            return ResolutionDecision(
                "surface",
                reason="credential_revoked",
                error_code="stream_interrupted",
            )
        return ResolutionDecision("fallback", reason="credential_revoked")

    @staticmethod
    def _request_for_exact_reasoning_effort(
        request: Mapping[str, Any],
        source: ModelHubSourceConfig,
        model_id: str,
    ) -> ExactReasoningEffortRequest:
        model = next((item for item in source.models if item.id == model_id), None)
        declared = tuple(model.reasoning_efforts) if model is not None else ()
        supported = set(declared)

        payload = dict(request)
        changed = False
        stripped: list[str] = []

        def note_stripped(value: object) -> None:
            safe_value = _bounded_reasoning_effort_telemetry(value)
            if safe_value not in stripped:
                stripped.append(safe_value)

        direct = payload.get("reasoning_effort")
        if "reasoning_effort" in payload and not (
            isinstance(direct, str) and direct in supported
        ):
            payload.pop("reasoning_effort")
            changed = True
            note_stripped(direct)
        reasoning = payload.get("reasoning")
        nested = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
        if (
            isinstance(reasoning, Mapping)
            and "effort" in reasoning
            and not (isinstance(nested, str) and nested in supported)
        ):
            filtered_reasoning = dict(reasoning)
            filtered_reasoning.pop("effort")
            if filtered_reasoning:
                payload["reasoning"] = filtered_reasoning
            else:
                payload.pop("reasoning")
            changed = True
            note_stripped(nested)
        if not changed:
            return ExactReasoningEffortRequest(request=request)
        if isinstance(request, ModelHubRequest):
            filtered_request: Mapping[str, Any] = ModelHubRequest(
                payload,
                protocol=request.protocol,
                headers=request.headers,
            )
        else:
            filtered_request = payload
        safe_declared = _bounded_declared_effort_telemetry(declared)
        logger.info(
            "Stripped undeclared Model Hub reasoning effort(s) %s for source %s "
            "model %s; declared tiers: %s",
            stripped,
            source.id,
            model_id,
            list(safe_declared),
        )
        return ExactReasoningEffortRequest(
            request=filtered_request,
            stripped_efforts=tuple(stripped),
            declared_efforts=safe_declared,
        )

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
            projection_config, projection_resolution = config, resolution
            if supply_channel is not None:
                projection_config, projection_resolution = self._inspect_terminal_chain(
                    backend=cast(BackendName, backend),
                    model_id=model_id,
                )
            turn_outcome = self._produce_no_candidate_terminal_outcome(
                config=projection_config,
                resolution=projection_resolution,
            )
            facts = turn_outcome.supply_facts
            if facts is None:
                raise AssertionError("no-candidate outcome must carry supply facts")
            raise ModelHubError(
                "mapping_target_unavailable",
                status=409,
                supply_state=facts.supply_state,
                blockers=exact_hop_blockers(projection_resolution),
                turn_outcome=turn_outcome,
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
            exact_reasoning_request = self._request_for_exact_reasoning_effort(
                request,
                source,
                target_model,
            )
            exact_request = exact_reasoning_request.request
            if source.supply_channel == "native_cli":
                self._emit_switch(
                    agent=event_agent,
                    model_id=model_id,
                    failed_source=failed_source,
                    failed_reason=failed_reason,
                    source=source,
                )
                return ResolvedInvocation(
                    backend=cast(BackendName, backend),
                    requested_model_id=model_id,
                    source_id=source.id,
                    source_label=source.display_name,
                    model_id=target_model,
                    handle=None,
                    outcome=None,
                    supply_channel="native_cli",
                )
            await self._prepare_engine_for_demand(already_synced=engine_prepared)
            engine_prepared = True
            settlement_generation = self._reserve_settlement_generation(source.id)
            if attempt_observer is not None:
                attempt_observer(
                    source.id,
                    target_model,
                    "hub",
                    False,
                    None,
                    None,
                    exact_reasoning_request.stripped_efforts,
                    exact_reasoning_request.declared_efforts,
                )
            handle, outcome, cancelled = await self._invoke(
                source=source,
                model_id=target_model,
                request=exact_request,
                stream=stream,
                backend=backend,
            )
            if outcome is None:
                if cancelled is not None:
                    raise cancelled
                self._emit_switch(
                    agent=event_agent,
                    model_id=model_id,
                    failed_source=failed_source,
                    failed_reason=failed_reason,
                    source=source,
                )
                return ResolvedInvocation(
                    backend=cast(BackendName, backend),
                    requested_model_id=model_id,
                    source_id=source.id,
                    source_label=source.display_name,
                    model_id=target_model,
                    handle=handle,
                    outcome=None,
                    credential_ref=source.credential_ref,
                    settlement_generation=settlement_generation,
                )
            decision = await self._classify_source_outcome(source, outcome)
            if cancelled is not None:
                await self._settle_cancelled_attempt(
                    source=source,
                    source_model_id=target_model,
                    requested_model_id=model_id,
                    backend=cast(BackendName, backend),
                    outcome=outcome,
                    decision=decision,
                    settlement_generation=settlement_generation,
                    attempt_observer=attempt_observer,
                )
                raise cancelled
            if decision.action == "refresh":
                # The engine refreshes its credential internally; L2 retries the
                # exact same source once and never falls through on a second 401.
                handle, outcome, cancelled = await self._invoke(
                    source=source,
                    model_id=target_model,
                    request=exact_request,
                    stream=stream,
                    backend=backend,
                )
                if outcome is None:
                    if cancelled is not None:
                        raise cancelled
                    self._emit_switch(
                        agent=event_agent,
                        model_id=model_id,
                        failed_source=failed_source,
                        failed_reason=failed_reason,
                        source=source,
                    )
                    return ResolvedInvocation(
                        backend=cast(BackendName, backend),
                        requested_model_id=model_id,
                        source_id=source.id,
                        source_label=source.display_name,
                        model_id=target_model,
                        handle=handle,
                        outcome=None,
                        credential_ref=source.credential_ref,
                        settlement_generation=settlement_generation,
                    )
                decision = classify_outcome(outcome, refresh_attempted=True)
                if cancelled is not None:
                    await self._settle_cancelled_attempt(
                        source=source,
                        source_model_id=target_model,
                        requested_model_id=model_id,
                        backend=cast(BackendName, backend),
                        outcome=outcome,
                        decision=decision,
                        settlement_generation=settlement_generation,
                        attempt_observer=attempt_observer,
                    )
                    raise cancelled
            if attempt_observer is not None:
                attempt_observer(
                    source.id,
                    target_model,
                    "hub",
                    False,
                    outcome,
                    decision,
                    (),
                    (),
                )
            if decision.action == "return":
                self._emit_switch(
                    agent=event_agent,
                    model_id=model_id,
                    failed_source=failed_source,
                    failed_reason=failed_reason,
                    source=source,
                )
                return ResolvedInvocation(
                    backend=cast(BackendName, backend),
                    requested_model_id=model_id,
                    source_id=source.id,
                    source_label=source.display_name,
                    model_id=target_model,
                    handle=handle,
                    outcome=outcome,
                    credential_ref=source.credential_ref,
                    settlement_generation=settlement_generation,
                )
            if decision.action == "surface":
                source_transition_persisted: bool | None = None
                if outcome.stream_started and decision.reason is not None:
                    source_transition_persisted = False
                    _reason, source_transition_persisted = await self._settle_fallback_source(
                        source,
                        decision,
                        backend=cast(BackendName, backend),
                        model_id=model_id,
                        settlement_generation=settlement_generation,
                    )
                raise ModelHubError(
                    decision.error_code or outcome.error_code or "engine_down",
                    status=decision.downstream_status or (
                        outcome.http_status
                        if outcome.http_status is not None and 400 <= outcome.http_status <= 599
                        else 502
                    ),
                    turn_outcome=self._produce_attempt_terminal_outcome(
                        backend=cast(BackendName, backend),
                        model_id=model_id,
                        source_id=source.id,
                        source_model_id=target_model,
                        outcome=outcome,
                        decision=decision,
                        source_transition_persisted=source_transition_persisted,
                    ),
                )
            if decision.action == "fallback":
                event_reason, _persisted = await self._settle_fallback_source(
                    source,
                    decision,
                    backend=cast(BackendName, backend),
                    model_id=model_id,
                    settlement_generation=settlement_generation,
                )
                globally_blocked_source_ids.add(source.id)
                failed_source = source
                failed_reason = event_reason
                continue
            raise ModelHubError(
                decision.error_code or "engine_down",
                status=502,
                turn_outcome=ENGINE_DOWN_TURN_OUTCOME,
            )
        final_config, final_resolution = self._inspect_terminal_chain(
            backend=cast(BackendName, backend),
            model_id=model_id,
        )
        turn_outcome = self._produce_exhausted_terminal_outcome(
            config=final_config,
            resolution=final_resolution,
        )
        final_facts = turn_outcome.supply_facts
        if final_facts is None:
            raise AssertionError("exhausted outcome must carry supply facts")
        raise ModelHubError(
            "mapping_target_unavailable",
            status=503,
            supply_state=final_facts.supply_state,
            blockers=exact_hop_blockers(final_resolution),
            turn_outcome=turn_outcome,
        )


def create_default_service(
    *,
    adapter: Optional[EngineAdapter] = None,
    native_oauth_adapter: Optional[NativeOAuthAdapter] = None,
    requested_model_override: Optional[Callable[[BackendName], Optional[str]]] = None,
    selected_agent_override: Optional[Callable[[BackendName], Optional[str]]] = None,
    named_agents_override: Optional[
        Callable[[BackendName], list[tuple[str, Optional[str]]]]
    ] = None,
    cli_present_override: Optional[Callable[[BackendName], bool]] = None,
    cli_presence_refresh: Optional[
        Callable[[bool, tuple[BackendName, ...] | None], None]
    ] = None,
    backend_catalog_changed: Optional[
        Callable[[BackendName], Awaitable[None]]
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
        usage=BoundedUsageLedger(paths.get_state_dir() / "model_hub_usage.json"),
        native_oauth_adapter=native_oauth_adapter,
        oauth_flows=OAuthFlowRegistry(paths.get_state_dir() / "model_hub_oauth_flows.json"),
        revocations=CredentialRevocationJournal(paths.get_state_dir() / "model_hub_pending_revocations.json"),
        migration_claude_oauth_probe=claude_oauth_probe,
        requested_model_override=requested_model_override,
        selected_agent_override=selected_agent_override,
        named_agents_override=named_agents_override,
        cli_present_override=cli_present_override,
        cli_presence_refresh=cli_presence_refresh,
        backend_catalog_changed=backend_catalog_changed,
    )
